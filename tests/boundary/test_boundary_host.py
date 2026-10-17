"""Probe the local or workstation loopback/shared_token deployment on THIS host.

Tickets `0234938d` and `d1d87ff3`. This opt-in probe measures a local or workstation
host running the loopback deployment in `shared_token` mode, including the former
workstation systemd deployment (rollback B, retired by G14). Production red-base
(`deploy/compose.yaml`) is pinned instead by `tests/unit/test_red_base_compose.py`
and `tests/unit/test_documentation_contract.py`. These workstation claims do not
describe the current production network boundary.

WHY IT IS OPT-IN, and what that costs. Every assertion below reads the machine it
runs on: listening sockets, published container ports, a live bearer refusal.
Hosted CI has no workstation deployment, so a boundary probe wired into the default
run would be GREEN BY ABSENCE rather than measure the intended host. It therefore
carries the `boundary_host` marker, excluded by `addopts`, and is run
deliberately:

    .venv/bin/pytest -m boundary_host -q

WHAT IT PROVES, AND WHEN. It proves the boundary at the instant it runs, on one
workstation host. It does not watch. Three ways to reopen that boundary are one
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

#: The four workstation services expected to be loopback-bound, with the port each
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
    """The workstation's own LAN address, expected to refuse MCP and shim connections."""
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
    """Workstation MCP, PostgreSQL and Neo4j listen only on loopback."""
    addresses = listening.get(port, set())
    assert addresses, f"{service} n'écoute pas sur :{port} — l'affirmation n'est pas vérifiable"
    assert addresses <= {"127.0.0.1", "::1"}, (
        f"{service} (:{port}) est lié hors loopback : {sorted(addresses)}"
    )


def test_claim_2_the_embedding_publish_is_loopback_only() -> None:
    """The workstation Compose target publishes the embedding shim only on loopback."""
    published = _run("docker", "port", "brain_v42_embedding_shim")

    assert published.strip(), "le conteneur du shim ne publie rien — mesure impossible"
    for line in published.splitlines():
        _, _, host = line.partition("->")
        assert host.strip().startswith("127.0.0.1:"), f"publish hors loopback : {line.strip()}"
    assert f"127.0.0.1:{SHIM_PORT}" in published


def test_claim_3_the_hosts_own_lan_address_refuses() -> None:
    """The workstation's own LAN address refuses MCP and embedding shim connections."""
    lan = _lan_address()
    assert not lan.startswith("127."), f"adresse LAN non trouvée (obtenu {lan})"

    for port in (SHIM_PORT, MCP_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(3)
            assert probe.connect_ex((lan, port)) != 0, (
                f"{lan}:{port} ACCEPTE la connexion — la frontière est ouverte sur le LAN"
            )


def test_claim_4_the_bearer_is_set_in_the_live_process() -> None:
    """The workstation shared_token server process has a non-empty `MCP_HTTP_TOKEN`.

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
    """Workstation `POST /mcp` returns `401` for missing or incorrect shared bearers."""
    assert _http_status(f"http://127.0.0.1:{MCP_PORT}/mcp", header=header) == "401", (
        f"POST /mcp {label} n'a pas répondu 401"
    )


def test_claim_6_brain_net_carries_the_shim_and_both_auto_discord() -> None:
    """Workstation `brain-net` holds the embedding shim and both `auto-discord` containers."""
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
    """The workstation deployment's repository manages no firewall rule."""
    hits = _run(
        "git",
        "-C",
        str(REPO_ROOT),
        "grep",
        "-lIE",
        "iptables|nftables|ufw enable|firewall-cmd",
        # `:!` excludes THIS file. The probe carries the pattern as a literal, so
        # it matched itself the moment it stopped being untracked and `git grep`
        # could see it — green while unstaged, red one commit later. A census
        # that scans its own pattern definition is the same defect class as a
        # guard sharing its alarm's blind spot.
        "--",
        ".",
        ":!tests/boundary/test_boundary_host.py",
    ).split()

    assert not hits, f"le dépôt gère une règle de pare-feu : {hits}"


def test_claim_8_no_publish_override_reopens_the_shim() -> None:
    """No workstation Compose override can reopen the embedding shim host publish."""
    assert not (REPO_ROOT / "docker-compose.override.yml").exists(), (
        "docker-compose.override.yml est de retour : le publish hôte peut avoir été rouvert"
    )
    assert not os.environ.get("COMPOSE_FILE"), "COMPOSE_FILE est défini — la cible Compose diverge"


def test_claim_9_the_metrics_bind_is_loopback_and_now_guarded(
    listening: dict[int, set[str]],
) -> None:
    """Workstation metrics binds to loopback and refuses an unapproved non-loopback bind.

    The fail-closed validator (`6c61b63`) requires explicit non-loopback opt-in.
    """
    from brain_v42.config import Settings

    addresses = listening.get(9200, set())
    assert addresses <= {"127.0.0.1", "::1"}, f"metrics lié hors loopback : {sorted(addresses)}"

    with pytest.raises(ValueError):
        Settings(metrics_host="0.0.0.0")  # noqa: S104 - exactly what must be refused


#: The boolean deciding whether dream nights run SCOPED or on an ADMIN token.
#: Its value is not a secret — the ticket says so — and only this one line is
#: ever read out of the file that holds it.
_CAPABILITY_FLAG = "BRAIN_DREAM_CAPABILITY_ENFORCEMENT"


def _socket_owner_pid(port: int) -> str:
    """The pid HOLDING the port, read from `ss -ltnp`.

    Deliberately not `pgrep`. On 2026-09-03 `pgrep -f 'brain_v42.mcp' | head -1`
    returned a pid that had already died, whose `/proc/<pid>/environ` could not be
    read, and taking that for "the flag is absent" pointed at a disarmed server
    that was in fact armed. A socket has exactly one owner; a name pattern has as
    many as the host happens to carry.
    """
    for line in _run("ss", "-ltnp").splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[3].endswith(f":{port}"):
            continue
        match = re.search(r"pid=(\d+)", line)
        if match:
            return match.group(1)
    pytest.fail(f"personne ne détient :{port} — l'affirmation n'est pas vérifiable")


def _systemctl_show(unit: str, prop: str) -> str:
    """`systemctl --user show`, with the session bus supplied rather than assumed.

    A user manager is reachable only through `XDG_RUNTIME_DIR` and
    `DBUS_SESSION_BUS_ADDRESS`, which pytest does not inherit from an interactive
    shell. Without them `systemctl --user` returns an EMPTY string and exits 0 --
    so a check reading it would find no EnvironmentFile and conclude the units
    share nothing, which is the false-negative this helper removes. Derived from
    the running uid, never from the caller's environment.
    """
    uid = os.getuid()
    env = {
        **os.environ,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
    }
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", prop],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        env=env,
    )
    return result.stdout


def test_claim_10_the_server_holding_8765_enforces_the_capability_scope() -> None:
    """The workstation Dream capability scope is ARMED in the process that serves MCP.

    Learning `f5686a04`: `systemctl show -p Environment` renders ONLY `Environment=`
    directives, never the contents of an `EnvironmentFile=`. Measured on
    2026-09-03, BOTH units report zero matching lines while the live server holds
    the flag at `true`. So this assertion cannot be written against unit files,
    and one that is would read green on a disarmed server.

    What it protects: the flag lives in a secrets file nobody reviews, and a
    botched registry rotation would drop it at the same instant as the bearers.
    The code default is `False` = ADMIN token, so its disappearance is SILENT and
    widens a night from a scoped 27% of the corpus to all of it.
    """
    pid = _socket_owner_pid(MCP_PORT)
    environ = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "replace")

    values = re.findall(rf"(?:^|\x00){_CAPABILITY_FLAG}=([^\x00]*)", environ)

    assert values, (
        f"{_CAPABILITY_FLAG} absent du process qui sert :{MCP_PORT} (pid {pid}) — "
        "les nuits dream repartiraient en jeton ADMIN"
    )
    assert values[-1].strip().lower() == "true", (
        f"{_CAPABILITY_FLAG} vaut {values[-1]!r} dans le process qui sert :{MCP_PORT}"
    )


def test_claim_10b_the_dream_unit_reads_the_same_environment_file() -> None:
    """The workstation MCP and Dream units share the file that DEFINES the flag.

    The MCP server enforces the scope; the dream phases pick their per-phase
    token. They agree only because ONE file is listed by both. Moving the key to
    a dream-only drop-in would disarm the server while looking like a cleanup —
    the trap ticket `9e1a2b39` names, and the reason this second assertion exists.
    """

    def files(unit: str) -> set[str]:
        raw = _systemctl_show(unit, "EnvironmentFiles")
        return set(re.findall(r"(/\S+?)\s*\(ignore_errors=", raw))

    mcp_files = files("brain-mcp-http.service")
    dream_files = files("brain-v42-dream.service")
    assert mcp_files, "impossible de lire les EnvironmentFiles de brain-mcp-http"

    defining = {
        path
        for path in mcp_files
        if Path(path).is_file()
        and any(
            line.startswith(f"{_CAPABILITY_FLAG}=")
            for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        )
    }

    assert defining, f"aucun EnvironmentFile de brain-mcp-http ne définit {_CAPABILITY_FLAG}"
    assert defining & dream_files, (
        f"le fichier qui définit {_CAPABILITY_FLAG} ({sorted(defining)}) n'est pas lu par "
        "brain-v42-dream : les deux unités divergeraient en silence"
    )
