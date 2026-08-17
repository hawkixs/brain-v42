"""Identité du build : quelle version tourne, et quel schéma elle sait jouer.

Les deux faits sont MESURÉS à l'exécution, jamais écrits à la main.

- La version vient du dist-info du paquet installé, pas de `pyproject.toml`.
  La production tourne un install éditable : le dist-info est figé au moment
  du `uv sync`. C'est précisément ce qu'on veut annoncer — ce qui tourne, et
  non ce que le dépôt prétend décrire. Un écart entre les deux est un signal,
  pas un bug à masquer.
- La tête Alembic est dérivée des fichiers de révision LIVRÉS avec le paquet :
  la révision que personne ne désigne comme parent. Un littéral dans ce module
  serait la répétition d'une faute documentée du projet — le README a affirmé
  « la production reste à 037 » pendant trois jours après la bascule en 039.

La lecture des fichiers passe par `ast`, sans exécuter les migrations : les
importer pour connaître leur numéro coûterait 44 imports et des effets de bord,
sur un chemin qui sert une sonde de liveness.

Les deux lectures sont mémoïsées. `/health` est appelée par un watchdog dont
l'échec REDÉMARRE le serveur : rien de ce qui touche le disque ne doit y être
payé par requête.
"""

from __future__ import annotations

import ast
import importlib.metadata
from functools import cache
from pathlib import Path

#: Réponse quand aucune distribution n'est installée (arbre source nu).
DEV_VERSION = "dev"

_DISTRIBUTION_NAME = "brain_v42"
_REVISION_FIELD = "revision"
_PARENT_FIELD = "down_revision"
#: Une migration est un fichier de schéma, pas un corpus. Garde-fou de lecture.
_MAX_REVISION_BYTES = 1024 * 1024


@cache
def package_version() -> str:
    """Version de la distribution installée, ou `dev` si rien n'est installé."""
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        return DEV_VERSION


def _string_constants(node: ast.expr | None) -> set[str]:
    """Les littéraux texte d'une valeur — couvre `None`, une str, un tuple."""
    if node is None:
        return set()
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _declared_fields(source: str) -> tuple[str | None, set[str]]:
    """Extrait `revision` et les parents déclarés au niveau module."""
    revision: str | None = None
    parents: set[str] = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            names = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            names = [statement.target.id]
        else:
            continue
        value = statement.value
        if _REVISION_FIELD in names:
            declared = _string_constants(value)
            revision = next(iter(declared)) if len(declared) == 1 else None
        if _PARENT_FIELD in names:
            parents |= _string_constants(value)
    return revision, parents


def head_of_versions(directory: Path) -> str | None:
    """Tête de la chaîne portée par `directory`, ou None si elle est ambiguë.

    La tête est la révision qu'aucune autre ne déclare comme parent. Zéro
    candidat (répertoire vide, chaîne cyclique) ou plusieurs (chaîne forkée)
    ne se résument pas : mieux vaut n'annoncer aucun numéro qu'en inventer un.
    """
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(directory.glob("*.py")):
        try:
            if path.stat().st_size > _MAX_REVISION_BYTES:
                continue
            revision, declared_parents = _declared_fields(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError, ValueError):
            continue
        if revision is None:
            continue
        revisions.add(revision)
        parents |= declared_parents
    heads = revisions - parents
    return heads.pop() if len(heads) == 1 else None


def _versions_directory() -> Path | None:
    """Localise les révisions dans les deux dispositions possibles.

    Wheel : `force-include` les recopie sous le paquet importable.
    Arbre source ou install éditable : elles vivent à la racine du dépôt,
    deux niveaux au-dessus de `src/brain_v42/`.
    """
    package_root = Path(__file__).resolve().parent
    candidates = (
        package_root / "alembic" / "versions",
        package_root.parent.parent / "alembic" / "versions",
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), None)


@cache
def shipped_alembic_head() -> str | None:
    """Tête Alembic embarquée avec CE paquet, ou None si aucune ne l'est.

    None n'est pas un détail cosmétique : c'est un paquet incapable de migrer
    sa propre base, et il doit le dire au lieu de se taire.
    """
    directory = _versions_directory()
    return head_of_versions(directory) if directory is not None else None
