"""The guard is proven against the CONFIGURED primary, not an invented one.

Ticket 7511c210(b). `4fb7ef4` armed a guard — a wet roadmap night on a primary
inside `AUTO_APPLY_MODELS` is refused unless
`BRAIN_DREAM_ROADMAP_AUTO_APPLY_ACK=yes` — and covered it with
`test_dream_roadmap_refuses_silent_auto_apply.py`. That file is thorough about
the guard and says nothing about the trap, because it CHOOSES its model:

    _AUTO_APPLY_MODEL = sorted(module.AUTO_APPLY_MODELS)[0]

It therefore proves "IF the primary is an auto-apply model, wet without ack is
refused". It cannot notice that the primary really IS one today, and it would
keep passing unchanged if the operator swapped `models.conf` for something else
tomorrow. The ticket's complaint — "aucun test ne le voit" — is about that gap,
not about the guard.

THE COMBINATION, MEASURED ON 2026-09-03 AND NOT COPIED FROM THE TICKET:

  models.conf     BRAIN_NVIDIA_ROADMAP_MODEL=nvidia/nemotron-3-super-120b-a12b
  roadmap_curate  that model IS DEFAULT_WET_ROADMAP_MODEL, hence in AUTO_APPLY_MODELS
  killswitches    BRAIN_DREAM_ROADMAP_DRY_RUN=true   <- the only thing keeping it inert
  dream.sh        adds `--wet` only for an explicit `false` (fail-closed since
                  2026-09-04; it used to arm on anything that was not "true")

So this test starts at the CONFIG and walks to the consequence. It reads the
model list from the module and the primary from a captured copy of the live
drop-in, never a name retyped in this file (learning d43e760d) — and a separate
test compares that copy byte for byte with the live file, because a hand-pinned
fixture drifts in silence exactly like a hand-pinned constant.

WHAT IT ASSERTS, AND WHY IT IS GREEN TODAY. It does NOT demand that the primary
stay out of `AUTO_APPLY_MODELS`: the operator chose that primary deliberately
for five DRY nights (decision 7511c210(a), option B), and a test reddening on a
deliberate choice would be turned off. It asserts that the configured primary is
COVERED — whichever side of the allowlist it sits on, the wet path is safe:
refused by name when the model can auto-apply, harmlessly downgraded when it
cannot. It reddens only when the configuration is trapped AND the guard fails to
cover it, which is the fault the ticket describes.

The refusal is asserted by its NAMED cause — the model and the variable that
lifts it — never by a return code (learning e2e5a550). No systemd, no DBus, no
PostgreSQL is touched: the drop-in is read as a file, and the live comparison
skips when that file is absent, which is the normal state in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import brain_v42.scripts.roadmap_curate as module

_ACK_VAR = "BRAIN_DREAM_ROADMAP_AUTO_APPLY_ACK"
_PRIMARY_VAR = "BRAIN_NVIDIA_ROADMAP_MODEL"

#: Byte-identical capture of the live systemd drop-in, taken 2026-09-03 from
#: `~/.config/systemd/user/brain-v42-dream.service.d/models.conf`. Committed
#: because CI has no operator home, so a test that only read the live path would
#: skip in the one place it has to run.
FIXTURE = Path(__file__).parent / "data" / "models.conf.2026-09-03-live-dream-drop-in"

#: The live file this fixture copies. Absent on any machine but the server.
LIVE_DROP_IN = Path.home() / ".config/systemd/user/brain-v42-dream.service.d/models.conf"


def _drop_in_environment(text: str) -> dict[str, str]:
    """Read `Environment=KEY=VALUE` lines out of a systemd drop-in.

    Comment lines are skipped, and that matters here: this drop-in's comments
    MENTION the very variable it sets, so a naive substring search would read a
    warning as a setting.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("Environment="):
            continue
        key, _, value = stripped[len("Environment=") :].partition("=")
        values[key.strip()] = value.strip()
    return values


def configured_roadmap_primary() -> str:
    primary = _drop_in_environment(FIXTURE.read_text(encoding="utf-8")).get(_PRIMARY_VAR)
    assert primary, f"{FIXTURE.name} sets no {_PRIMARY_VAR}"
    return primary


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: list[str],
    model: str,
    ack: str | None = None,
) -> tuple[int, bool]:
    """Run the real `main()` up to the phase, which is stubbed.

    Deliberately NOT imported from `test_dream_roadmap_refuses_silent_auto_apply`.
    Borrowing that module's harness would make this test inherit the very
    assumption it exists to challenge — that the model under test is the one the
    code picks rather than the one the configuration sets.
    """
    ran = {"value": False}

    async def _fake_run(*_args: Any, **_kwargs: Any) -> int:
        ran["value"] = True
        return 0

    monkeypatch.setattr(module, "load_env_file", lambda _path: {})
    monkeypatch.setattr(module, "_run", _fake_run)
    monkeypatch.setenv(module._API_KEY_VAR, "not-a-real-key")
    monkeypatch.setenv(_PRIMARY_VAR, model)
    monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", raising=False)
    if ack is None:
        monkeypatch.delenv(_ACK_VAR, raising=False)
    else:
        monkeypatch.setenv(_ACK_VAR, ack)
    monkeypatch.setattr(sys, "argv", ["roadmap_curate", *argv])

    return module.main(), ran["value"]


# ── the fixture must not drift away from the machine it describes ────────────


def test_the_captured_drop_in_still_matches_the_live_one() -> None:
    """A hand-copied fixture drifts in silence, exactly like a hand-pinned constant.

    Skips where the live file cannot exist — CI runs on a hosted runner with no
    operator home — so this is a server-side drift alarm, not a portability
    requirement.
    """
    if not LIVE_DROP_IN.exists():
        pytest.skip(f"{LIVE_DROP_IN} absent — not the server, nothing to compare")

    assert FIXTURE.read_bytes() == LIVE_DROP_IN.read_bytes(), (
        f"{FIXTURE.name} no longer matches {LIVE_DROP_IN}. The configuration this "
        "test reasons about has changed; re-capture the fixture and re-read the "
        "conclusions below rather than deleting this assertion."
    )


def test_the_fixture_declares_a_roadmap_primary_at_all() -> None:
    """Guard on the reader: a parser that returned nothing would make the rest vacuous."""
    assert configured_roadmap_primary()


def test_the_comment_block_is_not_mistaken_for_a_setting() -> None:
    """This drop-in's prose names the variable it sets, twice.

    A substring search would find `BRAIN_DREAM_ROADMAP_DRY_RUN` in a warning and
    report it as configured here, which it is not — it lives in
    `killswitches.conf`. The reader must parse, not grep.
    """
    parsed = _drop_in_environment(FIXTURE.read_text(encoding="utf-8"))

    assert set(parsed) == {_PRIMARY_VAR, "BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL"}


# ── the configured primary, walked to its consequence ────────────────────────


def wet_regime_models() -> frozenset[str]:
    """The models the WET regime uses by default — the SOURCE of the allowlist.

    Read here instead of `AUTO_APPLY_MODELS`, and the difference is not cosmetic.
    `AUTO_APPLY_MODELS` is the list the guard consults; branching on it would let
    this test decide what to expect by asking the very thing it verifies, so
    emptying that list would flip the branch instead of failing the test —
    measured, on 2026-09-03, on the first version of this file, where the
    neutralised guard left all seven tests green. Learning 187f107c, met head on.
    """
    return frozenset({module.DEFAULT_WET_ROADMAP_MODEL, module.DEFAULT_WET_ROADMAP_FALLBACK_MODEL})


def test_the_allowlist_is_still_derived_from_the_wet_defaults() -> None:
    """What makes the branch above trustworthy, asserted rather than assumed.

    If someone redefines `AUTO_APPLY_MODELS` as a literal, the two sources stop
    being one and this test's independence becomes a fiction. It would then be
    THIS assertion that reddens, and it names the reason.
    """
    assert module.AUTO_APPLY_MODELS == wet_regime_models()


def test_the_configured_primary_is_covered_whichever_side_it_sits_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: start from the configuration, end at the outcome.

    Green on today's trapped-but-guarded configuration, green again the day the
    operator swaps the primary for a review-only model, and red only if a
    configuration that CAN auto-apply reaches a wet night unrefused.
    """
    primary = configured_roadmap_primary()

    rc, ran = _invoke(monkeypatch, argv=["--wet"], model=primary)
    printed = "".join(capsys.readouterr())

    if primary in wet_regime_models():
        assert not ran, (
            f"the configured ROADMAP primary {primary} can auto-apply and a wet night "
            "started anyway — this is the trap of ticket 7511c210, unguarded"
        )
        assert primary in printed, "the refusal must name the model that caused it"
        assert _ACK_VAR in printed, "the refusal must name the word that lifts it"
    else:
        assert ran, (
            f"the configured primary {primary} is review-only, so `main` should "
            "downgrade the wet run rather than stop the night"
        )
        assert rc == 0


def test_an_operator_who_means_it_can_still_run_wet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must block an accident, never a decision that was made.

    Runs the ack path on the CONFIGURED primary rather than a chosen one, so the
    escape hatch is proven for the model actually in place.
    """
    rc, ran = _invoke(monkeypatch, argv=["--wet"], model=configured_roadmap_primary(), ack="yes")

    assert (rc, ran) == (0, True)


def test_todays_dry_night_says_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The nominal path, on the real primary. A guard that shouts nightly is muted."""
    rc, ran = _invoke(monkeypatch, argv=[], model=configured_roadmap_primary())

    assert (rc, ran) == (0, True)
    assert _ACK_VAR not in "".join(capsys.readouterr())


# ── the flag that would arm it, read where it is actually interpreted ────────


def test_only_an_explicit_false_arms_the_wet_flag() -> None:
    """CONTRACT REPLACED on 2026-09-04, and the replacement is the point.

    This assertion used to pin `!= "true"` and read it as a finding: the ticket
    speaks of "the first DRY_RUN=false", while the shell armed `--wet` on
    `False`, `0`, a typo or an EMPTY value just as well. The trap did not need a
    decision to spring.

    That finding is now fixed rather than merely recorded. `dream.sh` routes the
    four killswitches through `dream_wants_wet`, which returns wet ONLY for an
    explicit `false` and falls back to DRY loudly otherwise. What this test pins
    is therefore the reversal, and the semantics themselves are EXECUTED under
    bash by `test_dream_sh_wet_flag_is_fail_closed.py` — text can show the
    spelling, only bash can show what `0` does.
    """
    dream_sh = (Path(__file__).resolve().parents[2] / "scripts" / "dream.sh").read_text(
        encoding="utf-8"
    )

    # SUPERSEDED on 2026-09-10, and by removal rather than by a better guard.
    #
    # This used to pin that the roadmap wet flag was routed through
    # `dream_wants_wet`, so only an explicit `false` could arm it. ADR 45671595
    # took the phase off the nightly rail entirely: dream.sh no longer reads
    # BRAIN_DREAM_ROADMAP_ENABLED or _DRY_RUN at all, so no unattended path is
    # left that could arm auto-apply on the configured primary.
    #
    # That is a STRONGER statement than the one it replaces, and it is the one
    # worth pinning: ticket 7511c210's trap needed a nightly invocation to
    # spring. The CLI survives for the operator, who is a human reading a report
    # before applying — which is what the ticket asked for. If a nightly
    # invocation ever comes back, this fails, and whoever brings it back has to
    # answer the auto-apply question again.
    assert "BRAIN_DREAM_ROADMAP" not in dream_sh
