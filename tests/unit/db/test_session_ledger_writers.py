"""Census the writers of ``brain_session_artifacts`` against a declared allowlist.

The capture ledger is written by exactly one runtime path today, and nothing on
the server derives it.  That is a property worth *seeing* change, not a property
worth freezing: the roadmap plans to derive capture server-side.  So this module
does not forbid a writer — it compares the real set to
``DECLARED_SESSION_LEDGER_WRITERS`` and reddens on any drift, in both directions.

The census is anchored on the TABLE, never on a tool name or an argument shape:
on this project, recensing by argument shape has already missed writers.  Two
surfaces are covered — Python (SQLAlchemy constructs and embedded SQL strings)
and raw SQL/shell text.  A ``CREATE TRIGGER`` on the table counts as a writer,
because a trigger *is* a server-side derivation.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from brain_v42.db.session_ledger_writers import (
    DECLARED_SESSION_LEDGER_WRITERS,
    SESSION_LEDGER_WRITER_SCAN_ROOTS,
)

TABLE = "brain_session_artifacts"

REPO_ROOT = Path(__file__).resolve().parents[3]

_PY_WRITE_CALLS = frozenset({"insert", "update", "delete"})
_PY_WRITE_ALIASES = {
    "pg_insert": "insert",
    "insert": "insert",
    "update": "update",
    "delete": "delete",
}

_SQL_WRITES: dict[str, re.Pattern[str]] = {
    "insert": re.compile(rf"\binsert\s+into\s+(?:public\.)?{TABLE}\b", re.IGNORECASE),
    "update": re.compile(rf"\bupdate\s+(?:only\s+)?(?:public\.)?{TABLE}\b", re.IGNORECASE),
    "delete": re.compile(rf"\bdelete\s+from\s+(?:public\.)?{TABLE}\b", re.IGNORECASE),
    "truncate": re.compile(rf"\btruncate\s+(?:table\s+)?(?:public\.)?{TABLE}\b", re.IGNORECASE),
    "copy": re.compile(rf"\bcopy\s+(?:public\.)?{TABLE}\b", re.IGNORECASE),
    "trigger": re.compile(
        rf"\bcreate\s+(?:or\s+replace\s+)?(?:constraint\s+)?trigger\b[\s\S]{{0,600}}?"
        rf"\bon\s+(?:public\.)?{TABLE}\b",
        re.IGNORECASE,
    ),
}

_RAW_TEXT_SUFFIXES = frozenset({".sql", ".sh"})


def _denotes_ledger(node: ast.expr) -> bool:
    """True when ``node`` names the ledger table, however it was reached."""
    if isinstance(node, ast.Name):
        return node.id == TABLE
    if isinstance(node, ast.Attribute):
        return node.attr == TABLE
    if isinstance(node, ast.Subscript):
        index = node.slice
        return isinstance(index, ast.Constant) and index.value == TABLE
    return False


def _sql_operations(text: str) -> set[str]:
    return {operation for operation, pattern in _SQL_WRITES.items() if pattern.search(text)}


class _LedgerWriterVisitor(ast.NodeVisitor):
    """Collect ``qualname::operation`` pairs for every write against the ledger."""

    def __init__(self) -> None:
        self.found: set[str] = set()
        self._scope: list[str] = []

    @property
    def _qualname(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

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
        if isinstance(func, ast.Attribute) and func.attr in _PY_WRITE_CALLS:
            if _denotes_ledger(func.value):
                self.found.add(f"{self._qualname}::{func.attr}")
        else:
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            operation = _PY_WRITE_ALIASES.get(name or "")
            if operation and node.args and _denotes_ledger(node.args[0]):
                self.found.add(f"{self._qualname}::{operation}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            for operation in _sql_operations(node.value):
                self.found.add(f"{self._qualname}::{operation}")
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


def scan_ledger_writers(roots: Iterable[Path], repo_root: Path) -> set[str]:
    """Return ``"<relpath>::<enclosing def>::<operation>"`` for every ledger write."""
    writers: set[str] = set()
    for path in _candidate_files(roots):
        text = path.read_text(encoding="utf-8", errors="replace")
        if TABLE not in text:
            continue
        relative = path.relative_to(repo_root).as_posix()
        if path.suffix == ".py":
            visitor = _LedgerWriterVisitor()
            visitor.visit(ast.parse(text, filename=str(path)))
            writers.update(f"{relative}::{entry}" for entry in visitor.found)
        else:
            writers.update(f"{relative}::<file>::{op}" for op in _sql_operations(text))
    return writers


def test_ledger_writers_match_declared_allowlist() -> None:
    """Every writer of the capture ledger is declared, and every declaration is real."""
    roots = [REPO_ROOT / name for name in SESSION_LEDGER_WRITER_SCAN_ROOTS]
    observed = scan_ledger_writers(roots, REPO_ROOT)

    undeclared = observed - set(DECLARED_SESSION_LEDGER_WRITERS)
    vanished = set(DECLARED_SESSION_LEDGER_WRITERS) - observed

    assert not undeclared, (
        "undeclared writer(s) of brain_session_artifacts — a server-side derivation "
        "is allowed, but it must be added to DECLARED_SESSION_LEDGER_WRITERS in the "
        f"same commit: {sorted(undeclared)}"
    )
    assert not vanished, (
        "DECLARED_SESSION_LEDGER_WRITERS names writer(s) that no longer exist; "
        f"drop them from the list: {sorted(vanished)}"
    )


def test_scan_roots_all_exist() -> None:
    """A typo in a scan root would silently shrink the census to nothing."""
    for name in SESSION_LEDGER_WRITER_SCAN_ROOTS:
        assert (REPO_ROOT / name).is_dir(), f"declared scan root is missing: {name}"


def test_scanner_detects_sqlalchemy_insert(tmp_path: Path) -> None:
    (tmp_path / "writer.py").write_text(
        "from brain_v42.db import brain_session_artifacts\n"
        "\n"
        "class Repo:\n"
        "    async def derive(self, session):\n"
        "        await session.execute(pg_insert(brain_session_artifacts).values(x=1))\n",
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == {"writer.py::Repo.derive::insert"}


def test_scanner_detects_write_through_dynamic_metadata_lookup(tmp_path: Path) -> None:
    (tmp_path / "dyn.py").write_text(
        'def sweep(conn):\n    conn.execute(METADATA.tables["brain_session_artifacts"].delete())\n',
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == {"dyn.py::sweep::delete"}


def test_scanner_detects_embedded_raw_sql(tmp_path: Path) -> None:
    (tmp_path / "mig.py").write_text(
        "def upgrade():\n"
        '    op.execute("INSERT INTO brain_session_artifacts (knowledge_id) VALUES (1)")\n',
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == {"mig.py::upgrade::insert"}


def test_scanner_detects_trigger_in_sql_file(tmp_path: Path) -> None:
    (tmp_path / "derive.sql").write_text(
        "CREATE TRIGGER trg_autocapture\n"
        "AFTER INSERT ON learnings\n"
        "FOR EACH ROW EXECUTE FUNCTION autocapture();\n"
        "CREATE TRIGGER trg_ledger_guard\n"
        "BEFORE INSERT ON public.brain_session_artifacts\n"
        "FOR EACH ROW EXECUTE FUNCTION guard();\n",
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == {"derive.sql::<file>::trigger"}


def test_scanner_detects_write_from_shell(tmp_path: Path) -> None:
    (tmp_path / "nightly.sh").write_text(
        "#!/usr/bin/env bash\n"
        'psql -c "DELETE FROM brain_session_artifacts WHERE captured_at < now()"\n',
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == {"nightly.sh::<file>::delete"}


def test_scanner_ignores_reads_and_ddl(tmp_path: Path) -> None:
    """A reader must not be reported — otherwise the allowlist drowns in noise."""
    (tmp_path / "reader.py").write_text(
        "from brain_v42.db import brain_session_artifacts\n"
        "\n"
        "def load(session):\n"
        "    return session.execute(sa.select(brain_session_artifacts))\n",
        encoding="utf-8",
    )
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE brain_session_artifacts (knowledge_id uuid PRIMARY KEY);\n"
        "SELECT count(*) FROM brain_session_artifacts;\n"
        "DROP TABLE IF EXISTS brain_session_artifacts;\n",
        encoding="utf-8",
    )
    assert scan_ledger_writers([tmp_path], tmp_path) == set()


def test_capture_declares_its_take_over_update() -> None:
    """La reprise par `capture` est un ÉCRIVAIN de plus, pas un détail.

    `capture()` refusait une ligne détenue par une traçante ; elle la REPREND
    désormais quand — et seulement quand — le détenteur est `nature='agent'`.
    C'est un `UPDATE` du ledger, donc un site que l'allowlist doit nommer :
    ce que la règle d'exclusivité refuse d'attribuer reste réparable par un
    humain qui nomme l'UUID, et ce chemin-là doit être vu en revue.
    """
    from brain_v42.db.session_ledger_writers import DECLARED_SESSION_LEDGER_WRITERS

    assert (
        "src/brain_v42/repositories/pg_brain_session.py::PgBrainSessionRepo.capture::update"
        in DECLARED_SESSION_LEDGER_WRITERS
    )
