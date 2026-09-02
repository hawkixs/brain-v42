#!/usr/bin/env bash
# Measures the HNSW rebuild CHURN on the corpus's REAL embeddings.
#
# POURQUOI CE SCRIPT EXISTE
# Ticket cfd26e9d established two facts: `pg_dump` carries no HNSW graph
# at all (9 `CREATE INDEX ... USING hnsw`, zero bytes of index), and an
# HNSW rebuild is not deterministic. The reference measurement on the
# real embeddings lives in
# `docs/runbooks/2026-08-23-hnsw-restore-churn-declaration.md` (n=1544
# queries, the 9 tables, noise bands and operator probe); this script is
# the REPLAYABLE instrument, one table at a time, for refreshing the
# runbook's churn line as the corpus grows. It does not replace it.
#
# NEVER TOUCHES PRODUCTION: the vectors are read by a `COPY ... TO STDOUT`
# (read-only) and everything else lives in an ephemeral container on tmpfs,
# destroyed by a trap on exit — the bench holds a copy of the production
# embeddings in trust, and must not outlive the script.
#
# THREE TRAPS, all met while writing or re-reading this script:
#
# 1. HNSW requires a CONSTANT operand. Writing the query as a join
#    (`from corpus, probe ... order by corpus.embedding <=> probe.v`) yields a
#    Nested Loop + Seq Scan: the index is NOT used. We would then measure the
#    EXACT search three times, obtain 10.00/10, and conclude "no churn". A
#    perfectly credible false green. Hence the plpgsql loop, which passes the
#    vector as a parameter.
#
# 2. An EXPLAIN guard proves the PLAN, not the EXECUTION. So we check the
#    `pg_stat_user_indexes.idx_scan` counter: it must advance by exactly one
#    per probe. That is proof of execution, not of intent. It is
#    arithmetically DEFEATED at zero probes — hence the empty-bench guard
#    below, without which an interrupted COPY produces a "zero churn" at exit 0.
#
# 3. `row_number()` WITHOUT a window order numbers the SCAN POSITION. On the
#    exact path, the planner places the WindowAgg below the Sort: the rank
#    written was 1..N across the whole table instead of 1..10 (measured). The
#    rank is therefore explicitly ordered by distance, in BOTH blocks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Do NOT name this variable COMPOSE_FILE: the compose CLI reads an environment
# variable of that name, and the assignment would clobber an inherited export.
BENCH_COMPOSE="$SCRIPT_DIR/../tests/support/hnsw-churn-compose.yml"

C=${CHURN_CONTAINER:-brain_v42_hnsw_churn}
BUILDS=${BUILDS:-5}                 # C(5,2)=10 — les « dix paires » du runbook
SRC_TABLE=${SRC_TABLE:-learnings}   # la plus grosse des 9 tables à index HNSW
PROD=${PROD_CONTAINER:-brain_v42_postgres}
SEED=${SEED:-0.42}
export CHURN_CONTAINER="$C"
q() { docker exec "$C" psql -U churn -d churn -Atc "$1"; }
qq() { docker exec "$C" psql -U churn -d churn -q -c "$1"; }

# The bench MARKS what it creates — a label set by the compose on the container
# AND on the network — and destroys NOTHING that does not carry that marker.
# Without this guard, `docker compose -p "$C" down` matches project+service BY
# LABEL: with CHURN_CONTAINER=brain_v42, the trap took brain_v42_postgres away on
# the very first provision, stderr discarded — reproduced twice in re-review.
BENCH_LABEL="com.brain-v42.hnsw-churn-bench"

bench_marked_container() {
  docker container inspect --format "{{ index .Config.Labels \"$BENCH_LABEL\" }}" "$1" 2>/dev/null \
    | grep -qx true
}
bench_marked_network() {
  docker network inspect --format "{{ index .Labels \"$BENCH_LABEL\" }}" "$1" 2>/dev/null \
    | grep -qx true
}

# `-p "$C"` everywhere: with no explicit project, compose derives the project
# from the file's folder (`support`) and reconciles on (project, service) — two
# benches with different CHURN_CONTAINER would recreate one another.
# No orphan-removal option on the down: no legitimate effect on a dedicated
# project, and it widens what the down is able to take away.
cleanup_bench() {
  if bench_marked_container "$C"; then
    if ! docker compose -p "$C" -f "$BENCH_COMPOSE" down --timeout 5 >/dev/null 2>&1; then
      echo "AVERTISSEMENT : le down compose a échoué sur le banc $C — filet rm -f + network rm." >&2
    fi
    docker rm -f "$C" >/dev/null 2>&1 || true
  fi
  # An `up` interrupted before the container is created leaves the network
  # behind, and the `docker rm -f` net never removes a network. The marker is
  # required here too: we remove only the network this bench created.
  if bench_marked_network "${C}_default"; then
    docker network rm "${C}_default" >/dev/null 2>&1 || true
  fi
}

# --- name-collision guard — BEFORE arming any trap --------------------------
# A trap armed before this guard would destroy at the very moment of refusal.
if [ "$C" = "$PROD" ]; then
  echo "ABANDON : CHURN_CONTAINER=$C désigne le conteneur de production ($PROD)." >&2
  exit 2
fi

refuse_foreign_name() {
  local holder name
  for holder in $(docker ps -aq --filter "label=com.docker.compose.project=$C"); do
    name=$(docker container inspect --format '{{ .Name }}' "$holder" | sed 's|^/||')
    if ! bench_marked_container "$name"; then
      echo "ABANDON : le projet compose '$C' appartient déjà à '$name', que ce banc n'a pas créé." >&2
      echo "Le détruire par homonymie est exactement l'accident que cette garde ferme." >&2
      exit 2
    fi
  done
  if docker container inspect "$C" >/dev/null 2>&1 && ! bench_marked_container "$C"; then
    echo "ABANDON : un conteneur étranger porte déjà le nom '$C'." >&2
    exit 2
  fi
  if docker network inspect "${C}_default" >/dev/null 2>&1 && ! bench_marked_network "${C}_default"; then
    echo "ABANDON : un réseau étranger porte déjà le nom '${C}_default'." >&2
    exit 2
  fi
}
refuse_foreign_name

trap cleanup_bench EXIT
trap 'trap - EXIT; cleanup_bench; exit 130' INT
trap 'trap - EXIT; cleanup_bench; exit 143' TERM

# --- provisionnement ---------------------------------------------------------
# The bench is described by `tests/support/hnsw-churn-compose.yml`, NOT by a
# `docker run`: it thereby inherits the pinned digest, hence a reproducible
# measurement. The `scripts/check_container_image_pins.py` gate refuses an
# unpinned `docker run` anyway, and it is right to here.
#
# The bench reproduces the VERSIONED TARGET, not the production runtime: the
# two have diverged (see the compose file's header).
provision() {
  echo "== provisionnement du banc isolé =="
  cleanup_bench
  docker compose -p "$C" -f "$BENCH_COMPOSE" up --detach --no-build >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$C" pg_isready -U churn -d churn >/dev/null 2>&1 && break
    sleep 1
  done
  qq "create extension vector;
      create table corpus (id uuid primary key, embedding vector(1536));
      create table probe  (qid int primary key, grp text not null, v vector(1536));
      create table res    (build int, qid int, rank int, id uuid);
      create table truth  (qid int, rank int, id uuid, d float8);
      create table meta   (src_table text not null, prod_container text not null,
                           copied_at timestamptz not null, seed float8 not null);"
  # READ-ONLY against production.
  docker exec "$PROD" psql -U brain -d brain -Atc \
    "copy (select id, embedding from $SRC_TABLE where embedding is not null) to stdout" \
  | docker exec -i "$C" psql -U churn -d churn -q -c "copy corpus (id, embedding) from stdin"
  qq "insert into meta (src_table, prod_container, copied_at, seed)
      values ('$SRC_TABLE', '$PROD', now(), $SEED);"
  # NON-MEMBER probes of the corpus, in TWO groups, the seed set in the SAME
  # session as the random() calls it governs:
  # - 'proche'   : noise ±0.01/dim → cosine distance ≈ 0.025 from the base. A
  #   probe that IS in the corpus has its top-1 at zero distance, which biases
  #   towards stability — a trap met on the first draft.
  # - 'realiste' : noise ±0.045/dim — measured d(1-NN) 0.282–0.325 on the real
  #   learnings embeddings (2026-08-29, seed 0.42). The band obtained is
  #   PRINTED on every run: that is what counts, not this comment.
  qq "select setseed($SEED);
      insert into probe (qid, grp, v)
      select qid, 'proche', (select array_agg(e + (random()-0.5)*0.02)::vector
                   from unnest(base::real[]) e)
      from (select row_number() over (order by md5(id::text)) qid,
                   embedding::real[]::text::real[] base
            from corpus order by md5(id::text) limit 20) s;
      insert into probe (qid, grp, v)
      select qid + 20, 'realiste', (select array_agg(e + (random()-0.5)*0.09)::vector
                   from unnest(base::real[]) e)
      from (select row_number() over (order by md5(id::text)) qid,
                   embedding::real[]::text::real[] base
            from corpus order by md5(id::text) limit 20) s;"
  echo
}

if ! docker exec "$C" pg_isready -U churn -d churn >/dev/null 2>&1; then
  provision
else
  # A bench surviving a run killed before its trap. Reuse it only if its
  # provenance matches the request: without this check, a re-run with
  # `SRC_TABLE=decisions` would silently re-measure the old table.
  META=$(q "select src_table||'|'||prod_container from meta;" 2>/dev/null || true)
  if [ "$META" != "$SRC_TABLE|$PROD" ]; then
    echo "== banc réutilisé de provenance divergente (${META:-aucune}) ≠ $SRC_TABLE|$PROD — reprovisionnement =="
    provision
  else
    # Same provenance != same corpus: the runbook's "re-run after corpus
    # growth" instruction would reuse the OLD copy and "prove" that the growth
    # changed nothing. The source count is replayed, read-only.
    SRC_COUNT=$(docker exec "$PROD" psql -U brain -d brain -Atc \
      "select count(*) from $SRC_TABLE where embedding is not null;")
    BENCH_COUNT=$(q "select count(*) from corpus;")
    if [ "$SRC_COUNT" -ne "$BENCH_COUNT" ]; then
      echo "== banc réutilisé mais la source a bougé ($SRC_COUNT lignes ≠ $BENCH_COUNT copiées) — reprovisionnement =="
      provision
    fi
  fi
fi

CORPUS=$(q "select count(*) from corpus;")
NPROBE=$(q "select count(*) from probe;")
if [ "$CORPUS" -eq 0 ] || [ "$NPROBE" -eq 0 ]; then
  echo "ABANDON : corpus=$CORPUS · sondes=$NPROBE — banc vide, rien à mesurer." >&2
  echo "Un COPY interrompu laisse exactement cet état ; la garde idx_scan (+0/0) ne le voit pas." >&2
  exit 2
fi
[ "$BUILDS" -ge 2 ] || {
  echo "ABANDON : BUILDS=$BUILDS — il faut au moins 2 reconstructions pour une paire." >&2
  echo "À 1 la section churn sortirait vide en exit 0 ; à 0 la garde idx_scan n'est jamais évaluée." >&2
  exit 2
}

echo "provenance : $(q "select 'src='||src_table||' · prod='||prod_container||' · copié le '||copied_at||' · seed='||seed from meta;")"
echo "corpus=$CORPUS vecteurs réels · sondes=$NPROBE (20 'proche' + 20 'realiste' — bandes d(1-NN) mesurées, imprimées plus bas) · builds=$BUILDS"
echo "vector $(q "select extversion from pg_extension where extname='vector';") · chemin d'index FORCÉ (set enable_seqscan=off) — la prod, elle, seq-scanne $SRC_TABLE aujourd'hui (déclaration 2026-08-23, §0)"
echo

echo "== vérité terrain : recherche EXACTE (aucun index) =="
qq "drop index if exists idx_corpus_embedding;"
qq "truncate truth;
    do \$\$
    declare r record;
    begin
      for r in select qid, v from probe order by qid loop
        insert into truth (qid, rank, id, d)
        select r.qid, t.rn, t.id, t.dist
        from (select id, row_number() over (order by embedding <=> r.v) rn,
                     embedding <=> r.v dist
              from corpus order by embedding <=> r.v limit 10) t;
      end loop;
    end \$\$;"
echo "   $(q "select count(*) from truth;") lignes de référence"
# The probe distance is a SETTING, not a fact: print the band actually
# obtained, so the runbook cites a measurement and not an intention.
q "select '   '||p.grp||' : d(1-NN) min '||round(min(t.d)::numeric,3)
      ||' · moyenne '||round(avg(t.d)::numeric,3)
      ||' · max '||round(max(t.d)::numeric,3)
   from truth t join probe p on p.qid=t.qid
   where t.rank=1 group by p.grp order by p.grp;"
echo

echo "== $BUILDS reconstructions HNSW (m=16, ef_construction=64, cosine) =="
qq "truncate res;"
for b in $(seq 1 "$BUILDS"); do
  qq "drop index if exists idx_corpus_embedding;"
  T0=$(date +%s)
  qq "create index idx_corpus_embedding on corpus using hnsw (embedding vector_cosine_ops) with (m='16', ef_construction='64');"
  T1=$(date +%s)

  BEFORE=$(q "select coalesce(idx_scan,0) from pg_stat_user_indexes where indexrelname='idx_corpus_embedding';")
  qq "set enable_seqscan=off;
      do \$\$
      declare r record;
      begin
        for r in select qid, v from probe order by qid loop
          insert into res (build, qid, rank, id)
          select $b, r.qid, t.rn, t.id
          from (select id, row_number() over (order by embedding <=> r.v) rn
                from corpus order by embedding <=> r.v limit 10) t;
        end loop;
      end \$\$;"
  AFTER=$(q "select coalesce(idx_scan,0) from pg_stat_user_indexes where indexrelname='idx_corpus_embedding';")
  USED=$((AFTER - BEFORE))

  if [ "$USED" -ne "$NPROBE" ]; then
    echo "ABANDON build $b : idx_scan a avancé de $USED, attendu $NPROBE." >&2
    echo "L'index n'a pas servi à chaque sonde — la mesure serait un faux vert." >&2
    exit 2
  fi
  echo "   build $b : index en $((T1-T0))s · idx_scan +$USED/$NPROBE — usage prouvé à l'exécution"
done

echo
echo "== CHURN : recouvrement entre reconstructions, par groupe de sondes =="
q "select p.grp||' · builds '||a.build||'×'||b.build
      ||' : recouvrement '||round(avg(ov)::numeric,2)||'/10'
      ||' · top-10 identiques (ensemble) '||count(*) filter (where ov=10)||'/'||count(*)
      ||' · identiques (ordre strict) '||count(*) filter (where a.ids=b.ids)||'/'||count(*)
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build
   join probe p on p.qid=a.qid,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by p.grp, a.build, b.build order by p.grp, a.build, b.build;"

echo
echo "== SYNTHÈSE churn (toutes paires confondues) =="
q "select p.grp||' : recouvrement moyen '||round(avg(ov)::numeric,2)||'/10'
      ||' · ensembles identiques '||count(*) filter (where ov=10)||'/'||count(*)
      ||' · ordre strict '||count(*) filter (where a.ids=b.ids)||'/'||count(*)
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build
   join probe p on p.qid=a.qid,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by p.grp order by p.grp;"

echo
echo "== RAPPEL vs recherche exacte (ce qu'un opérateur perd réellement) =="
q "select p.grp||' · build '||r.build||' : rappel '||round(avg(ov)::numeric,2)||'/10'
      ||' · top-10 exacts '||count(*) filter (where ov=10)||'/'||count(*)
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) r
   join (select qid, array_agg(id order by rank) ids from truth group by qid) t on t.qid=r.qid
   join probe p on p.qid=r.qid,
   lateral (select count(*) ov from unnest(r.ids) x where x = any(t.ids)) o
   group by p.grp, r.build order by p.grp, r.build;"

echo
echo "== MANQUES DE RAPPEL : le Δd dit s'ils comptent (règle de la déclaration 2026-08-23 :"
echo "   comparer les DISTANCES, jamais les listes d'identifiants — Δd=0 est un départage d'égalité) =="
q "select p.grp||' · build '||x.build||' · sonde '||x.qid
      ||' : intrus à Δd='||round((cd.d - tmax.dmax)::numeric, 6)
   from (select r.build, r.qid, r.id from res r
         where not exists (select 1 from truth t where t.qid=r.qid and t.id=r.id)) x
   join probe p on p.qid=x.qid
   join lateral (select c.embedding <=> p.v d from corpus c where c.id=x.id) cd on true
   join lateral (select max(t.d) dmax from truth t where t.qid=x.qid) tmax on true
   order by p.grp, x.qid, x.build;"

echo
echo "== PIRE CAS PAR SONDE (la moyenne cache les sondes instables) =="
q "select p.grp||' · sonde '||a.qid||' : recouvrement min '||min(ov)||'/10'
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build
   join probe p on p.qid=a.qid,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by p.grp, a.qid having min(ov) < 10 order by p.grp, min(ov), a.qid;"
