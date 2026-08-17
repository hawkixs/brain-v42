"""L'acteur doit être capturé à la mise en file, pas au flush."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from brain_v42.provenance import UNKNOWN_ACTOR, set_current_actor
from brain_v42.services.access_logger import AccessLogger


class TestAccessLoggerActor:
    def test_event_carries_current_actor(self) -> None:
        logger = AccessLogger(session_factory=MagicMock())
        set_current_actor("dream-codex-reorg")
        logger.log_access("learning", uuid4(), "get_by_id")

        event = logger._queue.get_nowait()
        assert event["actor"] == "dream-codex-reorg"

    def test_actor_is_frozen_at_enqueue_not_at_flush(self) -> None:
        """Le flush tourne hors contexte de requête : l'acteur doit déjà être figé."""
        logger = AccessLogger(session_factory=MagicMock())

        set_current_actor("red-lab")
        logger.log_access("learning", uuid4(), "get_by_id")
        set_current_actor("dream-codex-synth")
        logger.log_access("learning", uuid4(), "get_by_id")

        first = logger._queue.get_nowait()
        second = logger._queue.get_nowait()
        assert first["actor"] == "red-lab"
        assert second["actor"] == "dream-codex-synth"

    def test_defaults_to_unknown_outside_request(self) -> None:
        logger = AccessLogger(session_factory=MagicMock())
        set_current_actor(UNKNOWN_ACTOR)
        logger.log_access("learning", uuid4(), "search_hit")

        event = logger._queue.get_nowait()
        assert event["actor"] == UNKNOWN_ACTOR
