"""The morning report replays the local assertions on CLAUDE.md and counts the reds.

Ticket 87ac8b7a. `CLAUDE.md` is the document the operator reads first and the one
nothing confronts with the source: the briefing DERIVES the schema revision but
does not read the file, and the five assertions that do read it live in
`test_documentation_contract.py`, behind a `pytest` nobody runs at the moment they
are reading the document. Measured 2026-09-03: the file said `migration 049` while
production had been at `052` since 11:20, and its network-boundary paragraph had
fallen behind a fail-closed validator that landed the same morning.

No new power: `post_run_alert` already runs every morning from the repository with
filesystem access. No prose parsing either — the addressable unit the ticket asks
for already exists, and it is the assertion itself. The claims are IMPORTED from
one module that `test_documentation_contract` imports too, so a fragment can never
drift between the guard and the report.

MUTE ON THE NOMINAL PATH, and that is a constraint rather than a nicety: an alarm
that fires every night stops being read, and this repository has already paid that
price (learning `4480d3df`).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _block(path: Path) -> list[str]:
    from scripts.dream.post_run_alert import build_claude_md_block

    return build_claude_md_block(path)


def _conforming_text() -> str:
    from brain_v42.documentation.claude_md_claims import claims

    return "\n\n".join(claim.expected for claim in claims())


def test_a_conforming_document_says_nothing(tmp_path: Path) -> None:
    """The nominal path is silent. Anything else and the rubric trains its reader
    to skip it, which is the failure mode this block exists to avoid."""
    document = tmp_path / "CLAUDE.md"
    document.write_text(_conforming_text(), encoding="utf-8")

    assert _block(document) == []


def test_a_missing_document_is_not_a_failure(tmp_path: Path) -> None:
    """`CLAUDE.md` is gitignored since the open-source publication.

    On a clean checkout — CI, a fresh clone — the file simply is not there. That
    is the documented state, not a red: a block shouting about an absence would
    be noise on every machine that never had the file.
    """
    assert _block(tmp_path / "CLAUDE.md") == []


def test_a_stale_document_names_the_claims_it_breaks(tmp_path: Path) -> None:
    """Two reds, named — the exact pair measured on 2026-09-03."""
    from brain_v42.documentation.claude_md_claims import claims

    kept = [
        claim.expected
        for claim in claims()
        if claim.id not in {"documented_migration_head", "documented_network_boundary"}
    ]
    stale = "\n\n".join(
        [
            "La production a été mesurée à migration 049 le 2026-08-30.",
            "**Tracked network boundary** (replayed 2026-08-23): an older paragraph.",
            *kept,
        ]
    )
    document = tmp_path / "CLAUDE.md"
    document.write_text(stale, encoding="utf-8")

    block = _block(document)

    assert block, "a stale document must not be silent"
    joined = " ".join(block)
    assert "CLAUDE.md" in joined
    assert "2 assertion" in joined
    assert "documented_migration_head" in joined
    assert "documented_network_boundary" in joined
    # and NOT the three that still hold
    assert "documented_reranker_endpoint" not in joined
    assert "documented_fastmcp_major" not in joined


def test_the_claims_are_the_ones_the_documentation_contract_asserts() -> None:
    """One path, and this is what pins it.

    If a fragment were rebuilt here instead of imported, the report could go green
    on a document the contract rejects — the exact drift the ticket describes,
    reproduced inside its own fix.
    """
    from brain_v42.documentation.claude_md_claims import claims

    ids = {claim.id for claim in claims()}
    assert ids == {
        "documented_migration_head",
        "documented_mcp_transport",
        "documented_reranker_endpoint",
        "documented_network_boundary",
        "documented_fastmcp_major",
    }
    for claim in claims():
        assert claim.expected, f"{claim.id} has an empty expectation"


@pytest.mark.parametrize("claim_id", ["documented_migration_head", "documented_fastmcp_major"])
def test_each_claim_is_derived_from_the_repository_not_typed(claim_id: str) -> None:
    """A retyped number goes stale exactly like the document it guards."""
    from brain_v42.documentation.claude_md_claims import claims

    claim = next(c for c in claims() if c.id == claim_id)
    if claim_id == "documented_migration_head":
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parents[2]
        head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads()[0]
        assert head in claim.expected
    else:
        assert "FastMCP" in claim.expected
