"""Contrat du banc churn HNSW : jetable, borné, honnête sur sa provenance.

Le banc porte une COPIE des embeddings réels de production, en `trust`, sans
mot de passe. `tests/support/run_task0_fixture_impl.sh` a déjà tranché la règle
pour un banc jetable aux données SYNTHÉTIQUES (`trap cleanup_pg16 EXIT INT
TERM`, épinglé par `test_task0_fixture_cleanup.py`) ; un banc aux données de
PRODUCTION ne peut pas recevoir une garantie plus faible.

Chaque pin ci-dessous ferme un défaut MESURÉ lors de la review adverse de la
PR #41 (2026-08-28), pas un défaut hypothétique :

* sans trap, le banc survivait au script, indéfiniment ;
* un banc aux tables vides passait la garde `idx_scan` (`+0/0`) et sortait
  en 0 avec un rapport « churn nul » ;
* `docker compose -f` sans `-p` dérive le projet du dossier (`support`) et
  deux bancs aux noms différents se détruisaient mutuellement ;
* la branche de réutilisation ne relisait jamais `SRC_TABLE` : une relance
  `SRC_TABLE=decisions` remesurait `learnings` en silence ;
* `row_number() over ()` sans ORDER BY numérote la position de scan, pas le
  rang de distance — mesuré faux sur le bloc `truth` ;
* les sondes vivaient toutes à ~0,025 de distance cosinus quand les vraies
  requêtes atterrissent bien plus loin (bande d(1-NN) mesurée 0,282–0,325,
  imprimée par le banc à chaque run) ;
* le défaut `BUILDS=3` ne pouvait pas produire les « dix paires de builds »
  publiées (il en faut C(5,2)) ;
* les sondes sortaient d'un `random()` non grainé, dans un banc qui vend la
  reproductibilité.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "hnsw_churn_measure.sh"
COMPOSE_FILE = REPOSITORY_ROOT / "tests" / "support" / "hnsw-churn-compose.yml"


def test_bench_is_destroyed_on_exit() -> None:
    """Un banc portant des embeddings de prod ne survit pas au script."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "cleanup_bench()" in source
    assert "trap cleanup_bench EXIT" in source
    # `--remove-orphans` n'a aucun effet légitime sur un projet dédié, et il
    # élargit ce que `down` peut emporter : banni.
    assert "--remove-orphans" not in source
    assert 'docker rm -f "$C"' in source
    # Le filet `docker rm -f` ne supprime pas le réseau du projet, et un down
    # qui échoue ne doit pas se taire.
    assert 'docker network rm "${C}_default"' in source
    assert "AVERTISSEMENT" in source


def test_interrupts_stop_the_script_after_teardown() -> None:
    """`trap cmd INT` sans `exit` détruit le banc puis CONTINUE le script
    (vérifié en re-review) : chaque signal doit se terminer par son exit."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "trap 'trap - EXIT; cleanup_bench; exit 130' INT" in source
    assert "trap 'trap - EXIT; cleanup_bench; exit 143' TERM" in source


def test_cleanup_never_destroys_what_the_bench_did_not_mark() -> None:
    """`docker compose -p "$C" down` matche projet+service PAR LABEL : avec
    CHURN_CONTAINER=brain_v42, le trap emportait brain_v42_postgres — reproduit
    deux fois en re-review, dès la première provision, stderr jeté. Le banc
    marque donc ce qu'il crée (label conteneur ET réseau), refuse un nom déjà
    porté par autrui, et ne détruit JAMAIS un objet sans son marqueur."""
    source = SCRIPT.read_text(encoding="utf-8")
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    label = "com.brain-v42.hnsw-churn-bench"
    assert f'BENCH_LABEL="{label}"' in source
    assert compose.count(f'{label}: "true"') == 2, (
        "le marqueur doit être posé sur le conteneur ET sur le réseau"
    )

    # Refus du nom de prod et de tout porteur étranger AVANT d'armer le trap :
    # un trap armé avant la garde détruirait au moment même du refus.
    assert 'if [ "$C" = "$PROD" ]; then' in source
    assert "refuse_foreign_name" in source
    assert source.index("refuse_foreign_name\n") < source.index("trap cleanup_bench EXIT"), (
        "la garde d'homonymie doit s'exécuter avant l'armement du trap"
    )

    # La destruction vit derrière le marqueur, jamais avant.
    cleanup = source.split("cleanup_bench() {", 1)[1].split("\n}", 1)[0]
    assert 'bench_marked_container "$C"' in cleanup
    assert "bench_marked_network" in cleanup


def test_every_compose_invocation_is_project_scoped() -> None:
    """Sans `-p`, compose réconcilie sur (dossier, service) : deux bancs aux
    noms différents partagent le projet `support` et se détruisent l'un
    l'autre — destruction collatérale reproduite pendant la review."""
    source = SCRIPT.read_text(encoding="utf-8")

    compose_lines = [line for line in source.splitlines() if "docker compose" in line]
    assert compose_lines, "le banc doit passer par docker compose (digest épinglé)"
    for line in compose_lines:
        assert '-p "$C"' in line, f"invocation compose sans projet explicite : {line.strip()}"


def test_the_script_does_not_shadow_the_compose_file_variable() -> None:
    """`COMPOSE_FILE` est une variable d'environnement lue par docker compose :
    l'assigner dans le script écraserait un export hérité en silence."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "COMPOSE_FILE=" not in source


def test_an_empty_bench_is_refused_before_the_first_build() -> None:
    """corpus=0 défait la garde `idx_scan` arithmétiquement : `+0/0` passe
    `[ "$USED" -ne "$NPROBE" ]` et le run sort en 0 sans rien avoir mesuré.
    BUILDS=1 rend la section churn vide en exit 0 (aucune paire) ; BUILDS=0
    saute jusqu'à la garde idx_scan elle-même."""
    source = SCRIPT.read_text(encoding="utf-8")

    guard = 'if [ "$CORPUS" -eq 0 ] || [ "$NPROBE" -eq 0 ]; then'
    assert guard in source
    assert source.index(guard) < source.index("using hnsw"), (
        "la garde de banc vide doit précéder la première reconstruction"
    )
    # La garde doit SORTIR, pas seulement parler : exit 2 dans son bloc.
    empty_block = source.split(guard, 1)[1].split("fi\n", 1)[0]
    assert "exit 2" in empty_block
    assert '[ "$BUILDS" -ge 2 ] || {' in source
    builds_guard = source.split('[ "$BUILDS" -ge 2 ] || {', 1)[1].split("}", 1)[0]
    assert "exit 2" in builds_guard


def test_provenance_is_recorded_and_checked_on_reuse() -> None:
    """Le banc écrit d'où vient son corpus et refuse de resservir un corpus
    d'une autre table, d'un autre conteneur — ou d'une autre ÉPOQUE : un banc
    survivant de même table n'était jamais re-copié après croissance du
    corpus, exactement le cas que la consigne « re-run after corpus growth »
    prétend couvrir."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "create table meta" in source
    for column in ("src_table", "prod_container", "copied_at", "seed"):
        assert column in source
    # La comparaison meta↔environnement vit dans un IF dont la branche
    # REPROVISIONNE — pas un simple écho.
    mismatch = 'if [ "$META" != "$SRC_TABLE|$PROD" ]; then'
    assert mismatch in source
    mismatch_block = source.split(mismatch, 1)[1].split("fi\n", 1)[0]
    assert "provision" in mismatch_block
    # Même provenance ≠ même corpus : le compte source est rejoué et un écart
    # reprovisionne aussi.
    drift = 'if [ "$SRC_COUNT" -ne "$BENCH_COUNT" ]; then'
    assert drift in source
    drift_block = source.split(drift, 1)[1].split("fi\n", 1)[0]
    assert "provision" in drift_block


def test_probes_are_seeded() -> None:
    """Le compose vend la reproductibilité ; les sondes sortent donc d'un
    `random()` grainé, et la graine est imprimée avec la mesure."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "setseed($SEED)" in source
    assert "SEED=${SEED:-" in source


def test_rank_is_the_distance_rank_in_both_blocks() -> None:
    """`row_number() over ()` numérote la position de scan : mesuré, le bloc
    exact plaçait la WindowAgg SOUS le Sort et `truth.rank` valait 1..N sur
    toute la table au lieu de 1..10."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "row_number() over ()" not in source
    assert source.count("row_number() over (order by embedding <=> r.v)") == 2


def test_builds_default_produces_the_published_ten_pairs() -> None:
    """Le runbook publie « dix paires de builds » : C(5,2)=10, pas C(3,2)=3."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "BUILDS=${BUILDS:-5}" in source


def test_probes_cover_the_realistic_query_distance() -> None:
    """Un banc qui ne sonde qu'à 0,025 ne démontre la stabilité que pour des
    quasi-copies. Les coefficients de bruit sont épinglés : ±0,01/dim pour le
    groupe quasi-copie, ±0,045/dim pour le groupe réaliste — mesuré d(1-NN)
    0,282–0,325 sur learnings, la bande que le script IMPRIME à chaque run."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "'proche'" in source
    assert "'realiste'" in source
    assert "(random()-0.5)*0.02" in source
    assert "(random()-0.5)*0.09" in source


def test_bench_never_touches_the_default_bridge() -> None:
    """`network_mode: bridge` posait le corpus de prod, en trust, sur le
    bridge Docker par défaut — joignable sans jeton par tout conteneur
    co-résident (reproduit pendant la review : connexion superuser depuis un
    conteneur jetable posé sur ce bridge). Le banc vit sur le réseau interne
    de SON projet compose : tout passe par `docker exec`, aucun trafic réseau
    n'est nécessaire, et le `down` du trap emporte le réseau avec lui."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "network_mode" not in compose
    assert "ports:" not in compose
    assert "internal: true" in compose
