"""Unit tests for AccessLogger — bounded queue + batch consumer."""

from __future__ import annotations

import unittest.mock
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.access_logger import AccessLogger


class TestLogAccess:
    @pytest.mark.asyncio
    async def test_log_access_enqueues_event(self) -> None:
        """log_access puts event on the queue."""
        logger = AccessLogger(session_factory=MagicMock())
        logger.log_access("decision", uuid.uuid4(), "search_hit")
        assert logger._queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_log_access_drops_on_full_queue(self) -> None:
        """log_access silently drops when queue is full."""
        logger = AccessLogger(session_factory=MagicMock(), max_queue_size=2)
        for _ in range(5):
            logger.log_access("decision", uuid.uuid4(), "search_hit")
        assert logger._queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_log_access_event_structure(self) -> None:
        """Enqueued event has the expected structure."""
        logger = AccessLogger(session_factory=MagicMock())
        entity_id = uuid.uuid4()
        logger.log_access("learning", entity_id, "get_by_id")

        event = logger._queue.get_nowait()
        assert event["entity_type"] == "learning"
        assert event["entity_id"] == entity_id
        assert event["access_type"] == "get_by_id"


class TestFlushBatch:
    @pytest.mark.asyncio
    async def test_flush_batch_inserts_to_db(self) -> None:
        """_flush_batch inserts queued events via session."""
        session = AsyncMock()
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        session_factory = MagicMock(return_value=ctx_manager)

        logger = AccessLogger(session_factory=session_factory)
        logger.log_access("learning", uuid.uuid4(), "get_by_id")

        await logger._flush_batch()

        session.execute.assert_called_once()
        session.commit.assert_called_once()
        assert logger._queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_flush_batch_noop_on_empty_queue(self) -> None:
        """_flush_batch does nothing when queue is empty."""
        session_factory = MagicMock()
        logger = AccessLogger(session_factory=session_factory)

        await logger._flush_batch()

        session_factory.assert_not_called()


class TestQueueFullLogging:
    def test_log_access_warns_on_queue_full(self) -> None:
        """log_access must emit a warning when queue is full."""
        al = AccessLogger(session_factory=MagicMock(), max_queue_size=1)
        al.log_access("decision", uuid.uuid4(), "search_hit")  # fills queue

        with unittest.mock.patch("brain_v42.services.access_logger.logger") as mock_log:
            al.log_access("decision", uuid.uuid4(), "search_hit")  # overflow
            mock_log.warning.assert_called_once()
            call_args = mock_log.warning.call_args
            assert "queue_full" in call_args[0][0]


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self) -> None:
        """start() creates a background consumer task."""
        logger = AccessLogger(session_factory=MagicMock())
        await logger.start()
        assert logger._task is not None
        assert not logger._task.done()
        await logger.stop()

    @pytest.mark.asyncio
    async def test_stop_flushes_remaining(self) -> None:
        """stop() flushes remaining events before shutting down."""
        session = AsyncMock()
        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=session)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)
        session_factory = MagicMock(return_value=ctx_manager)

        logger = AccessLogger(session_factory=session_factory)
        logger.log_access("decision", uuid.uuid4(), "search_hit")
        # Don't start the loop — just stop (which should flush remaining)
        await logger.stop()

        session.execute.assert_called_once()
