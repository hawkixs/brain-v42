"""Le `# nosec B608` de ``run_cleanup_loop`` repose sur un invariant, pas sur une opinion.

Le sidecar métriques purge `process_metrics` avec une f-string :

    text(f"DELETE FROM process_metrics WHERE {PROCESS_METRICS_STALE_SQL}")

Bandit la signale (B608, MEDIUM/LOW) parce qu'il voit une interpolation dans du SQL. Elle est
inoffensive pour UNE raison précise, et cette raison doit rester vraie : le seul fragment
interpolé est une constante de module construite à l'import à partir d'un littéral entier,
dans un module (`brain_v42.metrics.retention`) qui n'importe rien.

Le site de `runtime.py` mérite son propre garde-fou plutôt qu'un test partagé avec
`flusher.py` : celui-ci vit dans une fonction qui reçoit des paramètres (`session_factory`,
`interval`), donc le jour où quelqu'un voudra rendre la fenêtre configurable, c'est ICI qu'il
la câblera. Le test doit alors échouer sur ce fichier-là.

Ces tests gardent la chaîne maillon par maillon. Ils échouent si quelqu'un rend la constante
dynamique — `int(os.environ[...])`, un `Settings`, un paramètre d'appel — ou si un second
fragment apparaît dans la requête. Le jour où c'est le cas, le nosec devient un mensonge, et
c'est ce test qui doit le dire, pas la production.
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
    """La seule f-string du module qui écrit un DELETE sur process_metrics."""
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
    """Le seul trou de la f-string est un nom importé de brain_v42.metrics.retention.

    C'est le cœur du nosec : pas d'appel, pas d'attribut, pas de variable locale, donc
    aucune valeur calculée à l'exécution ne peut entrer dans la requête.
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
    """Le nom importé n'est jamais réaffecté, sinon le contrôle précédent serait contournable.

    Une affectation locale ``PROCESS_METRICS_STALE_SQL = <valeur reçue>`` juste avant la
    requête laisserait l'AST identique tout en faisant entrer une entrée dans le SQL.
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
    """PROCESS_METRICS_RETENTION_SECONDS est un littéral entier, écrit dans le source.

    RED si quelqu'un écrit ``= int(os.environ["..."])`` ou ``= get_settings().x`` : la valeur
    deviendrait pilotable de l'extérieur du dépôt et l'interpolation cesserait d'être sûre.
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
    """retention.py n'importe rien (hors `__future__`), donc rien d'externe ne peut y entrer.

    Garde volontairement large : `os`, `brain_v42.config`, `json`… n'ont aucune raison d'être
    dans un module de deux constantes, et leur arrivée est exactement le signal qu'il faut
    relire les deux nosec B608 qui s'appuient sur lui.
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
    """La constante effectivement chargée ne contient que du SQL littéral et un entier."""
    from brain_v42.metrics.retention import PROCESS_METRICS_STALE_SQL

    assert re.fullmatch(
        r"updated_at < NOW\(\) - INTERVAL '\d+ seconds'", PROCESS_METRICS_STALE_SQL
    ), (
        "PROCESS_METRICS_STALE_SQL doit rester un prédicat figé sur un entier ; il vaut "
        f"{PROCESS_METRICS_STALE_SQL!r}"
    )


def test_the_nosec_names_b608_on_the_line_bandit_reports() -> None:
    """Le nosec est ciblé et posé sur la ligne exacte, jamais nu.

    Un `# nosec` sans identifiant éteindrait TOUS les contrôles de la ligne : c'est la règle
    blanket que la décision opérateur du 2026-08-16 interdit.
    """
    lines = _TARGET.read_text(encoding="utf-8").splitlines()
    delete_line = lines[_sole_interpolated_delete(_parse(_TARGET)).lineno - 1]

    assert "# nosec B608" in delete_line, (
        "la ligne du DELETE doit porter son propre `# nosec B608` ; bandit rapporte cette "
        f"ligne-là et aucune autre. Ligne lue : {delete_line.strip()!r}"
    )
    bare = [n for n, line in enumerate(lines, 1) if re.search(r"#\s*nosec(?![:\s]*B\d)", line)]
    assert bare == [], f"nosec sans identifiant de test aux lignes {bare} de {_TARGET.name}"
