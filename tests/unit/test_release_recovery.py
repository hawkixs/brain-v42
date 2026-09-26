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
#: `switch_live` validates its `sha` argument as a lowercase 40-character hex
#: string (task c below) — the release *directory name* it names, not
#: `RELEASE_SHA` above (the unrelated `source_sha` field inside
#: `delivery-release.json`). Kept distinct so a literal collision between the
#: two never hides a bug in either check.
DIR_SHA = "c" * 40


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
    sha: str = DIR_SHA,
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
    assert stat.S_IMODE(recovery_dir.stat().st_mode) == 0o700
    for asset_key in ("manifest", "attestation_sql", "restored_attestation_sql"):
        asset = data[asset_key]
        copied = recovery_dir / asset["path"]
        assert copied.is_file()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o644
        assert _sha256(copied) == asset["sha256"]


def test_publish_recovery_binding_sets_exact_modes_and_fresh_inodes_under_a_permissive_umask(
    tmp_path: Path,
) -> None:
    """A permissive process umask must never leak into `recovery/`'s modes.

    `os.chmod` after every write already forces the exact mode regardless of
    umask; this test is the guard against a regression that swaps a `chmod`
    for a mode passed only to `open`/`mkdir`, which the umask would then
    widen.
    """
    release_dir = _build_release(tmp_path)
    previous_umask = os.umask(0o002)
    try:
        binding_path, _digest = rr.publish_recovery_binding(release_dir)
    finally:
        os.umask(previous_umask)

    recovery_dir = binding_path.parent
    assert stat.S_IMODE(recovery_dir.stat().st_mode) == 0o700
    entries = list(recovery_dir.iterdir())
    assert entries, "recovery/ must not be empty"
    for path in entries:
        info = path.lstat()
        assert not path.is_symlink()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o644
        assert info.st_nlink == 1


def test_publish_recovery_binding_replaces_a_preexisting_hardlinked_destination(
    tmp_path: Path,
) -> None:
    """Publishing over a destination that is a hard link must not corrupt the other link.

    `shutil.copyfile` onto an existing destination opens it in place and
    overwrites its content in-place: if that destination shared an inode with
    another file (a hard link), the other file's content would change too.
    Publishing must always land on a fresh inode instead.
    """
    release_dir = _build_release(tmp_path)
    recovery_dir = release_dir / "recovery"
    recovery_dir.mkdir(parents=True)
    other = tmp_path / "other-hardlink-target.sql"
    other.write_text("-- content shared by the hard link before publish\n", encoding="utf-8")
    preexisting = recovery_dir / "brain-v42-v1.sql"
    os.link(other, preexisting)
    assert preexisting.stat().st_nlink == 2

    binding_path, _digest = rr.publish_recovery_binding(release_dir)

    data = json.loads(binding_path.read_text(encoding="utf-8"))
    published = recovery_dir / data["attestation_sql"]["path"]
    assert published == preexisting
    assert published.stat().st_nlink == 1
    assert other.stat().st_nlink == 1
    assert (
        other.read_text(encoding="utf-8") == "-- content shared by the hard link before publish\n"
    )


def test_publish_recovery_binding_refuses_an_asset_destination_name_with_an_unsafe_character(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    source_root = release_dir / "brain-v42"
    unsafe = source_root / "ops" / "recovery" / "weird name.sql"
    unsafe.write_text("-- unsafe destination file name\n", encoding="utf-8")
    current_path = source_root / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["attestation_sql"] = {
        "path": "ops/recovery/weird name.sql",
        "sha256": _sha256(unsafe),
    }
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match="not safe to publish"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_an_asset_named_like_the_binding_itself(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    source_root = release_dir / "brain-v42"
    collision = source_root / "ops" / "recovery" / "recovery-binding.json"
    collision.write_text("-- masquerading as the binding file itself\n", encoding="utf-8")
    current_path = source_root / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["manifest"] = {
        "path": "ops/recovery/recovery-binding.json",
        "sha256": _sha256(collision),
    }
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match="not safe to publish"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_a_binding_larger_than_the_byte_limit(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    current_path = release_dir / "brain-v42" / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["contract_id"] = "x" * 20000
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match=str(rr.MAX_BINDING_BYTES)):
        rr.publish_recovery_binding(release_dir)


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


def test_publish_recovery_binding_refuses_a_dotdot_asset_path(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)
    source_root = release_dir / "brain-v42"
    outside = release_dir / "outside.sql"
    outside.write_text("-- outside the release's source tree\n")
    current_path = source_root / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["attestation_sql"] = {"path": "../outside.sql", "sha256": _sha256(outside)}
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match="not a safe relative path"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_an_absolute_asset_path(tmp_path: Path) -> None:
    release_dir = _build_release(tmp_path)
    source_root = release_dir / "brain-v42"
    outside = tmp_path / "outside-abs.sql"
    outside.write_text("-- absolute, outside the release's source tree\n")
    current_path = source_root / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["manifest"] = {"path": str(outside), "sha256": _sha256(outside)}
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match="not a safe relative path"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_a_symlink_escaping_the_source_tree(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    source_root = release_dir / "brain-v42"
    secret = tmp_path / "secret.sql"
    secret.write_text("-- secret, outside the release's source tree\n")
    link = source_root / "ops" / "recovery" / "escape-link.sql"
    link.symlink_to(secret)
    current_path = source_root / "ops" / "recovery" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["restored_attestation_sql"] = {
        "path": "ops/recovery/escape-link.sql",
        "sha256": _sha256(secret),
    }
    current_path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")

    with pytest.raises(rr.RecoveryBindingError, match="outside the release's source tree"):
        rr.publish_recovery_binding(release_dir)


def test_publish_recovery_binding_refuses_to_overwrite_an_existing_symlink_in_recovery(
    tmp_path: Path,
) -> None:
    release_dir = _build_release(tmp_path)
    recovery_dir = release_dir / "recovery"
    recovery_dir.mkdir(parents=True)
    planted_target = tmp_path / "planted-target.sql"
    planted_target.write_text("-- whatever was there before\n")
    (recovery_dir / "brain-v42-v1.sql").symlink_to(planted_target)

    with pytest.raises(rr.RecoveryBindingError, match="symlink"):
        rr.publish_recovery_binding(release_dir)

    assert planted_target.read_text(encoding="utf-8") == "-- whatever was there before\n"


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
    release_dir = _build_release(tmp_path, sha=DIR_SHA)
    rr.publish_recovery_binding(release_dir)

    live_path = rr.switch_live(releases_root, DIR_SHA)

    assert live_path == releases_root / "live"
    assert live_path.is_symlink()
    assert os.readlink(live_path) == DIR_SHA
    assert not Path(os.readlink(live_path)).is_absolute()


def test_switch_live_replaces_an_existing_link_atomically(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    sha_one = "1" * 40
    sha_two = "2" * 40
    first = _build_release(tmp_path, sha=sha_one)
    second = _build_release(tmp_path, sha=sha_two)
    rr.publish_recovery_binding(first)
    rr.publish_recovery_binding(second)
    rr.switch_live(releases_root, sha_one)

    rr.switch_live(releases_root, sha_two)

    assert os.readlink(releases_root / "live") == sha_two
    assert list(releases_root.glob(".live.tmp-*")) == []


def test_switch_live_refuses_a_sha_without_a_release_directory(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    with pytest.raises(rr.RecoveryBindingError, match="does not exist"):
        rr.switch_live(releases_root, "d" * 40)


def test_switch_live_refuses_a_sha_without_a_recovery_binding(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    _build_release(tmp_path, sha=DIR_SHA)  # never published

    with pytest.raises(rr.RecoveryBindingError, match="recovery binding"):
        rr.switch_live(releases_root, DIR_SHA)


def test_switch_live_refuses_a_sha_that_is_too_short_before_touching_the_filesystem(
    tmp_path: Path,
) -> None:
    releases_root = tmp_path / "releases"
    # No `releases_root` on disk at all: a format failure must be raised
    # before any filesystem access, not surface as "does not exist".
    with pytest.raises(rr.RecoveryBindingError, match="not a valid release sha"):
        rr.switch_live(releases_root, "cafef00d")


def test_switch_live_refuses_an_uppercase_sha(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"

    with pytest.raises(rr.RecoveryBindingError, match="not a valid release sha"):
        rr.switch_live(releases_root, "D" * 40)


def test_switch_live_refuses_a_sha_with_a_non_hex_character(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"

    with pytest.raises(rr.RecoveryBindingError, match="not a valid release sha"):
        rr.switch_live(releases_root, "g" + "0" * 39)


def test_switch_live_refuses_when_the_release_directory_is_a_symlink(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)
    real_target = tmp_path / "elsewhere"
    real_target.mkdir()
    (releases_root / DIR_SHA).symlink_to(real_target)

    with pytest.raises(rr.RecoveryBindingError, match="not a symlink"):
        rr.switch_live(releases_root, DIR_SHA)


def test_switch_live_refuses_when_the_recovery_subdirectory_is_a_symlink(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha=DIR_SHA)
    rr.publish_recovery_binding(release_dir)
    real_recovery = release_dir / "recovery"
    decoy = release_dir / "recovery-elsewhere"
    real_recovery.rename(decoy)
    real_recovery.symlink_to(decoy)

    with pytest.raises(rr.RecoveryBindingError, match="not a symlink"):
        rr.switch_live(releases_root, DIR_SHA)


def test_read_live_returns_none_without_a_symlink(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    assert rr.read_live(releases_root) is None


def test_read_live_returns_the_sha(tmp_path: Path) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha=DIR_SHA)
    rr.publish_recovery_binding(release_dir)
    rr.switch_live(releases_root, DIR_SHA)

    assert rr.read_live(releases_root) == DIR_SHA


def test_cli_publish_then_live_then_show_live(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases_root = tmp_path / "releases"
    release_dir = _build_release(tmp_path, sha=DIR_SHA)

    assert rr.cli(["publish", str(release_dir)]) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["sha256"] == _sha256(release_dir / "recovery" / "recovery-binding.json")
    manifest = json.loads((release_dir / "delivery-release.json").read_text(encoding="utf-8"))
    assert manifest["recovery_binding"]["sha256"] == published["sha256"]

    assert rr.cli(["live", str(releases_root), DIR_SHA]) == 0
    capsys.readouterr()

    assert rr.cli(["show-live", str(releases_root)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["sha"] == DIR_SHA


def test_cli_reports_failure_on_stderr_with_a_nonzero_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    releases_root = tmp_path / "releases"
    releases_root.mkdir(parents=True)

    exit_code = rr.cli(["live", str(releases_root), "d" * 40])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "does not exist" in captured.err


def test_publish_recovery_binding_never_writes_through_a_symlink_planted_at_a_temporary_name(
    tmp_path: Path,
) -> None:
    """Review of PR #227: a predictable temporary name let a planted symlink
    redirect the copy outside recovery/. Every name the copy could use is
    pre-planted here; none of their targets may change."""
    release_dir = _build_release(tmp_path)
    recovery_dir = release_dir / "recovery"
    recovery_dir.mkdir(parents=True)
    planted_target = tmp_path / "outside.txt"
    planted_target.write_text("untouched\n")
    for name in ("brain-v42-v1.json", "brain-v42-v1.sql", "brain-v42-v1-pgrestore.sql"):
        (recovery_dir / f".{name}.tmp-{os.getpid()}").symlink_to(planted_target)

    rr.publish_recovery_binding(release_dir)

    assert planted_target.read_text(encoding="utf-8") == "untouched\n"
