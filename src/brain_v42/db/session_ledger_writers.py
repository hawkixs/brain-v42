"""Declared writers of the ``brain_session_artifacts`` capture ledger.

Why this list exists
--------------------
Session capture is *declared by the client*, never derived.  Nothing on the
server observes an artifact being created and files it under the open session:
the ledger has exactly one runtime writer — ``PgBrainSessionRepo.capture``,
reached only from the explicit ``brain_session_capture`` tool — plus the 037
backfill, which is a migration and not a path in flight.  Two server-side
mechanisms that *are* armed in production, session auto-open and the inactive
sweep, never touch this table.

That property was load-bearing: ``session.attributed_knowledge_ids`` is read as
provenance.  But it was **not** a specification, and the difference has now been
paid out: the roadmap's ``[building]`` line — "heartbeat and capture DERIVED
server-side, or the ritual removed from the contract" — arrived, and the
derivation below was added by editing this list, in the same commit, exactly as
intended.  A test asserting "the server never derives the ledger" would have
forbidden that repair instead.

So this is an allowlist, not a prohibition.  Adding a server-side derivation
stays possible; it just has to add its site to this list in the same commit.
An undeclared new writer turns the census red, and so does a declared entry that
has disappeared.  The point is a reviewed gesture, not a locked door.

Scope and shape
---------------
``DECLARED_SESSION_LEDGER_WRITERS`` holds one entry per writer, formatted
``"<repo-relative path>::<enclosing def>::<operation>"``.  ``<enclosing def>`` is
``<module>`` for module-level code and ``<file>`` for non-Python files, so the
entries survive edits above them.

The census is anchored on the TABLE — never on a tool name or an argument shape,
which has already missed writers on this project.  It reads the versioned tree
only: Python (SQLAlchemy write constructs, including dynamic metadata lookups,
plus SQL embedded in string literals) and raw ``.sql``/``.sh`` text.  A
``CREATE TRIGGER`` on the table counts as a writer, because a trigger *is* a
server-side derivation.

``tests/`` is deliberately outside ``SESSION_LEDGER_WRITER_SCAN_ROOTS``: fixtures
write the ledger and are not derivations.  Widening the roots is itself a
declared gesture — see ``tests/unit/db/test_session_ledger_writers.py``.
"""

from __future__ import annotations

from typing import Final

SESSION_LEDGER_WRITER_SCAN_ROOTS: Final[tuple[str, ...]] = ("src", "alembic", "scripts", "ops")

DECLARED_SESSION_LEDGER_WRITERS: Final[frozenset[str]] = frozenset(
    {
        # The one runtime writer.  Reached only from brain_session_capture:
        # session_lifecycle_tools -> BrainSessionService.capture -> here.
        "src/brain_v42/repositories/pg_brain_session.py::PgBrainSessionRepo.capture::insert",
        # Backfill of pre-v4 attributions (knowledge_type='legacy').  A migration,
        # not a path in flight.
        "alembic/versions/037_session_lifecycle_v4.py::upgrade::insert",
        # The server-side derivation this list was built to make VISIBLE rather
        # than to forbid. It deposits into the `agent` tracer of the current
        # connection, never into a session a human opened, and it never steals:
        # ON CONFLICT DO NOTHING on the ledger's primary key. Closed by default
        # (`brain_session_derived_capture_enabled`).
        "src/brain_v42/db/session_derived_capture.py::derive_capture::insert",
        # The other half of the same mechanism: the user's session ABSORBS the
        # tracer's ledger on its next command. It is an UPDATE of session_id and
        # not an insert, and it is bounded to exactly what an explicit capture
        # would have accepted — same project, created_at >= started_at — so the
        # derivation is never a more permissive path than the command it
        # replaces. Donor is `agent` only; a tracer is never promoted.
        "src/brain_v42/db/session_derived_capture.py::absorb_tracer_ledger::update",
    }
)
