"""La porte de remplacement de `end` : le JUGEMENT, et rien que le jugement.

Le XOR retiré, `end` ne mesure plus la diligence du client. Ce qui reste doit
être exactement ce que le serveur **ne peut pas fabriquer à la place de
l'utilisateur** — sinon on aurait remplacé un reçu par un autre.

Le recensement plus bas le prouve au lieu de l'argumenter : `summary` n'a qu'UN
écrivain dans tout `src/`, et c'est la fermeture explicite. Le balayage laisse la
colonne à `NULL`, et la branche `closed_inactive` du CHECK 046 le lui INTERDIT
même s'il essayait. Aucun chemin serveur ne peut donc produire un `summary` :
c'est ce qui fait du jugement le seul objet de `end` hors de portée du serveur.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _session_row,
    _terminal_router,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = REPO_ROOT / "src"
MIGRATION_046 = REPO_ROOT / "alembic" / "versions" / "046_session_identity_and_nature.py"

#: Les seuls sites de `src/` qui font voyager un `summary` de session — et leur
#: liste EST le résultat : deux maillons d'une même chaîne humaine, aucun tiers.
DECLARED_SUMMARY_WRITERS = frozenset(
    {
        # Le tool explicite : il RELAIE le texte de l'utilisateur, il n'en
        # fabrique pas. C'est l'unique porte d'entrée.
        (
            "src/brain_v42/mcp/tools/session_lifecycle_tools.py"
            "::register_session_lifecycle_tools.brain_session_end"
        ),
        # Et l'unique site qui le persiste, au bout de cette même commande.
        "src/brain_v42/repositories/pg_brain_session.py::PgBrainSessionRepo._mark_ended",
    }
)


@pytest.mark.asyncio
async def test_end_with_an_empty_ledger_and_no_reason_now_passes() -> None:
    """La porte n'est PLUS la diligence : ne rien avoir produit est une issue valide.

    Avant, cette fermeture exigeait une justification écrite. La dérivation rend
    cette exigence absurde — l'utilisateur n'a plus la main sur ce que le ledger
    contient — et surtout elle punissait le cas honnête.
    """
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
    )
    _, _statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
        )
    )

    result = await PgBrainSessionRepo(factory).end(
        opened["id"], "client-a", "reviewed design", "implement tools", 7, None
    )

    assert result.session.captured_knowledge_ids == []
    assert result.session.nothing_to_capture_reason is None


@pytest.mark.parametrize("field", ["summary", "next_focus"])
def test_the_model_still_refuses_a_blank_judgement(field: str) -> None:
    """Le rail Pydantic garde ce que le serveur ne sait pas produire."""
    from brain_v42.models.brain_session import BrainSession

    payload = dict(_session_row(status="ended", summary="s", next_focus="n"))
    payload["nothing_to_capture_reason"] = "nothing durable"
    payload[field] = "   "

    with pytest.raises(ValueError, match=f"ended session requires {field}"):
        BrainSession.model_validate(payload)


def test_a_blank_reason_is_still_refused() -> None:
    """Donner une raison reste un acte : une raison blanche n'en est pas une."""
    from brain_v42.models.brain_session import BrainSession

    payload = dict(_session_row(status="ended", summary="s", next_focus="n"))
    payload["nothing_to_capture_reason"] = "   "

    with pytest.raises(ValueError, match="must not be blank"):
        BrainSession.model_validate(payload)


class _SummaryKeywordVisitor(ast.NodeVisitor):
    """Relève tout appel qui passe un mot-clé ``summary``, avec sa def englobante."""

    def __init__(self, relative: str) -> None:
        self.relative = relative
        self.found: set[str] = set()
        self._scope: list[str] = []

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
        if any(keyword.arg == "summary" for keyword in node.keywords):
            self.found.add(f"{self.relative}::{'.'.join(self._scope) or '<module>'}")
        self.generic_visit(node)


def _summary_writers() -> set[str]:
    """Tout site de `src/` qui fait voyager un ``summary`` de session.

    Recensé par la COLONNE et non par le nom du tool, et restreint aux fichiers
    qui nomment ``brain_sessions`` : ailleurs, `summary` résume autre chose et
    n'a rien à voir avec la fermeture d'une session.
    """
    writers: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "brain_sessions" not in text:
            continue
        visitor = _SummaryKeywordVisitor(path.relative_to(REPO_ROOT).as_posix())
        visitor.visit(ast.parse(text, filename=str(path)))
        writers.update(visitor.found)
    return writers


def test_no_server_path_can_write_a_session_summary() -> None:
    """LA preuve, pas l'argument : un seul écrivain, et c'est la fermeture explicite.

    Si un chemin serveur gagnait le droit d'écrire `summary`, `end` cesserait de
    mesurer un jugement humain et redeviendrait un reçu — exactement le défaut
    qu'on vient de retirer, réintroduit ailleurs.
    """
    assert _summary_writers() == set(DECLARED_SUMMARY_WRITERS)


def test_the_closed_inactive_branch_forbids_a_summary_outright() -> None:
    """Et la base le refuse aussi : la garantie n'est pas qu'applicative.

    Le balayage laisse `summary` à `NULL` ; la branche `closed_inactive` du CHECK
    046 le lui IMPOSE. Même un balayage qui essaierait serait rejeté par la base.
    """
    branch = MIGRATION_046.read_text(encoding="utf-8")
    closed_inactive = branch.split("status = 'closed_inactive'")[1].split(")")[0]
    assert "summary IS NULL" in closed_inactive
    assert "next_focus IS NULL" in closed_inactive


def test_the_census_is_not_blind() -> None:
    """Témoin de non-vacuité : un recenseur qui ne trouve rien passerait pour toujours."""
    assert _summary_writers(), "le recenseur ne désigne plus AUCUN site — il est aveugle"
