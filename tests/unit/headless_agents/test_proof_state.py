"""Proof status per rail and kind, for a person, before a run fails (spec 0.5.2, lot 2).

The engine's own gate (:mod:`headless_agents.proofs`) stays the sole authority for
whether a rail may run: every invariant test here re-derives the expected verdict from
:func:`headless_agents.proofs.isolation_ok` and :func:`headless_agents.proofs.confinement`
directly, never from a status this module also computed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.proof_state import (
    UNPROVABLE_CONFINEMENT,
    proof_status,
    rail_state,
    reprove_command,
)
from headless_agents.proofs import confinement, isolation_ok, proof_path, record_proof
from headless_agents.state import publish

# ── proof_status: the five statuses ─────────────────────────────────────────


def test_no_file_is_missing_for_both_kinds(tmp_path: Path) -> None:
    assert proof_status(tmp_path, "codex", "isolation", "codex 1.0").status == "missing"
    assert proof_status(tmp_path, "codex", "confinement", "codex 1.0").status == "missing"


def test_a_passing_isolation_for_the_installed_version_is_passed(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1.0", isolation=True, today="2026-09-25")
    status = proof_status(tmp_path, "codex", "isolation", "codex 1.0")
    assert status.status == "passed"
    assert status.date == "2026-09-25"
    assert status.recorded_version == "codex 1.0"


def test_a_failed_proof_is_failed_with_its_date(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1.0", isolation=False, today="2026-09-25")
    status = proof_status(tmp_path, "codex", "isolation", "codex 1.0")
    assert status.status == "failed"
    assert status.date == "2026-09-25"


def test_a_proof_for_another_version_is_stale_and_names_it(tmp_path: Path) -> None:
    record_proof(
        tmp_path,
        "claude",
        version="2.1.282 (Claude Code)",
        isolation=True,
        today="2026-09-25",
    )
    status = proof_status(tmp_path, "claude", "isolation", "2.1.283 (Claude Code)")
    assert status.status == "stale"
    assert status.recorded_version == "2.1.282 (Claude Code)"
    assert "2.1.282" in status.reason


def test_a_moved_probed_version_turns_passed_into_stale(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="1.0", isolation=True, today="2026-09-25")
    assert proof_status(tmp_path, "codex", "isolation", "1.0").status == "passed"
    assert proof_status(tmp_path, "codex", "isolation", "1.1").status == "stale"


def test_a_failed_proof_of_another_version_is_stale_not_failed(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="1.0", isolation=False, today="2026-09-25")
    assert proof_status(tmp_path, "codex", "isolation", "1.1").status == "stale"


def test_an_isolation_fingerprint_that_moved_is_stale_for_the_same_version(
    tmp_path: Path,
) -> None:
    publish(
        proof_path(tmp_path, "codex"),
        {
            "rail": "codex",
            "version": "1.0",
            "isolation": {"passed": True, "date": "2026-09-25", "fingerprint": "0" * 16},
            "confinement": None,
        },
    )
    status = proof_status(tmp_path, "codex", "isolation", "1.0")
    assert status.status == "stale"
    assert "isolation source changed" in status.reason
    assert status.recorded_version == "1.0"


def test_a_record_without_fingerprint_stays_passed(tmp_path: Path) -> None:
    publish(
        proof_path(tmp_path, "codex"),
        {
            "rail": "codex",
            "version": "1.0",
            "isolation": {"passed": True, "date": "2026-09-25"},
            "confinement": None,
        },
    )
    assert proof_status(tmp_path, "codex", "isolation", "1.0").status == "passed"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{", id="unparsable"),
        pytest.param(
            '{"rail": "claude", "version": "1.0", '
            '"isolation": {"passed": true, "date": "2026-09-25"}, "confinement": null}',
            id="another-rail",
        ),
    ],
)
def test_unparsable_json_or_another_rail_is_unreadable_for_both_kinds(
    tmp_path: Path, content: str
) -> None:
    path = proof_path(tmp_path, "codex")
    path.parent.mkdir(parents=True)
    path.write_text(content)
    assert proof_status(tmp_path, "codex", "isolation", "1.0").status == "unreadable"
    assert proof_status(tmp_path, "codex", "confinement", "1.0").status == "unreadable"


def test_a_proof_value_of_the_wrong_shape_is_unreadable(tmp_path: Path) -> None:
    publish(
        proof_path(tmp_path, "codex"),
        {"rail": "codex", "version": "1.0", "isolation": "yes", "confinement": None},
    )
    assert proof_status(tmp_path, "codex", "isolation", "1.0").status == "unreadable"
    assert proof_status(tmp_path, "codex", "confinement", "1.0").status == "missing"


# ── mode: the engine's own functions decide, status is display ─────────────

_SHAPES = ("none", "passed", "failed", "bad_fingerprint", "malformed")


def _kind_raw(shape: str) -> object | None:
    if shape == "none":
        return None
    if shape == "passed":
        return {"passed": True, "date": "2026-09-25"}
    if shape == "failed":
        return {"passed": False, "date": "2026-09-25"}
    if shape == "bad_fingerprint":
        return {"passed": True, "date": "2026-09-25", "fingerprint": "0" * 16}
    if shape == "malformed":
        return "not-a-table"
    raise ValueError(shape)


@pytest.mark.parametrize("confinement_shape", _SHAPES)
@pytest.mark.parametrize("isolation_shape", _SHAPES)
def test_mode_matches_the_engine_functions(
    tmp_path: Path, isolation_shape: str, confinement_shape: str
) -> None:
    """Every (isolation, confinement) shape combination the record can hold at a MATCHING
    version (staleness-by-version is exercised by the dedicated tests above, not here: the
    two kinds share one ``version`` field, so they cannot go stale independently of each
    other within a single record -- see the module docstring)."""
    rail, version = "codex", "codex 1.0"
    publish(
        proof_path(tmp_path, rail),
        {
            "rail": rail,
            "version": version,
            "isolation": _kind_raw(isolation_shape),
            "confinement": _kind_raw(confinement_shape),
        },
    )
    rs = rail_state(tmp_path, rail, version)
    assert (rs.mode != "refused") == isolation_ok(tmp_path, rail, version)
    assert (rs.mode == "parallel") == (
        isolation_ok(tmp_path, rail, version)
        and confinement(tmp_path, rail, version)[0] == "confined"
    )
    assert (rs.isolation.status == "passed") == isolation_ok(tmp_path, rail, version)


# ── reprove: the one command, and when there is none ────────────────────────


def test_claude_confinement_is_never_offered_a_reprove_command(tmp_path: Path) -> None:
    record_proof(
        tmp_path,
        "claude",
        version="2.1.282 (Claude Code)",
        isolation=True,
        today="2026-09-25",
    )
    rs = rail_state(tmp_path, "claude", "2.1.283 (Claude Code)")
    assert rs.isolation.status == "stale"
    assert "unprovable" in rs.confinement.reason
    assert rs.reprove is not None
    assert 'isolation and claude"' in rs.reprove
    assert "confinement" not in rs.reprove


def test_reprove_is_none_when_everything_passed(tmp_path: Path) -> None:
    record_proof(tmp_path, "opencode", version="1.18.30", isolation=True, today="2026-09-25")
    record_proof(tmp_path, "opencode", version="1.18.30", confinement=True, today="2026-09-25")
    rs = rail_state(tmp_path, "opencode", "1.18.30")
    assert rs.isolation.status == "passed"
    assert rs.confinement.status == "passed"
    assert rs.reprove is None


def test_reprove_names_one_kind_or_both() -> None:
    live = "HA_LIVE=1 pytest -m live tests/live/headless_agents/test_proofs_live.py"
    assert reprove_command("codex", ("isolation",)) == f'{live} -k "isolation and codex"'
    assert reprove_command("codex", ("confinement",)) == f'{live} -k "confinement and codex"'
    assert reprove_command("codex", ("isolation", "confinement")) == f'{live} -k "codex"'


def test_unprovable_confinement_is_declared_for_claude_only() -> None:
    assert set(UNPROVABLE_CONFINEMENT) == {"claude"}
