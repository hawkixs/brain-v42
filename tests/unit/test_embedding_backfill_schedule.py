"""Contract for the inactive, bounded embedding-backfill cadence."""

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_backfill_timer_is_daily_and_not_enabled_by_the_template() -> None:
    timer = (ROOT / "deploy/systemd/brain-v42-embedding-backfill.timer").read_text()
    service = (ROOT / "deploy/systemd/brain-v42-embedding-backfill.service.tmpl").read_text()

    assert "OnCalendar=*-*-* 04:30:00" in timer
    assert "--project-key brain-v42" in service
    assert "--entity-type learning --entity-type decision" in service
    assert "--batch-size 20 --limit 100" in service
    assert "WantedBy=" not in service
    installer = (ROOT / "deploy/systemd/install.sh").read_text()
    assert "brain-v42-embedding-backfill.service" in installer
    assert "brain-v42-embedding-backfill.timer" in installer
