"""``run_cleanup_loop``'s `# nosec B608` rests on an invariant, not an opinion.

The metrics sidecar purges `process_metrics` with an f-string:

    text(f"DELETE FROM process_metrics WHERE {PROCESS_METRICS_STALE_SQL}")

Bandit flags it (B608, MEDIUM/LOW) because it sees an interpolation inside SQL. It is harmless
for ONE precise reason, and that reason must stay true: the only interpolated fragment is a
module constant built at import time from a literal integer, in a module
(`brain_v42.metrics.retention`) that imports nothing.

The `runtime.py` site deserves its own guardrail rather than a test shared with `flusher.py`:
this one lives in a function that receives parameters (`session_factory`, `interval`), so the
day someone wants to make the window configurable, HERE is where they will wire it. The test
must then fail on that file.

These tests guard the chain link by link. They fail if anyone makes the constant dynamic —
`int(os.environ[...])`, a `Settings`, a call parameter — or if a second fragment appears in the
query. The day that happens, the nosec becomes a lie, and this test is what must say so, not
production.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "brain_v42" / "metrics"
_TARGET = _SRC / "runtime.py"
_RETENTION = _SRC / "retention.py"

_DELETE_MARKER = "DELETE FROM process_metrics"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _sole_interpolated_delete(tree: ast.Module) -> ast.JoinedStr:
    """The module's only f-string that writes a DELETE on process_metrics."""
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        and any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and _DELETE_MARKER in part.value
            for part in node.values
        )
    ]
    assert len(found) == 1, (
        f"{_TARGET.name} doit contenir exactement une f-string de purge process_metrics, "
        f"il en contient {len(found)} — le nosec B608 ne couvre qu'un site connu"
    )
    return found[0]


def test_the_purge_interpolates_only_the_retention_constant() -> None:
    """The f-string's only hole is a name imported from brain_v42.metrics.retention.

    That is the heart of the nosec: no call, no attribute, no local variable, so no
    value computed at runtime can enter the query.
    """
    tree = _parse(_TARGET)

    from_retention = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "brain_v42.metrics.retention"
        for alias in node.names
    }
    assert from_retention, "runtime.py n'importe plus rien de brain_v42.metrics.retention"

    holes = [
        part
        for part in _sole_interpolated_delete(tree).values
        if isinstance(part, ast.FormattedValue)
    ]
    assert len(holes) == 1, (
        f"la purge doit interpoler exactement un fragment, elle en interpole {len(holes)}"
    )

    (hole,) = holes
    assert isinstance(hole.value, ast.Name), (
        "le fragment interpolé doit être un simple nom de constante importée, pas "
        f"{type(hole.value).__name__} — un appel ou un attribut peut porter une entrée"
    )
    assert hole.value.id in from_retention, (
        f"{hole.value.id} n'est pas importé de brain_v42.metrics.retention : le nosec B608 "
        "affirme une provenance qui n'est plus vraie"
    )
    assert hole.format_spec is None and hole.conversion == -1, (
        "ni format_spec ni conversion sur ce trou : tout ajout signale une valeur travaillée"
    )


def test_the_imported_constant_is_never_rebound_in_the_module() -> None:
    """The imported name is never reassigned, otherwise the previous check is bypassable.

    A local assignment ``PROCESS_METRICS_STALE_SQL = <received value>`` right before the
    query would leave the AST identical while letting an input into the SQL.
    """
    rebinds = [
        node.lineno
        for node in ast.walk(_parse(_TARGET))
        if isinstance(node, ast.Name)
        and node.id == "PROCESS_METRICS_STALE_SQL"
        and isinstance(node.ctx, ast.Store)
    ]
    assert rebinds == [], (
        f"PROCESS_METRICS_STALE_SQL est réaffecté aux lignes {rebinds} de {_TARGET.name} : "
        "le fragment interpolé ne vient plus forcément de brain_v42.metrics.retention"
    )


def test_the_retention_window_is_an_integer_literal_not_a_runtime_value() -> None:
    """PROCESS_METRICS_RETENTION_SECONDS is a literal integer, written in the source.

    RED if anyone writes ``= int(os.environ["..."])`` or ``= get_settings().x``: the value
    would become drivable from outside the repository and the interpolation would stop
    being safe.
    """
    assignments = [
        node
        for node in _parse(_RETENTION).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "PROCESS_METRICS_RETENTION_SECONDS"
            for t in node.targets
        )
    ]
    assert len(assignments) == 1, (
        "PROCESS_METRICS_RETENTION_SECONDS doit être affecté une seule fois, au niveau module"
    )
    value = assignments[0].value
    assert isinstance(value, ast.Constant) and isinstance(value.value, int), (
        "PROCESS_METRICS_RETENTION_SECONDS doit rester un littéral entier ; il vaut "
        f"{ast.dump(value)} — une valeur calculée à l'exécution invalide le nosec B608"
    )


def test_the_retention_module_imports_nothing_that_could_carry_an_input() -> None:
    """retention.py imports nothing (beyond `__future__`), so nothing external can enter it.

    A deliberately wide guard: `os`, `brain_v42.config`, `json`… have no business in a
    module of two constants, and their arrival is exactly the signal to re-read the two
    B608 nosec that lean on it.
    """
    offenders = [
        ast.unparse(node)
        for node in _parse(_RETENTION).body
        if isinstance(node, ast.Import | ast.ImportFrom)
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    assert offenders == [], (
        f"brain_v42.metrics.retention ne doit rien importer, il importe {offenders} : "
        "relire les nosec B608 de flusher.py et runtime.py avant de garder cet import"
    )


def test_the_stale_predicate_holds_no_free_text() -> None:
    """The constant actually loaded contains only literal SQL and an integer."""
    from brain_v42.metrics.retention import PROCESS_METRICS_STALE_SQL

    assert re.fullmatch(
        r"updated_at < NOW\(\) - INTERVAL '\d+ seconds'", PROCESS_METRICS_STALE_SQL
    ), (
        "PROCESS_METRICS_STALE_SQL doit rester un prédicat figé sur un entier ; il vaut "
        f"{PROCESS_METRICS_STALE_SQL!r}"
    )


def test_the_nosec_names_b608_on_the_line_bandit_reports() -> None:
    """The nosec is targeted and placed on the exact line, never bare.

    A `# nosec` without an identifier would switch off EVERY check on the line: that is the
    blanket rule the operator decision of 2026-08-16 forbids.
    """
    lines = _TARGET.read_text(encoding="utf-8").splitlines()
    delete_line = lines[_sole_interpolated_delete(_parse(_TARGET)).lineno - 1]

    assert "# nosec B608" in delete_line, (
        "la ligne du DELETE doit porter son propre `# nosec B608` ; bandit rapporte cette "
        f"ligne-là et aucune autre. Ligne lue : {delete_line.strip()!r}"
    )
    bare = [n for n, line in enumerate(lines, 1) if re.search(r"#\s*nosec(?![:\s]*B\d)", line)]
    assert bare == [], f"nosec sans identifiant de test aux lignes {bare} de {_TARGET.name}"
