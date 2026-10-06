"""The single construction path for the embedding client.

Every runtime builds its embedding service here, so an operator switching
``BRAIN_EMBEDDING_BACKEND`` switches all of them at once. A construction site
that bypasses this function pins one runtime to a different backend than the
other eight, which is the kind of split that only shows up as unexplained
search-quality drift.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import structlog
from pydantic import ValidationError

from brain_v42.config import Settings, get_settings
from brain_v42.services.embedding_wire import EmbeddingWire, OpenAIWire, ShimWire
from brain_v42.services.gpu_embedding_service import GPUEmbeddingService
from brain_v42.services.rerank_wire import CohereRerankWire, RerankWire, ShimRerankWire
from brain_v42.services.reranker_client import RerankerClient, RerankObserver

logger = structlog.get_logger(__name__)


class EmbeddingBearerError(RuntimeError):
    """The shim bearer was configured and could not be used.

    Raised at construction time, which is startup for all nine runtimes that go
    through this module. Failing here rather than falling back to an unauthenticated
    call is the whole point: the shim answers `optional` today, so a bearer-less
    call still succeeds and would leave a misconfiguration invisible until the day
    someone arms `required` — at which point every search stops with no clue in
    this process's logs.
    """


class RerankKeyError(RuntimeError):
    """The hosted reranker's API key was configured and could not be used.

    Same doctrine as ``EmbeddingBearerError``: raised at construction, never
    falling back to an unauthenticated call. Messages carry the path and the
    setting names, never the content of the file.
    """


#: Said once per PROCESS, not once per client: nine runtimes build clients on a
#: hot path, and a line repeated per request drowns the log it was meant to make
#: readable.
_bearer_absence_announced = False


def reset_bearer_absence_announcement() -> None:
    """Test seam. A test PROCESS builds many clients; a runtime builds a few."""
    global _bearer_absence_announced
    _bearer_absence_announced = False


def _announce_bearer_absence_once() -> None:
    """Say that this runtime calls the shim with no Authorization header at all.

    Measured 2026-09-03: 1724 header-less calls reached the shim from this host in
    twelve hours, and not one of the processes making them said anything. The
    resolution is central and correct; what was missing was the variable, in every
    runtime but one. A misconfiguration that only the SHIM's log can see is
    invisible from inside the process that has it -- until an operator arms
    `required` and every one of those calls becomes a 401.

    A WARNING and not an error: header-less is still a valid deployment today, and
    refusing to build would take a runtime down for a state the shim accepts.
    """
    global _bearer_absence_announced
    if _bearer_absence_announced:
        return
    _bearer_absence_announced = True
    logger.warning(
        "embedding_factory.no_shim_bearer",
        runtime=" ".join(sys.argv) or "<unknown>",
        pid=os.getpid(),
        cwd=os.getcwd(),
        setting="BRAIN_EMBEDDING_TOKEN_FILE",
        detail=(
            "this runtime sends no Authorization header to the shim; set "
            "BRAIN_EMBEDDING_TOKEN_FILE to the bearer file's PATH (never its value)"
        ),
    )


def _resolve_shim_bearer(settings: Settings, api_key: str) -> str:
    """The single place the shim bearer is resolved, for both clients.

    Returns the token to hand to the client's existing ``api_key`` parameter, so
    the header keeps being injected once per client rather than once per route.
    An unconfigured file returns the caller's ``api_key`` untouched, which is how
    the hosted-provider path and today's header-less contract both survive.

    Never logs, never renders and never returns the value in an exception: the
    only things named here are paths and the two setting names.
    """
    token_file = settings.brain_embedding_token_file
    if token_file is None:
        if not api_key:
            # `cwd` is in the line on purpose: `Settings` reads `.env` RELATIVE to
            # the working directory, so a runtime started elsewhere is the failure
            # mode this warning exists to make visible.
            _announce_bearer_absence_once()
        return api_key
    if api_key:
        raise EmbeddingBearerError(
            "two sources for one Authorization header: "
            "brain_embedding_token_file is set and an api key is configured; "
            "clear one of them"
        )
    # `~` is what an operator writes and what the switch runbook prescribed;
    # pydantic's `Path` field does not expand it, so the literal string would be
    # read as a relative directory named `~` under the working directory and
    # every writer would fail to build.
    path = Path(token_file).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EmbeddingBearerError(
            f"embedding bearer file {path} cannot be read: {exc.strerror}"
        ) from exc
    token = raw.strip()
    if not token:
        raise EmbeddingBearerError(f"embedding bearer file {path} is empty")
    return token


_RERANK_KEY_MAX_LENGTH = 512
_RERANK_KEY_NAME = "BRAIN_RERANK_API_KEY"
_DOTENV_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)")


def _is_single_token(value: str) -> bool:
    """A bearer is one token: no whitespace, no ``=``, bounded length.

    The bound is what stops an env fragment from being forwarded wholesale to a
    third party as an ``Authorization`` header.
    """
    return (
        0 < len(value) <= _RERANK_KEY_MAX_LENGTH
        and "=" not in value
        and not any(ch.isspace() for ch in value)
    )


def _rerank_key_from_dotenv(content: str, path: Path) -> str:
    """Extract ``BRAIN_RERANK_API_KEY`` BY NAME from a dotenv-style file.

    Every other line belongs to someone else and is never read into the bearer.
    Errors name the path and the key name, never any content.
    """
    malformed = RerankKeyError(
        f"rerank key file {path} must hold only the key value or {_RERANK_KEY_NAME}=<value>"
    )
    values: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DOTENV_LINE.fullmatch(line)
        if match is None:
            raise malformed
        if match.group(1) != _RERANK_KEY_NAME:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values.append(value)
    if len(values) != 1:
        raise RerankKeyError(f"rerank key file {path} must name {_RERANK_KEY_NAME} exactly once")
    if not _is_single_token(values[0]):
        raise malformed
    return values[0]


def _resolve_rerank_bearer(settings: Settings) -> str:
    """The key the reranker client sends as ``Authorization: Bearer``.

    The cohere backend talks to a different party than the shim, so it has its own
    key and NEVER consults ``brain_embedding_token_file``: that file holds the
    shim's bearer, and sending it to a hosted vendor would be a credential leak
    where the shim path only raises a collision. The shim backend resolves exactly
    as before.
    """
    if settings.rerank_backend != "cohere":
        return _resolve_shim_bearer(settings, settings.rerank_api_key.get_secret_value())
    key_file = settings.rerank_api_key_file
    if key_file is None:
        return settings.rerank_api_key.get_secret_value()
    path = Path(key_file).expanduser()
    try:
        mode = path.stat().st_mode
        if mode & 0o077:
            raise RerankKeyError(
                f"rerank key file {path} must be 0600 or stricter (mode is {mode & 0o777:04o})"
            )
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RerankKeyError(f"rerank key file {path} cannot be read: {exc.strerror}") from exc
    key = raw.strip()
    if not key:
        raise RerankKeyError(f"rerank key file {path} is empty")
    if _is_single_token(key):
        return key
    return _rerank_key_from_dotenv(key, path)


def settings_for_standalone_script(postgres_url: str) -> Settings:
    """Settings for a script that already knows its own database URL.

    ``get_settings()`` requires POSTGRES_URL in the environment and insists on
    the ``postgresql+asyncpg://`` form. Standalone scripts here take
    ``--postgres-url``, default to a plain ``postgresql://`` DSN and are
    expected to run from any working directory with no ``.env`` in reach — so
    calling ``get_settings()`` unguarded turns "no env var" into a crash before
    the script does anything at all.

    Everything except the database URL still comes from the environment, so the
    embedding backend, model and prefixes are the ones actually configured.
    """
    try:
        return get_settings()
    except ValidationError:
        return Settings(postgres_url=as_asyncpg_dsn(postgres_url))


def as_asyncpg_dsn(url: str) -> str:
    """Normalise a plain ``postgresql://`` DSN to the driver form Settings wants."""
    if url.startswith("postgresql+asyncpg://"):
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


def build_embedding_wire(settings: Settings) -> EmbeddingWire:
    """Select the wire format named by ``settings.embedding_backend``."""
    if settings.embedding_backend == "openai":
        return OpenAIWire(model=settings.embedding_model)
    return ShimWire()


def build_embedding_service(settings: Settings) -> GPUEmbeddingService:
    """Build the embedding client configured for this deployment.

    Defaults reproduce the shim contract exactly: same routes, same bodies, no
    prefixes, no Authorization header.
    """
    wire = build_embedding_wire(settings)
    logger.info(
        "embedding_factory.built",
        backend=settings.embedding_backend,
        base_url=settings.embedding_service_url,
        model=settings.embedding_model if settings.embedding_backend == "openai" else None,
        query_prefixed=bool(settings.embedding_query_prefix),
        document_prefixed=bool(settings.embedding_document_prefix),
    )
    return GPUEmbeddingService(
        base_url=settings.embedding_service_url,
        timeout=settings.embedding_timeout,
        wire=wire,
        query_prefix=settings.embedding_query_prefix,
        document_prefix=settings.embedding_document_prefix,
        api_key=_resolve_shim_bearer(settings, settings.embedding_api_key.get_secret_value()),
    )


def build_rerank_wire(settings: Settings) -> RerankWire:
    """Select the rerank wire format named by ``settings.rerank_backend``."""
    if settings.rerank_backend == "cohere":
        provider = settings.rerank_provider
        return CohereRerankWire(
            model=settings.rerank_model,
            health_path=settings.rerank_health_path,
            routing=provider.model_dump(exclude_none=True) if provider is not None else None,
        )
    return ShimRerankWire()


def build_reranker_client(
    settings: Settings, *, observer: RerankObserver | None = None
) -> RerankerClient | None:
    """Build the reranker client configured for this deployment.

    Reranking stays best-effort: an unavailable or misconfigured reranker
    makes HybridReranker fall back to RRF ordering rather than fail a search.
    The intentional ``none`` backend returns before resolving any key or calibration.
    """
    if settings.rerank_backend == "none":
        return None
    return RerankerClient(
        base_url=settings.reranker_url,
        timeout=settings.reranker_timeout,
        wire=build_rerank_wire(settings),
        api_key=_resolve_rerank_bearer(settings),
        budget_seconds=settings.rerank_budget_seconds,
        observer=observer,
    )
