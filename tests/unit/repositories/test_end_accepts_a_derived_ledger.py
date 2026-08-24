"""UNE scène, trois rails : fermer une session dont le SERVEUR a rempli le ledger.

La scène est le réflexe d'un utilisateur qu'on ne veut pas punir : il ferme sa
session en disant « je n'ai rien produit de durable », alors que la dérivation a
déjà attribué des artefacts pour lui. Avant ce lot, les trois rails le
refusaient — répertoire, modèle Pydantic, puis CHECK en base — et la session
devenait **infermable**. Un drapeau qui rend une session infermable n'est pas
armable ; c'est ce qui faisait de tout le lot précédent du code mort.

Le XOR mesurait « le client a-t-il DÉCLARÉ ». La dérivation supprime le seul
mode de panne qu'il attrapait (produit-mais-non-déclaré) et alimenterait
désormais son signal côté serveur. **Un contrôle est creux dès que l'objet
contrôlé peut influencer son signal.** On ne retire donc pas une garde : on
retire un reçu que le serveur se délivrerait à lui-même.

La porte de remplacement vit dans `test_end_gate_is_judgement_only.py`.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _make_session,
    _session_row,
    _terminal_router,
)


@pytest.mark.asyncio
async def test_end_accepts_a_derived_ledger_alongside_an_explicit_reason() -> None:
    """Rail 1 — le répertoire. Le ledger est plein SANS que le client l'ait demandé."""
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    opened = _session_row()
    derived = uuid4()
    ended = _session_row(
        session_id=opened["id"],
        status="ended",
        summary="reviewed design",
        next_focus="implement tools",
        captured_knowledge_ids=[derived],
        nothing_to_capture_reason="no durable new knowledge",
    )
    _, _statements, _, factory = _make_session(
        _terminal_router(
            opened,
            updated_row=ended,
            focus_row={"current_focus": "implement tools", "focus_revision": 8},
            current_focus_row={"current_focus": "old focus", "focus_revision": 7},
            artifact_rows=[{"knowledge_id": derived, "session_id": opened["id"]}],
            # L'artefact dérivé est dans le projet et dans la fenêtre : c'est
            # l'invariant de `absorb_tracer_ledger`, qui n'accepte QUE ce qu'une
            # capture explicite aurait accepté. `end` le revalide, et il passe.
            valid_capture_ids=[derived],
        )
    )

    result = await PgBrainSessionRepo(factory).end(
        opened["id"],
        "client-a",
        "reviewed design",
        "implement tools",
        7,
        "no durable new knowledge",
    )

    assert result.session.captured_knowledge_ids == [derived]
    assert result.session.nothing_to_capture_reason == "no durable new knowledge"


def test_the_model_accepts_an_ended_session_carrying_both() -> None:
    """Rail 2 — le rail PYDANTIC, celui que le brief d'origine avait oublié.

    Il aurait fait échouer le lot APRÈS la migration : la base aurait accepté la
    ligne, et le modèle aurait refusé de la relire. Une session persistée que
    son propre modèle ne sait pas charger est pire qu'un refus à l'écriture.
    """
    from brain_v42.models.brain_session import BrainSession

    now = _session_row(status="ended", summary="s", next_focus="n")
    payload = dict(now)
    payload["captured_knowledge_ids"] = [uuid4()]
    payload["attributed_knowledge_ids"] = payload["captured_knowledge_ids"]
    payload["nothing_to_capture_reason"] = "no durable new knowledge"

    session = BrainSession.model_validate(payload)

    assert session.nothing_to_capture_reason == "no durable new knowledge"
    assert len(session.captured_knowledge_ids) == 1
