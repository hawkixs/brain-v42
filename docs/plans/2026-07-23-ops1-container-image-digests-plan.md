# OPS1 — Images conteneur immuables

Date : 2026-07-23

Branche : `feat/ops1-container-image-digests`

Base : `main` à `8c271d1`

## Objectif

Fermer le sous-lot « images immuables » d'OPS1 : chaque image distante exécutée par la CI,
les Compose opérationnels, les Dockerfiles de services, le supervisor GPU ou les scripts Docker
hors benchmark doit conserver un tag lisible et être verrouillée sur un digest de manifeste
vérifié. Un gate statique doit maintenir une bijection entre un inventaire canonique et les
consommateurs découverts, afin qu'une nouvelle image ne puisse pas échapper au contrat.

## État initial observé

- `.gitlab-ci.yml` exécute `python:3.12-slim`, `pgvector/pgvector:pg16` et
  `docker:27-cli` sans digest.
- `Dockerfile` et quatre Dockerfiles de services opérationnels utilisent des bases sans
  digest.
- `docker-compose.yml` verrouille le digest Neo4j sans conserver son tag et ne verrouille ni
  PostgreSQL ni llama.cpp.
- `services/embedding_supervisor/main.py` et `deploy/dev-pc/setup-docker-ce.sh` exécutent la
  même image CUDA flottante.
- `scripts/embedding_gguf_build.sh` exécute Python et llama.cpp `full` sans digest.
- Seul le graphe Python de l'application racine et de la CI est déjà verrouillé par `uv.lock`.
  Les dépendances des services, les paquets système et les modèles ne sont pas rendus
  reproductibles par ce lot.

Les neuf digests ci-dessous ont été lus le 2026-07-23 directement dans les manifests des
registres, sans téléchargement d'image. Chaque référence exacte `tag@digest` a ensuite été
résolue et `linux/amd64` a été observé dans son index ou son descripteur de manifeste.

| Tag lisible | Digest de manifeste |
|---|---|
| `python:3.12-slim` | `sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de` |
| `python:3.11-slim` | `sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93` |
| `docker:27-cli` | `sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c` |
| `pgvector/pgvector:pg16` | `sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb` |
| `ghcr.io/ggml-org/llama.cpp:server-cuda` | `sha256:c1ddeb6d30932ddd9ddff962cb62dbc5450cd99d8e82c8c20de2fd1f99fde85b` |
| `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` | `sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2` |
| `nvidia/cuda:12.4.1-base-ubuntu22.04` | `sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95` |
| `ghcr.io/ggml-org/llama.cpp:full` | `sha256:0d70482d19f8a4a513e64c8cd839fa114070bfb0c29c8754d68f44691a8c5d22` |
| `neo4j:5.26.21` | `sha256:409728716bc239f9fa046368ac6ce6ef280f9e5f0bcb7cdd75031a4465cc192d` |

Le tag flottant `llama.cpp:full` a changé pendant la phase de critique : seul le digest relu
par le coordinateur ci-dessus est retenu. Cette dérive confirme que la preuve de registre doit
porter sur la référence exacte, et non sur une valeur rapportée par un reviewer.

## Critères d'acceptation

1. `config/container-images.lock.yml` expose un schéma strict et les neuf références
   canoniques sous la forme `<tag>@sha256:<64 hex>`. Pour chacune, il consigne le registre
   canonique, le tag, le digest, le media type, les plateformes observées, la date, la méthode
   de résolution et au moins un consommateur.
2. Le gate `scripts/check_container_image_pins.py` découvre structurellement les images dans
   la CI, le Compose principal, les Compose sous `deploy/`, tous les `services/**/Dockerfile`,
   le Dockerfile racine, les commandes `docker run`/`docker pull` des scripts shell hors
   `bench/` et le probe Docker Python du supervisor.
3. La relation est bijective : chaque image distante découverte appartient au catalogue,
   chaque entrée du catalogue est consommée et un même tag canonique ne peut pointer vers deux
   digests. Les deux tags locaux construits dans `deploy/dev-pc/docker-compose.yml` sont les
   seules exceptions nommées et doivent rester associés à `build:`.
4. Le gate rejette les digests malformés, les références sans tag, les variables d'image
   CI/Compose/Dockerfile, les constructions qu'il ne comprend pas et les références présentes
   seulement dans un commentaire. Il traite les images/services CI chaîne ou mapping, les
   ancres YAML résolues, `FROM --platform`, les stages internes et `scratch`. Il échoue fermé
   sur `include`, `extends`, `!reference` ou un `FROM $ARG` non résolu.
5. Les références CI sont littérales après résolution YAML et ne dépendent d'aucune variable
   surchargeable. Une ancre YAML peut réduire la répétition si sa valeur finale reste littérale
   et si le gate rejette tout `$` dans `image` ou `services.name`.
6. Les images PostgreSQL, llama.cpp et Neo4j du Compose principal conservent leur version/tag
   lisible et leur digest exact. Tous les Dockerfiles opérationnels et les consommateurs CUDA/
   GGUF utilisent également la référence canonique complète.
7. Les tests historiques PostgreSQL et Neo4j acceptent les nouvelles références immuables
   sans relâcher leurs contrôles de dépôt, tag ou version.
8. La preuve RED liste plusieurs tags flottants actuels même quand l'inventaire n'existe pas;
   elle ne se réduit pas à une erreur « fichier absent ». Des mutations adversariales couvrent
   digest 63/65/non-hex, tag absent, commentaire, digest divergent, image inconnue digestée,
   lock orphelin, doublon YAML, variable, formes CI, `FROM` et nouvelle Dockerfile de service.
9. Chaque référence exacte est revalidée auprès du registre et prouve `linux/amd64`. La preuve
   distingue index multi-architecture et manifeste PyTorch mono-architecture.
10. La roadmap marque uniquement « images immuables » comme livré. Elle laisse explicitement
    ouverts modèles, dépendances des services, paquets système, audit de dépendances, SBOM et
    SAST.
11. Les tests ciblés, la suite complète, Ruff, format, mypy, `bash -n`, Compose et
    `git diff --check` sont verts. Une revue indépendante du diff complet conclut `SHIP` sans
    constat P0–P3 non résolu.
12. Après fusion, les deux remotes `main` pointent sur le même SHA, la pipeline GitLab de ce
    SHA est verte et la feature Brain OPS1 reçoit un artifact de preuve avec commits, compteurs,
    limites et sous-lots restants.

## Non-objectifs et frontières

- Ne pas verrouiller dans ce lot les poids GGUF ou Hugging Face, leurs révisions ou checksums.
- Ne pas verrouiller ici les dépendances propres aux services ou les paquets système.
- Ne pas ajouter l'audit de dépendances, la génération de SBOM ou la SAST.
- Ne pas modifier les fichiers historiques de benchmark sous `bench/`.
- Ne pas tirer, reconstruire, publier ou déployer les images.
- Ne pas modifier les tags locaux produits par le dépôt ni les tags de publication de la CI.
- Le daemon/BuildKit et le helper GitLab appartiennent à l'infrastructure du runner; pinner
  `docker:27-cli` ne les rend pas reproductibles. Le cache `*-latest` reste une optimisation de
  build et non une entrée exécutée du catalogue.

## Découpage TDD

### Tâche 1 — Gate structurel et inventaire canonique

Fichiers : créer `scripts/check_container_image_pins.py`,
`tests/unit/test_container_image_pins.py` et `config/container-images.lock.yml`.

RED : écrire d'abord les tests de découverte et les mutations adversariales. Exécuter le gate
sans inventaire ou contre une fixture minimale afin que l'échec initial énumère les références
flottantes, puis consigner ce résultat. GREEN : implémenter le parseur fail-closed, le schéma
strict, la comparaison bijective et la CLI hors-ligne.

### Tâche 2 — Verrouillage des consommateurs opérationnels

Fichiers : modifier `.gitlab-ci.yml`, `Dockerfile`, `docker-compose.yml`, les quatre
Dockerfiles sous `services/embedding*`, `services/embedding_supervisor/main.py`,
`deploy/dev-pc/setup-docker-ce.sh`, `scripts/embedding_gguf_build.sh`,
`tests/unit/test_docker_compose.py` et les tests existants du supervisor/provisioning affectés.

Avant l'édition de tout symbole existant, exécuter `gitnexus_impact` et annoncer le rayon
d'impact; arrêter et avertir sur un risque HIGH/CRITICAL. Remplacer chaque tag distant par sa
référence canonique. Garder les ancres CI non surchargeables et les deux images locales
explicitement allowlistées. GREEN : gate, tests historiques, `bash -n`, YAML et
`docker compose config --images`.

### Tâche 3 — Maintenance et preuve OPS1

Fichiers : créer `docs/CONTAINER_IMAGE_PINS.md`; modifier
`deploy/dev-pc/README.md`, `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md` et compléter
la section de preuve du présent plan.

Documenter la procédure manuelle de mise à jour : déclencheur périodique ou advisory, lecture
du digest du tag auprès du registre, contrôle `linux/amd64`, mise à jour atomique inventaire +
consommateurs, gate hors-ligne, validation registre, suite complète et revue. Mettre à jour la
roadmap seulement après les gates de la tâche 2.

## Vérification

Tests ciblés :

```text
uv run pytest tests/unit/test_container_image_pins.py tests/unit/test_gitlab_ci.py \
  tests/unit/test_docker_compose.py \
  tests/unit/test_rotate_neo4j_credential.py \
  tests/unit/services/embedding_supervisor/test_state_machine.py \
  tests/dev_pc/test_validate_headless.py \
  tests/dev_pc/test_container_lifecycle_contracts.py -q
uv run python scripts/check_container_image_pins.py
bash -n deploy/dev-pc/setup-docker-ce.sh scripts/embedding_gguf_build.sh \
  scripts/rotate_neo4j_container.sh
docker compose config --quiet --no-interpolate --no-env-resolution
docker compose config --images
docker compose -f deploy/dev-pc/docker-compose.yml config --images
```

Preuve registre, pour chaque référence exacte :

```text
docker buildx imagetools inspect <tag@digest>
docker buildx imagetools inspect --raw <tag@digest>
docker manifest inspect --verbose <tag@digest>  # manifeste mono-architecture
```

Gates de branche :

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ scripts/check_container_image_pins.py \
  scripts/rotate_neo4j_credential.py
uv run pytest -q
git diff --check
git diff --check main...HEAD
```

Exécuter `gitnexus_detect_changes` avant chaque commit. Après fusion : répéter les gates
proportionnés, pousser sans force sur les deux remotes `main`, vérifier l'identité des refs,
attendre la pipeline GitLab verte et mettre à jour la feature Brain OPS1.

## Retour arrière

Un revert du ou des commits restaure les tags précédents. Aucun état externe, secret, volume,
image de registre ou déploiement n'est modifié par ce lot.

## Preuves de livraison

### Tâche 1 — gate et inventaire

- RED initial : `22 collected`, `22 failed`, `0 error`. Les erreurs énuméraient huit tags
  flottants et la référence Neo4j séparée de son tag; l'échec ne se limitait pas à l'absence du
  lock.
- GREEN initial : `25 passed`. Trois boucles adversariales ont ensuite porté la matrice finale à
  `56 passed`.
- Commits : `48dec79` (inventaire et gate), `d0791e5` et `4b346cd` (fermeture des bypasses),
  fusion dans la feature à `bdbf7be`.
- Vérifications indépendantes : tester `PASS`; reviewer `SHIP`, sans constat P0 à P3.

Le durcissement final du gate a ensuite fermé les provenances Python indirectes, les portées de
compréhension, les callbacks, factories, mappings dynamiques et chemins d'exécution shell/CI. Le
snapshot final du checker (`b2420ee…`) et de ses tests (`37d943c…`) passe **1 390/1 390**. Trois
revues indépendantes du même snapshot concluent `SHIP`; les quatre C901 restantes sont les dettes
historiques déjà identifiées, sans nouvelle complexité ajoutée par la dernière boucle.

### Tâche 2 — consommateurs opérationnels

- RED réel : le gate a relevé 27 violations avant le verrouillage des consommateurs.
- GREEN : `container image pins: OK (24 consumers)`. Les passes implementer/tester ont validé
  `148 passed`; le reviewer a validé `171 passed` avec un warning de dépendance préexistant.
- Le CI Lint officiel GitLab a été reproduit par un connecteur GitLab authentifié : `POST`
  `projects/hawkixs_project%2Fbrain_v42/ci/lint`, contenu de `.gitlab-ci.yml` et
  `include_merged_yaml: true`. La réponse porte `valid: true`, `errors: []` et `warnings: []`.
  Aucun token n'a été fourni manuellement ni inscrit dans les arguments ou les logs. Le runbook
  documente aussi la reproduction locale avec `glab ci lint .gitlab-ci.yml`, qui réutilise
  l'authentification `glab` stockée hors des arguments.
- Commit : `921b0e1`, fusion dans la feature à `c1a7eb2`. Le reviewer a conclu `SHIP` sans
  constat P0 à P3.

### Correctif de stabilité du cycle de vie local

- `pull_policy: build` a rendu explicite un risque préexistant : l'upgrade et le rollback
  pouvaient reconstruire une image locale au lieu d'utiliser l'image produite ou restaurée.
- Le commit `c41b2b3` impose `--no-build` sur les chemins de rollback et recrée Qodo à l'arrêt
  avant le supervisor pendant un upgrade. Il ajoute les contrats statiques du cycle de vie.
- Les audits ciblés passent 99/99 puis 42/42. Le commit est fusionné dans la feature à
  `065ffdb`; les revues concluent `SHIP` sans constat P0 à P3.

### Registres et plateformes

Les neuf références exactes `tag@digest` ont été résolues auprès de leur registre. Toutes
exposent `linux/amd64`. Huit sont des index ou listes multi-architecture; PyTorch est un manifeste
mono-architecture `application/vnd.docker.distribution.manifest.v2+json` dont le descripteur
porte `linux/amd64`. Le lock conserve les digests, media types et plateformes exacts.

Le tag `llama.cpp:full` a bougé pendant la critique; le coordinateur a relu et retenu le digest
`0d70482d…`. Le tag `server-cuda` a avancé après son verrouillage, tandis que la référence exacte
`c1ddeb6d…` est restée disponible pour `linux/amd64` et `linux/arm64`. Ces deux dérives illustrent
la menace couverte par le lot.

### Preuves locales finales

- commit technique de fermeture : `833be90`;
- gate réel : `container image pins: OK (24 consumers)`;
- checker : **1 390 tests passés** en 24,11 s sur le snapshot final;
- matrice OPS1 (CI, Compose, rotation Neo4j, supervisor) : **140 tests passés**;
- contrats headless et cycle de vie local : **9 tests passés**;
- stabilité unitaire, sans processus concurrent et sans changement de snapshot :
  **5 994 passés, 48 ignorés** trois fois de suite en 78,97 s, 78,10 s et 78,50 s;
- Ruff dépôt complet, format des 597 fichiers, mypy sur `src/` et les scripts gate/rotation,
  `bash -n`, Compose racine/dev-pc et `git diff --check` : verts;
- trois revues indépendantes du diff final : `SHIP`, aucun constat reproductible restant.

### Preuves d'intégration

- feature fusionnée par fast-forward; commit de livraison `90d50f4` poussé à l'identique sur
  GitHub et GitLab;
- validation post-fusion sous Python 3.12 : **6 003 passés, 39 ignorés** sur 6 042 collectés
  en 81,30 s; matrice ciblée **1 598/1 598**;
- pipeline GitLab `4248` verte sur `90d50f4` : 6/6 jobs, couverture 90 % et build Docker;
- ticket Brain `56245929…` confirmé clos avec les preuves; focus roadmap maintenu en `building`
  et prochain lot positionné sur le webhook `621fcc37…`.

Les modèles, les dépendances des services, les paquets système, l'audit de dépendances, le SBOM
et la SAST restent hors de ce sous-lot. OPS1 reste ouvert.
