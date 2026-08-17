# Images conteneur verrouillées

Ce runbook maintient les images distantes exécutées par le dépôt sous la forme
`tag@sha256:<digest>`. Le tag garde la version lisible; le digest empêche qu'un registre
remplace silencieusement le contenu exécuté.

## Menace couverte

Un tag de registre est mutable. Sans digest, deux exécutions du même commit peuvent tirer des
octets différents après une publication amont. Le catalogue
[`config/container-images.lock.yml`](../config/container-images.lock.yml) conserve les
références exactes et leur preuve de résolution. Le gate
[`scripts/check_container_image_pins.py`](../scripts/check_container_image_pins.py) découvre les
consommateurs opérationnels et exige une bijection avec ce catalogue.

Le verrouillage ne prouve ni l'innocuité de l'image, ni sa signature, ni sa disponibilité future.
Il rend toute rotation explicite, testable et réversible.

## Périmètre du gate

Le gate parcourt les fichiers opérationnels à la racine et sous `deploy/`, `ops/`, `scripts/`
et `services/`; il ignore `.claude/`, `.git/`, `bench/`, `docs/` et `tests/`. Il contrôle les
`Dockerfile*` et `Containerfile*`, les fichiers Compose/stack YAML, les scripts shell reconnus
par suffixe ou shebang, les images et commandes exécutables de GitLab CI, ainsi que les modules
Python et scripts à shebang Python de `scripts/` et `services/`.

Le contrat est fail-closed. Chaque image distante doit se résoudre statiquement vers une
référence exacte `tag@digest`. Chaque chemin qui participe à la provenance d'une image, à un
contexte de build ou à l'exécution d'un autre script doit être prouvé interne au dépôt. Les
commandes Compose en shell exigent des fichiers `-f` explicites; les contextes Compose et
Dockerfiles doivent être littéraux. Les dépendances shell exécutables passent par un
`SCRIPT_DIR` canonique; `/etc/os-release` reste la seule source de données externe explicitement
modélisée.

En shell et dans les commandes CI, le gate analyse aussi les interpréteurs et payloads inline,
les fonctions, alias et scripts transitifs. En Python, l'image de `containers.run` doit être
littérale; le gate suit ou rejette les appels Docker directs ou indirects via SDK, API de
processus, callbacks, `functools.partial`, wrappers, décorateurs, factories, fonctions d'ordre
supérieur, réflexion, imports locaux et mutations de payload.

Toute nouvelle syntaxe d'exécution doit recevoir un test adversarial et un modèle explicite
avant son adoption. Un rejet conservateur signale une forme non prise en charge; il ne doit pas
être contourné par une exception ad hoc.

## Inventaire opérationnel

Le fichier lock reste la source de vérité pour les digests, media types, plateformes et dates de
résolution. Cet inventaire nomme les neuf références suivies sans recopier ces métadonnées.

| Entrée du lock | Tag lisible | Consommateurs |
|---|---|---|
| `python-3-12-slim` | `python:3.12-slim` | CI, Dockerfile racine, build GGUF, shim et supervisor |
| `python-3-11-slim` | `python:3.11-slim` | service embedding legacy |
| `docker-27-cli` | `docker:27-cli` | job de build GitLab |
| `pgvector-pg16` | `pgvector/pgvector:pg16` | service CI et Compose principal |
| `llama-server-cuda` | `ghcr.io/ggml-org/llama.cpp:server-cuda` | Compose principal |
| `pytorch-2-7-1-cuda12-8-cudnn9-runtime` | `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` | service Qodo |
| `nvidia-cuda-12-4-1-base-ubuntu22-04` | `nvidia/cuda:12.4.1-base-ubuntu22.04` | probe dev-pc et supervisor |
| `llama-full` | `ghcr.io/ggml-org/llama.cpp:full` | build GGUF |
| `neo4j-5-26-21` | `neo4j:5.26.21` | Compose principal |

Deux images sont locales et ne possèdent donc aucun digest de registre :

| Image locale | Contexte de build |
|---|---|
| `brain-embedding-supervisor:local` | `services/embedding_supervisor` |
| `brain-embedding-qodo:local` | `services/embedding_qodo` |

Le fichier [`deploy/dev-pc/docker-compose.yml`](../deploy/dev-pc/docker-compose.yml) doit garder
pour chacune un bloc `build:` et `pull_policy: build`. Le gate refuse une exception locale sans
ces deux propriétés.

Le tag `brain-v42-ci-smoke:${CI_COMMIT_SHA}` n'est pas une troisième entrée locale du lock. Le job
`build:docker` le crée une seule fois depuis le contexte littéral `.`, puis l'exécute
immédiatement avec `docker run --pull=never`. Il ne doit être ni tiré, ni retagué, ni chargé,
ni poussé, ni sauvegardé.

## Cadence, responsabilité et sources

Le dépôt impose une revue des neuf références au moins tous les 30 jours. À chaque revue, la
personne qui la conduit ouvre ou met à jour une issue de maintenance, s'y nomme responsable et
indique `reviewed_at` ainsi qu'une `next_review_due` située au plus 30 jours plus tard. Cette
attribution dans l'issue constitue le contrat de propriété; elle ne suppose aucun rôle
organisationnel externe au dépôt.

Tout advisory pertinent déclenche immédiatement une revue, sans attendre `next_review_due`. La
personne qui le détecte ouvre l'issue, gèle la publication ou le déploiement de l'image concernée
et consigne la décision avant reprise.

Chaque revue relit le manifeste dans le registre canonique indiqué par le lock et consulte les
sources amont pertinentes :

| Entrées | Registre | Publications et advisories |
|---|---|---|
| Python 3.11/3.12 | Docker Hub | [Docker Official Images](https://github.com/docker-library/official-images), [Python Security](https://www.python.org/dev/security/) |
| Docker CLI | Docker Hub | [Docker security announcements](https://docs.docker.com/security/security-announcements/), [Docker Official Images](https://github.com/docker-library/official-images) |
| pgvector | Docker Hub | [releases pgvector](https://github.com/pgvector/pgvector/releases), [security pgvector](https://github.com/pgvector/pgvector/security) |
| llama.cpp | GHCR | [releases llama.cpp](https://github.com/ggml-org/llama.cpp/releases), [security llama.cpp](https://github.com/ggml-org/llama.cpp/security) |
| PyTorch | Docker Hub | [releases PyTorch](https://github.com/pytorch/pytorch/releases), [security PyTorch](https://github.com/pytorch/pytorch/security) |
| NVIDIA CUDA | Docker Hub | [NVIDIA Product Security](https://www.nvidia.com/en-us/security/) |
| Neo4j | Docker Hub | [Neo4j Security](https://neo4j.com/security/), [image officielle Neo4j](https://hub.docker.com/_/neo4j/) |

L'issue conserve une preuve assainie : responsable, dates, sources consultées, entrées
affectées, digests avant/après, plateforme, commandes et sorties utiles, résultat CI Lint,
tests et revue. Une revue sans rotation conserve la même preuve et planifie l'échéance suivante.
Le lock porte en plus la preuve de résolution de chaque digest modifié.

## Rotation manuelle

Déclencher une rotation après un advisory pertinent ou une revue planifiée. Une rotation traite
ensemble le catalogue et tous ses consommateurs.

1. Ouvrir une branche depuis un `main` propre. Ne tirer ni exécuter l'image pendant la phase de
   résolution.
2. Lire le manifeste du tag auprès du registre, puis relire la référence exacte candidate. Saisir
   les deux valeurs explicitement; ne pas les construire depuis un fichier d'environnement.

   ```bash
   TAG='python:3.12-slim'
   EXACT='python:3.12-slim@sha256:REPLACE_WITH_64_HEX_DIGEST'

   docker buildx imagetools inspect "$TAG"
   docker buildx imagetools inspect --raw "$TAG"
   docker buildx imagetools inspect "$EXACT"
   docker buildx imagetools inspect --raw "$EXACT"
   ```

   Pour un manifeste mono-architecture, vérifier aussi son descripteur :

   ```bash
   docker manifest inspect --verbose "$EXACT"
   ```

3. Confirmer que le tag et `tag@digest` résolvent le digest candidat et que leur index ou
   descripteur contient `linux/amd64`. Relever dans le lock le registre canonique, le media type,
   les plateformes observées, `resolved_at` et la commande de résolution. Un tag qui bouge entre
   ces lectures impose de recommencer la résolution.
4. Modifier dans le même commit l'entrée du lock et chaque consommateur listé. Conserver le tag
   lisible devant `@sha256:`. Ne laisser aucun ancien digest dans un consommateur.
5. Exécuter le gate hors ligne et les contrôles ciblés :

   ```bash
   uv sync --locked --no-dev --extra dev
   uv run python scripts/check_container_image_pins.py
   uv run pytest tests/unit/test_container_image_pins.py tests/unit/test_gitlab_ci.py \
     tests/unit/test_docker_compose.py \
     tests/unit/test_rotate_neo4j_credential.py \
     tests/unit/services/embedding_supervisor/test_state_machine.py \
     tests/dev_pc/test_validate_headless.py \
     tests/dev_pc/test_container_lifecycle_contracts.py -q
   bash -n deploy/dev-pc/setup-docker-ce.sh scripts/embedding_gguf_build.sh \
     scripts/rotate_neo4j_container.sh
   docker compose config --quiet --no-interpolate --no-env-resolution
   docker compose config --images
   docker compose -f deploy/dev-pc/docker-compose.yml config --images
   ```

6. Soumettre `.gitlab-ci.yml` au CI Lint officiel GitLab avec l'authentification `glab` stockée
   hors des arguments :

   ```bash
   glab auth status
   glab ci lint .gitlab-ci.yml
   ```

   Ne passer aucun token dans la ligne de commande et ne journaliser aucun en-tête
   d'authentification. Conserver le statut valide ainsi que les listes d'erreurs et de warnings.
7. Relire les neuf références exactes auprès des registres, puis exécuter les gates de branche :

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/ scripts/check_container_image_pins.py \
     scripts/rotate_neo4j_credential.py
   uv run pytest -q
   git diff --check
   git diff --check main...HEAD
   ```

8. Faire revoir le diff complet. Le reviewer doit confirmer l'inventaire, les consommateurs, la
   preuve `linux/amd64`, les frontières du lot et l'absence de constat P0 à P3.

## Retour arrière

Revertir le commit complet de rotation, jamais le lock ou un consommateur seul. Vérifier que les
anciennes références exactes restent disponibles, puis relancer le gate et les tests ciblés. Si
un ancien digest n'est plus servi, sélectionner un nouveau digest vérifié au lieu de revenir à un
tag flottant.

## Frontières

Ce contrat ne verrouille pas :

- les modèles GGUF ou Hugging Face, leurs révisions et leurs checksums;
- les dépendances Python propres aux services et les paquets système des images;
- le daemon Docker ou BuildKit du runner, ni son helper GitLab;
- les tags de publication produits par la CI;
- les caches `*-latest`, qui accélèrent le build sans devenir des images exécutées.

L'audit de dépendances, le SBOM et la SAST restent des sous-lots OPS1 distincts.
