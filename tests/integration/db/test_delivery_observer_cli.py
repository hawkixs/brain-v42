"""Real subprocess, TLS socket, PostgreSQL and evidence publication, without MCP."""

import asyncio
import ipaddress
import json
import signal
import ssl
import sys
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from brain_v42.db.tables import delivery_receipts
from brain_v42.delivery_observer.ownership import ObserverOwnership
from brain_v42.delivery_observer.runtime import ObservationRunResult
from tests.integration.db.delivery_observer_cases import ObserverCase
from tests.integration.db.delivery_observer_cases import (
    observer_queue_isolation as observer_queue_isolation,
)
from tests.unit.delivery_observer.test_cli import TOKEN, cli_environment

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("observer_queue_isolation"),
]


def tls_files(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "observer-fixture")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "fixture-ca.pem", tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path


@asynccontextmanager
async def cli_case(engine, factory, tmp_path, *, legacy_pg=False):
    case = ObserverCase(engine, factory)
    context, cert_path = tls_files(tmp_path)
    handlers = set()
    processes = []

    async def serve(reader, writer):
        current = asyncio.current_task()
        handlers.add(current)
        response = None
        try:
            headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            first, *lines = headers.decode("ascii").split("\r\n")
            method, target, _ = first.split(" ")
            assert method == "GET"
            header_values = {
                key.lower(): value.strip()
                for line in lines
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            assert header_values["authorization"] == f"Bearer {TOKEN}"
            response = await case.handle(httpx.Request(method, "https://127.0.0.1" + target))
            body = await response.aread()
            # GitHub record URLs belong to this TLS fixture's configured origin.
            body = body.replace(b"https://api.github.com", api_origin.encode())
            await response.aclose()
            response = None
            writer.write(
                f"HTTP/1.1 {case.status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        finally:
            if response is not None:
                await response.aclose()
            writer.close()
            with suppress(ConnectionError, ssl.SSLError):
                await writer.wait_closed()
            handlers.discard(current)

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=context, limit=16384)
    port = server.sockets[0].getsockname()[1]
    api_origin = f"https://127.0.0.1:{port}"
    config = tmp_path / "observer.env"
    pg_url = engine.url.render_as_string(hide_password=False)
    config.write_text(
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n"
        f"BRAIN_DELIVERY_GITHUB_API_ORIGIN={api_origin}\n"
        + ("" if legacy_pg else f"BRAIN_DELIVERY_POSTGRES_URL={pg_url}\n")
    )
    config.chmod(0o600)
    # A real import guard in the subprocess proves that composition does not
    # depend on MCP, Dream, graph clients or the semantic ingestor being usable.
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "class ForbidHeavyServices:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.startswith(('brain_v42.mcp', 'brain_v42.dream', 'brain_v42.services', 'brain_v42.automation', 'neo4j', 'litellm')):\n"
        "            raise AssertionError('forbidden observer dependency')\n"
        "sys.meta_path.insert(0, ForbidHeavyServices())\n"
    )
    env = cli_environment()
    env.update(SSL_CERT_FILE=str(cert_path), PYTHONPATH=str(tmp_path), GRAPH_ENABLED="true")
    if legacy_pg:
        env["POSTGRES_URL"] = pg_url

    async def start(*args):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "brain_v42.delivery_observer",
            "--env-file",
            str(config),
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(process)
        return process

    async def finish(process):
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert not stderr, stderr.decode()
        assert TOKEN.encode() not in stdout and pg_url.encode() not in stdout
        assert b"private fixture body" not in stdout
        payload = json.loads(stdout)
        if "collected" in payload:
            ObservationRunResult.model_validate(payload)
        return process.returncode, payload

    try:
        yield case, start, finish
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("legacy_pg", [False, True])
async def test_real_once_publishes_receipts_then_restart_is_idle(
    engine, session_factory, tmp_path, legacy_pg
):
    async with cli_case(engine, session_factory, tmp_path, legacy_pg=legacy_pg) as (
        case,
        start,
        finish,
    ):
        ticket, binding, _ = await case.create()
        code, result = await finish(await start("--once", "--project-key", "brain-v42"))
        assert code == result["exit_code"] == 0, (
            case.requests,
            [row["error_code"] for row in (await case.state(binding))[1]],
        )
        assert result["collected"] == 1 and result["failed"] == 0
        assert result["last_success_at"] is not None
        row, confirmations, snapshots, _ = await case.state(binding)
        assert row["latest_success_confirmation_id"] == confirmations[0]["id"] and snapshots == 1
        async with session_factory() as session:
            receipts = (
                (
                    await session.execute(
                        sa.select(delivery_receipts).where(
                            delivery_receipts.c.ticket_id == ticket.id
                        )
                    )
                )
                .mappings()
                .all()
            )
        assert {row["milestone"] for row in receipts} == {"integration", "fulfilled"}
        count = len(case.requests)
        code, result = await finish(await start("--once"))
        assert code == result["collected"] == 0 and len(case.requests) == count


async def test_real_once_provider_failure_is_nonzero_and_persisted_without_secret(
    engine, session_factory, tmp_path
):
    async with cli_case(engine, session_factory, tmp_path) as (case, start, finish):
        _, binding, _ = await case.create()
        case.status = 503
        code, result = await finish(await start("--once"))
        assert code == result["exit_code"] == 1 and result["failed"] == 1
        assert len(case.requests) == 1
        _, confirmations, snapshots, _ = await case.state(binding)
        assert len(confirmations) == 1 and snapshots == 0
        assert confirmations[0]["error_code"] == "provider_unavailable"


async def test_real_duplicate_process_cannot_collect(engine, session_factory, tmp_path):
    async with cli_case(engine, session_factory, tmp_path) as (case, start, finish):
        await case.create()
        owner = ObserverOwnership(engine)
        assert await owner.acquire()
        try:
            code, result = await finish(await start("--once"))
            assert code == result["exit_code"] == 2 and not case.requests
        finally:
            await owner.release()


@pytest.mark.parametrize("once", [False, True])
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
async def test_actual_sigterm_during_collection_releases_owner_and_restart_works(
    engine, session_factory, tmp_path, once, stop_signal
):
    async with cli_case(engine, session_factory, tmp_path) as (case, start, finish):
        _, binding, _ = await case.create()
        case.block = True
        process = await start(*(["--once"] if once else []))
        await asyncio.wait_for(case.http_entered.wait(), 10)
        process.send_signal(stop_signal)
        code, result = await finish(process)
        assert code == result["exit_code"] == 0
        assert not (await case.state(binding))[1]
        next_owner = ObserverOwnership(engine)
        assert await next_owner.acquire()
        await next_owner.release()
        case.allow_http.set()
        case.block = False
        code, result = await finish(await start("--once"))
        assert code == 0 and result["collected"] == 1


async def test_actual_lost_backend_during_collection_exits_nonzero_without_publication(
    engine, session_factory, tmp_path
):
    async with cli_case(engine, session_factory, tmp_path) as (case, start, finish):
        _, binding, _ = await case.create()
        case.block = True
        process = await start("--once")
        await asyncio.wait_for(case.http_entered.wait(), 10)
        async with engine.begin() as connection:
            pid = await connection.scalar(
                sa.text(
                    "SELECT pid FROM pg_locks WHERE locktype='advisory' AND granted AND objid=:key"
                ),
                {"key": ObserverOwnership.LOCK_KEY & 0xFFFFFFFF},
            )
            assert pid is not None
            assert await connection.scalar(
                sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
            )
        case.allow_http.set()
        code, result = await finish(process)
        assert code == result["exit_code"] == 2
        assert not (await case.state(binding))[1]
