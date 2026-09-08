"""Dedicated PAT or GitHub App installation credentials, with no interactive fallback."""

from __future__ import annotations

import asyncio
import os
import stat
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import jwt
from pydantic import SecretStr

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.transport import GitHubTransport, ProviderError


def read_private_file(path: Path) -> bytes:
    """Open every component without following links, then check the actual file."""
    descriptor: int | None = None
    directory: int | None = None
    try:
        absolute = path.absolute()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open("/", directory_flags)
        for component in absolute.parts[1:-1]:
            following = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = following
        descriptor = os.open(
            absolute.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 0 < metadata.st_size <= 65536
        ):
            raise ProviderError("provider_forbidden")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            contents = stream.read(65537)
        if not 0 < len(contents) <= 65536:
            raise ProviderError("provider_forbidden")
        return contents
    except (OSError, ValueError):
        raise ProviderError("provider_forbidden") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def _token(value: object) -> SecretStr:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 8192
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
    ):
        raise ProviderError("provider_invalid_response")
    return SecretStr(value)


def load_private_environment(path: Path) -> dict[str, str]:
    """Read a bounded private environment file without executing or expanding it."""
    try:
        text = read_private_file(path).decode("utf-8")
        values: dict[str, str] = {}
        allowed = {"BRAIN_DELIVERY_" + key.upper() for key in DeliverySettings.model_fields}
        allowed.update({"BRAIN_DELIVERY_POSTGRES_URL", "POSTGRES_URL"})
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in allowed or key in values:
                raise ProviderError("provider_forbidden")
            value = value.strip()
            if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
                value = value[1:-1]
            values[key] = value
        return values
    except UnicodeError:
        raise ProviderError("provider_forbidden") from None


def _load_pat(path: Path) -> SecretStr:
    return _token(load_private_environment(path).get("BRAIN_DELIVERY_GITHUB_TOKEN"))


class GitHubAuthProvider:
    def __init__(
        self,
        settings: DeliverySettings,
        transport: GitHubTransport,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self._settings, self._now = settings, now
        self._refresh = asyncio.Lock()
        self._installation_token: SecretStr | None = None
        self._expires_at = 0.0
        self._pat: SecretStr | None = None
        self._private_key: SecretStr | None = None

    async def authorization_headers(self) -> dict[str, str]:
        app_fields = (
            self._settings.github_app_id,
            self._settings.github_installation_id,
            self._settings.github_private_key_path,
        )
        if not any(value is not None for value in app_fields):
            if self._pat is None:
                self._pat = _load_pat(self._settings.observer_env_path)
            return {"Authorization": "Bearer " + self._pat.get_secret_value()}
        if any(value is None for value in app_fields):
            raise ProviderError("provider_forbidden")
        async with self._refresh:
            now = self._now()
            if self._installation_token is None or now + 60 >= self._expires_at:
                try:
                    if self._private_key is None:
                        path = self._settings.github_private_key_path
                        if path is None:
                            raise ProviderError("provider_forbidden")
                        self._private_key = SecretStr(read_private_file(path).decode("utf-8"))
                    bearer = jwt.encode(
                        {
                            "iss": str(self._settings.github_app_id),
                            "iat": int(now) - 60,
                            "exp": int(now) + 540,
                        },
                        self._private_key.get_secret_value(),
                        algorithm="RS256",
                    )
                except (ValueError, UnicodeError, jwt.PyJWTError):
                    raise ProviderError("provider_forbidden") from None
                response = await self.transport.request_json(
                    "POST",
                    f"/app/installations/{self._settings.github_installation_id}/access_tokens",
                    headers={"Authorization": "Bearer " + bearer},
                    json={},
                )
                try:
                    if not isinstance(response, dict) or not isinstance(
                        response.get("expires_at"), str
                    ):
                        raise ValueError
                    expires = datetime.fromisoformat(response["expires_at"].replace("Z", "+00:00"))
                    if (
                        expires.tzinfo is None
                        or not self._now() + 60 < expires.timestamp() <= self._now() + 86400
                    ):
                        raise ValueError
                    token = _token(response.get("token"))
                except (ValueError, TypeError, OverflowError):
                    raise ProviderError("provider_invalid_response") from None
                self._installation_token = token
                self._expires_at = expires.timestamp()
            return {"Authorization": "Bearer " + self._installation_token.get_secret_value()}
