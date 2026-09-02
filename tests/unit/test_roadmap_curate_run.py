"""Unit tests for scripts.roadmap_curate._run orchestration (mocked, no DB/LLM).

Night of 2026-07-05: SIGTERM at 20 m in the middle of batch 7/10 — the terminal
apply never ran (24 proposals stuck 'proposed', 0 applied) and record_dream_run
wrote no row. Contracts tested here:
- apply PER BATCH (a SIGTERM loses only the batch in flight);
- night budget: no new batch after NIGHT_BUDGET_S, a clean end (record_dream_run
  written, rc=0, the rotation will serve the projects again);
- merges held back by the judge: persisted 'proposed', NEVER auto-applied.
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from scripts import roadmap_curate as rc
from scripts.roadmap_curate import (
    BatchOutcome,
    CurationDraft,
    FeatureCard,
    PersistResult,
    ProjectBatch,
)

from brain_v42.dream_degradation import DEGRADED_PREFIX


def _mk_batch(key: str) -> ProjectBatch:
    return ProjectBatch(
        project_key=key,
        features=[
            FeatureCard(id=uuid4(), name=f"{key}-A", status="research", pinned=False),
            FeatureCard(id=uuid4(), name=f"{key}-B", status="research", pinned=False),
        ],
    )


def _archive_draft(batch: ProjectBatch) -> CurationDraft:
    return CurationDraft(op="archive", feature_id=batch.features[0].id, payload={}, rationale="r")


def _merge_draft(batch: ProjectBatch) -> CurationDraft:
    return CurationDraft(
        op="merge",
        feature_id=batch.features[0].id,
        payload={"into": str(batch.features[1].id)},
        rationale="r",
    )


def _args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "limit": 10,
        "wet": True,
        "apply_ids": None,
        "model": "test-model",
        "base_url": "https://mock.nvidia.local/v1",
        "budget_seconds": rc.NIGHT_BUDGET_S,
    }
    base.update(over)
    return argparse.Namespace(**base)


class _Clock:
    """An injectable clock: returns the list's values then repeats the last."""

    def __init__(self, values: list[float]):
        self._values = list(values)

    def __call__(self) -> float:
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


@pytest.fixture
def run_mocks(monkeypatch):
    monkeypatch.setenv("POSTGRES_URL", "postgresql+asyncpg://b:b@localhost:5433/test")
    monkeypatch.setattr(
        "brain_v42.db.engine.get_session_factory", lambda: MagicMock(), raising=True
    )
    mocks = {
        "record": AsyncMock(),
        "apply": AsyncMock(return_value=1),
        "judge": AsyncMock(return_value=set()),
    }
    monkeypatch.setattr(rc, "record_dream_run", mocks["record"])
    monkeypatch.setattr(rc, "apply_proposals", mocks["apply"])
    monkeypatch.setattr(rc, "judge_merges", mocks["judge"])
    return mocks


class TestFairShareWindow:
    @pytest.mark.asyncio
    async def test_curate_batch_receives_fair_share_window(self, run_mocks, monkeypatch):
        """Each batch gets a fair-share LLM window of the remaining budget — a
        large project can no longer eat the next ones' share (night of
        2026-07-10: red 383 s → budget exhausted at the 5th project, 5
        deferred)."""
        batches = [_mk_batch("p1"), _mk_batch("p2"), _mk_batch("p3")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        windows: list[float] = []

        async def fake_curate(client, model, batch, **kw):
            windows.append(kw["llm_timeout_s"])
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[101], [102], [103]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )
        # t0=0; pre-batch checks: 0, 400, 600; then the final duration.
        clock = _Clock([0.0, 0.0, 400.0, 600.0, 700.0])

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=clock,
        )

        assert rcode == 0
        # b1 : (720-0)/3 = 240 → /2 = 120 ; b2 : (720-400)/2 = 160 → 80 ;
        # b3 : (720-600)/1 = 120 → 60 (= plancher MIN_LLM_WINDOW_S).
        assert windows == [120.0, 80.0, 60.0]


class TestBudgetGuard:
    @pytest.mark.asyncio
    async def test_budget_stops_new_batches_cleanly(self, run_mocks, monkeypatch, capsys):
        """After NIGHT_BUDGET_S, NO new batch at all: a clean end,
        record_dream_run written (status done), rc=0 — not a failure."""
        batches = [_mk_batch("p1"), _mk_batch("p2"), _mk_batch("p3")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, batch, **kw):
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[101], [102], [103]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )
        # t0=0; pre-batch checks: 0 (b1 passes), 400 (b2 passes), 800 (> 720 →
        # stop); then the final duration.
        clock = _Clock([0.0, 0.0, 400.0, 800.0, 810.0])

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=clock,
        )

        assert rcode == 0
        assert run_mocks["apply"].await_count == 2  # b1 et b2 seulement
        run_mocks["record"].assert_awaited_once()
        assert run_mocks["record"].await_args.kwargs.get("status", "done") == "done" or (
            "done" in run_mocks["record"].await_args.args
        )
        out = capsys.readouterr().out
        assert "budget" in out.lower()
        assert "1 projet" in out  # p3 not processed, announced — no silent drop

    @pytest.mark.asyncio
    async def test_apply_runs_per_batch_not_terminally(self, run_mocks, monkeypatch):
        """The wet apply runs AFTER EACH batch with THAT batch's ids — no more
        terminal apply (SIGTERM of 2026-07-05: 24 proposals never applied)."""
        batches = [_mk_batch("p1"), _mk_batch("p2")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, batch, **kw):
            return BatchOutcome(batch=batch, drafts=[_archive_draft(batch)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        inserted = iter([[11], [22]])
        monkeypatch.setattr(
            rc,
            "persist_proposals",
            AsyncMock(side_effect=lambda sf, d: PersistResult(inserted=next(inserted))),
        )

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        ids_per_call = [c.args[1] for c in run_mocks["apply"].await_args_list]
        assert ids_per_call == [[11], [22]]


class TestJudgeGateInRun:
    @pytest.mark.asyncio
    async def test_missing_model_provenance_is_never_auto_applied(self, run_mocks, monkeypatch):
        batch = _mk_batch("p1")
        draft = _archive_draft(batch)
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[draft], model_used=None)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=PersistResult(inserted=[10]))
        )

        rcode = await rc._run(
            _args(wet=True),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["apply"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreviewed_fallback_is_persisted_but_never_auto_applied(
        self, run_mocks, monkeypatch
    ):
        """Any fallback outside the allowlist stays review-only even in a wet run."""
        batch = _mk_batch("p1")
        draft = _merge_draft(batch)
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[draft],
                model_used="unreviewed-fallback",
                fallback_used=True,
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        persist = AsyncMock(return_value=PersistResult(inserted=[9]))
        monkeypatch.setattr(rc, "persist_proposals", persist)

        rcode = await rc._run(
            _args(wet=True),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        assert persist.await_args.args[1] == [draft]
        run_mocks["judge"].assert_not_awaited()
        run_mocks["apply"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_held_merges_persisted_but_never_applied(self, run_mocks, monkeypatch, capsys):
        """A merge held back by the judge → persisted 'proposed' (morning review),
        excluded from the apply ids. The same batch's non-merge status applies."""
        batch = _mk_batch("p1")
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))
        merge = _merge_draft(batch)
        keep = CurationDraft(
            op="status",
            feature_id=batch.features[1].id,
            payload={"status": "deployed"},
            rationale="r",
        )

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[merge, keep],
                model_used=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        run_mocks["judge"].return_value = {0}  # the single merge is held back
        persist_calls: list[list[CurationDraft]] = []
        results = iter([PersistResult(inserted=[7]), PersistResult(inserted=[8])])

        async def fake_persist(sf, drafts):
            persist_calls.append(list(drafts))
            return next(results) if drafts else PersistResult()

        monkeypatch.setattr(rc, "persist_proposals", fake_persist)

        rcode = await rc._run(
            _args(),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        # The judge saw ONLY the merges.
        judged = run_mocks["judge"].await_args.args[3]
        assert judged == [merge]
        assert run_mocks["judge"].await_args.args[1] == rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        # Persist 1: applicable drafts (without the merge); persist 2: held back.
        assert persist_calls[0] == [keep]
        assert persist_calls[1] == [merge]
        # Apply: ids from the applicable persist only — never the held-back one.
        ids_applied = [c.args[1] for c in run_mocks["apply"].await_args_list]
        assert ids_applied == [[7]]
        assert "retenu" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_dry_mode_skips_judge_and_apply(self, run_mocks, monkeypatch):
        batch = _mk_batch("p1")
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[_merge_draft(b)], model_used=model)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=PersistResult(inserted=[5]))
        )

        rcode = await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["judge"].assert_not_awaited()
        run_mocks["apply"].assert_not_awaited()


class TestFallbackDegradationIsReported:
    """A night served entirely by the fallback must be visible without reading raw logs.

    2026-08-05: ten consecutive nights at 100 % 8B fallback, all `done`, all
    `8/8 phases OK`. The run stays `done` — making it `fail` would repeat the error
    4480d3df has just corrected — but it stops being mute.
    """

    @pytest.mark.asyncio
    async def test_full_fallback_run_prints_degradation_and_cause(
        self, run_mocks, monkeypatch, capsys
    ):
        batches = [_mk_batch("p1"), _mk_batch("p2")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[],
                model_used=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
                primary_error=f"{rc.MODEL_GONE_MARKER} — HTTP 410 end of life",
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        rcode = await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        out = capsys.readouterr().out
        assert rcode == 0
        assert "2/2" in out
        assert rc.MODEL_GONE_MARKER in out
        assert rc.DEFAULT_ROADMAP_MODEL in out

    @pytest.mark.asyncio
    async def test_nominal_run_stays_silent_about_fallback(self, run_mocks, monkeypatch, capsys):
        """No noise when the primary serves: the alarm must stay rare."""
        batches = [_mk_batch("p1")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[], model_used=model, fallback_used=False)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert "secours" not in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_dream_run_records_the_model_actually_used(self, run_mocks, monkeypatch):
        """dream_runs.model was NULL for roadmap: the table that feeds the briefing
        did not know which model had run."""
        batches = [_mk_batch("p1")]
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=batches))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(
                batch=b,
                drafts=[],
                model_used=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                fallback_used=True,
                primary_error="primaire mort",
            )

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(rc, "persist_proposals", AsyncMock(return_value=PersistResult()))

        await rc._run(
            _args(wet=False),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        kwargs = run_mocks["record"].await_args.kwargs
        assert kwargs["status"] == "done"
        assert kwargs["model"] == rc.DEFAULT_ROADMAP_FALLBACK_MODEL


class TestBudgetSecondsArg:
    @pytest.fixture
    def capture_args(self, monkeypatch):
        seen: dict[str, Any] = {}

        async def fake_run(args, api_key, model, base_url, **kw):
            seen["args"] = args
            seen["model"] = model
            seen["fallback_model"] = kw.get("fallback_model")
            return 0

        monkeypatch.setattr(rc, "_run", fake_run)
        monkeypatch.setattr(rc, "load_env_file", lambda p: None)
        monkeypatch.setenv("BRAIN_NVIDIA_API_KEY", "k")
        return seen

    def test_default_is_night_budget(self, capture_args, monkeypatch):
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--limit", "3"])
        assert rc.main() == 0
        assert capture_args["args"].budget_seconds == rc.NIGHT_BUDGET_S

    def test_explicit_budget_seconds(self, capture_args, monkeypatch):
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--budget-seconds", "300"])
        assert rc.main() == 0
        assert capture_args["args"].budget_seconds == 300.0

    def test_default_model_is_the_live_canaryed_primary(self, capture_args, monkeypatch):
        """Third primary in three weeks: each one died at the provider.

        qwen3-next-80b reached its EOL on 2026-07-27, replaced by
        deepseek-v4-flash after the canary of 2026-08-05 (3/3 valid, 16.6 s/batch).
        deepseek-v4-flash died in its turn on 2026-08-07, discovered on 08-16.

        Replaced by mistral-nemotron on TWO measurements, because the first nearly
        picked the wrong candidate:

        - Speed and shape: 3/3 valid, 12-20 s/batch, i.e. 126-204 s over the ten
          projects against a 720 s budget. The dated snapshot of the dead family,
          deepseek-v4-flash-0731, is also 3/3 valid but at 69.3 s — 693 s, 96 % of
          the budget, and FOUR TIMES slower than the alias it replaces. A dated pin
          does not inherit its alias's profile.

        - Content quality, which overturned the ranking. The proposal count ranks
          nothing: over three runs of the same batches, mistral-nemotron returned 31
          then 21, gpt-oss-20b 29 then 13, and the 8B 28/30/29. Blind judgement of
          the content: mistral-nemotron 48/100, gpt-oss-20b 35, llama-3.1-8b 10.

        The 8B fallback stays a fallback. It is the FASTEST and the WORST on
        substance — 9 empty rationales, 2 merges towards a target it archives in the
        same batch, and seven orchestrator runs melted into the oldest of them.
        Promoting it would also have collapsed the chain to a single link (see
        tests/unit/test_roadmap_model_chain.py).

        FALLBACK REPLACED on 2026-08-29: the 8B reached its end of life on
        2026-08-26 (410 measured by the probe AND by the nights of the 27th and
        28th, both in fail). Replacement: openai/gpt-oss-20b, re-measured IN ITS
        EXACT REGIME after correcting the instrument (60 s night windows, ten real
        batches, FALLBACK_* fallback caps): 10/10 carried, 12 proposals,
        7.8 s/batch — and 35/100 in blind judgement against 10/100 for the dead one.
        The fallback profile is not "reduced": same 3 features, tokens DOUBLED
        (1024), which avoids the reasoning truncation that cost 74.5 s/batch under
        the old instrument at 512 tokens. Discarded in the same regime: nano-30b
        (9.9 s/batch but 5/10 valid JSON); deepseek-v4-flash-0731 (69.3 s/batch on
        08-16, a family dead twice in one month, content never judged).

        The assertions compare against the CONSTANTS: this test proves the ROUTING
        (the default reaches curate), not the model's identity — the shape of the
        chain lives in test_roadmap_model_chain, the history of the choice in the
        constants' comment.
        """
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.delenv("BRAIN_NVIDIA_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_ROADMAP_MODEL
        assert capture_args["fallback_model"] == rc.DEFAULT_ROADMAP_FALLBACK_MODEL

    def test_dry_primary_can_never_auto_apply(self, capture_args, monkeypatch):
        """The DRY primary must stay out of the allowlist: a model not canaried for
        wet must never become applicable through a mere swap."""
        assert rc.DEFAULT_ROADMAP_MODEL not in rc.AUTO_APPLY_MODELS
        assert rc.DEFAULT_ROADMAP_MODEL in rc.PROPOSER_ONLY_MODELS

    def test_legacy_global_model_does_not_override_roadmap_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_MODEL", "legacy-global")
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_ROADMAP_MODEL

    def test_roadmap_model_env_overrides_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "roadmap-reviewed")
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["model"] == "roadmap-reviewed"

    def test_a_fallback_equal_to_the_primary_warns_about_the_one_link_chain(
        self, capture_args, monkeypatch, capsys
    ):
        """curate_batch treats fallback==primary as NO fallback, silently.

        The case only arises through an env override (the constants are kept
        distinct by test_roadmap_model_chain) — and it is the configuration
        deploy/nvidia.env.example carried as an example for weeks. A one-link chain
        that believes it has two must make noise.
        """
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", rc.DEFAULT_ROADMAP_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert "UN seul maillon" in capsys.readouterr().out

    def test_fallback_model_env_overrides_default(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_FALLBACK_MODEL", "fallback-model")
        monkeypatch.setattr("sys.argv", ["roadmap_curate"])

        assert rc.main() == 0

        assert capture_args["fallback_model"] == "fallback-model"

    def test_cli_model_overrides_roadmap_env(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "roadmap-fast")
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--model", "cli-model"])

        assert rc.main() == 0

        assert capture_args["model"] == "cli-model"

    def test_default_wet_model_is_reviewed_and_keeps_auto_apply(self, capture_args, monkeypatch):
        """WET PAIR REPLACED on 2026-08-29: llama-3.3-70b died with a 410.

        End of life measured between the nights of the 27th (extract done) and the
        28th (extract fail 410) — a DORMANT link on the roadmap side, since the
        phase runs in DRY. Yesterday's fallback becomes primary and gpt-oss-120b
        takes the fallback post: LIVE links, not a pair ready to arm. Measured on
        2026-08-29 in the real WET regime — the unmanaged path HALVES the window as
        soon as a fallback exists (30 s at ten projects; the bound is NOT the 180 s
        httpx read-timeout): the pair as ordered does not carry (super-120b 1/10, 9
        saved by the fallback). Re-arming WET requires a canary under 30 s first —
        the constants' comment carries the full precondition.
        """
        monkeypatch.delenv("BRAIN_NVIDIA_ROADMAP_MODEL", raising=False)
        monkeypatch.delenv("BRAIN_NVIDIA_MODEL", raising=False)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["model"] == rc.DEFAULT_WET_ROADMAP_MODEL
        assert capture_args["fallback_model"] == rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        assert capture_args["args"].wet is True

    def test_explicit_fallback_model_forces_proposer_only(self, capture_args, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", rc.DEFAULT_ROADMAP_FALLBACK_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is False
        assert "proposer-only" in capsys.readouterr().out

    def test_unreviewed_model_forces_review_only(self, capture_args, monkeypatch, capsys):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", "unknown-large-model")
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is False
        assert "review-only" in capsys.readouterr().out

    def test_reviewed_fallback_model_keeps_wet(self, capture_args, monkeypatch):
        monkeypatch.setenv("BRAIN_NVIDIA_ROADMAP_MODEL", rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL)
        monkeypatch.setattr("sys.argv", ["roadmap_curate", "--wet"])

        assert rc.main() == 0

        assert capture_args["args"].wet is True
        assert capture_args["fallback_model"] == rc.DEFAULT_WET_ROADMAP_MODEL


class TestDegradationNotice:
    """The degradation prefix is a CONTRACT between two processes.

    `scripts/roadmap_curate.py` writes it into `dream_runs.error_message`; the
    briefing (`DreamRunService`) reads it back to refuse counting the night as
    clean. Nothing holds them in agreement but that prefix, and there is no
    backfill: a divergence makes the past rows mute.
    """

    def test_the_degraded_prefix_literal_is_frozen(self):
        """Four nights already in the database depend on it — 08-06, 08-08, 08-09, 08-10.

        They carry the ACCENTED prefix. De-accenting or translating it would not
        merely orphan those rows: it would make them invisible to the reader
        without any write failing.
        """
        assert DEGRADED_PREFIX == "DÉGRADÉ"

    def test_the_notice_is_built_from_the_shared_prefix(self, monkeypatch):
        """Proves the USAGE, not the import.

        An `rc.DEGRADED_PREFIX is DEGRADED_PREFIX` would stay true with the literal
        re-inlined into the f-string: it would prove that an import exists, never
        that it is used. Only substitution bites.
        """
        monkeypatch.setattr(rc, "DEGRADED_PREFIX", "ZZZTEST")

        notice = rc._degradation_notice("primaire-mort", 10, 10, ["410 Gone"])

        assert notice is not None
        assert notice.startswith("ZZZTEST")

    def test_a_nominal_run_says_nothing(self):
        """The nominal case is MUTE: no batch served by the fallback."""
        assert rc._degradation_notice("primaire-vivant", 0, 10, []) is None


class TestReviewKeepsEveryOpTheWetGaveUp:
    """Narrowing the unattended path must not narrow the reviewed one (2026-09-02).

    Until that day the two were indistinguishable in effect: `allowed_ops=None`
    and `WET_APPLYABLE_OPS` admitted the same four ops, so nothing tested told
    them apart and either could have been passed anywhere. Bounding the wet to
    archive/status makes the difference load-bearing for the first time — and it
    is the difference that keeps the 140 pending `merge`/`rename` proposals
    applicable at all, instead of stranding them.
    """

    @pytest.mark.asyncio
    async def test_the_reviewed_apply_ids_path_allows_every_op(self, run_mocks, monkeypatch):
        rcode = await rc._run(
            _args(wet=False, apply_ids="10,11", project_key="brain-v42"),
            api_key="k",
            model=rc.DEFAULT_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["apply"].assert_awaited_once()
        assert run_mocks["apply"].await_args.kwargs["allowed_ops"] is None, (
            "--apply-ids is the HUMAN review: bounding it to the wet scope would "
            "make merge and rename unapplyable by anyone"
        )

    @pytest.mark.asyncio
    async def test_the_nightly_wet_path_is_bounded_to_the_wet_scope(self, run_mocks, monkeypatch):
        batch = _mk_batch("p1")
        draft = _archive_draft(batch)
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[batch]))

        async def fake_curate(client, model, b, **kw):
            return BatchOutcome(batch=b, drafts=[draft], model_used=rc.DEFAULT_WET_ROADMAP_MODEL)

        monkeypatch.setattr(rc, "curate_batch", fake_curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=PersistResult(inserted=[10]))
        )

        rcode = await rc._run(
            _args(wet=True),
            api_key="k",
            model=rc.DEFAULT_WET_ROADMAP_MODEL,
            base_url="https://mock.nvidia.local/v1",
            clock=_Clock([0.0]),
        )

        assert rcode == 0
        run_mocks["apply"].assert_awaited_once()
        passed = run_mocks["apply"].await_args.kwargs["allowed_ops"]
        assert set(passed) == {"archive", "status"}
        assert "merge" not in passed and "rename" not in passed
