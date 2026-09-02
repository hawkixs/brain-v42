"""The three `dream_runs.id` lookups filter on the project.

Spec `2026-08-08-dream-project-pool-design.md` §12, the argument that mandates 042
BEFORE the loop: "Three readers WRITE on the row they misidentified:
promote_validate marks `partial` and backfills dream_promotions.dream_run_id,
connect_validate marks `partial`, REORG_RUN_ID likewise. Shipping the loop first
would produce a false and unrepairable promotions audit — the correct attribution
is not recoverable from the rows once written."

All three do `WHERE phase = X AND run_date = Y ORDER BY id DESC LIMIT 1`.

With several projects, that selects the row of the LAST project to have written
that phase today. In the current sequential loop it is fortuitously the right
one: each project writes its row then reads it back immediately. But correctness
then rests on "nobody writes between my write and my read" — an invariant nothing
enforces, that the loop does not declare, and whose violation produces a
`partial` laid on the neighbouring project.

042 is shipped. The filter is available. These tests require it.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from scripts.dream import _promote_helpers, connect_validate


def _rendered(statement: sa.Select) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


def test_promote_run_id_lookup_filters_on_the_project() -> None:
    statement = _promote_helpers.dream_run_id_statement(dt.date(2026, 8, 10), "red")
    rendered = _rendered(statement)

    assert "dream_runs.project_key = 'red'" in rendered
    assert "dream_runs.phase = 'promote'" in rendered


def test_connect_run_id_lookup_filters_on_the_project() -> None:
    statement = connect_validate.connect_run_id_statement(dt.date(2026, 8, 10), "red-lab")
    rendered = _rendered(statement)

    assert "dream_runs.project_key = 'red-lab'" in rendered
    assert "dream_runs.phase = 'connect'" in rendered


def test_the_lookups_still_order_by_id_desc() -> None:
    """The filter is ADDED, it does not replace the re-run disambiguation.

    A project may have two rows on the same day (a manual re-run after an
    outage). `ORDER BY id DESC LIMIT 1` stays the only way to take the last one.
    """
    for statement in (
        _promote_helpers.dream_run_id_statement(dt.date(2026, 8, 10), "red"),
        connect_validate.connect_run_id_statement(dt.date(2026, 8, 10), "red"),
    ):
        rendered = _rendered(statement)
        assert "ORDER BY dream_runs.id DESC" in rendered
        assert "LIMIT 1" in rendered


def test_the_project_key_is_required_on_both_clis() -> None:
    """No default, like the three writers.

    A `default="brain-v42"` here would mark `partial` on brain-v42 while it is
    `red`'s phase that failed — the opposite of what the validator believes it is
    doing, and with no usable trace afterwards.
    """
    import pytest

    with pytest.raises(SystemExit):
        _promote_helpers.main(["dream-run-id", "--date", "2026-08-10"])

    with pytest.raises(SystemExit):
        connect_validate.main(["--report-log", "/dev/null", "--run-date", "2026-08-10"])


def test_the_reorg_lookup_in_dream_sh_filters_on_the_project() -> None:
    """The third lives as inline SQLAlchemy in dream.sh, not in a module.

    It can have no Python witness anywhere but here: it is script text. The anchor
    fails noisily if the query is rewritten without the filter.
    """
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"
    content = source.read_text(encoding="utf-8")

    reorg_query_start = content.index("dream_runs.c.phase == 'reorg'")
    reorg_query = content[reorg_query_start : reorg_query_start + 400]

    assert "dream_runs.c.project_key ==" in reorg_query, (
        "la requête REORG_RUN_ID de dream.sh ne filtre pas sur le projet — "
        "elle marquerait `partial` sur la ligne d'un autre projet du pool"
    )


def test_the_inline_python_in_dream_sh_still_compiles() -> None:
    """The witness that was missing, and whose absence cost a bug.

    This program travels inside `uv run python -c "…"`. It therefore lives at
    COLUMN 0 inside a script whose every other line is indented — and turning the
    phase loop into a function added two spaces to each of its lines. Result:
    `IndentationError`, an empty `REORG_RUN_ID`, a missing `--dream-run-id`, and
    the REORG validator losing its ability to mark the row `partial`.

    Nothing would have seen it. `bash -n` only sees a string. No test executed
    this program. The night would have stayed green while losing a guard.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "scripts" / "dream.sh").read_text(
        encoding="utf-8"
    )
    start = source.index('uv run python -c "') + len('uv run python -c "')
    end = source.index('\n" 2>>', start)
    program = source[start:end]

    # The shell interpolations become plausible literals: we test the program's
    # SHAPE, not the value of the day.
    program = program.replace("$TIMESTAMP", "2026-08-10").replace("$PROJECT_KEY", "brain-v42")

    compile(program, "dream.sh:inline", "exec")


def test_the_dream_script_passes_the_project_to_both_helper_clis() -> None:
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"
    content = source.read_text(encoding="utf-8")

    dream_run_id_call = content[content.index("_promote_helpers dream-run-id") :][:300]
    assert '--project-key "$PROJECT_KEY"' in dream_run_id_call

    connect_call = content[content.index("scripts.dream.connect_validate") :][:400]
    assert '--project-key "$PROJECT_KEY"' in connect_call
