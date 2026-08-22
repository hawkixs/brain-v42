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
    # Loopback par défaut (2026-07-04, supersède le 0.0.0.0 de b68356c2) :
    # les consommateurs réels (red-monitor) sont locaux ; le webhook GitLab
    # qui justifiait le bind LAN est off. Overridable via METRICS_HOST si
    # revival (gateway docker) — pas de validator loopback-only exprès.
    metrics_host: str = Field(default="127.0.0.1", validation_alias=_brain_alias("METRICS_HOST"))

    # --- Identité de transport (Mcp-Session-Id frappé par le serveur) ---
    # Contrairement au reste du dépôt, ce réglage est livré OUVERT (donc avec
    # état), parce que son alternative n'est pas « rien » mais « un panneau
    # faux » : sans identifiant de connexion, quatre moteurs lancés dans un
    # même répertoire déclarent le même acteur et s'effondrent en UNE ligne.
    # Le levier de secours est l'environnement, pas une édition de code —
    # MCP_HTTP_STATELESS=true suffit à revenir au mode sans état.
    mcp_http_stateless: bool = Field(
        default=False, validation_alias=_brain_alias("MCP_HTTP_STATELESS")
    )
    # Une session sans état vit dans un dict en mémoire et n'est rendue qu'au
    # DELETE du client. Un client tué net n'en envoie pas : sans échéance, son
    # état survit jusqu'au prochain redémarrage du processus.
    mcp_http_session_idle_seconds: float = Field(
        default=900.0, validation_alias=_brain_alias("MCP_HTTP_SESSION_IDLE_SECONDS")
    )

    # --- Client activity reporting (émetteur côté processus MCP) ---
    # Livré FERMÉ, comme toute capacité neuve de ce dépôt. brain-mcp-http a
    # Restart=always et le paquet est en install éditable sur src/ : un défaut
    # ouvert armerait l'émetteur au premier redémarrage venu, sans que
    # personne l'ait décidé. Le sidecar, lui, expose bien la route depuis la
    # tâche 9 — ce n'est donc pas le récepteur qui manque, c'est le geste
    # d'armement. Il appartient à l'opérateur, au rollout, avec la
    # vérification de bout en bout et la déclaration de frontière réseau.
    client_activity_reporting_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("CLIENT_ACTIVITY_REPORTING_ENABLED")
    )
    client_activity_url: str = Field(
        default="http://127.0.0.1:9200/v1/client-activity",
        validation_alias=_brain_alias("CLIENT_ACTIVITY_URL"),
    )

    # Auto-ouverture d'une session traçante `agent` par connexion HTTP, forme
    # signée `ae0d0475` / ADR §0ter. Livré FERMÉ, comme toute capacité neuve —
    # et ici la raison est plus dure qu'ailleurs : armé, ce drapeau fait ÉCRIRE
    # le serveur sur une frontière de cycle de vie que le covenant réserve aux
    # commandes explicites de l'utilisateur. Son armement est un geste
    # d'opérateur, avec sa fenêtre d'observation, jamais un défaut.
    #
    # Le nom suit la famille `BRAIN_SESSION_*` du PLAN §8bis, mais ne s'y
    # trouve PAS : ce récapitulatif ne nomme aucun drapeau d'auto-ouverture.
    # À faire signer avant armement.
    brain_session_auto_open_enabled: bool = Field(default=False)

    # Fermeture nocturne des traçantes `agent` inobservées (M-G, migration 046).
    # Livré FERMÉ, et ici « fermé » n'est pas de la prudence de forme : le
    # balayage tourne WET toutes les nuits depuis `dream.sh`, en `uv run` DEPUIS
    # LE DÉPÔT. Sans ce drapeau, merger la règle l'ARMERAIT dès la nuit suivante,
    # sans redémarrage, sans fenêtre d'observation et sans geste d'opérateur.
    #
    # Fermé, le prédicat du balayage est IDENTIQUE à celui d'avant la 046 —
    # épinglé par un test, pas seulement par cette phrase.
    #
    # Un drapeau et non un argument de `dream.sh` : `test_dream_sh_sweep.py`
    # épingle `sweep_args` à `["--wet"]` et refuse tout argument de plus.
    #
    # Nom NON SIGNÉ, comme celui de l'auto-ouverture. À faire trancher avant
    # armement — c'est un détail réversible, pas la capacité.
    brain_session_inactive_sweep_enabled: bool = Field(default=False)

    @field_validator("client_activity_url")
    @classmethod
    def _client_activity_loopback_only(cls, v: str) -> str:
        """Même garde que les binds, appliquée à une SORTIE.

        ``mcp_http_host`` et ``automation_host`` contraignent ce que la machine
        écoute ; cette URL décide de ce qu'elle émet, un enregistrement par
        appel de tool, en feu-et-oubli. Un ``CLIENT_ACTIVITY_URL`` LAN posé
        dans le ``.env`` partagé de ``brain-mcp-http.service`` sortirait donc
        silencieusement de la machine. Fail-closed : ce qui n'est pas lisible
        est refusé, pas ignoré.
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
            # Seul l'hôte est recopié : une URL peut porter des identifiants.
            raise ValueError(
                f"client_activity_url must be loopback (got {host!r}); off-host egress is forbidden"
            )
        return v

    # --- Tracing OTel (spans par appel de tool) ---
    # Livré FERMÉ, même doctrine que l'émetteur d'activité : le processus MCP
    # redémarre seul (``Restart=always``), donc un défaut ouvert armerait le
    # tracing dès la fusion, vers un collecteur que personne n'a déployé.
    otel_tracing_enabled: bool = Field(
        default=False, validation_alias=_brain_alias("OTEL_TRACING_ENABLED")
    )
    otel_endpoint: str = Field(
        default="http://127.0.0.1:4318/v1/traces", validation_alias=_brain_alias("OTEL_ENDPOINT")
    )

    @field_validator("otel_endpoint")
    @classmethod
    def _otel_endpoint_loopback_only(cls, v: str) -> str:
        """Un endpoint de traces est une SORTIE, il se valide comme un bind.

        Même garde que ``client_activity_url``, pour la même raison et un
        contenu plus sensible : un span par appel de tool, portant l'acteur et
        le nom du tool. Un endpoint LAN posé dans le ``.env`` PARTAGÉ sortirait
        ça de la machine sans que personne ne l'ait décidé.
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
            # Seul l'hôte est recopié : une URL peut porter des identifiants.
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
        # Exception bandit B104 datée du 2026-08-16 (burn-in sécurité, ticket 7adeddf2).
        # Le littéral ci-dessous n'est PAS une adresse de bind : c'est le motif que ce
        # validateur REFUSE. La valeur comparée vient du champ `brain_codex_gateway_host`,
        # dont le défaut est "127.0.0.1" et dont la seule autre source est la variable
        # d'environnement BRAIN_CODEX_GATEWAY_HOST ; le bind réel se fait plus loin, sur
        # le champ déjà validé (codex_gateway/launcher.py et codex_gateway/__main__.py).
        # Poser 0.0.0.0 sans `brain_codex_gateway_allow_all_interfaces=true` (défaut False)
        # lève ici, avant tout démarrage. Invariant épinglé par
        # tests/unit/test_config_codex_gateway.py.
        if host == "0.0.0.0":  # nosec B104 - rejet, source env BRAIN_CODEX_GATEWAY_HOST
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
    # §5.5 de la spec dream v2 — le SEUL changement du chantier qu'un humain
    # sentirait le jour même, donc livré fermé. Ouvert, le decay lit
    # `access_count_human` et `last_accessed_at_human` au lieu des totaux :
    # ce que la MACHINE relit cesse de faire vivre un artefact. Fermé, les
    # deux signaux restent les totaux et rien ne change.
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
