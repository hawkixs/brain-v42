"""The recovery-binding contract of one immutable release, on disk.

`ops/recovery/current.json` (`tests/unit/test_recovery_current_binding.py`) answers
"which recovery contract covers the schema THIS SOURCE TREE ships" — a question a
git checkout can answer. red-backup asks a harder question at every release: which
recovery contract covers the schema of the LIVE release, measured WITHOUT brain
running and WITHOUT reading brain's working checkout (a checkout moves under a
release the moment `main` advances). This module is the operational bridge:

- `publish_recovery_binding` reads the release's own `current.json` from its own
  source tree (`<release>/brain-v42/ops/recovery/current.json`, never the working
  checkout), verifies its `schema_head` against the head the release's own shipped
  Alembic revisions carry, verifies the sha256 of the three files it names, COPIES
  them into `<release>/recovery/` (so a later `git checkout` on the working tree
  cannot move them out from under an already-cut-over release), verifies the
  sha256 again after the copy, and writes `recovery/recovery-binding.json`
  atomically.
- `record_binding_in_manifest` publishes that binding's own sha256 into the
  release's `delivery-release.json`, so a preflight that already trusts one
  manifest gets one more field to trust, instead of a second file to fetch.
- `switch_live` / `read_live` are the one pointer both a normal cutover and a
  rollback flip: `<releases_root>/live -> <sha>`, replaced ATOMICALLY (a temporary
  symlink created next to it, then `os.replace`), so no reader ever observes a
  missing or half-written link. `switch_live` refuses a sha with no release
  directory or no recovery binding — a release that never published its binding is
  not survivable and must never become live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from brain_v42.release import head_of_versions_strict

#: Where a release's own source tree is checked out, relative to the release dir.
SOURCE_TREE_DIRNAME = "brain-v42"
CURRENT_JSON_RELATIVE = Path("ops") / "recovery" / "current.json"
VERSIONS_RELATIVE = Path("alembic") / "versions"
RECOVERY_DIRNAME = "recovery"
BINDING_FILENAME = "recovery-binding.json"
MANIFEST_FILENAME = "delivery-release.json"
LIVE_LINK_NAME = "live"
#: The three files `current.json` (and the binding it produces) name.
ASSET_KEYS = ("manifest", "attestation_sql", "restored_attestation_sql")


class RecoveryBindingError(RuntimeError):
    """A release's recovery binding cannot be published, recorded, or trusted."""


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RecoveryBindingError(f"cannot read {path}: {exc}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryBindingError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RecoveryBindingError(f"{path} does not carry a JSON object")
    return document


def _confined_source_path(
    source_root: Path,
    resolved_source_root: Path,
    raw_path: str,
    asset_key: str,
    current_path: Path,
) -> Path:
    """Resolve `raw_path` against `source_root`, refusing any escape.

    `current.json` is read from the release's own source tree, but it is data,
    not code: a compromised build (or a tampered `current.json`) could name an
    absolute path, a path with `.`/`..` components, or a relative path that
    looks safe lexically but is a symlink planted inside the source tree
    pointing outside it. Any of the three would let `publish_recovery_binding`
    read — and then publish into `recovery/` — a file that never belonged to
    this release's source tree.
    """
    candidate = Path(raw_path)
    if (
        not raw_path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RecoveryBindingError(
            f"{current_path}'s {asset_key!r} path {raw_path!r} is not a safe relative path"
        )
    lexical = source_root / candidate
    if not lexical.is_file():
        raise RecoveryBindingError(f"{asset_key}: source file is missing: {lexical}")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(resolved_source_root)
    except ValueError as exc:
        raise RecoveryBindingError(
            f"{asset_key}: source path {raw_path!r} resolves outside the release's "
            f"source tree ({resolved})"
        ) from exc
    return lexical


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """Write `text` to `path` so no reader ever observes a partial file."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def publish_recovery_binding(release_dir: Path) -> tuple[Path, str]:
    """Copy the release's named recovery contract into `recovery/`, bound.

    Returns `(binding_path, sha256)`. Raises `RecoveryBindingError` — never a bare
    assertion — on a stale `schema_head` or any sha256 mismatch, before or after
    the copy: a release that cannot prove its own recovery contract must not
    produce one that looks published.
    """
    release_dir = Path(release_dir)
    source_root = release_dir / SOURCE_TREE_DIRNAME
    current_path = source_root / CURRENT_JSON_RELATIVE
    if not current_path.is_file():
        raise RecoveryBindingError(
            f"release ships no recovery contract binding: {current_path} is missing"
        )
    current = _load_json_object(current_path)

    try:
        shipped_head = head_of_versions_strict(source_root / VERSIONS_RELATIVE)
    except ValueError as exc:
        raise RecoveryBindingError(
            f"cannot determine the release's shipped schema head: {exc}"
        ) from exc
    if current.get("schema_head") != shipped_head:
        raise RecoveryBindingError(
            f"{current_path} declares schema_head {current.get('schema_head')!r}, but the "
            f"release ships head {shipped_head!r}: mint the next recovery contract first"
        )

    manifest = _load_json_object(release_dir / MANIFEST_FILENAME)
    release_sha = manifest.get("source_sha")
    if not isinstance(release_sha, str) or not release_sha:
        raise RecoveryBindingError(f"{release_dir / MANIFEST_FILENAME} carries no source_sha")

    recovery_dir = release_dir / RECOVERY_DIRNAME
    recovery_dir.mkdir(mode=0o755, exist_ok=True)
    os.chmod(recovery_dir, 0o755)

    binding: dict[str, object] = {
        "contract_id": current.get("contract_id"),
        "contract_version": current.get("contract_version"),
        "schema_head": shipped_head,
        "release_sha": release_sha,
    }
    resolved_source_root = source_root.resolve()
    for asset_key in ASSET_KEYS:
        asset = current.get(asset_key)
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("path"), str)
            or not isinstance(asset.get("sha256"), str)
        ):
            raise RecoveryBindingError(f"{current_path}'s {asset_key!r} entry is malformed")
        source_path = _confined_source_path(
            source_root, resolved_source_root, str(asset["path"]), asset_key, current_path
        )
        declared = str(asset["sha256"])
        measured_before = _sha256_of(source_path)
        if measured_before != declared:
            raise RecoveryBindingError(
                f"{asset_key}: sha256 mismatch before copy for {source_path} "
                f"(current.json declares {declared}, measured {measured_before})"
            )
        destination = recovery_dir / source_path.name
        if destination.is_symlink():
            raise RecoveryBindingError(
                f"{asset_key}: destination {destination} already exists as a symlink; "
                "refusing to write through it and escape recovery/"
            )
        shutil.copyfile(source_path, destination)
        os.chmod(destination, 0o644)
        measured_after = _sha256_of(destination)
        if measured_after != declared:
            raise RecoveryBindingError(
                f"{asset_key}: sha256 mismatch after copy for {destination} "
                f"(expected {declared}, measured {measured_after})"
            )
        binding[asset_key] = {"path": destination.name, "sha256": measured_after}

    binding_path = recovery_dir / BINDING_FILENAME
    text = json.dumps(binding, indent=2, sort_keys=True) + "\n"
    _atomic_write(binding_path, text, mode=0o644)
    return binding_path, _sha256_of(binding_path)


def record_binding_in_manifest(release_dir: Path) -> None:
    """Record the published binding's sha256 into `delivery-release.json`.

    Rewrites the manifest atomically, preserving every other key and the
    compact, sorted-key convention the release script already writes it with
    (`json.dump(..., sort_keys=True, separators=(",", ":"))`).
    """
    release_dir = Path(release_dir)
    binding_path = release_dir / RECOVERY_DIRNAME / BINDING_FILENAME
    if not binding_path.is_file():
        raise RecoveryBindingError(
            f"no recovery binding to record: {binding_path} is missing "
            "(run publish_recovery_binding first)"
        )
    digest = _sha256_of(binding_path)

    manifest_path = release_dir / MANIFEST_FILENAME
    try:
        mode = stat.S_IMODE(manifest_path.stat().st_mode)
    except OSError as exc:
        raise RecoveryBindingError(f"cannot stat {manifest_path}: {exc}") from exc
    manifest = _load_json_object(manifest_path)
    manifest["recovery_binding"] = {
        "path": f"{RECOVERY_DIRNAME}/{BINDING_FILENAME}",
        "sha256": digest,
    }
    text = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    _atomic_write(manifest_path, text, mode=mode)


def switch_live(releases_root: Path, sha: str) -> Path:
    """Point `<releases_root>/live` at `sha`, atomically.

    Used both at a normal cutover and at a rollback: the swap is identical
    either way, only the target sha differs. Refuses a sha whose release
    directory does not exist, or that never published a recovery binding — a
    release with no verifiable recovery contract must never become live.
    """
    releases_root = Path(releases_root)
    release_dir = releases_root / sha
    if not release_dir.is_dir():
        raise RecoveryBindingError(f"release directory does not exist: {release_dir}")
    binding_path = release_dir / RECOVERY_DIRNAME / BINDING_FILENAME
    if not binding_path.is_file():
        raise RecoveryBindingError(f"release {sha!r} carries no recovery binding: {binding_path}")

    live_path = releases_root / LIVE_LINK_NAME
    tmp_link = releases_root / f".{LIVE_LINK_NAME}.tmp-{os.getpid()}"
    tmp_link.unlink(missing_ok=True)
    try:
        tmp_link.symlink_to(sha)
        os.replace(tmp_link, live_path)
    except Exception:
        tmp_link.unlink(missing_ok=True)
        raise
    return live_path


def read_live(releases_root: Path) -> str | None:
    """The sha `<releases_root>/live` points at, or None if there is no link."""
    live_path = Path(releases_root) / LIVE_LINK_NAME
    if not live_path.is_symlink():
        return None
    return os.readlink(live_path)


def cli(argv: list[str] | None = None) -> int:
    """`publish <release_dir>` / `live <releases_root> <sha>` / `show-live <releases_root>`."""
    parser = argparse.ArgumentParser(prog="python -m brain_v42.release_recovery")
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("release_dir", type=Path)

    live_parser = subparsers.add_parser("live")
    live_parser.add_argument("releases_root", type=Path)
    live_parser.add_argument("sha")

    show_live_parser = subparsers.add_parser("show-live")
    show_live_parser.add_argument("releases_root", type=Path)

    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "publish":
            binding_path, digest = publish_recovery_binding(arguments.release_dir)
            record_binding_in_manifest(arguments.release_dir)
            print(json.dumps({"binding": str(binding_path), "sha256": digest}, sort_keys=True))
        elif arguments.command == "live":
            live_path = switch_live(arguments.releases_root, arguments.sha)
            print(json.dumps({"live": str(live_path), "sha": arguments.sha}, sort_keys=True))
        else:
            print(json.dumps({"sha": read_live(arguments.releases_root)}, sort_keys=True))
    except RecoveryBindingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
