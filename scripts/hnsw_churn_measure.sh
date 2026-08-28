#!/usr/bin/env bash
# Mesure le CHURN de reconstruction HNSW sur les embeddings RÉELS du corpus.
#
# POURQUOI CE SCRIPT EXISTE
# Le ticket cfd26e9d a établi deux faits : `pg_dump` ne transporte aucun graphe
# HNSW (9 `CREATE INDEX ... USING hnsw`, zéro octet d'index), et une
# reconstruction HNSW n'est pas déterministe — 8,60/10 de recouvrement top-10.
# MAIS cette mesure portait sur des vecteurs SYNTHÉTIQUES ET UNIFORMES. Le
# ticket le dit lui-même : « 8,60/10 est une borne HAUTE du churn », et « le
# mesurer sur les embeddings RÉELS du corpus est le seul geste qui donnerait le
# chiffre à écrire dans le runbook ». C'est ce geste.
#
# NE TOUCHE JAMAIS LA PRODUCTION : les vecteurs sont lus par un
# `COPY ... TO STDOUT` (lecture seule) et tout le reste vit dans un conteneur
# éphémère sur tmpfs.
#
# DEUX PIÈGES, tous deux rencontrés en écrivant ce script :
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
#    unité par sonde. C'est une preuve d'exécution, pas d'intention.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$SCRIPT_DIR/../tests/support/hnsw-churn-compose.yml"

C=${CHURN_CONTAINER:-brain_v42_hnsw_churn}
BUILDS=${BUILDS:-3}
SRC_TABLE=${SRC_TABLE:-learnings}   # la plus grosse des 9 tables à index HNSW
PROD=${PROD_CONTAINER:-brain_v42_postgres}
q() { docker exec "$C" psql -U churn -d churn -Atc "$1"; }
qq() { docker exec "$C" psql -U churn -d churn -q -c "$1"; }

# --- provisionnement, si le banc n'existe pas déjà -------------------------
# Le banc est décrit par `tests/support/hnsw-churn-compose.yml`, PAS par un
# `docker run` : il hérite ainsi du digest épinglé, donc d'une mesure
# reproductible. Le gate `scripts/check_container_image_pins.py` refuse de
# toute façon un `docker run` non épinglé, et il a raison ici.
#
# Le banc reproduit la CIBLE VERSIONNÉE, pas le runtime de production : les
# deux ont divergé (voir l'en-tête du fichier compose). La mesure ci-dessous
# porte donc sur `vector 0.8.5`, quand la production a `0.8.2` installé.
#
# Le conteneur porte une COPIE des embeddings de production. Il vit sur tmpfs
# et doit être détruit après usage :  docker rm -f "$CHURN_CONTAINER"
if ! docker exec "$C" pg_isready -U churn -d churn >/dev/null 2>&1; then
  echo "== provisionnement du banc isolé =="
  docker rm -f "$C" >/dev/null 2>&1 || true
  CHURN_CONTAINER="$C" \
    docker compose -f "$SCRIPT_DIR/../tests/support/hnsw-churn-compose.yml" \
    up --detach --no-build >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$C" pg_isready -U churn -d churn >/dev/null 2>&1 && break
    sleep 1
  done
  qq "create extension vector;
      create table corpus (id uuid primary key, embedding vector(1536));
      create table probe  (qid int primary key, v vector(1536));
      create table res    (build int, qid int, rank int, id uuid);
      create table truth  (qid int, rank int, id uuid);"
  # LECTURE SEULE sur la production.
  docker exec "$PROD" psql -U brain -d brain -Atc \
    "copy (select id, embedding from $SRC_TABLE where embedding is not null) to stdout" \
  | docker exec -i "$C" psql -U churn -d churn -q -c "copy corpus (id, embedding) from stdin"
  # Sondes NON-MEMBRES du corpus : un vrai vecteur bruité. Une sonde qui EST
  # dans le corpus a son top-1 à distance nulle, ce qui biaise la mesure vers
  # la stabilité — piège rencontré au premier jet.
  qq "insert into probe (qid, v)
      select qid, (select array_agg(e + (random()-0.5)*0.02)::vector
                   from unnest(base::real[]) e)
      from (select row_number() over (order by md5(id::text)) qid,
                   embedding::real[]::text::real[] base
            from corpus order by md5(id::text) limit 20) s;"
  echo "   corpus=$(q "select count(*) from corpus;") depuis $SRC_TABLE · sondes=$(q "select count(*) from probe;")"
  echo
fi

NPROBE=$(q "select count(*) from probe;")
echo "corpus=$(q "select count(*) from corpus;") vecteurs réels · sondes=$NPROBE · builds=$BUILDS"
echo "vector $(q "select extversion from pg_extension where extname='vector';")"
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
        from (select id, row_number() over () rn
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
          from (select id, row_number() over () rn
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
echo "== CHURN : recouvrement entre reconstructions =="
q "select 'builds '||a.build||'×'||b.build
      ||' : recouvrement '||round(avg(ov)::numeric,2)||'/10'
      ||' · top-10 identiques (ensemble) '||count(*) filter (where ov=10)||'/'||count(*)
      ||' · identiques (ordre strict) '||count(*) filter (where a.ids=b.ids)||'/'||count(*)
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by a.build, b.build order by a.build, b.build;"

echo
echo "== RAPPEL vs recherche exacte (ce qu'un opérateur perd réellement) =="
q "select 'build '||r.build||' : rappel '||round(avg(ov)::numeric,2)||'/10'
      ||' · top-10 exacts '||count(*) filter (where ov=10)||'/'||count(*)
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) r
   join (select qid, array_agg(id order by rank) ids from truth group by qid) t on t.qid=r.qid,
   lateral (select count(*) ov from unnest(r.ids) x where x = any(t.ids)) o
   group by r.build order by r.build;"

echo
echo "== PIRE CAS PAR SONDE (la moyenne cache les sondes instables) =="
q "select 'sonde '||a.qid||' : recouvrement min '||min(ov)||'/10'
   from (select build, qid, array_agg(id order by rank) ids from res group by build, qid) a
   join (select build, qid, array_agg(id order by rank) ids from res group by build, qid) b
     on a.qid=b.qid and a.build<b.build,
   lateral (select count(*) ov from unnest(a.ids) x where x = any(b.ids)) o
   group by a.qid having min(ov) < 10 order by min(ov), a.qid;"
