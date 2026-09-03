"""Replay the tracked network boundary against THIS host, claim by claim.

Ticket `0234938d`. The boundary paragraph lives identically in
`docs/ARCHITECTURE.md`, `docs/OPERATIONS.md` and the private `CLAUDE.md`, and
`test_documentation_contract.py` pins it -- but that pin compares STRINGS across
four files and is indifferent to whether they are true. It stayed green on
2026-09-03 while one clause had become false, which is the defect this module
answers: the paragraph says "watched by no test", and until now that was exact.

WHY IT IS OPT-IN, and what that costs. Every assertion below reads the machine it
runs on: listening sockets, published container ports, a live bearer refusal.
Hosted CI has none of that, and `red-ci` measures only at push, so a boundary
probe wired into the default run would be GREEN BY ABSENCE everywhere it matters
-- the same shape of false comfort the string pin already provides. It therefore
carries the `boundary_host` marker, excluded by `addopts`, and is run
deliberately:

    .venv/bin/pytest -m boundary_host -q

WHAT IT PROVES, AND WHEN. It proves the boundary at the instant it runs, on one
host. It does not watch. Three of the falsifiers the paragraph names are one
command away -- a publish override, an env var, an `unset` -- and nothing here
notices them between two runs. Running it after a compose change or a restart is
the whole point; treating a past green as a current fact is the error it exists
to make harder.

It reads no secret. `MCP_HTTP_TOKEN` is checked by PRESENCE (a count of matching
environment lines), never by value, and no `docker inspect` output is printed.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.boundary_host

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The four services the paragraph names as loopback-bound, with the port each
#: is published on. Enumerated rather than discovered: a service dropping off the
#: host would otherwise pass by having nothing to check.
LOOPBACK_PORTS = {
    "MCP": 8765,
    "PostgreSQL": 5433,
    "Neo4j bolt": 7687,
    "Neo4j browser": 7474,
}

SHIM_PORT = 8003
MCP_PORT = 8765


def _run(*argv: str, timeout: int = 10) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return result.stdout


def _listening() -> dict[int, set[str]]:
    """Port -> set of bind addresses, read from `ss -ltn`."""
    binds: dict[int, set[str]] = {}
    for line in _run("ss", "-ltn").splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        local = fields[3]
        address, _, port = local.rpartition(":")
        if not port.isdigit():
            continue
        binds.setdefault(int(port), set()).add(address.strip("[]"))
    return binds


def _lan_address() -> str:
    """The host's own LAN address — the one the paragraph says refuses."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.168.1.1", 1))
        return probe.getsockname()[0]


def _http_status(url: str, *, header: str | None = None) -> str:
    argv = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST", "--max-time", "5"]
    if header:
        argv += ["-H", header]
    argv += ["-H", "Content-Type: application/json", "-d", "{}", url]
    return _run(*argv, timeout=15).strip()


@pytest.fixture(scope="module")
def listening() -> dict[int, set[str]]:
    if shutil.which("ss") is None:
        pytest.fail("`ss` absent : la sonde ne peut pas mesurer les binds, elle ne les suppose pas")
    return _listening()


@pytest.mark.parametrize("service,port", sorted(LOOPBACK_PORTS.items()))
def test_claim_1_the_named_services_bind_to_loopback(
    service: str, port: int, listening: dict[int, set[str]]
) -> None:
    """« MCP, PostgreSQL and Neo4j bind to loopback »."""
    addresses = listening.get(port, set())
    assert addresses, f"{service} n'écoute pas sur :{port} — l'affirmation n'est pas vérifiable"
    assert addresses <= {"127.0.0.1", "::1"}, (
        f"{service} (:{port}) est lié hors loopback : {sorted(addresses)}"
    )


def test_claim_2_the_embedding_publish_is_loopback_only() -> None:
    """« The versioned Compose target binds the embedding host publish to loopback »."""
    published = _run("docker", "port", "brain_v42_embedding_shim")

    assert published.strip(), "le conteneur du shim ne publie rien — mesure impossible"
    for line in published.splitlines():
        _, _, host = line.partition("->")
        assert host.strip().startswith("127.0.0.1:"), f"publish hors loopback : {line.strip()}"
    assert f"127.0.0.1:{SHIM_PORT}" in published


def test_claim_3_the_hosts_own_lan_address_refuses() -> None:
    """« with the host's own LAN address refusing the connection »."""
    lan = _lan_address()
    assert not lan.startswith("127."), f"adresse LAN non trouvée (obtenu {lan})"

    for port in (SHIM_PORT, MCP_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(3)
            assert probe.connect_ex((lan, port)) != 0, (
                f"{lan}:{port} ACCEPTE la connexion — la frontière est ouverte sur le LAN"
            )


def test_claim_4_the_bearer_is_set_in_the_live_process() -> None:
    """« `MCP_HTTP_TOKEN` is set and non-empty in the live server process ».

    Presence only. The value is never read, never compared, never printed.
    """
    pids = _run("pgrep", "-f", "brain_v42.mcp").split()
    assert pids, "aucun process MCP vivant — l'affirmation n'est pas vérifiable"

    found = 0
    for pid in pids:
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")
        except (PermissionError, FileNotFoundError):  # pragma: no cover - host dependent
            continue
        found += len(re.findall(r"(?:^|\x00)MCP_HTTP_TOKEN=.", environ))

    assert found >= 1, "MCP_HTTP_TOKEN absent ou vide dans le process vivant"


@pytest.mark.parametrize(
    "label,header",
    [("sans bearer", None), ("avec un mauvais bearer", "Authorization: Bearer wrong-on-purpose")],
)
def test_claim_5_post_mcp_answers_401(label: str, header: str | None) -> None:
    """« `POST /mcp` answers `401` both without a bearer and with a wrong one »."""
    assert _http_status(f"http://127.0.0.1:{MCP_PORT}/mcp", header=header) == "401", (
        f"POST /mcp {label} n'a pas répondu 401"
    )


def test_claim_6_brain_net_carries_the_shim_and_both_auto_discord() -> None:
    """« `brain-net` holds the embedding shim and both `auto-discord` containers »."""
    names = _run(
        "docker",
        "network",
        "inspect",
        "brain-net",
        "--format",
        "{{range .Containers}}{{.Name}} {{end}}",
    ).split()

    assert "brain_v42_embedding_shim" in names, f"shim absent de brain-net : {sorted(names)}"
    auto_discord = [name for name in names if name.startswith("auto-discord")]
    assert len(auto_discord) >= 2, f"les deux auto-discord ne sont pas sur brain-net : {names}"


def test_claim_7_the_repository_manages_no_firewall_rule() -> None:
    """« the repository manages no firewall rule at all »."""
    hits = _run(
        "git",
        "-C",
        str(REPO_ROOT),
        "grep",
        "-lIE",
        "iptables|nftables|ufw enable|firewall-cmd",
    ).split()

    assert not hits, f"le dépôt gère une règle de pare-feu : {hits}"


def test_claim_8_no_publish_override_reopens_the_shim() -> None:
    """First named falsifier: « a host-publish override reopening `:8003` »."""
    assert not (REPO_ROOT / "docker-compose.override.yml").exists(), (
        "docker-compose.override.yml est de retour : le publish hôte peut avoir été rouvert"
    )
    assert not os.environ.get("COMPOSE_FILE"), "COMPOSE_FILE est défini — la cible Compose diverge"


def test_claim_9_the_metrics_bind_is_loopback_and_now_guarded(
    listening: dict[int, set[str]],
) -> None:
    """Second falsifier, DISARMED on 2026-09-03: the validator exists (`6c61b63`).

    Two halves, and the second is the one the paragraph got wrong for a morning:
    the bind is loopback, AND a non-loopback bind is now refused rather than
    silently accepted.
    """
    from brain_v42.config import Settings

    addresses = listening.get(9200, set())
    assert addresses <= {"127.0.0.1", "::1"}, f"metrics lié hors loopback : {sorted(addresses)}"

    with pytest.raises(ValueError):
        Settings(metrics_host="0.0.0.0")  # noqa: S104 - exactly what must be refused
