"""The five assertions the repository makes about `CLAUDE.md`, owned ONCE.

Ticket 87ac8b7a. `CLAUDE.md` carries claims of MEASURABLE state — the schema
revision in production, the MCP transport URL, the reranker port, the network
boundary, the FastMCP major — and nothing confronted them with the source until
`test_documentation_contract.py` grew assertions for them. Those assertions are
the addressable unit the ticket asked for; they simply were not reusable.

This module holds them, and BOTH consumers import from here:

* `tests/unit/test_documentation_contract.py`, which fails the suite;
* `scripts/dream/post_run_alert.py`, which counts the reds in the morning report
  the operator actually reads.

The single path is the point. A fragment rebuilt on one side could go green on a
document the other side rejects — the very drift this ticket describes,
reproduced inside its own fix.

Everything that CAN be derived IS derived, at call time: the Alembic head, the
port from `Settings`, the transport URL from `.mcp.json`, the FastMCP major from
`uv.lock`. A retyped number goes stale exactly like the document it guards. What
stays literal is prose that has no machine-readable source — the three
operator-facing paragraphs — and it lives here rather than in a test so the
report cannot hold a different copy of it.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"


@dataclass(frozen=True)
class Claim:
    """One assertion, named by the test that owns it.

    `id` is the test's name minus its prefix, so a red in the morning report and
    a red in the suite are recognisably the same fact.
    """

    id: str
    expected: str
    #: Compare against whitespace-normalized text. The boundary paragraph is
    #: wrapped differently in each document it appears in; the wrapping is not
    #: the contract.
    normalized: bool = False

    def holds(self, text: str) -> bool:
        haystack = " ".join(text.split()) if self.normalized else text
        needle = " ".join(self.expected.split()) if self.normalized else self.expected
        return needle in haystack


def repository_head(root: Path | None = None) -> str:
    """The single Alembic head, read from the chain and never retyped."""
    from alembic.config import Config  # noqa: PLC0415
    from alembic.script import ScriptDirectory  # noqa: PLC0415

    base = root or REPO_ROOT
    config = Config(str(base / "alembic.ini"))
    config.set_main_option("script_location", str(base / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one alembic head, found {heads}")
    return heads[0]


def _locked_version(package_name: str, root: Path | None = None) -> str:
    with ((root or REPO_ROOT) / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)
    versions = {
        package["version"] for package in lock["package"] if package["name"] == package_name
    }
    if len(versions) != 1:
        raise RuntimeError(f"expected one locked version for {package_name}, got {versions}")
    return str(versions.pop())


def _production_mcp_url(root: Path | None = None) -> str:
    config = json.loads(((root or REPO_ROOT) / ".mcp.json").read_text(encoding="utf-8"))
    return str(config["mcpServers"]["brain-v42"]["url"])


def _reranker_port() -> int:
    from urllib.parse import urlsplit  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415

    default = Settings.model_fields["reranker_url"].default
    port = urlsplit(str(default)).port
    if port is None:
        raise RuntimeError(f"reranker_url has no port: {default!r}")
    return port


#: The three operator-facing paragraphs. Literal because they have no
#: machine-readable source — and here rather than in a test so that the guard and
#: the report cannot hold two copies of the same sentence.
NETWORK_BOUNDARY_CONTRACT = (
    "**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to "
    "loopback; metrics and automation default to loopback. The versioned Compose target "
    "binds the embedding host publish to loopback and the live runtime matches it — "
    "measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. "
    "Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and "
    "non-empty in the live server process, and `POST /mcp` answers `401` both without a "
    "bearer and with a wrong one. The dedicated Docker client network exists and carries "
    "the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. "
    "Repository-managed WAN isolation remains unproven — the repository manages no "
    "firewall rule at all. What would make this paragraph false again, and is watched by "
    "no test: a host-publish override reopening `:8003`, or `MCP_HTTP_TOKEN` cleared. "
    "`METRICS_HOST` has LEFT that list: since 2026-09-03 (`6c61b63`) a fail-closed "
    "validator refuses a non-loopback bind unless `METRICS_ALLOW_NON_LOOPBACK` names the "
    "decision, and under that opt-in the three POST receivers stay unregistered and say "
    "so on `/healthz`. Re-measure with `ss -ltnp`, "
    "`docker port` and an unauthenticated `POST /mcp` — do not copy this line forward."
)

SHIM_LIMITS_CONTRACT = (
    "**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read "
    "timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum "
    "JSON depth 64, one embedding calculation and one rerank calculation per worker. "
    "Saturation returns short `503` JSON with `Retry-After: 1`."
)

SEC2_RESIDUALS_CONTRACT = (
    "**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated "
    "Docker client network are done — the coordinated `auto-discord` cutover happened, and "
    "both `auto-discord` containers sit on `brain-net`. One residual stands, and it is "
    "wider than previously written: the versioned legacy PyTorch profile remains unbounded "
    "— `services/embedding/main.py` carries no body cap, no read deadline, no concurrency "
    "semaphore and no `413`/`503` — and it preserves neither of the two DNS names its "
    "clients use. A `--profile legacy` rollback publishes `embedding` and "
    "`brain_v42_embedding` on `brain-net`, while the compose sets "
    "`EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no "
    "`EMBEDDING_URL` of its own, falls back to the code default "
    "`http://brain_v42_embedding_shim:8003`. Two names break, not one."
)


def mcp_transport_contract(root: Path | None = None) -> str:
    return (
        f"**MCP transport**: production = HTTP loopback `{_production_mcp_url(root)}`; "
        "configuration default and dev/fallback = `stdio`."
    )


def reranker_contract() -> str:
    return f"unified embedding endpoint `:{_reranker_port()}/rerank`"


def fastmcp_contract(root: Path | None = None) -> str:
    return f"FastMCP {_locked_version('fastmcp', root).split('.', maxsplit=1)[0]}.x"


def claims(root: Path | None = None) -> tuple[Claim, ...]:
    """The five, derived fresh on every call."""
    return (
        Claim("documented_migration_head", f"migration {repository_head(root)}"),
        Claim("documented_mcp_transport", mcp_transport_contract(root)),
        Claim("documented_reranker_endpoint", reranker_contract()),
        Claim("documented_network_boundary", NETWORK_BOUNDARY_CONTRACT, normalized=True),
        Claim("documented_fastmcp_major", fastmcp_contract(root)),
    )


def failing(text: str, root: Path | None = None) -> tuple[Claim, ...]:
    """The claims this document does NOT satisfy.

    `documented_migration_head` is matched case-insensitively, as the contract
    does: the document says "migration 052" in prose that may capitalise it.
    """
    lowered = text.lower()
    return tuple(
        claim
        for claim in claims(root)
        if not (
            claim.holds(lowered) if claim.id == "documented_migration_head" else claim.holds(text)
        )
    )
