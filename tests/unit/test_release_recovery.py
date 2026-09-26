"""`brain_v42.release_recovery` — the recovery-binding contract red-backup reads.

`ops/recovery/current.json` already answers "which recovery contract covers the
schema THIS SOURCE TREE ships" (`tests/unit/test_recovery_current_binding.py`). This
module answers the operational half red-backup needs at a running release: which
recovery contract covers the schema of the LIVE release, verifiable without brain
running and without reading brain's working checkout — only the release tree on
disk. `publish_recovery_binding` copies the three named files out of the release's
own source tree into `recovery/` (so a later `git checkout` of the working tree
cannot move them out from under an already-cut-over release), verifies every
sha256 both before and after the copy, and writes the binding atomically.
`record_binding_in_manifest` publishes that binding's own sha256 into
`delivery-release.json`, so a tampered `recovery/recovery-binding.json` disagrees
with the one non-secret manifest a preflight already trusts. `switch_live` /
`read_live` are the atomic pointer red-backup and a rollback both read.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from brain_v42 import release_recovery as rr

SCHEMA_HEAD = "058"
RELEASE_SHA = "b" * 40


def _write_revision(directory: Path, revision: str, down_revision: str | None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    parent = "None" if down_revision is None else f'"{down_revision}"'
    (directory / f"{revision}_fabricated.py").write_text(
        f'"""fabricated."""\n\nrevision = "{revision}"\ndown_revision = {parent}\n',
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_release(
    tmp_path: Path,
    *,
    sha: str = "cafef00d",
    schema_head: str = SCHEMA_HEAD,
    declared_schema_head: str | None = None,
    release_sha: str = RELEASE_SHA,
) -> Path:
    """A release directory shaped like `<releases_root>/<sha>/`, minimal but real."""
    release_dir = tmp_path / "releases" / sha
    source_root = release_dir / "brain-v42"
    _write_revision(source_root / "alembic" / "versions", schema_head, None)

    recovery_source = source_root / "ops" / "recovery"
    recovery_source.mkdir(parents=True)
    manifest_sql = recovery_source / "brain-v42-v1.json"
    attestation_sql = recovery_source / "brain-v42-v1.sql"
    restored_sql = recovery_source / "brain-v42-v1-pgrestore.sql"
    manifest_sql.write_text('{"contract_id": "brain-v42/postgresql-recovery/v1"}\n')
    attestation_sql.write_text("-- attestation\n")
    restored_sql.write_text("-- restored\n")
    current = {
        "attestation_sql": {
            "path": "ops/recovery/brain-v42-v1.sql",
            "sha256": _sha256(attestation_sql),
        },
        "contract_id": "brain-v42/postgresql-recovery/v1",
        "contract_version": 1,
        "manifest": {"path": "ops/recovery/brain-v42-v1.json", "sha256": _sha256(manifest_sql)},
        "restored_attestation_sql": {
            "path": "ops/recovery/brain-v42-v1-pgrestore.sql",
            "sha256": _sha256(restored_sql),
        },
        "schema_head": declared_schema_head if declared_schema_head is not None else schema_head,
    }
    (recovery_source / "current.json").write_text(
        json.dumps(current, indent=2, sort_keys=True) + "\n"
    )

    manifest_path = release_dir / "delivery-release.json"
    manifest_path.write_text(
        json.dumps(
            {"schema_version": 1, "source_sha": release_sha, "version": "1.0.0"}, sort_keys=True
        )
    )
    return release_dir


def test_publish_recovery_binding_writes_sorted_two_space_indented_binding_with_trailing_newline(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)

    binding_path, digest = rr.publish_recovery_binding(release_dir)

    text = binding_path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, indent=2, sort_keys=True) + "\n"
    assert binding_path == release_dir / "recovery" / "recovery-binding.json"
    assert digest == _sha256(binding_path)


def test_publish_recovery_binding_declares_the_release_and_shipped_schema(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)

    binding_path, _digest = rr.publish_recovery_binding(release_dir)

    data = json.loads(binding_path.read_text(encoding="utf-8"))
    assert data["release_sha"] == RELEASE_SHA
    assert data["schema_head"] == SCHEMA_HEAD
    assert data["contract_id"] == "brain-v42/postgresql-recovery/v1"
    assert data["contract_version"] == 1


def test_publish_recovery_binding_copies_the_three_files_with_mode_0644(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)

    binding_path, _digest = rr.publish_recovery_binding(release_dir)

    data = json.loads(binding_path.read_text(encoding="utf-8"))
    recovery_dir = binding_path.parent
    assert stat.S_IMODE(recovery_dir.stat().st_mode) == 0o755
    for asset_key in ("manifest", "attestation_sql", "restored_attestation_sql"):
        asset = data[asset_key]
        copied = recovery_dir / asset["path"]
        assert copied.is_file()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o644
        assert _sha256(copied) == asset["sha256"]


def test_publish_recovery_binding_refuses_a_stale_schema_head(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path, schema_head=SCHEMA_HEAD, declared_schema_head="057")

    with pytest.raises(rr.RecoveryBindingError, match="schema_head"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_a_sha256_mismatch_before_copy(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)
    tampered = release_dir / "brain-v42" / "ops" / "recovery" / "brain-v42-v1.sql"
    tampered.write_text("-- tampered after current.json was minted\n")

    with pytest.raises(rr.RecoveryBindingError, match="sha256 mismatch"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_a_release_with_no_current_json(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)
    (release_dir / "brain-v42" / "ops" / "recovery" / "current.json").unlink()

    with pytest.raises(rr.RecoveryBindingError, match="current.json"):
        rr.publish_recovery_binding(release_dir)


def test_record_binding_in_manifest_adds_key_preserving_other_keys_and_format(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    binding_path, digest = rr.publish_recovery_binding(release_dir)

    rr.record_binding_in_manifest(release_dir)

    manifest_path = release_dir / "delivery-release.json"
    text = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert manifest["recovery_binding"] == {
        "path": "recovery/recovery-binding.json",
        "sha256": digest,
    }
    assert manifest["source_sha"] == RELEASE_SHA
    assert manifest["schema_version"] == 1
    assert manifest["version"] == "1.0.0"
    # Same compact, sorted-key convention the release script already writes.
    assert text == json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    assert digest == _sha256(binding_path)


def test_record_binding_in_manifest_refuses_without_a_published_binding(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)

    with pytest.raises(rr.RecoveryBindingError, match="recovery"):
        rr.record_binding_in_manifest(release_dir)


def test_switch_live_creates_a_relative_symlink(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha="cafef00d")
    rr.publish_recovery_binding(release_dir)

    live_path = rr.switch_live(releases_root, "cafef00d")

    assert live_path == releases_root / "live"
    assert live_path.is_symlink()
    assert os.readlink(live_path) == "cafef00d"
    assert not Path(os.readlink(live_path)).is_absolute()


def test_switch_live_replaces_an_existing_link_atomically(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    first = _build_release(tmp_path, sha="aaaaaaaa")
    second = _build_release(tmp_path, sha="bbbbbbbb")
    rr.publish_recovery_binding(first)
    rr.publish_recovery_binding(second)
    rr.switch_live(releases_root, "aaaaaaaa")

    rr.switch_live(releases_root, "bbbbbbbb")

    assert os.readlink(releases_root / "live") == "bbbbbbbb"
    assert list(releases_root.glob(".live.tmp-*")) == []


def test_switch_live_refuses_a_sha_without_a_release_directory(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    with pytest.raises(rr.RecoveryBindingError, match="does not exist"):
        rr.switch_live(releases_root, "ghost")


def test_switch_live_refuses_a_sha_without_a_recovery_binding(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    _build_release(tmp_path, sha="cafef00d")  # never published

    with pytest.raises(rr.RecoveryBindingError, match="recovery binding"):
        rr.switch_live(releases_root, "cafef00d")


def test_read_live_returns_none_without_a_symlink(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    assert rr.read_live(releases_root) is None


def test_read_live_returns_the_sha(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha="cafef00d")
    rr.publish_recovery_binding(release_dir)
    rr.switch_live(releases_root, "cafef00d")

    assert rr.read_live(releases_root) == "cafef00d"


def test_cli_publish_then_live_then_show_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha="cafef00d")

    assert rr.cli(["publish", str(release_dir)]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["sha256"] == _sha256(release_dir / "recovery" / "recovery-binding.json")
    manifest = json.loads((release_dir / "delivery-release.json").read_text(encoding="utf-8"))
    assert manifest["recovery_binding"]["sha256"] == published["sha256"]

    assert rr.cli(["live", str(releases_root), "cafef00d"]) == 0
    capsys.readouterr()

    assert rr.cli(["show-live", str(releases_root)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["sha"] == "cafef00d"


def test_cli_reports_failure_on_stderr_with_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    exit_code = rr.cli(["live", str(releases_root), "ghost"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err
