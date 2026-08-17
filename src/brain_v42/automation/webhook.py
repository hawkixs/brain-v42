"""Shared GitLab webhook HTTP endpoint."""

from __future__ import annotations

import hmac
import json
import zlib
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from brain_v42.automation.ownership import OwnershipLostError

ProjectKeyResolver = Callable[[str], Awaitable[str | None]]


class _InvalidWebhookBody(ValueError):
    """A compressed webhook body is malformed or incomplete."""


class _WebhookBodyTooLarge(ValueError):
    """A webhook body reached the aiohttp request limit."""


def webhook_token_matches(supplied_token: str, configured_secret: str) -> bool:
    """Compare a supplied webhook token with the configured secret."""
    if not configured_secret:
        return False
    return hmac.compare_digest(
        supplied_token.encode("utf-8", errors="surrogateescape"),
        configured_secret.encode("utf-8", errors="surrogateescape"),
    )


def _decompress_zlib_bounded(body: bytes, wbits: int, max_size: int) -> bytes:
    if max_size <= 0:
        raise _WebhookBodyTooLarge
    decompressor = zlib.decompressobj(wbits)
    try:
        decoded = decompressor.decompress(body, max_size)
    except zlib.error:
        raise _InvalidWebhookBody from None
    if len(decoded) >= max_size or decompressor.unconsumed_tail:
        raise _WebhookBodyTooLarge
    if not decompressor.eof or decompressor.unused_data:
        raise _InvalidWebhookBody
    return decoded


def _decompress_webhook_body(body: bytes, encoding: str, max_size: int) -> bytes:
    if encoding == "gzip":
        return _decompress_zlib_bounded(body, zlib.MAX_WBITS | 16, max_size)
    try:
        return _decompress_zlib_bounded(body, zlib.MAX_WBITS, max_size)
    except _WebhookBodyTooLarge:
        raise
    except _InvalidWebhookBody:
        return _decompress_zlib_bounded(body, -zlib.MAX_WBITS, max_size)


def _webhook_body_error(status: int) -> web.Response:
    outcome = "payload_too_large" if status == 413 else "invalid_request"
    return web.json_response({"status": outcome}, status=status)


async def _read_bounded_webhook_body(request: web.Request) -> bytes:
    encodings = request.headers.getall("Content-Encoding", ())
    if len(encodings) > 1:
        raise _InvalidWebhookBody
    encoding = encodings[0].strip().casefold() if encodings else "identity"
    if encoding not in {"identity", "gzip", "deflate"}:
        raise _InvalidWebhookBody

    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        raise _WebhookBodyTooLarge from None
    if len(body) >= request.client_max_size:
        raise _WebhookBodyTooLarge
    if encoding in {"gzip", "deflate"}:
        body = _decompress_webhook_body(body, encoding, request.client_max_size)
    return body


class GitLabWebhookEndpoint:
    """Handle the GitLab webhook contract independently of its HTTP server."""

    def __init__(
        self,
        gitlab_ingestor: Any,
        project_key_resolver: ProjectKeyResolver,
        webhook_secret: str,
    ) -> None:
        self._gitlab_ingestor = gitlab_ingestor
        self._project_key_resolver = project_key_resolver
        self._webhook_secret = webhook_secret

    async def handle(self, request: web.Request) -> web.Response:
        """Handle POST /gitlab/webhook."""
        token = request.headers.get("X-Gitlab-Token", "")
        if not self._webhook_secret:
            return web.Response(status=401, text="Webhook authentication not configured")
        if not webhook_token_matches(token, self._webhook_secret):
            return web.Response(status=401, text="Invalid token")

        try:
            body = await _read_bounded_webhook_body(request)
        except _WebhookBodyTooLarge:
            return _webhook_body_error(413)
        except _InvalidWebhookBody:
            return _webhook_body_error(400)

        payload = json.loads(body.decode(request.charset or "utf-8"))
        event_uuid = request.headers.get("X-Gitlab-Event-UUID", "")
        if not event_uuid:
            return web.Response(status=400, text="Missing X-Gitlab-Event-UUID")

        project_path = payload.get("project", {}).get("path_with_namespace", "")
        try:
            project_key = await self._project_key_resolver(project_path)
            if not project_key:
                return web.json_response({"status": "unknown_project", "path": project_path})
            result = await self._gitlab_ingestor.process_event(payload, event_uuid, project_key)
        except OwnershipLostError:
            return web.json_response({"status": "ownership_lost"}, status=503)
        return web.json_response(result)
