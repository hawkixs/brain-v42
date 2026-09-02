"""Unit tests for scripts.ticket_extract pure functions (no DB, no network)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from scripts.ticket_extract import (
    CorpusDedupUnavailable,
    CorpusDuplicate,
    DedupResult,
    ProposalDraft,
    ResponseParseError,
    ThreadOutcome,
    TicketThread,
    _extract_thread_with_budget,
    _run,
    _safe_error,
    apply_proposals,
    build_messages,
    deduplicate_drafts,
    extract_thread,
    parse_and_validate,
    persist_proposals,
    render_thread,
)
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs


async def _no_sleep(_seconds: float) -> None:
    """Neutralize the backoff: a test must never wait on a clock."""


def _thread(**kw) -> TicketThread:
    defaults: dict = {
        "id": uuid4(),
        "kind": "request",
        "title": "pourquoi camelCase",
        "body": "le endpoint /api/signals renvoie du camelCase ?",
        "from_project": "red-shrik",
        "to_project": "red-data",
        "status": "closed",
        "messages": [
            (
                "red-data",
                "c'est le middleware de sérialisation, voulu",
                "resolved",
                datetime(2026, 7, 3, tzinfo=UTC),
            ),
        ],
    }
    defaults.update(kw)
    return TicketThread(**defaults)


class TestRenderAndBuild:
    def test_render_thread_contains_all_parts(self):
        text = render_thread(_thread())
        assert "red-shrik" in text and "red-data" in text
        assert "camelCase" in text
        assert "middleware" in text

    def test_build_messages_has_system_and_user(self):
        msgs = build_messages(_thread())
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"
        assert "JSON" in msgs[0]["content"]


class TestParseAndValidate:
    def test_valid_learning_proposal(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-shrik", '
            '"payload": {"topic": "red-data camelCase", '
            '"insight": "le middleware sérialise en camelCase, contrat voulu", '
            '"tags": ["api"]}, "rationale": "contrat durable"}]'
        )
        drafts = parse_and_validate(content, _thread())
        assert len(drafts) == 1
        assert drafts[0].target_type == "learning"
        assert drafts[0].target_project == "red-shrik"

    def test_empty_array_is_valid_zero_proposals(self):
        assert parse_and_validate("[]", _thread()) == []

    def test_markdown_fences_stripped(self):
        content = "```json\n[]\n```"
        assert parse_and_validate(content, _thread()) == []

    def test_invalid_json_raises(self):
        with pytest.raises(ResponseParseError):
            parse_and_validate("pas du json", _thread())

    def test_unknown_target_type_rejected(self):
        content = (
            '[{"target_type": "runbook", "target_project": "red-shrik", '
            '"payload": {}, "rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_type"):
            parse_and_validate(content, _thread())

    def test_target_project_must_be_participant(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-lab", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_project"):
            parse_and_validate(content, _thread())

    def test_missing_payload_keys_rejected(self):
        content = (
            '[{"target_type": "decision", "target_project": "red-data", '
            '"payload": {"title": "t"}, "rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="payload"):
            parse_and_validate(content, _thread())

    def test_overlong_topic_truncated_to_200(self):
        content = (
            '[{"target_type": "learning", "target_project": "red-data", '
            '"payload": {"topic": "' + "x" * 300 + '", "insight": "i", "tags": []}, '
            '"rationale": "r"}]'
        )
        drafts = parse_and_validate(content, _thread())
        assert len(drafts[0].payload["topic"]) == 200


class TestTargetProjectCanonicalization:
    """The poison pill of the 19→20 night: `brain_v42` refused, replayed every night.

    The model returns the repository's underscore form (`brain_v42`) where the
    canonical key is `brain-v42`. The membership test was on the RAW string, hence
    `ResponseParseError` — and since the ticket stays `pending`, the SAME error
    replays every night. That is a poison pill, not a one-off failure.

    `_ALIASES` maps `brain` and `brain_v42` BEFORE the shape test and without ever
    raising: canonicalizing with `strict=False` is therefore enough, and it is the
    only admissible mode here — `strict=True` would raise a `ValueError` that would
    escape the caller's `except ResponseParseError` and kill the corrective
    re-prompt.
    """

    def test_underscore_alias_is_canonicalized_before_the_membership_test(self):
        content = (
            '[{"target_type": "learning", "target_project": "brain_v42", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "r"}]'
        )
        drafts = parse_and_validate(content, _thread(from_project="brain-v42"))
        assert len(drafts) == 1
        assert drafts[0].target_project == "brain-v42", (
            "la valeur canonicalisée doit être RÉASSIGNÉE dans le draft : elle se "
            "propage jusqu'à la dédup SQL et à la colonne persistée. Laisser passer "
            "`brain_v42` au-delà du test d'appartenance recréerait le projet fantôme."
        )

    def test_surrounding_whitespace_does_not_reject_a_valid_key(self):
        content = (
            '[{"target_type": "learning", "target_project": "  red-shrik  ", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "r"}]'
        )
        drafts = parse_and_validate(content, _thread())
        assert drafts[0].target_project == "red-shrik"

    def test_a_non_participant_is_still_rejected_after_canonicalization(self):
        """Canonicalization must not WIDEN what is accepted."""
        content = (
            '[{"target_type": "learning", "target_project": "red-lab", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_project"):
            parse_and_validate(content, _thread())

    def test_a_malformed_key_raises_the_parse_error_never_a_value_error(self):
        """`strict=False`: the function's error contract does not change.

        If canonicalization were done with `strict=True`, `ValueError` would leave
        `parse_and_validate` without being a `ResponseParseError` — the caller
        would not catch it and the corrective re-prompt would never be played.
        """
        content = (
            '[{"target_type": "learning", "target_project": "Red Lab!", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_project"):
            parse_and_validate(content, _thread())

    @pytest.mark.parametrize("bad", ["null", "42", "{}", "[]"])
    def test_a_non_string_target_project_is_rejected_without_crashing(self, bad: str):
        """`canonicalize_project_key` calls `.strip()`: a non-str would raise an
        `AttributeError` there, which is not a `ResponseParseError` and would kill
        the night."""
        content = (
            '[{"target_type": "learning", "target_project": ' + bad + ", "
            '"payload": {"topic": "t", "insight": "i", "tags": []}, '
            '"rationale": "x"}]'
        )
        with pytest.raises(ResponseParseError, match="target_project"):
            parse_and_validate(content, _thread())


# ── Helpers for TestApplyProposals ─────────────────────────────────────────────


def _make_proposal_row(
    proposal_id: int = 1,
    ticket_id: UUID | None = None,
    target_type: str = "learning",
    target_project: str = "red-shrik",
    payload: dict | None = None,
) -> dict:
    """Return a dict that mimics a SQLAlchemy RowMapping for a proposal row."""
    return {
        "id": proposal_id,
        "ticket_id": ticket_id or uuid4(),
        "target_type": target_type,
        "target_project": target_project,
        "payload": payload
        or {
            "topic": "camelCase contract",
            "insight": "middleware serialises camelCase — intended contract",
            "tags": ["api"],
        },
        "status": "proposed",
    }


def _make_session_factory(
    proposals: list[dict],
    remaining_count: int = 0,
) -> tuple[Any, MagicMock]:
    """
    Build a fake session_factory async context-manager.

    Returns (session_factory, mock_session) where mock_session has
    spec=AsyncSession — so calling non-existent methods like .mappings()
    raises AttributeError instead of silently succeeding.
    """
    mock_session = MagicMock(spec=AsyncSession)

    # execute() must be an AsyncMock so it can be awaited.
    mock_execute = AsyncMock()
    mock_session.execute = mock_execute

    # begin() must return an async context manager.
    mock_begin_cm = MagicMock()
    mock_begin_cm.__aenter__ = AsyncMock(return_value=None)
    mock_begin_cm.__aexit__ = AsyncMock(return_value=False)
    mock_session.begin = MagicMock(return_value=mock_begin_cm)

    # Side-effects for execute(): ordered sequence of calls.
    # Call 1: SELECT one proposal → returns a row via .mappings().one_or_none().
    proposals_result = MagicMock()
    proposals_result.mappings.return_value.all.return_value = proposals
    proposals_result.mappings.return_value.one_or_none.return_value = (
        proposals[0] if proposals else None
    )

    # Call 2: UPDATE proposal → applied.
    update_proposal_result = MagicMock()

    # Call 3: lock the parent ticket before checking whether triage is complete.
    ticket_lock_result = MagicMock()

    # Call 4: remaining count (scalar_one → remaining_count).
    remaining_result = MagicMock()
    remaining_result.scalar_one.return_value = remaining_count

    # Call 5: UPDATE ticket → done (if remaining == 0).
    update_ticket_result = MagicMock()

    # Wire the side_effect: each await session.execute() pops the next value.
    mock_execute.side_effect = [
        proposals_result,
        update_proposal_result,
        ticket_lock_result,
        remaining_result,
        update_ticket_result,
    ]

    @asynccontextmanager
    async def _fake_session_factory():
        yield mock_session

    return _fake_session_factory, mock_session


class TestApplyProposals:
    """
    TDD coverage for apply_proposals().

    RED proof: before the CRITICAL-1 fix, the first test raises AttributeError
    because AsyncSession (spec=) has no .mappings() method — the buggy code
    calls `session.mappings().execute(stmt)` instead of
    `session.execute(stmt).mappings()`.
    """

    @pytest.mark.asyncio
    async def test_mappings_called_on_result_not_session_raises_on_buggy_code(self):
        """
        RED test: proves CRITICAL-1 — `session.mappings()` does not exist on
        AsyncSession. With spec=AsyncSession the mock raises AttributeError.
        After the fix, execute() is called on session directly and .mappings()
        on the Result — this test must PASS (no AttributeError).
        """
        proposal_row = _make_proposal_row()
        session_factory, mock_session = _make_session_factory([proposal_row])

        mock_learning = AsyncMock()
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            applied, entities = await apply_proposals(session_factory, [proposal_row["id"]])

        # Must not raise — CRITICAL-1 is fixed.
        assert applied == 1
        assert entities == 1

    @pytest.mark.asyncio
    async def test_begin_called_before_execute_for_remaining_count(self):
        """
        Anti-regression for CRITICAL-2: session.begin() must be entered
        BEFORE session.execute() on the remaining-count path, not after.
        Verifies call ORDER on the mock.
        """
        proposal_row = _make_proposal_row()
        session_factory, mock_session = _make_session_factory([proposal_row], remaining_count=0)

        mock_learning = AsyncMock()
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            await apply_proposals(session_factory, [proposal_row["id"]])

        # On the session used for remaining-count + ticket update, begin()
        # must have been called.  Because we use separate context-manager
        # invocations (each `async with session_factory() as session` yields
        # the SAME mock_session), we verify begin() was called at least once.
        assert mock_session.begin.called, (
            "session.begin() must be called for remaining-count transaction"
        )

    @pytest.mark.asyncio
    async def test_learning_entity_created_with_correct_fields(self):
        """
        When a 'proposed' learning proposal is applied, LearningService.create()
        is called with confidence='medium', source_type='automated',
        source='ticket:<ticket_id>'.
        """
        ticket_id = uuid4()
        proposal_row = _make_proposal_row(
            proposal_id=42,
            ticket_id=ticket_id,
            target_type="learning",
            target_project="red-shrik",
            payload={
                "topic": "camelCase contract",
                "insight": "middleware intended",
                "tags": ["api"],
            },
        )
        session_factory, _ = _make_session_factory([proposal_row])

        mock_learning = AsyncMock()
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            applied, entities = await apply_proposals(session_factory, [42])

        assert applied == 1
        assert entities == 1

        create_call_args = mock_learning.create.call_args
        assert create_call_args is not None, "LearningService.create() was not called"
        learning_create = create_call_args[0][0]  # positional arg

        assert learning_create.confidence == "medium"
        assert learning_create.source_type == "automated"
        assert learning_create.source == f"ticket:{ticket_id}"
        assert learning_create.project_key == "red-shrik"

    @pytest.mark.asyncio
    async def test_ticket_marked_done_when_no_remaining_proposed(self):
        """
        After all proposals for a ticket are applied and remaining_count == 0,
        the ticket's extraction_status must be updated to 'done'.
        Verifies that session.execute() is called for the UPDATE tickets statement.
        """
        proposal_row = _make_proposal_row()
        session_factory, mock_session = _make_session_factory([proposal_row], remaining_count=0)

        mock_learning = AsyncMock()
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            await apply_proposals(session_factory, [proposal_row["id"]])

        # session.execute() must have been called multiple times (the last one
        # is the UPDATE tickets SET extraction_status='done').
        assert mock_session.execute.call_count >= 4, (
            f"Expected >= 4 execute calls (fetch + update-proposal + count + update-ticket), "
            f"got {mock_session.execute.call_count}"
        )

    @pytest.mark.asyncio
    async def test_empty_proposal_ids_returns_zero(self):
        """Calling apply_proposals with [] does nothing and returns (0, 0)."""
        mock_session = MagicMock(spec=AsyncSession)

        # The single-id facade has no proposal to dispatch and must not touch DB.
        mock_session.execute = AsyncMock()
        mock_session.begin = MagicMock()

        @asynccontextmanager
        async def _empty_factory():
            yield mock_session

        mock_learning = AsyncMock()
        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            applied, entities = await apply_proposals(_empty_factory, [])

        assert applied == 0
        assert entities == 0
        mock_session.execute.assert_not_awaited()
        # LearningService.create() must not be called when there are no proposals.
        mock_learning.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_stale_reconcile_graph_hint_after_entity_creation(self, capsys):
        """
        Entities created here get their graph_outbox events from the PG
        triggers on learnings/decisions — the projector syncs Neo4j without
        operator action, and scripts.reconcile_graph is rejected by Settings
        when graph_projector_enabled is active. The legacy stdout hint telling
        the operator to run it must not be printed.
        """
        proposal_row = _make_proposal_row()
        session_factory, _ = _make_session_factory([proposal_row])

        mock_learning = AsyncMock()
        created_entity = MagicMock()
        created_entity.id = uuid4()
        mock_learning.create = AsyncMock(return_value=created_entity)

        with (
            patch(
                "scripts.ticket_extract._build_apply_services",
                return_value=(mock_learning, AsyncMock()),
            ),
        ):
            applied, entities = await apply_proposals(session_factory, [proposal_row["id"]])

        # Guard: the branch that used to print the hint (>= 1 entity) was reached.
        assert entities == 1
        out = capsys.readouterr().out
        assert "reconcile_graph" not in out
        assert "graph not updated" not in out


# ---------------------------------------------------------------------------
# extract_thread — named error capture (incident 2026-07-04: an empty "failed:",
# str() of httpx transport errors is often ""; the class name must always appear
# in outcome.error).
# ---------------------------------------------------------------------------


class TestExtractThreadErrorCapture:
    @pytest.mark.asyncio
    async def test_transport_error_names_exception_type(self) -> None:
        """A transport error with an empty message produces a named outcome.error."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.nvidia.local/v1",
        ) as client:
            outcome = await extract_thread(client, "test-model", _thread())

        assert outcome.failed
        assert outcome.error  # never empty
        assert "ConnectError" in outcome.error


class TestBoundedExtraction:
    @pytest.mark.asyncio
    async def test_ticket_deadline_returns_timeout_outcome_without_raising(self) -> None:
        thread = _thread()

        async def slow_extract(*_args: Any, **_kwargs: Any) -> ThreadOutcome:
            await __import__("asyncio").sleep(0.05)
            return ThreadOutcome(thread=thread, drafts=[])

        outcome = await _extract_thread_with_budget(
            object(), "model", thread, timeout_seconds=0.001, extract=slow_extract
        )

        assert outcome.failed is True
        assert "ticket timeout" in (outcome.error or "")

    def test_error_summary_redacts_bearer_credentials(self) -> None:
        assert "secret-token" not in _safe_error("HTTP 401 Bearer secret-token")
        assert "[redacted]" in _safe_error("HTTP 401 Bearer secret-token")


# ---------------------------------------------------------------------------
# Corpus dedup gate — prerequisite before EXTRACT can leave DRY mode.
# ---------------------------------------------------------------------------


def _draft(**kw: Any) -> ProposalDraft:
    defaults = {
        "ticket_id": uuid4(),
        "target_type": "learning",
        "target_project": "red-shrik",
        "payload": {
            "topic": "camelCase contract",
            "insight": "The middleware serialises API responses to camelCase.",
            "tags": ["api"],
        },
        "rationale": "durable API contract",
    }
    defaults.update(kw)
    return ProposalDraft(**defaults)


def _valid_vector(axis: int = 0) -> list[float]:
    vector = [0.0] * 1536
    vector[axis] = 1.0
    return vector


def _dedup_session(
    *,
    matches: list[Any] | None = None,
    backlog_rows: list[Any] | None = None,
) -> tuple[Any, MagicMock]:
    session = MagicMock(spec=AsyncSession)
    backlog_rows = backlog_rows or [SimpleNamespace(missing_learning=False, missing_decision=False)]
    matches = matches or [None]

    results: list[MagicMock] = []
    for backlog_row in backlog_rows:
        backlog_result = MagicMock()
        backlog_result.one.return_value = backlog_row
        results.append(backlog_result)
    for match in matches:
        match_result = MagicMock()
        match_result.first.return_value = match
        results.append(match_result)
    session.execute = AsyncMock(side_effect=results)

    @asynccontextmanager
    async def factory():
        yield session

    return factory, session


class TestCorpusDedupGate:
    @pytest.mark.asyncio
    async def test_cross_type_duplicate_is_removed_before_persist(self) -> None:
        entity_id = uuid4()
        factory, session = _dedup_session(
            matches=[
                SimpleNamespace(
                    entity_type="decision",
                    entity_id=entity_id,
                    label="API camelCase is intentional",
                    similarity=0.93,
                )
            ]
        )
        embedding = AsyncMock()
        embedding.embed.return_value = _valid_vector()
        draft = _draft()

        result = await deduplicate_drafts(factory, embedding, [draft])

        assert result.kept == []
        assert len(result.duplicates) == 1
        duplicate = result.duplicates[0]
        assert duplicate.draft is draft
        assert duplicate.entity_type == "decision"
        assert duplicate.entity_id == entity_id
        assert duplicate.similarity == pytest.approx(0.93)
        embedding.embed.assert_awaited_once_with(
            "camelCase contract The middleware serialises API responses to camelCase."
        )

        stmt = session.execute.await_args_list[-1].args[0]
        sql = str(stmt.compile()).lower()
        assert "union all" in sql
        assert "<=>" in sql
        assert "learnings" in sql and "decisions" in sql
        assert "project_key" in sql
        assert "freshness_status" in sql
        assert "merged_into" in sql
        assert "status" in sql and "superseded_by" in sql

    @pytest.mark.asyncio
    async def test_below_threshold_candidate_is_kept_defensively(self) -> None:
        factory, _session = _dedup_session(
            matches=[
                SimpleNamespace(
                    entity_type="learning",
                    entity_id=uuid4(),
                    label="related but distinct",
                    similarity=0.849,
                )
            ]
        )
        embedding = AsyncMock()
        embedding.embed.return_value = _valid_vector()
        draft = _draft()

        result = await deduplicate_drafts(factory, embedding, [draft])

        assert result.kept == [draft]
        assert result.duplicates == []

    @pytest.mark.asyncio
    async def test_empty_batch_does_not_touch_embedding_or_database(self) -> None:
        factory, session = _dedup_session()
        embedding = AsyncMock()

        result = await deduplicate_drafts(factory, embedding, [])

        assert result == DedupResult()
        embedding.embed.assert_not_awaited()
        session.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_embedding_dimension_fails_closed(self) -> None:
        factory, session = _dedup_session()
        embedding = AsyncMock()
        embedding.embed.return_value = [0.0] * 10

        with pytest.raises(CorpusDedupUnavailable, match="dimension"):
            await deduplicate_drafts(factory, embedding, [_draft()])

        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_zero_norm_embedding_fails_closed(self) -> None:
        factory, session = _dedup_session()
        embedding = AsyncMock()
        embedding.embed.return_value = [0.0] * 1536

        with pytest.raises(CorpusDedupUnavailable, match="zero norm"):
            await deduplicate_drafts(factory, embedding, [_draft()])

        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_active_null_embedding_backlog_fails_before_embedding(self) -> None:
        factory, session = _dedup_session(
            backlog_rows=[SimpleNamespace(missing_learning=True, missing_decision=False)]
        )
        embedding = AsyncMock()

        with pytest.raises(CorpusDedupUnavailable, match="embedding backlog.*red-shrik"):
            await deduplicate_drafts(factory, embedding, [_draft()])

        session.execute.assert_awaited_once()
        embedding.embed.assert_not_awaited()
        backlog_stmt = session.execute.await_args.args[0]
        backlog_sql = str(backlog_stmt.compile()).lower()
        assert "learnings.embedding is null" in backlog_sql
        assert "decisions.embedding is null" in backlog_sql
        assert "vector_norm(learnings.embedding)" in backlog_sql
        assert "vector_norm(decisions.embedding)" in backlog_sql
        assert "project_key" in backlog_sql
        assert "freshness_status" in backlog_sql
        assert "merged_into" in backlog_sql
        assert "status" in backlog_sql and "superseded_by" in backlog_sql

    def test_operational_dream_alerts_are_excluded_from_corpus_backlog(self) -> None:
        from scripts.ticket_extract import _corpus_backlog_stmt

        sql = str(
            _corpus_backlog_stmt("brain-v42").compile(compile_kwargs={"literal_binds": True})
        ).lower()
        assert "dream_post_run_alert" in sql

    @pytest.mark.asyncio
    async def test_embedding_failure_is_wrapped_and_fails_closed(self) -> None:
        factory, session = _dedup_session()
        embedding = AsyncMock()
        embedding.embed.side_effect = RuntimeError("gpu unavailable")

        with pytest.raises(CorpusDedupUnavailable, match="embedding"):
            await deduplicate_drafts(factory, embedding, [_draft()])

        session.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_database_failure_is_wrapped_and_fails_closed(self) -> None:
        factory, session = _dedup_session()
        session.execute.side_effect = RuntimeError("postgres unavailable")
        embedding = AsyncMock()
        embedding.embed.return_value = _valid_vector()

        with pytest.raises(CorpusDedupUnavailable, match="corpus"):
            await deduplicate_drafts(factory, embedding, [_draft()])

        embedding.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_run_duplicate_is_removed_cross_type(self) -> None:
        first = _draft()
        second = _draft(
            target_type="decision",
            payload={
                "title": "camelCase contract",
                "description": "The middleware serialises API responses to camelCase.",
                "reasoning": "This is an intentional API contract.",
                "tags": ["api"],
            },
        )
        factory, _session = _dedup_session(matches=[None, None])
        embedding = AsyncMock()
        embedding.embed.return_value = _valid_vector()

        result = await deduplicate_drafts(factory, embedding, [first, second])

        assert result.kept == [first]
        assert len(result.duplicates) == 1
        assert result.duplicates[0].draft is second
        assert result.duplicates[0].entity_id == first.ticket_id
        assert result.duplicates[0].entity_type == first.target_type
        assert result.duplicates[0].match_source == "run"
        assert result.duplicates[0].similarity == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_same_vector_in_different_projects_is_kept(self) -> None:
        first = _draft(target_project="red-shrik")
        second = _draft(target_project="red-data")
        factory, _session = _dedup_session(
            backlog_rows=[
                SimpleNamespace(missing_learning=False, missing_decision=False),
                SimpleNamespace(missing_learning=False, missing_decision=False),
            ],
            matches=[None, None],
        )
        embedding = AsyncMock()
        embedding.embed.return_value = _valid_vector()

        result = await deduplicate_drafts(factory, embedding, [first, second])

        assert result.kept == [first, second]
        assert result.duplicates == []


class TestPersistProposalsConcurrency:
    @pytest.mark.asyncio
    async def test_pending_ticket_is_persisted_under_lock(self) -> None:
        thread = _thread()
        session = MagicMock(spec=AsyncSession)
        ticket_lock_result = MagicMock()
        ticket_lock_result.scalar_one_or_none.return_value = "pending"
        insert_result = MagicMock()
        insert_result.scalar_one.return_value = 73
        session.execute = AsyncMock(side_effect=[ticket_lock_result, insert_result, MagicMock()])
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield session

        result = await persist_proposals(factory, thread, [_draft(ticket_id=thread.id)])

        assert result == [73]
        assert session.execute.await_count == 3
        statements = [
            str(call.args[0].compile()).lower() for call in session.execute.await_args_list
        ]
        assert "for update" in statements[0]
        assert "insert into ticket_extraction_proposals" in statements[1]
        assert "update tickets" in statements[2]

    @pytest.mark.asyncio
    async def test_stale_runner_does_not_persist_after_ticket_was_claimed(self) -> None:
        thread = _thread()
        session = MagicMock(spec=AsyncSession)
        ticket_lock_result = MagicMock()
        ticket_lock_result.scalar_one_or_none.return_value = "proposed"
        session.execute = AsyncMock(return_value=ticket_lock_result)
        begin_cm = MagicMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

        @asynccontextmanager
        async def factory():
            yield session

        result = await persist_proposals(factory, thread, [_draft(ticket_id=thread.id)])

        assert result == []
        session.execute.assert_awaited_once()
        lock_stmt = session.execute.await_args.args[0]
        assert "FOR UPDATE" in str(lock_stmt.compile()).upper()


class TestRunDedupWiring:
    @pytest.mark.asyncio
    async def test_timeout_after_first_ticket_keeps_checkpoint_and_records_terminal_run(
        self,
    ) -> None:
        first, second = _thread(), _thread()
        first_draft = _draft(ticket_id=first.id)
        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=300,
            ticket_budget_seconds=1,
        )
        session_factory = MagicMock()
        embedding = MagicMock(close=AsyncMock())
        persist = AsyncMock(return_value=[41])
        record = AsyncMock()
        attempts = AsyncMock()

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[first, second]),
            ),
            patch(
                "scripts.ticket_extract._extract_thread_with_budget",
                new=AsyncMock(
                    side_effect=[
                        ThreadOutcome(thread=first, drafts=[first_draft]),
                        ThreadOutcome(
                            thread=second,
                            drafts=[],
                            failed=True,
                            error="ticket timeout after 1s",
                        ),
                    ]
                ),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(return_value=DedupResult(kept=[first_draft])),
            ),
            patch("scripts.ticket_extract.persist_proposals", persist),
            patch("scripts.ticket_extract.record_ticket_attempt", attempts),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 3
        persist.assert_awaited_once_with(session_factory, first, [first_draft])
        assert [call.args[2] for call in attempts.await_args_list] == ["done", "timeout"]
        assert record.await_args.kwargs["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_gate_failure_never_persists_or_applies(self) -> None:
        thread = _thread()
        draft = _draft(ticket_id=thread.id)
        args = SimpleNamespace(apply_ids=None, limit=20, wet=True)
        session_factory = MagicMock()
        embedding = MagicMock()
        embedding.close = AsyncMock()
        persist = AsyncMock()
        apply = AsyncMock()
        record = AsyncMock()
        attempts = AsyncMock()

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch(
                "brain_v42.db.engine.get_session_factory",
                return_value=session_factory,
            ),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(return_value=ThreadOutcome(thread=thread, drafts=[draft])),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(
                    side_effect=CorpusDedupUnavailable("embedding unavailable: Bearer secret-token")
                ),
            ),
            patch("scripts.ticket_extract.persist_proposals", persist),
            patch("scripts.ticket_extract.apply_proposals", apply),
            patch("scripts.ticket_extract.record_ticket_attempt", attempts),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 1
        persist.assert_not_awaited()
        apply.assert_not_awaited()
        embedding.close.assert_awaited_once()
        record.assert_awaited_once()
        assert record.await_args.kwargs["status"] == "fail"
        recorded_error = attempts.await_args.args[4]
        assert "secret-token" not in recorded_error
        assert "[redacted]" in recorded_error

    @pytest.mark.asyncio
    async def test_wet_persists_and_applies_only_novel_drafts(self) -> None:
        thread = _thread()
        duplicate_draft = _draft(ticket_id=thread.id)
        novel_draft = _draft(
            ticket_id=thread.id,
            payload={
                "topic": "Novel contract",
                "insight": "A genuinely new reusable constraint.",
                "tags": ["contract"],
            },
        )
        args = SimpleNamespace(apply_ids=None, limit=20, wet=True)
        session_factory = MagicMock()
        embedding = MagicMock()
        embedding.close = AsyncMock()
        persist = AsyncMock(return_value=[41])
        apply = AsyncMock(return_value=(1, 1))
        record = AsyncMock()
        dedup = DedupResult(
            kept=[novel_draft],
            duplicates=[
                CorpusDuplicate(
                    draft=duplicate_draft,
                    entity_type="decision",
                    entity_id=uuid4(),
                    label="Existing contract",
                    similarity=0.94,
                )
            ],
        )

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch(
                "brain_v42.db.engine.get_session_factory",
                return_value=session_factory,
            ),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(
                    return_value=ThreadOutcome(
                        thread=thread,
                        drafts=[duplicate_draft, novel_draft],
                    )
                ),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(return_value=dedup),
            ),
            patch("scripts.ticket_extract.persist_proposals", persist),
            patch("scripts.ticket_extract.apply_proposals", apply),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 0
        persist.assert_awaited_once_with(session_factory, thread, [novel_draft])
        apply.assert_awaited_once_with(session_factory, [41])
        embedding.close.assert_awaited_once()
        assert record.await_args.kwargs["status"] == "done"


# ---------------------------------------------------------------------------
# Deferral is not a timeout (ticket 572220e9)
# ---------------------------------------------------------------------------


class TestDeferralIsNotATimeout:
    """A deferred ticket is the budget working, not the budget failing.

    Measured on 2026-08-04: `ticket_extraction_attempts` held 15 `deferred`,
    5 `done`, and zero `timeout` — yet the phase reported TIMEOUT, dream.sh
    pushed it into TIMED_OUT_PHASES, and the systemd unit went to `failed`.
    It had done so every night. An alarm that fires every night stops being
    an alarm, which is how the real signal would have been missed.

    The database already recorded the honest status; only the exit path lied.
    """

    def test_a_clean_run_exits_zero(self) -> None:
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=0, any_failed=False, deferred=0) == 0

    def test_a_pure_deferral_gets_its_own_code(self) -> None:
        """Not 3: nothing timed out. Not 0: work is still owed."""
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=0, any_failed=False, deferred=15) == 4

    def test_a_real_timeout_still_reports_three(self) -> None:
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=1, any_failed=False, deferred=0) == 3

    def test_a_real_timeout_outranks_a_deferral(self) -> None:
        """A run that both timed out and deferred is a timeout run."""
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=1, any_failed=False, deferred=15) == 3

    def test_a_failure_outranks_a_deferral(self) -> None:
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=0, any_failed=True, deferred=15) == 1

    @pytest.mark.asyncio
    async def test_run_budget_deferral_records_a_done_dream_run(self) -> None:
        """The terminal dream_run must not claim a failure that did not happen.

        The briefing reads dream_runs for its "Last failure" block, so a
        nominal deferral recorded as `timeout` puts a false failure in front
        of the operator at every session start.
        """
        first, second = _thread(), _thread()
        # Below the gate (ticket budget + finalization reserve), so every
        # thread is deferred before any model call is made.
        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=120.4,
            ticket_budget_seconds=1,
        )
        session_factory = MagicMock()
        record = AsyncMock()
        attempts = AsyncMock()
        extract = AsyncMock()

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[first, second]),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", extract),
            patch("scripts.ticket_extract.record_ticket_attempt", attempts),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 4
        extract.assert_not_awaited()
        assert [call.args[2] for call in attempts.await_args_list] == ["deferred", "deferred"]
        assert record.await_args.kwargs["status"] == "done"
        assert record.await_args.kwargs["error"] is None


class TestHardFailureOutranksTimeout:
    """`3` must mean "nothing but a deadline", otherwise it lies to dream.sh.

    Since 2026-08-07 dream.sh treats rc=3 as a CONTROLLED deadline and exits 0. The
    exit code therefore becomes the only thing distinguishing a nominal night from
    a failure — and it returned 3 as soon as one ticket had expired, even when
    other tickets had HARD failed alongside.

    Case measured in production (`dream_runs`, 2026-08-02):
    `phase=extract, status='timeout'`, and yet
    `error_message='corpus dedup unavailable: corpus embedding backlog …'` — a
    message that can only come out of the dedup's hard-failure branch. Both
    natures coexisted, the embedding service was down, and the old priority
    (`if timed_out: return 3` before `if any_failed`) would have made that night
    green. The day before, the same root cause WITHOUT a concurrent timeout had
    returned 1: the failure's visibility depended on the fortuitous presence of a
    timeout.
    """

    def test_a_hard_failure_beside_a_timeout_does_not_report_three(self) -> None:
        """The priority, not merely the counter's existence."""
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=1, any_failed=True, hard_failed=1, deferred=0) == 1

    def test_a_pure_deadline_keeps_reporting_three(self) -> None:
        """Without a hard failure, rc=3 keeps its meaning — that is what exits 0."""
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=2, any_failed=True, hard_failed=0, deferred=0) == 3

    def test_a_hard_failure_alone_still_reports_one(self) -> None:
        from scripts.ticket_extract import _exit_code

        assert _exit_code(timed_out=0, any_failed=True, hard_failed=1, deferred=3) == 1

    def test_every_failure_site_feeds_a_discriminating_counter(self) -> None:
        """A SHAPE invariant, complementary to the two scenarios below.

        `any_failed` alone discriminates nothing: it is True for a timeout as much
        as for a failure, and becomes visible in the exit code only if `timed_out`
        is 0 — in other words, by accident of calendar. A failure branch added
        tomorrow that spoke only to `any_failed` would therefore fall straight back
        into the 2026-08-02 defect.

        This AST inspects `_run`'s REAL blocks: every block that sets
        `any_failed = True` must, in the same block, touch `hard_failed` or
        `timed_out`. It does NOT prove the counter touched is the right one — that
        is the job of the two scenario tests, which exercise `_run` end to end on
        the two branches actually observed in production. It does, however, cover
        the `persist` and `wet apply` branches, which those scenarios do not reach.
        """
        import ast  # noqa: PLC0415
        import inspect  # noqa: PLC0415

        from scripts import ticket_extract  # noqa: PLC0415

        module = ast.parse(inspect.getsource(ticket_extract))
        run_node = next(
            node
            for node in ast.walk(module)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run"
        )

        def _sets_any_failed(stmt: ast.stmt) -> bool:
            return (
                isinstance(stmt, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "any_failed" for t in stmt.targets)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is True
            )

        blind_sites: list[int] = []
        for node in ast.walk(run_node):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list) or not any(map(_sets_any_failed, block)):
                    continue
                names = {
                    inner.id
                    for stmt in block
                    for inner in ast.walk(stmt)
                    if isinstance(inner, ast.Name)
                }
                if not names & {"hard_failed", "timed_out"}:
                    blind_sites.append(next(s.lineno for s in block if _sets_any_failed(s)))

        assert blind_sites == [], (
            "branche(s) d'échec aveugle(s) au code de sortie, lignes "
            f"{blind_sites} de scripts/ticket_extract.py : elles posent "
            "any_failed sans dire à personne s'il s'agit d'une panne ou de "
            "l'horloge, donc rc=3 (unité verte) pourra les masquer"
        )

        # Harness guard: with no site detected, the assertion above would be true
        # over nothing (the exact failure mode of learning 670c74a3).
        sites = sum(
            1
            for node in ast.walk(run_node)
            for field in ("body", "orelse", "finalbody")
            if isinstance(getattr(node, field, None), list)
            for stmt in getattr(node, field)
            if _sets_any_failed(stmt)
        )
        # 7th site added on 2026-08-12 and RE-READ as this message requires: the
        # "no live model left" loop exit (410/404 on the primary AND the
        # fallback). It feeds `hard_failed`, and that is deliberate — a model
        # retired at the provider is not a deadline, and rc=3 would leave the unit
        # green over a phase that could extract nothing.
        assert sites == 7, (
            f"l'AST n'a trouvé que {sites} site(s) d'échec au lieu des 7 "
            "mesurés : soit l'ancre est cassée et l'assertion ci-dessus est "
            "vraie sur du vide, soit un site a changé de forme et doit être "
            "relu ici avant d'être recompté"
        )

    @pytest.mark.asyncio
    async def test_a_run_mixing_a_dedup_outage_and_a_deadline_exits_one(self) -> None:
        """The WIRING, not merely the pure function.

        A correct `hard_failed` counter that is never incremented at the call site
        would let the three tests above pass. Here the night of 2026-08-02 is
        replayed through `_run`: one ticket dies on an unavailable dedup (a HARD
        failure), the other expires (a deadline).
        """
        failing, expiring = _thread(), _thread()
        draft = _draft(ticket_id=failing.id)

        async def _extract(_client: Any, _model: Any, thread: TicketThread) -> ThreadOutcome:
            if thread.id == failing.id:
                return ThreadOutcome(thread=thread, drafts=[draft])
            return ThreadOutcome(
                thread=thread, drafts=[], failed=True, error="ticket timeout after 180s"
            )

        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=MagicMock(close=AsyncMock()),
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[failing, expiring]),
            ),
            patch("scripts.ticket_extract.extract_thread", new=AsyncMock(side_effect=_extract)),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(side_effect=CorpusDedupUnavailable("embedding backlog")),
            ),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        # Scenario guard: without BOTH natures, the assertion below would prove
        # nothing (a purely hard run returns 1 anyway).
        assert record.await_args.kwargs["status"] == "timeout", (
            f"le scénario n'a pas produit de timeout concomitant : {record.await_args.kwargs!r}"
        )
        assert exit_code == 1, (
            "une panne dure concomitante ne doit pas être déguisée en échéance "
            "contrôlée — dream.sh sortirait en 0 sur une extraction cassée"
        )

    @pytest.mark.asyncio
    async def test_a_run_mixing_an_extraction_error_and_a_deadline_exits_one(self) -> None:
        """The SAME branch serves both natures, discriminated by `is_timeout`.

        A ticket whose extraction hard-fails (HTTP 500, parse) and a ticket that
        expires both go through the same `if outcome.failed`. A counter wired into
        the dedup but not here would let the previous test pass while masking a
        dead API.
        """
        broken, expiring = _thread(), _thread()

        async def _extract(_client: Any, _model: Any, thread: TicketThread) -> ThreadOutcome:
            error = (
                "HTTPStatusError: 500" if thread.id == broken.id else "ticket timeout after 180s"
            )
            return ThreadOutcome(thread=thread, drafts=[], failed=True, error=error)

        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[broken, expiring]),
            ),
            patch("scripts.ticket_extract.extract_thread", new=AsyncMock(side_effect=_extract)),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert record.await_args.kwargs["status"] == "timeout", (
            f"le scénario n'a pas produit de timeout concomitant : {record.await_args.kwargs!r}"
        )
        assert exit_code == 1, (
            "une extraction durement cassée à côté d'une échéance doit rester "
            "un échec, pas une nuit nominale"
        )


# ---------------------------------------------------------------------------
# Timing instrumentation (ticket 572220e9, criterion #1) — structlog, never
# print. The run must make it observable, after the fact, where the 240s of
# a bounded extract phase actually went: per-ticket duration, time spent
# before the first ticket, remaining budget when the gate closes, and how
# many tickets were deferred. Pure additions — no assertion here may change
# an existing exit code, DB write or print already covered above.
# ---------------------------------------------------------------------------


class TestExtractTimingInstrumentation:
    @pytest.mark.asyncio
    async def test_gate_closure_logs_remaining_budget_and_deferred_count(self) -> None:
        """When the run-budget gate closes, one structured event carries the
        exact numbers an operator needs: how much was left, and how many
        tickets got deferred as a result — not just a per-ticket reason.
        """
        first, second = _thread(), _thread()
        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=120.4,
            ticket_budget_seconds=1,
        )
        session_factory = MagicMock()

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[first, second]),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", new=AsyncMock()),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 4
        gate_events = [log for log in logs if log["event"] == "extract_gate_closed"]
        assert len(gate_events) == 1
        event = gate_events[0]
        assert event["deferred_count"] == 2
        assert event["ticket_budget_s"] == 1
        assert event["finalization_reserve_s"] == 120
        assert 0 <= event["remaining_s"] <= 120.4
        assert event["elapsed_s"] >= 0

    @pytest.mark.asyncio
    async def test_pretickets_elapsed_logged_once_before_first_ticket(self) -> None:
        """Time spent before ticket #1 even starts (DB fetch, HTTP client
        setup) must be visible on its own — it is currently unaccounted for.
        """
        thread = _thread()
        draft = _draft(ticket_id=thread.id)
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        session_factory = MagicMock()
        embedding = MagicMock(close=AsyncMock())

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(return_value=ThreadOutcome(thread=thread, drafts=[draft])),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(return_value=DedupResult(kept=[draft])),
            ),
            patch("scripts.ticket_extract.persist_proposals", new=AsyncMock(return_value=[41])),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 0
        pretickets_events = [log for log in logs if log["event"] == "extract_phase_pretickets"]
        assert len(pretickets_events) == 1
        assert pretickets_events[0]["elapsed_before_first_ticket_s"] >= 0

    @pytest.mark.asyncio
    async def test_ticket_timing_logged_for_a_successful_ticket(self) -> None:
        thread = _thread()
        draft = _draft(ticket_id=thread.id)
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        session_factory = MagicMock()
        embedding = MagicMock(close=AsyncMock())

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(return_value=ThreadOutcome(thread=thread, drafts=[draft])),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(return_value=DedupResult(kept=[draft])),
            ),
            patch("scripts.ticket_extract.persist_proposals", new=AsyncMock(return_value=[41])),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 0
        timing_events = [log for log in logs if log["event"] == "extract_ticket_timing"]
        assert len(timing_events) == 1
        event = timing_events[0]
        assert event["ticket_id"] == str(thread.id)
        assert event["position"] == 0
        assert event["status"] == "done"
        assert event["extraction_duration_s"] >= 0
        assert event["ticket_elapsed_s"] >= 0
        assert event["remaining_before_s"] >= 0

    @pytest.mark.asyncio
    async def test_ticket_timing_logged_for_extraction_failure(self) -> None:
        thread = _thread()
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        session_factory = MagicMock()

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract._extract_thread_with_budget",
                new=AsyncMock(
                    return_value=ThreadOutcome(
                        thread=thread, drafts=[], failed=True, error="boom: ConnectError"
                    )
                ),
            ),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 1
        timing_events = [log for log in logs if log["event"] == "extract_ticket_timing"]
        assert len(timing_events) == 1
        event = timing_events[0]
        assert event["status"] == "failed"
        assert event["phase"] == "extraction"

    @pytest.mark.asyncio
    async def test_ticket_timing_logged_for_dedup_failure(self) -> None:
        thread = _thread()
        draft = _draft(ticket_id=thread.id)
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        session_factory = MagicMock()
        embedding = MagicMock(close=AsyncMock())

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(return_value=ThreadOutcome(thread=thread, drafts=[draft])),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(side_effect=CorpusDedupUnavailable("embedding unavailable")),
            ),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 1
        timing_events = [log for log in logs if log["event"] == "extract_ticket_timing"]
        assert len(timing_events) == 1
        event = timing_events[0]
        assert event["status"] == "failed"
        assert event["phase"] == "dedup"

    @pytest.mark.asyncio
    async def test_phase_summary_logged_with_run_counters(self) -> None:
        thread = _thread()
        draft = _draft(ticket_id=thread.id)
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        session_factory = MagicMock()
        embedding = MagicMock(close=AsyncMock())

        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=session_factory),
            patch(
                "brain_v42.services.embedding_factory.build_embedding_service",
                return_value=embedding,
            ),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[thread]),
            ),
            patch(
                "scripts.ticket_extract.extract_thread",
                new=AsyncMock(return_value=ThreadOutcome(thread=thread, drafts=[draft])),
            ),
            patch(
                "scripts.ticket_extract.deduplicate_drafts",
                new=AsyncMock(return_value=DedupResult(kept=[draft])),
            ),
            patch("scripts.ticket_extract.persist_proposals", new=AsyncMock(return_value=[41])),
            patch("scripts.ticket_extract.record_ticket_attempt", new=AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", new=AsyncMock()),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            with capture_logs() as logs:
                exit_code = await _run(args, "secret", "model", "https://llm.test")

        assert exit_code == 0
        summary_events = [log for log in logs if log["event"] == "extract_phase_summary"]
        assert len(summary_events) == 1
        event = summary_events[0]
        assert event["scanned"] == 1
        assert event["total_proposals"] == 1
        assert event["deferred_count"] == 0
        assert event["failed"] == 0
        assert event["timed_out"] == 0
        assert event["phase_duration_s"] >= 0


# ---------------------------------------------------------------------------
# End-of-window slice budget (chantier C1)
#
# Measured 2026-08-07: the gate refused a ticket whenever `remaining` fell
# under `ticket_budget + reserve` (300s of a 540s window), so no ticket could
# START in the last 300s — 56% of the window was structurally dead, capped at
# two tickets a night, and 15 tickets were deferred with 204s still on the
# clock. The truncating `min()` right below the gate had therefore never once
# taken its second branch.
#
# The gate now refuses only when the slice that is REALLY usable
# (`remaining - reserve`) falls under `_MIN_TICKET_SLICE_SECONDS`.
# ---------------------------------------------------------------------------


class _ScriptedClock:
    """Deterministic replacement for `scripts.ticket_extract.time`.

    The module reads exactly one attribute of `time` (`monotonic`, verified by
    grep), so swapping the module reference in that namespace alone leaves
    asyncio's own clock untouched. Left frozen, `elapsed` is exactly 0, which
    makes budget arithmetic assertions exact instead of tolerant; tests that
    need spent time advance it explicitly from the extraction mock.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now


def _settled(value: Any) -> Any:
    """A settled future rather than a coroutine.

    `_within_deadline` drops its argument without awaiting it when the ticket
    deadline is already blown — the very path some tests below exercise. A
    coroutine dropped that way emits "never awaited"; a settled future does not.
    """
    import asyncio  # noqa: PLC0415

    future: Any = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


async def _run_budget_scenario(
    *,
    run_budget_seconds: float,
    ticket_budget_seconds: float = 180,
    threads: list[TicketThread] | None = None,
    advances: list[float] | None = None,
    wet: bool = False,
    proposals_per_ticket: int = 0,
    drafts_per_ticket: int = 0,
    apply_advance: float = 0.0,
) -> SimpleNamespace:
    """Drive `_run` on a scripted clock; return the mocks for the test to read.

    By default every thread yields a successful, draft-free outcome so the
    dedup and embedding paths stay out of the way. `advances[i]` is the wall
    time ticket #i is made to burn inside its extraction; `apply_advance` is
    the wall time each `--wet` auto-apply burns, which is how the post-loop
    budget can be driven down. It asserts nothing — the callers do.
    """
    threads = threads if threads is not None else [_thread()]
    burns = list(advances or [0.0] * len(threads))
    args = SimpleNamespace(
        apply_ids=None,
        limit=20,
        wet=wet,
        run_budget_seconds=run_budget_seconds,
        ticket_budget_seconds=ticket_budget_seconds,
    )
    clock = _ScriptedClock()
    pending = iter(list(zip(threads, burns, strict=True)))

    async def _extract(*_args: Any, **_kwargs: Any) -> ThreadOutcome:
        thread, burn = next(pending)
        clock.now += burn
        return ThreadOutcome(
            thread=thread,
            drafts=[_draft(ticket_id=thread.id) for _ in range(drafts_per_ticket)],
        )

    minted = [0]

    def _persist(_sf: Any, _thread: Any, _kept: Any) -> Any:
        ids = [minted[0] + n + 1 for n in range(proposals_per_ticket)]
        minted[0] += proposals_per_ticket
        return _settled(ids)

    async def _apply(*_args: Any, **_kwargs: Any) -> tuple[int, int]:
        clock.now += apply_advance
        return (1, 1)

    extract = AsyncMock(side_effect=_extract)
    attempts = AsyncMock()
    record = AsyncMock()
    apply = AsyncMock(side_effect=_apply)

    with (
        patch("scripts.ticket_extract.time", clock),
        patch("brain_v42.config.Settings") as settings_cls,
        patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
        patch(
            "brain_v42.services.embedding_factory.build_embedding_service",
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch(
            "scripts.ticket_extract.fetch_pending_threads",
            new=AsyncMock(return_value=threads),
        ),
        patch("scripts.ticket_extract._extract_thread_with_budget", extract),
        patch(
            "scripts.ticket_extract.deduplicate_drafts",
            MagicMock(side_effect=lambda _sf, _svc, drafts: _settled(DedupResult(kept=drafts))),
        ),
        patch("scripts.ticket_extract.persist_proposals", MagicMock(side_effect=_persist)),
        patch("scripts.ticket_extract.apply_proposals", apply),
        patch("scripts.ticket_extract.record_ticket_attempt", attempts),
        patch("scripts.ticket_extract.record_dream_run", record),
    ):
        settings_cls.return_value.embedding_service_url = "http://embedding.test"
        with capture_logs() as logs:
            exit_code = await _run(args, "secret", "model", "https://llm.test")

    return SimpleNamespace(
        exit_code=exit_code,
        extract=extract,
        attempts=attempts,
        record=record,
        apply=apply,
        logs=logs,
    )


def _granted_slice(run: SimpleNamespace, position: int = 0) -> float:
    """The budget the run actually handed to the ticket at `position`."""
    return run.extract.await_args_list[position].kwargs["timeout_seconds"]


class TestEndOfWindowSliceBudget:
    @pytest.mark.asyncio
    async def test_a_ticket_starts_when_the_usable_slice_covers_the_floor(self) -> None:
        """The exact state of the 2026-08-07 night: 204.329s left, nominal
        ticket budget 180s. The old gate deferred 15 tickets here. The usable
        slice is 84.3s — well above the floor — so work must start.
        """
        run = await _run_budget_scenario(run_budget_seconds=204.329)

        assert run.extract.await_count == 1

    @pytest.mark.asyncio
    async def test_an_end_of_window_ticket_gets_the_reduced_slice(self) -> None:
        """The second branch of the `min()` becomes reachable: the ticket is
        bounded by what is left, not by its nominal budget.
        """
        run = await _run_budget_scenario(run_budget_seconds=204.329)

        assert _granted_slice(run) == pytest.approx(84.329)

    @pytest.mark.asyncio
    async def test_a_full_window_ticket_still_gets_its_nominal_budget(self) -> None:
        """The reduction must not leak into a healthy window: 540s left means
        420s usable, and the ticket is still capped at its nominal 180s.
        """
        run = await _run_budget_scenario(run_budget_seconds=540)

        assert _granted_slice(run) == pytest.approx(180)

    @pytest.mark.parametrize(
        "run_budget_seconds",
        [540, 300, 204.329, 181, 180.0],
        ids=["full-window", "old-gate-boundary", "measured-night", "tight", "at-the-floor"],
    )
    @pytest.mark.asyncio
    async def test_the_granted_slice_never_eats_the_finalization_reserve(
        self, run_budget_seconds: float
    ) -> None:
        """THE safety property of this chantier: whatever slice a ticket gets,
        120s must survive it to record the terminal dream_run. Fails if the
        second branch of the `min()` is dropped, or its subtraction inverted.
        """
        from scripts.ticket_extract import _FINALIZATION_RESERVE_SECONDS

        run = await _run_budget_scenario(run_budget_seconds=run_budget_seconds)

        assert run.extract.await_count == 1, "gate closed: the reserve claim would be vacuous"
        left_after_ticket = run_budget_seconds - _granted_slice(run)
        assert left_after_ticket >= _FINALIZATION_RESERVE_SECONDS - 1e-6

    @pytest.mark.asyncio
    async def test_the_floor_is_a_governed_module_constant(self) -> None:
        """No CLI flag: dream.sh has no use for this button."""
        from scripts.ticket_extract import _MIN_TICKET_SLICE_SECONDS

        assert _MIN_TICKET_SLICE_SECONDS == 60

    @pytest.mark.asyncio
    async def test_a_ticket_starts_on_exactly_the_floor(self) -> None:
        """180s left → 60s usable → exactly the floor, which is inclusive."""
        run = await _run_budget_scenario(run_budget_seconds=180.0)

        assert _granted_slice(run) == pytest.approx(60.0)

    @pytest.mark.asyncio
    async def test_no_ticket_starts_just_below_the_floor(self) -> None:
        """179.9s left → 59.9s usable. Under a minute a model call is waste,
        so the gate closes rather than manufacturing one more timeout.
        """
        run = await _run_budget_scenario(run_budget_seconds=179.9)

        run.extract.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_floor_deferral_is_not_counted_as_a_timeout(self) -> None:
        """A ticket never started exceeded no deadline (ticket 572220e9):
        status `deferred`, exit 4, and a `done` dream_run — not a failure in
        front of the operator at every session start.
        """
        first, second = _thread(), _thread()
        run = await _run_budget_scenario(run_budget_seconds=179.9, threads=[first, second])

        assert run.exit_code == 4
        assert [call.args[2] for call in run.attempts.await_args_list] == ["deferred", "deferred"]
        assert run.record.await_args.kwargs["status"] == "done"

    @pytest.mark.asyncio
    async def test_the_gate_event_names_the_usable_slice_and_the_floor(self) -> None:
        """`extract_gate_closed` must still say WHY it closed, and the reason
        is now the usable slice against the floor — not the nominal budget.
        """
        run = await _run_budget_scenario(run_budget_seconds=179.9)

        event = next(log for log in run.logs if log["event"] == "extract_gate_closed")
        assert event["usable_slice_s"] == pytest.approx(59.9)
        assert event["min_ticket_slice_s"] == 60

    @pytest.mark.asyncio
    async def test_the_deferral_reason_persisted_names_the_floor(self) -> None:
        """`ticket_extraction_attempts.error` is what an operator reads the
        morning after; "exhausted with 179.9s remaining" would read as a lie.
        """
        run = await _run_budget_scenario(run_budget_seconds=179.9)

        reason = run.attempts.await_args_list[0].args[4]
        assert "59.9s usable" in reason
        assert "60s minimum slice" in reason

    @pytest.mark.asyncio
    async def test_a_reduced_slice_ticket_is_labelled_reduced_in_its_timing_event(self) -> None:
        """Without this label nobody can measure whether this chantier served
        any purpose: nominal and reduced tickets would look identical in logs.
        """
        run = await _run_budget_scenario(run_budget_seconds=204.329)

        event = next(log for log in run.logs if log["event"] == "extract_ticket_timing")
        assert event["budget_mode"] == "reduced"
        assert event["ticket_slice_s"] == pytest.approx(84.329)

    @pytest.mark.asyncio
    async def test_a_full_budget_ticket_is_labelled_nominal_in_its_timing_event(self) -> None:
        run = await _run_budget_scenario(run_budget_seconds=540)

        event = next(log for log in run.logs if log["event"] == "extract_ticket_timing")
        assert event["budget_mode"] == "nominal"
        assert event["ticket_slice_s"] == pytest.approx(180)

    @pytest.mark.asyncio
    async def test_the_slice_shrinks_with_the_time_already_spent(self) -> None:
        """The usable slice is measured against the clock, not against the
        nominal window. Ticket #1 burns 175s of a 400s window, so ticket #2
        may only use 225 - 120 = 105s. An implementation that derived the
        slice from `run_budget - reserve` alone would hand it 180s and blow
        past the run deadline.
        """
        first, second = _thread(), _thread()
        run = await _run_budget_scenario(
            run_budget_seconds=400,
            threads=[first, second],
            advances=[175.0, 0.0],
        )

        assert _granted_slice(run, position=1) == pytest.approx(105.0)

    @pytest.mark.asyncio
    async def test_the_reduced_deadline_also_bounds_the_checkpoint(self) -> None:
        """Truncating only the model call would leave dedup and the proposal
        checkpoint free to run on the nominal budget and eat the reserve. The
        84.3s slice must govern the whole ticket: an extraction that burns
        100s of it must make the checkpoint fail its deadline.
        """
        run = await _run_budget_scenario(run_budget_seconds=204.329, advances=[100.0])

        event = next(log for log in run.logs if log["event"] == "extract_ticket_timing")
        assert event["phase"] == "persist"
        assert event["status"] == "timeout"
        # The log event alone says nothing about what the RUN did with it. A
        # started ticket that blew its checkpoint deadline is a timeout, not a
        # deferral: exit 3 puts "extract" in dream.sh's TIMED_OUT_PHASES and
        # the systemd unit into `failed`. Reduced slices make this branch far
        # more reachable, so the reclassification must not be silent.
        assert (run.exit_code, run.record.await_args.kwargs["status"]) == (3, "timeout")

    @pytest.mark.asyncio
    async def test_a_dedup_that_blows_the_reduced_deadline_is_a_run_level_timeout(self) -> None:
        """Same run-level claim for the other checkpoint the slice governs.

        With drafts on the table the corpus dedup is what meets the blown
        deadline first, and that branch carries its own `timed_out` counter.
        """
        run = await _run_budget_scenario(
            run_budget_seconds=204.329, advances=[100.0], drafts_per_ticket=1
        )

        event = next(log for log in run.logs if log["event"] == "extract_ticket_timing")
        assert event["phase"] == "dedup"
        assert (run.exit_code, run.record.await_args.kwargs["status"]) == (3, "timeout")


class TestWetApplyIsNotStarvedByTheGate:
    """The loop must not eat the window the auto-apply needs.

    `BRAIN_DREAM_EXTRACT_DRY_RUN=false` in production, so dream.sh appends
    `--wet` and the phase's real output is APPLIED knowledge, not scanned
    tickets. The finalization reserve is dimensioned for `record_dream_run`
    alone; before this, nothing withheld time for the applies the loop had
    just earned.
    """

    @pytest.mark.asyncio
    async def test_a_full_end_of_window_loop_still_applies_what_it_proposed(self) -> None:
        """Replay of the 2026-08-07 night under `--wet`: ticket #1 exhausts
        its slice (so its checkpoint blows and it yields nothing, exactly as
        production logged), ticket #2 finishes in 155.4s with one proposal,
        and the backlog never runs out. That single proposal is the whole
        output of the phase — production recorded `applied|1` that day.

        A loop free to run until `remaining` meets the reserve exactly lands
        the wet block on `remaining == 120.0`, defers every apply, and strands
        that proposal as `proposed` forever, recoverable only by hand.
        """
        threads = [_thread() for _ in range(4)]
        run = await _run_budget_scenario(
            run_budget_seconds=540,
            threads=threads,
            advances=[180.0, 155.4, 84.6, 0.0],
            wet=True,
            proposals_per_ticket=1,
        )

        assert [call.args[1] for call in run.apply.await_args_list] == [[2]]

    @pytest.mark.asyncio
    async def test_the_wet_allowance_is_withheld_from_the_granted_slice(self) -> None:
        """250s left, `--wet`: 120s reserve plus a 30s wet allowance leaves
        100s of usable slice. Without the allowance the ticket would take 130s
        and hand the applies nothing.
        """
        run = await _run_budget_scenario(run_budget_seconds=250, wet=True)

        assert _granted_slice(run) == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_a_dry_run_pays_no_wet_allowance(self) -> None:
        """A dry run applies nothing, so withholding apply time from it would
        cost tickets for no reason. 200s left: dry starts on 80s, wet does not
        start at all because 50s is under the floor.
        """
        dry = await _run_budget_scenario(run_budget_seconds=200, wet=False)
        wet = await _run_budget_scenario(run_budget_seconds=200, wet=True)

        assert dry.extract.await_count == 1
        assert _granted_slice(dry) == pytest.approx(80.0)
        assert wet.extract.await_count == 0

    @pytest.mark.asyncio
    async def test_the_gate_event_names_the_wet_allowance(self) -> None:
        """`usable_slice_s` now has two subtrahends. Without naming the second
        one, nobody reading the log can reconstruct why the gate closed.
        """
        run = await _run_budget_scenario(run_budget_seconds=179.9, wet=True)

        event = next(log for log in run.logs if log["event"] == "extract_gate_closed")
        assert event["wet_allowance_s"] == pytest.approx(30.0)
        assert event["usable_slice_s"] == pytest.approx(29.9)

    @pytest.mark.asyncio
    async def test_the_wet_budget_constants_are_governed_module_constants(self) -> None:
        """Measured on four production nights (2026-08-03..07, extract logs):
        applying 16 proposals took at most 2s of wall clock. 30s is a 15x
        provision; 5s is the floor under which handing an apply a budget only
        cancels a healthy write and bills it as a timeout.
        """
        from scripts.ticket_extract import (  # noqa: PLC0415
            _MIN_WET_APPLY_SECONDS,
            _WET_APPLY_ALLOWANCE_SECONDS,
        )

        assert (_WET_APPLY_ALLOWANCE_SECONDS, _MIN_WET_APPLY_SECONDS) == (30.0, 5.0)

    @pytest.mark.asyncio
    async def test_an_apply_is_never_handed_a_sliver_of_budget(self) -> None:
        """Each apply burns 209s here, so the third would be started with
        122 - 120 = 2s. `asyncio.wait_for` would cancel that healthy write
        mid-flight and bill it a timeout; declining to start it is the
        conservative outcome.
        """
        run = await _run_budget_scenario(
            run_budget_seconds=540,
            wet=True,
            proposals_per_ticket=3,
            apply_advance=209.0,
        )

        assert run.apply.await_count == 2

    @pytest.mark.asyncio
    async def test_a_declined_wet_apply_is_a_deferral_not_a_timeout(self) -> None:
        """Same distinction as a never-started ticket (ticket 572220e9): an
        apply that never began exceeded no deadline. Exit 4 and a `done`
        dream_run, not exit 3 and a failure in front of the operator.
        """
        run = await _run_budget_scenario(
            run_budget_seconds=540,
            wet=True,
            proposals_per_ticket=3,
            apply_advance=209.0,
        )

        assert (run.exit_code, run.record.await_args.kwargs["status"]) == (4, "done")

    @pytest.mark.asyncio
    async def test_the_wet_deferral_names_the_proposals_it_left_behind(self) -> None:
        """An un-applied proposal is only recoverable by `--apply-ids`: the
        next run auto-applies nothing but the ids it created itself. Printing
        "deferred" without the ids makes that recovery a log-archaeology job.
        """
        run = await _run_budget_scenario(
            run_budget_seconds=540,
            wet=True,
            proposals_per_ticket=3,
            apply_advance=209.0,
        )

        event = next(log for log in run.logs if log["event"] == "extract_wet_deferred")
        assert event["wet_budget_s"] == pytest.approx(2.0)
        assert event["unapplied_proposal_ids"] == [3]


class TestADeadPrimaryModelFallsBack:
    """On 2026-08-12, `deepseek-ai/deepseek-v4-pro` went 410 Gone.

    The night's 20 tickets failed in 0.907 s out of a 540 s budget: twenty times
    the same definitive error, no fallback, and a report listing twenty distinct
    failures where there was a single cause. `roadmap_curate` already had a primary
    AND a fallback; extract had only a `DEFAULT_MODEL`, and that is the only reason
    roadmap survived the same failure the same morning.
    """

    @pytest.mark.asyncio
    async def test_extract_thread_surfaces_a_dead_model_instead_of_burying_it(self) -> None:
        """Burying the 410 in a ThreadOutcome(failed=True) makes it
        indistinguishable from a malformed ticket — and the loop can then decide
        nothing useful."""
        from scripts.domain_backfill import ModelGoneError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(410, text="retired")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://llm.test") as client:
            with pytest.raises(ModelGoneError):
                await extract_thread(client, "deepseek-ai/deepseek-v4-pro", _thread(), _no_sleep)

    @pytest.mark.asyncio
    async def test_the_run_switches_to_the_fallback_and_keeps_going(self) -> None:
        from scripts.domain_backfill import ModelGoneError

        threads = [_thread(), _thread()]
        used: list[str] = []

        async def extract(client, model, thread, **kw):
            used.append(model)
            if model == "primaire-mort":
                raise ModelGoneError(model, 410)
            return ThreadOutcome(thread=thread, drafts=[])

        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=600.0,
            ticket_budget_seconds=180.0,
        )
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=threads),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", extract),
            patch("scripts.ticket_extract.record_ticket_attempt", AsyncMock()),
            patch("scripts.ticket_extract.persist_proposals", AsyncMock(return_value=[])),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(
                args,
                "secret",
                "primaire-mort",
                "https://llm.test",
                fallback_model="secours-vivant",
            )

        assert used[0] == "primaire-mort", "le primaire est bien tenté en premier"
        assert used[1:] == ["secours-vivant", "secours-vivant"], (
            "le ticket refusé est REJOUÉ sur le secours, et la bascule tient "
            "pour la suite du run — sinon chaque ticket repaie le 410"
        )
        assert exit_code == 0
        assert record.await_args.kwargs["status"] == "done"

    @pytest.mark.asyncio
    async def test_a_run_without_any_live_model_fails_loudly(self) -> None:
        """If the fallback is dead too, there is nothing left to degrade to: the
        phase must fail LOUDLY, not return an empty `done`."""
        from scripts.domain_backfill import ModelGoneError

        async def extract(client, model, thread, **kw):
            raise ModelGoneError(model, 410)

        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=600.0,
            ticket_budget_seconds=180.0,
        )
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[_thread()]),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", extract),
            patch("scripts.ticket_extract.record_ticket_attempt", AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            exit_code = await _run(
                args,
                "secret",
                "primaire-mort",
                "https://llm.test",
                fallback_model="secours-mort-aussi",
            )

        assert exit_code == 1, "un modèle mort n'est pas une échéance : ce n'est pas un rc=3"
        assert record.await_args.kwargs["status"] == "fail"
        assert "secours-mort-aussi" in (record.await_args.kwargs["error"] or ""), (
            "l'erreur doit NOMMER le dernier modèle essayé, sinon la nuit "
            "suivante rejoue la même impasse à l'aveugle"
        )


class TestTheTerminalRowNamesItsModel:
    """extract's `dream_runs.model`: 53 rows out of 53 at NULL, measured on 19→20.

    Extract is the ONLY phase that can change model MID-RUN — switching to the
    fallback is a run decision, not a ticket one. It is therefore the phase for
    which the column carries the most information, and the only one where the
    configured model is not enough to reconstruct what ran.

    It is also what migration 045 widened the column to hold: the configured WET
    fallback is 33 characters, against the previous 30.
    """

    @pytest.mark.asyncio
    async def test_the_terminal_row_names_the_model_that_actually_ran(self) -> None:
        async def extract(client, model, thread, **kw):
            return SimpleNamespace(
                drafts=[], failed=False, error=None, timed_out=False, duration_s=0.1
            )

        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=600.0,
            ticket_budget_seconds=180.0,
        )
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[_thread()]),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", extract),
            patch("scripts.ticket_extract.record_ticket_attempt", AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            await _run(args, "secret", "primaire-vivant", "https://llm.test")

        assert record.await_args.kwargs["model"] == "primaire-vivant"

    @pytest.mark.asyncio
    async def test_a_fallback_switch_is_recorded_as_the_model_that_finished(self) -> None:
        """The model WRITTEN is the one that finished the run, not the one that opened it.

        Writing the primary here would make a night served entirely by the
        fallback indistinguishable from a nominal night — the exact failure mode
        that left qwen 80B dead through ten green nights.
        """
        from scripts.domain_backfill import ModelGoneError

        seen: list[str] = []

        async def extract(client, model, thread, **kw):
            seen.append(model)
            if model == "primaire-mort":
                raise ModelGoneError(model, 410)
            return SimpleNamespace(
                drafts=[], failed=False, error=None, timed_out=False, duration_s=0.1
            )

        args = SimpleNamespace(
            apply_ids=None,
            limit=20,
            wet=False,
            run_budget_seconds=600.0,
            ticket_budget_seconds=180.0,
        )
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[_thread()]),
            ),
            patch("scripts.ticket_extract._extract_thread_with_budget", extract),
            patch("scripts.ticket_extract.record_ticket_attempt", AsyncMock()),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            await _run(
                args,
                "secret",
                "primaire-mort",
                "https://llm.test",
                fallback_model="secours-vivant",
            )

        assert seen[0] == "primaire-mort"
        assert record.await_args.kwargs["model"] == "secours-vivant"

    @pytest.mark.asyncio
    async def test_a_run_that_never_called_a_model_records_no_model(self) -> None:
        """No pending ticket: NO model call, so the column stays NULL.

        Writing the CONFIGURED model there would be a lie — the row would say a
        model ran when nothing called one. `NULL` means "none", and that is
        information, not an omission.
        """
        args = SimpleNamespace(apply_ids=None, limit=20, wet=False)
        record = AsyncMock()
        with (
            patch("brain_v42.config.Settings") as settings_cls,
            patch("brain_v42.db.engine.get_session_factory", return_value=MagicMock()),
            patch(
                "scripts.ticket_extract.fetch_pending_threads",
                new=AsyncMock(return_value=[]),
            ),
            patch("scripts.ticket_extract.record_dream_run", record),
        ):
            settings_cls.return_value.embedding_service_url = "http://embedding.test"
            await _run(args, "secret", "primaire-vivant", "https://llm.test")

        assert record.await_args.kwargs.get("model") is None


class TestTheCorrectiveRepromptCarriesTheError:
    """extract's re-prompt was BLIND — it asked again without saying what for.

    `roadmap_curate` has always passed the precise error through
    (`_curate_llm_attempt`); extract settled for the generic instruction "your
    answer was not a valid JSON array". A model that returned the wrong PROJECT
    KEY therefore re-reads "return valid JSON" and returns the same JSON, valid,
    with the same wrong key. That is what makes the 19→20 failure reproducible
    identically rather than recoverable on the second attempt.

    No test pinned this re-prompt's content, in either module. This one does, for
    extract.
    """

    @pytest.mark.asyncio
    async def test_the_reprompt_names_the_precise_error_and_the_valid_keys(self) -> None:
        from scripts import ticket_extract as mod

        sent: list[list[dict[str, str]]] = []
        bad = (
            '[{"target_type": "learning", "target_project": "red-lab", '
            '"payload": {"topic": "t", "insight": "i", "tags": []}, "rationale": "r"}]'
        )

        async def fake_post_chat(client, model, messages, sleep, **kw):
            sent.append(messages)
            return (bad, {}) if len(sent) == 1 else ("[]", {})

        with patch("scripts.ticket_extract._post_chat", fake_post_chat):
            outcome = await extract_thread(MagicMock(), "m", _thread(), sleep=_no_sleep)

        assert outcome.failed is False, "le second essai est valide"
        assert len(sent) == 2, "un re-prompt correctif, exactement"

        reprompt = sent[1][-1]["content"]
        assert mod._REPROMPT_INSTRUCTION in reprompt, "l'instruction générique reste"
        assert "target_project" in reprompt, "l'erreur précise doit voyager"
        assert "red-lab" in reprompt, "le modèle doit lire CE QU'IL a proposé"
        # The valid keys travel FOR FREE: `parse_and_validate`'s message already
        # enumerates them. Without the error, they never reached the model.
        assert "red-shrik" in reprompt and "red-data" in reprompt

    @pytest.mark.asyncio
    async def test_a_second_failure_still_reports_the_second_error(self) -> None:
        """The failure contract does not change: it is the SECOND error that surfaces."""

        async def fake_post_chat(client, model, messages, sleep, **kw):
            return ("pas du json", {})

        with patch("scripts.ticket_extract._post_chat", fake_post_chat):
            outcome = await extract_thread(MagicMock(), "m", _thread(), sleep=_no_sleep)

        assert outcome.failed is True
        assert outcome.error is not None
        assert outcome.error.startswith("unparseable after corrective re-prompt: ")


class TestThePrimaryModelIsAliveRatherThanRetired:
    """`deepseek-ai/deepseek-v4-pro` died on 2026-08-12 and stayed dead.

    MEASURED on 2026-08-21 with `scripts/probe_model_liveness.py`, the probe
    written for exactly this question: `deepseek-ai/deepseek-v4-pro` returns **410
    GONE**, `meta/llama-3.3-70b-instruct` returns **200 ALIVE**. A 410 is not
    transient — no retry will ever repair it.

    For nine days, every extract run therefore paid a round trip to a retired
    model before switching to its fallback. The run DID COMPLETE — the fallback
    chain delivered after the 08-12 failure does its job — but it started with a
    call whose outcome was known in advance.

    `DEFAULT_EXTRACT_FALLBACK_MODEL`'s comment set an explicit condition on this
    promotion: "PROVEN ALIVE, NOT PROVEN GOOD FOR THIS PROMPT […] to be canaried
    before promoting it to primary". The condition is MET, and by the best possible
    proof — a production run on the real prompt. Measured in the database on
    2026-08-21:

        run_date=2026-08-21 phase=extract status=done
        model=meta/llama-3.3-70b-instruct

    That is the first night of the model instrumentation (commit 6148a9c), and it
    names the model that ACTUALLY finished the run. The canary asked for is not a
    16-token probe — it is the whole night.
    """

    def test_the_primary_is_not_the_model_measured_gone(self) -> None:
        from scripts.domain_backfill import DEFAULT_MODEL

        assert DEFAULT_MODEL not in {
            "deepseek-ai/deepseek-v4-pro",
            "meta/llama-3.3-70b-instruct",
        }, (
            "le primaire est un modèle retiré chez le fournisseur : chaque run "
            "paie un 410 certain avant de basculer. deepseek-v4-pro est mort le "
            "2026-08-12 ; llama-3.3-70b entre les nuits du 27 et du 28 août "
            "(extract done le 27, fail 410 le 28, sonde GONE le 29)"
        )

    def test_the_extract_chain_has_two_distinct_living_links(self) -> None:
        """A PROPERTY, not a pin: equality against a literal copied from the diff
        only proved "the commit copied the same string twice" (PR 42 review,
        2026-08-29). What is executably checkable: the chain has two DISTINCT
        links, and neither is a known corpse. The history of the choice (canary
        without persistence on 2026-08-29 through the night's exact path:
        super-120b 3/3, 13 drafts, 16.1 s/ticket; mistral-nemotron 3/3, 15
        drafts, 25.9 s as fallback) lives in the constants' comment, not in an
        assertion paraphrasing it.
        """
        from scripts.domain_backfill import DEFAULT_MODEL
        from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

        dead = {
            "deepseek-ai/deepseek-v4-pro",
            "meta/llama-3.3-70b-instruct",
            "meta/llama-3.1-8b-instruct",
        }

        assert DEFAULT_MODEL != DEFAULT_EXTRACT_FALLBACK_MODEL
        assert DEFAULT_EXTRACT_FALLBACK_MODEL not in dead


class TestAFallbackIdenticalToThePrimaryIsNotAFallback:
    """The collision guard existed, was untested, and starts FIRING.

    Promoting the fallback to primary makes the two constants equal. The code
    already anticipated the case — "a fallback identical to the primary is not a
    fallback: it would suggest a chain where there is a single point of failure" —
    but nothing checked it, and until then the branch never ran. It was the NOMINAL
    path from 2026-08-21 to 2026-08-29.

    Since 2026-08-29 the chain has two distinct links again, both canaried on the
    real extraction prompt against real tickets (mistral-nemotron fallback: 3/3
    valid, 15 drafts, 25.9 s/ticket). The equality guard stays covered here through
    the env path: it must cancel an explicit collision, never the nominal chain.
    """

    @staticmethod
    def _resolved_fallback(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str | None:
        import asyncio as _asyncio

        from scripts import ticket_extract as te

        captured: dict[str, str | None] = {}

        async def _fake_run(*_args: object, **kwargs: object) -> int:
            captured["fallback_model"] = kwargs.get("fallback_model")  # type: ignore[assignment]
            return 0

        monkeypatch.setattr(te, "_run", _fake_run)
        monkeypatch.setattr(te, "load_env_file", lambda *_a, **_k: None)
        monkeypatch.setattr(te.sys, "argv", ["ticket_extract"])
        for key in ("BRAIN_NVIDIA_MODEL", "BRAIN_NVIDIA_FALLBACK_MODEL", "BRAIN_NVIDIA_BASE_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("BRAIN_NVIDIA_API_KEY", "unused-in-this-path")
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        assert te.main() == 0
        assert _asyncio is not None
        return captured["fallback_model"]

    def test_the_default_chain_resolves_two_distinct_links(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default is a real chain again: the fallback survives the resolver."""
        from scripts.domain_backfill import DEFAULT_MODEL
        from scripts.ticket_extract import DEFAULT_EXTRACT_FALLBACK_MODEL

        resolved = self._resolved_fallback(monkeypatch, {})

        assert resolved == DEFAULT_EXTRACT_FALLBACK_MODEL
        assert resolved != DEFAULT_MODEL

    def test_a_fallback_equal_to_the_primary_is_annulled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scripts.domain_backfill import DEFAULT_MODEL

        assert (
            self._resolved_fallback(monkeypatch, {"BRAIN_NVIDIA_FALLBACK_MODEL": DEFAULT_MODEL})
            is None
        ), (
            "un secours égal au primaire ferait croire à une chaîne à deux "
            "maillons là où il n'y a qu'un seul point de panne"
        )

    def test_a_genuinely_distinct_fallback_still_survives(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative witness: the guard must cancel on EQUALITY, not always.

        Without it, a broken guard returning `None` in all circumstances would pass
        the test above, and the fallback chain would be silently dead the day it
        was given a second model again.
        """
        resolved = self._resolved_fallback(
            monkeypatch, {"BRAIN_NVIDIA_FALLBACK_MODEL": "mistralai/mistral-nemotron"}
        )

        assert resolved == "mistralai/mistral-nemotron"
