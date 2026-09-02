"""Unit tests for scripts.roadmap_curate pure functions (no DB, no network)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
import scripts.roadmap_curate as rc
from scripts.roadmap_curate import (
    _SYSTEM_PROMPT,
    LLM_ATTEMPT_TIMEOUT_S,
    MAX_COMPLETION_TOKENS,
    MIN_LLM_WINDOW_S,
    PROPOSABLE_STATUSES,
    VALID_OPS,
    WET_APPLYABLE_OPS,
    BatchOutcome,
    CurationDraft,
    FeatureCard,
    ProjectBatch,
    ResponseParseError,
    batch_allowance,
    batch_llm_window,
    build_messages,
    curate_batch,
    drop_noops,
    fetch_project_batches,
    format_digest,
    parse_and_validate,
    render_batch,
    rotate_keys,
)
from sqlalchemy.ext.asyncio import AsyncSession

_F1 = uuid4()
_F2 = uuid4()
_PINNED = uuid4()


def _batch() -> ProjectBatch:
    return ProjectBatch(
        project_key="brain-v42",
        features=[
            FeatureCard(
                id=_F1,
                name="Recherche hybride",
                status="research",
                pinned=False,
                artifacts=["2026-07-01 [decision] RRF retenu"],
            ),
            FeatureCard(
                id=_F2,
                name="recherche hybride v2",
                status="research",
                pinned=False,
                artifacts=[],
            ),
            FeatureCard(
                id=_PINNED,
                name="Feature épinglée",
                status="building",
                pinned=True,
                artifacts=["2026-06-30 [plan] Plan X (plan done)"],
            ),
        ],
    )


def _batch_n(n: int, artifacts_per_feature: int = 0) -> ProjectBatch:
    """A batch of n distinct features — for the progressive-shrink tests."""
    return ProjectBatch(
        project_key="big",
        features=[
            FeatureCard(
                id=uuid4(),
                name=f"F{i}",
                status="research",
                pinned=False,
                artifacts=[f"F{i}-artifact-{j}" for j in range(artifacts_per_feature)],
            )
            for i in range(n)
        ],
    )


def _item(op: str, fid, payload: dict) -> str:
    import json

    return json.dumps([{"op": op, "feature_id": str(fid), "payload": payload, "rationale": "r"}])


class TestRenderAndBuild:
    def test_render_batch_contains_ids_names_statuses(self):
        text = render_batch(_batch())
        assert str(_F1) in text and str(_F2) in text
        assert "Recherche hybride" in text
        assert "research" in text
        assert "PINNED" in text  # marker on the pinned feature
        assert "RRF retenu" in text

    def test_build_messages_has_system_and_user(self):
        msgs = build_messages(_batch())
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "JSON" in msgs[0]["content"]

    def test_format_digest(self):
        from datetime import UTC, datetime

        d = format_digest("decision", "RRF retenu", datetime(2026, 7, 1, tzinfo=UTC), None)
        assert d == "2026-07-01 [decision] RRF retenu"

    def test_format_digest_plan_status(self):
        from datetime import UTC, datetime

        d = format_digest("plan", "Plan X", datetime(2026, 6, 30, tzinfo=UTC), "done")
        assert d == "2026-06-30 [plan] Plan X (plan done)"


class TestParseAndValidate:
    def test_all_four_ops_valid(self):
        assert set(VALID_OPS) == {"merge", "archive", "status", "rename"}
        assert parse_and_validate(_item("archive", _F1, {}), _batch())[0].op == "archive"
        assert parse_and_validate(_item("merge", _F2, {"into": str(_F1)}), _batch())[0].payload[
            "into"
        ] == str(_F1)
        assert (
            parse_and_validate(_item("status", _F1, {"status": "building"}), _batch())[0].payload[
                "status"
            ]
            == "building"
        )
        assert (
            parse_and_validate(
                _item("rename", _F2, {"name": "Recherche hybride (fusion)"}), _batch()
            )[0].payload["name"]
            == "Recherche hybride (fusion)"
        )

    def test_empty_array_valid(self):
        assert parse_and_validate("[]", _batch()) == []

    def test_fences_stripped(self):
        assert parse_and_validate("```json\n[]\n```", _batch()) == []

    def test_invalid_json_raises(self):
        with pytest.raises(ResponseParseError):
            parse_and_validate("pas du json", _batch())

    def test_unknown_op_rejected(self):
        with pytest.raises(ResponseParseError, match="op"):
            parse_and_validate(_item("delete", _F1, {}), _batch())

    def test_feature_outside_batch_rejected(self):
        with pytest.raises(ResponseParseError, match="not in batch"):
            parse_and_validate(_item("archive", uuid4(), {}), _batch())

    def test_merge_target_outside_batch_rejected(self):
        with pytest.raises(ResponseParseError, match="not in batch"):
            parse_and_validate(_item("merge", _F1, {"into": str(uuid4())}), _batch())

    def test_merge_into_self_rejected(self):
        with pytest.raises(ResponseParseError, match="equals"):
            parse_and_validate(_item("merge", _F1, {"into": str(_F1)}), _batch())

    def test_pinned_only_status_allowed(self):
        with pytest.raises(ResponseParseError, match="pinned"):
            parse_and_validate(_item("archive", _PINNED, {}), _batch())
        # status on pinned: OK
        drafts = parse_and_validate(_item("status", _PINNED, {"status": "deployed"}), _batch())
        assert drafts[0].feature_id == _PINNED

    def test_status_archived_rejected_use_archive_op(self):
        assert "archived" not in PROPOSABLE_STATUSES
        with pytest.raises(ResponseParseError, match="status"):
            parse_and_validate(_item("status", _F1, {"status": "archived"}), _batch())

    def test_rename_empty_rejected_and_truncated_200(self):
        with pytest.raises(ResponseParseError, match="name"):
            parse_and_validate(_item("rename", _F1, {"name": "  "}), _batch())
        drafts = parse_and_validate(_item("rename", _F1, {"name": "x" * 300}), _batch())
        assert len(drafts[0].payload["name"]) == 200

    def test_wet_applyable_ops_includes_all_ops(self):
        """Aggressive regime of the 2026-07-04 evening (Armand's decision: the
        roadmap is little consumed, Claude validates at the morning check): wet
        ALSO applies merge/rename — replaces the "archive/status ONLY" pin of
        rollout §4."""
        assert set(WET_APPLYABLE_OPS) == set(VALID_OPS)

    def test_system_prompt_pins_aggressive_grouping(self):
        """The prompt is a consolidator (thematic merges), no longer "conservative",
        and carries the anti-chain instruction aligned with parse_and_validate's
        guard."""
        assert "conservateur" not in _SYSTEM_PROMPT
        assert "regroup" in _SYSTEM_PROMPT.lower()
        assert "chaîne" in _SYSTEM_PROMPT

    def test_system_prompt_pins_anti_dump_rules(self):
        """Night of 2026-07-05: 10/23 aberrant merges — a "dump everything into one
        survivor" pattern (distinct technical gotchas merged into a neighbouring
        workstream, distinct plans/cycles merged into each other). Embedding
        similarity and per-target counting do NOT discriminate (measured over 62
        applied merges) — the guardrail is the prompt + judge."""
        assert "rangement" in _SYSTEM_PROMPT
        assert "même sujet" in _SYSTEM_PROMPT.lower()
        assert "proximité" in _SYSTEM_PROMPT

    def test_system_prompt_limits_one_proposal_per_feature(self):
        assert "une seule proposition par feature" in _SYSTEM_PROMPT.lower()

    def test_multiple_ops_for_same_feature_are_rejected(self):
        content = json.dumps(
            [
                {
                    "op": "status",
                    "feature_id": str(_F1),
                    "payload": {"status": "building"},
                    "rationale": "r1",
                },
                {
                    "op": "rename",
                    "feature_id": str(_F1),
                    "payload": {"name": "Nouveau nom"},
                    "rationale": "r2",
                },
            ]
        )

        with pytest.raises(ResponseParseError, match="une seule proposition"):
            parse_and_validate(content, _batch())


class TestMergeChainGuards:
    """An aggressive prompt ⇒ multiple thematic merges — never chained: applying
    A→B then B→C would fail A's artifacts onto an archived B."""

    def _batch3(self):
        a, b, c = uuid4(), uuid4(), uuid4()
        features = [
            FeatureCard(id=f, name=f"F{i}", status="research", pinned=False)
            for i, f in enumerate((a, b, c))
        ]
        return a, b, c, ProjectBatch(project_key="p", features=features)

    def _merge(self, loser, into):
        return {
            "op": "merge",
            "feature_id": str(loser),
            "payload": {"into": str(into)},
            "rationale": "r",
        }

    def test_merge_chain_rejected(self):
        a, b, c, batch = self._batch3()
        content = json.dumps([self._merge(a, b), self._merge(b, c)])
        with pytest.raises(ResponseParseError, match="chaîne"):
            parse_and_validate(content, batch)

    def test_same_loser_merged_twice_rejected(self):
        a, b, c, batch = self._batch3()
        content = json.dumps([self._merge(a, b), self._merge(a, c)])
        with pytest.raises(ResponseParseError, match="une seule proposition"):
            parse_and_validate(content, batch)

    def test_multiple_losers_into_same_survivor_ok(self):
        a, b, c, batch = self._batch3()
        content = json.dumps([self._merge(a, c), self._merge(b, c)])
        drafts = parse_and_validate(content, batch)
        assert len(drafts) == 2
        assert all(d.payload["into"] == str(c) for d in drafts)


class TestCurateBatchErrorCapture:
    @pytest.mark.asyncio
    async def test_full_batch_uses_extended_max_tokens(self) -> None:
        """The consolidating prompt produces long answers — the brain-v42 batch (30
        features) was truncated at 4096 tokens on the first wet run (2026-07-04,
        char 12160); the curator asks for MAX_COMPLETION_TOKENS."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["max_tokens"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch_n(30))

        assert not outcome.failed
        assert MAX_COMPLETION_TOKENS == 8192
        assert seen == [MAX_COMPLETION_TOKENS]

    @pytest.mark.asyncio
    async def test_small_batch_uses_economic_completion_budget(self) -> None:
        """A shrink to 3 features does not reserve the full batch's 8192 tokens."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["max_tokens"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch())

        assert not outcome.failed
        assert seen == [2048]

    @pytest.mark.asyncio
    async def test_medium_batch_uses_balanced_completion_budget(self) -> None:
        """The economical batch of 10 keeps enough headroom without reserving 8k."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["max_tokens"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch_n(10))

        assert not outcome.failed
        assert seen == [4096]

    @pytest.mark.asyncio
    async def test_default_big_model_uses_compact_profile(self) -> None:
        batch = _batch_n(30, artifacts_per_feature=5)
        sizes: list[int] = []
        token_caps: list[int] = []
        prompts: list[str] = []
        models: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            user_prompt = payload["messages"][1]["content"]
            prompts.append(user_prompt)
            models.append(payload["model"])
            sizes.append(sum(str(feature.id) in user_prompt for feature in batch.features))
            token_caps.append(payload["max_tokens"])
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                batch,
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=200.0,
            )

        assert not outcome.failed
        assert outcome.shrunk is True
        assert outcome.model_used == rc.DEFAULT_ROADMAP_MODEL
        assert outcome.fallback_used is False
        assert models == [rc.DEFAULT_ROADMAP_MODEL]
        assert sizes == [3]
        assert token_caps == [512]
        assert "F0-artifact-2" in prompts[0]
        assert "F0-artifact-3" not in prompts[0]

    def test_compact_batch_does_not_mutate_source(self) -> None:
        batch = _batch_n(8, artifacts_per_feature=5)

        compact = rc._compact_batch(batch, feature_cap=5, artifact_cap=3)

        assert len(compact.features) == 5
        assert all(len(feature.artifacts) == 3 for feature in compact.features)
        assert len(batch.features) == 8
        assert all(len(feature.artifacts) == 5 for feature in batch.features)

    @pytest.mark.asyncio
    async def test_corrective_reprompt_keeps_economic_completion_budget(self) -> None:
        """An invalid JSON answer must not redo a call at 8192 tokens."""
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content)["max_tokens"])
            content = "pas du JSON" if len(seen) == 1 else "[]"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch())

        assert not outcome.failed
        assert seen == [2048, 2048]

    @pytest.mark.asyncio
    async def test_corrective_reprompt_includes_validation_error(self) -> None:
        corrections: list[str] = []
        invalid_status = json.dumps(
            [
                {
                    "op": "status",
                    "feature_id": str(_F1),
                    "payload": {"status": "archived"},
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            messages = json.loads(request.content)["messages"]
            if len(messages) == 2:
                content = invalid_status
            else:
                corrections.append(messages[-1]["content"])
                content = "[]"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch())

        assert not outcome.failed
        assert len(corrections) == 1
        assert "invalid status 'archived'" in corrections[0]

    @pytest.mark.asyncio
    async def test_transport_error_names_exception_type(self) -> None:
        """Empty str() on transport errors → a named outcome.error (learning 7144c5ae)."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch())

        assert outcome.failed
        assert outcome.error
        assert "ConnectError" in outcome.error


class TestManagedModelChain:
    @pytest.mark.asyncio
    async def test_proposer_only_duplicates_keep_first_proposal(self) -> None:
        batch = _batch_n(3)
        content = json.dumps(
            [
                {
                    "op": "status",
                    "feature_id": str(batch.features[0].id),
                    "payload": {"status": "building"},
                },
                {
                    "op": "rename",
                    "feature_id": str(batch.features[0].id),
                    "payload": {"name": "Doublon"},
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, rc.DEFAULT_ROADMAP_MODEL, batch)

        assert not outcome.failed
        assert len(outcome.drafts) == 1
        assert outcome.drafts[0].op == "status"
        assert outcome.drafts[0].payload == {"status": "building"}

    @pytest.mark.asyncio
    async def test_proposer_only_drops_self_merge_without_reprompt(self) -> None:
        batch = _batch_n(3)
        calls = 0
        self_merge = json.dumps(
            [
                {
                    "op": "merge",
                    "feature_id": str(batch.features[0].id),
                    "payload": {"into": str(batch.features[0].id)},
                    "rationale": "mauvaise proposition",
                }
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": self_merge}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, rc.DEFAULT_ROADMAP_MODEL, batch)

        assert not outcome.failed
        assert outcome.drafts == []
        assert calls == 1

    @pytest.mark.asyncio
    async def test_proposer_only_keeps_valid_items_beside_invalid_ones(self) -> None:
        batch = _batch_n(3)
        content = json.dumps(
            [
                {
                    "op": "merge",
                    "feature_id": str(batch.features[0].id),
                    "payload": {"into": str(batch.features[0].id)},
                },
                {
                    "op": "status",
                    "feature_id": str(batch.features[1].id),
                    "payload": {"status": "building"},
                    "rationale": "valide",
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, rc.DEFAULT_ROADMAP_MODEL, batch)

        assert not outcome.failed
        assert len(outcome.drafts) == 1
        assert outcome.drafts[0].feature_id == batch.features[1].id
        assert outcome.drafts[0].payload == {"status": "building"}

    @pytest.mark.asyncio
    async def test_fallback_timeout_retries_once_with_smaller_batch(self) -> None:
        import asyncio

        batch = _batch_n(8)
        calls: list[dict] = []
        fallback_calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fallback_calls
            payload = json.loads(request.content)
            calls.append(payload)
            if payload["model"] == rc.DEFAULT_ROADMAP_MODEL:
                await asyncio.sleep(0.2)
            else:
                fallback_calls += 1
                if fallback_calls == 1:
                    await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                batch,
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=0.03,
            )

        assert not outcome.failed
        assert outcome.fallback_used is True
        assert [call["model"] for call in calls] == [
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
        ]
        retry_prompt = calls[-1]["messages"][1]["content"]
        assert sum(str(feature.id) in retry_prompt for feature in batch.features) == 2

    @pytest.mark.asyncio
    async def test_primary_timeout_opens_circuit_for_remaining_batches(self) -> None:
        import asyncio

        calls: list[str] = []
        disabled_models: set[str] = set()

        async def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == rc.DEFAULT_ROADMAP_MODEL:
                await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            first = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=0.03,
                disabled_models=disabled_models,
            )
            second = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=0.03,
                disabled_models=disabled_models,
            )

        assert not first.failed
        assert not second.failed
        assert first.fallback_used is True
        assert second.fallback_used is True
        assert disabled_models == {rc.DEFAULT_ROADMAP_MODEL}
        assert calls == [
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
        ]

    @pytest.mark.asyncio
    async def test_primary_timeout_falls_back_to_compact_fallback(self) -> None:
        import asyncio

        batch = _batch_n(12, artifacts_per_feature=5)
        calls: list[dict] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            calls.append(payload)
            if payload["model"] == rc.DEFAULT_ROADMAP_MODEL:
                await asyncio.sleep(0.2)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                batch,
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=0.03,
            )

        assert not outcome.failed
        assert outcome.model_used == rc.DEFAULT_ROADMAP_FALLBACK_MODEL
        assert outcome.fallback_used is True
        assert [call["model"] for call in calls] == [
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
        ]
        fallback_prompt = calls[-1]["messages"][1]["content"]
        assert sum(str(feature.id) in fallback_prompt for feature in batch.features) == 3
        assert calls[-1]["max_tokens"] == 1024
        assert "F0-artifact-2" in fallback_prompt
        assert "F0-artifact-3" not in fallback_prompt

    @pytest.mark.asyncio
    async def test_primary_503_retries_then_falls_back(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == rc.DEFAULT_ROADMAP_MODEL:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async def no_sleep(_seconds: float) -> None:
            return None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                sleep=no_sleep,
            )

        assert not outcome.failed
        assert calls == [rc.DEFAULT_ROADMAP_MODEL] * 3 + [rc.DEFAULT_ROADMAP_FALLBACK_MODEL]
        assert outcome.fallback_used is True

    @pytest.mark.asyncio
    async def test_primary_invalid_json_twice_falls_back(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            content = "pas du json" if model == rc.DEFAULT_ROADMAP_MODEL else "[]"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            )

        assert not outcome.failed
        assert calls == [
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
        ]
        assert outcome.fallback_used is True

    @pytest.mark.asyncio
    async def test_primary_malformed_success_envelope_falls_back(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == rc.DEFAULT_ROADMAP_MODEL:
                return httpx.Response(200, json={"choices": []})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            )

        assert not outcome.failed
        assert calls == [
            rc.DEFAULT_ROADMAP_MODEL,
            rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
        ]
        assert outcome.fallback_used is True

    @pytest.mark.asyncio
    async def test_auth_error_never_calls_fallback(self) -> None:
        from scripts.domain_backfill import NvidiaAuthError

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["model"])
            return httpx.Response(401, text="invalid key")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            with pytest.raises(NvidiaAuthError):
                await curate_batch(
                    client,
                    rc.DEFAULT_ROADMAP_MODEL,
                    _batch_n(8),
                    fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                )

        assert calls == [rc.DEFAULT_ROADMAP_MODEL]

    @pytest.mark.asyncio
    async def test_both_models_invalid_reports_both(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "pas du json"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            )

        assert outcome.failed
        assert rc.DEFAULT_ROADMAP_MODEL in outcome.error
        assert rc.DEFAULT_ROADMAP_FALLBACK_MODEL in outcome.error


class TestPrimaryFailureIsNeverSwallowed:
    """A fallback that succeeds must not erase the primary's failure.

    Discovered on 2026-08-05: qwen 80B died with a 410 on 2026-07-27 and curation
    ran ten nights on the 8B fallback without a single run reporting it. `errors`
    was only surfaced if the WHOLE chain failed.
    """

    @pytest.mark.asyncio
    async def test_primary_error_survives_a_successful_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["model"] == rc.DEFAULT_ROADMAP_MODEL:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            )

        assert not outcome.failed
        assert outcome.fallback_used is True
        assert outcome.primary_error is not None
        assert rc.DEFAULT_ROADMAP_MODEL in outcome.primary_error

    @pytest.mark.asyncio
    async def test_model_gone_is_flagged_permanent_not_transient(self) -> None:
        """410 Gone (provider EOL) ≠ 503 busy: no retry will repair it."""

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["model"] == rc.DEFAULT_ROADMAP_MODEL:
                return httpx.Response(
                    410,
                    json={"detail": "has reached its end of life on 2026-07-27"},
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
            )

        assert not outcome.failed
        assert outcome.fallback_used is True
        assert outcome.primary_error is not None
        assert rc.MODEL_GONE_MARKER in outcome.primary_error

    @pytest.mark.asyncio
    async def test_open_circuit_still_reports_why_the_primary_is_skipped(self) -> None:
        """Batches 2..N skip the primary without calling it — the pattern must survive."""

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["model"] == rc.DEFAULT_ROADMAP_MODEL:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        disabled_models: set[str] = set()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                disabled_models=disabled_models,
            )
            second = await curate_batch(
                client,
                rc.DEFAULT_ROADMAP_MODEL,
                _batch_n(8),
                fallback_model=rc.DEFAULT_ROADMAP_FALLBACK_MODEL,
                disabled_models=disabled_models,
            )

        assert second.fallback_used is True
        assert second.primary_error is not None
        assert rc.DEFAULT_ROADMAP_MODEL in second.primary_error


class TestReviewedModelFallback:
    @pytest.mark.asyncio
    async def test_candidates_share_the_total_reviewed_window(self, monkeypatch) -> None:
        timeouts: list[float] = []

        class RecordingTimeout:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        def record_timeout(seconds: float):
            timeouts.append(seconds)
            return RecordingTimeout()

        async def fail_attempt(client, model, batch, sleep, **kwargs):
            return BatchOutcome(
                batch=batch,
                drafts=[],
                failed=True,
                error="synthetic failure",
                model_used=model,
            )

        monkeypatch.setattr(rc.asyncio, "timeout", record_timeout)
        monkeypatch.setattr(rc, "_curate_llm_attempt", fail_attempt)

        async with httpx.AsyncClient(base_url="https://mock.nvidia.local/v1") as client:
            outcome = await curate_batch(
                client,
                rc.DEFAULT_WET_ROADMAP_MODEL,
                _batch(),
                fallback_model=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                llm_timeout_s=200.0,
            )

        assert outcome.failed
        assert timeouts == [100.0, 100.0]

    @pytest.mark.asyncio
    async def test_transport_failure_falls_back_and_opens_circuit(self) -> None:
        calls: list[str] = []
        disabled_models: set[str] = set()

        def handler(request: httpx.Request) -> httpx.Response:
            model = json.loads(request.content)["model"]
            calls.append(model)
            if model == rc.DEFAULT_WET_ROADMAP_MODEL:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        async def no_sleep(_seconds: float) -> None:
            return None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            first = await curate_batch(
                client,
                rc.DEFAULT_WET_ROADMAP_MODEL,
                _batch(),
                fallback_model=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                disabled_models=disabled_models,
                sleep=no_sleep,
            )
            second = await curate_batch(
                client,
                rc.DEFAULT_WET_ROADMAP_MODEL,
                _batch(),
                fallback_model=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                disabled_models=disabled_models,
                sleep=no_sleep,
            )

        assert not first.failed
        assert not second.failed
        assert first.model_used == rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL
        assert first.fallback_used is True
        assert second.fallback_used is True
        assert disabled_models == {rc.DEFAULT_WET_ROADMAP_MODEL}
        assert calls == [
            rc.DEFAULT_WET_ROADMAP_MODEL,
            rc.DEFAULT_WET_ROADMAP_MODEL,
            rc.DEFAULT_WET_ROADMAP_MODEL,
            rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
            rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
        ]

    @pytest.mark.asyncio
    async def test_auth_failure_never_calls_reviewed_fallback(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["model"])
            return httpx.Response(401, text="invalid key")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            with pytest.raises(rc.NvidiaAuthError):
                await curate_batch(
                    client,
                    rc.DEFAULT_WET_ROADMAP_MODEL,
                    _batch(),
                    fallback_model=rc.DEFAULT_WET_ROADMAP_FALLBACK_MODEL,
                )

        assert calls == [rc.DEFAULT_WET_ROADMAP_MODEL]


class TestCurateBatchShrinkRetry:
    """Night of 2026-07-05: red (30 features, 8k-token generation) burned ~9 min in
    ReadTimeout×3 on the SAME payload — retrying identically is useless when
    generation exceeds the read-timeout. The retry must SHRINK the batch (half the
    features) under a per-attempt asyncio cap — without touching _post_chat (impact
    CRITICAL cross-scripts)."""

    def _slow_then_ok_handler(self, calls: list[dict]):
        async def handler(request: httpx.Request) -> httpx.Response:
            import asyncio

            payload = json.loads(request.content)
            calls.append(payload)
            if len(calls) == 1:
                await asyncio.sleep(0.5)  # > llm_timeout_s du test
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "[]"}}], "usage": {}},
            )

        return handler

    @pytest.mark.asyncio
    async def test_llm_timeout_shrinks_batch_and_succeeds(self) -> None:
        calls: list[dict] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(self._slow_then_ok_handler(calls)),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch(), llm_timeout_s=0.05)

        assert not outcome.failed
        assert outcome.shrunk is True
        assert len(calls) == 2
        # The 2nd call carries only half the features (3 → 1).
        user1 = calls[0]["messages"][1]["content"]
        user2 = calls[1]["messages"][1]["content"]
        ids_in = lambda text: sum(str(f.id) in text for f in _batch().features)  # noqa: E731
        assert ids_in(user1) == 3
        assert ids_in(user2) == 1

    @pytest.mark.asyncio
    async def test_llm_timeout_after_shrink_fails_batch(self) -> None:
        async def always_slow(request: httpx.Request) -> httpx.Response:
            import asyncio

            await asyncio.sleep(0.5)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(always_slow),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch(), llm_timeout_s=0.05)

        assert outcome.failed
        assert outcome.error is not None
        assert "timeout" in outcome.error.lower()

    @pytest.mark.asyncio
    async def test_single_feature_batch_cannot_shrink(self) -> None:
        """A one-feature batch cannot shrink — direct failure, a single call."""
        calls: list[dict] = []

        async def slow(request: httpx.Request) -> httpx.Response:
            import asyncio

            calls.append(json.loads(request.content))
            await asyncio.sleep(0.5)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}}
            )

        single = ProjectBatch(
            project_key="p",
            features=[FeatureCard(id=_F1, name="A", status="research", pinned=False)],
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", single, llm_timeout_s=0.05)

        assert outcome.failed
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_tight_window_uses_two_viable_small_attempts(self, monkeypatch) -> None:
        """Regression of 2026-07-13: 60 s then 30 s+30 s made 30→3 fail.

        In a tight window, ROADMAP must spare the provider: start at 10 features
        then give the full window to the shrink at 3. The test scales 60 s down to
        60 ms and simulates an NVIDIA answer in 40 ms.
        """
        monkeypatch.setattr(rc, "MIN_LLM_WINDOW_S", 0.06)
        batch = _batch_n(30)
        calls: list[dict] = []

        async def full_too_slow_small_ok(request: httpx.Request) -> httpx.Response:
            import asyncio

            calls.append(json.loads(request.content))
            await asyncio.sleep(0.2 if len(calls) == 1 else 0.04)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(full_too_slow_small_ok),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", batch, llm_timeout_s=0.06)

        sizes = [
            sum(str(f.id) in c["messages"][1]["content"] for f in batch.features) for c in calls
        ]
        assert not outcome.failed
        assert outcome.shrunk is True
        assert sizes == [10, 3]

    def test_tight_window_first_size_is_monotone_up_to_economic_cap(self, monkeypatch) -> None:
        """Going from 8 to 9 features must not reduce the first attempt to 3."""
        monkeypatch.setattr(rc, "MIN_LLM_WINDOW_S", 60.0)

        assert rc._llm_attempt_schedule(8, 60.0) == [(8, 60.0), (2, 60.0)]
        assert rc._llm_attempt_schedule(9, 60.0) == [(9, 60.0), (3, 60.0)]
        assert rc._llm_attempt_schedule(10, 60.0) == [(10, 60.0), (3, 60.0)]

    @pytest.mark.asyncio
    async def test_multi_step_shrink_lands_smaller_batch(self, monkeypatch) -> None:
        """Nights of 2026-07-06 (red-shrik) / 2026-07-07 (brain-v42): the full 30 AND
        the ÷2 shrink to 15 both timed out → the old shrink-once-by-half failed the
        phase. Progressive shrink retries in smaller and smaller steps: here full(9)
        and the 1st step(3) time out, but the 2nd step(1) passes → success (not a
        failure), and each slice is ÷3 smaller."""
        monkeypatch.setattr(rc, "MIN_LLM_WINDOW_S", 0.01)
        batch = _batch_n(9)
        calls: list[dict] = []

        async def slow_twice_then_ok(request: httpx.Request) -> httpx.Response:
            import asyncio

            calls.append(json.loads(request.content))
            if len(calls) <= 2:  # full(9) + palier(3) timeout ; palier(1) OK
                await asyncio.sleep(0.5)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_twice_then_ok),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", batch, llm_timeout_s=0.05)

        assert not outcome.failed
        assert outcome.shrunk is True
        sizes = [
            sum(str(f.id) in c["messages"][1]["content"] for f in batch.features) for c in calls
        ]
        assert sizes == [9, 3, 1]  # shrink progressif ÷3 : 9 → 3 → 1

    @pytest.mark.asyncio
    async def test_multi_step_shrink_bounded_by_max_retries(self, monkeypatch) -> None:
        """A wholly stuck NIM night does NOT blow up the budget: at most full +
        SHRINK_MAX_RETRIES steps, which share a single LLM_ATTEMPT_TIMEOUT_S window
        (total ≈ 2×timeout, the 20 m SIGTERM margin preserved)."""
        from scripts.roadmap_curate import SHRINK_MAX_RETRIES

        monkeypatch.setattr(rc, "MIN_LLM_WINDOW_S", 0.005)

        calls: list[dict] = []

        async def always_slow(request: httpx.Request) -> httpx.Response:
            import asyncio

            calls.append(json.loads(request.content))
            await asyncio.sleep(0.5)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "[]"}}], "usage": {}}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(always_slow),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await curate_batch(client, "test-model", _batch_n(30), llm_timeout_s=0.02)

        assert outcome.failed
        assert "timeout" in outcome.error.lower()
        assert len(calls) == 1 + SHRINK_MAX_RETRIES  # full(30) + 2 paliers (10, 3)


class TestJudgeMerges:
    """Two-tier anti-dump gate (night of 2026-07-05: 10/23 aberrant merges). An LLM
    judge validates only "same subject" merges; the rejected ones stay 'proposed'
    for the morning review. FAIL-CLOSED: error/timeout/missing index → the merge is
    held back (never auto-applied on the judge's silence)."""

    def _merges(self, batch: ProjectBatch) -> list[CurationDraft]:
        return [
            CurationDraft(
                op="merge",
                feature_id=batch.features[0].id,
                payload={"into": str(batch.features[2].id)},
                rationale="r0",
            ),
            CurationDraft(
                op="merge",
                feature_id=batch.features[1].id,
                payload={"into": str(batch.features[2].id)},
                rationale="r1",
            ),
        ]

    @pytest.mark.asyncio
    async def test_flagged_and_missing_indices_are_held(self) -> None:
        from scripts.roadmap_curate import judge_merges

        batch = _batch()

        def handler(request: httpx.Request) -> httpx.Response:
            # i=0 validated; i=1 ABSENT from the answer → fail-closed.
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": '[{"i": 0, "same_subject": true}]'}}],
                    "usage": {},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            held = await judge_merges(client, "test-model", batch, self._merges(batch))

        assert held == {1}

    @pytest.mark.asyncio
    async def test_same_subject_false_is_held(self) -> None:
        from scripts.roadmap_curate import judge_merges

        batch = _batch()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '[{"i": 0, "same_subject": false},'
                                ' {"i": 1, "same_subject": true}]'
                            }
                        }
                    ],
                    "usage": {},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            held = await judge_merges(client, "test-model", batch, self._merges(batch))

        assert held == {0}

    @pytest.mark.asyncio
    async def test_transport_error_holds_all(self) -> None:
        from scripts.roadmap_curate import judge_merges

        batch = _batch()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            held = await judge_merges(client, "test-model", batch, self._merges(batch))

        assert held == {0, 1}

    @pytest.mark.asyncio
    async def test_empty_merges_makes_no_call(self) -> None:
        from scripts.roadmap_curate import judge_merges

        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            held = await judge_merges(client, "test-model", _batch(), [])

        assert held == set()
        assert calls == []

    @pytest.mark.asyncio
    async def test_judge_prompt_names_source_and_target(self) -> None:
        from scripts.roadmap_curate import judge_merges

        batch = _batch()
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '[{"i": 0, "same_subject": true},'
                                ' {"i": 1, "same_subject": true}]'
                            }
                        }
                    ],
                    "usage": {},
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            await judge_merges(client, "test-model", batch, self._merges(batch))

        system = seen[0]["messages"][0]["content"]
        user = seen[0]["messages"][1]["content"]
        assert "sujet" in system.lower()
        assert "Recherche hybride" in user  # source i=0
        assert "Feature épinglée" in user  # shared target


class TestDropNoops:
    def _batch_one(self, *, name="Feature A", status="research", pinned=False):
        fid = UUID("11111111-1111-1111-1111-111111111111")
        return fid, ProjectBatch(
            project_key="p",
            features=[FeatureCard(id=fid, name=name, status=status, pinned=pinned)],
        )

    def test_status_identical_is_dropped(self):
        fid, batch = self._batch_one(status="deployed")
        drafts = [
            CurationDraft(
                op="status", feature_id=fid, payload={"status": "deployed"}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_status_different_is_kept(self):
        fid, batch = self._batch_one(status="research")
        drafts = [
            CurationDraft(op="status", feature_id=fid, payload={"status": "done"}, rationale="r")
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_rename_identical_modulo_whitespace_is_dropped(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(
                op="rename", feature_id=fid, payload={"name": "  Feature A "}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_rename_different_is_kept(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(op="rename", feature_id=fid, payload={"name": "Feature B"}, rationale="r")
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_archive_and_merge_never_noop(self):
        fid, batch = self._batch_one()
        drafts = [
            CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r"),
            CurationDraft(op="merge", feature_id=fid, payload={"into": str(fid)}, rationale="r"),
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 2 and dropped == []


class TestRotateKeys:
    def test_window_advances_by_limit_each_day(self):
        keys = [f"p{i:02d}" for i in range(26)]
        day0 = rotate_keys(keys, 10, day_ordinal=0)
        day1 = rotate_keys(keys, 10, day_ordinal=1)
        day2 = rotate_keys(keys, 10, day_ordinal=2)
        assert day0 == keys[0:10]
        assert day1 == keys[10:20]
        assert day2 == keys[20:26] + keys[0:4]  # wrap

    def test_full_cycle_covers_every_project(self):
        keys = [f"p{i:02d}" for i in range(26)]
        seen: set[str] = set()
        for day in range(3):  # ceil(26/10) = 3 nuits
            seen.update(rotate_keys(keys, 10, day_ordinal=day))
        assert seen == set(keys)

    def test_fewer_projects_than_limit_returns_all(self):
        keys = ["a", "b", "c"]
        assert sorted(rotate_keys(keys, 10, day_ordinal=5)) == keys
        assert len(rotate_keys(keys, 10, day_ordinal=5)) == 3

    def test_empty_keys(self):
        assert rotate_keys([], 10, day_ordinal=3) == []

    def test_deterministic_same_day(self):
        keys = [f"p{i}" for i in range(26)]
        assert rotate_keys(keys, 10, 7) == rotate_keys(keys, 10, 7)


class TestFetchRotationWiring:
    @staticmethod
    def _mapped_result(rows):
        result = MagicMock()
        result.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
        return result

    @pytest.mark.asyncio
    async def test_fetch_queries_only_rotated_window(self):
        """fetch_project_batches only queries the projects of the rotated window."""
        keys_result = MagicMock()
        keys_result.all = MagicMock(return_value=[("a",), ("b",), ("c",)])
        empty_features = MagicMock()
        empty_features.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[keys_result, empty_features, empty_features])

        @asynccontextmanager
        async def factory():
            yield session

        batches = await fetch_project_batches(factory, limit=2, day_ordinal=1)
        assert batches == []  # empty features → batches skipped
        # offset = (1*2) % 3 = 2 → rotated window = ['c', 'a']
        feature_calls = session.execute.await_args_list[1:]
        assert [call.args[1]["pk"] for call in feature_calls] == ["c", "a"]

    @pytest.mark.asyncio
    async def test_fetch_rotates_features_for_tight_window_coverage(self):
        """The order advances by the fallback of 3 to serve new cards."""
        rows = [
            {
                "id": uuid4(),
                "name": f"F{i}",
                "status": "research",
                "pinned": False,
            }
            for i in range(30)
        ]
        keys_result = MagicMock()
        keys_result.all = MagicMock(return_value=[("p",)])
        features_result = MagicMock()
        features_result.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=rows))
        )
        artifacts_result = MagicMock()
        artifacts_result.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[keys_result, features_result, artifacts_result])

        @asynccontextmanager
        async def factory():
            yield session

        batches = await fetch_project_batches(factory, limit=1, day_ordinal=1)

        assert [card.name for card in batches[0].features[:10]] == [f"F{i}" for i in range(3, 13)]

    @pytest.mark.asyncio
    async def test_feature_rotation_advances_between_project_cycles(self):
        """Even if only the fallback of 3 passes, ten cycles cover the 30 cards."""
        keys = [(f"p{i:02d}",) for i in range(30)]
        rows = [
            {
                "id": uuid4(),
                "name": f"F{i}",
                "status": "research",
                "pinned": False,
            }
            for i in range(30)
        ]

        async def fetch_p00(day_ordinal: int) -> list[str]:
            keys_result = MagicMock()
            keys_result.all = MagicMock(return_value=keys)

            async def execute(_stmt, params=None):
                if params is None:
                    return keys_result
                if "fids" in params:
                    return self._mapped_result([])
                return self._mapped_result(rows if params["pk"] == "p00" else [])

            session = MagicMock(spec=AsyncSession)
            session.execute = AsyncMock(side_effect=execute)

            @asynccontextmanager
            async def factory():
                yield session

            batches = await fetch_project_batches(factory, limit=10, day_ordinal=day_ordinal)
            return [card.name for card in batches[0].features[:10]]

        chunks = [(await fetch_p00(day))[:3] for day in range(0, 30, 3)]

        assert chunks == [[f"F{i}" for i in range(start, start + 3)] for start in range(0, 30, 3)]


class TestBatchAllowance:
    def test_even_split(self):
        assert batch_allowance(40, 10) == 4

    def test_ceil_redistributes(self):
        assert batch_allowance(38, 9) == 5  # ceil — unconsumed slots are redistributed

    def test_last_batch_gets_all_remaining(self):
        assert batch_allowance(7, 1) == 7

    def test_exhausted_cap(self):
        assert batch_allowance(0, 5) == 0


class TestBatchLlmWindow:
    """Per-project TIME fair-share (sister of batch_allowance, for the cap).

    Night of 2026-07-10: the red project ate 383 s (full window + shrink) → the
    720 s budget was exhausted at the 5th project, 5 projects deferred to the
    rotation. A batch's LLM window becomes min(full, max(floor, share/2)) — share =
    remaining budget / remaining batches; /2 because a batch consumes ≈ 2 windows
    (the full attempt + the shared shrink steps).
    """

    def test_plenty_of_budget_gives_full_window(self):
        # share 720/1 = 720 → /2 = 360, capped at the full window
        assert batch_llm_window(720.0, 0.0, 1) == LLM_ATTEMPT_TIMEOUT_S

    def test_fair_share_caps_big_project(self):
        # share 720/3 = 240 → window 120: a large project no longer eats everything
        assert batch_llm_window(720.0, 0.0, 3) == 120.0

    def test_slack_rolls_forward_to_later_batches(self):
        # fast batches leave budget behind: share (720-400)/2 = 160 → 80
        assert batch_llm_window(720.0, 400.0, 2) == 80.0

    def test_floor_keeps_tight_windows_viable(self):
        # share (720-700)/5 = 4 → /2 = 2 → floor: a normal project (~40 s of LLM
        # call) stays servable; _run's budget hard-break remains the stop
        # guardrail
        assert batch_llm_window(720.0, 700.0, 5) == MIN_LLM_WINDOW_S

    def test_budget_overrun_returns_floor(self):
        # elapsed > budget: a null share → the floor (it is _run that breaks)
        assert batch_llm_window(720.0, 900.0, 2) == MIN_LLM_WINDOW_S

    def test_no_remaining_batches_defensive_full_window(self):
        assert batch_llm_window(720.0, 0.0, 0) == LLM_ATTEMPT_TIMEOUT_S

    def test_no_remaining_batches(self):
        assert batch_allowance(10, 0) == 0


class TestRunProposeLoop:
    """_run's propose flow — monkeypatched collaborators, no real I/O."""

    def _args(self, limit=10, wet=False):
        return SimpleNamespace(limit=limit, wet=wet, apply_ids=None, model=None, base_url=None)

    def _feature(self, fid):
        return FeatureCard(id=fid, name="F", status="research", pinned=False)

    def _outcome(self, batch, fid):
        draft = CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r")
        return BatchOutcome(batch=batch, drafts=[draft])

    def _hermetic(self, monkeypatch):
        monkeypatch.setattr("brain_v42.config.Settings", MagicMock())
        monkeypatch.setattr(
            "brain_v42.db.engine.get_session_factory", MagicMock(return_value=MagicMock())
        )
        import scripts.roadmap_curate as rc

        monkeypatch.setattr(rc, "record_dream_run", AsyncMock())
        return rc

    @pytest.mark.asyncio
    async def test_persist_called_per_batch_and_progress_flushed(self, monkeypatch, capsys):
        """Incremental persist: one persist PER batch + an [i/N] line per batch."""
        rc = self._hermetic(monkeypatch)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        monkeypatch.setattr(
            rc,
            "curate_batch",
            AsyncMock(side_effect=[self._outcome(b1, fid1), self._outcome(b2, fid2)]),
        )
        persist = AsyncMock(
            side_effect=[rc.PersistResult(inserted=[1]), rc.PersistResult(inserted=[2])]
        )
        monkeypatch.setattr(rc, "persist_proposals", persist)

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert persist.await_count == 2  # incremental — the old design persisted once
        out = capsys.readouterr().out
        assert "[1/2] p1:" in out and "[2/2] p2:" in out

    @pytest.mark.asyncio
    async def test_cap_exhausted_skips_remaining_llm_calls(self, monkeypatch, capsys):
        """Cap exhausted → break BEFORE the next LLM call, explicit message."""
        rc = self._hermetic(monkeypatch)
        monkeypatch.setattr(rc, "MAX_PROPOSALS_PER_NIGHT", 1)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        curate = AsyncMock(return_value=self._outcome(b1, fid1))
        monkeypatch.setattr(rc, "curate_batch", curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=rc.PersistResult(inserted=[1]))
        )

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert curate.await_count == 1  # batch 2 is never sent to the LLM
        assert "épuisé" in capsys.readouterr().out
