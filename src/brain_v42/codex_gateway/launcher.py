"""Secure container launcher for the Codex management gateway."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import uvicorn
from pydantic import SecretStr

from brain_v42.codex_gateway.app import create_production_app
from brain_v42.codex_gateway.auth import require_non_empty_token
from brain_v42.config import get_settings

_TOKEN_KEY = "BRAIN_CODEX_GATEWAY_TOKEN"
_TOKEN_FILE_KEY = "BRAIN_CODEX_GATEWAY_TOKEN_FILE"


def load_gateway_token_file(path: Path) -> SecretStr:
    """Load one strong token from an owned, regular 0600 environment file."""
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise RuntimeError("Codex gateway token file is unavailable") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError("Codex gateway token file must be a regular file")
    if file_stat.st_uid != os.getuid():
        raise RuntimeError("Codex gateway token file must be owned by the service user")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise RuntimeError("Codex gateway token file must have exact mode 0600")

    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise RuntimeError("Codex gateway token file cannot be read") from exc

    values: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or key != _TOKEN_KEY:
            raise RuntimeError("Codex gateway token file must contain only the token key")
        values.append(value.strip())
    if len(values) != 1:
        raise RuntimeError("Codex gateway token file must contain the token key exactly once")

    token = SecretStr(values[0])
    require_non_empty_token(token)
    return token


def run() -> None:
    """Validate the mounted secret, compose the app, then start uvicorn."""
    token_file = os.environ.get(_TOKEN_FILE_KEY, "").strip()
    if not token_file:
        raise RuntimeError(f"{_TOKEN_FILE_KEY} must point to the mounted private file")
    token = load_gateway_token_file(Path(token_file))

    os.environ[_TOKEN_KEY] = token.get_secret_value()
    get_settings.cache_clear()
    try:
        settings = get_settings()
        app = create_production_app(settings)
    finally:
        os.environ.pop(_TOKEN_KEY, None)

    uvicorn.run(
        app,
        host=settings.brain_codex_gateway_host,
        port=settings.brain_codex_gateway_port,
    )


if __name__ == "__main__":
    run()
