# OPS1 — Immutable container images

Date: 2026-07-23

Branch: `feat/ops1-container-image-digests`

Base: `main` at `8c271d1`

## Objective

Close the "immutable images" sub-lot of OPS1: every remote image run by CI,
the operational Compose files, the service Dockerfiles, the GPU supervisor or the Docker
scripts outside benchmark must keep a readable tag and be locked to a verified manifest
digest. A static gate must maintain a bijection between a canonical inventory and the
discovered consumers, so a new image cannot escape the contract.

## Initial state observed

- `.gitlab-ci.yml` runs `python:3.12-slim`, `pgvector/pgvector:pg16` and
  `docker:27-cli` without a digest.
- `Dockerfile` and four operational service Dockerfiles use bases without a
  digest.
- `docker-compose.yml` locks the Neo4j digest without keeping its tag and locks neither
  PostgreSQL nor llama.cpp.
- `services/embedding_supervisor/main.py` and `deploy/dev-pc/setup-docker-ce.sh` run the
  same floating CUDA image.
- `scripts/embedding_gguf_build.sh` runs Python and llama.cpp `full` without a digest.
- Only the Python graph of the root application and of CI is already locked by `uv.lock`.
  The service dependencies, the system packages and the models are not made
  reproducible by this lot.

The nine digests below were read on 2026-07-23 directly from the registry manifests,
without downloading an image. Each exact `tag@digest` reference was then
resolved and `linux/amd64` was observed in its index or manifest descriptor.

| Readable tag | Manifest digest |
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

The floating `llama.cpp:full` tag changed during the critique phase: only the digest reread
by the coordinator above is kept. This drift confirms that registry proof must
bear on the exact reference, not on a value reported by a reviewer.

## Acceptance criteria

1. `config/container-images.lock.yml` exposes a strict schema and the nine canonical
   references in the form `<tag>@sha256:<64 hex>`. For each, it records the canonical
   registry, the tag, the digest, the media type, the observed platforms, the date, the
   resolution method and at least one consumer.
2. The `scripts/check_container_image_pins.py` gate structurally discovers the images in
   CI, the main Compose file, the Compose files under `deploy/`, all `services/**/Dockerfile`,
   the root Dockerfile, the `docker run`/`docker pull` commands of shell scripts outside
   `bench/` and the supervisor's Docker Python probe.
3. The relationship is bijective: every discovered remote image belongs to the catalog,
   every catalog entry is consumed and the same canonical tag cannot point to two
   digests. The two local tags built in `deploy/dev-pc/docker-compose.yml` are the
   only named exceptions and must stay associated with `build:`.
4. The gate rejects malformed digests, references without a tag, image variables in
   CI/Compose/Dockerfile, constructions it does not understand and references present
   only in a comment. It handles CI images/services as string or mapping, resolved
   YAML anchors, `FROM --platform`, internal stages and `scratch`. It fails closed
   on `include`, `extends`, `!reference` or an unresolved `FROM $ARG`.
5. CI references are literal after YAML resolution and depend on no overridable variable.
   A YAML anchor may reduce repetition if its final value stays literal and if the gate
   rejects any `$` in `image` or `services.name`.
6. The PostgreSQL, llama.cpp and Neo4j images in the main Compose file keep their readable
   version/tag and their exact digest. All operational Dockerfiles and the CUDA/GGUF
   consumers also use the full canonical reference.
7. The existing PostgreSQL and Neo4j tests accept the new immutable references
   without relaxing their repository, tag or version checks.
8. The RED proof lists several current floating tags even when the inventory does not exist;
   it is not reduced to a "file not found" error. Adversarial mutations cover
   digest 63/65/non-hex, missing tag, comment, divergent digest, unknown digested image,
   orphan lock entry, YAML duplicate, variable, CI forms, `FROM` and a new service Dockerfile.
9. Every exact reference is revalidated against the registry and proves `linux/amd64`. The
   proof distinguishes multi-architecture indexes from the single-architecture PyTorch manifest.
10. The roadmap marks only "immutable images" as delivered. It explicitly leaves
    models, service dependencies, system packages, dependency audit, SBOM and
    SAST open.
11. The targeted tests, the full suite, Ruff, format, mypy, `bash -n`, Compose and
    `git diff --check` are green. An independent review of the full diff concludes `SHIP` with
    no unresolved P0–P3 finding.
12. After merge, both `main` remotes point to the same SHA, that SHA's GitLab pipeline
    is green and the Brain OPS1 feature receives a proof artifact with commits, counters,
    limits and remaining sub-lots.

## Non-goals and boundaries

- Do not lock GGUF or Hugging Face weights, their revisions or checksums in this lot.
- Do not lock service-specific dependencies or system packages here.
- Do not add dependency audit, SBOM generation or SAST.
- Do not modify the historical benchmark files under `bench/`.
- Do not pull, rebuild, publish or deploy the images.
- Do not modify the local tags produced by the repository or the CI's publication tags.
- The daemon/BuildKit and the GitLab helper belong to the runner infrastructure; pinning
  `docker:27-cli` does not make them reproducible. The `*-latest` cache remains a build
  optimization, not an executed catalog entry.

## TDD breakdown

### Task 1 — Structural gate and canonical inventory

Files: create `scripts/check_container_image_pins.py`,
`tests/unit/test_container_image_pins.py` and `config/container-images.lock.yml`.

RED: first write the discovery tests and the adversarial mutations. Run the gate
without an inventory or against a minimal fixture so the initial failure enumerates the
floating references, then record that result. GREEN: implement the fail-closed parser, the
strict schema, the bijective comparison and the offline CLI.

### Task 2 — Locking the operational consumers

Files: modify `.gitlab-ci.yml`, `Dockerfile`, `docker-compose.yml`, the four
Dockerfiles under `services/embedding*`, `services/embedding_supervisor/main.py`,
`deploy/dev-pc/setup-docker-ce.sh`, `scripts/embedding_gguf_build.sh`,
`tests/unit/test_docker_compose.py` and the existing affected supervisor/provisioning tests.

Before editing any existing symbol, run `gitnexus_impact` and announce the blast
radius; stop and warn on a HIGH/CRITICAL risk. Replace every remote tag with its
canonical reference. Keep the non-overridable CI anchors and the two explicitly
allowlisted local images. GREEN: gate, historical tests, `bash -n`, YAML and
`docker compose config --images`.

### Task 3 — Maintenance and OPS1 proof

Files: create `docs/CONTAINER_IMAGE_PINS.md`; modify
`deploy/dev-pc/README.md`, `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md` and complete
the proof section of this plan.

Document the manual update procedure: periodic or advisory trigger, reading the
tag's digest from the registry, `linux/amd64` check, atomic inventory + consumer update,
offline gate, registry validation, full suite and review. Update the
roadmap only after the task 2 gates.

## Verification

Targeted tests:

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

Registry proof, for each exact reference:

```text
docker buildx imagetools inspect <tag@digest>
docker buildx imagetools inspect --raw <tag@digest>
docker manifest inspect --verbose <tag@digest>  # single-architecture manifest
```

Branch gates:

```text
uv run ruff check .
uv run ruff format --check .
uv run mypy src/ scripts/check_container_image_pins.py \
  scripts/rotate_neo4j_credential.py
uv run pytest -q
git diff --check
git diff --check main...HEAD
```

Run `gitnexus_detect_changes` before every commit. After merge: repeat the proportioned
gates, push without force to both `main` remotes, check ref identity,
wait for the green GitLab pipeline and update the Brain OPS1 feature.

## Rollback

A revert of the commit(s) restores the previous tags. No external state, secret, volume,
registry image or deployment is modified by this lot.

## Delivery evidence

### Task 1 — gate and inventory

- Initial RED: `22 collected`, `22 failed`, `0 error`. The errors enumerated eight
  floating tags and the Neo4j reference separated from its tag; the failure was not limited to
  the missing lock.
- Initial GREEN: `25 passed`. Three adversarial loops then brought the final matrix to
  `56 passed`.
- Commits: `48dec79` (inventory and gate), `d0791e5` and `4b346cd` (closing the bypasses),
  merged into the feature at `bdbf7be`.
- Independent checks: tester `PASS`; reviewer `SHIP`, with no P0 to P3 finding.

The gate's final hardening then closed the indirect Python provenances, the comprehension
scopes, the callbacks, factories, dynamic mappings and shell/CI execution paths. The
final snapshot of the checker (`b2420ee…`) and its tests (`37d943c…`) passes **1,390/1,390**.
Three independent reviews of the same snapshot conclude `SHIP`; the four remaining C901s are
the already-identified historical debt, with no new complexity added by the last loop.

### Task 2 — operational consumers

- Real RED: the gate flagged 27 violations before the consumers were locked.
- GREEN: `container image pins: OK (24 consumers)`. The implementer/tester passes validated
  `148 passed`; the reviewer validated `171 passed` with a pre-existing dependency warning.
- The official GitLab CI Lint was reproduced by an authenticated GitLab connector: `POST`
  `projects/hawkixs_project%2Fbrain_v42/ci/lint`, `.gitlab-ci.yml` content and
  `include_merged_yaml: true`. The response carries `valid: true`, `errors: []` and `warnings: []`.
  No token was supplied manually or written into the arguments or the logs. The runbook
  also documents local reproduction with `glab ci lint .gitlab-ci.yml`, which reuses
  the `glab` authentication stored outside the arguments.
- Commit: `921b0e1`, merged into the feature at `c1a7eb2`. The reviewer concluded `SHIP` with
  no P0 to P3 finding.

### Local lifecycle stability fix

- `pull_policy: build` made an existing risk explicit: upgrade and rollback
  could rebuild a local image instead of using the produced or restored image.
- Commit `c41b2b3` enforces `--no-build` on the rollback paths and recreates Qodo at stop
  before the supervisor during an upgrade. It adds the static lifecycle contracts.
- The targeted audits pass 99/99 then 42/42. The commit is merged into the feature at
  `065ffdb`; the reviews conclude `SHIP` with no P0 to P3 finding.

### Registries and platforms

The nine exact `tag@digest` references were resolved against their registry. All
expose `linux/amd64`. Eight are multi-architecture indexes or lists; PyTorch is a
single-architecture `application/vnd.docker.distribution.manifest.v2+json` manifest whose
descriptor carries `linux/amd64`. The lock keeps the exact digests, media types and platforms.

The `llama.cpp:full` tag moved during the critique; the coordinator reread and kept the digest
`0d70482d…`. The `server-cuda` tag advanced after it was locked, while the exact reference
`c1ddeb6d…` remained available for `linux/amd64` and `linux/arm64`. These two drifts illustrate
the threat covered by the lot.

### Final local evidence

- technical closing commit: `833be90`;
- real gate: `container image pins: OK (24 consumers)`;
- checker: **1,390 tests passed** in 24,11 s on the final snapshot;
- OPS1 matrix (CI, Compose, Neo4j rotation, supervisor): **140 tests passed**;
- headless and local lifecycle contracts: **9 tests passed**;
- unit stability, without a concurrent process and without a snapshot change:
  **5,994 passed, 48 skipped** three times in a row in 78,97 s, 78,10 s and 78,50 s;
- full repo Ruff, format of 597 files, mypy on `src/` and the gate/rotation scripts,
  `bash -n`, root/dev-pc Compose and `git diff --check`: green;
- three independent reviews of the final diff: `SHIP`, no reproducible finding remaining.

### Integration evidence

- feature merged by fast-forward; delivery commit `90d50f4` pushed identically to
  GitHub and GitLab;
- post-merge validation under Python 3.12: **6,003 passed, 39 skipped** out of 6,042 collected
  in 81,30 s; targeted matrix **1,598/1,598**;
- GitLab pipeline `4248` green on `90d50f4`: 6/6 jobs, 90 % coverage and Docker build;
- Brain ticket `56245929…` confirmed closed with the proofs; roadmap focus kept at `building`
  and next lot positioned on webhook `621fcc37…`.

The models, service dependencies, system packages, dependency audit, SBOM
and SAST stay outside this sub-lot. OPS1 stays open.
