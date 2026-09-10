"""Executable contract for the guarded deployed-delivery preflight."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import textwrap
import threading
import venv
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
CHECKER = Path(
    os.environ.get("DELIVERY_CHECKER", ROOT / "scripts" / "check_delivery_deployment.py")
)
GUARDED_SHA = "fcc9328ff6e7f061879af2540c69717fc3061434"
SOURCE_SHA = "a" * 40
SECRET = "delivery-deployment-test-secret-never-print"
SOURCE_FILES = {
    "brain-v42/.mcp.json": b'{"mcpServers":{}}\n',
    "brain-v42/src/brain_v42/__init__.py": b'__version__ = "9.0.0"\n',
    "brain-v42/alembic.ini": b"[alembic]\nscript_location = brain_v42:alembic\n",
    "brain-v42/alembic/versions/053_delivery.py": b'revision = "053"\n',
    "brain-v42/uv.lock": b'version = 1\nrequires-python = ">=3.12"\n',
    "brain-v42/pyproject.toml": b'[project]\nname = "brain_v42"\nversion = "9.0.0"\n',
    "brain-v42/scripts/dream.sh": b"#!/usr/bin/env bash\nexit 0\n",
    "brain-v42/scripts/probe_model_liveness.py": b"raise SystemExit(0)\n",
    "brain-v42/scripts/rebuild_graph_projection.py": b"raise SystemExit(0)\n",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, data: str | bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(textwrap.dedent(data).lstrip(), encoding="utf-8")
    else:
        path.write_bytes(data)
    path.chmod(mode)


def _json(path: Path, value: dict[str, Any], *, mode: int = 0o600) -> None:
    _write(path, json.dumps(value, sort_keys=True), mode=mode)


@dataclass
class DeploymentCase:
    root: Path
    config: Path
    manifest: Path
    observer_env: Path
    health_endpoint: str = "http://127.0.0.1:18742/health"
    guard_result: str = "ahead"

    def _effective_runtime_fixture(
        self, *, manager_environment: dict[str, str] | None = None
    ) -> Path:
        if self.config.exists():
            units, processes = _effective_units(self)
        else:
            units, processes = {}, {}
        fixture = self.root / "effective-units.json"
        _json(
            fixture,
            {
                "manager_environment": manager_environment or {},
                "units": units,
                "processes": processes,
            },
            mode=0o600,
        )
        return fixture

    def run(
        self,
        *,
        extra_env: dict[str, str] | None = None,
        manager_environment: dict[str, str] | None = None,
        use_actual_guard: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        runtime_fixture = self._effective_runtime_fixture(manager_environment=manager_environment)
        bootstrap = self.root / "check-bootstrap.py"
        guard = "None" if use_actual_guard else "lambda *_: sys.argv[3]"
        _write(
            bootstrap,
            f"""
            import importlib.util
            import sys
            import json

            spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules["delivery_checker"] = module
            spec.loader.exec_module(module)
            runtime = json.loads(open(sys.argv[4], encoding="utf-8").read())

            def process_identity(pid):
                value = runtime["processes"][str(pid)]
                return module.ProcessIdentity(
                    executable=value["executable"],
                    argv=tuple(value["argv"]),
                    cwd=value["cwd"],
                    environment=value["environment"],
                )

            dependencies = module.PreflightDependencies(
                schema_revision=lambda _: "053",
                health=lambda _: {{"version": "9.0.0", "alembic_head": "053"}},
                systemd_properties=lambda unit: runtime["units"][unit],
                process_identity=process_identity,
                manager_environment=lambda: runtime["manager_environment"],
            )
            raise SystemExit(module.main(["--config", sys.argv[2]], guard_comparator={guard}, dependencies=dependencies))
            """,
            mode=0o700,
        )
        command = [
            sys.executable,
            str(bootstrap),
            str(CHECKER),
            str(self.config),
            self.guard_result,
            str(runtime_fixture),
        ]
        return subprocess.run(
            command,
            cwd=self.root,
            env={**os.environ, "PATH": os.environ["PATH"], **(extra_env or {})},
            text=True,
            capture_output=True,
            check=False,
        )

    def run_with_schema_revision(
        self, revision: str, *, manager_environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        runtime_fixture = self._effective_runtime_fixture(manager_environment=manager_environment)
        bootstrap = self.root / "check-schema-bootstrap.py"
        _write(
            bootstrap,
            """
            import importlib.util
            import sys
            import json

            spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules["delivery_checker"] = module
            spec.loader.exec_module(module)
            runtime = json.loads(open(sys.argv[4], encoding="utf-8").read())

            def process_identity(pid):
                value = runtime["processes"][str(pid)]
                return module.ProcessIdentity(
                    executable=value["executable"],
                    argv=tuple(value["argv"]),
                    cwd=value["cwd"],
                    environment=value["environment"],
                )
            dependencies = module.PreflightDependencies(
                schema_revision=lambda _: sys.argv[3],
                health=lambda _: {"version": "9.0.0", "alembic_head": "053"},
                systemd_properties=lambda unit: runtime["units"][unit],
                process_identity=process_identity,
                manager_environment=lambda: runtime["manager_environment"],
            )
            raise SystemExit(module.main(["--config", sys.argv[2]], guard_comparator=lambda *_: "ahead", dependencies=dependencies))
            """,
            mode=0o700,
        )
        return subprocess.run(
            [
                sys.executable,
                str(bootstrap),
                str(CHECKER),
                str(self.config),
                revision,
                str(runtime_fixture),
            ],
            cwd=self.root,
            env={**os.environ, "PATH": os.environ["PATH"]},
            text=True,
            capture_output=True,
            check=False,
        )

    def run_with_effective_units(
        self,
        units: dict[str, dict[str, str]],
        processes: dict[str, dict[str, object]],
        *,
        manager_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Exercise the public executable with fake systemd and /proc seams.

        The checker still runs in a fresh subprocess.  The seams model only the
        host boundaries that unit tests cannot safely mutate.
        """
        fixture = self.root / "effective-units.json"
        _json(
            fixture,
            {
                "manager_environment": manager_environment or {},
                "units": units,
                "processes": processes,
            },
            mode=0o600,
        )
        bin_dir = self.root / "controlled-bin"
        _write(
            bin_dir / "systemctl",
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys

            fixture = json.loads(open(os.environ["DELIVERY_SYSTEMD_FIXTURE"], encoding="utf-8").read())
            for key, value in fixture["units"][sys.argv[-1]].items():
                print(f"{key}={value}")
            """,
            mode=0o700,
        )
        bootstrap = self.root / "check-effective-units-bootstrap.py"
        _write(
            bootstrap,
            """
            import importlib.util
            import json
            import os
            import sys

            spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules["delivery_checker"] = module
            spec.loader.exec_module(module)
            fixture = json.loads(open(os.environ["DELIVERY_SYSTEMD_FIXTURE"], encoding="utf-8").read())

            def process_identity(pid):
                value = fixture["processes"][str(pid)]
                return module.ProcessIdentity(
                    executable=value["executable"],
                    argv=tuple(value["argv"]),
                    cwd=value["cwd"],
                    environment=value["environment"],
                )

            dependencies = module.PreflightDependencies(
                schema_revision=lambda _: "053",
                health=lambda _: {"version": "9.0.0", "alembic_head": "053"},
                systemd_properties=lambda unit: fixture["units"][unit],
                process_identity=process_identity,
                manager_environment=lambda: fixture["manager_environment"],
            )
            raise SystemExit(module.main(["--config", sys.argv[2]], guard_comparator=lambda *_: "ahead", dependencies=dependencies))
            """,
            mode=0o700,
        )
        return subprocess.run(
            [sys.executable, str(bootstrap), str(CHECKER), str(self.config)],
            cwd=self.root,
            env={
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "DELIVERY_SYSTEMD_FIXTURE": str(fixture),
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def config_document(self) -> dict[str, Any]:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def manifest_document(self) -> dict[str, Any]:
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def write_config(self, document: dict[str, Any]) -> None:
        _json(self.config, document)

    def write_manifest(self, document: dict[str, Any]) -> None:
        _json(self.manifest, document, mode=0o644)


@contextmanager
def _github_tls_fixture(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "delivery-preflight-fixture")])
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
    cert_path = tmp_path / "fixture-ca.pem"
    key_path = tmp_path / "fixture-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            status = 200 if self.headers.get("Authorization") == f"Bearer {SECRET}" else 401
            if status == 200 and self.path == "/repos/hawkixs/brain-v42":
                payload = b'{"id":1337360966,"full_name":"hawkixs/brain-v42"}'
            elif status == 200 and self.path == "/repos/hawkixs/brain-v42/pulls/42":
                payload = b'{"number":42}'
            elif status == 200 and "/check-runs" in self.path:
                payload = b'{"check_runs":[]}'
            elif status == 200 and self.path.endswith("/status"):
                payload = b'{"statuses":[]}'
            elif status == 200 and "/contents/.mcp.json?ref=" in self.path:
                payload = b'{"type":"file"}'
            elif status == 200 and self.path.startswith("/repos/hawkixs/brain-v42/compare/"):
                payload = b'{"status":"ahead"}'
            else:
                payload = b"{}"
                status = 404 if status == 200 else status
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_port}", cert_path
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _tar_source(path: Path, source_sha: str = SOURCE_SHA) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(
        path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": source_sha}
    ) as archive:
        for name, body in SOURCE_FILES.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, fileobj=io.BytesIO(body))
    path.chmod(0o644)


def _wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("brain_v42/__init__.py", '__version__ = "9.0.0"\n')
        archive.writestr(
            "brain_v42/alembic.ini", "[alembic]\nscript_location = brain_v42:alembic\n"
        )
        archive.writestr("brain_v42/alembic/versions/053_delivery.py", 'revision = "053"\n')
        archive.writestr("brain_v42-9.0.0.dist-info/METADATA", "Name: brain-v42\nVersion: 9.0.0\n")
    path.chmod(0o644)


@pytest.fixture
def deployment_case(tmp_path: Path) -> DeploymentCase:
    release = tmp_path / "release"
    artifacts = release / "artifacts"
    source = artifacts / "brain-v42.tar.gz"
    wheel = artifacts / "brain_v42-9.0.0-py3-none-any.whl"
    lock = artifacts / "uv.lock"
    interpreter = release / "venv" / "bin" / "python"
    _tar_source(source)
    _wheel(wheel)
    _write(lock, 'version = 1\nrequires-python = ">=3.12"\n', mode=0o644)
    venv.EnvBuilder(with_pip=False, symlinks=False).create(release / "venv")
    for archive_path, body in SOURCE_FILES.items():
        _write(release / archive_path, body, mode=0o644)
    for relative, body in {
        "venv/lib/python3.12/site-packages/brain_v42/__init__.py": b'__version__ = "9.0.0"\n',
        "venv/lib/python3.12/site-packages/brain_v42/alembic.ini": b"[alembic]\nscript_location = brain_v42:alembic\n",
        "venv/lib/python3.12/site-packages/brain_v42/alembic/versions/053_delivery.py": b'revision = "053"\n',
        "venv/lib/python3.12/site-packages/brain_v42-9.0.0.dist-info/METADATA": b"Name: brain_v42\nVersion: 9.0.0\n",
    }.items():
        _write(release / relative, body, mode=0o644)
    for directory in (release, *release.rglob("*")):
        if directory.is_dir():
            directory.chmod(0o755)

    manifest = release / "delivery-release.json"
    manifest_document = {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "minimum_guarded_sha": GUARDED_SHA,
        "version": "9.0.0",
        "source_archive": {"path": "artifacts/brain-v42.tar.gz", "sha256": _sha256(source)},
        "wheel": {"path": "artifacts/brain_v42-9.0.0-py3-none-any.whl", "sha256": _sha256(wheel)},
        "uv_lock": {"path": "artifacts/uv.lock", "sha256": _sha256(lock)},
        "interpreter": {"path": "venv/bin/python", "sha256": _sha256(interpreter)},
        "package_payload": [
            {
                "source_path": "brain-v42/alembic.ini",
                "wheel_path": "brain_v42/alembic.ini",
                "installed_path": "venv/lib/python3.12/site-packages/brain_v42/alembic.ini",
                "sha256": hashlib.sha256(
                    b"[alembic]\nscript_location = brain_v42:alembic\n"
                ).hexdigest(),
            },
            {
                "source_path": "brain-v42/src/brain_v42/__init__.py",
                "wheel_path": "brain_v42/__init__.py",
                "installed_path": "venv/lib/python3.12/site-packages/brain_v42/__init__.py",
                "sha256": hashlib.sha256(b'__version__ = "9.0.0"\n').hexdigest(),
            },
            {
                "source_path": "brain-v42/alembic/versions/053_delivery.py",
                "wheel_path": "brain_v42/alembic/versions/053_delivery.py",
                "installed_path": "venv/lib/python3.12/site-packages/brain_v42/alembic/versions/053_delivery.py",
                "sha256": hashlib.sha256(b'revision = "053"\n').hexdigest(),
            },
        ],
        "source_payload": [
            {
                "archive_path": "brain-v42/.mcp.json",
                "extracted_path": "brain-v42/.mcp.json",
                "sha256": hashlib.sha256(b'{"mcpServers":{}}\n').hexdigest(),
            },
            {
                "archive_path": "brain-v42/scripts/dream.sh",
                "extracted_path": "brain-v42/scripts/dream.sh",
                "sha256": hashlib.sha256(b"#!/usr/bin/env bash\nexit 0\n").hexdigest(),
            },
            {
                "archive_path": "brain-v42/scripts/probe_model_liveness.py",
                "extracted_path": "brain-v42/scripts/probe_model_liveness.py",
                "sha256": hashlib.sha256(b"raise SystemExit(0)\n").hexdigest(),
            },
            {
                "archive_path": "brain-v42/scripts/rebuild_graph_projection.py",
                "extracted_path": "brain-v42/scripts/rebuild_graph_projection.py",
                "sha256": hashlib.sha256(b"raise SystemExit(0)\n").hexdigest(),
            },
        ],
    }
    _json(manifest, manifest_document, mode=0o644)

    observer_env = tmp_path / "private" / "delivery-observer.env"
    _write(
        observer_env,
        "BRAIN_DELIVERY_ENABLED=true\n"
        f"BRAIN_DELIVERY_GITHUB_TOKEN={SECRET}\n"
        "BRAIN_DELIVERY_POSTGRES_URL=postgresql://unused:unused@127.0.0.1:5432/brain\n",
        mode=0o600,
    )
    observer_env.parent.chmod(0o755)
    config = tmp_path / "private" / "deployment-preflight.json"
    config_document = {
        "schema_version": 1,
        "release_manifest": str(manifest),
        "observer_env_file": str(observer_env),
        "health_endpoint": "http://127.0.0.1:18742/health",
        "required_schema_revision": "053",
        "repository": {"id": 1337360966, "slug": "hawkixs/brain-v42"},
        "probe_pull_request": 42,
        "mode": "dormant",
        "writers": {
            "brain-mcp-http.service": {
                "kind": "wheel_module",
                "module": "brain_v42.mcp.server",
                "must_be_active": True,
            },
            "brain-metrics.service": {
                "kind": "wheel_module",
                "module": "brain_v42.metrics",
                "must_be_active": True,
            },
            "brain-mcp-reaper.service": {
                "kind": "wheel_module",
                "module": "brain_v42.maintenance.reap_stale_mcp",
                "must_be_active": False,
                "trigger_unit": "brain-mcp-reaper.timer",
            },
            "brain-v42-dream.service": {
                "kind": "source_script",
                "source_path": "brain-v42/scripts/dream.sh",
                "must_be_active": False,
                "trigger_unit": "brain-v42-dream.timer",
            },
            "brain-v42-model-liveness.service": {
                "kind": "source_script",
                "source_path": "brain-v42/scripts/probe_model_liveness.py",
                "must_be_active": False,
                "trigger_unit": "brain-v42-model-liveness.timer",
            },
            "brain-v42-automation.service": {
                "kind": "wheel_module",
                "module": "brain_v42.automation",
                "must_be_active": False,
            },
            "brain-v42-graph-recon.service": {
                "kind": "source_script",
                "source_path": "brain-v42/scripts/rebuild_graph_projection.py",
                "must_be_active": False,
            },
            "brain-v42-delivery-observer.service": {
                "kind": "wheel_module",
                "module": "brain_v42.delivery_observer",
                "must_be_active": False,
            },
        },
    }
    _json(config, config_document)
    return DeploymentCase(tmp_path, config, manifest, observer_env)


def _receipt(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def _effective_units(
    case: DeploymentCase,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, object]]]:
    config = case.config_document()
    release = case.manifest.parent
    source_root = release / "brain-v42"
    interpreter = release / "venv" / "bin" / "python"
    units: dict[str, dict[str, str]] = {}
    processes: dict[str, dict[str, object]] = {}
    next_pid = 7101
    for name, writer in config["writers"].items():
        if writer["kind"] == "wheel_module":
            argv = [str(interpreter), "-m", writer["module"]]
            if name == "brain-v42-delivery-observer.service":
                argv.extend(["--env-file", str(case.observer_env)])
            environment = {
                "PATH": f"{release}/venv/bin:/usr/bin",
                "UV_NO_SYNC": "1",
                "PYTHONSAFEPATH": "1",
            }
        else:
            source = source_root / writer["source_path"].removeprefix("brain-v42/")
            argv = (
                ["/bin/bash", str(source)]
                if source.suffix == ".sh"
                else [str(interpreter), str(source)]
            )
            environment = {
                "PATH": f"{release}/venv/bin:/usr/bin",
                "UV_PROJECT_ENVIRONMENT": str(release / "venv"),
                "UV_NO_SYNC": "1",
                "PYTHONSAFEPATH": "1",
                "PYTHONPATH": str(source_root),
            }
        active = bool(writer["must_be_active"])
        pid = str(next_pid) if active else "0"
        if active:
            processes[pid] = {
                "executable": argv[0],
                "argv": argv,
                "cwd": str(source_root),
                "environment": environment,
            }
            next_pid += 1
        units[name] = {
            "ExecStart": " ".join(argv),
            "EnvironmentFiles": str(case.observer_env)
            if name == "brain-v42-delivery-observer.service"
            else "",
            "Environment": " ".join(f"{key}={value}" for key, value in environment.items()),
            "UnsetEnvironment": "",
            "ActiveState": "active" if active else "inactive",
            "MainPID": pid,
            "WorkingDirectory": str(source_root),
        }
    return units, processes


def test_preflight_accepts_a_complete_guarded_release_configuration(
    deployment_case: DeploymentCase,
) -> None:
    result = deployment_case.run()

    assert result.returncode == 0, result.stderr
    receipt = _receipt(result)
    assert receipt["status"] == "ok"
    assert receipt["source_sha"] == SOURCE_SHA
    assert receipt["schema_revision"] == "053"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_uses_only_the_selected_private_env_for_the_real_tls_guard(
    deployment_case: DeploymentCase,
) -> None:
    with _github_tls_fixture(deployment_case.root) as (origin, certificate):
        _write(
            deployment_case.observer_env,
            "BRAIN_DELIVERY_ENABLED=true\n"
            f"BRAIN_DELIVERY_GITHUB_TOKEN={SECRET}\n"
            f"BRAIN_DELIVERY_GITHUB_API_ORIGIN={origin}\n"
            "BRAIN_DELIVERY_POSTGRES_URL=postgresql://unused:unused@127.0.0.1:5432/brain\n",
            mode=0o600,
        )
        decoy_path = deployment_case.root / "decoy" / "delivery-observer.env"
        _write(
            decoy_path,
            "BRAIN_DELIVERY_ENABLED=true\n"
            "BRAIN_DELIVERY_GITHUB_TOKEN=decoy-default-path-token\n"
            "BRAIN_DELIVERY_POSTGRES_URL=postgresql://unused:unused@127.0.0.1:5432/brain\n",
            mode=0o600,
        )
        result = deployment_case.run(
            extra_env={
                "BRAIN_DELIVERY_OBSERVER_ENV_PATH": str(decoy_path),
                "SSL_CERT_FILE": str(certificate),
            },
            use_actual_guard=True,
        )

    assert result.returncode == 0, result.stderr
    receipt = _receipt(result)
    assert receipt["status"] == "ok"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_github_compare_for_a_different_numeric_repository(
    deployment_case: DeploymentCase,
) -> None:
    with _github_tls_fixture(deployment_case.root) as (origin, certificate):
        _write(
            deployment_case.observer_env,
            "BRAIN_DELIVERY_ENABLED=true\n"
            f"BRAIN_DELIVERY_GITHUB_TOKEN={SECRET}\n"
            f"BRAIN_DELIVERY_GITHUB_API_ORIGIN={origin}\n"
            "BRAIN_DELIVERY_POSTGRES_URL=postgresql://unused:unused@127.0.0.1:5432/brain\n",
            mode=0o600,
        )
        config = deployment_case.config_document()
        config["repository"]["id"] = 1
        deployment_case.write_config(config)
        result = deployment_case.run(
            extra_env={"SSL_CERT_FILE": str(certificate)}, use_actual_guard=True
        )

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "guarded_revision_unverified"
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["missing", "public", "symlink"])
def test_preflight_refuses_an_invalid_private_configuration(
    deployment_case: DeploymentCase, mutation: str
) -> None:
    if mutation == "missing":
        deployment_case.config.unlink()
    elif mutation == "public":
        deployment_case.config.chmod(0o644)
    else:
        target = deployment_case.config.with_name("different.json")
        shutil.copy2(deployment_case.config, target)
        deployment_case.config.unlink()
        deployment_case.config.symlink_to(target)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "private_config_invalid"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_an_unknown_configuration_schema(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["schema_version"] = 2
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "config_schema_invalid"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_declared_pre_053_schema_capability(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["required_schema_revision"] = "052"
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "schema_capability_unavailable"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_measured_pre_053_database_revision(
    deployment_case: DeploymentCase,
) -> None:
    result = deployment_case.run_with_schema_revision("052")

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "schema_capability_unavailable"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_health_response_for_a_different_release(
    deployment_case: DeploymentCase,
) -> None:
    runtime_fixture = deployment_case._effective_runtime_fixture()
    bootstrap = deployment_case.root / "check-health-bootstrap.py"
    _write(
        bootstrap,
        """
        import importlib.util
        import json
        import sys

        spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["delivery_checker"] = module
        spec.loader.exec_module(module)
        runtime = json.loads(open(sys.argv[3], encoding="utf-8").read())

        def process_identity(pid):
            value = runtime["processes"][str(pid)]
            return module.ProcessIdentity(
                executable=value["executable"],
                argv=tuple(value["argv"]),
                cwd=value["cwd"],
                environment=value["environment"],
            )

        dependencies = module.PreflightDependencies(
            schema_revision=lambda _: "053",
            health=lambda _: {"version": "wrong", "alembic_head": "053"},
            systemd_properties=lambda unit: runtime["units"][unit],
            process_identity=process_identity,
            manager_environment=lambda: {},
        )
        raise SystemExit(module.main(["--config", sys.argv[2]], guard_comparator=lambda *_: "ahead", dependencies=dependencies))
        """,
        mode=0o700,
    )
    result = subprocess.run(
        [
            sys.executable,
            str(bootstrap),
            str(CHECKER),
            str(deployment_case.config),
            str(runtime_fixture),
        ],
        cwd=deployment_case.root,
        env={**os.environ, "PATH": os.environ["PATH"]},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "service_health_unavailable"
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize("mutation", ["public", "symlink"])
def test_preflight_refuses_an_invalid_observer_private_environment(
    deployment_case: DeploymentCase, mutation: str
) -> None:
    if mutation == "public":
        deployment_case.observer_env.chmod(0o644)
    else:
        target = deployment_case.observer_env.with_name("different.env")
        shutil.copy2(deployment_case.observer_env, target)
        deployment_case.observer_env.unlink()
        deployment_case.observer_env.symlink_to(target)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "private_environment_invalid"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_release_path_that_escapes_the_manifest_root(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    manifest["wheel"]["path"] = "../outside.whl"
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_path_unsafe"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_an_internal_release_symlink(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    wheel = deployment_case.manifest.parent / manifest["wheel"]["path"]
    alias = wheel.with_name("internal-alias.whl")
    alias.symlink_to(wheel.name)
    manifest["wheel"]["path"] = str(alias.relative_to(deployment_case.manifest.parent))
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_path_unsafe"


def test_preflight_refuses_a_release_file_below_a_writable_parent(
    deployment_case: DeploymentCase,
) -> None:
    artifacts = deployment_case.manifest.parent / "artifacts"
    artifacts.chmod(0o777)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_path_unsafe"


def test_preflight_refuses_a_mismatched_release_artifact_hash(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    manifest["uv_lock"]["sha256"] = "0" * 64
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_same_hash_source_outside_the_canonical_package_path(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    source = deployment_case.manifest.parent / manifest["source_archive"]["path"]
    misleading = dict(SOURCE_FILES)
    misleading["brain-v42/not-the-package/__init__.py"] = SOURCE_FILES[
        "brain-v42/src/brain_v42/__init__.py"
    ]
    with tarfile.open(
        source, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SOURCE_SHA}
    ) as archive:
        for name, body in misleading.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, fileobj=io.BytesIO(body))
    source.chmod(0o644)
    manifest["source_archive"]["sha256"] = _sha256(source)
    manifest["package_payload"][0]["source_path"] = "brain-v42/not-the-package/__init__.py"
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"


def test_preflight_refuses_a_manifested_binary_that_does_not_import_the_release(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    replacement = deployment_case.manifest.parent / "artifacts" / "not-python"
    _write(replacement, Path("/bin/true").read_bytes(), mode=0o755)
    manifest["interpreter"] = {
        "path": str(replacement.relative_to(deployment_case.manifest.parent)),
        "sha256": _sha256(replacement),
    }
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"


def test_preflight_compares_the_retained_lock_to_the_archived_lock(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    source = deployment_case.manifest.parent / manifest["source_archive"]["path"]
    changed = dict(SOURCE_FILES)
    changed["brain-v42/uv.lock"] = b'version = 2\nrequires-python = ">=3.12"\n'
    with tarfile.open(
        source, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SOURCE_SHA}
    ) as archive:
        for name, body in changed.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, fileobj=io.BytesIO(body))
    source.chmod(0o644)
    manifest["source_archive"]["sha256"] = _sha256(source)
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"


def test_preflight_refuses_an_archive_without_the_guarded_mcp_configuration(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    source = deployment_case.manifest.parent / manifest["source_archive"]["path"]
    without_mcp = {
        name: body for name, body in SOURCE_FILES.items() if name != "brain-v42/.mcp.json"
    }
    with tarfile.open(
        source, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": SOURCE_SHA}
    ) as archive:
        for name, body in without_mcp.items():
            member = tarfile.TarInfo(name)
            member.size = len(body)
            member.mode = 0o644
            archive.addfile(member, fileobj=io.BytesIO(body))
    source.chmod(0o644)
    manifest["source_archive"]["sha256"] = _sha256(source)
    manifest["source_payload"] = [
        item for item in manifest["source_payload"] if item["archive_path"] != "brain-v42/.mcp.json"
    ]
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"


def test_preflight_refuses_an_unmanifested_installed_package_file(
    deployment_case: DeploymentCase,
) -> None:
    _write(
        deployment_case.manifest.parent
        / "venv/lib/python3.12/site-packages/brain_v42/unexpected.py",
        "unexpected = True\n",
        mode=0o644,
    )

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "installed_package_unexpected"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_allows_only_derived_python_caches_beside_verified_sources(
    deployment_case: DeploymentCase,
) -> None:
    _write(
        deployment_case.manifest.parent
        / "venv/lib/python3.12/site-packages/brain_v42/__pycache__/__init__.cpython-312.pyc",
        b"derived bytecode",
        mode=0o644,
    )

    result = deployment_case.run()

    assert result.returncode == 0, result.stderr


def test_preflight_refuses_an_unguarded_source_revision(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    source = deployment_case.manifest.parent / manifest["source_archive"]["path"]
    _tar_source(source, "b" * 40)
    manifest["source_sha"] = "b" * 40
    manifest["source_archive"]["sha256"] = _sha256(source)
    deployment_case.write_manifest(manifest)
    deployment_case.guard_result = "behind"

    before = {
        path: path.read_bytes()
        for path in (deployment_case.manifest, deployment_case.observer_env, deployment_case.config)
    }
    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "guarded_revision_unverified"
    assert {path: path.read_bytes() for path in before} == before
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_dormant_mcp_or_metrics_unit(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["writers"]["brain-mcp-http.service"]["must_be_active"] = False
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "required_unit_inventory_invalid"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_requires_an_active_observer_in_canary_mode(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["mode"] = "canary"
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "required_unit_inventory_invalid"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_writer_without_a_guarded_launch_mapping(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["writers"]["brain-v42-dream.service"]["kind"] = "shell_wrapper"
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_checks_each_effective_unit_and_the_active_processes(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode == 0, result.stderr
    assert _receipt(result)["status"] == "ok"
    assert SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("unit", "property_name", "value"),
    [
        ("brain-v42-automation.service", "ExecStart", "/bin/echo mutable-checkout"),
        ("brain-v42-dream.service", "Environment", "PATH=/usr/bin"),
        ("brain-metrics.service", "ActiveState", "inactive"),
    ],
)
def test_preflight_refuses_mismatched_effective_unit_properties(
    deployment_case: DeploymentCase, unit: str, property_name: str, value: str
) -> None:
    units, processes = _effective_units(deployment_case)
    units[unit][property_name] = value

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_an_active_process_from_another_release(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    processes[units["brain-mcp-http.service"]["MainPID"]]["executable"] = "/usr/bin/python3"

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"
    assert SECRET not in result.stdout + result.stderr


def test_preflight_refuses_a_source_script_with_the_right_argv_but_wrong_executable(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["writers"]["brain-v42-dream.service"]["must_be_active"] = True
    deployment_case.write_config(config)
    units, processes = _effective_units(deployment_case)
    processes[units["brain-v42-dream.service"]["MainPID"]]["executable"] = "/bin/true"

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_a_command_that_only_mentions_the_expected_module(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    release = deployment_case.manifest.parent
    units["brain-v42-automation.service"]["ExecStart"] = (
        f"/bin/true {release}/venv/bin/python -m brain_v42.automation"
    )

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_a_source_script_outside_the_verified_archive_payload(
    deployment_case: DeploymentCase,
) -> None:
    config = deployment_case.config_document()
    config["writers"]["brain-v42-dream.service"]["source_path"] = "brain-v42/scripts/unverified.sh"
    deployment_case.write_config(config)

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_controlled_environment_overridden_by_an_environment_file(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    environment_file = deployment_case.root / "private" / "source-runtime.env"
    _write(environment_file, "PYTHONPATH=/mutable/checkout\n", mode=0o600)
    units["brain-v42-dream.service"]["EnvironmentFiles"] = str(environment_file)

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_a_quoted_environment_file_value_that_only_looks_expected(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    environment_file = deployment_case.root / "private" / "quoted-source-runtime.env"
    expected = deployment_case.manifest.parent / "brain-v42"
    _write(environment_file, f"PYTHONPATH=\"'{expected}'\"\n", mode=0o600)
    units["brain-v42-dream.service"]["EnvironmentFiles"] = str(environment_file)

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


@pytest.mark.parametrize(
    "suffix",
    [
        ["--env-file", "/private/decoy.env"],
        ["--once"],
        ["--env-file", "/private/decoy.env", "--env-file", "/private/decoy.env"],
    ],
)
def test_preflight_refuses_an_observer_argv_that_is_not_the_configured_daemon(
    deployment_case: DeploymentCase, suffix: list[str]
) -> None:
    config = deployment_case.config_document()
    config["mode"] = "canary"
    config["writers"]["brain-v42-delivery-observer.service"]["must_be_active"] = True
    deployment_case.write_config(config)
    units, processes = _effective_units(deployment_case)
    unit = "brain-v42-delivery-observer.service"
    pid = units[unit]["MainPID"]
    base = processes[pid]["argv"]
    assert isinstance(base, list)
    processes[pid]["argv"] = [*base, *suffix]
    units[unit]["ExecStart"] = " ".join(processes[pid]["argv"])

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_systemd_path_that_disagrees_with_its_argv(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    command = units["brain-v42-automation.service"]["ExecStart"]
    units["brain-v42-automation.service"]["ExecStart"] = (
        f"{{ path=/bin/true ; argv[]={command} ; ignore_errors=no ; }}"
    )

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_refuses_pythonhome_in_a_unit_environment(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    units["brain-v42-automation.service"]["Environment"] += " PYTHONHOME=/mutable/python"

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PYTHONHOME", "/mutable/python"),
        ("PYTHONPATH", "/mutable/checkout"),
    ],
)
def test_preflight_refuses_import_controls_inherited_from_the_manager(
    deployment_case: DeploymentCase, name: str, value: str
) -> None:
    units, processes = _effective_units(deployment_case)

    result = deployment_case.run_with_effective_units(
        units, processes, manager_environment={name: value}
    )

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_uses_a_unit_environment_in_preference_to_the_manager(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)

    result = deployment_case.run_with_effective_units(
        units, processes, manager_environment={"PATH": "/mutable/bin"}
    )

    assert result.returncode == 0, result.stderr
    assert _receipt(result)["status"] == "ok"


@pytest.mark.parametrize("unset", ["PYTHONPATH", "PYTHONPATH=/release/brain-v42"])
def test_preflight_refuses_unset_environment_that_touches_an_import_control(
    deployment_case: DeploymentCase, unset: str
) -> None:
    units, processes = _effective_units(deployment_case)
    units["brain-v42-dream.service"]["UnsetEnvironment"] = unset

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "writer_launch_unsupported"


def test_preflight_accepts_a_configured_observer_environment_file_with_systemd_options(
    deployment_case: DeploymentCase,
) -> None:
    units, processes = _effective_units(deployment_case)
    units["brain-v42-delivery-observer.service"]["EnvironmentFiles"] = (
        f"{deployment_case.observer_env} (ignore_errors=no)"
    )

    result = deployment_case.run_with_effective_units(units, processes)

    assert result.returncode == 0, result.stderr
    assert _receipt(result)["status"] == "ok"


def test_systemd_reader_keeps_the_user_bus_environment(tmp_path: Path) -> None:
    bootstrap = tmp_path / "systemd-reader-bootstrap.py"
    _write(
        bootstrap,
        """
        import importlib.util
        import os
        import sys
        from types import SimpleNamespace

        spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["delivery_checker"] = module
        spec.loader.exec_module(module)

        def controlled_run(*_args, **kwargs):
            runtime = f"/run/user/{os.getuid()}"
            assert kwargs["env"]["XDG_RUNTIME_DIR"] == runtime
            assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime}/bus"
            return SimpleNamespace(
                returncode=0,
                stdout="ExecStart=/bin/true\\nEnvironmentFiles=\\nEnvironment=\\nActiveState=inactive\\nMainPID=0\\nWorkingDirectory=/tmp\\n",
            )

        module.subprocess.run = controlled_run
        module._read_systemd_properties("brain-metrics.service")
        print("ok")
        """,
        mode=0o700,
    )
    result = subprocess.run(
        [sys.executable, str(bootstrap), str(CHECKER)],
        env={
            **os.environ,
            "XDG_RUNTIME_DIR": "/run/user/1234",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1234/bus",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


def test_systemd_reader_treats_an_unset_environment_files_property_as_empty(tmp_path: Path) -> None:
    bootstrap = tmp_path / "systemd-empty-environment-files.py"
    _write(
        bootstrap,
        """
        import importlib.util
        import sys
        from types import SimpleNamespace

        spec = importlib.util.spec_from_file_location("delivery_checker", sys.argv[1])
        module = importlib.util.module_from_spec(spec)
        sys.modules["delivery_checker"] = module
        spec.loader.exec_module(module)
        module.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="ExecStart=/bin/true\\nEnvironment=\\nActiveState=inactive\\nMainPID=0\\nWorkingDirectory=/tmp\\n",
        )
        assert module._read_systemd_properties("brain-metrics.service")["EnvironmentFiles"] == ""
        print("ok")
        """,
        mode=0o700,
    )
    result = subprocess.run(
        [sys.executable, str(bootstrap), str(CHECKER)], text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"


# --- pyvenv.cfg: the file that names the base interpreter ----------------------
#
# Measured 2026-09-10: the release venv ships no standard library. `pyvenv.cfg`
# is what points `sys.base_prefix` at the tree `ssl.py` and `hashlib.py` are
# actually loaded from, it is 434 bytes of plain text inside a tree the runtime
# uid can write, and no manifest hashed it. So a guarded release could be
# repointed at any interpreter tree with one `sed -i` on the `home =` line while
# every other manifest digest still matched and the preflight still said ok.
#
# Hashing it is only half a fix; the preflight has to compare it. These three
# tests pin the compare, the tamper, and — the one that matters operationally —
# that the six releases built before this change keep passing.


def test_preflight_accepts_a_manifested_pyvenv_cfg_that_matches(
    deployment_case: DeploymentCase,
) -> None:
    manifest = deployment_case.manifest_document()
    pyvenv = deployment_case.manifest.parent / "venv" / "pyvenv.cfg"
    manifest["pyvenv_cfg"] = {"path": "venv/pyvenv.cfg", "sha256": _sha256(pyvenv)}
    deployment_case.write_manifest(manifest)

    result = deployment_case.run()

    assert result.returncode == 0, result.stderr


def test_preflight_refuses_a_pyvenv_cfg_edited_after_the_build(
    deployment_case: DeploymentCase,
) -> None:
    """The post-build edit — the whole reason this entry exists.

    THE TAMPER IS DELIBERATELY BENIGN, and the assertion below the run is why.
    Rewriting `home =` to a bogus path was the first thing I tried and it made
    this test a false witness: the interpreter then dies during init with no
    Python frame, `_validate_interpreter` cannot run its probe, and the run
    fails on `release_artifact_mismatch` for a completely different reason —
    green while the digest was never compared. A comment line changes the bytes
    and nothing else, so the only thing that can refuse it is the compare.
    """
    manifest = deployment_case.manifest_document()
    pyvenv = deployment_case.manifest.parent / "venv" / "pyvenv.cfg"
    manifest["pyvenv_cfg"] = {"path": "venv/pyvenv.cfg", "sha256": _sha256(pyvenv)}
    deployment_case.write_manifest(manifest)
    pyvenv.write_text(
        pyvenv.read_text(encoding="utf-8") + "# repointed\n",
        encoding="utf-8",
    )
    interpreter = deployment_case.manifest.parent / "venv" / "bin" / "python"
    probe = subprocess.run(  # noqa: S603
        [str(interpreter), "-I", "-c", "print('alive')"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0 and "alive" in probe.stdout, (
        "the tamper broke the interpreter, so a refusal below would prove "
        "nothing about the pyvenv.cfg digest — pick a benign edit"
    )

    result = deployment_case.run()

    assert result.returncode != 0
    assert _receipt(result)["failure"] == "release_artifact_mismatch"


def test_preflight_still_accepts_a_release_built_before_pyvenv_cfg_was_hashed(
    deployment_case: DeploymentCase,
) -> None:
    """Backward compatibility is the operational half of this change.

    Six releases exist that carry no `pyvenv_cfg` entry, and one of them is the
    documented rollback target. Making the key mandatory would turn this
    hardening into an outage the first time someone rolls back under pressure —
    the preflight would report `config_schema_invalid` on an artifact that is
    exactly as trustworthy as it was yesterday.
    """
    manifest = deployment_case.manifest_document()
    assert "pyvenv_cfg" not in manifest

    result = deployment_case.run()

    assert result.returncode == 0, result.stderr
