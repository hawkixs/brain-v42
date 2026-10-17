"""The five assertions the repository makes about `CLAUDE.md`, owned ONCE.

Ticket 87ac8b7a. `CLAUDE.md` carries claims of MEASURABLE state — the schema
revision in production, the MCP transport URL, the hosted reranker, the network
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
local transport URL from the `Settings` MCP HTTP defaults, the FastMCP major from
`uv.lock`. A retyped number goes stale exactly like the document it guards. What
stays literal is the operator-facing prose, including the hosted reranker
contract checked against deploy/compose.yaml by the documentation tests. It
lives here so the report cannot hold a different copy of the same assertion.
"""

from __future__ import annotations

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
    #: one identical line in each document; normalization keeps incidental
    #: whitespace outside the contract.
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


def _local_mcp_url() -> str:
    # The repository's .mcp.json declares no server: each operator configures the
    # client at user scope. The local HTTP example follows the server's defaults;
    # the production host publish uses BIND_ADDRESS instead.
    from brain_v42.config import Settings  # noqa: PLC0415

    host = Settings.model_fields["mcp_http_host"].default
    port = Settings.model_fields["mcp_http_port"].default
    return f"http://{host}:{port}/mcp"


#: The three operator-facing paragraphs. Literal because they have no
#: machine-readable source — and here rather than in a test so that the guard and
#: the report cannot hold two copies of the same sentence.
NETWORK_BOUNDARY_CONTRACT = (
    "**Tracked network boundary**: on red-base (`deploy/compose.yaml`) only two "
    "services publish a host port, MCP (`:8765`) and metrics (`:9200`), both on the "
    "host's WireGuard address (`BIND_ADDRESS`); no host publish is ever `0.0.0.0`, "
    "even though each container listens on all of its own interfaces. MCP runs in "
    "credentials mode (`BRAIN_MCP_AUTH_MODE: credentials`): the shared "
    "`MCP_HTTP_TOKEN` bearer is refused and must be absent, and `POST /mcp` answers "
    "`401` without a valid credential. Metrics opts into the non-loopback bind "
    "through `METRICS_ALLOW_NON_LOOPBACK`, which the fail-closed validator "
    "(`6c61b63`) requires, and its POST receivers are gated by credentials "
    "(`METRICS_RECEIVER_AUTH: credentials`). PostgreSQL and Neo4j publish no host "
    "port. The host firewall is the perimeter: the `DOCKER-USER` chain on `wg0` is "
    "owned by `red-watcher`, and the repository manages no firewall rule. The "
    "development `docker-compose.yml` binds to loopback, which remains the "
    "local-development default. What would make this paragraph false again: a host "
    "publish outside `BIND_ADDRESS`, or MCP leaving credentials mode. Re-measure "
    "with `ss -ltnp`, `docker port` and an unauthenticated `POST /mcp` — do not "
    "copy this line forward."
)

SHIM_LIMITS_CONTRACT = (
    "**Optional local embedding shim limits**: the retained development source enforces 8 "
    "MiB body, 5 s body-read timeout, 8 concurrent ingress reads, 100 embed texts, 128 "
    "rerank candidates, maximum JSON depth 64, one embedding calculation and one rerank "
    "calculation per worker. Saturation returns short `503` JSON with `Retry-After: 1`. "
    "The development `docker-compose.yml` still defines the shim for local use; it does not "
    "serve production search."
)

SEC2_RESIDUALS_CONTRACT = (
    "**SEC2 residuals**: the retained legacy source `services/embedding/main.py` has no "
    "body cap, no read deadline, no concurrency semaphore and no `413`/`503`. The "
    "development `docker-compose.yml` still defines `embedding`, `embedding-llama` and "
    "`embedding-shim`; their removal waits on brain ticket `14bb4157` (the auto-discord bot "
    "still embeds through the shim). This source is "
    "not a production dependency or a reranker rollback; any separate local deployment "
    "needs its own network and authentication boundary."
)


def mcp_transport_contract() -> str:
    from urllib.parse import urlsplit  # noqa: PLC0415

    local_url = _local_mcp_url()
    return (
        "**MCP transport**: production (red-base) = HTTP on the host's WireGuard address "
        f"(`BIND_ADDRESS`, port {urlsplit(local_url).port}, path `/mcp`) in credentials mode, "
        "every call authenticated; local HTTP (workstation/dev) = loopback "
        f"`{local_url}` (the configuration default bind); "
        "configuration default and dev/fallback transport = `stdio`."
    )


def reranker_contract() -> str:
    return (
        "**Reranker**: production uses hosted Voyage `rerank-3-lite` with OpenRouter "
        "`cohere/rerank-4-fast` fallback via `deploy/compose.yaml` (`x-rerank-env`). "
        'Rollback replaces that block with `BRAIN_RERANK_BACKEND="none"` alone.'
    )


def fastmcp_contract(root: Path | None = None) -> str:
    return f"FastMCP {_locked_version('fastmcp', root).split('.', maxsplit=1)[0]}.x"


def claims(root: Path | None = None) -> tuple[Claim, ...]:
    """The five, derived fresh on every call."""
    return (
        Claim("documented_migration_head", f"migration {repository_head(root)}"),
        Claim("documented_mcp_transport", mcp_transport_contract()),
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
