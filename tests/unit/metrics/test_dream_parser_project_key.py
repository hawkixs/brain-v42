"""BOTH parsers require `--project-key` and carry it down to the SQL bind.

There is only one `INSERT` site (`dream_parser._insert_dream_run`) but two
command-line entry points, and `dream.sh` builds a SINGLE argument array for them
(`dream.sh:315-317`). The rail actually taken in production is
`codex_dream_parser` — `BRAIN_DREAM_AGENT_PROVIDER` is `codex` by default
(`dream.sh:19`) — and until now it had no CLI test at all.

Teaching the flag to `dream_parser` alone therefore repairs the fallback rail and
breaks the live one: `argparse` exits 2, `set -euo pipefail` propagates, and
`dream.sh` logs `WARN codex_dream_parser failed for $name (non-fatal)`. The night
loses its six rows per project, silently. That is what these tests forbid.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from brain_v42.metrics import codex_dream_parser, dream_parser

_INSERT = re.compile(
    r"INSERT\s+INTO\s+dream_runs\s*\((?P<cols>[^)]*)\)\s*VALUES\s*\((?P<vals>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


def _insert_shape(sql: str) -> tuple[list[str], list[str]]:
    """Columns and placeholders of the INSERT, in the order they are written."""
    match = _INSERT.search(sql)
    assert match is not None, f"INSERT dream_runs introuvable dans : {sql}"
    columns = [part.strip() for part in match.group("cols").split(",")]
    placeholders = [part.strip() for part in match.group("vals").split(",")]
    return columns, placeholders


_COMMON = [
    "--phase",
    "scan",
    "--model",
    "gpt-5.6-sol",
    "--date",
    "2026-08-09",
    "--status",
    "done",
    "--duration",
    "12",
]


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_both_parsers_refuse_to_run_without_a_project_key(module: object) -> None:
    with pytest.raises(SystemExit) as excinfo:
        module._build_arg_parser().parse_args([*_COMMON, "log.txt"])

    assert excinfo.value.code == 2


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_neither_parser_offers_a_default_project_key(module: object) -> None:
    """A `brain-v42` default is exactly the class of bug targeted: it would label
    another project's night with nothing flagging it."""
    action = next(
        candidate
        for candidate in module._build_arg_parser()._actions
        if "--project-key" in candidate.option_strings
    )

    assert action.required is True
    assert action.default is None


@pytest.mark.parametrize("module", [dream_parser, codex_dream_parser], ids=["claude", "codex"])
def test_both_parsers_accept_the_flag_dream_sh_will_pass(module: object) -> None:
    args = module._build_arg_parser().parse_args(
        [*_COMMON, "--project-key", "red-shrik", "log.txt"]
    )

    assert args.project_key == "red-shrik"


@pytest.mark.asyncio
async def test_insert_dream_run_binds_the_project_key_before_phase_dry_run() -> None:
    """`project_key` is `$14`, `phase_dry_run` stays the LAST one bound.

    The order is not cosmetic: `test_dream_parser_phase_dry_run` pins
    `bind_args[-1] is True` to prove the dry-run flag is not lost. Inserting the
    key at the end of `VALUES` would turn that pin red for a reason unrelated to
    what it guards.
    """
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()
    mock_conn.close = AsyncMock()

    with patch(
        "brain_v42.metrics.dream_parser.asyncpg.connect", new=AsyncMock(return_value=mock_conn)
    ):
        await dream_parser._insert_dream_run(
            run_date="2026-08-09",
            phase="scan",
            model="gpt-5.6-sol",
            status="done",
            duration_s=12.0,
            telemetry=None,
            error_message=None,
            phase_dry_run=True,
            project_key="red-shrik",
        )

    sql, *bind_args = mock_conn.execute.await_args.args
    columns, placeholders = _insert_shape(sql)

    # Column ↔ placeholder ↔ argument, joined — never read as three separate
    # facts. The four `sa.text` writers have this guard (the zip in
    # `_written_project_key`); this one, which writes the most rows, did not:
    # permuting the column list alone left the assertions green while binding
    # `project_key` to the boolean and `phase_dry_run` to the key.
    # 049: sixteen columns — thinking_tokens slots in before the flag.
    assert len(columns) == len(placeholders) == len(bind_args) == 16
    assert placeholders == [f"${index + 1}" for index in range(16)], (
        "les placeholders doivent être en ordre : sinon l'index d'une colonne "
        "ne dit plus quel argument elle reçoit"
    )
    assert bind_args[columns.index("project_key")] == "red-shrik"
    assert bind_args[columns.index("phase_dry_run")] is True
    assert columns.index("phase_dry_run") == 15, "phase_dry_run reste le dernier lié"
    assert bind_args[columns.index("thinking_tokens")] is None, (
        "telemetry=None : NULL, jamais 0 — « pas mesuré » n'est pas « mesuré nul »"
    )


@pytest.mark.asyncio
async def test_insert_dream_run_demands_the_project_key() -> None:
    """No default at the function level either: both `main()` must supply it,
    otherwise one forgetful caller would go unnoticed."""
    with pytest.raises(TypeError, match="project_key"):
        await dream_parser._insert_dream_run(  # type: ignore[call-arg]
            run_date="2026-08-09",
            phase="scan",
            model="gpt-5.6-sol",
            status="done",
            duration_s=12.0,
            telemetry=None,
        )


@pytest.mark.parametrize(
    ("module", "name"),
    [(dream_parser, "dream_parser"), (codex_dream_parser, "codex_dream_parser")],
    ids=["claude", "codex"],
)
def test_both_mains_forward_the_project_key_to_the_insert(
    module: object, name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """The test that matters: the flag is useless if it stops at `argparse`."""
    inserted = AsyncMock()
    monkeypatch.setattr(module, "_insert_dream_run", inserted)
    monkeypatch.setattr(
        "sys.argv",
        [name, *_COMMON, "--project-key", "red-shrik", str(tmp_path / "absent.log")],
    )

    module.main()

    assert inserted.await_args.kwargs["project_key"] == "red-shrik"
