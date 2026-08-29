"""Verify dream_parser accepts --phase-dry-run and forwards it to the INSERT."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics.dream_parser import _build_arg_parser, _insert_dream_run


def test_phase_dry_run_arg_parses_true():
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--phase",
            "reorg",
            "--model",
            "sonnet",
            "--date",
            "2026-05-14",
            "--status",
            "done",
            "--project-key",
            "brain-v42",
            "--duration",
            "10",
            "--phase-dry-run",
            "true",
            "log.txt",
        ]
    )
    assert args.phase_dry_run is True


def test_phase_dry_run_arg_parses_false():
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--phase",
            "promote",
            "--model",
            "sonnet",
            "--date",
            "2026-05-14",
            "--status",
            "done",
            "--project-key",
            "brain-v42",
            "--duration",
            "10",
            "--phase-dry-run",
            "false",
            "log.txt",
        ]
    )
    assert args.phase_dry_run is False


def test_phase_dry_run_defaults_to_false_when_omitted():
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--phase",
            "scan",
            "--model",
            "sonnet",
            "--date",
            "2026-05-14",
            "--status",
            "done",
            "--project-key",
            "brain-v42",
            "--duration",
            "10",
            "log.txt",
        ]
    )
    assert args.phase_dry_run is False


@pytest.mark.asyncio
async def test_insert_dream_run_passes_phase_dry_run_to_asyncpg():
    """The persistence helper forwards phase_dry_run as the LAST bind param.

    It was `$14` until migration 042's `project_key` slid in ahead of it; it is
    `$15` now. What this test guards is unchanged and is the reason the new
    column was inserted *before* rather than appended: `phase_dry_run` must
    stay the last positional bind and must arrive as a real bool. A silently
    dropped flag would feed `_clean_dry_streak` a night that never rehearsed.
    """
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch(
        "brain_v42.metrics.dream_parser.asyncpg.connect", new=AsyncMock(return_value=mock_conn)
    ):
        await _insert_dream_run(
            run_date="2026-05-14",
            phase="reorg",
            model="sonnet",
            status="done",
            duration_s=12.0,
            telemetry=None,
            project_key="brain-v42",
            error_message=None,
            phase_dry_run=True,
        )

    assert mock_conn.execute.await_count == 1
    sql, *bind_args = mock_conn.execute.await_args.args
    assert "phase_dry_run" in sql
    # 049 : thinking_tokens entre à $15, le drapeau glisse à $16 — et reste
    # le DERNIER lié, ce qui est l'invariant que ce test garde.
    assert "$16" in sql
    assert len(bind_args) == 16
    assert bind_args[-1] is True
