#!/usr/bin/env bash
# Mesure le CHURN de reconstruction HNSW sur les embeddings RÉELS du corpus.
#
# POURQUOI CE SCRIPT EXISTE
# Le ticket cfd26e9d a établi deux faits : `pg_dump` ne transporte aucun graphe
# HNSW (9 `CREATE INDEX ... USING hnsw`, zéro octet d'index), et une
# reconstruction HNSW n'est pas déterministe. La mesure de référence sur les
# embeddings réels vit dans
# `docs/runbooks/2026-08-23-hnsw-restore-churn-declaration.md` (n=1544
# requêtes, les 9 tables, bandes de bruit et sonde opérateur) ; ce script est
# l'instrument REJOUABLE, une table à la fois, pour rafraîchir la ligne churn
# du runbook quand le corpus grossit. Il ne remplace pas la déclaration.
#
# NE TOUCHE JAMAIS LA PRODUCTION : les vecteurs sont lus par un
# `COPY ... TO STDOUT` (lecture seule) et tout le reste vit dans un conteneur
# éphémère sur tmpfs, détruit par trap à la sortie — le banc porte une copie
# des embeddings de production en trust, il ne doit pas survivre au script.
#
# TROIS PIÈGES, tous rencontrés en écrivant ou en relisant ce script :
#
# 1. HNSW exige un opérande CONSTANT. Formuler la requête en jointure
#    (`from corpus, probe ... order by corpus.embedding <=> probe.v`) donne un
#    Nested Loop + Seq Scan : l'index n'est PAS emprunté. On mesurerait alors
#    trois fois la recherche EXACTE, on obtiendrait 10,00/10, et on conclurait
#    « aucun churn ». Un faux vert parfaitement crédible. D'où la boucle
#    plpgsql, qui passe le vecteur en paramètre.
#
# 2. Une garde par EXPLAIN prouve le PLAN, pas l'EXÉCUTION. On vérifie donc le
#    compteur `pg_stat_user_indexes.idx_scan` : il doit avancer d'exactement une
#    unité par sonde. C'est une preuve d'exécution, pas d'intention. Elle est
#    arithmétiquement DÉFAITE à zéro sonde — d'où la garde de banc vide plus
#    bas, sans laquelle un COPY interrompu produit un « churn nul » en exit 0.
#
# 3. `row_number()` SANS ordre de fenêtre numérote la POSITION DE SCAN. Sur le
#    chemin exact, le planner place la WindowAgg sous le Sort : le rang écrit
#    valait 1..N sur toute la table au lieu de 1..10 (mesuré). Le rang est donc
#    explicitement ordonné par la distance, dans les DEUX blocs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Ne PAS nommer cette variable COMPOSE_FILE : le CLI compose lit une variable
# d'environnement de ce nom, et l'assignation écraserait un export hérité.
BENCH_COMPOSE="$SCRIPT_DIR/../tests/support/hnsw-churn-compose.yml"

C=${CHURN_CONTAINER:-brain_v42_hnsw_churn}
BUILDS=${BUILDS:-5}                 # C(5,2)=10 — les « dix paires » du runbook
SRC_TABLE=${SRC_TABLE:-learnings}   # la plus grosse des 9 tables à index HNSW
PROD=${PROD_CONTAINER:-brain_v42_postgres}
SEED=${SEED:-0.42}
export CHURN_CONTAINER="$C"
q() { docker exec "$C" psql -U churn -d churn -Atc "$1"; }
qq() { docker exec "$C" psql -U churn -d churn -q -c "$1"; }

# `-p "$C"` partout : sans projet explicite, compose dérive le projet du
# dossier du fichier (`support`) et réconcilie sur (projet, service) — deux
# bancs aux CHURN_CONTAINER différents se recréeraient l'un l'autre.
cleanup_bench() {
  docker compose -p "$C" -f "$BENCH_COMPOSE" down --remove-orphans --timeout 5 >/dev/null 2>&1 || true
  docker rm -f "$C" >/dev/null 2>&1 || true
}
trap cleanup_bench EXIT INT TERM

# --- provisionnement ---------------------------------------------------------
# Le banc est décrit par `tests/support/hnsw-churn-compose.yml`, PAS par un
# `docker run` : il hérite ainsi du digest épinglé, donc d'une mesure
# reproductible. Le gate `scripts/check_container_image_pins.py` refuse de
# toute façon un `docker run` non épinglé, et il a raison ici.
#
# Le banc reproduit la CIBLE VERSIONNÉE, pas le runtime de production : les
# deux ont divergé (voir l'en-tête du fichier compose).
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
      create table truth  (qid int, rank int, id uuid);
      create table meta   (src_table text not null, prod_container text not null,
                           copied_at timestamptz not null, seed float8 not null);"
  # LECTURE SEULE sur la production.
  docker exec "$PROD" psql -U brain -d brain -Atc \
    "copy (select id, embedding from $SRC_TABLE where embedding is not null) to stdout" \
  | docker exec -i "$C" psql -U churn -d churn -q -c "copy corpus (id, embedding) from stdin"
  qq "insert into meta (src_table, prod_container, copied_at, seed)
      values ('$SRC_TABLE', '$PROD', now(), $SEED);"
  # Sondes NON-MEMBRES du corpus, en DEUX groupes, la graine posée dans la
  # MÊME session que les random() qu'elle gouverne :
  # - 'proche'   : bruit ±0,01/dim → distance cosinus ≈ 0,025 de la base. Une
  #   sonde qui EST dans le corpus a son top-1 à distance nulle, ce qui biaise
  #   vers la stabilité — piège rencontré au premier jet.
  # - 'realiste' : bruit ±0,045/dim → ≈ 0,25-0,35, la bande où atterrissent
  #   les vraies questions d'utilisateur (mesuré 2026-08-28 : d(q,1-NN)=0,297
  #   en moyenne sur 12 questions passées par l'endpoint :8003).
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
  # Banc survivant d'un run tué avant son trap. Ne le resservir que si sa
  # provenance correspond à la demande : sans ce contrôle, une relance
  # `SRC_TABLE=decisions` remesurait l'ancienne table en silence — et la
  # consigne du runbook « re-run after corpus growth » remesurait l'ancien
  # corpus en silence.
  META=$(q "select src_table||'|'||prod_container from meta;" 2>/dev/null || true)
  if [ "$META" != "$SRC_TABLE|$PROD" ]; then
    echo "== banc réutilisé de provenance divergente (${META:-aucune}) ≠ $SRC_TABLE|$PROD — reprovisionnement =="
    provision
  fi
fi

CORPUS=$(q "select count(*) from corpus;")
NPROBE=$(q "select count(*) from probe;")
if [ "$CORPUS" -eq 0 ] || [ "$NPROBE" -eq 0 ]; then
  echo "ABANDON : corpus=$CORPUS · sondes=$NPROBE — banc vide, rien à mesurer." >&2
  echo "Un COPY interrompu laisse exactement cet état ; la garde idx_scan (+0/0) ne le voit pas." >&2
  exit 2
fi

echo "provenance : $(q "select 'src='||src_table||' · prod='||prod_container||' · copié le '||copied_at||' · seed='||seed from meta;")"
echo "corpus=$CORPUS vecteurs réels · sondes=$NPROBE (20 'proche' d≈0,025 + 20 'realiste' d≈0,25-0,35) · builds=$BUILDS"
echo "vector $(q "select extversion from pg_extension where extname='vector';") · chemin d'index FORCÉ (set enable_seqscan=off) — la prod, elle, seq-scanne $SRC_TABLE aujourd'hui (déclaration 2026-08-23, §0)"
echo

echo "== vérité terrain : recherche EXACTE (aucun index) =="
qq "drop index if exists idx_corpus_embedding;"
qq "truncate truth;
    do \$\$
    declare r record;
    begin
      for r in select qid, v from probe order by qid loop
        insert into truth (qid, rank, id)
        select r.qid, t.rn, t.id
        from (select id, row_number() over (order by embedding <=> r.v) rn
              from corpus order by embedding <=> r.v limit 10) t;
      end loop;
    end \$\$;"
echo "   $(q "select count(*) from truth;") lignes de référence"
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
echo "== PIRE CAS PAR SONDE (la moyenne cache les sondes instables) =="
q "select p.grp||' · sonde '||a.qid||' : recouvrement min '||min(ov)||'/10'
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build
   join probe p on p.qid=a.qid,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by p.grp, a.qid having min(ov) < 10 order by p.grp, min(ov), a.qid;"
