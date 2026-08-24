"""Census the INSERT sites into the six CAPTURE_TABLES against a declared list.

Why this module exists
----------------------
Derived session capture hooks into exactly one site: ``BasePgRepository.create``,
which inserts ``self.table`` — it never names a table, so every repository that
subclasses it is covered for free.  Insert paths that name a capture table
themselves are, by construction, the paths that go *around* that hook.  Measured
today, four of them exist and nothing said so — a derived ledger missing four
producers reads exactly like a complete provenance.  Three are repositories; the
fourth reaches five of the six tables through a dict lookup and is invisible to
any census anchored on a class name, a file name or an argument shape.

This is a census, not a prohibition
-----------------------------------
The three sites below may well be wired into the hook tomorrow — that is the
point of naming them.  So nothing here asserts that a site bypasses the hook, and
nothing asserts how many bypasses there are.  The only assertions are set
equality in both directions against ``DECLARED_CAPTURE_TABLE_INSERT_SITES``:

* a site that appears and is not declared reddens — declare it, or wire it;
* a declared site that disappears reddens — drop the line.

Wiring a bypass into ``create`` therefore means deleting its entry in the same
commit, and the test turns green on the deletion.  A test written as "these three
must stay outside the hook" would instead have forbidden the repair.  On this
project that mistake — a non-regression test written at the moment of the defect,
which then hardens the defect into a specification — has been paid for four times.

Shape
-----
Entries read ``"<repo-relative path>::<enclosing def>::<table>"``, one per
(site, table): a site that writes five capture tables yields five entries, so the
census is readable BY TABLE, which is the only anchor this project trusts.
Recensing by class name, file name or argument shape has already missed writers
here — see ``migrate_neo4j_to_pg`` below, which reaches five capture tables
through a dict lookup and is invisible to every one of those angles.

Two surfaces are covered, because the hardest bypass lives in the second:
SQLAlchemy constructs (``t.insert()``, ``insert(t)``, ``pg_insert(t)``, including
a table reached through a module-local alias or a dict literal) and SQL text
(``INSERT INTO t``, ``COPY t FROM``) in Python literals and in raw ``.sql``/``.sh``
files.

``tests/`` is deliberately outside the scan roots: fixtures insert rows and are
not production producers.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Final

from brain_v42.repositories.pg_brain_session import CAPTURE_TABLES

REPO_ROOT = Path(__file__).resolve().parents[3]

CAPTURE_INSERT_SCAN_ROOTS: Final[tuple[str, ...]] = ("src", "alembic", "scripts", "ops")

#: Anchored on the TABLE, and read from the canonical tuple so that a seventh
#: capture table widens this census without anyone remembering to.
CAPTURE_TABLE_NAMES: Final[frozenset[str]] = frozenset(
    table.name for table, _knowledge_type in CAPTURE_TABLES
)

DECLARED_CAPTURE_TABLE_INSERT_SITES: Final[frozenset[str]] = frozenset(
    {
        # Routable, simply never routed. `PgRunbookRepo.create` already calls
        # `super().create(payload, session=session)` with the payload that
        # `_runbook_create_to_dict` builds; `create_with_promotion` builds the
        # very same payload and then inserts it by hand so the row, the learning
        # stamp and the dream_promotions audit row share one transaction — which
        # `create(..., session=session)` also supports. Nothing here needs to
        # stay outside the hook.
        "src/brain_v42/repositories/pg_runbook.py::PgRunbookRepo.create_with_promotion::runbooks",
        # Routable for the same reason, one step further from it: the values are
        # spelled out at the call site rather than pre-built, and include
        # `number` from `_next_number` and a conditional `sa.func.now()`. Both
        # survive `.values(**data)` unchanged, so this is inertia, not necessity.
        "src/brain_v42/repositories/pg_adr.py::PgADRRepo.create_with_promotion::adrs",
        # The one site genuinely out of reach of the hook as it stands, and the
        # reason the census had to read SQL text and not only SQLAlchemy calls.
        # `PgIndexedPlanRepo` declares no base class, so it has no `create` to
        # route through; and the statement is an UPSERT — `ON CONFLICT
        # (file_path) DO UPDATE`, with `to_tsvector(...)` computed in the VALUES
        # — a shape `BasePgRepository.create` cannot express. Wiring this one
        # means giving the hook an upsert, not moving a call.
        "src/brain_v42/repositories/pg_indexed_plan_repo.py::PgIndexedPlanRepo.upsert_plan_with_chunks::indexed_plans",
        # The Neo4j -> PostgreSQL one-shot migration CLI, reaching five capture
        # tables through `pg_insert(table)` where `table = table_map[entity_type]`.
        # Out of reach the way the 037 backfill is out of reach of the ledger
        # census: it is an operator-run import of rows that predate every
        # session, not a path in flight, and attributing them to whoever ran the
        # import would be a false provenance. It is also the site that no census
        # anchored on a class name, a file name or an argument shape can see.
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py::insert_entity_batch::decisions",
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py::insert_entity_batch::learnings",
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py::insert_entity_batch::snippets",
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py::insert_entity_batch::runbooks",
        "src/brain_v42/scripts/migrate_neo4j_to_pg.py::insert_entity_batch::adrs",
    }
)

_INSERT_CALLABLES: Final[frozenset[str]] = frozenset({"insert", "pg_insert"})
_RAW_TEXT_SUFFIXES: Final[frozenset[str]] = frozenset({".sql", ".sh"})


def _sql_insert_patterns(names: Iterable[str]) -> Mapping[str, re.Pattern[str]]:
    """One pattern per capture table, matching the SQL ways of adding a row."""
    return {
        name: re.compile(
            rf"\binsert\s+into\s+(?:public\.)?{re.escape(name)}\b"
            rf"|\bcopy\s+(?:public\.)?{re.escape(name)}\b[\s\S]{{0,200}}?\bfrom\b",
            re.IGNORECASE,
        )
        for name in names
    }


_SQL_INSERTS: Final[Mapping[str, re.Pattern[str]]] = _sql_insert_patterns(CAPTURE_TABLE_NAMES)


def _sql_tables(text: str) -> frozenset[str]:
    return frozenset(name for name, pattern in _SQL_INSERTS.items() if pattern.search(text))


class _AliasResolver:
    """Bind module-local names to the capture tables they can denote.

    ``migrate_neo4j_to_pg`` reaches five capture tables as ``pg_insert(table)``
    where ``table = table_map[entity_type]``.  Without this pass the census would
    report that ``decisions``, ``learnings`` and ``snippets`` have no direct
    insert site at all — which is exactly the false green this module exists to
    prevent.  The pass is deliberately order-insensitive and unions across
    rebinds: over-reporting a site costs one declared line, under-reporting it
    costs the whole census.
    """

    def __init__(self, tree: ast.AST) -> None:
        self._aliases: dict[str, frozenset[str]] = {}
        for _ in range(2):  # one extra sweep so an alias of an alias resolves
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                denoted = self.tables_of(node.value)
                if not denoted:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._aliases[target.id] = (
                            self._aliases.get(target.id, frozenset()) | denoted
                        )

    def tables_of(self, node: ast.expr | None) -> frozenset[str]:
        """Every capture table ``node`` can denote, however it was reached."""
        if node is None:
            return frozenset()
        if isinstance(node, ast.Name):
            if node.id in CAPTURE_TABLE_NAMES:
                return frozenset({node.id})
            return self._aliases.get(node.id, frozenset())
        if isinstance(node, ast.Attribute):
            return frozenset({node.attr}) if node.attr in CAPTURE_TABLE_NAMES else frozenset()
        if isinstance(node, ast.Subscript):
            index = node.slice
            if isinstance(index, ast.Constant) and index.value in CAPTURE_TABLE_NAMES:
                return frozenset({index.value})
            # A non-constant index selects an unknown entry: every capture table
            # the container can hold is reachable here.
            return self.tables_of(node.value)
        if isinstance(node, ast.Dict):
            return frozenset().union(*(self.tables_of(v) for v in node.values)) or frozenset()
        if isinstance(node, ast.List | ast.Tuple | ast.Set):
            return frozenset().union(*(self.tables_of(e) for e in node.elts)) or frozenset()
        return frozenset()


class _CaptureInsertVisitor(ast.NodeVisitor):
    """Collect ``qualname::table`` for every insert reaching a capture table."""

    def __init__(self, aliases: _AliasResolver) -> None:
        self.found: set[str] = set()
        self._aliases = aliases
        self._scope: list[str] = []

    @property
    def _qualname(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

    def _record(self, tables: Iterable[str]) -> None:
        for table in tables:
            self.found.add(f"{self._qualname}::{table}")

    def _enter(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._enter(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, node.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "insert":
            self._record(self._aliases.tables_of(func.value))
        else:
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _INSERT_CALLABLES and node.args:
                self._record(self._aliases.tables_of(node.args[0]))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._record(_sql_tables(node.value))
        self.generic_visit(node)


def _candidate_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix == ".py" or path.suffix in _RAW_TEXT_SUFFIXES:
                yield path


def scan_capture_insert_sites(roots: Iterable[Path], repo_root: Path) -> set[str]:
    """Return ``"<relpath>::<enclosing def>::<table>"`` for every insert site."""
    sites: set[str] = set()
    for path in _candidate_files(roots):
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(name in text for name in CAPTURE_TABLE_NAMES):
            continue
        relative = path.relative_to(repo_root).as_posix()
        if path.suffix == ".py":
            tree = ast.parse(text, filename=str(path))
            visitor = _CaptureInsertVisitor(_AliasResolver(tree))
            visitor.visit(tree)
            sites.update(f"{relative}::{entry}" for entry in visitor.found)
        else:
            sites.update(f"{relative}::<file>::{table}" for table in _sql_tables(text))
    return sites


def test_capture_table_insert_sites_match_declared_list() -> None:
    """Every insert site naming a capture table is declared, and every line is real."""
    roots = [REPO_ROOT / name for name in CAPTURE_INSERT_SCAN_ROOTS]
    observed = scan_capture_insert_sites(roots, REPO_ROOT)

    undeclared = observed - DECLARED_CAPTURE_TABLE_INSERT_SITES
    vanished = DECLARED_CAPTURE_TABLE_INSERT_SITES - observed

    assert not undeclared, (
        "insert site(s) naming a CAPTURE_TABLE that no one declared. This is not a "
        "verdict: route it through BasePgRepository.create, or add it to "
        "DECLARED_CAPTURE_TABLE_INSERT_SITES in the same commit with the reason it "
        f"stays outside: {sorted(undeclared)}"
    )
    assert not vanished, (
        "DECLARED_CAPTURE_TABLE_INSERT_SITES names site(s) that no longer exist — "
        f"wired in, or deleted. Drop the line: {sorted(vanished)}"
    )


def test_scan_roots_all_exist() -> None:
    """A typo in a scan root would silently shrink the census to nothing."""
    for name in CAPTURE_INSERT_SCAN_ROOTS:
        assert (REPO_ROOT / name).is_dir(), f"declared scan root is missing: {name}"


# ---------------------------------------------------------------------------
# The scanner's own reds.  A census whose scanner silently sees nothing is a
# census of nothing, and every shape below is one a real site here uses.
# ---------------------------------------------------------------------------


def test_scanner_detects_sqlalchemy_table_insert(tmp_path: Path) -> None:
    (tmp_path / "repo.py").write_text(
        "from brain_v42.db.tables import runbooks\n"
        "\n"
        "class Repo:\n"
        "    async def promote(self, session):\n"
        "        await session.execute(runbooks.insert().values(x=1))\n",
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == {"repo.py::Repo.promote::runbooks"}


def test_scanner_detects_insert_construct_call(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text(
        "def save(session):\n    session.execute(pg_insert(adrs).values(x=1))\n",
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == {"svc.py::save::adrs"}


def test_scanner_detects_raw_sql_upsert(tmp_path: Path) -> None:
    """PgIndexedPlanRepo's shape: the whole reason SQL text is a scanned surface."""
    (tmp_path / "plans.py").write_text(
        "def upsert(session):\n"
        '    session.execute(text("""\n'
        "        INSERT INTO indexed_plans (file_path) VALUES (:file_path)\n"
        "        ON CONFLICT (file_path) DO UPDATE SET title = EXCLUDED.title\n"
        '    """))\n',
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == {"plans.py::upsert::indexed_plans"}


def test_scanner_detects_table_reached_through_a_dict_lookup(tmp_path: Path) -> None:
    """migrate_neo4j_to_pg's shape: the site every other angle misses."""
    (tmp_path / "migrate.py").write_text(
        "def run(session, entity_type):\n"
        '    table_map = {"Decision": decisions, "ADR": adrs}\n'
        "    table = table_map[entity_type]\n"
        "    session.execute(pg_insert(table).values(x=1))\n",
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == {
        "migrate.py::run::decisions",
        "migrate.py::run::adrs",
    }


def test_scanner_detects_insert_from_sql_and_shell_files(tmp_path: Path) -> None:
    (tmp_path / "seed.sql").write_text(
        "INSERT INTO public.learnings (topic) VALUES ('x');\n", encoding="utf-8"
    )
    (tmp_path / "load.sh").write_text(
        '#!/usr/bin/env bash\npsql -c "COPY snippets FROM STDIN"\n', encoding="utf-8"
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == {
        "seed.sql::<file>::learnings",
        "load.sh::<file>::snippets",
    }


def test_scanner_ignores_the_generic_hook_and_non_capture_tables(tmp_path: Path) -> None:
    """`self.table.insert()` names no table: the hook is covered, not a bypass.

    A near-miss table name must not be caught either, or the census drowns.
    """
    (tmp_path / "base.py").write_text(
        "from brain_v42.db.tables import decisions\n"
        "\n"
        "class BaseRepo:\n"
        "    async def create(self, sess, data):\n"
        "        await sess.execute(self.table.insert().values(**data))\n"
        "        await sess.execute(dream_decisions.insert().values(**data))\n"
        '        await sess.execute(text("INSERT INTO indexed_plan_chunks (x) VALUES (1)"))\n',
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == set()


def test_scanner_ignores_reads_updates_and_ddl(tmp_path: Path) -> None:
    (tmp_path / "reader.py").write_text(
        "def load(session):\n"
        "    session.execute(sa.select(adrs))\n"
        "    session.execute(runbooks.update().values(x=1))\n"
        "    session.execute(learnings.delete())\n",
        encoding="utf-8",
    )
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE decisions (id uuid PRIMARY KEY);\n"
        "SELECT count(*) FROM snippets;\n"
        "DROP TABLE IF EXISTS adrs;\n",
        encoding="utf-8",
    )
    assert scan_capture_insert_sites([tmp_path], tmp_path) == set()
