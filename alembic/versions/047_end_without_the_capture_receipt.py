"""Retirer le XOR de fermeture : `end` cesse de mesurer la diligence du client.

Revision ID: 047
Revises: 046

CE N'EST PAS UN AFFAIBLISSEMENT, et la nuance décide de tout. Le XOR
« ledger non vide XOR `nothing_to_capture_reason` » mesurait une seule chose :
« le client a-t-il DÉCLARÉ ce qu'il a produit ». La capture dérivée
(`brain_session_derived_capture_enabled`) supprime le mode de panne qu'il
attrapait — produit-mais-non-déclaré — et alimenterait désormais son signal
DEPUIS LE SERVEUR.

**Un contrôle est creux dès que l'objet contrôlé peut influencer son signal.**
Le conserver ne garderait donc rien : ce serait un reçu que le serveur se
délivre à lui-même. Pire, il rendrait INFERMABLE toute session dont le serveur a
rempli le ledger — l'utilisateur ferme en disant « rien de durable », le serveur
a déjà attribué pour lui, et la base refuse. Un drapeau qui rend une session
infermable n'est pas armable.

CE QUI RESTE est ce que le serveur ne peut pas fabriquer à la place de
l'utilisateur : `summary` et `next_focus` non blancs, et une raison qui dit
quelque chose SI elle est donnée. Prouvé plutôt qu'affirmé par
`tests/unit/repositories/test_end_gate_is_judgement_only.py` : `summary` n'a que
deux sites dans tout `src/` — le tool qui relaie le texte humain et le dépôt qui
le persiste — et la branche `closed_inactive` du CHECK 046 INTERDIT à un
balayage d'en écrire un.

LE TEXTE DU CHECK EST RELU DANS LA 046, jamais retapé — gabarit de la 045. Une
seconde source de vérité pour cette contrainte ne divergerait qu'en production,
et seulement le jour où quelqu'un tenterait une ligne que l'autre version
refuse. Le remplacement est ASSERTÉ : si la 046 bougeait, cette révision
échouerait à l'import au lieu de reposer une contrainte inchangée.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


#: Le bloc retiré, tel qu'il est écrit dans la 046. Sert de repère de coupe ET
#: de témoin : sa disparition de la 046 doit faire échouer cette révision.
_CAPTURE_RECEIPT = """
        AND (
            (
                cardinality(captured_knowledge_ids) > 0
                AND nothing_to_capture_reason IS NULL
            )
            OR (
                cardinality(captured_knowledge_ids) = 0
                AND nothing_to_capture_reason IS NOT NULL
                AND btrim(nothing_to_capture_reason) <> ''
            )
        )"""

#: Ce qui le remplace : donner une raison reste un acte, ne pas en donner aussi.
_JUDGEMENT_ONLY = """
        AND (
            nothing_to_capture_reason IS NULL
            OR btrim(nothing_to_capture_reason) <> ''
        )"""


def _terminal_state_046() -> str:
    """Relire la contrainte terminale DANS la 046, jamais la retaper ici."""
    source = Path(__file__).with_name("046_session_identity_and_nature.py")
    spec = importlib.util.spec_from_file_location("_migration_046_sessions", source)
    if spec is None or spec.loader is None:  # pragma: no cover — chemin figé
        raise RuntimeError(f"046 illisible depuis la 047 : {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._TERMINAL_STATE_V5)


_TERMINAL_STATE_V6_SOURCE = _terminal_state_046()

if _CAPTURE_RECEIPT not in _TERMINAL_STATE_V6_SOURCE:  # pragma: no cover — garde d'import
    raise RuntimeError(
        "le bloc XOR de la 046 a changé de forme : la 047 reposerait une "
        "contrainte inchangée en croyant l'avoir relâchée"
    )

#: v6 = v5 sans le reçu. `captured_knowledge_ids` ne porte plus AUCUNE
#: contrainte sur la branche `ended`, exactement comme sur `closed_inactive`.
_TERMINAL_STATE_V6 = _TERMINAL_STATE_V6_SOURCE.replace(_CAPTURE_RECEIPT, _JUDGEMENT_ONLY)

#: Ce qu'un downgrade restaurerait — et donc ce qu'il détruirait.
_TERMINAL_STATE_V5 = _TERMINAL_STATE_V6_SOURCE

_DROP = "ALTER TABLE brain_sessions DROP CONSTRAINT brain_sessions_terminal_state_valid"

#: Fail-closed, gabarit 037. Deux formes deviennent légales avec la 047 et
#: illégales sans elle : « ledger ET raison » (le cas dérivé, celui qui motive
#: la révision) et « ni ledger ni raison » (la fermeture honnête d'une session
#: qui n'a rien produit). Un downgrade silencieux ne les corromprait pas — la
#: base refuserait la contrainte — mais il échouerait au milieu, avec un message
#: de contrainte, sans dire ce qui est en cause. Celui-ci le dit.
_REFUSE_LOSSY_DOWNGRADE = """
DO $$
DECLARE
    offending bigint;
BEGIN
    SELECT count(*) INTO offending
    FROM brain_sessions
    WHERE status = 'ended'
      AND (
          (cardinality(captured_knowledge_ids) > 0 AND nothing_to_capture_reason IS NOT NULL)
          OR (cardinality(captured_knowledge_ids) = 0 AND nothing_to_capture_reason IS NULL)
      );

    IF offending > 0 THEN
        RAISE EXCEPTION
            'cannot downgrade 047: % ended session(s) hold a capture outcome the '
            'restored XOR forbids (derived ledger with a reason, or neither). '
            'Reconcile them before downgrading — they are user-visible closures.',
            offending;
    END IF;
END;
$$;
"""


def upgrade() -> None:
    op.execute(_DROP)
    op.execute(_TERMINAL_STATE_V6)


def downgrade() -> None:
    op.execute(_REFUSE_LOSSY_DOWNGRADE)
    op.execute(_DROP)
    op.execute(_TERMINAL_STATE_V5)
