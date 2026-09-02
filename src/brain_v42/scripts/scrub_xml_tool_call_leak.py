"""One-shot: scrub stranded XML tool-call fragments from brain entities.

Usage:
    # Dry run (default) — prints what would change, writes nothing
    uv run python -m scripts.scrub_xml_tool_call_leak

    # Apply
    uv run python -m scripts.scrub_xml_tool_call_leak --live

Historical corruption documented in learning 4575ae14. Scrubs:
  learnings.insight
  decisions.context, decisions.decision_made, decisions.reasoning

Re-computes the embedding for any row whose content changes, via
``_embed_text`` from the embedding service, so the FTS + pgvector columns
stay consistent with the scrubbed body.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import sqlalchemy as sa

from brain_v42.db.engine import get_session_factory
from brain_v42.db.focus_history import record_focus_history
from brain_v42.db.tables import decisions, learnings, project_contexts
from brain_v42.maintenance.xml_scrub import scrub_xml_tool_call_leak

_LEARNING_COLS = ("insight",)
_DECISION_COLS = ("description", "reasoning", "consequences")
_PROJECT_CONTEXT_COLS = ("current_focus",)


async def _scrub_table(
    session: sa.ext.asyncio.AsyncSession,  # type: ignore[name-defined]
    table: sa.Table,
    cols: tuple[str, ...],
    live: bool,
    quiet: bool = False,
) -> tuple[int, int]:
    """Return ``(rows_inspected, rows_modified)``."""
    select_cols = [table.c.id] + [getattr(table.c, c) for c in cols]
    stmt = sa.select(*select_cols)
    # Fetch rows whose column contains any of the known leak signatures:
    # `<parameter name=` (the common nested-param leak), `</invoke>` (tool-call
    # close without a param after), `</function_calls>` or `<function_calls>`
    # (the outer wrapper leaking in). Filter here avoids scanning 1900+ rows —
    # actual leak detection is done by the xml_scrub regex afterwards.
    _MARKERS = ("<parameter name=", "</invoke>", "</function_calls>", "<function_calls>")
    conds = []
    for c in cols:
        col = getattr(table.c, c)
        for marker in _MARKERS:
            conds.append(col.ilike(f"%{marker}%"))
    stmt = stmt.where(sa.or_(*conds))

    result = await session.execute(stmt)
    rows = result.fetchall()
    inspected = len(rows)
    modified = 0

    for row in rows:
        updates: dict[str, str] = {}
        for col in cols:
            original = getattr(row, col)
            if original is None:
                continue
            cleaned, was_modified = scrub_xml_tool_call_leak(original)
            if was_modified:
                updates[col] = cleaned

        if not updates:
            continue

        modified += 1
        if not quiet:
            print(f"{table.name}.{row.id}:")
            for col, new_value in updates.items():
                old_len = len(getattr(row, col))
                new_len = len(new_value)
                print(f"  {col}: {old_len} → {new_len} chars (-{old_len - new_len})")

        if live:
            # Clear the embedding only on tables that have one; project_contexts
            # has no embedding column.
            values: dict = dict(updates)
            if "embedding" in table.c:
                values["embedding"] = None
            upd = sa.update(table).where(table.c.id == row.id).values(**values)
            # The seventh focus writer, and the only one outside the MCP surface.
            # It rewrites `current_focus` to strip a leaked tool call — a real
            # mutation of the prose, so it owes the same audit row as the six
            # others, and the deferred constraint trigger would refuse its COMMIT
            # without one. `RETURNING` because the revision is the trigger's to
            # assign, never this script's to guess.
            if table is project_contexts and "current_focus" in updates:
                upd = upd.returning(
                    project_contexts.c.project_key,
                    project_contexts.c.focus_revision,
                    project_contexts.c.current_focus,
                )
                scrubbed = (await session.execute(upd)).mappings().one()
                await record_focus_history(
                    session,
                    project_key=str(scrubbed["project_key"]),
                    focus_revision=int(scrubbed["focus_revision"]),
                    focus=scrubbed["current_focus"],
                    source="maintenance_scrub",
                )
            else:
                await session.execute(upd)

    if live:
        await session.commit()

    return inspected, modified


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Apply changes. Default is dry-run (read-only).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-row output; print only the final one-line summary.",
    )
    args = parser.parse_args()

    if not args.quiet:
        mode = "LIVE (writing)" if args.live else "DRY-RUN (read-only)"
        print(f"=== scrub_xml_tool_call_leak — {mode} ===\n")

    sf = get_session_factory()
    async with sf() as session:
        if not args.quiet:
            print("--- learnings ---")
        l_inspected, l_modified = await _scrub_table(
            session, learnings, _LEARNING_COLS, args.live, quiet=args.quiet
        )
        if not args.quiet:
            print("\n--- decisions ---")
        d_inspected, d_modified = await _scrub_table(
            session, decisions, _DECISION_COLS, args.live, quiet=args.quiet
        )
        if not args.quiet:
            print("\n--- project_contexts ---")
        p_inspected, p_modified = await _scrub_table(
            session, project_contexts, _PROJECT_CONTEXT_COLS, args.live, quiet=args.quiet
        )

    total_inspected = l_inspected + d_inspected + p_inspected
    total_modified = l_modified + d_modified + p_modified
    if args.quiet:
        tag = "LIVE" if args.live else "DRY"
        print(
            f"[scrub_xml_tool_call_leak {tag}] inspected={total_inspected} "
            f"modified={total_modified} "
            f"(learnings={l_modified}/{l_inspected} decisions={d_modified}/{d_inspected} "
            f"project_contexts={p_modified}/{p_inspected})"
        )
        return 0

    print("\n=== summary ===")
    print(
        f"inspected: {total_inspected} ({l_inspected} learnings + {d_inspected} decisions "
        f"+ {p_inspected} project_contexts)"
    )
    print(
        f"modified:  {total_modified} ({l_modified} learnings + {d_modified} decisions "
        f"+ {p_modified} project_contexts)"
    )
    if args.live:
        print("\nEmbeddings cleared on modified rows. Backfill via brain_refresh_entity or")
        print("scripts/backfill_embeddings.py to restore semantic search coverage.")
    else:
        print("\nRe-run with --live to apply.")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
