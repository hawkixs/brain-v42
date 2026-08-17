"""Clés de projet des tests unitaires qui écrivent dans la base partagée.

`tests/unit/` frappe la MÊME base `brain_test` que la suite d'intégration dès
que `BRAIN_V42_TEST_DB_URL` est posée — et les deux rails CI la posent. Mais le
nettoyage de fin de session vit dans `tests/integration/conftest.py`, ne
s'applique qu'à sa propre suite, et ne reconnaît qu'un seul préfixe : `integ-`.

Chaque module unitaire fabriquait donc sa clé sur place, avec son préfixe à lui
— `t8-`, `t9-`, `rv-`, `t-adr-`, `t-run-`. Aucun n'était purgé. Mesuré le
2026-08-11 : 5 674 learnings dans brain_test, dont 4 241 sous `t8-` et 581 sous
`t9-`, pour 188 lignes réelles. Le symptôme est INVISIBLE en CI, qui recrée sa
base à chaque pipeline ; il ne grossit que sur les bases de développement, donc
différemment sur chaque machine (ticket cb888186).

Passer par ce module est ce qui rattache une clé unitaire au nettoyage. Le
contrat est tenu par tests/unit/test_unit_project_keys_are_purged.py, qui
applique la purge RÉELLE à une clé fabriquée ici.

Ne JAMAIS étendre le préfixe à une clé de production comme `brain-v42` : le
garde-fou du conftest ne refuse que le NOM de base `brain`, donc un
`BRAIN_V42_TEST_DB_URL` pointé sur une restauration effacerait des données
réelles. La sonde négative `test_the_purge_leaves_a_non_integration_key_alone`
existe pour faire échouer cette tentation.
"""

from __future__ import annotations

import uuid

#: Le seul préfixe que `_INTEGRATION_PROJECT_PREDICATE` reconnaît. Le tag reste
#: dans la clé après lui, pour qu'une ligne orpheline nomme encore le test qui
#: l'a écrite.
UNIT_KEY_PREFIX = "integ-"


def make_unit_project_key(tag: str) -> str:
    """Return a per-test project key that the shared purge will delete.

    ``tag`` identifie le module appelant (``t8``, ``rv``, …) et n'a aucun effet
    sur le nettoyage : c'est le préfixe qui compte.
    """
    if not tag or not tag.strip():
        raise ValueError("tag is required — an unnamed key cannot be traced back to its test")
    return f"{UNIT_KEY_PREFIX}{tag.strip()}-{uuid.uuid4().hex[:8]}"
