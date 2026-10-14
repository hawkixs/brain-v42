"""Admit protected reads without changing telemetry receivers or their observations."""

from __future__ import annotations

import contextlib

import structlog
from aiohttp import web

from brain_v42.credentials.bucket import TokenBucket
from brain_v42.credentials.reasons import REFUSAL_STATUSES
from brain_v42.credentials.verifier import CredentialRefused, CredentialVerifier

logger = structlog.get_logger(__name__)


def _refuse_read(
    route: str, reason: str, status: int, client_id: str | None = None
) -> web.Response:
    """Log only policy labels and verified identity, never credential material."""
    fields = {"client_id": client_id} if client_id is not None else {}
    with contextlib.suppress(Exception):
        logger.warning("metrics.read_refused", route=route, reason=reason, **fields)
    body = (
        {"error": "loopback peer required"} if reason == "loopback_required" else {"reason": reason}
    )
    return web.json_response(
        body, status=status, headers={"Retry-After": "1"} if status in {429, 503} else None
    )


async def admit_read(
    request: web.Request,
    *,
    route: str,
    loopback_peer: bool,
    verifier: CredentialVerifier | None,
    clients: frozenset[str],
    bucket: TokenBucket,
) -> web.Response | None:
    """Apply local header precedence before registry checks and the separate read quota.

    Headerless loopback access permits local diagnostics during registry outages.
    A supplied header always opts into validation, including on loopback.
    """
    if verifier is None and not loopback_peer:
        return _refuse_read(route, "loopback_required", 403)

    values = request.headers.getall("Authorization", ())
    client_id: str | None = None
    if len(values) == 0:
        if not loopback_peer:
            return _refuse_read(route, "missing_token", 401)
    else:
        if len(values) != 1:
            return _refuse_read(route, "malformed_authorization", 401)
        scheme, separator, token = values[0].partition(" ")
        if (
            not separator
            or scheme.casefold() != "bearer"
            or not token
            or any(character.isspace() for character in token)
        ):
            return _refuse_read(route, "malformed_authorization", 401)
        if verifier is None:
            return _refuse_read(route, "unknown_token", 401)
        try:
            principal = await verifier.verify(token)
        except CredentialRefused as exc:
            return _refuse_read(route, exc.reason, REFUSAL_STATUSES[exc.reason])
        client_id = principal.client_id
        if client_id not in clients:
            return _refuse_read(route, "client_not_allowed", 403, client_id)
        if "telemetry" not in principal.families:
            return _refuse_read(route, "family_denied", 403, client_id)
        if not bucket.allow(client_id):
            return _refuse_read(route, "rate_limited", 429, client_id)

    fields = {"client_id": client_id} if client_id is not None else {}
    with contextlib.suppress(Exception):
        logger.debug("metrics.read_admitted", route=route, **fields)
    return None
