"""An absent Dream killswitch file means Dream is not deployed, not a fault.

Measured on red-base 2026-10-12 (ticket c206929f): Dream is suspended there and no
killswitch file exists, so the sidecar logged metrics.nightly.killswitches_unreadable
as a warning on every collection, hourly noise that buries real warnings.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from brain_v42.metrics import collector_nightly
from brain_v42.metrics.collector_nightly import _NightlyCollectorsMixin
from brain_v42.metrics.slow_block_cache import CollectorDegraded


class _Collector(_NightlyCollectorsMixin):
    def __init__(self) -> None:
        def unavailable():
            raise OSError("database not needed here")

        self._session_factory = unavailable  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _forget_the_notice(monkeypatch):
    monkeypatch.setattr(collector_nightly, "_KILLSWITCHES_ABSENT_NOTED", False)


async def test_an_absent_file_is_noted_once_and_never_warned(tmp_path: Path) -> None:
    collector = _Collector()
    with capture_logs() as logs:
        for _ in range(3):
            # The database part degrades without a database; only the file matters here.
            with suppress(CollectorDegraded):
                await collector.collect_nightly_ops(killswitches_path=tmp_path / "absent.conf")
    killswitch_events = [log for log in logs if "killswitches" in log["event"]]
    assert killswitch_events == [
        {
            "event": "metrics.nightly.killswitches_absent",
            "log_level": "info",
            "path": str(tmp_path / "absent.conf"),
        }
    ]


async def test_an_unreadable_file_is_still_a_warning(tmp_path: Path) -> None:
    directory = tmp_path / "killswitches.conf"
    directory.mkdir()
    with capture_logs() as logs, suppress(CollectorDegraded):
        await _Collector().collect_nightly_ops(killswitches_path=directory)
    assert [
        log["log_level"] for log in logs if log["event"].endswith("killswitches_unreadable")
    ] == ["warning"]
