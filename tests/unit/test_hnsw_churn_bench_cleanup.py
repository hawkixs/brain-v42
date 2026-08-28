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
  requêtes atterrissent à 0,25-0,35 ;
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
    assert "trap cleanup_bench EXIT INT TERM" in source
    # Le down compose emporte le conteneur ET le réseau du projet ; le rm -f
    # est le filet si compose lui-même est indisponible.
    assert "down --remove-orphans" in source
    assert 'docker rm -f "$C"' in source


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
    `[ "$USED" -ne "$NPROBE" ]` et le run sort en 0 sans rien avoir mesuré."""
    source = SCRIPT.read_text(encoding="utf-8")

    guard = 'if [ "$CORPUS" -eq 0 ] || [ "$NPROBE" -eq 0 ]; then'
    assert guard in source
    assert source.index(guard) < source.index("using hnsw"), (
        "la garde de banc vide doit précéder la première reconstruction"
    )


def test_provenance_is_recorded_and_checked_on_reuse() -> None:
    """Le banc écrit d'où vient son corpus et refuse de resservir un corpus
    d'une autre table ou d'un autre conteneur sans reprovisionner."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "create table meta" in source
    for column in ("src_table", "prod_container", "copied_at", "seed"):
        assert column in source
    # La comparaison meta↔environnement vit HORS du bloc de provisionnement :
    # c'est la branche de réutilisation qui mentait.
    assert '"$META" != "$SRC_TABLE|$PROD"' in source


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
    """Les vraies requêtes atterrissent à 0,25-0,35 de distance cosinus
    (mesuré sur 12 questions via l'endpoint :8003) ; un banc qui ne sonde
    qu'à 0,025 ne démontre la stabilité que pour des quasi-copies."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "'proche'" in source
    assert "'realiste'" in source


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
