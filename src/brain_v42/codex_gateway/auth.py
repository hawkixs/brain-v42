"""Static Bearer authentication for the loopback Codex gateway."""

from __future__ import annotations

import secrets
from typing import Annotated, NoReturn

from fastapi import Header, HTTPException, status
from pydantic import SecretStr

_MIN_TOKEN_BYTES = 32


def require_non_empty_token(token: SecretStr) -> str:
    value = token.get_secret_value().strip()
    if len(value.encode()) < _MIN_TOKEN_BYTES or value.upper().startswith("REPLACE_"):
        raise RuntimeError(
            "BRAIN_CODEX_GATEWAY_TOKEN must be a generated secret of at least 32 bytes"
        )
    return value


class BearerAuthenticator:
    def __init__(self, token: SecretStr) -> None:
        self._token = require_non_empty_token(token).encode()

    async def __call__(
        self,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        prefix = "Bearer "
        if authorization is None or not authorization.startswith(prefix):
            self._reject()
        supplied = authorization.removeprefix(prefix)
        if not supplied or not secrets.compare_digest(supplied.encode(), self._token):
            self._reject()

    @staticmethod
    def _reject() -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
