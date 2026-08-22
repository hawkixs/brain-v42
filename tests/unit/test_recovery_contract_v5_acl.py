"""Les ACL et les propriétaires sont une preuve SÉPARÉE, et c'est structurel.

`60708007`, quatrième enfant de la porte `8eaefe36`. Le constat hérité du parent
n'est pas une préférence de rangement : **le restore sandbox tourne
`--no-owner --no-acl`**. Une attestation de restauration ne peut donc pas,
même en principe, prouver quoi que ce soit sur les droits — elle les a effacés
avant de regarder.

D'où un actif à part, `brain-v42-v5-acl.sql`, joué contre la PRODUCTION en
lecture seule. Et d'où, surtout, **l'absence de variante `-pgrestore`** : ce
n'est pas un oubli de parité, c'est le contenu de la décision. Un jumeau
`-pgrestore` de cette preuve laisserait croire qu'elle s'applique là où elle ne
peut pas s'appliquer. Un test ci-dessous épingle cette absence pour que personne
ne la « complète ».

**Le contrat principal est structurellement MUET là-dessus**, et c'est mesuré :
sous un `REVOKE` sur une vue du contrat, sous un changement de propriétaire, et
sous un `ALTER ROLE codex_ro CREATEDB`, il rend exactement son bruit de fond.
C'est la prémisse du split, vérifiée plutôt que supposée.

**La liste attendue est DÉRIVÉE de la migration 036**, jamais recopiée — exigence
explicite du ticket. Le précédent qui la motive est la 045 : un `DROP VIEW`
emporte ses `GRANT` en silence, la vue revient, et `codex_ro` a perdu sa lecture
sans qu'une seule ligne ne le dise.

**Volume chiffré AVANT d'écrire, mesuré le 2026-08-22 à la tête `046`** : 51
relations (32 tables, 10 vues, 9 séquences), toutes propriété de `brain` ; 40
portent une ACL explicite, 11 sont à `NULL` ; un seul bénéficiaire hors
propriétaire, `codex_ro`, avec `SELECT` sur exactement 10 vues ; 0 ACL de
colonne, 0 ACL de fonction, 0 `GRANT OPTION`, 0 appartenance de rôle.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
ACL_SQL = RECOVERY / "brain-v42-v5-acl.sql"
ACL_JSON = RECOVERY / "brain-v42-v5-acl.json"
MIGRATION_036 = ROOT / "alembic" / "versions" / "036_codex_contract_views.py"
MIGRATION_045 = ROOT / "alembic" / "versions" / "045_dream_run_model_width.py"

CHECK_ID = "acl_and_ownership"

#: Les quatre sous-compteurs. Chacun a été prouvé par une mutation qui ne touche
#: QUE lui — sept mutations au total, sur `brain_test`, transactions annulées.
COUNTERS = (
    "contract_grant_mismatches",
    "relation_owner_mismatches",
    "role_privilege_mismatches",
    "unexpected_grantee_mismatches",
)


def _granted_views(source: Path) -> set[str]:
    """Les vues qui reçoivent `SELECT` pour `codex_ro`, LUES dans la migration."""
    return set(re.findall(r"GRANT SELECT ON (codex_\w+) TO codex_ro", source.read_text("utf-8")))


def test_the_expected_grants_are_derived_from_the_migration_not_retyped() -> None:
    """Exigence explicite du ticket : dériver le registre, ne pas le recopier.

    Une liste tapée à la main dériverait de la migration au premier ajout de vue,
    et l'attestation validerait alors un périmètre que plus personne n'a décidé.
    Ce test relit la 036 à chaque exécution.
    """
    expected = _granted_views(MIGRATION_036)
    assert len(expected) == 10

    declared = set(json.loads(ACL_JSON.read_text(encoding="utf-8"))["checks"][0]["objects"])
    assert declared == expected

    sql = ACL_SQL.read_text(encoding="utf-8")
    for view in sorted(expected):
        assert f"('{view}', 'codex_ro', 'SELECT')" in sql, view


def test_the_045_regrant_stays_inside_the_derived_registry() -> None:
    """La 045 repose UN grant après un `DROP VIEW`. Il doit être dans la liste.

    C'est le précédent qui justifie tout ce lot : un `DROP VIEW` emporte ses
    droits en silence. Si la 045 regrantait une vue absente du registre de la
    036, le registre serait déjà incomplet — et personne ne le saurait.
    """
    assert _granted_views(MIGRATION_045) <= _granted_views(MIGRATION_036)


def test_this_proof_has_no_pgrestore_twin_and_that_is_the_point() -> None:
    """L'absence de jumeau est le CONTENU de la décision, pas un trou de parité.

    Le restore sandbox tourne `--no-owner --no-acl` : il efface l'objet de cette
    preuve avant de l'observer. Un `brain-v42-v5-acl-pgrestore.sql` ferait croire
    qu'elle s'applique là-bas, et rendrait `0/1` sur toute restauration valide.
    """
    assert not (RECOVERY / "brain-v42-v5-acl-pgrestore.sql").exists()
    assert json.loads(ACL_JSON.read_text(encoding="utf-8"))["proof_scope"] == (
        "live-production-only"
    )


def test_the_proof_is_read_only() -> None:
    """Une attestation qui mute son objet n'atteste rien."""
    sql = ACL_SQL.read_text(encoding="utf-8")
    assert sql.startswith("WITH ") and sql.endswith(";\n") and sql.count(";") == 1
    for forbidden in ("GRANT ", "REVOKE ", "ALTER ", "CREATE ", "DROP ", "INSERT ", "UPDATE "):
        # Les littéraux du registre contiennent « GRANT » en prose de commentaire
        # nulle part : la seule occurrence acceptable serait dans une chaîne
        # quotée, et il n'y en a pas.
        assert forbidden not in sql, forbidden


def test_the_check_row_names_its_four_counters() -> None:
    """Un échec doit dire LEQUEL des quatre a bougé.

    Les quatre ont des causes sans rapport : un `REVOKE` accidentel, un
    `ALTER TABLE ... OWNER TO`, un rôle qui gagne un attribut, un bénéficiaire
    inattendu. Un booléen nu ferait relire tout le SQL.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    for counter in COUNTERS:
        assert f"'{counter}', 0" in sql, counter
        assert f"{counter}.value" in sql, counter
    entry = json.loads(ACL_JSON.read_text(encoding="utf-8"))["checks"][0]
    assert entry["id"] == CHECK_ID


def test_the_contract_grant_check_is_bidirectional() -> None:
    """Les deux sens, et ils attrapent deux pannes différentes.

    Sens 1 — un `GRANT` du registre qui a DISPARU : le précédent 045, une vue
    recréée qui a perdu ses droits. Sens 2 — un `GRANT` en TROP : `codex_ro` qui
    gagne la lecture d'une table hors contrat, ce qu'aucun autre contrôle ne
    verrait. Le second terme exclut `brain` : le propriétaire n'est pas un
    bénéficiaire à recenser, et l'y inclure ferait rougir les 40 relations.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    body = sql.split("contract_grant_mismatches AS (", 1)[1].split("\n),", 1)[0]
    assert body.count("SELECT count(*)") == 2
    assert "LEFT JOIN observed_relation_privileges" in body
    assert "LEFT JOIN expected_contract_grants" in body
    assert "observed_grant.grantee <> 'brain'" in body


def test_the_owner_check_reads_every_relation_kind() -> None:
    """Séquences et vues comprises : un propriétaire dérive aussi bien là.

    Une séquence dont le propriétaire change laisse l'application capable de
    lire et incapable d'insérer — la panne se présente comme un bug applicatif,
    jamais comme un problème de droits.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    assert "relation_record.relkind IN ('r', 'v', 'S', 'm')" in sql
    assert "relation_record.owner <> 'brain'" in sql


def test_the_role_check_pins_what_codex_ro_must_never_gain() -> None:
    """Le fait de sécurité, pas la forme : `codex_ro` reste strictement lecteur.

    `brain` n'est PAS épinglé en superutilisateur — l'y figer graverait l'état
    courant comme une exigence et rougirait le jour où quelqu'un le durcirait.
    Ce qui est épinglé est ce qui ne doit jamais bouger dans le mauvais sens.
    """
    sql = ACL_SQL.read_text(encoding="utf-8")
    for attribute in (
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        assert f"role_record.{attribute}" in sql, attribute
    assert "role_record.rolname = 'codex_ro'" in sql
    # Le rôle en TROP est le sens inverse, et il compte : un compte de service
    # ajouté à la main n'est visible nulle part ailleurs dans ce dépôt.
    assert "expected_role.role_name IS NULL" in sql
    # L'appartenance de rôle est un contournement classique du contrôle
    # ci-dessus : `GRANT brain TO codex_ro` ne change aucun attribut.
    assert "pg_auth_members" in sql
    # Et l'USAGE sur le schéma : sans lui, les dix GRANT sont inertes.
    assert "acl_entry.privilege_type = 'USAGE'" in sql
