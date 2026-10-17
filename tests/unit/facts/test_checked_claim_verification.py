"""The verifier owns admission, the source lock and the verdict transaction."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.db.tables import learnings
from brain_v42.facts import verification
from brain_v42.facts.verification import ClaimVerificationService
from brain_v42.repositories.checked_project import CheckedProjectMismatch
from tests.unit.facts.test_claim_verification import _Probe, _registry


@pytest.mark.parametrize("source_state", ["owned", "moved", "missing"])
async def test_checked_verification_locks_source_before_claim_in_one_transaction(
    monkeypatch, source_state
):
    claim_id, source_id = uuid4(), uuid4()
    session = AsyncMock()
    session.begin = MagicMock(return_value=AsyncMock())
    session.__aenter__.return_value = session
    session.execute.return_value = MagicMock(
        one_or_none=lambda: ("other" if source_state == "moved" else "allowed",)
    )
    verifier = ClaimVerificationService(_registry(_Probe()), MagicMock(return_value=session))

    async def verify_locked(owned_session, *args, **kwargs):
        assert owned_session is session
        assert "FOR UPDATE" in str(session.execute.call_args.args[0])
        return SimpleNamespace(row="verdict")

    verify = AsyncMock(side_effect=verify_locked)
    monkeypatch.setattr(verifier, "_verify", verify)
    monkeypatch.setattr(
        verification,
        "claim_source",
        AsyncMock(return_value=None if source_state == "missing" else (learnings, source_id)),
    )
    call = verifier.verify_checked_source(
        claim_id, "mcp:test-writer", "robot", "retry-key", source_project_key="allowed"
    )
    if source_state == "owned":
        assert await call == "verdict"
        verify.assert_awaited_once()
    else:
        with pytest.raises(CheckedProjectMismatch):
            await call
        verify.assert_not_awaited()
    session.begin.assert_called_once()
