"""Observe persisted delivery work with dedicated GitHub credentials and PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import ssl
from pathlib import Path
from typing import NoReturn

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.auth import GitHubAuthProvider
from brain_v42.delivery_observer.config import load_observer_settings
from brain_v42.delivery_observer.github import GitHubClient
from brain_v42.delivery_observer.ownership import ObserverOwnership
from brain_v42.delivery_observer.runtime import DeliveryObserverRuntime
from brain_v42.delivery_observer.transport import GitHubTransport
from brain_v42.models.delivery import ReceiptIssuerProvenance
from brain_v42.repositories.pg_delivery_evidence import PgDeliveryEvidenceRepo


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse otherwise echoes arbitrary invalid command-line values.
        raise ValueError("invalid observer arguments")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = _SafeParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Observe one bounded pass and exit.")
    parser.add_argument("--project-key", help="Restrict a --once pass to one executor project.")
    parser.add_argument("--env-file", type=Path, help="Dedicated private observer configuration.")
    args = parser.parse_args(argv)
    if args.project_key is not None and (
        not args.once or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,49}", args.project_key)
    ):
        raise ValueError("invalid observer project filter")
    return args


async def _execute(
    settings: DeliverySettings, *, once: bool, project_key: str | None, stop: asyncio.Event
) -> dict[str, object]:
    assert settings.postgres_url is not None
    engine = create_async_engine(
        settings.postgres_url.get_secret_value(),
        poolclass=NullPool,
        echo=False,
        hide_parameters=True,
        connect_args={
            "timeout": 10,
            "command_timeout": 15,
            "server_settings": {"application_name": "brain-v42-delivery-observer"},
        },
    )
    owner = ObserverOwnership(engine)
    try:
        # Standard system CA verification also honors an explicit SSL_CERT_FILE;
        # ambient proxy/auth settings are deliberately not inherited by HTTPX.
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(),
            trust_env=False,
            timeout=settings.request_timeout_seconds,
            limits=httpx.Limits(max_connections=settings.max_concurrent_requests),
        ) as http:
            transport = GitHubTransport(http, settings)
            auth = GitHubAuthProvider(settings, transport)
            runtime = DeliveryObserverRuntime(
                settings=settings,
                owner=owner,
                client=GitHubClient(http, settings, auth),
                evidence_repository=PgDeliveryEvidenceRepo(
                    async_sessionmaker(engine, expire_on_commit=False),
                    settings=settings,
                    observer_provenance=ReceiptIssuerProvenance(
                        issuer_project="brain-v42",
                        issuer_identity="brain-v42-delivery-observer",
                        issuer_kind="observer",
                    ),
                ),
            )
            if once:
                return (await runtime.run_once(project_key)).model_dump(mode="json")
            return {"exit_code": await runtime.run(stop)}
    finally:
        try:
            await owner.release()
        finally:
            await engine.dispose()


async def _run(settings: DeliverySettings, args: argparse.Namespace) -> dict[str, object]:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    signals = (signal.SIGTERM, signal.SIGINT)
    for item in signals:
        loop.add_signal_handler(item, stop.set)
    worker = asyncio.create_task(
        _execute(settings, once=args.once, project_key=args.project_key, stop=stop)
    )
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait((worker, stopping), return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            return worker.result()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        return {"exit_code": 0, "stopped": True}
    finally:
        for item in signals:
            loop.remove_signal_handler(item)
        for task in (worker, stopping):
            if not task.done():
                task.cancel()
        await asyncio.gather(worker, stopping, return_exceptions=True)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        path = (
            args.env_file
            or Path(
                os.environ.get(
                    "BRAIN_DELIVERY_OBSERVER_ENV_PATH", "~/.config/brain-v42/delivery-observer.env"
                )
            ).expanduser()
        )
        settings = load_observer_settings(path)
    except Exception:
        print(json.dumps({"exit_code": 2, "error_code": "observer_configuration_invalid"}))
        return 2
    try:
        result = asyncio.run(_run(settings, args))
    except Exception:
        result = {"exit_code": 2, "error_code": "observer_unavailable"}
    print(json.dumps(result))
    code = result["exit_code"]
    return code if isinstance(code, int) and code in {0, 1, 2} else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
