"""brain_v42 configuration via pydantic-settings.

All settings are loaded from environment variables or .env file.
HTTP transport defaults to loopback; off-loopback binds require a named opt-in.
Neo4j is optional and disabled by default (graph_enabled=False).

Env var naming: every setting is reachable under a BRAIN_-prefixed name
(BRAIN_POSTGRES_URL, BRAIN_METRICS_PORT, ...). Fields that predate this
convention (POSTGRES_URL, METRICS_PORT, GRAPH_ENABLED, ...) keep their
original bare name working as a fallback alias, via ``_brain_alias()`` --
existing .env files and systemd units need no changes. Fields that already
carried a ``brain_``-prefixed name (``brain_mcp_profile``, ``brain_code_mode``,
...) needed no change: the field name already IS the BRAIN_-prefixed env
var, pydantic-settings maps it automatically.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _is_loopback_host(value: str | None) -> bool:
    """The one definition every bind and every egress URL of this file shares.

    Four validators carried the same literal set inline. Four copies of a
    security predicate drift the day one of them learns something -- and the one
    that did NOT learn it is the one nobody notices.

    Deliberately stricter than `metrics.server._is_loopback_bind`, which accepts
    all of 127.0.0.0/8 and IPv4-mapped IPv6. The difference is fail-closed in the
    safe direction: `127.0.0.2` is refused here and would be treated as loopback
    there, so it needs the opt-in. Unifying the two is a separate lot -- widening
    a security predicate is not a tidy-up.
    """
    return value in {"127.0.0.1", "localhost", "::1"}


# pgvector 0.8.2 refuses an HNSW index on a column wider than this
# ("column cannot have more than 2000 dimensions for hnsw index"), and every
# embedding column in this schema carries one. Bounding the setting turns a
# detonation inside a migration's CREATE INDEX into a config-load error.
PGVECTOR_HNSW_MAX_DIMENSIONS = 2000

# Settings sets `str_strip_whitespace=True` model-wide. Every asymmetric-model
# instruction prefix worth having ends in a space ("query: ", "passage: "), and
# that space is what separates the prefix from the text. These fields opt out.
_UnstrippedStr = Annotated[str, StringConstraints(strip_whitespace=False)]


#: The keys of a declared PostgreSQL identity and the JSON type of each;
#: `server_addr` may be omitted (Q134 = c), the other three may not.
#: Only the SHAPE is checked here: the deep validation (a 64-bit identifier, an
#: IP literal, a port range) belongs to `brain_v42.facts.model.SourceIdentity`,
#: which the composition root builds from this mapping. `config` must not import
#: `facts`: `facts` imports `repositories`, which imports `db`, which imports
#: this module — the layering DAG would close on itself.
_PRODUCTION_IDENTITY_KEYS: dict[str, type] = {
    "system_identifier": str,
    "database": str,
    "server_addr": str,
    "server_port": int,
}
_PRODUCTION_IDENTITY_OPTIONAL_KEYS: frozenset[str] = frozenset({"server_addr"})
_LIVE_RELEASE_IDENTITY_KEYS: dict[str, type] = {
    "release_sha": str,
    "package_version": str,
}
_HOST_IDENTITY_KEYS: dict[str, type] = {"hostname": str}


def _parse_identity_json(
    raw: str,
    keys: Mapping[str, type],
    *,
    label: str,
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Parse exactly the identity shape declared for one independently verified target.

    A key named in ``optional`` may be absent; when present it is type-checked
    like any other.
    """
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("not a JSON document") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object with {len(keys)} keys")
    actual = set(payload)
    expected = set(keys)
    missing = expected - actual - optional
    extra = actual - expected
    if missing or extra:
        raise ValueError(f"missing keys {sorted(missing)!r}, extra keys {sorted(extra)!r}")
    for key, kind in keys.items():
        if key not in payload:
            continue
        value = payload[key]
        if type(value) is not kind:  # bool is not an int here, on purpose
            raise ValueError(f"{key} must be a JSON {kind.__name__}")
    return dict(payload)


def _parse_production_identity(raw: str) -> dict[str, object]:
    """Keep the production parser stable while the generic declaration parser expands."""
    return _parse_identity_json(
        raw,
        _PRODUCTION_IDENTITY_KEYS,
        label="production identity",
        optional=_PRODUCTION_IDENTITY_OPTIONAL_KEYS,
    )


def _brain_alias(legacy_env: str) -> AliasChoices:
    """BRAIN_<legacy_env> is preferred; the pre-migration bare name still works.

    First alias present in the environment wins (pydantic-settings resolves
    AliasChoices in order), so BRAIN_POSTGRES_URL overrides POSTGRES_URL if
    both happen to be set -- but nothing needs to change for deployments that
    only know the bare name.
    """
    return AliasChoices(f"BRAIN_{legacy_env}", legacy_env)


class RerankProviderRouting(BaseModel):
    """OpenRouter's ``provider`` routing object, as the rerank wire sends it.

    ``extra="forbid"``: a mistyped key would otherwise be dropped by pydantic and
    sent as nothing, leaving the request unrouted with every field "set".
    """

    model_config = ConfigDict(extra="forbid")

    only: list[str] = Field(min_length=1)
    allow_fallbacks: bool = False
    data_collection: Literal["deny", "allow"] = "deny"
    zdr: bool | None = None


def is_relative_request_path(path: str) -> bool:
    """True for a plain absolute-path reference like ``/v1/key``.

    httpx resolves an absolute URL against ``base_url`` by IGNORING the base, so a
    health path such as ``https://other.example/x`` would carry the client's
    Authorization header to that host. ``//host/x`` is refused for the same reason,
    and a backslash because some parsers read it as a slash. Shared by the settings
    validator and the client: one predicate, not two that can drift.
    """
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 for ch in path):
        return False
    parts = urlsplit(path)
    return not parts.scheme and not parts.netloc


def _is_openrouter_host(url: str) -> bool:
    """Read the host the way the transport will, not the way ``urlsplit`` does.

    ``https://openrouter.ai./`` (trailing dot) and ``https://openrouter\u3002ai/``
    (ideographic full stop) both reach openrouter.ai, and ``urlsplit`` calls
    neither of them that. httpx normalises both; so does this predicate.
    """
    try:
        host = httpx.URL(url).host
    except httpx.InvalidURL as exc:
        raise ValueError("reranker_url is not a valid URL") from exc
    host = host.lower().rstrip(".")
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Required:
        POSTGRES_URL (or BRAIN_POSTGRES_URL): PostgreSQL connection URL using
                      the asyncpg driver. Format: postgresql+asyncpg://user:pass@host:port/db

    Optional:
        LOG_LEVEL: Logging level (default: INFO)
        EMBEDDING_SERVICE_URL: GPU embedding service URL
            (default: http://localhost:8003 — PC serveur local, restore 2026-07-06)
        EMBEDDING_DIMENSION: Embedding vector dimension (default: 1536)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown env vars (PATH, HOME, etc.)
        str_strip_whitespace=True,
        hide_input_in_errors=True,
        # Fields below carry a `validation_alias=_brain_alias(...)` so their
        # env var can be read as either BRAIN_<X> or the legacy bare <X>.
        # Without populate_by_name, giving a field a validation_alias makes
        # its plain Python name stop working for *keyword* construction
        # (Settings(postgres_url="...") is exactly how dozens of existing
        # tests build a Settings instance directly, bypassing the
        # environment entirely) -- populate_by_name keeps both working.
        populate_by_name=True,
    )

    # --- Database ---
    postgres_url: str = Field(validation_alias=_brain_alias("POSTGRES_URL"))
    """PostgreSQL connection URL. Must use postgresql+asyncpg:// scheme."""

    elevatable_client_ids: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset({"workstation-claude"}),
        validation_alias="BRAIN_ELEVATABLE_CLIENT_IDS",
    )
    """Clients allowed to claim an unowned operator session; never declared actors."""

    @field_validator("elevatable_client_ids", mode="before")
    @classmethod
    def validate_elevatable_client_ids(cls, value: object) -> frozenset[str]:
        """Refuse malformed entries rather than silently widen the attribution allowlist."""
        entries = value.split(",") if isinstance(value, str) else value
        if not isinstance(entries, (list, tuple, set, frozenset)) or any(
            not isinstance(entry, str) or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", entry) is None
            for entry in entries
        ):
            raise ValueError("elevatable client ids must be comma-separated valid client ids")
        return frozenset(entries)

    # Session budgets sent to PostgreSQL at connection time, in MILLISECONDS; 0
    # disables, exactly as in PostgreSQL. Three profiles because three very
    # different jobs share the engine factory (see ``brain_v42.db.engine``):
    # the long-lived interactive processes (MCP server, codex gateway, automation
    # runtime) are tight, so a stuck query or lock wait fails in minutes instead of
    # exhausting the pool; maintenance jobs and scripts are generous and carry NO
    # idle-in-transaction limit, because some hold a transaction open on purpose
    # (the embedding-backfill advisory-lock session); the metrics sidecar's scrape
    # path is the tightest. Retune from ``monitoring.pg_stat_statements`` and an
    # environment override, without a release.
    pg_statement_timeout_ms: int = Field(
        default=120_000, ge=0, validation_alias=_brain_alias("PG_STATEMENT_TIMEOUT_MS")
    )
    pg_lock_timeout_ms: int = Field(
        default=30_000, ge=0, validation_alias=_brain_alias("PG_LOCK_TIMEOUT_MS")
    )
    pg_idle_in_transaction_session_timeout_ms: int = Field(
        default=300_000,
        ge=0,
        validation_alias=_brain_alias("PG_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS"),
    )
    pg_maintenance_statement_timeout_ms: int = Field(
        default=1_800_000,
        ge=0,
        validation_alias=_brain_alias("PG_MAINTENANCE_STATEMENT_TIMEOUT_MS"),
    )
    pg_maintenance_lock_timeout_ms: int = Field(
        default=300_000, ge=0, validation_alias=_brain_alias("PG_MAINTENANCE_LOCK_TIMEOUT_MS")
    )
    pg_maintenance_idle_in_transaction_session_timeout_ms: int = Field(
        default=0,
        ge=0,
        validation_alias=_brain_alias("PG_MAINTENANCE_IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS"),
    )
    metrics_pg_statement_timeout_ms: int = Field(
        default=10_000, ge=0, validation_alias=_brain_alias("METRICS_PG_STATEMENT_TIMEOUT_MS")
    )

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", validation_alias=_brain_alias("LOG_LEVEL")
    )
    brain_log_format: Literal["console", "json"] = "console"

    @field_validator("brain_log_format", mode="before")
    @classmethod
    def _log_format_is_known(cls, value: object) -> object:
        """Refuse typos instead of silently breaking the container log watcher."""
        if value not in ("console", "json"):
            raise ValueError("BRAIN_LOG_FORMAT must be 'console' or 'json'")
        return value

    # --- Embedding ---
    # Default points at the local brain-host container (restore 2026-07-06,
    # reverses the 2026-04-15 gpu-host cutover after the gpu-host crash).
    # Avoids a dead-endpoint fallback when the MCP starts from a foreign cwd
    # without env injection — see learning 2a4930a9 (env drift gotcha).
    embedding_service_url: str = Field(
        default="http://localhost:8003", validation_alias=_brain_alias("EMBEDDING_SERVICE_URL")
    )
    embedding_dimension: int = Field(
        default=1536,
        ge=1,
        le=PGVECTOR_HNSW_MAX_DIMENSIONS,
        validation_alias=_brain_alias("EMBEDDING_DIMENSION"),
    )

    # --- Embedding backend (pluggable wire shape) ---
    # "shim"   — the private three-route contract (POST /embed, /embed/query),
    #            served by the bundled reference stack. Default: production
    #            keeps posting exactly the bodies it posts today.
    # "openai" — POST /v1/embeddings, spoken by Ollama, vLLM, llama.cpp server,
    #            LM Studio, TEI, Jina, Mistral, Voyage and OpenAI itself.
    embedding_backend: Literal["shim", "openai"] = Field(
        default="shim", validation_alias=_brain_alias("EMBEDDING_BACKEND")
    )
    embedding_model: str = Field(default="qodo", validation_alias=_brain_alias("EMBEDDING_MODEL"))
    """Model name sent as ``"model"`` by the openai backend. Hosted providers
    reject an unknown name; most local servers ignore the field entirely."""

    embedding_api_key: SecretStr = Field(
        default=SecretStr(""), repr=False, validation_alias=_brain_alias("EMBEDDING_API_KEY")
    )
    """Sent as ``Authorization: Bearer`` only when non-empty. Belongs in a
    private 0600 file, never in the shared .env — same doctrine as MCP_HTTP_TOKEN."""

    brain_embedding_token_file: Path | None = Field(
        default=None, validation_alias=_brain_alias("EMBEDDING_TOKEN_FILE")
    )
    """Path to the shim bearer, read at startup. ``None`` keeps today's contract:
    no Authorization header at all, which is what the shim's ``optional`` census
    mode accepts and logs.

    A PATH and never a value: `systemctl show` and `docker inspect` both print an
    environment verbatim, so a token passed that way is readable by anyone who can
    reach the service manager or the daemon. Configured but unreadable is a named
    startup failure, never a silent call without the header — once the shim is
    armed to ``required``, that silence would be an outage with no cause in this
    process's own logs."""

    embedding_timeout: float = Field(
        default=30.0, gt=0, validation_alias=_brain_alias("EMBEDDING_TIMEOUT")
    )

    embedding_query_prefix: _UnstrippedStr = Field(
        default="", validation_alias=_brain_alias("EMBEDDING_QUERY_PREFIX")
    )
    """Prepended by ``embed_query()`` only. Changing it is free — no re-embed."""

    embedding_document_prefix: _UnstrippedStr = Field(
        default="", validation_alias=_brain_alias("EMBEDDING_DOCUMENT_PREFIX")
    )
    """Prepended by ``embed()`` and ``embed_texts()``. Changing it on a populated
    corpus requires a full ``scripts/regen_embeddings.py`` pass, otherwise
    prefixed and unprefixed vectors share one column."""

    # --- Reranker backend (pluggable wire shape) ---
    # "shim"   — the private POST /rerank contract (raw cross-encoder logits).
    # "cohere" — POST /v1/rerank, implemented by TEI, Jina and vLLM.
    # "none"   — intentional rollback to RRF ordering, without a client or probe.
    rerank_backend: Literal["shim", "cohere", "none"] = Field(
        default="shim", validation_alias=_brain_alias("RERANK_BACKEND")
    )
    rerank_model: str = Field(default="", validation_alias=_brain_alias("RERANK_MODEL"))
    """Model name sent by the cohere backend. Required by hosted providers."""

    rerank_api_key: SecretStr = Field(
        default=SecretStr(""), repr=False, validation_alias=_brain_alias("RERANK_API_KEY")
    )

    rerank_api_key_file: Path | None = Field(
        default=None, validation_alias=_brain_alias("RERANK_API_KEY_FILE")
    )
    """Path to the hosted reranker's API key, read once at startup (0600 or
    stricter). A PATH and never a value, for the reason given on
    ``brain_embedding_token_file``: ``systemctl show`` and ``docker inspect`` print
    an environment verbatim. Mutually exclusive with ``rerank_api_key``."""

    rerank_health_path: str = Field(
        default="/health", validation_alias=_brain_alias("RERANK_HEALTH_PATH")
    )
    """GET path the availability probe calls. Hosted APIs expose no liveness route:
    for OpenRouter use ``/v1/key``, which is free and answers 200 only with a valid
    key (401 otherwise), so it proves the route AND the key."""

    rerank_provider: RerankProviderRouting | None = Field(
        default=None, validation_alias=_brain_alias("RERANK_PROVIDER")
    )
    """Provider routing sent with every rerank request (JSON in the environment).
    Mandatory, and policed, when the cohere backend targets OpenRouter."""

    rerank_probe_interval_seconds: float = Field(
        default=300.0, gt=0, validation_alias=_brain_alias("RERANK_PROBE_INTERVAL_SECONDS")
    )
    """Period of the background availability probe run by the MCP server."""

    rerank_budget_seconds: float = Field(
        default=1.5, gt=0, validation_alias=_brain_alias("RERANK_BUDGET_SECONDS")
    )
    """Elapsed-time budget across all hosted rerank attempts and backoff sleeps."""

    @field_validator("rerank_health_path")
    @classmethod
    def _rerank_health_path_stays_on_the_base_url(cls, value: str) -> str:
        if not is_relative_request_path(value):
            raise ValueError("rerank_health_path must be a path starting with '/', not a URL")
        return value

    @model_validator(mode="after")
    def _rerank_key_and_routing_policy(self) -> Self:
        if self.rerank_backend == "none":
            conflicts = [
                name
                for name, configured in (
                    ("rerank_model", bool(self.rerank_model)),
                    ("rerank_api_key", bool(self.rerank_api_key.get_secret_value())),
                    ("rerank_api_key_file", self.rerank_api_key_file is not None),
                    ("rerank_provider", self.rerank_provider is not None),
                    ("reranker_url", "reranker_url" in self.model_fields_set),
                    ("rerank_health_path", "rerank_health_path" in self.model_fields_set),
                )
                if configured
            ]
            if conflicts:
                raise ValueError("rerank_backend='none' refuses: " + ", ".join(conflicts))
            return self
        if self.rerank_api_key.get_secret_value() and self.rerank_api_key_file is not None:
            raise ValueError(
                "two sources for one rerank key: rerank_api_key and rerank_api_key_file "
                "are both set; clear one of them"
            )
        if self.rerank_backend != "cohere":
            if self.rerank_api_key_file is not None:
                # The shim authenticates with brain_embedding_token_file: a key file
                # here would be ignored, and the operator would believe it armed.
                raise ValueError("rerank_api_key_file requires rerank_backend='cohere'")
            return self
        openrouter = _is_openrouter_host(self.reranker_url)
        if openrouter and self.rerank_api_key.get_secret_value():
            # An inline value can arrive through the shared .env, which many more
            # processes can read than a 0600 file. The hosted vendor's key goes
            # through the file only.
            raise ValueError(
                "rerank_api_key is refused when reranker_url targets openrouter.ai; "
                "set BRAIN_RERANK_API_KEY_FILE to the key file's path"
            )
        provider = self.rerank_provider
        if provider is None:
            if openrouter:
                raise ValueError(
                    "rerank_provider is required when reranker_url targets openrouter.ai"
                )
            return self
        author, separator, _ = self.rerank_model.partition("/")
        if not separator or not author:
            raise ValueError("rerank_model must be '<author>/<name>' when rerank_provider is set")
        if provider.only != [author]:
            raise ValueError(
                f"rerank_provider.only must be exactly [{author!r}], the author of rerank_model"
            )
        if provider.allow_fallbacks:
            raise ValueError("rerank_provider.allow_fallbacks must be false")
        if provider.data_collection != "deny" and provider.zdr is not True:
            raise ValueError(
                "rerank_provider.data_collection must be 'deny' unless rerank_provider.zdr is true"
            )
        return self

    # --- Code Mode (experimental) ---
    brain_code_mode: bool = False

    # --- MCP tool catalog exposure ---
    brain_mcp_profile: Literal["compact", "native"] = "compact"

    # --- CLAUDE.md dynamic section paths ---
    # Env var format (JSON): CLAUDE_MD_PATHS='{"brain_v42": "/path/to/CLAUDE.md"}'
    claude_md_paths: dict[str, str] = Field(
        default={}, validation_alias=_brain_alias("CLAUDE_MD_PATHS")
    )

    # --- MCP transport ---
    brain_mcp_transport: Literal["stdio", "http"] = "stdio"  # env BRAIN_MCP_TRANSPORT
    brain_mcp_auth_mode: Literal["shared_token", "credentials"] = "shared_token"
    mcp_http_allow_non_loopback: bool = Field(
        default=False, validation_alias=_brain_alias("MCP_HTTP_ALLOW_NON_LOOPBACK")
    )
    mcp_http_allowed_hosts: Annotated[frozenset[str], NoDecode] = Field(
        default=frozenset(), validation_alias=_brain_alias("MCP_HTTP_ALLOWED_HOSTS")
    )
    """Additional Host authorities, as a comma-separated list of host[:port]."""
    mcp_http_host: str = Field(default="127.0.0.1", validation_alias=_brain_alias("MCP_HTTP_HOST"))
    mcp_http_port: int = Field(default=8765, validation_alias=_brain_alias("MCP_HTTP_PORT"))
    mcp_http_allow_unauthenticated: bool = Field(
        default=False, validation_alias=_brain_alias("MCP_HTTP_ALLOW_UNAUTHENTICATED")
    )
    """Development only; refused with a token or under capability enforcement."""
    mcp_http_token: str = Field(
        default="", repr=False, validation_alias=_brain_alias("MCP_HTTP_TOKEN")
    )
    """Bearer token for HTTP transport authentication.

    Empty = refused at HTTP startup unless MCP_HTTP_ALLOW_UNAUTHENTICATED=true.

    Non-empty = BearerTokenGuard is activated; HTTP requests outside /health,
    /healthz and /version must carry ``Authorization: Bearer <token>``.

    IMPORTANT — enabling this is a coordinated deployment operation:
    all fleet .mcp.json clients must be updated to inject the Authorization header
    before this token is set in production. Changing only the server side without
    updating every client will break all MCP calls. This workstream only wires the
    server-side guard; fleet client updates are out of scope.
    """

    # --- Dream HTTP capability firewall (dormant by default) ---
    brain_dream_capability_enforcement: bool = False
    mcp_http_dream_tokens: SecretStr = Field(
        default=SecretStr(""), validation_alias=_brain_alias("MCP_HTTP_DREAM_TOKENS")
    )
    """Secret JSON registry for phase-scoped Dream HTTP bearer tokens."""

    @model_validator(mode="after")
    def _credentials_require_attributed_http(self) -> Self:
        """Keep the credential boundary stateful and free of competing identities."""
        if self.brain_mcp_auth_mode == "credentials":
            if self.brain_dream_capability_enforcement:
                raise ValueError("credentials mode is incompatible with Dream capabilities")
            if self.mcp_http_allow_unauthenticated:
                raise ValueError("credentials mode requires authentication")
            if self.mcp_http_stateless:
                raise ValueError("credentials mode requires stateful HTTP")
            if self.mcp_http_token:
                raise ValueError("MCP_HTTP_TOKEN must be absent in credentials mode")
        return self

    @field_validator("metrics_host")
    @classmethod
    def _metrics_bind_is_loopback_unless_opted_in(cls, v: str, info: ValidationInfo) -> str:
        """A LAN bind is a decision, so it has to be written down somewhere.

        Fail-closed and NAMED: the message carries the setting that refused and
        the setting that reopens it, because a refusal that sends people to the
        source is a refusal they work around.
        """
        if _is_loopback_host(v) or info.data.get("metrics_allow_non_loopback"):
            return v
        raise ValueError(
            f"METRICS_HOST must be loopback (got {v!r}): a non-loopback bind serves "
            "/metrics and /api/cockpit to the network while silently dropping the three "
            "POST receivers, whose refusal the access log cannot see. Set "
            "METRICS_ALLOW_NON_LOOPBACK=yes to take that trade deliberately."
        )

    @field_validator("mcp_http_allowed_hosts", mode="before")
    @classmethod
    def _parse_mcp_http_allowed_hosts(cls, value: object) -> object:
        """Parse operator-declared authorities without treating commas as JSON."""
        if isinstance(value, str):
            return frozenset(entry.strip() for entry in value.split(",") if entry.strip())
        return value

    @model_validator(mode="after")
    def _mcp_bind_requires_named_opt_in(self) -> Self:
        """A network listener must opt into credentials and an explicit Host boundary."""
        if not _is_loopback_host(self.mcp_http_host) and not self.mcp_http_allow_non_loopback:
            raise ValueError(
                "MCP_HTTP_HOST must be loopback unless MCP_HTTP_ALLOW_NON_LOOPBACK=true"
            )
        if self.mcp_http_allow_non_loopback:
            if self.brain_mcp_auth_mode != "credentials":
                raise ValueError(
                    "MCP_HTTP_ALLOW_NON_LOOPBACK requires BRAIN_MCP_AUTH_MODE=credentials"
                )
            if not self.mcp_http_allowed_hosts:
                raise ValueError(
                    "MCP_HTTP_ALLOW_NON_LOOPBACK requires non-empty MCP_HTTP_ALLOWED_HOSTS"
                )
        return self

    # --- Metrics sidecar ---
    metrics_enabled: bool = Field(default=False, validation_alias=_brain_alias("METRICS_ENABLED"))
    metrics_port: int = Field(default=9200, validation_alias=_brain_alias("METRICS_PORT"))
    # Loopback by default (2026-07-04, supersedes the 0.0.0.0 of b68356c2): the
    # real consumers (red-monitor) are local; the GitLab webhook that justified
    # the LAN bind is off. Overridable through METRICS_HOST on revival (docker
    # gateway) — deliberately no loopback-only validator.
    metrics_allow_non_loopback: bool = Field(
        default=False, validation_alias=_brain_alias("METRICS_ALLOW_NON_LOOPBACK")
    )
    """The opt-in that reopens a non-loopback metrics bind (eac03668).

    `metrics_host` was the only bind in this repository with no validator at all,
    on a comment dated 2026-07-04 anticipating a docker-gateway revival. The
    revival never came and the hole stayed: a LAN bind started happily, dropped
    its three POST receivers, and their refusal came from the aiohttp router
    upstream of anything that could log it.

    Closed by default, reopened by NAME. It governs the BIND and never the
    receivers: with the opt-in the process starts, the receivers stay absent, and
    both `/healthz` and one startup line say so. It COMPOSES with
    `metrics_nonloopback_posture` rather than replacing it -- this is the outer
    gate, the posture is what happens once through it."""
    # DECLARED BEFORE `metrics_host`: its validator reads this through `info.data`,
    # which pydantic fills in field-definition order.
    metrics_host: str = Field(default="127.0.0.1", validation_alias=_brain_alias("METRICS_HOST"))
    # Posture for a NON-loopback bind (eac03668). On such a bind the three POST
    # receivers are not registered and the refusal comes from the aiohttp router,
    # invisible to the access log and to the counters alike. Three postures:
    # `silent` (historical, DEFAULT — the behaviour two tests pinned without
    # naming it), `warn` (routes still absent, one line at startup naming the
    # sacrifice), `fail_closed` (construction refuses). Choosing between the
    # three is an OPERATOR DECISION, not a fix: this field exists so it can be
    # taken in one environment variable, not in a batch.
    metrics_nonloopback_posture: Literal["silent", "warn", "fail_closed"] = Field(
        default="silent", validation_alias=_brain_alias("METRICS_NONLOOPBACK_POSTURE")
    )

    # TTL memo for the /metrics slow collectors (decision 1669d429 item 2). Red-monitor
    # polls GET /metrics roughly every 5s, and the dream, nightly-ops and
    # graph-inventory collectors it assembles together run on the order of fifteen
    # PostgreSQL queries plus a Neo4j round trip on EVERY poll, uncached. 30s (six
    # polls) trades a small staleness window for a 6x cut in that load; `database`
    # and the embedding healthcheck are deliberately excluded and stay live on every
    # poll -- they are cheap and their freshness matters more than the others'.
    metrics_slow_block_cache_ttl_seconds: float = Field(
        default=30.0, validation_alias=_brain_alias("METRICS_SLOW_BLOCK_CACHE_TTL_SECONDS")
    )
    # Applied only when a cached collector RAISES instead of degrading to `{}`/`None`
    # itself (every collector this cache wraps already does the latter -- this is
    # defense in depth for the one that doesn't, present or future). Deliberately
    # short and independent of the TTL above: caching a raised exception for the full
    # 30s window would turn one bad poll into six silent misses on the panel. One
    # error-TTL cycle, sized to roughly one poll interval, is enough for
    # single-flight to still protect a concurrent stampede on that failure without
    # freezing recovery any longer than an uncached miss would.
    metrics_slow_block_cache_error_ttl_seconds: float = Field(
        default=5.0, validation_alias=_brain_alias("METRICS_SLOW_BLOCK_CACHE_ERROR_TTL_SECONDS")
    )

    # --- Transport identity (Mcp-Session-Id, minted by the server) ---
    # Unlike the rest of this repository, this setting ships OPEN (hence
    # stateful), because its alternative is not "nothing" but "a wrong panel":
    # without a connection identifier, four engines launched in the same
    # directory declare the same actor and collapse into ONE row. The escape
    # lever is the environment, not a code edit — MCP_HTTP_STATELESS=true is
    # enough to go back to stateless mode.
    mcp_http_stateless: bool = Field(
        default=False, validation_alias=_brain_alias("MCP_HTTP_STATELESS")
    )
    # A stateful session lives in an in-memory dict and is only released on the
    # client's DELETE. A client killed outright sends none: without a deadline,
    # its state survives until the next process restart.
    #
    # Eight hours, not fifteen minutes (ticket dc51c7b5). Claude Code recovers an
    # evicted session by itself, but every recovery mints a new Mcp-Session-Id:
    # a new agent tracer, and a connection exact absorption cannot link to the
    # operator session (03291fdc). The 900 s deadline produced ~480 evictions a
    # day; eight hours covers a working day's pauses and still releases a dead
    # client's state the same day.
    mcp_http_max_body_bytes: int = Field(
        default=2_097_152,
        ge=65_536,
        le=67_108_864,
        validation_alias=_brain_alias("MCP_HTTP_MAX_BODY_BYTES"),
    )
    mcp_http_session_idle_seconds: float = Field(
        default=8 * 3600.0, validation_alias=_brain_alias("MCP_HTTP_SESSION_IDLE_SECONDS")
    )

    # --- Client activity reporting (emitter on the MCP process side) ---
    # Shipped CLOSED, like every new capability in this repository.
    # brain-mcp-http has Restart=always and the package is an editable install on
    # src/: an open default would arm the emitter at the first restart to come,
    # with nobody having decided it. The sidecar does expose the route, since
    # task 9 — so it is not the receiver that is missing, it is the arming
    # gesture. That belongs to the operator, to the rollout, with the end-to-end
    # verification and the network boundary declaration.
    client_activity_reporting_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("CLIENT_ACTIVITY_REPORTING_ENABLED")
    )
    client_activity_url: str = Field(
        default="http://127.0.0.1:9200/v1/client-activity",
        validation_alias=_brain_alias("CLIENT_ACTIVITY_URL"),
    )

    # The identity the facts registry requires of the `production` target
    # (spec 2026-09-19 measured facts and claims, §5.3 rule 4): the cluster's
    # system identifier, the database, and the server address and port as
    # PostgreSQL sees the connection — declared by the operator, NEVER derived
    # from POSTGRES_URL (a wrong DSN would confirm itself). Absent means no
    # production fact can register: the service starts without them and says
    # so, rather than measuring an unverified database. A JSON object, not a
    # colon-delimited string, so an IPv6 literal cannot be misread.
    facts_production_identity_json: str | None = Field(
        default=None,
        validation_alias=_brain_alias("FACTS_PRODUCTION_IDENTITY"),
    )
    # These declarations stay strings until composition so a typo leaves the
    # service available and merely refuses facts whose target cannot be proved.
    facts_live_release_identity_json: str | None = Field(
        default=None,
        validation_alias=_brain_alias("FACTS_LIVE_RELEASE_IDENTITY"),
    )
    facts_host_identity_json: str | None = Field(
        default=None,
        validation_alias=_brain_alias("FACTS_HOST_IDENTITY"),
    )

    # Auto-opening of an `agent` tracer session per HTTP connection, the signed
    # shape `ae0d0475` / ADR §0ter. Shipped CLOSED, like every new capability —
    # and here the reason is harder than elsewhere: armed, this flag makes the
    # server WRITE on a lifecycle boundary the covenant reserves for the user's
    # explicit commands. Arming it is an operator gesture, with its observation
    # window, never a default.
    #
    # The name follows the `BRAIN_SESSION_*` family of PLAN §8bis, but is NOT in
    # it: that summary names no auto-open flag. To be signed off before arming.
    brain_session_auto_open_enabled: bool = Field(default=False)

    # The slot-relay amendment (ADR #34): a guard mod the operator enabled counts as a
    # standing user command for ONE gesture, brain_session_relay of a bound
    # operator session onto its slot. Shipped CLOSED: while false, a relay with
    # initiator='guard_mod' is refused server-side (relay_guard_mod_disabled) and
    # the standing command is void. Arming it is an operator gesture; its live
    # value is measured in the MCP process environment, never read from a doc.
    brain_session_relay_guard_mod_enabled: bool = Field(default=False)

    # DERIVED capture: the server deposits the artifact into the connection's
    # tracer at creation time, and the user's session ABSORBS that ledger on its
    # next command. Shipped CLOSED, and here "closed" is a delivery CONDITION,
    # not caution: closing (`end`) still requires "non-empty ledger XOR
    # nothing_to_capture_reason", so arming this flag would make `end` fail
    # closed on a session whose ledger was filled without any explicit capture
    # being requested. Removing that XOR is a decision that has not been made.
    brain_session_derived_capture_enabled: bool = Field(default=False)

    # Nightly closing of unobserved `agent` tracers (M-G, migration 046).
    # Shipped CLOSED, and here "closed" is not formal caution: the sweep runs WET
    # every night from `dream.sh`, under `uv run` FROM THE REPOSITORY. Without
    # this flag, merging the rule would ARM it from the following night, with no
    # restart, no observation window and no operator gesture.
    #
    # Closed, the sweep's predicate is IDENTICAL to the pre-046 one — pinned by a
    # test, not only by this sentence.
    #
    # A flag and not a `dream.sh` argument: `test_dream_sh_sweep.py` pins
    # `sweep_args` to `["--wet"]` and refuses any further argument.
    #
    # An UNSIGNED name, like the auto-open one. To be settled before arming — it
    # is a reversible detail, not the capability.
    brain_session_inactive_sweep_enabled: bool = Field(default=False)

    # ADR 27 lot C: nightly claim verification cap (spec §4.2, Q5 default 200).
    # `1..5000` matches the spec's bound; the setting exists so an operator can
    # widen or narrow the nightly cap without a code change.
    brain_dream_verify_max_claims: int = Field(default=200, ge=1, le=5000)

    # ADR 27 claim extraction: deterministic, server-owned claims produced at
    # knowledge write time. Shipped CLOSED, like every new capability here: a
    # merge must never arm a write path the operator has not decided to switch
    # on. Arming it is an operator gesture (`BRAIN_CLAIM_EXTRACTION_ENABLED=true`
    # on the live MCP service), and switching it off stops NEW extraction
    # immediately without touching already stored claims.
    brain_claim_extraction_enabled: bool = Field(default=False)

    @field_validator("client_activity_url")
    @classmethod
    def _client_activity_loopback_only(cls, v: str) -> str:
        """The same guard as the binds, applied to an OUTPUT.

        ``mcp_http_host`` and ``automation_host`` constrain what the machine
        listens on; this URL decides what it emits, one record per tool call, in
        fire-and-forget. A LAN ``CLIENT_ACTIVITY_URL`` placed in
        ``brain-mcp-http.service``'s shared ``.env`` would therefore silently
        leave the machine. Fail-closed: what is not readable is refused, not
        ignored.
        """
        try:
            parsed = urlsplit(v)
            _ = parsed.port  # lève ValueError sur un port illisible ou hors bornes
        except ValueError as exc:
            raise ValueError("client_activity_url must be a readable http(s) URL") from exc
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"client_activity_url must be http(s) (got scheme {parsed.scheme!r})")
        host = parsed.hostname
        if not _is_loopback_host(host):
            # Only the host is copied over: a URL can carry credentials.
            raise ValueError(
                f"client_activity_url must be loopback (got {host!r}); off-host egress is forbidden"
            )
        return v

    # --- OTel tracing (spans per tool call) ---
    # Shipped CLOSED, the same doctrine as the activity emitter: the MCP process
    # restarts on its own (``Restart=always``), so an open default would arm
    # tracing from the merge onwards, towards a collector nobody deployed.
    otel_tracing_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("OTEL_TRACING_ENABLED")
    )
    otel_endpoint: str = Field(
        default="http://127.0.0.1:4318/v1/traces", validation_alias=_brain_alias("OTEL_ENDPOINT")
    )

    def facts_production_identity(self) -> dict[str, object] | None:
        """The declared identity of the production target as a mapping, or None.

        A malformed declaration raises here, at composition — never at settings
        load: spec §5.3 rule 4 wants the service UP without production facts and
        the refusal said in the journal, not a process that refuses to start on
        a typo in a drop-in. The composition root turns the mapping into a
        `SourceIdentity`, whose own validation may still refuse it (a malformed
        IP literal, a port out of range): that refusal is
        `UnverifiableTargetError` at registration.
        """
        if self.facts_production_identity_json is None:
            return None
        try:
            return _parse_production_identity(self.facts_production_identity_json)
        except ValueError as exc:
            raise ValueError(f"BRAIN_FACTS_PRODUCTION_IDENTITY is invalid: {exc}") from exc

    def facts_live_release_identity(self) -> dict[str, object] | None:
        """Return the declared release identity without making a bad drop-in fatal at load.

        The process must start even if this declaration is malformed: composition
        logs the refusal and does not register facts that would overstate trust.
        """
        if self.facts_live_release_identity_json is None:
            return None
        try:
            return _parse_identity_json(
                self.facts_live_release_identity_json,
                _LIVE_RELEASE_IDENTITY_KEYS,
                label="live release identity",
            )
        except ValueError as exc:
            raise ValueError(f"BRAIN_FACTS_LIVE_RELEASE_IDENTITY is invalid: {exc}") from exc

    def facts_host_identity(self) -> dict[str, object] | None:
        """Return the declared host identity without making a bad drop-in fatal at load.

        The process must start even if this declaration is malformed: composition
        logs the refusal and does not register facts that would overstate trust.
        """
        if self.facts_host_identity_json is None:
            return None
        try:
            return _parse_identity_json(
                self.facts_host_identity_json,
                _HOST_IDENTITY_KEYS,
                label="host identity",
            )
        except ValueError as exc:
            raise ValueError(f"BRAIN_FACTS_HOST_IDENTITY is invalid: {exc}") from exc

    @field_validator("otel_endpoint")
    @classmethod
    def _otel_endpoint_loopback_only(cls, v: str) -> str:
        """A traces endpoint is an OUTPUT, it is validated like a bind.

        The same guard as ``client_activity_url``, for the same reason and over
        more sensitive content: one span per tool call, carrying the actor and
        the tool name. A LAN endpoint placed in the SHARED ``.env`` would take
        that off the machine without anyone having decided it.
        """
        try:
            parsed = urlsplit(v)
            _ = parsed.port  # lève ValueError sur un port illisible ou hors bornes
        except ValueError as exc:
            raise ValueError("otel_endpoint must be a readable http(s) URL") from exc
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"otel_endpoint must be http(s) (got scheme {parsed.scheme!r})")
        host = parsed.hostname
        if not _is_loopback_host(host):
            # Only the host is copied over: a URL can carry credentials.
            raise ValueError(
                f"otel_endpoint must be loopback (got {host!r}); off-host egress is forbidden"
            )
        return v

    # --- Automation runtime ---
    automation_host: str = Field(
        default="127.0.0.1", validation_alias=_brain_alias("AUTOMATION_HOST")
    )
    automation_port: int = Field(
        default=9201, ge=1, le=65535, validation_alias=_brain_alias("AUTOMATION_PORT")
    )
    automation_dedup_interval_seconds: int = Field(
        default=21600, gt=0, validation_alias=_brain_alias("AUTOMATION_DEDUP_INTERVAL_SECONDS")
    )
    metrics_legacy_automation_enabled: bool = Field(
        default=True, validation_alias=_brain_alias("METRICS_LEGACY_AUTOMATION_ENABLED")
    )

    @field_validator("automation_host")
    @classmethod
    def _automation_loopback_only(cls, v: str) -> str:
        if not _is_loopback_host(v):
            raise ValueError(
                f"automation_host must be loopback (got {v!r}); non-loopback binds are forbidden"
            )
        return v

    # --- Codex management gateway ---
    brain_codex_gateway_host: str = "127.0.0.1"
    brain_codex_gateway_port: int = Field(default=9211, ge=1, le=65535)
    brain_codex_gateway_token: SecretStr = SecretStr("")
    brain_codex_gateway_allow_all_interfaces: bool = False
    brain_codex_gateway_killswitches_path: Path = Field(
        default_factory=lambda: (
            Path.home() / ".config/systemd/user/brain-v42-dream.service.d/killswitches.conf"
        )
    )

    @model_validator(mode="after")
    def _codex_gateway_private_bind_only(self) -> Self:
        host = self.brain_codex_gateway_host
        # Bandit B104 exception dated 2026-08-16 (security burn-in, ticket 7adeddf2).
        # The literal below is NOT a bind address: it is the pattern this validator
        # REFUSES. The compared value comes from the `brain_codex_gateway_host` field,
        # whose default is "127.0.0.1" and whose only other source is the
        # BRAIN_CODEX_GATEWAY_HOST environment variable; the real bind happens later, on
        # the already validated field (codex_gateway/launcher.py and
        # codex_gateway/__main__.py). Setting 0.0.0.0 without
        # `brain_codex_gateway_allow_all_interfaces=true` (default False) raises here,
        # before any startup. Invariant pinned by tests/unit/test_config_codex_gateway.py.
        if host == "0.0.0.0":  # nosec B104 - rejection, env source BRAIN_CODEX_GATEWAY_HOST
            if not self.brain_codex_gateway_allow_all_interfaces:
                raise ValueError(
                    "brain_codex_gateway_host=0.0.0.0 requires explicit "
                    "brain_codex_gateway_allow_all_interfaces=true"
                )
        elif host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"brain_codex_gateway_host must be an approved private bind (got {host!r})"
            )
        return self

    # --- Decay ---
    decay_enabled: bool = Field(default=True, validation_alias=_brain_alias("DECAY_ENABLED"))
    decay_floor: float = Field(default=0.3, validation_alias=_brain_alias("DECAY_FLOOR"))
    decay_flush_interval_seconds: int = Field(
        default=300, validation_alias=_brain_alias("DECAY_FLUSH_INTERVAL_SECONDS")
    )
    # §5.5 of the dream v2 spec — the ONLY change in this project a human would
    # feel the same day, hence shipped closed. Open, the decay reads
    # `access_count_human` and `last_accessed_at_human` instead of the totals:
    # what the MACHINE re-reads stops keeping an artifact alive. Closed, both
    # signals stay the totals and nothing changes.
    decay_human_signal_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("DECAY_HUMAN_SIGNAL_ENABLED")
    )
    stale_threshold: float = Field(default=0.5, validation_alias=_brain_alias("STALE_THRESHOLD"))
    archive_threshold: float = Field(
        default=0.2, validation_alias=_brain_alias("ARCHIVE_THRESHOLD")
    )
    forgetting_archive_days: int = Field(
        default=180, validation_alias=_brain_alias("FORGETTING_ARCHIVE_DAYS")
    )

    # --- Consolidation ---
    consolidation_interval_seconds: int = Field(
        default=21600, validation_alias=_brain_alias("CONSOLIDATION_INTERVAL_SECONDS")
    )
    consolidation_similarity_threshold: float = Field(
        default=0.92, validation_alias=_brain_alias("CONSOLIDATION_SIMILARITY_THRESHOLD")
    )

    # --- Reranker ---
    # Same target as embedding_service_url (single 8003 unified service since
    # decision 40d63a94). Default aligned to the local container to match.
    reranker_url: str = Field(
        default="http://localhost:8003", validation_alias=_brain_alias("RERANKER_URL")
    )
    reranker_timeout: float = Field(default=10.0, validation_alias=_brain_alias("RERANKER_TIMEOUT"))

    # --- PROMOTE dedup shadow verdict (W25, lot 1 — server-side, shadow only) ---
    # Bounds measured against the live dream_promotions/ADR/runbook corpus
    # (W25-promote-nearest-tool-design.md §1.3, replayed and reproduced to the
    # thousandth): raw pgvector cosine `1 - (l.embedding <=> x.embedding)`.
    #   ADR:     duplicate-proxy (learning -> its own ADR) min 0.760, n=72;
    #            non-duplicate (promoted learning -> nearest PRIOR ADR) max
    #            0.820, n=66. Overlap band: [0.760, 0.820].
    #   Runbook: duplicate-proxy min 0.712, n=35; non-duplicate max 0.818,
    #            n=33. Overlap band: [0.712, 0.818].
    # Below the low bound: no historical duplicate-proxy ever measured that
    # low -> band "clear". Above the high bound: no historical non-duplicate
    # ever measured that high -> band "block". Inside the closed interval
    # (bounds included, since both were themselves observed on the opposite
    # population): band "borderline" -- the server says "I don't know"
    # instead of guessing. Per family, never a shared constant: the two
    # families embed different text (embedding_text.py -- learning excludes
    # nothing, adr = "title context decision", runbook = "title description
    # trigger" with steps EXCLUDED), which is exactly why the runbook anchor
    # sits lower than the ADR one.
    promote_dedup_borderline_low_adr: float = Field(
        default=0.760, validation_alias=_brain_alias("PROMOTE_DEDUP_BORDERLINE_LOW_ADR")
    )
    promote_dedup_block_adr: float = Field(
        default=0.820, validation_alias=_brain_alias("PROMOTE_DEDUP_BLOCK_ADR")
    )
    promote_dedup_borderline_low_runbook: float = Field(
        default=0.712, validation_alias=_brain_alias("PROMOTE_DEDUP_BORDERLINE_LOW_RUNBOOK")
    )
    promote_dedup_block_runbook: float = Field(
        default=0.818, validation_alias=_brain_alias("PROMOTE_DEDUP_BLOCK_RUNBOOK")
    )

    # --- Neo4j (optional — disabled by default) ---
    neo4j_url: str | None = Field(default=None, validation_alias=_brain_alias("NEO4J_URL"))
    neo4j_user: str = Field(default="neo4j", validation_alias=_brain_alias("NEO4J_USER"))
    neo4j_password: str = Field(default="", validation_alias=_brain_alias("NEO4J_PASSWORD"))
    neo4j_timeout: float = Field(default=5.0, validation_alias=_brain_alias("NEO4J_TIMEOUT"))
    graph_enabled: bool = Field(default=False, validation_alias=_brain_alias("GRAPH_ENABLED"))

    # --- Canonical graph ledger (additive cutover) ---
    # The schema can be deployed and backfilled while writes still use the
    # historical Neo4j-only path. Enable only after migrations 033-035 are applied.
    graph_ledger_write_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("GRAPH_LEDGER_WRITE_ENABLED")
    )
    graph_outbox_interval_seconds: float = Field(
        default=5.0, gt=0, validation_alias=_brain_alias("GRAPH_OUTBOX_INTERVAL_SECONDS")
    )
    graph_outbox_batch_size: int = Field(
        default=100, ge=1, le=1000, validation_alias=_brain_alias("GRAPH_OUTBOX_BATCH_SIZE")
    )
    graph_outbox_max_attempts: int = Field(
        default=10, ge=1, le=100, validation_alias=_brain_alias("GRAPH_OUTBOX_MAX_ATTEMPTS")
    )

    # The projector credential is intentionally separate from the legacy
    # NEO4J_* settings so it can live in a service-private 0600 environment.
    graph_projector_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("GRAPH_PROJECTOR_ENABLED")
    )
    graph_projector_neo4j_url: str | None = Field(
        default=None, validation_alias=_brain_alias("GRAPH_PROJECTOR_NEO4J_URL")
    )
    graph_projector_neo4j_user: str = Field(
        default="neo4j", validation_alias=_brain_alias("GRAPH_PROJECTOR_NEO4J_USER")
    )
    graph_projector_neo4j_password: SecretStr = Field(
        default=SecretStr(""), validation_alias=_brain_alias("GRAPH_PROJECTOR_NEO4J_PASSWORD")
    )

    @field_validator("graph_projector_neo4j_url")
    @classmethod
    def _validate_projector_neo4j_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("invalid graph projector Neo4j URL") from exc
        allowed_schemes = {
            "bolt",
            "bolt+s",
            "bolt+ssc",
            "neo4j",
            "neo4j+s",
            "neo4j+ssc",
        }
        if (
            parsed.scheme not in allowed_schemes
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("graph projector Neo4j URL must be a credential-free Bolt URI")
        return value

    @model_validator(mode="after")
    def _ledger_requires_projection_driver(self) -> Self:
        if self.graph_ledger_write_enabled and not self.graph_enabled:
            raise ValueError("graph_ledger_write_enabled requires graph_enabled")
        if self.graph_projector_enabled and not self.graph_ledger_write_enabled:
            raise ValueError("graph_projector_enabled requires graph_ledger_write_enabled")
        if self.graph_projector_enabled and (
            not self.graph_projector_neo4j_url
            or not self.graph_projector_neo4j_user
            or not self.graph_projector_neo4j_password.get_secret_value()
        ):
            raise ValueError("graph_projector_enabled requires isolated Neo4j credentials")
        if self.graph_projector_enabled and (self.neo4j_url or self.neo4j_password):
            raise ValueError("legacy NEO4J credentials must be absent when graph_projector_enabled")
        return self

    # --- GitLab webhooks ---
    gitlab_webhook_secret: str = Field(
        default="", validation_alias=_brain_alias("GITLAB_WEBHOOK_SECRET")
    )

    # --- Plan index: periodic refresh ---
    # Ships CLOSED. This loop WRITES: an indexed plan reaches ClusterGuard and
    # `plan` is in CREATING_SIGNALS, so a sweep can create features. The
    # roadmap tap is under observation -- the purge of pseudo-features waits
    # for the dry-up to be judged established -- and arming a periodic creator
    # by default would change the very thing being measured. Until an operator
    # arms it, `brain_reindex_plans` remains the way to say "now".
    plan_index_refresh_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("PLAN_INDEX_REFRESH_ENABLED")
    )
    plan_index_refresh_interval_seconds: int = Field(
        default=900,
        gt=0,
        validation_alias=_brain_alias("PLAN_INDEX_REFRESH_INTERVAL_SECONDS"),
    )
    """Seconds between two sweeps. 900 s keeps a new plan findable within the
    quarter hour while an unchanged corpus costs one read and one lookup per
    file and no embedding at all."""

    # --- Plan-index repair (canonical multi-project maintenance) ---
    # No personal default: an unconfigured root must fail closed, not guess
    # a path. brain_v42.maintenance.plan_index_repair reads this lazily via
    # get_settings(), never at import time (see its _resolve_target_projects_root).
    brain_plan_projects_root: Path | None = None
    """Root directory holding the ReD_v1-style project tree this repair
    boundary scans. Env: BRAIN_PLAN_PROJECTS_ROOT."""

    # --- Neo4j graph init: optional project hierarchy seed ---
    # config/project_hierarchy.yml is tracked only as project_hierarchy.example.yml
    # (real project topology is operator-private). Resolved relative to the
    # current working directory at call time -- init_graph.py is an ops
    # script run from a repo checkout, not a portable installed entry
    # point, so there is no "package-relative" path that would survive a
    # real wheel install anyway. Missing file is a graceful no-op, not an
    # error (see create_project_hierarchy in scripts/init_graph.py).
    brain_project_hierarchy_path: Path = Field(
        default=Path("config/project_hierarchy.yml"),
        validation_alias=_brain_alias("PROJECT_HIERARCHY_PATH"),
    )

    # --- Cross-project (Dream v3 Spec C MVP β) ---
    brain_dream_cross_project_enabled: bool = False
    """Master killswitch for cross-project briefing section + resonance script."""

    brain_cross_project_briefing_domains_top_n: int = 2
    """Top-N active domains of the current project surfaced in the briefing."""

    brain_cross_project_briefing_entries_max: int = 5
    """Cap on cross-project entries rendered in the briefing."""

    @field_validator("postgres_url")
    @classmethod
    def validate_postgres_url(cls, v: str) -> str:
        """Ensure the postgres_url uses the asyncpg driver scheme."""
        if not re.match(r"^postgresql\+asyncpg://", v):
            raise ValueError(
                "postgres_url must use postgresql+asyncpg:// scheme. "
                "Example: postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance (singleton).

    Uses lru_cache to ensure a single instance is created per process.
    This is important for performance (avoids re-reading env vars on every call)
    and for consistency (same object throughout the application lifecycle).

    Usage:
        from brain_v42.config import get_settings
        settings = get_settings()
        engine = create_async_engine(settings.postgres_url)
    """
    return Settings()  # type: ignore[call-arg]  # postgres_url loaded from env var
