"""Le contrat v5 empreinte les QUATORZE fonctions de trigger, pas une seule.

`75112bc6`, troisième enfant de la porte `8eaefe36`, et le dernier des cinq.

**Le chiffre du corps du ticket était faux d'un facteur ~4, et la passe
sceptique l'avait déjà corrigé.** Ce n'est pas 55 fonctions : 55 est le nombre de
TRIGGERS. Les fonctions distinctes sont **14**. Les trois nombres du ticket ont
été REMESURÉS ici, à la tête `046`, le 2026-08-22 :

- **55** triggers non internes ;
- **44** déjà nommés dans l'actif, donc **11** ne l'étaient pas — l'écart réel,
  et non « ~40 » ;
- **6** `trg_*_freshness_stamped`, et non 12 ;
- **14** fonctions de trigger distinctes, dont **UNE SEULE** était empreintée
  (`update_updated_at`, par l'invariant 039 hérité). Treize pouvaient changer de
  corps sans qu'un octet du contrat ne bouge.

**LE PRÉALABLE DUR EST LEVÉ, ET PAR MESURE.** Le parent l'exigeait en toutes
lettres : normaliser puis auditer la dérive prod vs migration fraîche AVANT tout
fingerprint, sans quoi l'empreinte graverait la dérive comme référence. Une base
a été construite À NEUF par `alembic upgrade head`, puis les 14 `prosrc_sha256`
comparés à la production : **écart ZÉRO**. La comparaison n'a délibérément PAS
été faite contre `brain_test` — cette base est migrée à chaque session mais on
ne peut pas établir qu'elle n'a jamais été clonée depuis la prod, et un contrôle
dont on ne peut pas exclure que l'objet ait produit le témoin est creux.

**L'échantillonnage par classe, proposé par le ticket, n'a pas été retenu** — la
passe sceptique l'avait déjà rendu facultatif en ramenant le volume de 55 à 14.
Quatorze empreintes tiennent dans UNE liste `VALUES`, là où les deux fonctions
déjà attestées coûtent ~135 lignes de CTE chacune. Échantillonner aurait laissé
des fonctions hors contrat pour économiser des lignes qu'on n'a pas besoin
d'économiser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
V5_JSON = RECOVERY / "brain-v42-v5.json"
V5_SQL = RECOVERY / "brain-v42-v5.sql"
V5_PGRESTORE = RECOVERY / "brain-v42-v5-pgrestore.sql"

CHECK_ID = "trigger_function_fingerprints"

#: MESURÉ le 2026-08-22 contre la production à la tête `046`, et rejoué contre
#: une base construite à neuf : identiques. `update_updated_at` figure ici ET
#: dans l'invariant 039 — un test compare les deux exemplaires, sinon l'un
#: dériverait de l'autre en restant vert.
TRIGGER_FUNCTIONS: tuple[tuple[str, str, int], ...] = (
    (
        "enforce_immutable_ticket_participants",
        "cdb295b8a5c811467706ac9c10622fe1140d957e51f94b0fb50911ac9629bb30",
        256,
    ),
    (
        "enforce_live_feature_artifact_target",
        "11f79d4116738608988f29c53bb1db708537cc3fdeac18c5bdede106bf6bccd7",
        496,
    ),
    (
        "increment_project_focus_revision",
        "424dfc1a9154dbc48e08ffc70712920cbfdc42e659a1500da681a2e50526df76",
        215,
    ),
    (
        "normalize_project_key_alias",
        "13b945bb4a5c307f430b0b6ba1387a3a38cc0abe8ac8ed58aa391f9a99e63518",
        921,
    ),
    (
        "normalize_related_project_aliases",
        "f9b325aed559eef8c28c46d5168cd88027e51a851678381146fc94317e012e6a",
        572,
    ),
    (
        "reject_project_context_key_change",
        "e800aecbe1054d8333babd1c43f1f52893db81b6b8f4e7fc6d167c6cd6f9de82",
        226,
    ),
    (
        "set_project_context_updated_at",
        "60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419",
        391,
    ),
    (
        "stamp_content_updated_at",
        "070b4db370dbe20a280a4f75e58edc72f337e9abcce9cadf673af1f1d30b2342",
        77,
    ),
    (
        "stamp_freshness_status",
        "179caf250bf9fe5aae1d1e1fdb040b4b08008a9c5d76cc1f65ebaf3272db86dd",
        890,
    ),
    (
        "sync_brain_entity_registry",
        "dab84538fedcd42d28038a3055c1b7e6d4e1f7f02f21891e1195cafdb3f0489c",
        10485,
    ),
    (
        "sync_project_registry",
        "ff39be21e857296038f463ff71eb932a65d7e3be7c7120a2414a3f5832ce4565",
        3699,
    ),
    (
        "sync_referenced_project_registry",
        "6844d14802019487796602f9cef95327f67a2c56798c1cb561541b4537f6a093",
        306,
    ),
    (
        "sync_related_project_registry",
        "f1dd4dd21283d6a98a9f14e801685b13415f4de23dbdc59336201baafb3d60be",
        349,
    ),
    ("update_updated_at", "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59", 96),
)

#: L'empreinte de `update_updated_at` telle que l'invariant 039 la pinne déjà.
#: Écrite à la main ICI, exprès : c'est le TERME DE COMPARAISON, et le dériver de
#: la même source que ce qu'il compare le rendrait creux.
UPDATE_UPDATED_AT_SHA256 = "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59"

#: Les 11 triggers de tamponnage que l'actif ne NOMMAIT pas — 5 de la 041, 6 de
#: la 043. `19` = BEFORE + INSERT + UPDATE + FOR EACH ROW.
STAMPING_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("adrs", "trg_adrs_content_updated", "stamp_content_updated_at"),
    ("adrs", "trg_adrs_freshness_stamped", "stamp_freshness_status"),
    ("decisions", "trg_decisions_content_updated", "stamp_content_updated_at"),
    ("decisions", "trg_decisions_freshness_stamped", "stamp_freshness_status"),
    ("indexed_plans", "trg_indexed_plans_freshness_stamped", "stamp_freshness_status"),
    ("learnings", "trg_learnings_content_updated", "stamp_content_updated_at"),
    ("learnings", "trg_learnings_freshness_stamped", "stamp_freshness_status"),
    ("runbooks", "trg_runbooks_content_updated", "stamp_content_updated_at"),
    ("runbooks", "trg_runbooks_freshness_stamped", "stamp_freshness_status"),
    ("snippets", "trg_snippets_content_updated", "stamp_content_updated_at"),
    ("snippets", "trg_snippets_freshness_stamped", "stamp_freshness_status"),
)

NEW_CTES = (
    "expected_trigger_functions",
    "observed_trigger_functions",
    "trigger_function_mismatches",
    "expected_stamping_triggers",
    "observed_stamping_triggers",
    "stamping_trigger_mismatches",
)

#: Les attributs qu'une fonction de trigger doit garder pour entrer dans
#: l'ensemble OBSERVÉ. Les mettre dans le prédicat plutôt que dans la liste
#: attendue est ce qui fait tomber une fonction dérivée HORS de l'observation :
#: elle devient « attendue et introuvable », ce qui est exactement le fait.
INVARIANT_ATTRIBUTES = (
    "prokind = 'f'",
    "provolatile = 'v'",
    "pronargs = 0",
    "pronargdefaults = 0",
    "NOT function_record.prosecdef",
    "NOT function_record.proleakproof",
    "NOT function_record.proretset",
    "proconfig IS NULL",
)


def _cte_body(path: Path, name: str) -> str:
    match = re.search(
        rf"^{name}(?:\([^)]*\))? AS \((.*?)^\),$",
        path.read_text(encoding="utf-8"),
        re.M | re.S,
    )
    assert match is not None, f"{path.name}: {name}"
    return match.group(1)


def test_the_remeasured_volume_is_pinned() -> None:
    """Les trois nombres du ticket, remesurés — le corps en avait faux deux."""
    assert len(TRIGGER_FUNCTIONS) == 14
    assert len({entry[0] for entry in TRIGGER_FUNCTIONS}) == 14
    assert len(STAMPING_TRIGGERS) == 11
    assert sum(1 for _, name, _ in STAMPING_TRIGGERS if name.endswith("_freshness_stamped")) == 6
    assert sum(1 for _, name, _ in STAMPING_TRIGGERS if name.endswith("_content_updated")) == 5
    # Toutes les empreintes sont des SHA-256 hexadécimaux, et toutes distinctes :
    # deux fonctions au même digest signalerait un copier-coller de la liste.
    for _, digest, octets in TRIGGER_FUNCTIONS:
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert octets > 0
    assert len({entry[1] for entry in TRIGGER_FUNCTIONS}) == 14


def test_both_assets_pin_every_trigger_function() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_trigger_functions")
        for name, digest, octets in TRIGGER_FUNCTIONS:
            assert f"('{name}', '{digest}', {octets})" in expected, f"{asset.name}: {name}"


def test_the_039_fingerprint_and_the_census_agree_on_update_updated_at() -> None:
    """Deux exemplaires de la même empreinte : les comparer, ou l'un dérivera.

    L'invariant 039 pinne `update_updated_at` depuis longtemps ; le recensement
    la pinne à nouveau. Chacun resterait vert en dérivant de l'autre — c'est le
    mode de panne des empreintes dupliquées, celui que `2bb1988f` a déjà
    rencontré sur la formule de colonnes.
    """
    census = {name: digest for name, digest, _ in TRIGGER_FUNCTIONS}
    assert census["update_updated_at"] == UPDATE_UPDATED_AT_SHA256

    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        # Présente DEUX fois : une dans l'invariant 039, une dans le recensement.
        assert sql.count(UPDATE_UPDATED_AT_SHA256) == 2, asset.name


def test_the_observed_set_is_bounded_by_the_invariant_attributes() -> None:
    """Une fonction qui perd un de ces attributs SORT de l'observation.

    C'est délibéré et c'est le point : `SECURITY DEFINER` posé sur une fonction
    de trigger est une escalade de privilège, pas une divergence de corps. En la
    faisant sortir de l'ensemble observé, elle devient « attendue et
    introuvable » — l'exacte vérité, et un échec plutôt qu'un silence.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_trigger_functions")
        for attribute in INVARIANT_ATTRIBUTES:
            assert attribute in body, f"{asset.name}: {attribute}"
        assert "lanname = 'plpgsql'" in body, asset.name


def test_both_assets_declare_the_eleven_unnamed_stamping_triggers() -> None:
    """Les 11 que l'actif ne nommait pas — l'écart RÉEL, mesuré à 11 et non ~40."""
    for asset in (V5_SQL, V5_PGRESTORE):
        expected = _cte_body(asset, "expected_stamping_triggers")
        for table, trigger, function in STAMPING_TRIGGERS:
            assert f"('{table}', '{trigger}', '{function}', 19, " in expected, (
                f"{asset.name}: {trigger}"
            )


def test_the_stamping_check_pins_the_when_clause() -> None:
    """La clause WHEN est TOUT le sens de ces triggers, et elle est perdable.

    Les 11 sont CONDITIONNELS : `WHEN (old.x IS DISTINCT FROM new.x)`. Recréé
    sans sa clause, le trigger tamponnerait à CHAQUE écriture — la 041 a été
    écrite précisément pour que `content_updated_at` ne bouge que sur un vrai
    changement de contenu. Le nom, la table et la fonction seraient tous les
    trois intacts : seul le md5 de la condition voit la perte.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_stamping_triggers")
        assert "pg_get_triggerdef" in body, asset.name
        assert "WHEN \\((.*)\\) EXECUTE " in body, asset.name
        mismatches = _cte_body(asset, "stamping_trigger_mismatches")
        assert "condition_md5 = expected_trigger.condition_md5" in mismatches, asset.name


def test_a_disabled_trigger_falls_out_of_the_observed_set() -> None:
    """`tgenabled = 'O'` dans le prédicat, et c'est la panne la plus muette.

    Un trigger DÉSACTIVÉ existe encore, porte son nom, sa table et sa fonction —
    `pg_trigger` le rend, un `\\d table` l'affiche. Il ne fait simplement plus
    rien. Sans ce prédicat, l'attestation le compterait comme présent.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        assert "trigger_record.tgenabled = 'O'" in _cte_body(asset, "observed_stamping_triggers"), (
            asset.name
        )


def test_both_checks_are_bidirectional() -> None:
    """Les deux sens, et le second n'est pas décoratif.

    Une fonction de trigger AJOUTÉE n'est attrapée par rien d'autre : elle n'est
    ni une table, ni un index, ni une contrainte. Un trigger de tamponnage posé
    sur une table de plus ferait bouger `content_updated_at` là où personne ne
    l'attend — et le recensement des fonctions, lui, resterait vert.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        for name in ("trigger_function_mismatches", "stamping_trigger_mismatches"):
            body = _cte_body(asset, name)
            assert body.count("SELECT count(*)") == 2, f"{asset.name}: {name}"
            assert "IS NULL" in body, f"{asset.name}: {name}"


def test_the_stamping_observation_is_bounded_by_function_not_by_name() -> None:
    """Le second sens doit pouvoir voir une table NEUVE, pas seulement les onze.

    Borner l'observation aux tables attendues aurait rendu le terme inverse
    aveugle au cas qui compte — un tamponnage posé ailleurs. Le borner aux deux
    FONCTIONS de tamponnage le garde ouvert à toute la base.
    """
    for asset in (V5_SQL, V5_PGRESTORE):
        body = _cte_body(asset, "observed_stamping_triggers")
        assert (
            "function_record.proname IN ('stamp_content_updated_at', 'stamp_freshness_status')"
            in body
        ), asset.name
        for table, _, _ in STAMPING_TRIGGERS:
            assert f"table_record.relname = '{table}'" not in body, f"{asset.name}: {table}"


def test_the_new_ctes_are_byte_identical_across_the_two_variants() -> None:
    """Parité MESURÉE : zéro clause WHEN ne porte de motif normalisé.

    Elles contiennent des `::text` nus — `(old.title)::text` — mais aucun
    `::character varying::text` ni `]::text[]`, les deux seules formes que
    `pg_restore` réécrit. Vérifié EN BASE sur les 55 triggers, pas à l'œil.
    """
    for name in NEW_CTES:
        assert _cte_body(V5_SQL, name) == _cte_body(V5_PGRESTORE, name), name

    for asset in (V5_SQL, V5_PGRESTORE):
        for name in NEW_CTES:
            body = _cte_body(asset, name)
            assert "'::character varying::text'" not in body, f"{asset.name}: {name}"
            assert "']::text[]'" not in body, f"{asset.name}: {name}"


def test_the_check_row_names_its_two_counters() -> None:
    for asset in (V5_SQL, V5_PGRESTORE):
        sql = asset.read_text(encoding="utf-8")
        row = sql.split(f"'{CHECK_ID}',", 1)[1].split("UNION ALL", 1)[0]
        assert "'stamping_trigger_mismatches', 0" in row, asset.name
        assert "'trigger_function_mismatches', 0" in row, asset.name


def test_the_json_manifest_declares_the_fingerprint_check() -> None:
    checks = json.loads(V5_JSON.read_text(encoding="utf-8"))["checks"]
    entry = next(check for check in checks if check["id"] == CHECK_ID)
    assert entry == {"id": CHECK_ID, "kind": "brain_schema_invariant", "name": CHECK_ID}
