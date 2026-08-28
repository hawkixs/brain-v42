"""Security contracts for the repository-managed systemd user services."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"

SERVICE_TEMPLATES = tuple(path.name for path in sorted(SYSTEMD_DIR.glob("*.service.tmpl")))

NAMESPACE_DIRECTIVES = frozenset(
    {
        "BindPaths",
        "BindReadOnlyPaths",
        "ExecPaths",
        "ExtensionImages",
        "InaccessiblePaths",
        "MountAPIVFS",
        "MountImages",
        "NoExecPaths",
        "PrivateDevices",
        "PrivateIPC",
        "PrivateMounts",
        "PrivateNetwork",
        "PrivateTmp",
        "ProcSubset",
        "ProtectClock",
        "ProtectControlGroups",
        "ProtectHome",
        "ProtectHostname",
        "ProtectKernelLogs",
        "ProtectKernelModules",
        "ProtectKernelTunables",
        "ProtectProc",
        "ProtectSystem",
        "ReadOnlyPaths",
        "ReadWritePaths",
        "RootDirectory",
        "RootImage",
        "TemporaryFileSystem",
    }
)

PROFILE_DIRECTIVES = NAMESPACE_DIRECTIVES | {
    "AmbientCapabilities",
    "CapabilityBoundingSet",
    "KeyringMode",
    "LockPersonality",
    "MemoryDenyWriteExecute",
    "NoNewPrivileges",
    "PrivateUsers",
    "RestrictAddressFamilies",
    "RestrictNamespaces",
    "RestrictRealtime",
    "RestrictSUIDSGID",
    "SystemCallArchitectures",
    "SystemCallErrorNumber",
    "SystemCallFilter",
    "UMask",
}

STRONG_INTEGRITY_PROFILE = (
    "UMask=0077",
    "NoNewPrivileges=true",
    "PrivateUsers=true",
    "PrivateTmp=true",
    "PrivateDevices=true",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "ProtectClock=true",
    "ProtectControlGroups=true",
    "ProtectKernelLogs=true",
    "ProtectKernelModules=true",
    "ProtectKernelTunables=true",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "KeyringMode=private",
    "LockPersonality=true",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    "RestrictRealtime=true",
    "RestrictSUIDSGID=true",
    "SystemCallArchitectures=native",
)

MCP_HTTP_PROFILE = tuple(
    line
    for line in STRONG_INTEGRITY_PROFILE
    if not line.startswith(("ProtectSystem=", "ProtectHome="))
) + (
    "ProtectSystem=full",
    "ReadOnlyPaths=__REPO_ROOT__/.env %h/.config/brain-v42",
)

REDUCED_PROFILE = (
    "UMask=0077",
    "NoNewPrivileges=true",
    "LockPersonality=true",
    "RestrictRealtime=true",
    "RestrictSUIDSGID=true",
    "SystemCallArchitectures=native",
)

EXPECTED_PROFILES = {
    "brain-v42-automation.service.tmpl": STRONG_INTEGRITY_PROFILE,
    "brain-v42-graph-recon.service.tmpl": STRONG_INTEGRITY_PROFILE,
    "brain-v42-model-liveness.service.tmpl": STRONG_INTEGRITY_PROFILE,
    "brain-mcp-http.service.tmpl": MCP_HTTP_PROFILE,
    "brain-v42-dream.service.tmpl": REDUCED_PROFILE,
    "brain-v42-embedding-backfill.service.tmpl": REDUCED_PROFILE,
    "brain-mcp-http-watchdog.service.tmpl": REDUCED_PROFILE
    + ("RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",),
}

DEFERRED_DREAM_DIRECTIVES = frozenset(
    {
        "AmbientCapabilities",
        "CapabilityBoundingSet",
        "MemoryDenyWriteExecute",
        "PrivateDevices",
        "PrivateTmp",
        "PrivateUsers",
        "ProtectHome",
        "ProtectSystem",
        "ReadOnlyPaths",
        "ReadWritePaths",
        "RestrictAddressFamilies",
        "RestrictNamespaces",
        "SystemCallErrorNumber",
        "SystemCallFilter",
    }
)


def _directive(line: str) -> tuple[str, str] | None:
    key, separator, value = line.partition("=")
    if not separator:
        return None
    return key.strip(), value.strip()


def _logical_lines(unit_name: str) -> list[str]:
    logical_lines: list[str] = []
    pending = ""
    for raw_line in (SYSTEMD_DIR / unit_name).read_text(encoding="utf-8").splitlines():
        physical_line = raw_line.rstrip("\r")
        if physical_line.lstrip().startswith(("#", ";")):
            continue
        trailing_backslashes = len(physical_line) - len(physical_line.rstrip("\\"))
        if trailing_backslashes % 2 == 1:
            pending += physical_line[:-1] + " "
            continue
        logical_lines.append(pending + physical_line)
        pending = ""
    if pending:
        logical_lines.append(pending)
    return logical_lines


def _section_directives(unit_name: str, expected_section: str) -> list[tuple[str, str]]:
    directives: list[tuple[str, str]] = []
    current_section = ""
    for raw_line in _logical_lines(unit_name):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_section = line[1:-1]
            continue
        if current_section == expected_section and (directive := _directive(line)):
            directives.append(directive)
    return directives


def _directives(unit_name: str) -> list[tuple[str, str]]:
    return _section_directives(unit_name, "Service")


def _profile_lines(unit_name: str) -> list[str]:
    return [f"{key}={value}" for key, value in _directives(unit_name) if key in PROFILE_DIRECTIVES]


def test_every_service_template_has_an_exact_profile_contract() -> None:
    assert set(SERVICE_TEMPLATES) == set(EXPECTED_PROFILES)


@pytest.mark.parametrize("unit_name", SERVICE_TEMPLATES)
def test_namespace_directives_require_private_users(unit_name: str) -> None:
    directives = _directives(unit_name)
    namespace_keys = {key for key, _ in directives} & NAMESPACE_DIRECTIVES

    if not namespace_keys:
        return

    assert ("PrivateUsers", "true") in directives, (
        f"{unit_name} uses namespace directives without PrivateUsers=true: {sorted(namespace_keys)}"
    )


@pytest.mark.parametrize(
    ("unit_name", "expected_profile"),
    EXPECTED_PROFILES.items(),
    ids=EXPECTED_PROFILES,
)
def test_systemd_security_profile_is_exact(
    unit_name: str,
    expected_profile: tuple[str, ...],
) -> None:
    actual_profile = _profile_lines(unit_name)
    actual_keys = [line.partition("=")[0] for line in actual_profile]

    assert len(actual_keys) == len(set(actual_keys)), (
        f"{unit_name} repeats or overrides a security directive: {actual_profile}"
    )
    assert Counter(actual_profile) == Counter(expected_profile)


def test_dream_omits_deferred_sandbox_directives() -> None:
    dream_keys = {key for key, _ in _directives("brain-v42-dream.service.tmpl")}

    assert dream_keys.isdisjoint(DEFERRED_DREAM_DIRECTIVES)


def test_graph_recon_runs_read_only_ledger_inventory() -> None:
    exec_starts = [
        value
        for key, value in _directives("brain-v42-graph-recon.service.tmpl")
        if key == "ExecStart"
    ]

    assert exec_starts == [
        "__REPO_ROOT__/.venv/bin/python __REPO_ROOT__/scripts/rebuild_graph_projection.py"
    ]
    assert "/bin/bash" not in exec_starts[0]
    assert "--fix" not in exec_starts[0]
    assert "recover_graph_projection.py" not in exec_starts[0]


def test_model_liveness_runs_the_read_only_probe_and_nothing_else() -> None:
    """La sonde hebdomadaire reste HORS du chemin de la nuit (décision b002c0a4).

    Elle mesure et sort — jamais `dream.sh`, jamais un remplacement de modèle,
    jamais une écriture. `-m` est obligatoire : lancé par chemin de fichier,
    `sys.path[0]` serait `scripts/` et l'import de l'inventaire échouerait.
    """
    exec_starts = [
        value
        for key, value in _directives("brain-v42-model-liveness.service.tmpl")
        if key == "ExecStart"
    ]

    assert exec_starts == ["__REPO_ROOT__/.venv/bin/python -m scripts.probe_model_liveness"]

    unit_text = (SYSTEMD_DIR / "brain-v42-model-liveness.service.tmpl").read_text(encoding="utf-8")
    assert "dream.sh" not in unit_text


@pytest.mark.parametrize(
    "unit_name",
    (
        "brain-v42-dream.service.tmpl",
        "brain-v42-graph-recon.service.tmpl",
        "brain-v42-model-liveness.service.tmpl",
    ),
)
def test_user_services_do_not_reference_system_network_online_target(
    unit_name: str,
) -> None:
    ordering_targets = {
        target
        for key, value in _section_directives(unit_name, "Unit")
        if key in {"After", "Wants"}
        for target in value.split()
    }

    assert "network-online.target" not in ordering_targets
