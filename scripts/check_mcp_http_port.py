#!/usr/bin/env python3
"""Fail closed when effective settings diverge from the production MCP binding."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

_DEFAULT_MCP_HTTP_HOST = "127.0.0.1"
_DEFAULT_MCP_HTTP_PORT = 8765
_MAX_ENV_BYTES = 64 * 1024
_TRACKED_SETTINGS = frozenset({"mcp_http_host", "mcp_http_port"})
_PRIVATE_SHARED_SETTINGS = frozenset({"mcp_http_token", "mcp_http_dream_tokens"})
_ATTESTED_RUNTIME_SETTINGS = frozenset(
    {
        "brain_dream_capability_enforcement",
        "mcp_http_dream_tokens",
        "mcp_http_token",
    }
)
_CANONICAL_RUNTIME_SETTINGS = {
    setting.casefold(): setting
    for setting in (
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT",
        "MCP_HTTP_DREAM_TOKENS",
        "MCP_HTTP_TOKEN",
    )
}


class McpHttpPortContractError(RuntimeError):
    """Raised without echoing environment-file content."""


def _parse_port(raw_value: str, *, label: str) -> int:
    if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {"'", '"'}:
        raw_value = raw_value[1:-1].strip()
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise McpHttpPortContractError(f"{label} must be a decimal integer")
    resolved_port = int(raw_value)
    if not 1 <= resolved_port <= 65535:
        raise McpHttpPortContractError(f"{label} is outside 1..65535")
    return resolved_port


def _unquote(raw_value: str) -> str:
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _effective_assignment(
    environment: Mapping[str, str],
    setting_name: str,
) -> str | None:
    values = [
        value for key, value in environment.items() if key.casefold() == setting_name.casefold()
    ]
    if len(values) > 1:
        raise McpHttpPortContractError(
            f"effective {setting_name.upper()} is assigned more than once"
        )
    return values[0] if values else None


def _require_private_runtime_file(path: Path, *, label: str, expected_uid: int) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise McpHttpPortContractError(f"{label} is required") from exc
    except OSError as exc:
        raise McpHttpPortContractError(f"{label} is inaccessible") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise McpHttpPortContractError(f"{label} must be a regular file")
    if file_stat.st_uid != expected_uid:
        raise McpHttpPortContractError(f"{label} has the wrong owner")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise McpHttpPortContractError(f"{label} must use mode 0600")
    if file_stat.st_size > _MAX_ENV_BYTES:
        raise McpHttpPortContractError(f"{label} exceeds the size limit")


def _systemd_assignments(
    path: Path,
    *,
    tracked_settings: frozenset[str],
    label: str,
    reject_untracked: bool = False,
) -> dict[str, str]:
    try:
        assignments: dict[str, str] = {}
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            exported = line.split(maxsplit=1)
            uses_export = len(exported) == 2 and exported[0] == "export"
            if uses_export:
                line = exported[1]
            key, separator, value = line.partition("=")
            normalized_key = key.strip().casefold()
            if normalized_key not in tracked_settings:
                if reject_untracked:
                    raise McpHttpPortContractError(f"{label} contains unexpected keys")
                continue
            if not separator:
                raise McpHttpPortContractError(
                    f"{_CANONICAL_RUNTIME_SETTINGS[normalized_key]} is not a valid assignment"
                )
            canonical_key = _CANONICAL_RUNTIME_SETTINGS[normalized_key]
            if uses_export:
                raise McpHttpPortContractError(
                    f"{canonical_key} must use a plain systemd assignment"
                )
            if normalized_key in assignments:
                raise McpHttpPortContractError(f"{canonical_key} is assigned more than once")
            assignments[normalized_key] = _unquote(value)
    except McpHttpPortContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise McpHttpPortContractError(f"{label} is unreadable") from exc
    return assignments


def validate_mcp_http_runtime_files(
    shared_path: Path,
    token_path: Path,
    *,
    expected_uid: int,
    effective_environment: Mapping[str, str] | None = None,
    require_effective_token: bool = False,
    require_effective_runtime_settings: bool = False,
) -> None:
    """Attest production environment files without exposing their values."""
    _require_private_runtime_file(
        shared_path,
        label="shared MCP environment",
        expected_uid=expected_uid,
    )
    _require_private_runtime_file(
        token_path,
        label="private MCP token environment",
        expected_uid=expected_uid,
    )
    shared_assignments = _systemd_assignments(
        shared_path,
        tracked_settings=_ATTESTED_RUNTIME_SETTINGS,
        label="shared MCP environment",
    )
    if _PRIVATE_SHARED_SETTINGS.intersection(shared_assignments):
        raise McpHttpPortContractError("shared environment contains private MCP secrets")
    private_assignments = _systemd_assignments(
        token_path,
        tracked_settings=_ATTESTED_RUNTIME_SETTINGS,
        label="private MCP token environment",
        reject_untracked=True,
    )
    private_token = private_assignments.get("mcp_http_token")
    if not private_token:
        raise McpHttpPortContractError(
            "private MCP token environment requires one non-empty MCP_HTTP_TOKEN"
        )
    if not require_effective_token and not require_effective_runtime_settings:
        return
    if effective_environment is None:
        raise McpHttpPortContractError("effective MCP_HTTP_TOKEN is required")
    attested_assignments = shared_assignments | private_assignments
    required_settings = (
        _ATTESTED_RUNTIME_SETTINGS
        if require_effective_runtime_settings
        else frozenset({"mcp_http_token"})
    )
    for setting_name in sorted(required_settings):
        canonical_name = _CANONICAL_RUNTIME_SETTINGS[setting_name]
        expected_value = attested_assignments.get(setting_name)
        effective_value = _effective_assignment(effective_environment, setting_name)
        if expected_value is None:
            if effective_value is not None:
                raise McpHttpPortContractError(
                    f"effective {canonical_name} is not attested by an environment file"
                )
            continue
        if effective_value is None:
            raise McpHttpPortContractError(f"effective {canonical_name} is required")
        if effective_value != expected_value:
            raise McpHttpPortContractError(
                f"effective {canonical_name} differs from the private environment"
            )


def validate_mcp_http_port(
    shared_path: Path,
    *,
    expected_port: int,
    expected_host: str = _DEFAULT_MCP_HTTP_HOST,
    effective_environment: Mapping[str, str] | None = None,
) -> None:
    """Require one unambiguous host/port pair equal to the production contract."""
    if not 1 <= expected_port <= 65535:
        raise McpHttpPortContractError("expected MCP HTTP port is outside 1..65535")
    if not expected_host:
        raise McpHttpPortContractError("expected MCP HTTP host is empty")
    try:
        if not shared_path.exists():
            assignments: dict[str, list[str]] = {
                setting_name: [] for setting_name in _TRACKED_SETTINGS
            }
        else:
            if shared_path.stat().st_size > _MAX_ENV_BYTES:
                raise McpHttpPortContractError("shared environment exceeds the size limit")
            assignments = {setting_name: [] for setting_name in _TRACKED_SETTINGS}
            for raw_line in shared_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                exported = line.split(maxsplit=1)
                if len(exported) == 2 and exported[0] == "export":
                    line = exported[1]
                key, separator, value = line.partition("=")
                normalized_key = key.strip().casefold()
                if normalized_key not in _TRACKED_SETTINGS:
                    continue
                if not separator:
                    raise McpHttpPortContractError(
                        f"{normalized_key.upper()} is not a valid assignment"
                    )
                assignments[normalized_key].append(value.strip())
    except McpHttpPortContractError:
        raise
    except (OSError, UnicodeError) as exc:
        raise McpHttpPortContractError("shared environment is unreadable") from exc

    for setting_name, values in assignments.items():
        if len(values) > 1:
            raise McpHttpPortContractError(f"{setting_name.upper()} is assigned more than once")
    port_assignments = assignments["mcp_http_port"]
    raw_value = port_assignments[0] if port_assignments else str(_DEFAULT_MCP_HTTP_PORT)
    resolved_port = _parse_port(raw_value, label="MCP_HTTP_PORT")
    if resolved_port != expected_port:
        raise McpHttpPortContractError("MCP_HTTP_PORT differs from the production contract")

    host_assignments = assignments["mcp_http_host"]
    resolved_host = _unquote(host_assignments[0] if host_assignments else _DEFAULT_MCP_HTTP_HOST)
    if resolved_host != expected_host:
        raise McpHttpPortContractError("MCP_HTTP_HOST differs from the production contract")

    if effective_environment is not None:
        effective_port = _effective_assignment(effective_environment, "mcp_http_port")
        if effective_port is not None and effective_port != str(expected_port):
            raise McpHttpPortContractError(
                "effective MCP HTTP port differs from the production contract"
            )
        effective_host = _effective_assignment(effective_environment, "mcp_http_host")
        if effective_host is not None and effective_host != expected_host:
            raise McpHttpPortContractError(
                "effective MCP HTTP host differs from the production contract"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--expected", type=int, required=True)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--require-effective-token", action="store_true")
    parser.add_argument("--require-effective-runtime-settings", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if (
            args.require_effective_token or args.require_effective_runtime_settings
        ) and args.token_file is None:
            raise McpHttpPortContractError(
                "--token-file is required for effective runtime attestation"
            )
        if args.token_file is not None:
            validate_mcp_http_runtime_files(
                args.shared,
                args.token_file,
                expected_uid=os.getuid(),
                effective_environment=os.environ,
                require_effective_token=args.require_effective_token,
                require_effective_runtime_settings=(args.require_effective_runtime_settings),
            )
        validate_mcp_http_port(
            args.shared,
            expected_port=args.expected,
            expected_host=args.expected_host,
            effective_environment=os.environ,
        )
    except McpHttpPortContractError as exc:
        print(f"MCP HTTP binding preflight failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
