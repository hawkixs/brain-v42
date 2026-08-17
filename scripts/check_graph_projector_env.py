#!/usr/bin/env python3
"""Fail closed on an unsafe service-private graph projector environment file."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

_REQUIRED_PRIVATE_KEYS = frozenset(
    {
        "GRAPH_PROJECTOR_ENABLED",
        "GRAPH_PROJECTOR_NEO4J_URL",
        "GRAPH_PROJECTOR_NEO4J_USER",
        "GRAPH_PROJECTOR_NEO4J_PASSWORD",
    }
)
_LEGACY_NEO4J_KEYS = frozenset({"NEO4J_URL", "NEO4J_USER", "NEO4J_PASSWORD"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MAX_ENV_BYTES = 64 * 1024
_CANONICAL_SECURITY_KEYS = {
    key.casefold(): key
    for key in _REQUIRED_PRIVATE_KEYS | _LEGACY_NEO4J_KEYS | {"GRAPH_LEDGER_WRITE_ENABLED"}
}


class ProjectorEnvironmentError(RuntimeError):
    """Raised without including any environment value or credential."""


def _assignments(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > _MAX_ENV_BYTES:
            raise ProjectorEnvironmentError("environment file exceeds the size limit")
        lines = path.read_text(encoding="utf-8").splitlines()
    except ProjectorEnvironmentError:
        raise
    except OSError as exc:
        raise ProjectorEnvironmentError("environment file is unreadable") from exc

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        exported = line.split(maxsplit=1)
        uses_export = len(exported) == 2 and exported[0] == "export"
        if uses_export:
            line = exported[1]
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ProjectorEnvironmentError("environment file contains an invalid assignment")
        canonical_key = _CANONICAL_SECURITY_KEYS.get(key.casefold(), key)
        if uses_export and canonical_key in _CANONICAL_SECURITY_KEYS.values():
            raise ProjectorEnvironmentError(f"{canonical_key} must use a plain systemd assignment")
        if canonical_key in values and canonical_key in _CANONICAL_SECURITY_KEYS.values():
            raise ProjectorEnvironmentError(
                f"environment file assigns {canonical_key} more than once"
            )
        values[canonical_key] = value.strip().strip("'\"")
    return values


def _effective_security_assignments(environment: Mapping[str, str]) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for key, value in environment.items():
        canonical_key = _CANONICAL_SECURITY_KEYS.get(key.casefold())
        if canonical_key is None:
            continue
        if canonical_key in assignments:
            raise ProjectorEnvironmentError(f"effective {canonical_key} is assigned more than once")
        assignments[canonical_key] = value.strip().strip("'\"")
    return assignments


def _effective_bool(assignments: Mapping[str, str], key: str) -> bool | None:
    raw_value = assignments.get(key)
    if raw_value is None:
        return None
    normalized = raw_value.casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ProjectorEnvironmentError(f"effective {key} is not a valid boolean")


def validate_projector_environment(
    shared_path: Path,
    private_path: Path,
    *,
    expected_uid: int,
    effective_environment: Mapping[str, str] | None = None,
    require_effective_private: bool = False,
) -> None:
    """Require an owned regular 0600 credential file when the ledger is active."""
    shared = _assignments(shared_path)
    if _REQUIRED_PRIVATE_KEYS.intersection(shared):
        raise ProjectorEnvironmentError("shared environment contains private graph projector keys")
    if _LEGACY_NEO4J_KEYS.intersection(shared):
        raise ProjectorEnvironmentError("shared environment contains legacy Neo4j keys")
    shared_ledger_enabled = shared.get("GRAPH_LEDGER_WRITE_ENABLED", "").casefold() in _TRUE_VALUES
    effective_assignments = _effective_security_assignments(effective_environment or {})
    if _LEGACY_NEO4J_KEYS.intersection(effective_assignments):
        raise ProjectorEnvironmentError("effective environment contains legacy Neo4j keys")
    effective_ledger_enabled = _effective_bool(
        effective_assignments,
        "GRAPH_LEDGER_WRITE_ENABLED",
    )
    if (
        effective_ledger_enabled is not None
        and effective_ledger_enabled is not shared_ledger_enabled
    ):
        raise ProjectorEnvironmentError(
            "effective graph ledger flag differs from the shared environment"
        )
    ledger_enabled = (
        shared_ledger_enabled if effective_ledger_enabled is None else effective_ledger_enabled
    )

    try:
        private_stat = private_path.lstat()
    except FileNotFoundError as exc:
        if not ledger_enabled:
            if _REQUIRED_PRIVATE_KEYS.intersection(effective_assignments):
                raise ProjectorEnvironmentError(
                    "effective graph projector settings are not attested by a private file"
                ) from exc
            return
        raise ProjectorEnvironmentError(
            "private graph projector environment is required while the ledger is active"
        ) from exc
    except OSError as exc:
        raise ProjectorEnvironmentError(
            "private graph projector environment is inaccessible"
        ) from exc

    if not stat.S_ISREG(private_stat.st_mode):
        raise ProjectorEnvironmentError(
            "private graph projector environment must be a regular file"
        )
    if private_stat.st_uid != expected_uid:
        raise ProjectorEnvironmentError("private graph projector environment has the wrong owner")
    if stat.S_IMODE(private_stat.st_mode) != 0o600:
        raise ProjectorEnvironmentError("private graph projector environment must use mode 0600")

    private = _assignments(private_path)
    if _LEGACY_NEO4J_KEYS.intersection(private):
        raise ProjectorEnvironmentError(
            "private graph projector environment contains legacy Neo4j keys"
        )
    if set(private) - _REQUIRED_PRIVATE_KEYS:
        raise ProjectorEnvironmentError(
            "private graph projector environment contains unexpected keys"
        )
    missing = sorted(key for key in _REQUIRED_PRIVATE_KEYS if not private.get(key))
    if missing or private.get("GRAPH_PROJECTOR_ENABLED", "").casefold() not in _TRUE_VALUES:
        raise ProjectorEnvironmentError("private graph projector environment lacks required keys")
    if private["GRAPH_PROJECTOR_NEO4J_PASSWORD"] == "REPLACE_WITH_ROTATED_PASSWORD":
        raise ProjectorEnvironmentError(
            "private graph projector password placeholder was not replaced"
        )
    mismatches = {
        key
        for key in _REQUIRED_PRIVATE_KEYS
        if key in effective_assignments and effective_assignments[key] != private[key]
    }
    if mismatches:
        raise ProjectorEnvironmentError(
            "effective graph projector settings differ from the private environment"
        )
    if (
        ledger_enabled
        and require_effective_private
        and not _REQUIRED_PRIVATE_KEYS.issubset(effective_assignments)
    ):
        raise ProjectorEnvironmentError(
            "effective private projector settings are required while the ledger is active"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--require-effective-private", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_projector_environment(
            args.shared,
            args.private,
            expected_uid=os.getuid(),
            effective_environment=os.environ,
            require_effective_private=args.require_effective_private,
        )
    except ProjectorEnvironmentError as exc:
        print(f"graph projector env preflight failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
