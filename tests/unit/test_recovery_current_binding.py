"""`ops/recovery/current.json` is the one machine-readable recovery-contract binding.

Before this file, the only declaration of "which recovery contract covers the schema
the code ships" was the `dr-current` prose block of
`docs/PLAN_INDEX_REPAIR_RUNBOOK.md` — read by an operator, checked by no CI gate, and
stale in production (it announced head `052` / contract v9 while the repository shipped
head `057` and contract v14). `red-backup` needs a machine-readable answer that CANNOT
land out of sync with a migration: this test IS that gate, run by `pytest tests/unit` in
CI on every pull request.

`validate_binding` is deliberately kept in this module rather than in `src/`: the same
convention `test_runbook_normative_values_have_one_source.py` and
`test_dr_declaration_agrees_with_its_receipts.py` already use for a repository-shape
guard that only this suite consumes. `shipped_head` is always an explicit parameter,
never read internally — the one call that must reach into `release.py` for the REAL
answer lives in the test, so a fabricated chain (simulating a migration that landed
without its contract) can be exercised without monkeypatching module internals.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

from brain_v42 import release

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "ops" / "recovery"
CURRENT_JSON = RECOVERY_DIR / "current.json"

#: The asset kinds the binding names, each pointing at a repository-relative file.
ASSET_KEYS = ("manifest", "attestation_sql", "restored_attestation_sql")
ASSET_FIELDS = frozenset({"path", "sha256"})
REQUIRED_TOP_KEYS = frozenset({"contract_id", "contract_version", "schema_head", *ASSET_KEYS})


def load_binding(path: Path) -> dict[str, object]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def validate_binding(
    binding: Mapping[str, object], *, repo_root: Path, shipped_head: str
) -> list[str]:
    """Return every way `binding` disagrees with the schema the code ships.

    An empty list is the only passing answer. Structural problems (missing or
    unknown keys) short-circuit the rest: a binding shaped wrongly cannot be read
    for its `schema_head` or its assets without guessing.
    """
    keys = set(binding)
    missing = REQUIRED_TOP_KEYS - keys
    unknown = keys - REQUIRED_TOP_KEYS
    problems: list[str] = []
    if missing:
        problems.append(f"missing key(s) in ops/recovery/current.json: {sorted(missing)}")
    if unknown:
        problems.append(f"unknown key(s) in ops/recovery/current.json: {sorted(unknown)}")
    if missing or unknown:
        return problems

    if binding["schema_head"] != shipped_head:
        problems.append(
            f"ops/recovery/current.json declares schema_head {binding['schema_head']!r}, "
            f"but the code ships head {shipped_head!r}: mint the next recovery contract "
            "and update ops/recovery/current.json."
        )

    manifest_path: Path | None = None
    for asset_key in ASSET_KEYS:
        asset = binding[asset_key]
        if not isinstance(asset, Mapping) or set(asset) != ASSET_FIELDS:
            problems.append(
                f"{asset_key}: expected exactly {sorted(ASSET_FIELDS)}, "
                f"got {sorted(asset) if isinstance(asset, Mapping) else asset!r}"
            )
            continue
        asset_path = repo_root / str(asset["path"])
        if not asset_path.is_file():
            problems.append(f"{asset_key}: path does not exist: {asset['path']}")
            continue
        measured = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        if measured != asset["sha256"]:
            problems.append(
                f"{asset_key}: sha256 mismatch for {asset['path']} "
                f"(declared {asset['sha256']}, measured {measured})"
            )
        if asset_key == "manifest":
            manifest_path = asset_path

    if manifest_path is not None and manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if binding["contract_id"] != manifest.get("contract_id"):
            problems.append(
                f"contract_id {binding['contract_id']!r} does not match the manifest's "
                f"own contract_id {manifest.get('contract_id')!r}"
            )
        if binding["contract_version"] != manifest.get("schema_version"):
            problems.append(
                f"contract_version {binding['contract_version']!r} does not match the "
                f"manifest's schema_version {manifest.get('schema_version')!r}"
            )

    return problems


def _write_revision(directory: Path, revision: str, down_revision: str | None) -> None:
    parent = "None" if down_revision is None else f'"{down_revision}"'
    (directory / f"{revision}_fabricated.py").write_text(
        f'"""fabriquée."""\n\nrevision = "{revision}"\ndown_revision = {parent}\n',
        encoding="utf-8",
    )


def test_current_json_is_sorted_two_space_indented_with_a_trailing_newline() -> None:
    text = CURRENT_JSON.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True) + "\n"


def test_the_live_binding_agrees_with_the_schema_the_code_ships() -> None:
    """The real file, checked against the REAL shipped head — the CI gate itself."""
    binding = load_binding(CURRENT_JSON)
    shipped_head = release.shipped_alembic_head_strict()

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head=shipped_head)

    assert problems == []


def test_a_stale_schema_head_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    binding["schema_head"] = "056"

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("schema_head" in problem for problem in problems)


def test_a_missing_asset_path_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    binding["manifest"] = {"path": "ops/recovery/does-not-exist.json", "sha256": "0" * 64}

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("does not exist" in problem for problem in problems)


def test_a_wrong_sha256_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    attestation = dict(binding["attestation_sql"])  # type: ignore[arg-type]
    attestation["sha256"] = "0" * 64
    binding["attestation_sql"] = attestation

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("sha256 mismatch" in problem for problem in problems)


def test_a_contract_id_diverging_from_the_manifest_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    binding["contract_id"] = "brain-v42/postgresql-recovery/v13"

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("contract_id" in problem for problem in problems)


def test_a_contract_version_diverging_from_the_manifest_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    binding["contract_version"] = 13

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("contract_version" in problem for problem in problems)


def test_an_unknown_key_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    binding["surprise"] = "unexpected"

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("unknown key" in problem for problem in problems)


def test_a_missing_key_is_refused() -> None:
    binding = load_binding(CURRENT_JSON)
    del binding["schema_head"]

    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head="057")

    assert any("missing key" in problem for problem in problems)


def test_a_migration_that_lands_without_its_contract_fails_the_gate(tmp_path: Path) -> None:
    """The scenario this whole module exists to catch.

    A new migration ships (`058`, beyond the `057` this repository's `current.json`
    declares) and nobody minted its recovery contract yet. The gate must fail, and
    its message must tell the author what to do about it — not just that something
    disagrees.
    """
    _write_revision(tmp_path, "057", None)
    _write_revision(tmp_path, "058", "057")
    new_shipped_head = release.head_of_versions_strict(tmp_path)
    assert new_shipped_head == "058"

    binding = load_binding(CURRENT_JSON)
    problems = validate_binding(binding, repo_root=REPO_ROOT, shipped_head=new_shipped_head)

    assert problems, "a migration beyond the declared contract must fail the gate"
    message = problems[0]
    assert "mint the next recovery contract" in message
    assert "ops/recovery/current.json" in message
