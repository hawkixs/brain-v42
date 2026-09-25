"""Per-rail proof records (spec 0.5.0 §3.8.0, plan decisions P3 and P4)."""

from __future__ import annotations

import json
from pathlib import Path

from headless_agents.proofs import (
    CLI_RAILS,
    isolation_label,
    isolation_ok,
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
        "isolation": {"passed": True, "date": "2026-09-25"},
        "confinement": None,
    }
