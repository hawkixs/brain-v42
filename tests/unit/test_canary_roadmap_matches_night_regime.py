"""The canary must measure the regime the NIGHT imposes, not a regime of its own.

On 2026-08-16 the roadmap primary was switched to `mistralai/mistral-nemotron` on
the strength of two canaries: 3/3 valid batches, 12.6 to 20.4 s/batch. The first
real night refuted it — TimeoutError, open circuit, 10/10 batches falling back to
the 8B fallback. The proof did not predict production.

The canary called `_curate_llm_attempt`, the layer BELOW the one the night uses.
It was therefore missing four constraints at once, not one:

  1. the time bound — the night wraps each attempt in
     `asyncio.timeout(batch_llm_window(...))`, i.e. 60 s at ten batches; the
     canary only let the httpx read timeout run, 180 s;
  2. the completion cap (`BIG_MODEL_COMPLETION_TOKENS`);
  3. the batch compaction (`_compact_batch`);
  4. the CIRCUIT — a single primary failure removes it from every subsequent
     batch of the run.

The fourth is the one that explains "10/10 on the fallback": one lost batch
condemns the other nine. A canary that measures each batch in isolation cannot
structurally observe it, and a canary that lets the fallback catch up without
saying so returns a green for a dead primary — the exact failure of qwen 80B,
dead on 2026-07-27 and discovered on 08-05 after ten green nights.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from scripts.canary_roadmap_model import measure_model
from scripts.roadmap_curate import (
    LLM_ATTEMPT_TIMEOUT_S,
    NIGHT_BUDGET_S,
    BatchOutcome,
    FeatureCard,
    ProjectBatch,
    batch_llm_window,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_cli_default_batch_count_is_the_nights() -> None:
    """The CLI DEFAULT must measure the night's regime, not one 2× gentler.

    Measured on 2026-08-29 (PR 42 review): `--batches 3` gave windows of 120 s
    growing up to 200 s, where the night at `--limit 10` bounds each attempt to
    60 s. Under that regime the canary validated a fallback at 74.5 s/batch that
    would have timed out every one of its attempts in production — the exact
    repetition of the 2026-08-17 failure this instrument was meant to fix. The
    default is pinned on the `--limit` dream.sh ACTUALLY passes, not on a
    retyped 10: if the night changes width, this test forces the canary to
    follow.
    """
    from scripts.canary_roadmap_model import DEFAULT_CANARY_BATCHES

    dream_sh = (_REPO_ROOT / "scripts" / "dream.sh").read_text(encoding="utf-8")
    limit_match = re.search(r"roadmap_args=\(--limit (\d+)\)", dream_sh)

    assert limit_match, "dream.sh ne déclare plus roadmap_args=(--limit N)"
    assert DEFAULT_CANARY_BATCHES == int(limit_match.group(1))


_PRIMARY = "mistralai/mistral-nemotron"
_FALLBACK = "meta/llama-3.1-8b-instruct"


def _batches(count: int) -> list[ProjectBatch]:
    return [
        ProjectBatch(
            project_key=f"projet-{index}",
            features=[FeatureCard(id=uuid4(), name=f"F{index}", status="building", pinned=False)],
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_each_batch_is_bounded_by_the_night_window_not_the_default() -> None:
    """The window must be the night's, derived from the remaining budget."""
    windows: list[float] = []

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        windows.append(llm_timeout_s)
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    batches = _batches(10)
    await measure_model(
        None,
        _PRIMARY,
        batches,
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert len(windows) == 10
    assert windows[0] == pytest.approx(60.0), (
        "à dix batches la nuit borne la première tentative à 60 s; le canary "
        f"laissait courir le read timeout httpx, 180 s: {windows[0]}"
    )
    # The contract is not a constant but the night's FUNCTION: a batch's share
    # depends on the remaining budget and the number of remaining batches. The
    # clock is frozen in this test, so `elapsed` stays null and the last batches
    # inherit a large share — it is `batch_llm_window` that decides, and that is
    # exactly what we want to pin.
    assert windows == [batch_llm_window(NIGHT_BUDGET_S, 0.0, 10 - index) for index in range(10)], (
        windows
    )
    assert all(window <= LLM_ATTEMPT_TIMEOUT_S for window in windows), windows


@pytest.mark.asyncio
async def test_a_primary_killed_on_the_first_batch_is_never_blessed_by_its_fallback() -> None:
    """The circuit is shared by the whole run, and the verdict names the primary.

    The fallback may return ten perfectly valid batches: the measured candidate
    carried none of them. A canary that counts "10/10 valid" here is the one that
    switched production onto a dead model.
    """

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        # Reproduces curate_batch: the primary dies once then stays out, and the
        # fallback serves every subsequent batch.
        if model in disabled_models:
            return BatchOutcome(
                batch=batch,
                drafts=[],
                model_used=fallback_model,
                fallback_used=True,
                primary_error=f"{model}: écarté (circuit ouvert plus tôt dans ce run)",
            )
        disabled_models.add(model)
        return BatchOutcome(
            batch=batch,
            drafts=[],
            model_used=fallback_model,
            fallback_used=True,
            primary_error=f"{model}: TimeoutError",
        )

    measurement = await measure_model(
        None,
        _PRIMARY,
        _batches(10),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert measurement.carried_by_primary == 0, (
        "le candidat n'a porté aucun batch; le compter valide, c'est publier "
        "le secours sous le nom du primaire"
    )
    assert measurement.rescued_by_fallback == 10
    assert measurement.circuit_opened_at == 1, (
        "le circuit doit s'ouvrir au premier batch et rester ouvert: "
        f"{measurement.circuit_opened_at}"
    )
    assert measurement.verdict == "MORT", measurement.verdict


@pytest.mark.asyncio
async def test_a_primary_that_carries_every_batch_is_alive() -> None:
    """Counter-proof: without a fallback, the verdict stays alive."""

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    measurement = await measure_model(
        None,
        _PRIMARY,
        _batches(3),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
    )

    assert measurement.carried_by_primary == 3
    assert measurement.rescued_by_fallback == 0
    assert measurement.circuit_opened_at is None
    assert measurement.verdict == "OK"


@pytest.mark.asyncio
async def test_a_candidate_is_measured_in_the_regime_it_would_have_once_adopted() -> None:
    """`PROPOSER_ONLY_MODELS` is DERIVED from `DEFAULT_ROADMAP_MODEL`.

    A candidate evaluated for the DRY primary slot would therefore enter it the day
    it is adopted, and change routing at the same instant: proposer-only parser,
    caps and retries of the `_curate_managed_model_chain` path. Measuring it
    outside that set is measuring a regime it will no longer have — the exact
    mistake this file corrects, repeated one notch further out.
    """
    import scripts.roadmap_curate as rc

    seen: list[bool] = []

    async def curate(
        client: Any,
        model: str,
        batch: ProjectBatch,
        sleep: Any,
        *,
        llm_timeout_s: float,
        fallback_model: str | None,
        disabled_models: set[str],
        proposer_only: bool | None = None,
    ) -> BatchOutcome:
        seen.append(proposer_only is True)
        return BatchOutcome(batch=batch, drafts=[], model_used=model)

    candidate = "openai/gpt-oss-120b"
    assert candidate not in rc.PROPOSER_ONLY_MODELS

    await measure_model(
        None,
        candidate,
        _batches(2),
        fallback_model=_FALLBACK,
        curate=curate,
        clock=lambda: 0.0,
        as_dry_primary=True,
    )

    assert seen == [True, True], (
        "le candidat doit être routé comme le primaire DRY qu'il deviendrait"
    )
    # And the global is never mutated: a measurement does not reroute production.
    assert candidate not in rc.PROPOSER_ONLY_MODELS
