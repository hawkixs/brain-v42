"""Le canary comptait les propositions et jetait leur contenu.

Ses trois mesures — JSON valide, secondes par batch, NOMBRE de propositions —
ne disent rien de ce qui est proposé. Deux modèles peuvent rendre 30
propositions chacun, l'un archivant des features vivantes et l'autre voyant les
vrais doublons, et le tableau les classerait à égalité. Le choix du 2026-08-16
allait se faire sur ce tableau.

Le dump ne mesure pas la qualité — il rend le contenu LISIBLE pour qu'un humain
ou un juge la mesure. C'est délibéré : un score de qualité rendu par le même
étage qui produit les propositions n'aurait aucune valeur d'arbitrage.

Rien n'est persisté : le canary n'appelle ni `persist_proposals` ni
`apply_proposals`, et ce dump n'y change rien.
"""

from __future__ import annotations

from uuid import UUID

from scripts.canary_roadmap_model import _proposals_payload
from scripts.roadmap_curate import BatchOutcome, CurationDraft, FeatureCard, ProjectBatch

_FEATURE_ID = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")


def _outcome() -> BatchOutcome:
    batch = ProjectBatch(
        project_key="red-lab",
        features=[
            FeatureCard(id=_FEATURE_ID, name="Feature vivante", status="building", pinned=True),
            FeatureCard(id=_OTHER_ID, name="Doublon", status="design", pinned=False),
        ],
    )
    return BatchOutcome(
        batch=batch,
        drafts=[
            CurationDraft(
                op="merge",
                feature_id=_OTHER_ID,
                payload={"into": str(_FEATURE_ID)},
                rationale="même périmètre",
            )
        ],
    )


def test_dump_carries_the_target_feature_not_just_its_uuid() -> None:
    """Un UUID nu ne se juge pas : il faut la feature que la proposition vise.

    Sans le nom, le statut et l'épinglage de la cible, personne ne peut dire si
    `archive` sur cette ligne est un bon appel ou la destruction d'un engagement.
    """
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    proposal = payload["proposals"][0]
    assert proposal["target"]["name"] == "Doublon"
    assert proposal["target"]["status"] == "design"
    assert proposal["target"]["pinned"] is False


def test_dump_keeps_the_rationale_which_is_the_judgeable_part() -> None:
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["proposals"][0]["rationale"] == "même périmètre"
    assert payload["proposals"][0]["op"] == "merge"


def test_merge_names_the_feature_it_would_absorb_into() -> None:
    """`merge` sans la cible d'absorption est illisible : qui mange qui ?"""
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["proposals"][0]["payload"]["into_name"] == "Feature vivante"


def test_dump_is_json_serialisable_end_to_end() -> None:
    """Les UUID doivent sortir en str, sinon `json.dump` casse à l'écriture."""
    import json

    json.dumps(_proposals_payload("m", _outcome()))


def test_dump_records_the_model_and_the_project_it_ran_on() -> None:
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["model"] == "mistralai/mistral-nemotron"
    assert payload["project_key"] == "red-lab"
