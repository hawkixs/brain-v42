"""Contract of the HNSW churn bench: disposable, bounded, honest about its provenance.

The bench carries a COPY of the real production embeddings, in `trust`, with no
password. `tests/support/run_task0_fixture_impl.sh` has already settled the rule
for a disposable bench with SYNTHETIC data (`trap cleanup_pg16 EXIT INT TERM`,
pinned by `test_task0_fixture_cleanup.py`); a bench with PRODUCTION data cannot
receive a weaker guarantee.

Each pin below closes a defect MEASURED during the adversarial review of PR #41
(2026-08-28), not a hypothetical one:

* without a trap, the bench outlived the script, indefinitely;
* a bench with empty tables passed the `idx_scan` guard (`+0/0`) and exited 0
  with a "zero churn" report;
* `docker compose -f` without `-p` derives the project from the folder
  (`support`) and two benches with different names destroyed each other;
* the reuse branch never re-read `SRC_TABLE`: a re-run with
  `SRC_TABLE=decisions` silently re-measured `learnings`;
* `row_number() over ()` without ORDER BY numbers the scan position, not the
  distance rank — measured wrong on the `truth` block;
* the probes all lived at ~0.025 cosine distance while real queries land far
  further out (measured d(1-NN) band 0.282–0.325, printed by the bench on every
  run);
* the `BUILDS=3` default could not produce the published "ten build pairs"
  (C(5,2) are needed);
* the probes came out of an unseeded `random()`, in a bench that sells
  reproducibility.
"""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "hnsw_churn_measure.sh"
COMPOSE_FILE = REPOSITORY_ROOT / "tests" / "support" / "hnsw-churn-compose.yml"


def test_bench_is_destroyed_on_exit() -> None:
    """A bench carrying production embeddings does not outlive the script."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "cleanup_bench()" in source
    assert "trap cleanup_bench EXIT" in source
    # `--remove-orphans` has no legitimate effect on a dedicated project, and it
    # widens what `down` can take away: banned.
    assert "--remove-orphans" not in source
    assert 'docker rm -f "$C"' in source
    # The `docker rm -f` safety net does not delete the project's network, and a
    # failing down must not stay silent.
    assert 'docker network rm "${C}_default"' in source
    assert "AVERTISSEMENT" in source


def test_interrupts_stop_the_script_after_teardown() -> None:
    """`trap cmd INT` without `exit` destroys the bench then CONTINUES the script
    (verified in re-review): each signal must end with its own exit."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "trap 'trap - EXIT; cleanup_bench; exit 130' INT" in source
    assert "trap 'trap - EXIT; cleanup_bench; exit 143' TERM" in source


def test_cleanup_never_destroys_what_the_bench_did_not_mark() -> None:
    """`docker compose -p "$C" down` matches project+service BY LABEL: with
    CHURN_CONTAINER=brain_v42, the trap took brain_v42_postgres away — reproduced
    twice in re-review, from the very first provision, stderr discarded. The bench
    therefore marks what it creates (container AND network label), refuses a name
    already borne by someone else, and NEVER destroys an object without its
    marker."""
    source = SCRIPT.read_text(encoding="utf-8")
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    label = "com.brain-v42.hnsw-churn-bench"
    assert f'BENCH_LABEL="{label}"' in source
    assert compose.count(f'{label}: "true"') == 2, (
        "le marqueur doit être posé sur le conteneur ET sur le réseau"
    )

    # Refusal of the production name and of any foreign bearer BEFORE arming the
    # trap: a trap armed before the guard would destroy at the very moment of the
    # refusal.
    assert 'if [ "$C" = "$PROD" ]; then' in source
    assert "refuse_foreign_name" in source
    assert source.index("refuse_foreign_name\n") < source.index("trap cleanup_bench EXIT"), (
        "la garde d'homonymie doit s'exécuter avant l'armement du trap"
    )

    # The destruction lives behind the marker, never before it.
    cleanup = source.split("cleanup_bench() {", 1)[1].split("\n}", 1)[0]
    assert 'bench_marked_container "$C"' in cleanup
    assert "bench_marked_network" in cleanup


def test_every_compose_invocation_is_project_scoped() -> None:
    """Without `-p`, compose reconciles on (folder, service): two benches with
    different names share the `support` project and destroy each other —
    collateral destruction reproduced during the review."""
    source = SCRIPT.read_text(encoding="utf-8")

    compose_lines = [line for line in source.splitlines() if "docker compose" in line]
    assert compose_lines, "le banc doit passer par docker compose (digest épinglé)"
    for line in compose_lines:
        assert '-p "$C"' in line, f"invocation compose sans projet explicite : {line.strip()}"


def test_the_script_does_not_shadow_the_compose_file_variable() -> None:
    """`COMPOSE_FILE` is an environment variable read by docker compose:
    assigning it in the script would silently overwrite an inherited export."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "COMPOSE_FILE=" not in source


def test_an_empty_bench_is_refused_before_the_first_build() -> None:
    """corpus=0 defeats the `idx_scan` guard arithmetically: `+0/0` passes
    `[ "$USED" -ne "$NPROBE" ]` and the run exits 0 having measured nothing.
    BUILDS=1 makes the churn section empty at exit 0 (no pair); BUILDS=0 skips all
    the way to the idx_scan guard itself."""
    source = SCRIPT.read_text(encoding="utf-8")

    guard = 'if [ "$CORPUS" -eq 0 ] || [ "$NPROBE" -eq 0 ]; then'
    assert guard in source
    assert source.index(guard) < source.index("using hnsw"), (
        "la garde de banc vide doit précéder la première reconstruction"
    )
    # The guard must EXIT, not merely speak: exit 2 inside its block.
    empty_block = source.split(guard, 1)[1].split("fi\n", 1)[0]
    assert "exit 2" in empty_block
    assert '[ "$BUILDS" -ge 2 ] || {' in source
    builds_guard = source.split('[ "$BUILDS" -ge 2 ] || {', 1)[1].split("}", 1)[0]
    assert "exit 2" in builds_guard


def test_provenance_is_recorded_and_checked_on_reuse() -> None:
    """The bench writes down where its corpus comes from and refuses to reuse a
    corpus from another table, another container — or another EPOCH: a surviving
    bench on the same table was never re-copied after corpus growth, exactly the
    case the "re-run after corpus growth" instruction claims to cover."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "create table meta" in source
    for column in ("src_table", "prod_container", "copied_at", "seed"):
        assert column in source
    # The meta↔environment comparison lives in an IF whose branch REPROVISIONS —
    # not a mere echo.
    mismatch = 'if [ "$META" != "$SRC_TABLE|$PROD" ]; then'
    assert mismatch in source
    mismatch_block = source.split(mismatch, 1)[1].split("fi\n", 1)[0]
    assert "provision" in mismatch_block
    # Same provenance ≠ same corpus: the source count is replayed and a mismatch
    # reprovisions too.
    drift = 'if [ "$SRC_COUNT" -ne "$BENCH_COUNT" ]; then'
    assert drift in source
    drift_block = source.split(drift, 1)[1].split("fi\n", 1)[0]
    assert "provision" in drift_block


def test_probes_are_seeded() -> None:
    """The compose sells reproducibility; the probes therefore come out of a
    seeded `random()`, and the seed is printed with the measurement."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "setseed($SEED)" in source
    assert "SEED=${SEED:-" in source


def test_rank_is_the_distance_rank_in_both_blocks() -> None:
    """`row_number() over ()` numbers the scan position: measured, the exact block
    placed the WindowAgg BELOW the Sort and `truth.rank` was 1..N over the whole
    table instead of 1..10."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "row_number() over ()" not in source
    assert source.count("row_number() over (order by embedding <=> r.v)") == 2


def test_builds_default_produces_the_published_ten_pairs() -> None:
    """The runbook publishes "ten build pairs": C(5,2)=10, not C(3,2)=3."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "BUILDS=${BUILDS:-5}" in source


def test_probes_cover_the_realistic_query_distance() -> None:
    """A bench that only probes at 0.025 demonstrates stability for near-copies
    alone. The noise coefficients are pinned: ±0.01/dim for the near-copy group,
    ±0.045/dim for the realistic group — measured d(1-NN) 0.282–0.325 on learnings,
    the band the script PRINTS on every run."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "'proche'" in source
    assert "'realiste'" in source
    assert "(random()-0.5)*0.02" in source
    assert "(random()-0.5)*0.09" in source


def test_bench_never_touches_the_default_bridge() -> None:
    """`network_mode: bridge` put the production corpus, in trust, on the default
    Docker bridge — reachable without a token by any co-resident container
    (reproduced during the review: a superuser connection from a disposable
    container placed on that bridge). The bench lives on ITS OWN compose project's
    internal network: everything goes through `docker exec`, no network traffic is
    needed, and the trap's `down` takes the network away with it."""
    compose = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "network_mode" not in compose
    assert "ports:" not in compose
    assert "internal: true" in compose
