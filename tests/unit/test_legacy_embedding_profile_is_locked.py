"""`--profile legacy` answers to the right names and refuses to start unnoticed.

Ticket 530d796a (SEC2 residual), brief of 2026-09-03. The legacy PyTorch service
is a ROLLBACK ARTIFACT: measured that morning, it had not run for one second
since the 2026-07-06 cutover, and `services/embedding/main.py` carries none of
the shim's bounds — no body cap, no read deadline, no semaphore, no 413/503.
The arbitration was to keep it as a rollback and LOCK it, not to bound 179 lines
that only run inside a degraded window opened by hand.

Two defects were closed, and they are of different natures.

**The DNS break was silent and certain.** The legacy service publishes
`container_name: brain_v42_embedding` and declared no alias, while its two live
clients on `brain-net` — `auto-discord-bot` and `auto-discord-dagster-daemon` —
carry no `EMBEDDING_URL` at all and fall back to the code default
`http://brain_v42_embedding_shim:8003`. The compose of auto_discord has since
been fixed to pin `embedding-shim:8003`, but the RUNNING containers predate that
fix. Both names therefore had to be preserved, not one.

**THIS FILE IS ADVISORY ON THE SECOND POINT, AND SAYING SO IS THE POINT.**
`pytest` does not intercept `docker compose --profile legacy up`; Docker does not
consult a test suite. What actually refuses is the `entrypoint` in the compose
file, executed by the container itself. This module therefore does two different
things: it FREEZES the aliases as text, and it EXECUTES the guard's real shell
script in a subshell — which is a behaviour test, not a text assertion. Neither
can stop an operator; the entrypoint can.

**Why the guard lives in the compose `entrypoint` and not in the Dockerfile.**
`brain_v42-embedding:latest` was built eight weeks ago and `docker compose up`
reuses an existing image: an `ENTRYPOINT` added to the Dockerfile stays INERT
until someone rebuilds. And a rebuild is precisely the operation nobody can
vouch for — the base image is pinned by digest but `torch` is not, so eight
weeks of PyTorch/CUDA drift sit between the recipe and the artifact. A guard
that only arms itself through the least trustworthy operation available is not a
guard. The compose entrypoint overrides the image and applies to the artifact
that exists today.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"

_ACK_VAR = "BRAIN_LEGACY_EMBEDDING_ACK"
#: The names the two live clients on `brain-net` actually resolve. Both, not one:
#: the compose of auto_discord pins the first, the running containers fall back
#: to the code default, which is the second.
_REQUIRED_ALIASES = {"embedding-shim", "brain_v42_embedding_shim"}
#: EX_CONFIG. Distinguishable from a crash (non-zero but arbitrary) and from the
#: 125-127 range Docker uses for its own failures to start a container.
_EX_CONFIG = 78


def _legacy_service() -> dict[str, Any]:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["embedding"]
    assert service["profiles"] == ["legacy"], "ce test ne parle que du service à profil legacy"
    return service


def _guard_script() -> str:
    """The refusal's real shell source, read from the compose — never retyped.

    `$$` is un-escaped to `$` because that is what Compose itself does before
    handing the string to the container: in the FILE the sequence protects the
    shell variable from Compose's own interpolation. Doing it here is the one
    transformation this test applies, and the rendering is checked against the
    real `docker compose --profile legacy config` outside the suite (the report
    of 2026-09-03 carries the command and its output). A test that executed the
    raw file content would prove a script production never runs.
    """
    entrypoint = _legacy_service()["entrypoint"]
    assert entrypoint[:2] == ["/bin/sh", "-c"], entrypoint
    return entrypoint[2].replace("$$", "$")


def _run_guard(*, ack: str | None) -> subprocess.CompletedProcess[str]:
    """Execute the guard with a harmless payload instead of the real server.

    Docker appends the image's `CMD` after the entrypoint's own arguments, so
    `"$@"` is what the guard hands over on success. Here that payload is `echo`,
    which is why this test can prove the pass-through without starting uvicorn,
    without a GPU and without a container.
    """
    env = {"PATH": "/usr/bin:/bin"}
    if ack is not None:
        env[_ACK_VAR] = ack
    return subprocess.run(
        ["/bin/sh", "-c", _guard_script(), "legacy-guard", "echo", "SERVER-STARTED"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


class TestItKeepsBothNamesItsClientsUse:
    def test_the_legacy_service_answers_to_both_dns_names(self) -> None:
        aliases = set(_legacy_service()["networks"]["brain-net"]["aliases"])

        assert _REQUIRED_ALIASES <= aliases, (
            "un rollback qui ne préserve qu'un des deux noms casse en silence "
            "les clients qui utilisent l'autre"
        )

    def test_it_still_reaches_the_default_network_and_stays_on_loopback(self) -> None:
        """The alias fix must not quietly drop the rest of the block."""
        service = _legacy_service()

        assert "default" in service["networks"]
        assert service["ports"] == ["127.0.0.1:8003:8003"]


class TestItRefusesToStartUnacknowledged:
    def test_without_the_acknowledgement_it_exits_78_and_starts_nothing(self) -> None:
        result = _run_guard(ack=None)

        assert result.returncode == _EX_CONFIG
        assert "SERVER-STARTED" not in result.stdout

    def test_a_wrong_value_is_not_an_acknowledgement(self) -> None:
        """`yes` exactly. `true`, `1` and `YES` are somebody guessing."""
        assert _run_guard(ack="true").returncode == _EX_CONFIG
        assert _run_guard(ack="YES").returncode == _EX_CONFIG
        assert _run_guard(ack="").returncode == _EX_CONFIG

    def test_the_refusal_says_what_is_missing_and_why_it_matters(self) -> None:
        """An operator reading this at 03:00 needs the word to type AND the risk."""
        printed = _run_guard(ack=None).stderr

        assert _ACK_VAR in printed
        assert "legacy" in printed.lower()
        for missing_bound in ("body", "deadline", "semaphore"):
            assert missing_bound in printed.lower(), missing_bound

    def test_the_acknowledgement_hands_over_to_the_image_command(self) -> None:
        """On success the guard must EXEC the payload, not swallow it.

        A guard that refuses correctly and then fails to start the service would
        turn a rollback into an outage — with the acknowledgement already given.
        """
        result = _run_guard(ack="yes")

        assert result.returncode == 0
        assert "SERVER-STARTED" in result.stdout


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_the_refusal_never_leaks_the_acknowledgement_into_stdout(stream: str) -> None:
    """The message belongs on stderr; stdout is where a caller may parse output."""
    result = _run_guard(ack=None)

    assert (_ACK_VAR in getattr(result, stream)) == (stream == "stderr")
