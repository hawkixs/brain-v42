"""A WET flip must not arm auto-apply by accident (ticket 7511c210).

The trap was armed on 2026-09-03 and measured the same morning: the strict
canary put `nvidia/nemotron-3-super-120b-a12b` in `models.conf` as the ROADMAP
primary, and that model IS `DEFAULT_WET_ROADMAP_MODEL`, hence a member of
`AUTO_APPLY_MODELS`. The killswitch is DRY, so nothing applies today. But
`BRAIN_DREAM_ROADMAP_DRY_RUN=false` is ONE word, and that word would, with no
other change, turn the night's curation from a proposer into a writer —
`WET_APPLYABLE_OPS` archives features and moves statuses.

Under the previous chain (`gpt-oss-20b`) the same flip was harmless: the model
sat outside the allowlist and `main` downgraded `--wet` with a printed refusal.
The danger is therefore NOT the flip, it is the flip on a primary the operator
did not choose for that purpose.

**Why the guard lives in Python and not in `dream.sh`.** Two reasons, and each
is sufficient. `AUTO_APPLY_MODELS` exists in exactly one place —
`src/brain_v42/scripts/roadmap_curate.py:132` — and nothing under `scripts/`
knows it; a shell preflight would have to RETYPE the model list, and a mirror
that copies its constants stops being one the first time a single side changes.
And the effective primary is not readable from the environment alone: when
`BRAIN_NVIDIA_ROADMAP_MODEL` is unset it comes from a Python default that
itself depends on `--wet`.

This test reads the REAL frozensets, never a copy of the names.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import brain_v42.scripts.roadmap_curate as module

_ACK_VAR = "BRAIN_DREAM_ROADMAP_AUTO_APPLY_ACK"
#: Read from the code, never retyped: a test that hard-codes the model name
#: keeps passing on the day the allowlist changes underneath it.
_AUTO_APPLY_MODEL = sorted(module.AUTO_APPLY_MODELS)[0]
_REVIEW_ONLY_MODEL = sorted(module.PROPOSER_ONLY_MODELS)[0]


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: list[str],
    model: str,
    ack: str | None = None,
) -> tuple[int, bool]:
    """Run the real `main()` up to the phase, which is stubbed. Returns (rc, ran)."""
    ran = {"value": False}

    async def _fake_run(*_args: Any, **_kwargs: Any) -> int:
        ran["value"] = True
        return 0

    monkeypatch.setattr(module, "load_env_file", lambda _path: {})
    monkeypatch.setattr(module, "_run", _fake_run)
    monkeypatch.setenv(module._API_KEY_VAR, "not-a-real-key")
    monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", model)
    monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", raising=False)
    if ack is None:
        monkeypatch.delenv(_ACK_VAR, raising=False)
    else:
        monkeypatch.setenv(_ACK_VAR, ack)
    monkeypatch.setattr(sys, "argv", ["roadmap_curate", *argv])

    return module.main(), ran["value"]


class TestItRefusesAnUnacknowledgedAutoApply:
    def test_wet_on_an_auto_apply_primary_without_ack_stops_the_phase(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Fail-closed: a non-zero exit, and the phase never runs."""
        rc, ran = _invoke(monkeypatch, argv=["--wet"], model=_AUTO_APPLY_MODEL)

        assert rc != 0
        assert not ran, "la curation ne doit pas démarrer quand le garde refuse"

    def test_the_refusal_names_the_model_AND_the_variable_that_lifts_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal nobody can act on is an outage with extra steps.

        The operator reading this line at 06:00 needs two things: WHICH model
        made the night dangerous, and the exact word that says "yes, I meant it".
        """
        _invoke(monkeypatch, argv=["--wet"], model=_AUTO_APPLY_MODEL)

        captured = capsys.readouterr()
        printed = captured.out + captured.err
        assert _AUTO_APPLY_MODEL in printed
        assert _ACK_VAR in printed

    def test_an_explicit_ack_lets_the_wet_run_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard blocks an ACCIDENT, never a decision that was made."""
        rc, ran = _invoke(monkeypatch, argv=["--wet"], model=_AUTO_APPLY_MODEL, ack="yes")

        assert rc == 0
        assert ran

    def test_a_dry_run_passes_without_a_word(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Today's nominal path. A guard that shouts every night stops being read."""
        rc, ran = _invoke(monkeypatch, argv=[], model=_AUTO_APPLY_MODEL)

        assert (rc, ran) == (0, True)
        assert _ACK_VAR not in capsys.readouterr().out

    def test_a_review_only_primary_in_wet_is_none_of_the_guard_s_business(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`main` already downgrades `--wet` here, printing its own refusal.

        The guard must not double-refuse a case the code already renders
        harmless — that would turn a working configuration into a dead night.
        """
        rc, ran = _invoke(monkeypatch, argv=["--wet"], model=_REVIEW_ONLY_MODEL)

        assert (rc, ran) == (0, True)
