"""brain_v42 configuration via pydantic-settings.

All settings are loaded from environment variables or .env file.
HTTP transport is an opt-in, loopback-only mode; stdio is the default.
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

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# pgvector 0.8.2 refuses an HNSW index on a column wider than this
# ("column cannot have more than 2000 dimensions for hnsw index"), and every
# embedding column in this schema carries one. Bounding the setting turns a
# detonation inside a migration's CREATE INDEX into a config-load error.
PGVECTOR_HNSW_MAX_DIMENSIONS = 2000

# Settings sets `str_strip_whitespace=True` model-wide. Every asymmetric-model
# instruction prefix worth having ends in a space ("query: ", "passage: "), and
# that space is what separates the prefix from the text. These fields opt out.
_UnstrippedStr = Annotated[str, StringConstraints(strip_whitespace=False)]


def _brain_alias(legacy_env: str) -> AliasChoices:
    """BRAIN_<legacy_env> is preferred; the pre-migration bare name still works.

    First alias present in the environment wins (pydantic-settings resolves
    AliasChoices in order), so BRAIN_POSTGRES_URL overrides POSTGRES_URL if
    both happen to be set -- but nothing needs to change for deployments that
    only know the bare name.
    """
    return AliasChoices(f"BRAIN_{legacy_env}", legacy_env)


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

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", validation_alias=_brain_alias("LOG_LEVEL")
    )

    # --- Embedding ---
    # Default points at the local PC-serveur container (restore 2026-07-06,
    # reverses the 2026-04-15 dev-pc cutover after the dev-pc crash).
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
    rerank_backend: Literal["shim", "cohere"] = Field(
        default="shim", validation_alias=_brain_alias("RERANK_BACKEND")
    )
    rerank_model: str = Field(default="", validation_alias=_brain_alias("RERANK_MODEL"))
    """Model name sent by the cohere backend. Required by hosted providers."""

    rerank_api_key: SecretStr = Field(
        default=SecretStr(""), repr=False, validation_alias=_brain_alias("RERANK_API_KEY")
    )

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
    mcp_http_host: str = Field(
        default="127.0.0.1", validation_alias=_brain_alias("MCP_HTTP_HOST")
    )  # loopback-only
    mcp_http_port: int = Field(default=8765, validation_alias=_brain_alias("MCP_HTTP_PORT"))
    mcp_http_token: str = Field(
        default="", repr=False, validation_alias=_brain_alias("MCP_HTTP_TOKEN")
    )
    """Bearer token for HTTP transport auth (opt-in).

    Empty string (default) = auth disabled — current fleet behaviour is preserved
    without any changes to .mcp.json files.

    Non-empty = BearerTokenGuard is activated; every non-/health HTTP request must
    carry ``Authorization: Bearer <token>``.

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

    @field_validator("mcp_http_host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"mcp_http_host must be loopback (got {v!r}); bind-0.0.0.0 is forbidden"
            )
        return v

    # --- Metrics sidecar ---
    metrics_enabled: bool = Field(default=False, validation_alias=_brain_alias("METRICS_ENABLED"))
    metrics_port: int = Field(default=9200, validation_alias=_brain_alias("METRICS_PORT"))
    # Loopback by default (2026-07-04, supersedes the 0.0.0.0 of b68356c2): the
    # real consumers (red-monitor) are local; the GitLab webhook that justified
    # the LAN bind is off. Overridable through METRICS_HOST on revival (docker
    # gateway) — deliberately no loopback-only validator.
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
    mcp_http_session_idle_seconds: float = Field(
        default=900.0, validation_alias=_brain_alias("MCP_HTTP_SESSION_IDLE_SECONDS")
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
        if host not in {"127.0.0.1", "localhost", "::1"}:
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
        if host not in {"127.0.0.1", "localhost", "::1"}:
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
        if v not in {"127.0.0.1", "localhost", "::1"}:
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
