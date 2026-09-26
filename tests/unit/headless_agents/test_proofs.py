"""Per-rail proof records (spec 0.5.0 §3.8.0, plan decisions P3 and P4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headless_agents import proofs as proofs_module
from headless_agents.proofs import (
    CLI_RAILS,
    confinement,
    isolation_fingerprint,
    isolation_label,
    isolation_ok,
    plant_confinement_targets,
    proof_path,
    read_proof,
    record_proof,
)


def test_the_cli_rails() -> None:
    assert CLI_RAILS == ("claude", "codex", "agy", "opencode")


def test_a_passing_isolation_proof_for_the_installed_version_counts(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 0.156.0", isolation=True, today="2026-09-25")
    assert isolation_ok(tmp_path, "codex", "codex 0.156.0")
    assert isolation_label(tmp_path, "codex", "codex 0.156.0") == "isolated (2026-09-25)"


def test_no_record_is_not_proven(tmp_path: Path) -> None:
    assert not isolation_ok(tmp_path, "codex", "codex 0.156.0")
    assert isolation_label(tmp_path, "codex", "codex 0.156.0") == "not proven"


def test_a_record_for_another_version_is_not_proven(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 0.155.0", isolation=True, today="2026-09-20")
    assert not isolation_ok(tmp_path, "codex", "codex 0.156.0")
    assert "codex 0.155.0" in isolation_label(tmp_path, "codex", "codex 0.156.0")


def test_a_failed_proof_refuses(tmp_path: Path) -> None:
    record_proof(tmp_path, "claude", version="2.1.282", isolation=False, today="2026-09-25")
    assert not isolation_ok(tmp_path, "claude", "2.1.282")
    assert isolation_label(tmp_path, "claude", "2.1.282") == "failed (2026-09-25)"


def test_an_http_provider_needs_no_proof(tmp_path: Path) -> None:
    """Plan decision P4: no local executor, no operator configuration to load."""
    assert isolation_ok(tmp_path, "mistral", None)
    assert isolation_ok(tmp_path, "openai-compat", None)


def test_a_corrupt_record_is_not_proven(tmp_path: Path) -> None:
    path = proof_path(tmp_path, "codex")
    path.parent.mkdir(parents=True)
    path.write_text("{")
    assert not isolation_ok(tmp_path, "codex", "codex 0.156.0")
    assert read_proof(tmp_path, "codex") is None


def test_isolation_and_confinement_of_one_version_are_kept_together(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="v1", isolation=True, today="2026-09-25")
    record_proof(tmp_path, "codex", version="v1", confinement=True, today="2026-09-26")
    proof = read_proof(tmp_path, "codex")
    assert proof is not None
    assert proof.isolation is not None and proof.isolation.passed
    assert proof.confinement is not None and proof.confinement.date == "2026-09-26"


def test_a_new_version_replaces_the_whole_record(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="v1", isolation=True, confinement=True, today="d1")
    record_proof(tmp_path, "codex", version="v2", isolation=True, today="d2")
    proof = read_proof(tmp_path, "codex")
    assert proof is not None and proof.version == "v2"
    assert proof.confinement is None, "a proof of v1 says nothing about v2"


def test_the_record_is_a_plain_document(tmp_path: Path) -> None:
    record_proof(tmp_path, "agy", version="agy 1.2", isolation=True, today="2026-09-25")
    document = json.loads(proof_path(tmp_path, "agy").read_text())
    assert document == {
        "rail": "agy",
        "version": "agy 1.2",
        "isolation": {
            "passed": True,
            "date": "2026-09-25",
            "fingerprint": isolation_fingerprint("agy"),
        },
        "confinement": None,
    }


def test_a_passing_confinement_proof_for_this_version_is_confined(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1", confinement=True, today="2026-09-25")
    assert confinement(tmp_path, "codex", "codex 1") == ("confined", "2026-09-25")


def test_a_failed_confinement_proof_is_unconfined_with_its_date(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1", confinement=False, today="2026-09-25")
    assert confinement(tmp_path, "codex", "codex 1") == ("unconfined", "2026-09-25")


def test_a_confinement_proof_of_another_version_is_unconfined(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1", confinement=True, today="2026-09-25")
    assert confinement(tmp_path, "codex", "codex 2") == ("unconfined", None)


def test_no_confinement_proof_is_unconfined(tmp_path: Path) -> None:
    record_proof(tmp_path, "codex", version="codex 1", isolation=True, today="2026-09-25")
    assert confinement(tmp_path, "codex", "codex 1") == ("unconfined", None)
    assert confinement(tmp_path, "claude", "claude 1") == ("unconfined", None)


def test_the_confinement_targets_are_planted_outside_the_workspace(tmp_path: Path) -> None:
    targets = plant_confinement_targets(tmp_path / "claude", "claude")
    workspace = targets["workspace"]
    assert (workspace / ".git").is_file(), "the workspace is a linked worktree"
    for name in ("common_config", "ref", "operator_gitconfig"):
        assert targets[name].is_file(), name
        assert not targets[name].is_relative_to(workspace), name
    assert not any(name.startswith("tmp_repo") for name in targets)


def test_a_control_target_inside_the_workspace_proves_the_agent_tried(tmp_path: Path) -> None:
    """A refusal by the model, or a filtered prompt, writes nothing anywhere:
    without a write that succeeded inside the workspace, the run proves nothing."""
    targets = plant_confinement_targets(tmp_path / "opencode", "opencode")
    control = targets["control"]
    assert control.is_file() and control.is_relative_to(targets["workspace"])


# ── isolation proof binding (ticket ha-051-agy): a proof recorded under one
# isolation-relevant SOURCE must never unlock a rail running under another.
# `isolation_fingerprint` is real (reads the installed package's own files),
# so record and check land on the same value throughout ordinary use; these
# tests force a mismatch the way an upgrade or a regression would.


def test_isolation_fingerprint_is_stable_and_per_rail() -> None:
    assert isolation_fingerprint("agy") == isolation_fingerprint("agy")
    assert isolation_fingerprint("agy") != isolation_fingerprint("claude")
    for rail in CLI_RAILS:
        fingerprint = isolation_fingerprint(rail)
        assert isinstance(fingerprint, str) and len(fingerprint) == 16


def test_isolation_fingerprint_is_none_for_a_non_cli_rail() -> None:
    assert isolation_fingerprint("mistral") is None
    assert isolation_fingerprint("openai-compat") is None


def test_a_proof_recorded_under_a_different_isolation_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario ticket ha-051-agy flags: a proof recorded while the rail ran
    one way (e.g. headless-agents 0.5.1, fixed) must not silently cover a later
    run of the SAME CLI version under a DIFFERENT isolation mechanism (e.g. a
    downgrade back to 0.5.0's leak, or a future regression) -- the CLI's own
    `--version` string never changes to say so."""
    monkeypatch.setattr(proofs_module, "isolation_fingerprint", lambda rail: "fingerprint-a")
    record_proof(tmp_path, "agy", version="agy 1.2.11", isolation=True, today="2026-09-25")
    assert isolation_ok(tmp_path, "agy", "agy 1.2.11")

    monkeypatch.setattr(proofs_module, "isolation_fingerprint", lambda rail: "fingerprint-b")
    assert not isolation_ok(tmp_path, "agy", "agy 1.2.11")
    assert "agy 1.2.11" not in isolation_label(tmp_path, "agy", "agy 1.2.11")
    assert "stale" in isolation_label(tmp_path, "agy", "agy 1.2.11")


def test_a_record_predating_the_fingerprint_is_grandfathered(tmp_path: Path) -> None:
    """A proof written before this check shipped carries no `fingerprint` key at
    all -- not a mismatch, since nothing was ever measured to compare against.
    Retroactively invalidating every pre-existing proof on an upgrade would
    also force claude/codex/opencode to be re-recorded for a change that never
    touched their rails; only the rails that actually changed must lose their
    proof (covered by the mismatch test above)."""
    path = proof_path(tmp_path, "codex")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "rail": "codex",
                "version": "codex 1.0",
                "isolation": {"passed": True, "date": "2026-01-01"},
                "confinement": None,
            }
        )
    )
    assert isolation_ok(tmp_path, "codex", "codex 1.0")
    assert isolation_label(tmp_path, "codex", "codex 1.0") == "isolated (2026-01-01)"


def test_recording_confinement_alone_carries_no_fingerprint(tmp_path: Path) -> None:
    """The fingerprint binds the ISOLATION proof only (ticket ha-051-agy scopes
    it there): a confinement-only record stays exactly the two-key document it
    always was, with no `fingerprint` key at all."""
    record_proof(tmp_path, "codex", version="codex 1", confinement=True, today="2026-09-25")
    document = json.loads(proof_path(tmp_path, "codex").read_text())
    assert document["confinement"] == {"passed": True, "date": "2026-09-25"}


def test_codex_gets_a_repository_under_each_root_it_treats_as_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    targets = plant_confinement_targets(tmp_path / "codex", "codex")
    try:
        assert targets["tmp_repo_tmp"].is_file()
        assert targets["tmp_repo_tmp"].is_relative_to(Path("/tmp"))
        assert targets["tmp_repo_tmpdir"].is_relative_to(tmpdir)
    finally:
        import shutil

        shutil.rmtree(targets["tmp_repo_tmp"].parent.parent, ignore_errors=True)
