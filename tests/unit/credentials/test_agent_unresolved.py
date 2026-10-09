"""Count lost project attribution without retaining client or agent identities."""

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def counters() -> Iterator[None]:
    from brain_v42.credentials.agent_unresolved import reset_agent_unresolved_counts

    reset_agent_unresolved_counts()
    yield
    reset_agent_unresolved_counts()


def test_counts_start_empty_and_accumulate_by_reason() -> None:
    from brain_v42.credentials.agent_unresolved import (
        agent_unresolved_counts,
        count_agent_unresolved,
    )

    assert agent_unresolved_counts() == {}
    count_agent_unresolved("not_kebab")
    count_agent_unresolved("not_kebab")
    count_agent_unresolved("unknown_project")
    assert agent_unresolved_counts() == {"not_kebab": 2, "unknown_project": 1}


def test_counts_return_an_independent_snapshot() -> None:
    from brain_v42.credentials.agent_unresolved import (
        agent_unresolved_counts,
        count_agent_unresolved,
    )

    count_agent_unresolved("not_kebab")
    snapshot = agent_unresolved_counts()
    snapshot["not_kebab"] = 99
    assert agent_unresolved_counts() == {"not_kebab": 1}


def test_reset_drops_counts_without_synthetic_events() -> None:
    from brain_v42.credentials.agent_unresolved import (
        agent_unresolved_counts,
        count_agent_unresolved,
        reset_agent_unresolved_counts,
    )

    count_agent_unresolved("unknown_project")
    assert agent_unresolved_counts() == {"unknown_project": 1}
    reset_agent_unresolved_counts()
    assert agent_unresolved_counts() == {}


def test_unknown_reason_is_rejected_without_incrementing() -> None:
    from brain_v42.credentials.agent_unresolved import (
        agent_unresolved_counts,
        count_agent_unresolved,
    )

    with pytest.raises(ValueError, match="invalid agent unresolved reason"):
        count_agent_unresolved("unexpected")  # type: ignore[arg-type]
    assert agent_unresolved_counts() == {}
