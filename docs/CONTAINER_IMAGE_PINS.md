# Locked container images

This runbook maintains the remote images run by the repository in the form
`tag@sha256:<digest>`. The tag keeps the human-readable version; the digest prevents a
registry from silently replacing the executed content.

## Threat covered

A registry tag is mutable. Without a digest, two runs of the same commit can pull different
bytes after an upstream publish. The catalog
[`config/container-images.lock.yml`](../config/container-images.lock.yml) keeps the
exact references and their resolution proof. The gate
[`scripts/check_container_image_pins.py`](../scripts/check_container_image_pins.py) discovers the
operational consumers and requires a bijection with this catalog.

Locking proves neither the image's harmlessness, nor its signature, nor its future availability.
It makes any rotation explicit, testable and reversible.

## Gate scope

The gate scans the operational files at the root and under `deploy/`, `ops/`, `scripts/`
and `services/`; it ignores `.claude/`, `.git/`, `bench/`, `docs/` and `tests/`. It checks
`Dockerfile*` and `Containerfile*`, Compose/stack YAML files, shell scripts recognized
by suffix or shebang, GitLab CI executable images and commands, as well as Python
modules and Python-shebang scripts under `scripts/` and `services/`.

The contract is fail-closed. Every remote image must resolve statically to an exact
`tag@digest` reference. Every path that participates in an image's provenance, in a
build context or in the execution of another script must be proven internal to the repository. Shell
Compose commands require explicit `-f` files; Compose contexts and
Dockerfiles must be literal. Executable shell dependencies go through a
canonical `SCRIPT_DIR`; `/etc/os-release` remains the only external data source explicitly
modeled.

In shell and in CI commands, the gate also analyzes inline interpreters and payloads,
functions, aliases and transitive scripts. In Python, the image in `containers.run` must be
literal; the gate follows or rejects direct or indirect Docker calls via SDK, process
API, callbacks, `functools.partial`, wrappers, decorators, factories, higher-order
functions, reflection, local imports and payload mutations.

Any new execution syntax must receive an adversarial test and an explicit model
before adoption. A conservative rejection signals an unsupported form; it must not
be worked around with an ad hoc exception.

## Operational inventory

The lock file remains the source of truth for digests, media types, platforms and resolution
dates. This inventory names the eight tracked references without copying that metadata.
(`docker-27-cli` left the lock along with the GitLab rail, its only consumer.)

| Lock entry | Readable tag | Consumers |
|---|---|---|
| `python-3-12-slim` | `python:3.12-slim` | root Dockerfile, GGUF build, shim and supervisor |
| `python-3-11-slim` | `python:3.11-slim` | legacy embedding service |
| `pgvector-pg16` | `pgvector/pgvector:pg16` | CI service and main Compose |
| `llama-server-cuda` | `ghcr.io/ggml-org/llama.cpp:server-cuda` | main Compose |
| `pytorch-2-7-1-cuda12-8-cudnn9-runtime` | `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` | Qodo service |
| `nvidia-cuda-12-4-1-base-ubuntu22-04` | `nvidia/cuda:12.4.1-base-ubuntu22.04` | dev-pc probe and supervisor |
| `llama-full` | `ghcr.io/ggml-org/llama.cpp:full` | GGUF build |
| `neo4j-5-26-21` | `neo4j:5.26.21` | main Compose |

Two images are local and therefore carry no registry digest:

| Local image | Build context |
|---|---|
| `brain-embedding-supervisor:local` | `services/embedding_supervisor` |
| `brain-embedding-qodo:local` | `services/embedding_qodo` |

The file [`deploy/dev-pc/docker-compose.yml`](../deploy/dev-pc/docker-compose.yml) must keep
a `build:` block and `pull_policy: build` for each of them. The gate refuses a local exception without
these two properties.

The tag `brain-v42-ci-smoke:${CI_COMMIT_SHA}` is not a third local lock entry. The
`build:docker` job creates it once from the literal context `.`, then runs it
immediately with `docker run --pull=never`. It must be neither pulled, nor retagged, nor loaded,
nor pushed, nor saved.

## Cadence, ownership and sources

The repository requires a review of the nine references at least every 30 days. At each review, the
person conducting it opens or updates a maintenance issue, names themselves as owner in it and
records `reviewed_at` as well as a `next_review_due` set at most 30 days later. This
attribution in the issue constitutes the ownership contract; it does not assume any
organizational role external to the repository.

Any relevant advisory triggers an immediate review, without waiting for `next_review_due`. The
person who detects it opens the issue, freezes the publication or deployment of the affected image
and records the decision before resuming.

Each review rereads the manifest in the canonical registry indicated by the lock and consults the
relevant upstream sources:

| Entries | Registry | Publications and advisories |
|---|---|---|
| Python 3.11/3.12 | Docker Hub | [Docker Official Images](https://github.com/docker-library/official-images), [Python Security](https://www.python.org/dev/security/) |
| Docker CLI | Docker Hub | [Docker security announcements](https://docs.docker.com/security/security-announcements/), [Docker Official Images](https://github.com/docker-library/official-images) |
| pgvector | Docker Hub | [pgvector releases](https://github.com/pgvector/pgvector/releases), [pgvector security](https://github.com/pgvector/pgvector/security) |
| llama.cpp | GHCR | [llama.cpp releases](https://github.com/ggml-org/llama.cpp/releases), [llama.cpp security](https://github.com/ggml-org/llama.cpp/security) |
| PyTorch | Docker Hub | [PyTorch releases](https://github.com/pytorch/pytorch/releases), [PyTorch security](https://github.com/pytorch/pytorch/security) |
| NVIDIA CUDA | Docker Hub | [NVIDIA Product Security](https://www.nvidia.com/en-us/security/) |
| Neo4j | Docker Hub | [Neo4j Security](https://neo4j.com/security/), [official Neo4j image](https://hub.docker.com/_/neo4j/) |

The issue keeps sanitized proof: owner, dates, sources consulted, affected
entries, before/after digests, platform, useful commands and outputs, CI Lint result,
tests and review. A review without rotation keeps the same proof and schedules the next
deadline. The lock additionally carries the resolution proof for each changed digest.

## Manual rotation

Trigger a rotation after a relevant advisory or a scheduled review. A rotation treats
the catalog and all its consumers together.

1. Open a branch from a clean `main`. Do not pull or run the image during the
   resolution phase.
2. Read the tag's manifest from the registry, then reread the exact candidate reference. Enter
   both values explicitly; do not build them from an environment file.

   ```bash
   TAG='python:3.12-slim'
   EXACT='python:3.12-slim@sha256:REPLACE_WITH_64_HEX_DIGEST'

   docker buildx imagetools inspect "$TAG"
   docker buildx imagetools inspect --raw "$TAG"
   docker buildx imagetools inspect "$EXACT"
   docker buildx imagetools inspect --raw "$EXACT"
   ```

   For a single-architecture manifest, also check its descriptor:

   ```bash
   docker manifest inspect --verbose "$EXACT"
   ```

3. Confirm that the tag and `tag@digest` resolve to the candidate digest and that their index or
   descriptor contains `linux/amd64`. Record in the lock the canonical registry, the media type,
   the observed platforms, `resolved_at` and the resolution command. A tag that moves between
   these reads requires restarting the resolution.
4. Modify the lock entry and every listed consumer in the same commit. Keep the readable tag
   in front of `@sha256:`. Leave no old digest in a consumer.
5. Run the gate offline and the targeted checks:

   ```bash
   uv sync --locked --no-dev --extra dev
   uv run python scripts/check_container_image_pins.py
   uv run pytest tests/unit/test_container_image_pins.py tests/unit/test_github_workflows.py \
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

6. Reread the eight exact references from the registries, then run the branch gates:

   ```bash
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/ scripts/check_container_image_pins.py \
     scripts/rotate_neo4j_credential.py
   uv run pytest -q
   git diff --check
   git diff --check main...HEAD
   ```

7. Have the full diff reviewed. The reviewer must confirm the inventory, the consumers, the
   `linux/amd64` proof, the batch boundaries and the absence of a P0 to P3 finding.

## Rollback

Revert the entire rotation commit, never the lock or a single consumer alone. Verify that the
old exact references remain available, then rerun the gate and the targeted tests. If
an old digest is no longer served, select a new verified digest instead of reverting to a
floating tag.

## Boundaries

This contract does not lock:

- GGUF or Hugging Face models, their revisions and their checksums;
- Python dependencies specific to the services and the system packages of the images;
- the runner's Docker daemon or BuildKit, nor its GitLab helper;
- publication tags produced by CI;
- the `*-latest` caches, which speed up the build without becoming executed images.

Dependency auditing, the SBOM and SAST remain separate OPS1 sub-batches.
