"""Contract tests for the offline container-image pin gate."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_container_image_pins.py"
LOCK_PATH = REPO_ROOT / "config" / "container-images.lock.yml"

DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
TAG = "python:3.12-slim"
REFERENCE = f"{TAG}@{DIGEST}"
CI_SMOKE_IMAGE = "brain-v42-ci-smoke:${CI_COMMIT_SHA}"
CI_SMOKE_RUN = f"docker run --pull=never --rm {CI_SMOKE_IMAGE}"
SCRIPT_DIR_ASSIGNMENT = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'

BASELINE_FLOATING_TAGS = (
    "python:3.12-slim",
    "python:3.11-slim",
    "docker:27-cli",
    "pgvector/pgvector:pg16",
    "ghcr.io/ggml-org/llama.cpp:server-cuda",
    "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime",
    "nvidia/cuda:12.4.1-base-ubuntu22.04",
    "ghcr.io/ggml-org/llama.cpp:full",
)

EXPECTED_LOCKED_REFERENCES = {
    "python:3.12-slim": "sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de",
    "python:3.11-slim": "sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93",
    "pgvector/pgvector:pg16": "sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb",
    "ghcr.io/ggml-org/llama.cpp:server-cuda": "sha256:c1ddeb6d30932ddd9ddff962cb62dbc5450cd99d8e82c8c20de2fd1f99fde85b",
    "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime": "sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2",
    "nvidia/cuda:12.4.1-base-ubuntu22.04": "sha256:0f6bfcbf267e65123bcc2287e2153dedfc0f24772fb5ce84afe16ac4b2fada95",
    "ghcr.io/ggml-org/llama.cpp:full": "sha256:0d70482d19f8a4a513e64c8cd839fa114070bfb0c29c8754d68f44691a8c5d22",
    "neo4j:5.26.21": "sha256:409728716bc239f9fa046368ac6ce6ef280f9e5f0bcb7cdd75031a4465cc192d",
    "zricethezav/gitleaks:v8.30.1": (
        "sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f"
    ),
}


class _MissingChecker:
    def __getattr__(self, name: str) -> object:
        def missing(*args: object, **kwargs: object) -> None:
            del args, kwargs
            pytest.fail(
                f"container image pin gate has no {name}; floating baseline includes: "
                + ", ".join(BASELINE_FLOATING_TAGS)
            )

        return missing


def _load_checker() -> ModuleType | _MissingChecker:
    if not CHECKER_PATH.exists():
        return _MissingChecker()
    spec = importlib.util.spec_from_file_location("check_container_image_pins", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker() -> ModuleType | _MissingChecker:
    return _load_checker()


def _catalog(consumers: list[str], *, reference: str = REFERENCE) -> dict[str, object]:
    tag, digest = reference.rsplit("@", 1)
    return {
        "schema_version": 1,
        "images": {
            "python-3-12-slim": {
                "reference": reference,
                "registry": "registry-1.docker.io",
                "tag": tag,
                "digest": digest,
                "media_type": "application/vnd.oci.image.index.v1+json",
                "platforms": ["linux/amd64", "linux/arm64"],
                "resolved_at": "2026-07-23",
                "resolution": "docker buildx imagetools inspect",
                "consumers": consumers,
            }
        },
        "local_images": {
            "brain-embedding-supervisor:local": {
                "compose": "deploy/dev-pc/docker-compose.yml",
                "service": "embedding-supervisor",
                "context": "services/embedding_supervisor",
            },
            "brain-embedding-qodo:local": {
                "compose": "deploy/dev-pc/docker-compose.yml",
                "service": "embedding-qodo",
                "context": "services/embedding_qodo",
            },
        },
    }


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def _write_valid_repo(root: Path) -> list[str]:
    sources = [
        ".gitlab-ci.yml",
        "Dockerfile",
        "docker-compose.yml",
        "services/embedding/Dockerfile",
        "services/new-worker/Dockerfile",
        "scripts/build-image.sh",
        "deploy/dev-pc/setup.sh",
        "services/embedding_supervisor/main.py",
    ]

    (root / "services/embedding").mkdir(parents=True)
    (root / "services/new-worker").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "services/embedding_supervisor").mkdir(parents=True)
    (root / "services/embedding_qodo").mkdir(parents=True)
    (root / "deploy/dev-pc").mkdir(parents=True)
    (root / ".gitlab-ci.yml").write_text(
        f".python-image: &python-image {REFERENCE}\n"
        "job:\n"
        "  image: *python-image\n"
        "  services:\n"
        f"    - name: {REFERENCE}\n"
    )
    (root / "Dockerfile").write_text(
        f"FROM --platform=linux/amd64 {REFERENCE} AS base\nFROM base AS runtime\nFROM scratch\n"
    )
    _write_yaml(
        root / "docker-compose.yml",
        {"services": {"runtime": {"image": REFERENCE}}},
    )
    (root / "services/embedding/Dockerfile").write_text(f"FROM {REFERENCE}\n")
    (root / "services/new-worker/Dockerfile").write_text(f"FROM {REFERENCE}\n")
    (root / "services/embedding_supervisor/Dockerfile").write_text("FROM scratch\n")
    (root / "services/embedding_qodo/Dockerfile").write_text("FROM scratch\n")
    (root / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\ndocker run --rm -v /tmp:/work {REFERENCE} true\n"
    )
    (root / "deploy/dev-pc/setup.sh").write_text(
        f'#!/usr/bin/env bash\nexport PROBE_IMAGE="{REFERENCE}"\ndocker pull "$PROBE_IMAGE"\n'
    )
    _write_yaml(
        root / "deploy/dev-pc/docker-compose.yml",
        {
            "services": {
                "embedding-supervisor": {
                    "build": {"context": "../../services/embedding_supervisor"},
                    "image": "brain-embedding-supervisor:local",
                    "pull_policy": "build",
                },
                "embedding-qodo": {
                    "build": {"context": "../../services/embedding_qodo"},
                    "image": "brain-embedding-qodo:local",
                    "pull_policy": "build",
                },
            }
        },
    )
    (root / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        "    def run(self):\n"
        f"        self._client.containers.run('{REFERENCE}', command=['true'])\n"
    )
    _write_yaml(root / "config/container-images.lock.yml", _catalog(sources))
    return sources


def _errors(checker: ModuleType | _MissingChecker, root: Path) -> list[str]:
    return checker.validate_repository(root, root / "config/container-images.lock.yml")


def test_python_wrapper_analysis_scales_near_linearly(
    checker: ModuleType | _MissingChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    visitor_type = checker._DockerRunVisitor
    original = visitor_type._function_wrapper_spec
    calls = 0

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(visitor_type, "_function_wrapper_spec", counted)

    def analyze(function_count: int) -> int:
        nonlocal calls
        calls = 0
        definitions = [
            "import subprocess",
            "def helper_0(value):\n    subprocess.run(value)",
        ]
        definitions.extend(
            f"def helper_{index}(value):\n    helper_{index - 1}(value)"
            for index in range(1, function_count)
        )
        source = "\n".join(definitions)
        visitor_type("fixture.py", [], []).visit(ast.parse(source))
        return calls

    small = analyze(20)
    large = analyze(40)

    assert large <= small * 5 // 2, (small, large)


def test_python_owned_call_analysis_reuses_structural_walks(
    checker: ModuleType | _MissingChecker, monkeypatch: pytest.MonkeyPatch
) -> None:
    visitor_type = checker._DockerRunVisitor
    original = visitor_type._collect_owned_call_tree
    walks = 0

    def counted(
        cls: type[object],
        current: ast.AST,
        conditional: bool,
        calls: list[tuple[ast.Call, bool]],
    ) -> None:
        nonlocal walks
        walks += 1
        original(current, conditional, calls)

    monkeypatch.setattr(visitor_type, "_collect_owned_call_tree", classmethod(counted))
    definition = ast.parse(
        "def configure(value, enabled):\n"
        "    global command\n"
        "    if enabled:\n"
        "        normalize(value)\n"
        "    def nested():\n"
        "        normalize(value)\n"
        "    command = normalize(value)\n"
        "    return command\n"
        "    normalize(value)\n"
    ).body[0]
    assert isinstance(definition, ast.FunctionDef)
    visitor = visitor_type("fixture.py", [], [])

    first = visitor._owned_calls(definition)
    first_walks = walks
    assert [
        (call.func.id, conditional)
        for call, conditional in first
        if isinstance(call.func, ast.Name)
    ] == [("normalize", True), ("normalize", False)]
    first.clear()
    second = visitor._owned_calls(definition)

    assert first_walks > 0
    assert walks == first_walks
    assert second


def test_gate_boundary_python_keeps_forward_wrapper_and_late_binding_proofs(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "def wrapper(command):\n    helper(command)\n"
        "def helper(command):\n    runner(command)\n"
        "import subprocess\nrunner = subprocess.run\n"
        "wrapper(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


def test_repository_catalog_contains_the_nine_registry_verified_references(
    checker: ModuleType | _MissingChecker,
) -> None:
    catalog = checker.load_catalog(LOCK_PATH)
    actual = {entry.tag: entry.digest for entry in catalog.images.values()}
    assert actual == EXPECTED_LOCKED_REFERENCES
    assert all("linux/amd64" in entry.platforms for entry in catalog.images.values())


def test_repository_consumers_match_catalog(
    checker: ModuleType | _MissingChecker,
) -> None:
    errors = checker.validate_repository(REPO_ROOT, LOCK_PATH)

    assert errors == [], "\n".join(errors)


def test_discovers_every_supported_operational_consumer(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    expected_sources = _write_valid_repo(tmp_path)

    uses = checker.discover_images(tmp_path)

    assert {use.source for use in uses if not use.local} == set(expected_sources)
    assert {use.reference for use in uses if not use.local} == {REFERENCE}
    assert _errors(checker, tmp_path) == []


def test_supports_ci_string_and_mapping_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text(
        f"job:\n  image:\n    name: {REFERENCE}\n  services:\n    - {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "bad_digest",
    ["sha256:" + "a" * 63, "sha256:" + "a" * 65, "sha256:" + "g" * 64],
)
def test_rejects_malformed_lock_digests(
    checker: ModuleType | _MissingChecker, tmp_path: Path, bad_digest: str
) -> None:
    _write_valid_repo(tmp_path)
    lock = yaml.safe_load((tmp_path / "config/container-images.lock.yml").read_text())
    lock["images"]["python-3-12-slim"]["digest"] = bad_digest
    lock["images"]["python-3-12-slim"]["reference"] = f"{TAG}@{bad_digest}"
    _write_yaml(tmp_path / "config/container-images.lock.yml", lock)

    assert any("digest" in error for error in _errors(checker, tmp_path))


def test_rejects_reference_without_a_readable_tag(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text(f"FROM python@{DIGEST}\n")

    assert any("tag" in error and "Dockerfile" in error for error in _errors(checker, tmp_path))


def test_comments_do_not_count_as_consumers(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"# FROM {REFERENCE}\nFROM scratch\n")

    errors = _errors(checker, tmp_path)

    assert any(
        "consumer" in error and "services/new-worker/Dockerfile" in error for error in errors
    )


def test_rejects_two_digests_for_the_same_tag(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"FROM {TAG}@{OTHER_DIGEST}\n")

    assert any("divergent" in error for error in _errors(checker, tmp_path))


def test_rejects_unknown_digest_pinned_image(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"FROM alpine:3.22@{OTHER_DIGEST}\n")

    assert any("not in catalog" in error for error in _errors(checker, tmp_path))


def test_rejects_orphaned_catalog_entry(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    lock = yaml.safe_load((tmp_path / "config/container-images.lock.yml").read_text())
    orphan = dict(lock["images"]["python-3-12-slim"])
    orphan["reference"] = f"alpine:3.22@{OTHER_DIGEST}"
    orphan["tag"] = "alpine:3.22"
    orphan["digest"] = OTHER_DIGEST
    orphan["consumers"] = ["Dockerfile"]
    lock["images"]["orphan"] = orphan
    _write_yaml(tmp_path / "config/container-images.lock.yml", lock)

    assert any("orphan" in error for error in _errors(checker, tmp_path))


def test_rejects_duplicate_yaml_keys(checker: ModuleType | _MissingChecker, tmp_path: Path) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        f"services:\n  runtime:\n    image: {REFERENCE}\n    image: {REFERENCE}\n"
    )

    assert any("duplicate" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        (".gitlab-ci.yml", "job:\n  image: $PYTHON_IMAGE\n"),
        ("docker-compose.yml", "services:\n  app:\n    image: ${APP_IMAGE}\n"),
        ("Dockerfile", "ARG BASE_IMAGE\nFROM $BASE_IMAGE\n"),
    ],
)
def test_rejects_variable_image_references(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / relative_path).write_text(content)

    assert any(
        "variable" in error and relative_path in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "content",
    [
        "include: other.yml\njob:\n  image: " + REFERENCE + "\n",
        "job:\n  extends: .base\n  image: " + REFERENCE + "\n",
        "job:\n  image: !reference [.base, image]\n",
    ],
)
def test_ci_fails_closed_on_external_indirection(
    checker: ModuleType | _MissingChecker, tmp_path: Path, content: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text(content)

    assert any(
        marker in error
        for error in _errors(checker, tmp_path)
        for marker in ("include", "extends", "tag")
    )


def test_rejects_unresolved_shell_image_variable(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        '#!/usr/bin/env bash\ndocker run --rm "$UNRESOLVED_IMAGE" true\n'
    )

    assert any(
        "variable" in error and "scripts/build-image.sh" in error
        for error in _errors(checker, tmp_path)
    )


def test_ignores_shell_mentions_that_are_not_docker_run_or_pull(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\ncommand -v docker\ndocker info\ndocker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


def test_shell_resolves_assignments_in_execution_order(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{TAG}"\ndocker run --rm "$IMAGE" true\n'
        f'IMAGE="{REFERENCE}"\ndocker run --rm "$IMAGE" true\n'
    )

    assert any(
        "scripts/build-image.sh" in error and "opaque" in error and "IMAGE" in error
        for error in _errors(checker, tmp_path)
    )


def test_dynamic_shell_reassignment_invalidates_the_previous_literal(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{REFERENCE}"\n'
        'IMAGE="$IMAGE_OVERRIDE"\ndocker run --rm "$IMAGE" true\n'
    )

    assert any(
        "scripts/build-image.sh" in error and "variable" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "command",
    [
        f"/usr/bin/docker --context dev-pc run --rm {REFERENCE} true",
        f"docker --config /tmp/docker --host unix:///run/docker.sock "
        f"--log-level info --debug pull {REFERENCE}",
    ],
)
def test_discovers_absolute_docker_and_supported_global_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert _errors(checker, tmp_path) == []


def test_discovers_relative_docker_executable(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n./docker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        f"bash -c 'docker run --rm {REFERENCE} true'",
        f"sh -c 'docker pull {REFERENCE}'",
        f"dash -c 'docker run --rm {REFERENCE} true'",
        f"zsh -c 'docker pull {REFERENCE}'",
        f"sudo -u root /bin/bash -c 'docker pull {REFERENCE}'",
        f"eval 'docker run --rm {REFERENCE} true'",
    ],
)
def test_rejects_shell_indirection_that_contains_docker_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any(
        "scripts/build-image.sh" in error and "indirect" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "command",
    [
        f"bash -lc 'docker run --rm {REFERENCE} true'",
        f"bash --norc --rcfile /tmp/bashrc -c 'docker pull {REFERENCE}'",
    ],
)
def test_parses_only_real_interpreter_command_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("indirect" in error for error in _errors(checker, tmp_path))


def test_long_interpreter_option_does_not_enable_command_mode(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nbash --norc 'docker run {TAG}'\ndocker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("executor", ["eval", "bash -c"])
def test_resolves_static_indirect_command_variables(
    checker: ModuleType | _MissingChecker, tmp_path: Path, executor: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nCMD='docker run --rm {REFERENCE} true'\n{executor} \"$CMD\"\n"
    )

    assert any("indirect" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        'CMD="$OVERRIDE"\nbash -c "$CMD"',
        'eval "$MISSING_CMD"',
    ],
    ids=["dynamic", "unresolved"],
)
def test_rejects_opaque_indirect_command_variables(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any(
        marker in error
        for error in _errors(checker, tmp_path)
        for marker in ("dynamic", "unresolved")
    )


def test_prefixed_assignment_does_not_persist_or_expand_itself(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{REFERENCE}" docker run --rm "$IMAGE" true\n'
        'docker run --rm "$IMAGE" true\n'
    )

    assert any("unresolved variable" in error for error in _errors(checker, tmp_path))


def test_prefixed_assignment_uses_the_previous_environment(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{TAG}"\nIMAGE="{REFERENCE}" docker run --rm "$IMAGE" true\n'
    )

    assert any(TAG in error and "not pinned" in error for error in _errors(checker, tmp_path))


def test_does_not_treat_logged_docker_text_as_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nlog "would execute docker run {TAG}"\n'
        f"docker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("build_value", [None, {}], ids=["null", "empty-mapping"])
def test_local_image_allowlist_requires_a_valid_build_definition(
    checker: ModuleType | _MissingChecker, tmp_path: Path, build_value: object
) -> None:
    _write_valid_repo(tmp_path)
    compose_path = tmp_path / "deploy/dev-pc/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    compose["services"]["embedding-supervisor"]["build"] = build_value
    _write_yaml(compose_path, compose)

    assert any("local image" in error and "build" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "build_value",
    [
        "https://example.com/embedding.git",
        {"context": "../../../outside-repository"},
        {
            "context": "../../services/embedding_supervisor",
            "dockerfile_inline": "FROM scratch",
        },
    ],
    ids=["url-string", "outside-root", "dockerfile-inline"],
)
def test_local_build_rejects_non_repository_or_non_strict_contexts(
    checker: ModuleType | _MissingChecker, tmp_path: Path, build_value: object
) -> None:
    _write_valid_repo(tmp_path)
    compose_path = tmp_path / "deploy/dev-pc/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    compose["services"]["embedding-supervisor"]["build"] = build_value
    _write_yaml(compose_path, compose)

    assert any(
        "local image" in error and ("build" in error or "context" in error)
        for error in _errors(checker, tmp_path)
    )


def test_local_build_context_must_match_the_catalog(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    compose_path = tmp_path / "deploy/dev-pc/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    compose["services"]["embedding-supervisor"]["build"] = {
        "context": "../../services/embedding_qodo"
    }
    _write_yaml(compose_path, compose)

    assert any(
        "context" in error and "services/embedding_supervisor" in error
        for error in _errors(checker, tmp_path)
    )


def test_local_build_context_requires_a_dockerfile(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/Dockerfile").unlink()

    assert any("Dockerfile" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "pull_policy",
    [None, "missing", "always"],
    ids=["absent", "missing", "always"],
)
def test_local_image_allowlist_requires_pull_policy_build(
    checker: ModuleType | _MissingChecker, tmp_path: Path, pull_policy: str | None
) -> None:
    _write_valid_repo(tmp_path)
    compose_path = tmp_path / "deploy/dev-pc/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    service = compose["services"]["embedding-supervisor"]
    if pull_policy is None:
        service.pop("pull_policy")
    else:
        service["pull_policy"] = pull_policy
    _write_yaml(compose_path, compose)

    assert any(
        "local image" in error and "pull_policy" in error for error in _errors(checker, tmp_path)
    )


def test_local_catalog_records_exact_repository_contexts(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["local_images"]["brain-embedding-supervisor:local"]["context"] = (
        "services/embedding_supervisor"
    )
    lock["local_images"]["brain-embedding-qodo:local"]["context"] = "services/embedding_qodo"
    _write_yaml(lock_path, lock)

    catalog = checker.load_catalog(lock_path)

    assert catalog.local_images["brain-embedding-supervisor:local"].context == (
        "services/embedding_supervisor"
    )
    assert catalog.local_images["brain-embedding-qodo:local"].context == ("services/embedding_qodo")


def test_lock_schema_rejects_unknown_fields(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    lock = yaml.safe_load((tmp_path / "config/container-images.lock.yml").read_text())
    lock["images"]["python-3-12-slim"]["surprise"] = True
    _write_yaml(tmp_path / "config/container-images.lock.yml", lock)

    assert any("unknown" in error and "surprise" in error for error in _errors(checker, tmp_path))


def test_lock_registry_must_match_the_canonical_tag_registry(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    lock = yaml.safe_load((tmp_path / "config/container-images.lock.yml").read_text())
    lock["images"]["python-3-12-slim"]["registry"] = "ghcr.io"
    _write_yaml(tmp_path / "config/container-images.lock.yml", lock)

    assert any(
        "registry" in error and "registry-1.docker.io" in error
        for error in _errors(checker, tmp_path)
    )


def test_lock_rejects_unknown_manifest_media_types(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    lock = yaml.safe_load((tmp_path / "config/container-images.lock.yml").read_text())
    lock["images"]["python-3-12-slim"]["media_type"] = "application/vnd.example.unknown"
    _write_yaml(tmp_path / "config/container-images.lock.yml", lock)

    assert any("media_type" in error for error in _errors(checker, tmp_path))


def test_cli_is_offline_and_reports_actionable_errors(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    del checker
    _write_valid_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text(f"FROM {TAG}\n")

    result = subprocess.run(
        [
            sys.executable,
            str(CHECKER_PATH),
            "--root",
            str(tmp_path),
            "--lock",
            str(tmp_path / "config/container-images.lock.yml"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Dockerfile" in result.stderr
    assert TAG in result.stderr
    assert "not pinned" in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "docker container run --rm alpine:latest true",
        "docker image pull alpine:latest",
        "result=$(docker pull alpine:latest)",
    ],
    ids=["container-run", "image-pull", "command-substitution"],
)
def test_shell_rejects_floating_images_in_supported_docker_execution_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    errors = _errors(checker, tmp_path)

    assert any("alpine:latest" in error for error in errors)


def test_shell_resolves_a_literal_docker_executable_assignment(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nDOCKER=docker; "$DOCKER" run --rm {REFERENCE} true\n'
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "assignment",
    ['DOCKER="$DOCKER_OVERRIDE"', "DOCKER=$(command -v docker)"],
    ids=["dynamic-variable", "command-substitution"],
)
def test_shell_fails_closed_on_opaque_docker_executable_assignments(
    checker: ModuleType | _MissingChecker, tmp_path: Path, assignment: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{assignment}; "$DOCKER" run --rm {REFERENCE} true\n'
    )

    assert any(
        "scripts/build-image.sh" in error and "docker executable" in error
        for error in _errors(checker, tmp_path)
    )


def test_shell_does_not_treat_unquoted_logged_docker_text_as_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' docker run {TAG}\ndocker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("section", ["script", "before_script", "after_script"])
@pytest.mark.parametrize("form", ["string", "list", "multiline"])
def test_ci_rejects_floating_docker_images_in_every_script_section(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    section: str,
    form: str,
) -> None:
    _write_valid_repo(tmp_path)
    command = "docker container run --rm alpine:latest true"
    if form == "list":
        script_value: object = ["echo preparing", command]
    elif form == "multiline":
        script_value = f"echo preparing\n{command}\n"
    else:
        script_value = command
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"image": REFERENCE, section: script_value}},
    )

    errors = _errors(checker, tmp_path)

    assert any("alpine:latest" in error for error in errors)


def test_ci_allows_only_the_current_pipeline_produced_image_reference(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "build:docker": {
                "image": REFERENCE,
                "script": [
                    f"docker build -t {CI_SMOKE_IMAGE} .",
                    f"{CI_SMOKE_RUN} python -V",
                ],
            }
        },
    )

    assert _errors(checker, tmp_path) == []


def test_ci_rejects_arbitrary_external_variable_image_references(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"image": REFERENCE, "script": ["docker pull $EXTERNAL_IMAGE"]}},
    )

    assert any(
        ".gitlab-ci.yml" in error and "variable image reference" in error
        for error in _errors(checker, tmp_path)
    )


def test_compose_validates_and_scans_a_build_service_without_an_image(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    dockerfile = tmp_path / "ops/runtime-worker/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM alpine:latest\n")
    _write_yaml(
        tmp_path / "docker-compose.yml",
        {"services": {"runtime": {"build": {"context": "./ops/runtime-worker"}}}},
    )

    assert any(
        "ops/runtime-worker/Dockerfile" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "build",
    [
        "https://example.com/worker.git",
        {"context": "../outside-repository"},
        {"context": ".", "dockerfile_inline": "FROM alpine:latest"},
    ],
    ids=["remote", "outside-root", "inline-dockerfile"],
)
def test_compose_rejects_unverifiable_build_only_services(
    checker: ModuleType | _MissingChecker, tmp_path: Path, build: object
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "docker-compose.yml",
        {"services": {"runtime": {"build": build}}},
    )

    assert any(
        "docker-compose.yml" in error and "build" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"include": "compose.shared.yml"},
        {"services": {"runtime": {"image": REFERENCE, "extends": "base"}}},
    ],
    ids=["top-level-include", "service-extends"],
)
def test_compose_rejects_external_configuration_indirection(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: dict[str, object]
) -> None:
    _write_valid_repo(tmp_path)
    compose = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    for key, value in mutation.items():
        if key == "services":
            compose["services"].update(value)
        else:
            compose[key] = value
    _write_yaml(tmp_path / "docker-compose.yml", compose)

    assert any(
        marker in error for error in _errors(checker, tmp_path) for marker in ("include", "extends")
    )


def test_discovers_floating_dockerfiles_under_deploy(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    dockerfile = tmp_path / "deploy/x/worker/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM alpine:latest\n")

    errors = _errors(checker, tmp_path)

    assert any(
        "deploy/x/worker/Dockerfile" in error and "alpine:latest" in error for error in errors
    )


@pytest.mark.parametrize(
    "call",
    [
        f"self._client.containers.run('{TAG}', command=['true'])",
        (f"await asyncio.to_thread(self._client.containers.run, '{TAG}', command=['true'])"),
    ],
    ids=["direct", "asyncio-to-thread"],
)
def test_python_ast_rejects_floating_literals_at_each_docker_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, call: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"import asyncio\nclass NvidiaSmiGpuProbe:\n    async def run(self):\n        {call}\n"
    )

    errors = _errors(checker, tmp_path)

    assert any(TAG in error and "not pinned" in error for error in errors)


@pytest.mark.parametrize(
    "call",
    [
        f"self._client.containers.run('{REFERENCE}', command=['true'])",
        (f"await asyncio.to_thread(self._client.containers.run, '{REFERENCE}', command=['true'])"),
    ],
    ids=["direct", "asyncio-to-thread"],
)
def test_python_ast_accepts_exact_literals_at_each_docker_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, call: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"import asyncio\nclass NvidiaSmiGpuProbe:\n    async def run(self):\n        {call}\n"
    )

    assert _errors(checker, tmp_path) == []


def test_python_ast_rejects_starred_image_arguments(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"def run(client):\n    client.containers.run(*['{REFERENCE}'])\n"
    )

    assert any(
        "containers.run image must be explicit" in error for error in _errors(checker, tmp_path)
    )


def test_python_ast_accepts_an_exact_keyword_image_argument(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"def run(client):\n    client.containers.run(image='{REFERENCE}')\n"
    )

    assert _errors(checker, tmp_path) == []


def test_python_ast_rejects_class_image_constants_even_when_pinned(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        f"    IMAGE = '{REFERENCE}'\n"
        "    def run(self):\n"
        "        self._client.containers.run(self.IMAGE, command=['true'])\n"
    )

    assert any("literal image expression" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "image_expression",
    ["image", "self.image_factory()"],
    ids=["alias", "dynamic-expression"],
)
def test_python_ast_fails_closed_on_non_literal_image_expressions(
    checker: ModuleType | _MissingChecker, tmp_path: Path, image_expression: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        f"    IMAGE = '{REFERENCE}'\n"
        "    def run(self, image):\n"
        f"        self._client.containers.run({image_expression}, command=['true'])\n"
    )

    assert any(
        "services/embedding_supervisor/main.py" in error and "literal image expression" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "command",
    [
        "(docker run --rm alpine:latest true)",
        "result=`docker image pull alpine:latest`",
    ],
    ids=["subshell", "backticks"],
)
def test_shell_rejects_docker_execution_in_unsupported_subshell_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("alpine:latest" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "relative_path",
    [
        "compose.yml",
        "compose.yaml",
        "deploy/edge/compose.yml",
        "deploy/edge/compose.yaml",
    ],
)
def test_discovers_modern_compose_filenames(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / relative_path,
        {"services": {"worker": {"image": "alpine:latest"}}},
    )

    assert any(
        relative_path in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "relative_path",
    ["services/new-worker/runtime.py", "scripts/release_worker.py"],
)
def test_python_ast_scans_all_operational_service_and_script_modules(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"def run(client):\n    client.containers.run('{REFERENCE}', command=['true'])\n"
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append(relative_path)
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


def test_python_ast_rejects_unrecognised_containers_run_aliases(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(
        "def run(client):\n"
        "    invoke = client.containers.run\n"
        f"    invoke('{REFERENCE}', command=['true'])\n"
    )

    assert any(
        "services/new-worker/runtime.py" in error and "alias" in error
        for error in _errors(checker, tmp_path)
    )


def test_python_ast_excludes_bench_and_test_trees(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    for relative_path in ("bench/probe.py", "tests/probe.py"):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "def run(client):\n    client.containers.run('alpine:latest', command=['true'])\n"
        )

    assert _errors(checker, tmp_path) == []


def _write_ci_job(
    root: Path,
    commands: list[str],
    *,
    job: str = "build:docker",
    section: str = "script",
) -> None:
    _write_yaml(
        root / ".gitlab-ci.yml",
        {job: {"image": REFERENCE, section: commands}},
    )


@pytest.mark.parametrize(
    ("job", "section", "command"),
    [
        ("test:unit", "script", f"{CI_SMOKE_RUN} true"),
        ("build:docker", "before_script", f"{CI_SMOKE_RUN} true"),
        ("build:docker", "after_script", f"{CI_SMOKE_RUN} true"),
        ("build:docker", "script", f"docker pull {CI_SMOKE_IMAGE}"),
        (
            "build:docker",
            "script",
            "docker run --rm $CI_REGISTRY_IMAGE:${ENV_PREFIX}-${SHORT_SHA} true",
        ),
    ],
    ids=["wrong-job", "before-script", "after-script", "pull", "old-registry-tag"],
)
def test_ci_smoke_exception_is_bound_to_one_job_section_and_run_verb(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    job: str,
    section: str,
    command: str,
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [f"docker build -t {CI_SMOKE_IMAGE} .", command],
        job=job,
        section=section,
    )

    assert any(".gitlab-ci.yml" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "commands",
    [
        [f"{CI_SMOKE_RUN} true"],
        [
            f"{CI_SMOKE_RUN} true",
            f"docker build -t {CI_SMOKE_IMAGE} .",
        ],
        ["docker build .", f"{CI_SMOKE_RUN} true"],
        [
            f"docker build --build-arg -t {CI_SMOKE_IMAGE} .",
            f"{CI_SMOKE_RUN} true",
        ],
    ],
    ids=[
        "missing-build",
        "build-after-run",
        "build-without-smoke-tag",
        "tag-token-consumed-as-option-value",
    ],
)
def test_ci_smoke_image_must_be_built_with_the_exact_tag_before_run(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    commands: list[str],
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(tmp_path, commands)

    assert any("smoke" in error and "built" in error for error in _errors(checker, tmp_path))


def test_ci_smoke_image_build_must_be_unconditional(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [
            f"if false; then docker build -t {CI_SMOKE_IMAGE} .; fi",
            f"{CI_SMOKE_RUN} true",
        ],
    )

    assert any("smoke" in error and "built" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "build_command",
    [
        f"! docker build -t {CI_SMOKE_IMAGE} .",
        f"false && docker build -t {CI_SMOKE_IMAGE} .",
        f"docker build -t {CI_SMOKE_IMAGE} . || true",
        f"docker build -t {CI_SMOKE_IMAGE} . | tee build.log",
        f"docker build -t {CI_SMOKE_IMAGE} . &",
        f"docker build -t {CI_SMOKE_IMAGE} https://example.invalid/context.git",
        f"docker build -t {CI_SMOKE_IMAGE} -",
        f"printf x | xargs -r docker build -t {CI_SMOKE_IMAGE} .",
    ],
    ids=[
        "negated",
        "and-conditional",
        "or-fallback",
        "pipeline",
        "background",
        "remote-context",
        "stdin-context",
        "xargs-wrapper",
    ],
)
def test_ci_smoke_provenance_requires_one_direct_local_sequential_build(
    checker: ModuleType | _MissingChecker, tmp_path: Path, build_command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(tmp_path, [build_command, f"{CI_SMOKE_RUN} true"])

    assert any("smoke" in error and "built" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "tag_option",
    [f"--tag={CI_SMOKE_IMAGE}", f"-t{CI_SMOKE_IMAGE}"],
    ids=["long", "short"],
)
def test_ci_smoke_accepts_supported_attached_tag_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, tag_option: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [f"docker build {tag_option} .", f"{CI_SMOKE_RUN} true"],
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("build_options", "diagnostic"),
    [
        ("-f /tmp/evil", "Dockerfile"),
        ("-f -", "Dockerfile"),
        ("-f TotallyUnscanned", "Dockerfile"),
        ("--file=/tmp/evil", "Dockerfile"),
        ("--push", "--push"),
        ("--output type=oci,dest=image.tar", "--output"),
        ("--output=type=oci,dest=image.tar", "--output"),
        ("-o type=oci,dest=image.tar", "--output"),
        ("--check", "--check"),
    ],
    ids=[
        "absolute-dockerfile",
        "stdin-dockerfile",
        "unscanned-dockerfile",
        "attached-dockerfile",
        "push",
        "output",
        "attached-output",
        "short-output",
        "check",
    ],
)
def test_ci_smoke_rejects_non_inventory_build_inputs_and_non_build_modes(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    build_options: str,
    diagnostic: str,
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [
            f"docker build -t {CI_SMOKE_IMAGE} {build_options} .",
            f"{CI_SMOKE_RUN} true",
        ],
    )

    assert any(
        "CI smoke docker build" in error and diagnostic in error
        for error in _errors(checker, tmp_path)
    )


def test_ci_smoke_accepts_the_repository_build_flags(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [
            "docker build "
            "--cache-from type=registry,ref=$CI_REGISTRY_IMAGE:${ENV_PREFIX}-latest "
            "--build-arg BUILDKIT_INLINE_CACHE=1 "
            "-t $CI_REGISTRY_IMAGE:${ENV_PREFIX}-latest "
            "-t $CI_REGISTRY_IMAGE:${ENV_PREFIX}-${SHORT_SHA} "
            f"-t {CI_SMOKE_IMAGE} -f Dockerfile .",
            f"{CI_SMOKE_RUN} true",
        ],
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "intermediate_command",
    [
        f"docker image rm {CI_SMOKE_IMAGE}",
        f"docker tag alpine:latest {CI_SMOKE_IMAGE}",
        "docker load --input image.tar",
        f"docker push {CI_SMOKE_IMAGE}",
        f"docker save {CI_SMOKE_IMAGE}",
        "echo build-complete",
    ],
    ids=["image-rm", "tag", "load", "push", "save", "generic-command"],
)
def test_ci_smoke_build_provenance_expires_before_any_intermediate_command(
    checker: ModuleType | _MissingChecker, tmp_path: Path, intermediate_command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [
            f"docker build -t {CI_SMOKE_IMAGE} .",
            intermediate_command,
            f"{CI_SMOKE_RUN} true",
        ],
    )

    assert any("smoke" in error and "built" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "run_command",
    [
        f"docker run --rm {CI_SMOKE_IMAGE} true",
        f"docker run --pull=always --rm {CI_SMOKE_IMAGE} true",
        f"docker run --pull=$POLICY --rm {CI_SMOKE_IMAGE} true",
    ],
    ids=["missing", "always", "dynamic"],
)
def test_ci_smoke_run_requires_literal_pull_never(
    checker: ModuleType | _MissingChecker, tmp_path: Path, run_command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [f"docker build -t {CI_SMOKE_IMAGE} .", run_command],
    )

    assert any("--pull=never" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "time docker run --rm alpine:latest true",
        "time -f %E docker run --rm alpine:latest true",
        "exec docker image pull alpine:latest",
        "exec -a docker-cli docker image pull alpine:latest",
        "nohup docker run --rm alpine:latest true",
        "timeout 5 docker run --rm alpine:latest true",
        "timeout -k 1 5 docker run --rm alpine:latest true",
        "sudo env MODE=test docker run --rm alpine:latest true",
        "env -u MODE sudo -u root docker run --rm alpine:latest true",
        "{ docker run --rm alpine:latest true; }",
        "xargs docker run --rm alpine:latest",
        "xargs -n 1 docker run --rm alpine:latest",
        "command -- docker run --rm alpine:latest true",
    ],
    ids=[
        "time",
        "time-option",
        "exec",
        "exec-option",
        "nohup",
        "timeout",
        "timeout-option",
        "sudo-env",
        "env-sudo",
        "group",
        "xargs",
        "xargs-option",
        "command",
    ],
)
def test_shell_resolves_supported_wrapper_chains_and_groups(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("alpine:latest" in error for error in _errors(checker, tmp_path))


def test_shell_rejects_an_unknown_wrapper_around_docker(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nmystery-wrapper docker run --rm {REFERENCE} true\n"
    )

    assert any(
        "unsupported" in error and "wrapper" in error for error in _errors(checker, tmp_path)
    )


def test_shell_rejects_a_dynamic_top_level_docker_verb(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        '#!/usr/bin/env bash\nVERB=run\ndocker "$VERB" alpine:latest true\n'
    )

    assert any("dynamic docker command" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("command", "diagnostic"),
    [
        ("docker --context", "docker global option '--context' is missing its value"),
        ("docker build --tag", "docker build --tag value is missing"),
        ("docker build --file", "docker build --file value is missing"),
        ("docker pull", "docker pull image is missing"),
        ("docker run --volume", "docker run --volume value is missing"),
        ("docker create --name", "docker create --name value is missing"),
        (
            f"docker run --pull=never --pull=never {REFERENCE}",
            "docker run repeats --pull",
        ),
    ],
    ids=[
        "global-value",
        "build-tag",
        "build-option",
        "pull-image",
        "run-option",
        "create-option",
        "duplicate-pull",
    ],
)
def test_shell_rejects_malformed_docker_option_boundaries(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    command: str,
    diagnostic: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any(diagnostic in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        f"docker --context=default run --rm {REFERENCE} true",
        f"docker pull --platform=linux/amd64 {REFERENCE}",
        f"docker run --rm -w /repo -v /tmp/checkout:/repo {REFERENCE} dir .",
        f"docker run --rm --workdir=/repo {REFERENCE} true",
    ],
    ids=["global", "pull", "workdir-short", "workdir-attached"],
)
def test_shell_accepts_supported_attached_docker_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        "docker create --name repro alpine:latest true",
        "docker container create --name repro alpine:latest true",
    ],
    ids=["create", "container-create"],
)
def test_shell_rejects_floating_images_in_docker_create(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("alpine:latest" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("command", "diagnostic"),
    [
        ('env -S "docker run --rm alpine:latest true"', "env split-string"),
        ('nice bash -c "docker run --rm alpine:latest true"', "indirect shell execution"),
        ("nice docker --context default run --rm alpine:latest true", "alpine:latest"),
        (
            "$(command -v docker) run --rm alpine:latest true",
            "Docker executable command substitution",
        ),
    ],
    ids=["env-split-string", "nice-shell", "nice-docker", "command-substitution-executable"],
)
def test_shell_rejects_docker_execution_through_operational_wrappers(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    command: str,
    diagnostic: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any(diagnostic in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "mutation",
    [
        "read -r IMAGE <<< alpine:latest",
        "read -a IMAGE <<< alpine:latest",
        "read -d : IMAGE <<< alpine:latest",
        'eval "IMAGE=alpine:latest"',
        "printf -v IMAGE %s alpine:latest",
        "printf -vIMAGE %s alpine:latest",
    ],
    ids=["read", "read-array", "read-option", "eval", "printf-v", "printf-v-attached"],
)
def test_shell_image_state_rejects_non_assignment_mutations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{REFERENCE}"\n{mutation}\ndocker run --rm "$IMAGE" true\n'
    )

    assert any("opaque" in error and "IMAGE" in error for error in _errors(checker, tmp_path))


def test_indirect_shell_propagates_nested_scan_errors(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\nbash -c 'echo `docker pull alpine:latest`'\n"
    )

    assert any("indirect" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "harmless_line",
    [
        "echo `date`",
        f"VALUE=$(echo docker run {TAG})",
        f"VALUE=`echo docker run {TAG}`",
        f"true;# $(docker run {TAG})",
        f"printf '%s\\n' '$(docker pull {TAG})' '`docker run {TAG}`'",
        f"docker run --rm {REFERENCE} true # `docker pull {TAG}`",
        f"bash -c 'echo docker run {TAG}'",
        f"printf '%s\\n' bash -c 'docker run {TAG}'",
    ],
    ids=[
        "date-backtick",
        "echo-substitution",
        "echo-backtick",
        "comment-after-separator",
        "single-quoted",
        "comment",
        "shell-logs",
        "logged-shell",
    ],
)
def test_shell_ignores_non_executed_docker_text(
    checker: ModuleType | _MissingChecker, tmp_path: Path, harmless_line: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{harmless_line}\ndocker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


def test_shell_ignores_literal_docker_text_in_a_quoted_heredoc(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\ncat <<'EOF'\ndocker run alpine:latest\nEOF\n"
        f"docker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "script",
    [
        "cat <<'EOF'\ndocker run alpine:latest\n",
        'cat <<"$DELIMITER"\ndocker run alpine:latest\nEOF\n',
    ],
    ids=["unterminated", "dynamic-delimiter"],
)
def test_shell_heredocs_fail_closed_when_the_boundary_is_unverifiable(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}")

    assert any("shell heredoc" in error for error in _errors(checker, tmp_path))


def test_shell_resolves_image_state_behind_a_literal_docker_executable_variable(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nDOCKER=docker\nIMAGE="{REFERENCE}"\n'
        '"$DOCKER" run --rm "$IMAGE" true\n'
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "launcher",
    [
        "source",
        "bash",
        "env -i bash",
        "env -u MODE bash",
        "bash --",
        "bash --rcfile shellrc",
        "bash -O extglob",
    ],
    ids=[
        "source",
        "interpreter",
        "env-clean",
        "env-unset",
        "end-options",
        "rcfile",
        "shell-option",
    ],
)
def test_shell_scans_literal_local_dependency_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path, launcher: str
) -> None:
    _write_valid_repo(tmp_path)
    payload = tmp_path / "scripts/payload.inc"
    payload.write_text("docker run --rm alpine:latest true\n")
    (tmp_path / "scripts/shellrc").write_text("")
    launcher = launcher.replace("shellrc", '"$SCRIPT_DIR/shellrc"')
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n{launcher} "$SCRIPT_DIR/payload.inc"\n'
    )

    assert any(
        "scripts/payload.inc" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_shell_scans_interpreter_rcfile_dependency(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/shellrc").write_text("docker pull alpine:latest\n")
    (tmp_path / "scripts/payload.inc").write_text(f"docker pull {REFERENCE}\n")
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"
        'bash --rcfile "$SCRIPT_DIR/shellrc" "$SCRIPT_DIR/payload.inc"\n'
    )

    assert any(
        "scripts/shellrc" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_shell_scans_direct_anchored_dependency_file(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/payload.sh").write_text("#!/usr/bin/env bash\ndocker pull alpine:latest\n")
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"$SCRIPT_DIR/payload.sh"\n'
    )

    assert any(
        "scripts/payload.sh" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_shell_accepts_a_pinned_literal_local_dependency_file(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    payload = tmp_path / "scripts/payload.inc"
    payload.write_text(f"docker run --rm {REFERENCE} true\n")
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/payload.inc"\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].remove("scripts/build-image.sh")
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/payload.inc")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


def test_shell_scans_nested_dependencies_relative_to_each_caller(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    nested = tmp_path / "scripts/nested"
    nested.mkdir()
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/payload.inc"\n'
    )
    (tmp_path / "scripts/payload.inc").write_text(
        f'{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/nested/leaf.inc"\n'
    )
    (nested / "leaf.inc").write_text("docker pull alpine:latest\n")

    assert any(
        "scripts/nested/leaf.inc" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_shell_dependency_cycles_are_scanned_once(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"
        f'source "$SCRIPT_DIR/payload.inc"\ndocker pull {REFERENCE}\n'
    )
    (tmp_path / "scripts/payload.inc").write_text(
        f'{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/build-image.sh"\ndocker pull {REFERENCE}\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/payload.inc")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    ['source "$PAYLOAD"', "bash /tmp/payload.inc", "source ../../outside.inc"],
    ids=["dynamic", "absolute", "traversal-outside-repository"],
)
def test_shell_rejects_unverifiable_dependency_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("shell dependency" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        f'IMAGE="{REFERENCE}"\nif false; then IMAGE="{TAG}"; fi\ndocker run --rm "$IMAGE" true',
        f'IMAGE="{TAG}"\nIMAGE="{REFERENCE}"\ndocker run --rm "$IMAGE" true',
        f'if true; then IMAGE="{REFERENCE}"; fi\ndocker run --rm "$IMAGE" true',
        f'false && IMAGE="{REFERENCE}"\ndocker pull "$IMAGE"',
        f'true || IMAGE="{REFERENCE}"\ndocker pull "$IMAGE"',
        f'IMAGE="{REFERENCE}" &\ndocker pull "$IMAGE"',
        f'case "$MODE" in safe) IMAGE="{REFERENCE}" ;; esac\ndocker pull "$IMAGE"',
        f'( IMAGE="{REFERENCE}" )\ndocker pull "$IMAGE"',
    ],
    ids=[
        "branch-reassignment",
        "duplicate-assignment",
        "controlled-assignment",
        "and-conditional",
        "or-conditional",
        "background",
        "case-arm",
        "subshell",
    ],
)
def test_shell_image_variables_fail_closed_on_ambiguous_assignment_state(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any("opaque" in error and "IMAGE" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "continued_operator",
    ["false &&", "true ||", "printf x |"],
    ids=["and", "or", "pipeline"],
)
def test_shell_image_state_preserves_operators_continued_on_the_next_line(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    continued_operator: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\n{continued_operator}\nIMAGE="{REFERENCE}"\ndocker pull "$IMAGE"\n'
    )

    assert any("opaque" in error and "IMAGE" in error for error in _errors(checker, tmp_path))


def test_shell_image_state_accepts_an_uncontrolled_assignment_after_a_plain_newline(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\ntrue\nIMAGE="{REFERENCE}"\ndocker pull "$IMAGE"\n'
    )

    assert _errors(checker, tmp_path) == []


def test_shell_false_branch_cannot_mask_the_real_floating_image_value(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nIMAGE="{TAG}"\nif false; then\n'
        f'IMAGE="{REFERENCE}"\nfi\ndocker pull "$IMAGE"\n'
    )

    assert any("opaque" in error and "IMAGE" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        f'case "$MODE" in safe) docker pull "{REFERENCE}" ;; esac',
        f'case "$MODE" in safe) true ;; esac\nIMAGE="{REFERENCE}"\ndocker pull "$IMAGE"',
    ],
    ids=["literal-inside-case", "assignment-after-case"],
)
def test_shell_case_guards_preserve_unambiguous_pinned_images(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert _errors(checker, tmp_path) == []


def test_ci_script_list_preserves_one_literal_image_assignment(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [f'IMAGE="{REFERENCE}"', 'docker run --rm "$IMAGE" true'],
        job="test:image",
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "scripts/check.bash",
        "scripts/check.zsh",
        "deploy/edge/check.dash",
        "deploy/edge/check",
    ],
)
def test_discovers_operational_shell_extensions_and_shebang_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\ndocker run --rm alpine:latest true\n")

    assert any(
        relative_path in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


def test_ignores_extensionless_files_without_a_shell_shebang(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    path = tmp_path / "scripts/not-shell"
    path.write_text("#!/usr/bin/env python3\nTEXT = 'docker run alpine:latest'\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("copy_from", ["alpine:latest", "alpine:3.22"])
@pytest.mark.parametrize("syntax", ["--from={value}", "--from {value}"])
def test_dockerfile_rejects_floating_external_copy_sources(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    copy_from: str,
    syntax: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        "FROM scratch AS runtime\n"
        f"COPY {syntax.format(value=copy_from)} /usr/bin/tool /usr/bin/tool\n"
    )

    assert any(copy_from in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize("copy_from", ["base", "0"])
def test_dockerfile_copy_from_internal_stage_is_not_an_image_consumer(
    checker: ModuleType | _MissingChecker, tmp_path: Path, copy_from: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM {REFERENCE} AS base\n"
        "FROM scratch AS runtime\n"
        f"COPY --from={copy_from} /usr/bin/tool /usr/bin/tool\n"
    )

    assert _errors(checker, tmp_path) == []


def test_dockerfile_discovers_an_exact_external_copy_source(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM scratch\nCOPY --from={REFERENCE} /usr/bin/tool /usr/bin/tool\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("mount_from", ["alpine:latest", "alpine:3.22"])
@pytest.mark.parametrize(
    "mount_option",
    ["--mount=type=bind,from={source},target=/src", "--mount type=bind,from={source},target=/src"],
    ids=["equals", "separate-value"],
)
def test_dockerfile_rejects_floating_external_run_mount_sources(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    mount_from: str,
    mount_option: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM scratch\nRUN {mount_option.format(source=mount_from)} true\n"
    )

    assert any(mount_from in error for error in _errors(checker, tmp_path))


def test_dockerfile_discovers_an_exact_external_run_mount_source(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM scratch\nRUN --mount=type=bind,from={REFERENCE},target=/src true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("mount_from", ["base", "0"])
def test_dockerfile_run_mount_from_internal_stage_is_not_an_image_consumer(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mount_from: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM {REFERENCE} AS base\nRUN --mount=type=bind,from={mount_from},target=/src true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "instruction",
    [
        "ONBUILD COPY --from=alpine:latest /usr/bin/tool /usr/bin/tool",
        "ONBUILD RUN --mount=type=bind,from=alpine:latest,target=/src true",
    ],
    ids=["copy", "run-mount"],
)
def test_dockerfile_rejects_floating_external_sources_in_onbuild(
    checker: ModuleType | _MissingChecker, tmp_path: Path, instruction: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"FROM scratch\n{instruction}\n")

    assert any(
        "services/new-worker/Dockerfile:2" in error
        and "image alpine:latest is not in catalog" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "instruction",
    [
        f"ONBUILD COPY --from={REFERENCE} /usr/bin/tool /usr/bin/tool",
        f"ONBUILD RUN --mount=type=bind,from={REFERENCE},target=/src true",
    ],
    ids=["copy", "run-mount"],
)
def test_dockerfile_accepts_exact_external_sources_in_onbuild(
    checker: ModuleType | _MissingChecker, tmp_path: Path, instruction: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"FROM scratch\n{instruction}\n")

    assert _errors(checker, tmp_path) == []


def test_dockerfile_accepts_boolean_run_mount_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(
        f"FROM scratch\nRUN --mount=type=bind,from={REFERENCE},target=/src,rw true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "compose.override.yml",
        "docker-compose.override.yaml",
        "deploy/edge/compose.gpu.yaml",
        "deploy/edge/docker-compose.ci.yml",
    ],
)
def test_discovers_compose_override_and_suffix_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / relative_path,
        {"services": {"worker": {"image": "alpine:latest"}}},
    )

    assert any(
        relative_path in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "body",
    [
        f"        self.IMAGE = '{TAG}'\n",
        f"        setattr(self, 'IMAGE', '{TAG}')\n",
    ],
    ids=["attribute-assignment", "setattr"],
)
def test_python_ast_rejects_mutated_class_image_constants(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        f"    IMAGE = '{REFERENCE}'\n"
        "    def run(self):\n"
        f"{body}"
        "        self._client.containers.run(self.IMAGE, command=['true'])\n"
    )

    assert any("literal image expression" in error for error in _errors(checker, tmp_path))


def test_python_ast_rejects_dynamic_getattr_containers_run(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "def run(client):\n"
        f"    getattr(client.containers, 'run')('{REFERENCE}', command=['true'])\n"
    )

    assert any(
        "getattr" in error and "containers.run" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "declaration",
    ["containers = client.containers", "containers: object = client.containers"],
    ids=["assignment", "annotated-assignment"],
)
def test_python_ast_rejects_containers_object_aliases(
    checker: ModuleType | _MissingChecker, tmp_path: Path, declaration: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "def run(client):\n"
        f"    {declaration}\n"
        f"    containers.run('{REFERENCE}', command=['true'])\n"
    )

    assert any("containers alias" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "call",
    [
        f"builtins.getattr(client.containers, 'run')('{REFERENCE}')",
        f"getattr(client.containers, method)('{REFERENCE}')",
    ],
    ids=["builtins", "dynamic-method"],
)
def test_python_ast_rejects_alternate_getattr_containers_run_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, call: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"import builtins\ndef run(client, method='run'):\n    {call}\n"
    )

    assert any(
        "getattr" in error and "containers.run" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        f"NvidiaSmiGpuProbe.IMAGE = '{TAG}'",
        f"setattr(NvidiaSmiGpuProbe, 'IMAGE', '{TAG}')",
        f"Alias = NvidiaSmiGpuProbe\nAlias.IMAGE = '{TAG}'",
        f"attribute = 'IMAGE'\nsetattr(NvidiaSmiGpuProbe, attribute, '{TAG}')",
    ],
    ids=["assignment", "setattr", "class-alias", "dynamic-attribute"],
)
def test_python_ast_rejects_module_level_class_constant_mutations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        f"    IMAGE = '{REFERENCE}'\n"
        "    def run(self, client):\n"
        "        client.containers.run(self.IMAGE, command=['true'])\n"
        f"{mutation}\n"
    )

    assert any("literal image expression" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "mutation",
    [
        f"cls.IMAGE = '{TAG}'",
        f"type(self).IMAGE = '{TAG}'",
        f"self.__class__.IMAGE = '{TAG}'",
        f"klass = type(self); klass.IMAGE = '{TAG}'",
        f"klass = type(self); setattr(klass, 'IMAGE', '{TAG}')",
        f"attribute = 'IMAGE'; setattr(type(self), attribute, '{TAG}')",
    ],
    ids=[
        "cls",
        "type-self",
        "dunder-class",
        "type-alias",
        "setattr-type-alias",
        "dynamic-setattr",
    ],
)
def test_python_ast_rejects_class_constant_mutations_through_class_receivers(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        "class NvidiaSmiGpuProbe:\n"
        f"    IMAGE = '{REFERENCE}'\n"
        "    @classmethod\n"
        "    def mutate(cls):\n"
        f"        {mutation}\n"
        "    def run(self, client):\n"
        "        client.containers.run(self.IMAGE, command=['true'])\n"
    )

    assert any("literal image expression" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("call", "api_name"),
    [
        (f"client.containers.create('{REFERENCE}')", "containers.create"),
        (f"client.api.create_container(image='{REFERENCE}')", "api.create_container"),
        (f"client.images.pull('{REFERENCE}')", "images.pull"),
        ("client.images.build(path='.', tag='worker:local')", "images.build"),
    ],
    ids=["containers-create", "api-create-container", "images-pull", "images-build"],
)
def test_python_ast_rejects_unsupported_operational_docker_sdk_apis(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    call: str,
    api_name: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/embedding_supervisor/main.py").write_text(
        f"def run(client):\n    {call}\n"
    )

    assert any(
        "unsupported Docker SDK API" in error and api_name in error
        for error in _errors(checker, tmp_path)
    )


def test_invalid_local_compose_build_reports_one_canonical_error(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    compose_path = tmp_path / "deploy/dev-pc/docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text())
    compose["services"]["embedding-supervisor"]["build"] = {
        "context": "../../services/embedding_supervisor",
        "dockerfile_inline": "FROM scratch",
    }
    _write_yaml(compose_path, compose)

    matching = [
        error
        for error in _errors(checker, tmp_path)
        if "embedding-supervisor" in error and "dockerfile_inline" in error
    ]
    assert len(matching) == 1, matching


@pytest.mark.parametrize(
    "script",
    [
        "alias d=docker\nd run --rm alpine:latest true",
        "hash -p /usr/bin/docker d\nd pull alpine:latest",
    ],
    ids=["shell-alias", "hash-alias"],
)
def test_gate_boundary_shell_rejects_docker_executable_aliases(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any("Docker executable alias" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "docker service create alpine:latest",
        "docker service update --image alpine:latest worker",
        "docker stack deploy -c stack.yml worker",
        "docker compose -f docker-compose.yml run runtime",
        "docker buildx build -t worker:local .",
        "docker build https://example.com/worker.git",
        "docker totally-unknown alpine:latest",
    ],
    ids=[
        "service-create",
        "service-update",
        "stack-deploy",
        "compose-run",
        "buildx-build",
        "remote-build-context",
        "unknown-verb",
    ],
)
def test_gate_boundary_shell_rejects_unmodelled_docker_commands(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert any("unsupported Docker command" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_here_string_is_data_not_a_script_dependency(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        "bash <<< 'printf %s docker run alpine:latest'\n"
        f"docker pull {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        f"bash <<< 'docker pull {TAG}'",
        f"INTERP=bash; $INTERP <<< 'docker pull {TAG}'",
    ],
    ids=["direct", "bound-interpreter"],
)
def test_gate_boundary_shell_rejects_executable_here_string_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{command}\ndocker pull {REFERENCE}\n"
    )

    assert any(
        "indirect shell execution" in error or "Docker" in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_does_not_execute_logged_alias_text(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' 'alias d=docker'\ndocker pull {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_accepts_modelled_pinned_docker_commands(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"docker pull {REFERENCE}\n"
        f"docker create --name probe {REFERENCE} true\n"
        f"docker run --rm {REFERENCE} true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "ops/runtime/Dockerfile.cuda",
        "ops/runtime/Containerfile",
        "deploy/edge/Containerfile.gpu",
        "services/worker/Dockerfile.prod",
    ],
    ids=["ops-dockerfile", "ops-containerfile", "deploy-containerfile", "service-suffix"],
)
def test_gate_boundary_discovery_scans_operational_dockerfile_variants(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("FROM alpine:latest\n")

    assert any(
        relative_path in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "ops/runtime/compose.prod.yml",
        "services/worker/docker-compose.ci.yaml",
        "scripts/release/stack.prod.yml",
        "deploy/edge/stack.prod.yaml",
        "stack.prod.yml",
    ],
    ids=["ops-compose", "service-compose", "script-stack", "deploy-stack", "root-stack"],
)
def test_gate_boundary_discovery_scans_operational_compose_and_stack_variants(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / relative_path,
        {"services": {"worker": {"image": "alpine:latest"}}},
    )

    assert any(
        relative_path in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_discovery_excludes_non_operational_trees(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    for relative_path in (
        "bench/runtime/Containerfile.dev",
        "docs/examples/stack.demo.yml",
        "tests/fixtures/Dockerfile.bad",
        ".claude/worktrees/example/compose.yml",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FROM alpine:latest\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "directive",
    [
        "# syntax=docker/dockerfile:1",
        f"# syntax=docker/dockerfile:1@{DIGEST}",
    ],
    ids=["floating", "digest-pinned"],
)
def test_gate_boundary_dockerfile_rejects_unsupported_frontend_directives(
    checker: ModuleType | _MissingChecker, tmp_path: Path, directive: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"{directive}\nFROM {REFERENCE}\n")

    assert any(
        "Dockerfile syntax directive is unsupported" in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_compose_resolves_an_explicit_internal_containerfile(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    containerfile = tmp_path / "ops/runtime/Containerfile.prod"
    containerfile.parent.mkdir(parents=True)
    containerfile.write_text(f"FROM {REFERENCE}\n")
    _write_yaml(
        tmp_path / "ops/compose.prod.yml",
        {
            "services": {
                "worker": {
                    "build": {
                        "context": "./runtime",
                        "dockerfile": "Containerfile.prod",
                    }
                }
            }
        },
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("ops/runtime/Containerfile.prod")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "dockerfile",
    ["/tmp/evil", "../outside/Dockerfile", "-", "Missingfile"],
    ids=["absolute", "outside", "stdin", "missing"],
)
def test_gate_boundary_compose_rejects_unverifiable_explicit_dockerfiles(
    checker: ModuleType | _MissingChecker, tmp_path: Path, dockerfile: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "docker-compose.yml",
        {
            "services": {
                "runtime": {
                    "image": REFERENCE,
                    "build": {"context": ".", "dockerfile": dockerfile},
                }
            }
        },
    )

    assert any("Compose build Dockerfile" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "dockerfile",
    ["/tmp/evil", "-", "../outside/Dockerfile", "Missingfile"],
    ids=["absolute", "stdin", "outside", "missing"],
)
def test_gate_boundary_shell_build_rejects_unverifiable_dockerfiles(
    checker: ModuleType | _MissingChecker, tmp_path: Path, dockerfile: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"docker build -t worker:local -f {dockerfile} .\n"
        f"docker pull {REFERENCE}\n"
    )

    assert any("docker build Dockerfile" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_compose_scans_multiple_internal_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "ops/base.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    _write_yaml(
        tmp_path / "ops/override.yml",
        {"services": {"worker": {"image": "alpine:latest"}}},
    )
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"
        'docker compose -f "$SCRIPT_DIR/../ops/base.yml" '
        '-f "$SCRIPT_DIR/../ops/override.yml" up\n'
        f"docker pull {REFERENCE}\n"
    )

    assert any(
        "ops/override.yml" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_compose_rejects_external_files(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\ndocker compose -f /tmp/evil.yml up\ndocker pull {REFERENCE}\n"
    )

    assert any("Compose file" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_compose_accepts_an_explicit_project_name(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """`-p` names the compose project; it changes nothing about the resolved images.

    With no explicit project, compose derives the project from the file's FOLDER and
    reconciles on (project, service): two disposable benches with different container
    names recreated each other — collateral destruction measured on the HNSW churn
    bench on 2026-08-28. Refusing `-p` here forced a bench script to choose between
    the gate and isolation. `down` enters on the same grounds: it is the same bench's
    teardown gesture, and it brings in no image.
    """
    sources = _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "ops/base.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    _write_yaml(
        tmp_path / "config/container-images.lock.yml",
        _catalog([*sources, "ops/base.yml"]),
    )
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"
        'docker compose -p bench -f "$SCRIPT_DIR/../ops/base.yml" up --detach\n'
        'docker compose --project-name bench -f "$SCRIPT_DIR/../ops/base.yml" down\n'
        'docker compose --project-name=bench -f "$SCRIPT_DIR/../ops/base.yml" down '
        "--remove-orphans --timeout 5\n"
        f"docker pull {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_compose_project_name_does_not_bypass_the_scan(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """Accepting `-p` does not exempt the compose file from pinning."""
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "ops/bad.yml",
        {"services": {"worker": {"image": "alpine:latest"}}},
    )
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\n"
        'docker compose -p bench -f "$SCRIPT_DIR/../ops/bad.yml" up\n'
        f"docker pull {REFERENCE}\n"
    )

    assert any(
        "ops/bad.yml" in error and "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_compose_rejects_a_dangling_project_flag(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\ndocker compose -p\ndocker pull {REFERENCE}\n"
    )

    assert any("value is missing" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_accepts_docker_network_management(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """`docker network inspect|rm` brings in no image: it is the churn bench's
    teardown and homonymy guard (the project's network must be inspected before
    destruction and removed by the safety net)."""
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        "docker network inspect bench_default\n"
        "docker network rm bench_default\n"
        f"docker pull {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "def run(client):\n"
            "    containers, = (client.containers,)\n"
            f"    containers.run('{REFERENCE}')\n"
        ),
        (f"def run(client):\n    (containers := client.containers).run('{REFERENCE}')\n"),
        (f"def run(client):\n    getattr(getattr(client, 'containers'), 'run')('{REFERENCE}')\n"),
        (
            "def run(client):\n"
            "    runner = client.containers.__getattribute__('run')\n"
            f"    runner('{REFERENCE}')\n"
        ),
    ],
    ids=["tuple-alias", "walrus-alias", "nested-getattr", "attribute-wrapper"],
)
def test_gate_boundary_python_rejects_obscured_containers_run_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(body)

    assert any("unsupported Docker SDK construct" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("call", "api_name"),
    [
        (f"client.images.get('{TAG}')", "images.get"),
        (f"client.images.push('{TAG}')", "images.push"),
        (f"client.api.pull('{TAG}')", "api.pull"),
        ("client.api.build(path='.', tag='worker:local')", "api.build"),
        (f"daemon.images.pull('{TAG}')", "images.pull"),
        (f"daemon.api.create_container(image='{TAG}')", "api.create_container"),
    ],
    ids=[
        "images-get",
        "images-push",
        "api-pull",
        "api-build",
        "arbitrary-images-owner",
        "arbitrary-api-owner",
    ],
)
def test_gate_boundary_python_rejects_every_unmodelled_sdk_namespace_call(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    call: str,
    api_name: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(f"def run(client):\n    {call}\n")

    assert any(
        "unsupported Docker SDK API" in error and api_name in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "body",
    [
        f"import subprocess\nsubprocess.run(['docker', 'run', '--rm', '{TAG}'])\n",
        f"import subprocess\nsubprocess.Popen(('docker', 'pull', '{TAG}'))\n",
        (f"import subprocess\nsubprocess.check_call('docker run --rm {TAG}', shell=True)\n"),
        f"import os\nos.system('docker pull {TAG}')\n",
    ],
    ids=["subprocess-run", "subprocess-popen", "subprocess-shell", "os-system"],
)
def test_gate_boundary_python_rejects_direct_docker_cli_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            f"command = ['docker', 'run', '--rm', '{TAG}']\n"
            "subprocess.run(command)\n"
        ),
        (
            "import subprocess\n"
            "executable = 'docker'\n"
            f"subprocess.run([executable, 'pull', '{TAG}'])\n"
        ),
        ("import os\nimage = input()\ncommand = f'docker run {image}'\nos.system(command)\n"),
    ],
    ids=["command-variable", "executable-variable", "linked-dynamic-string"],
)
def test_gate_boundary_python_rejects_statically_linked_docker_cli_variables(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nsubprocess.run(['git', 'status'])\n",
        ("import subprocess\nsubprocess.run(['printf', '%s', 'docker run alpine:latest'])\n"),
    ],
    ids=["non-docker", "docker-text-data"],
)
def test_gate_boundary_python_allows_non_docker_subprocesses(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_accepts_direct_static_gateway_compose_probe(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import json\nimport subprocess\n"
        "_GATEWAY_PROBE_SCRIPT = 'print(1)'\n"
        "new_token = 'new'\nold_token = 'old'\n"
        "subprocess.run(\n"
        "    ['docker', 'compose', '-f', 'docker-compose.yml', 'exec', '-T', "
        "'brain-codex-gateway', 'python', '-c', _GATEWAY_PROBE_SCRIPT],\n"
        "    input=json.dumps({'new': new_token, 'old': old_token}), capture_output=True, "
        "check=False, text=True,\n"
        ")\n"
    )

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("environment_expression", "setup"),
    [
        ("{key: value for key, value in os.environ.items() if True}", ""),
        ("{key: value for key, value in os.environ.items() if not key.startswith('OTHER_')}", ""),
        ("{key: value for key, value in os.environ.items() if key.startswith('COMPOSE_')}", ""),
        (
            "{key: value for key, value in inherited_environment.items() "
            "if not key.startswith('COMPOSE_')}",
            "        inherited_environment = os.environ\n",
        ),
        (
            "{key: value for source_key, source_value in os.environ.items() "
            "if not source_key.startswith('COMPOSE_')}",
            "",
        ),
        (
            "{key: value for key, value in os.environ.items() "
            "if not key.startswith('COMPOSE_') if value}",
            "",
        ),
    ],
    ids=[
        "non-filtering-condition",
        "different-prefix",
        "missing-not",
        "other-environment-source",
        "incoherent-key-value-targets",
        "additional-condition",
    ],
)
def test_gate_boundary_python_rejects_noncanonical_compose_environment_filter(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    environment_expression: str,
    setup: str,
) -> None:
    """Only the probe's inline COMPOSE_* filter can receive the exemption."""
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/rotate_codex_gateway_credentials.py").write_text(
        "import json\nimport os\nimport subprocess\n"
        "_GATEWAY_PROBE_SCRIPT = 'print(1)'\n"
        "class DockerGatewayProbe:\n"
        "    def __init__(self, brain_root):\n"
        "        self.brain_root = brain_root\n"
        "    def prove(self, old_token, new_token):\n" + setup + "        subprocess.run(\n"
        "            ['docker', 'compose', '--project-name', 'brain-v42', '-f', "
        "'docker-compose.yml', 'exec', '-T', 'brain-codex-gateway', 'python', '-c', "
        "_GATEWAY_PROBE_SCRIPT],\n"
        "            input=json.dumps({'new': new_token, 'old': old_token}), "
        "capture_output=True, check=False, text=True, cwd=self.brain_root, "
        f"env={environment_expression},\n"
        "        )\n"
    )

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "keyword_arguments",
    [
        "env={'COMPOSE_FILE': '/tmp/attacker-compose.yml'}",
        "cwd='/tmp/attacker-compose'",
        "shell=False",
        "executable='/usr/bin/docker'",
        "**{'env': {'COMPOSE_FILE': '/tmp/attacker-compose.yml'}}",
        "**options",
    ],
    ids=["compose-file-env", "external-cwd", "shell", "executable", "kwargs", "dynamic-kwargs"],
)
def test_gate_boundary_python_rejects_compose_exec_redirection_keywords(
    checker: ModuleType | _MissingChecker, tmp_path: Path, keyword_arguments: str
) -> None:
    """The gateway probe exemption cannot redirect Compose or subprocess execution."""
    _write_valid_repo(tmp_path)
    dynamic_prefix = (
        "options = {'env': {'COMPOSE_FILE': '/tmp/attacker-compose.yml'}}\n"
        if keyword_arguments == "**options"
        else ""
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        "import json\nimport subprocess\n"
        "_GATEWAY_PROBE_SCRIPT = 'print(1)'\n"
        "new_token = 'new'\nold_token = 'old'\n" + dynamic_prefix + "subprocess.run(\n"
        "    ['docker', 'compose', '-f', 'docker-compose.yml', 'exec', '-T', "
        "'brain-codex-gateway', 'python', '-c', _GATEWAY_PROBE_SCRIPT],\n"
        "    input=json.dumps({'new': new_token, 'old': old_token}), capture_output=True, "
        f"check=False, text=True, {keyword_arguments},\n"
        ")\n"
    )

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_python_rejects_compose_file_environment_redirect(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """COMPOSE_FILE is an external Compose-file selection, not probe data."""
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "_GATEWAY_PROBE_SCRIPT = 'print(1)'\n"
        "subprocess.run(\n"
        "    ['docker', 'compose', 'exec', '-T', 'brain-codex-gateway', 'python', '-c', "
        "_GATEWAY_PROBE_SCRIPT],\n"
        "    input='{}', capture_output=True, check=False, cwd='.', "
        "env={'COMPOSE_FILE': '/tmp/attacker-compose.yml'}, text=True,\n"
        ")\n"
    )

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_python_rejects_unrecognised_gateway_probe_payload_names(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """Only the real probe's two bearer values may occupy the JSON payload."""
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import json\nimport subprocess\n"
        "_GATEWAY_PROBE_SCRIPT = 'print(1)'\n"
        "attacker = 'new'\nvictim = 'old'\n"
        "subprocess.run(\n"
        "    ['docker', 'compose', '-f', 'docker-compose.yml', 'exec', '-T', "
        "'brain-codex-gateway', 'python', '-c', _GATEWAY_PROBE_SCRIPT],\n"
        "    input=json.dumps({'new': attacker, 'old': victim}), capture_output=True, "
        "check=False, text=True,\n"
        ")\n"
    )

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "def execute(command):\n"
            "    subprocess.run(command)\n"
            "execute(['docker', 'compose', 'exec', '-T', 'gateway', 'python', '-V'])\n"
        ),
        (
            "import subprocess\n"
            "def execute(command):\n"
            "    subprocess.run(command)\n"
            "command = ['docker', 'compose', 'exec', '-T', 'gateway', 'python', '-V']\n"
            "command[-1] = input()\n"
            "execute(command)\n"
        ),
        (
            "import subprocess\n"
            "subprocess.run(['docker', 'compose', 'exec', '--env-file', '/tmp/evil.env', "
            "'gateway', 'python', '-V'])\n"
        ),
        (
            "import subprocess\n"
            "subprocess.run(['docker', 'compose', 'exec', 'gateway', 'docker', 'pull', "
            "'alpine:latest'])\n"
        ),
    ],
    ids=["wrapper", "mutated-argv", "external-env-file", "inner-image-command"],
)
def test_gate_boundary_python_rejects_compose_exec_bypasses(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    """Only the direct, literal operational Compose exec command is exempt."""
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        "def run(daemon):\n    daemon.containers.run('alpine:latest')\n",
        (
            "def launch(daemon):\n    daemon.containers.run('alpine:latest')\n\n"
            "def run(client):\n    launch(client)\n"
        ),
        "def run(client):\n    vars(client)['containers'].run('alpine:latest')\n",
        "import builtins\ndef run(client):\n    builtins.vars(client)['containers'].run('alpine:latest')\n",
        "def run(client):\n    client.__dict__['containers'].run('alpine:latest')\n",
        ("def run(client):\n    client.__getattribute__('containers').run('alpine:latest')\n"),
        (
            "def launch(containers):\n    containers.run('alpine:latest')\n\n"
            "def run(client):\n    launch(client.containers)\n"
        ),
    ],
    ids=[
        "arbitrary-receiver",
        "client-wrapper",
        "vars",
        "builtins-vars",
        "dunder-dict",
        "dunder-getattribute",
        "namespace-wrapper",
    ],
)
def test_gate_boundary_python_rejects_every_obscured_containers_run_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(body)

    assert any(
        "Docker SDK" in error or "alpine:latest" in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_python_accepts_direct_asyncio_run_for_any_receiver_name(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(
        "import asyncio\n\n"
        f"async def run(daemon):\n    await asyncio.to_thread(daemon.containers.run, '{REFERENCE}')\n"
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("services/new-worker/runtime.py")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess as sp\nsp.run(['docker', 'pull', 'alpine:latest'])\n",
        "from subprocess import run\nrun(['docker', 'pull', 'alpine:latest'])\n",
        "import os as operating_system\noperating_system.system(':; docker pull alpine:latest')\n",
        "from os import system\nsystem(':; docker pull alpine:latest')\n",
        "import subprocess\nsubprocess.run(['env', 'docker', 'pull', 'alpine:latest'])\n",
    ],
    ids=[
        "subprocess-module-alias",
        "subprocess-function-import",
        "os-module-alias-sequence",
        "os-function-import-sequence",
        "env-wrapper",
    ],
)
def test_gate_boundary_python_rejects_aliased_or_wrapped_docker_cli_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("unsupported Docker CLI execution" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        "COMPOSE_FILE=/tmp/evil.yml docker compose up",
        "COMPOSE_FILE=/tmp/evil.yml\ndocker compose up",
        "export COMPOSE_FILE=/tmp/evil.yml\ndocker compose up",
        "docker-compose -f /tmp/evil.yml up",
        "shopt -s expand_aliases\nalias dc='docker compose'\ndc -f /tmp/evil.yml up",
        'DOCKER=docker\n"$DOCKER" compose -f /tmp/evil.yml up',
        'SCRIPT_DIR=/tmp\ndocker compose -f "$SCRIPT_DIR/evil.yml" up',
    ],
    ids=[
        "inline-compose-file",
        "prior-compose-file",
        "exported-compose-file",
        "legacy-binary",
        "compose-alias",
        "docker-variable",
        "untrusted-script-dir",
    ],
)
def test_gate_boundary_shell_rejects_uninventoried_compose_provenance(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any(
        "Compose" in error or "Docker executable alias" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize("field", ["context", "dockerfile"])
def test_gate_boundary_compose_rejects_variable_build_paths(
    checker: ModuleType | _MissingChecker, tmp_path: Path, field: str
) -> None:
    _write_valid_repo(tmp_path)
    build = {"context": ".", "dockerfile": "Dockerfile"}
    build[field] = "$UNTRUSTED_PATH"
    _write_yaml(
        tmp_path / "ops/compose.prod.yml",
        {"services": {"worker": {"image": REFERENCE, "build": build}}},
    )

    assert any(
        "Compose build" in error and ("variable" in error or "literal" in error)
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_dockerfile_rejects_escape_directives(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/Dockerfile").write_text(f"# escape=`\nFROM {REFERENCE}\n")

    assert any(
        "Dockerfile escape directive is unsupported" in error
        for error in _errors(checker, tmp_path)
    )


def test_ci_smoke_rejects_negation_continued_on_the_next_line(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_ci_job(
        tmp_path,
        [
            f"!\ndocker build -t {CI_SMOKE_IMAGE} .",
            f"docker run --pull=never --rm {CI_SMOKE_IMAGE} true",
        ],
    )

    assert any("smoke" in error and "built" in error for error in _errors(checker, tmp_path))


def test_shell_background_operator_does_not_control_the_next_line(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\ntrue &\nIMAGE="{REFERENCE}"\ndocker pull "$IMAGE"\n'
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (f"import docker\nclient = docker.APIClient()\nclient.pull('{TAG}')\n"),
        (f"import docker\nclient = docker.APIClient()\nclient.create_container(image='{TAG}')\n"),
        f"import docker\ndocker.APIClient().pull('{TAG}')\n",
        (f"import docker\ndaemon = docker.APIClient()\ndaemon.pull('{TAG}')\n"),
        f"import docker as d\nd.APIClient().pull('{TAG}')\n",
        f"from docker import APIClient as AC\nAC().pull('{TAG}')\n",
        (f"import docker\nfactory = docker.APIClient\nfactory().pull('{TAG}')\n"),
    ],
    ids=[
        "bound-pull",
        "bound-create",
        "inline-pull",
        "arbitrary-receiver",
        "module-alias",
        "constructor-import",
        "constructor-alias",
    ],
)
def test_gate_boundary_python_rejects_low_level_api_client_operations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("Docker SDK" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        (f"import subprocess\nsubprocess.run(args=['docker', 'pull', '{TAG}'])\n"),
        (f"import subprocess\nsubprocess.run(['sh', '-c', 'docker pull {TAG}'])\n"),
        (f"import subprocess\ngetattr(subprocess, 'run')(['docker', 'pull', '{TAG}'])\n"),
        (f"import subprocess\nsubprocess.run(['pull', '{TAG}'], executable='docker')\n"),
        ("import os\nos.system('docker compose -f /tmp/evil.yml up')\n"),
        (f"import subprocess\nsubprocess.getoutput('docker run --rm {TAG}')\n"),
        f"import os\nos.popen('docker pull {TAG}')\n",
        (
            "import asyncio\n"
            f"asyncio.run(asyncio.create_subprocess_exec('docker', 'pull', '{TAG}'))\n"
        ),
        (f"import asyncio\nasyncio.run(asyncio.create_subprocess_shell('docker pull {TAG}'))\n"),
        f"import os\nos.execvp('docker', ['docker', 'pull', '{TAG}'])\n",
        f"import os\nos.system('true; ' + 'docker pull {TAG}')\n",
        f"import subprocess\nsubprocess.run(['docker'] + ['pull', '{TAG}'])\n",
        f"import os\nos.system('DOCKER=docker; $DOCKER pull {TAG}')\n",
    ],
    ids=[
        "args-keyword",
        "shell-command",
        "getattr-callable",
        "executable",
        "os-compose",
        "subprocess-getoutput",
        "os-popen",
        "asyncio-exec",
        "asyncio-shell",
        "os-execvp",
        "string-concatenation",
        "list-concatenation",
        "shell-variable",
    ],
)
def test_gate_boundary_python_rejects_indirect_docker_cli_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("Docker CLI" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_python_rejects_control_flow_dependent_docker_cli_alias(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "if flag:\n"
        "    invoke = subprocess.run\n"
        "else:\n"
        "    invoke = print\n"
        f"invoke(['docker', 'pull', '{TAG}'])\n"
    )

    assert any("Docker CLI" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_python_rejects_dynamic_python_execution_of_docker(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"""exec("import os; os.system('docker pull {TAG}')")\n"""
    )

    assert any("Docker" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        f"import docker.api.client\ndocker.api.client.APIClient().pull('{TAG}')\n",
        f"from docker.api.client import APIClient\nAPIClient().pull('{TAG}')\n",
        f"import docker.api.client as dac\ndac.APIClient().pull('{TAG}')\n",
        f"import docker\ngetattr(docker, 'APIClient')().pull('{TAG}')\n",
        f"import docker\ndocker.__dict__['APIClient']().pull('{TAG}')\n",
        f"def run(client, namespace):\n    getattr(client, namespace).run('{TAG}')\n",
        (
            "def run(client):\n"
            "    namespace = 'containers'\n"
            f"    getattr(client, namespace).run('{TAG}')\n"
        ),
        (f"def run(client, namespace):\n    client.__getattribute__(namespace).run('{TAG}')\n"),
    ],
    ids=[
        "qualified-import",
        "direct-qualified-import",
        "qualified-import-alias",
        "getattr-constructor",
        "module-dict-constructor",
        "dynamic-getattr-namespace",
        "bound-getattr-namespace",
        "dunder-getattribute-namespace",
    ],
)
def test_gate_boundary_python_rejects_obscured_docker_sdk_paths(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any("Docker SDK" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("owner", "location"),
    [
        ("job_hook", "root.job_hook.hooks.pre_get_sources_script"),
        ("default", "root.default.hooks.pre_get_sources_script"),
    ],
)
def test_gate_boundary_ci_scans_pre_get_sources_hooks(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    owner: str,
    location: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text(
        f"{owner}:\n  hooks:\n    pre_get_sources_script:\n      - docker pull {TAG}\n"
    )

    assert any(location in error and TAG in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "bash ci/launch.sh",
        "source ci/launch.sh",
        ". ci/launch.sh",
        "./ci/launch.sh",
        "./ci/launch",
    ],
    ids=["bash", "source", "dot", "direct", "direct-no-suffix"],
)
def test_gate_boundary_ci_rejects_unscanned_shell_dependencies(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(tmp_path / ".gitlab-ci.yml", {"job": {"script": [command]}})
    launch = tmp_path / "ci/launch.sh"
    launch.parent.mkdir()
    launch.write_text(f"docker pull {TAG}\n")
    (tmp_path / "ci/launch").write_text(f"#!/usr/bin/env bash\ndocker pull {TAG}\n")

    assert any("CI shell dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_scans_inline_python_for_docker_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text(
        "job:\n"
        "  script:\n"
        f"    - python -c \"import docker; docker.from_env().containers.run('{TAG}')\"\n"
    )

    assert any(TAG in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_rejects_unscanned_python_dependencies(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / ".gitlab-ci.yml").write_text("job:\n  script:\n    - python ci/launch.py\n")
    launch = tmp_path / "ci/launch.py"
    launch.parent.mkdir()
    launch.write_text(f"import os\nos.system('docker pull {TAG}')\n")

    assert any("CI Python dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_accepts_local_python_dependencies_already_scanned(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml", {"job": {"script": ["python scripts/release_worker.py"]}}
    )
    (tmp_path / "scripts/release_worker.py").write_text("print('static local tool')\n")
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].remove(".gitlab-ci.yml")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'source ci/launch.sh'",
        "bash -c './ci/launch.sh'",
        "sh -c 'exec ./ci/launch.sh'",
        "sh -c 'python ci/launch.py'",
        "uv run python ci/launch.py",
    ],
    ids=[
        "bash-source",
        "bash-direct",
        "sh-exec",
        "sh-python",
        "uv-python",
    ],
)
def test_gate_boundary_ci_rejects_indirect_script_dependencies(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(tmp_path / ".gitlab-ci.yml", {"job": {"script": [command]}})
    launch_shell = tmp_path / "ci/launch.sh"
    launch_shell.parent.mkdir()
    launch_shell.write_text(f"docker pull {TAG}\n")
    (tmp_path / "ci/launch.py").write_text(f"import os\nos.system('docker pull {TAG}')\n")

    assert any("CI" in error and "dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_rejects_dynamic_executable_dependencies(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"script": ["RUNNER=./ci/launch.sh", "$RUNNER"]}},
    )
    launch = tmp_path / "ci/launch.sh"
    launch.parent.mkdir()
    launch.write_text(f"docker pull {TAG}\n")

    assert any("CI" in error and "executable" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        (f"python - <<'PY'\nimport docker\ndocker.from_env().containers.run('{TAG}')\nPY"),
        (f"printf '%s' \"import docker; docker.from_env().containers.run('{TAG}')\" | python"),
        (f'bash -c \'python -c "import docker; docker.from_env().containers.run(\\"{TAG}\\")"\''),
        (f"uv run python -c \"import docker; docker.from_env().containers.run('{TAG}')\""),
        (f'python -c "exec(\\"import os; os.system(\'docker pull {TAG}\')\\")"'),
    ],
    ids=["heredoc", "stdin-pipe", "nested-shell", "uv-run", "dynamic-exec"],
)
def test_gate_boundary_ci_rejects_unmodelled_python_execution_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(tmp_path / ".gitlab-ci.yml", {"job": {"script": [script]}})

    assert any(
        "Python" in error or "Docker" in error or TAG in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_rejects_mutated_trusted_script_dir(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        "SCRIPT_DIR=/tmp\n"
        'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
    )
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    consumers = lock["images"]["python-3-12-slim"]["consumers"]
    consumers.remove("scripts/build-image.sh")
    consumers.append("scripts/internal.yml")
    _write_yaml(lock_path, lock)

    assert any("Compose file" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        (f'docker compose -f "$SCRIPT_DIR/internal.yml" up\n{SCRIPT_DIR_ASSIGNMENT}\n'),
        (
            "if true; then\n"
            f"  {SCRIPT_DIR_ASSIGNMENT}\n"
            "fi\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            "unset SCRIPT_DIR\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            "typeset SCRIPT_DIR=/tmp\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            "declare -g SCRIPT_DIR=/tmp\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            "SCRIPT_DIR+=/../../../../tmp\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
        (
            "dirname() { printf /tmp; }\n"
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
        ),
    ],
    ids=[
        "use-before",
        "conditional",
        "unset",
        "duplicate",
        "typeset-mutation",
        "declare-global-mutation",
        "augmented-assignment",
        "dirname-function-shadow",
    ],
)
def test_gate_boundary_shell_rejects_untrusted_script_dir_lifecycle(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}")
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )

    assert any("Compose file" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        "cd /tmp\ndocker compose -f docker-compose.yml up",
        "bash -c 'docker compose -f /tmp/evil.yml up'",
        "setsid docker-compose -f /tmp/evil.yml up",
        "shopt -s expand_aliases\nalias dc='env docker compose'\ndc -f /tmp/evil.yml up",
    ],
    ids=["changed-cwd", "nested-shell", "setsid-wrapper", "env-alias-wrapper"],
)
def test_gate_boundary_shell_rejects_indirect_compose_provenance(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any(
        "Compose" in error
        or "Docker executable alias" in error
        or "Docker command" in error
        or "indirect shell execution" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "script",
    [
        "bash -c 'source /tmp/evil.sh'",
        "sh -c '/tmp/evil.sh'",
        "eval 'source /tmp/evil.sh'",
        "command bash -c 'source /tmp/evil.sh'",
        "env bash -c 'source /tmp/evil.sh'",
        "nice bash -c 'source /tmp/evil.sh'",
        "SHELL=/bin/bash; $SHELL -c 'source /tmp/evil.sh'",
        "VALUE=\"$(bash -c 'source /tmp/evil.sh')\"",
        "VALUE=\"$(sh -c '/tmp/evil.sh')\"",
        "VALUE=\"$(eval 'source /tmp/evil.sh')\"",
    ],
    ids=[
        "bash-c-source",
        "sh-c-direct",
        "eval-source",
        "command-wrapper",
        "env-wrapper",
        "nice-wrapper",
        "bound-interpreter",
        "substitution-bash",
        "substitution-sh",
        "substitution-eval",
    ],
)
def test_gate_boundary_shell_rejects_indirect_interpreter_dependencies(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\n{script}\ndocker pull {REFERENCE}\n"
    )

    assert any(
        "shell dependency" in error or "indirect shell execution" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "if true; then FILE=/tmp/evil.yml; fi",
        "printf -v FILE %s /tmp/evil.yml",
    ],
    ids=["controlled-assignment", "printf-v"],
)
def test_gate_boundary_shell_rejects_mutated_compose_alias(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{SCRIPT_DIR_ASSIGNMENT}\n"
        'FILE="$SCRIPT_DIR/internal.yml"\n'
        f"{mutation}\n"
        'docker compose -f "$FILE" up\n'
    )
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )

    assert any("Compose file" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "docker compose -f docker-compose.yml up",
        "docker build -f Dockerfile .",
    ],
    ids=["compose", "build"],
)
def test_gate_boundary_ci_rejects_repository_paths_after_cwd_change(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"script": ["cd /tmp", command]}},
    )

    assert any(
        "working directory" in error or "CWD" in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_ignores_escaped_command_substitution_text(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\necho "\\$(source /tmp/evil.sh)"\ndocker pull {REFERENCE}\n'
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_rejects_cwd_ambiguous_docker_build(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        "cd /tmp\n"
        "docker build -t worker:local -f Dockerfile .\n"
        f"docker pull {REFERENCE}\n"
    )

    assert any("docker build" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        f"def run(daemon):\n    getattr(daemon, 'containers').run('{TAG}')\n",
        (
            "import builtins\n"
            f"def run(daemon):\n    builtins.getattr(daemon, 'containers').run('{TAG}')\n"
        ),
        f"def run(daemon):\n    getattr(daemon, 'images').pull('{TAG}')\n",
        f"def run(client):\n    client.services.create('{TAG}')\n",
    ],
    ids=["containers", "builtins-containers", "images", "services-create"],
)
def test_gate_boundary_python_rejects_dynamic_sdk_namespaces_on_any_receiver(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "services/new-worker/runtime.py").write_text(body)

    assert any("Docker SDK" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_rejects_cwd_ambiguous_source_dependency(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text("#!/usr/bin/env bash\nsource payload.inc\n")
    (tmp_path / "scripts/payload.inc").write_text(f"docker pull {REFERENCE}\n")
    (tmp_path / "payload.inc").write_text(f"docker pull {TAG}\n")
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    consumers = lock["images"]["python-3-12-slim"]["consumers"]
    consumers.remove("scripts/build-image.sh")
    consumers.append("scripts/payload.inc")
    _write_yaml(lock_path, lock)

    assert any("shell dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_rejects_cwd_ambiguous_direct_dependency(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text("#!/usr/bin/env bash\n./payload.sh\n")
    (tmp_path / "scripts/payload.sh").write_text(f"#!/usr/bin/env bash\ndocker pull {TAG}\n")

    assert any("shell dependency" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "substitution",
    [
        "source /tmp/evil.sh",
        ". /tmp/evil.sh",
        "bash /tmp/evil.sh",
        "env -i bash /tmp/evil.sh",
        "bash scripts/payload.inc",
    ],
    ids=["source", "dot", "bash", "env-bash", "internal-unanchored"],
)
def test_gate_boundary_shell_rejects_unverifiable_substitution_dependencies(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    substitution: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/payload.inc").write_text(f"docker pull {TAG}\n")
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\nVALUE="$({substitution})"\ndocker pull {REFERENCE}\n'
    )

    assert any("shell dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_rejects_backtick_dependency_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f"#!/usr/bin/env bash\nVALUE=`source /tmp/evil.sh`\ndocker pull {REFERENCE}\n"
    )

    assert any("shell dependency" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_shell_allows_exact_os_release_metadata_dependency(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        "#!/usr/bin/env bash\n"
        'codename="$(. /etc/os-release && echo "${VERSION_CODENAME}")"\n'
        f"docker pull {REFERENCE}\n"
    )

    assert _errors(checker, tmp_path) == []


def test_missing_lock_still_reports_discovered_floating_consumers(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n  db:\n    image: pgvector/pgvector:pg16\n"
    )
    (tmp_path / "config/container-images.lock.yml").unlink()

    errors = _errors(checker, tmp_path)

    assert any("Dockerfile" in error and "python:3.12-slim" in error for error in errors)
    assert any(
        "docker-compose.yml" in error and "pgvector/pgvector:pg16" in error for error in errors
    )


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        (
            "import subprocess\n(invoke,) = (subprocess.run,)\n"
            "invoke(['docker', 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
            "invoke(['docker', 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "import subprocess\ninvoke = lambda cmd: subprocess.run(cmd)\n"
            "invoke(['docker', 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "import subprocess\nsubprocess.run([*['docker'], 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "import subprocess\nsubprocess.run('docker pull alpine:latest'.split())\n",
            "Docker CLI",
        ),
        (
            "import subprocess\nfrom pathlib import Path\n"
            "subprocess.run([Path('/usr/bin/docker'), 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "import subprocess\nsubprocess.run([f\"{'docker'}\", 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
        (
            "from builtins import exec as execute\n"
            "execute(\"import os; os.system('docker pull alpine:latest')\")\n",
            "dynamic Python execution",
        ),
        (
            "__builtins__['exec'](\"import os; os.system('docker pull alpine:latest')\")\n",
            "dynamic Python execution",
        ),
        (
            "import docker\ndocker.api.APIClient().pull('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "def run(client, namespace):\n    vars(client)[namespace].run('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "def run(client, namespace):\n    client.__dict__[namespace].run('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "from docker import *\nAPIClient().pull('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "import docker\nif flag:\n    factory = docker.APIClient\n"
            "else:\n    factory = print\nfactory().pull('alpine:latest')\n",
            "Docker SDK",
        ),
    ],
    ids=[
        "tuple-callable-alias",
        "function-wrapper",
        "lambda-wrapper",
        "starred-static-list",
        "literal-split",
        "pathlike-executable",
        "constant-fstring",
        "builtins-exec-import",
        "dunder-builtins-exec",
        "docker-api-apiclient",
        "dynamic-vars-namespace",
        "dynamic-dunder-dict-namespace",
        "docker-star-import",
        "control-flow-constructor-alias",
    ],
)
def test_gate_boundary_python_rejects_remaining_fail_closed_forms(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    marker: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any(marker in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_preserves_inline_python_diagnostic_provenance(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    command = f"python -c \"import docker; docker.from_env().containers.run('{TAG}')\""
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job_a": {"image": REFERENCE, "script": [command]},
            "job_b": {"image": REFERENCE, "script": [command]},
        },
    )

    errors = [error for error in _errors(checker, tmp_path) if "is not pinned" in error]

    assert len(errors) == 2
    assert any("root.job_a.script[0]:1.python-c:1" in error for error in errors)
    assert any("root.job_b.script[0]:1.python-c:1" in error for error in errors)


def test_gate_boundary_shell_scans_shell_shebang_with_arbitrary_suffix(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(
        "#!/usr/bin/env bash\ndocker pull alpine:latest\n"
    )

    assert any(
        "release.command" in error and "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "script",
    [
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            'for SCRIPT_DIR in /tmp; do source "$SCRIPT_DIR/evil.sh"; done\n'
            f"docker pull {REFERENCE}\n"
        ),
        (
            f"{SCRIPT_DIR_ASSIGNMENT}\n"
            'source "$SCRIPT_DIR/mutate.inc"\n'
            'source "$SCRIPT_DIR/evil.sh"\n'
            f"docker pull {REFERENCE}\n"
        ),
    ],
    ids=["loop-mutation", "sourced-mutation"],
)
def test_gate_boundary_shell_rejects_transitive_script_dir_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(f"#!/usr/bin/env bash\n{script}")
    (tmp_path / "scripts/mutate.inc").write_text("SCRIPT_DIR=/tmp\n")
    (tmp_path / "scripts/evil.sh").write_text("#!/usr/bin/env bash\ntrue\n")

    assert any(
        "SCRIPT_DIR" in error or "shell dependency" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "script",
    [
        "bash</tmp/payload",
        "bash < /tmp/payload",
        "bash<<<'docker pull alpine:latest'",
        "busybox sh -c 'docker pull alpine:latest'",
        "busybox ash -c 'docker pull alpine:latest'",
        "stdbuf -oL bash /tmp/payload",
    ],
    ids=[
        "attached-stdin",
        "separate-stdin",
        "attached-here-string",
        "busybox-shell",
        "busybox-ash",
        "stdbuf-shell",
    ],
)
def test_gate_boundary_shell_rejects_unmodelled_interpreter_inputs(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert any(
        "dependency" in error
        or "redirection" in error
        or "indirect shell" in error
        or "alpine:latest" in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_allows_escaped_docker_substitution_text(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/build-image.sh").write_text(
        f'#!/usr/bin/env bash\necho "\\$(docker pull {TAG})"\ndocker pull {REFERENCE}\n'
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        f"PY=python; $PY -c \"import docker; docker.from_env().containers.run('{TAG}')\"",
        f"python -c\"import docker; docker.from_env().containers.run('{TAG}')\"",
        (
            'uv run --python 3.12 python -c "import docker; '
            f"docker.from_env().containers.run('{TAG}')\""
        ),
        "python < /tmp/ci_payload",
        "python /dev/stdin < /tmp/ci_payload",
        "python -- /tmp/ci_payload",
        "python ci/launch",
    ],
    ids=[
        "python-alias",
        "attached-c",
        "uv-option-value",
        "stdin-redirection",
        "dev-stdin-redirection",
        "option-terminated-script",
        "extensionless-script",
    ],
)
def test_gate_boundary_ci_rejects_unmodelled_python_entrypoints(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"image": REFERENCE, "script": [command]}},
    )
    launch = tmp_path / "ci/launch"
    launch.parent.mkdir()
    launch.write_text(f"import docker\ndocker.from_env().containers.run('{TAG}')\n")

    assert any(
        "Python" in error or "Docker" in error or TAG in error
        for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_ci_tracks_cwd_from_before_script_into_script(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": ["cd /tmp"],
                "script": ["docker build -f Dockerfile ."],
            }
        },
    )

    assert any(
        "working directory" in error or "CWD" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "builtin cd /tmp",
        "eval 'cd /tmp'",
        "shopt -s expand_aliases\nalias c='cd /tmp'\nc",
    ],
    ids=["builtin", "eval", "alias"],
)
def test_gate_boundary_ci_rejects_indirect_cwd_mutation_before_build(
    checker: ModuleType | _MissingChecker, tmp_path: Path, prefix: str
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "script": [prefix, "docker build -t worker:local -f Dockerfile ."],
            }
        },
    )

    assert any("working directory mutation" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_keeps_after_script_shell_state_separate(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": ["cd /tmp"],
                "script": ["true"],
                "after_script": ["docker build -t worker:local -f Dockerfile ."],
            }
        },
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_ci_forbids_smoke_lifecycle_in_before_script(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "build:docker": {
                "image": REFERENCE,
                "before_script": [
                    f"docker build -t {CI_SMOKE_IMAGE} -f Dockerfile .",
                    CI_SMOKE_RUN,
                ],
                "script": ["true"],
            }
        },
    )

    assert any("CI smoke image" in error for error in _errors(checker, tmp_path))


def test_gate_boundary_ci_resolves_python_alias_across_before_script(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": ["PY=python"],
                "script": [f"$PY -c \"import docker; docker.from_env().containers.run('{TAG}')\""],
            }
        },
    )

    assert any(TAG in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    ("body", "marker"),
    [
        (
            "import subprocess\n"
            "cmd = ['docker', 'pull', 'alpine:latest']\n"
            "if flag:\n    cmd = ['git', 'status']\n"
            "subprocess.run(cmd)\n",
            "Docker CLI",
        ),
        (
            "if flag:\n    invoke = exec\nelse:\n    invoke = print\n"
            "invoke(\"import os; os.system('docker pull alpine:latest')\")\n",
            "dynamic Python execution",
        ),
        (
            "def run(daemon, namespace):\n    vars(daemon)[namespace].run('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "from builtins import getattr as ga\n"
            "def run(daemon, namespace):\n"
            "    ga(daemon, namespace).run('alpine:latest')\n",
            "Docker SDK",
        ),
        (
            "import subprocess\n"
            "def invoke(label, cmd):\n    subprocess.run(cmd)\n"
            "invoke('release', ['docker', 'pull', 'alpine:latest'])\n",
            "Docker CLI",
        ),
    ],
    ids=[
        "branch-masked-command",
        "branch-masked-exec",
        "arbitrary-vars-receiver",
        "aliased-getattr",
        "wrapper-nonfirst-payload",
    ],
)
def test_gate_boundary_python_rejects_control_flow_and_wrapper_bypasses(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    marker: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert any(marker in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "inherited_before_script",
    [
        {"default": {"before_script": ["cd /tmp"]}},
        {"before_script": ["cd /tmp"]},
    ],
    ids=["default", "legacy-root"],
)
def test_gate_boundary_ci_applies_inherited_before_script_before_job_script(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    inherited_before_script: dict[str, object],
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            **inherited_before_script,
            "job": {
                "image": REFERENCE,
                "script": ["docker build -t worker:local -f Dockerfile ."],
            },
        },
    )

    assert any(
        "working directory" in error or "CWD" in error for error in _errors(checker, tmp_path)
    )


@pytest.mark.parametrize(
    ("before_script", "command"),
    [
        (
            ["shopt -s expand_aliases", "alias py=python"],
            f"py -c \"import docker; docker.from_env().containers.run('{TAG}')\"",
        ),
        (
            ["hash -p /usr/bin/python p"],
            f"p -c \"import docker; docker.from_env().containers.run('{TAG}')\"",
        ),
        (
            ["shopt -s expand_aliases", "alias uvpy='uv run python'"],
            f"uvpy -c \"import docker; docker.from_env().containers.run('{TAG}')\"",
        ),
    ],
    ids=["alias-python", "hash-python", "alias-uv-python"],
)
def test_gate_boundary_ci_rejects_python_execution_through_shell_aliases(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    before_script: list[str],
    command: str,
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": before_script,
                "script": [command],
            }
        },
    )

    assert any("root.job" in error for error in _errors(checker, tmp_path))


@pytest.mark.parametrize(
    "script",
    [
        f"printf '%s\\n' 'docker pull {TAG}' | bash",
        f"printf '%s\\n' 'docker pull {TAG}' | busybox sh",
        f"printf '%s\\n' 'docker pull {TAG}' | stdbuf -oL bash",
        "exec </tmp/payload\nbash",
    ],
    ids=["pipe-bash", "pipe-busybox", "pipe-stdbuf", "inherited-stdin"],
)
def test_gate_boundary_shell_rejects_unmodelled_stdin_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("mutation", "runtime_directory"),
    [
        ("let SCRIPT_DIR=0", "0"),
        ("mapfile -t SCRIPT_DIR <<< '0'", "0"),
        ("set -- -x; getopts x SCRIPT_DIR", "x"),
    ],
    ids=["let", "mapfile", "getopts"],
)
def test_gate_boundary_shell_rejects_additional_script_dir_mutations(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    mutation: str,
    runtime_directory: str,
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    _write_yaml(
        tmp_path / runtime_directory / "internal.yml",
        {"services": {"worker": {"image": "alpine:latest"}}},
    )
    (tmp_path / "scripts/runner.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{SCRIPT_DIR_ASSIGNMENT}\n"
        f"{mutation}\n"
        'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/internal.yml")
    _write_yaml(lock_path, lock)

    assert any(
        "SCRIPT_DIR" in error or "Compose file" in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_scans_busybox_shebang_with_arbitrary_suffix(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(f"#!/bin/busybox sh\ndocker pull {TAG}\n")

    assert any(
        "scripts/release.command" in error and TAG in error for error in _errors(checker, tmp_path)
    )


def test_gate_boundary_shell_allows_safe_sourced_state_before_compose(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/env.inc").write_text("SAFE=value\n")
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    (tmp_path / "scripts/runner.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{SCRIPT_DIR_ASSIGNMENT}\n"
        'source "$SCRIPT_DIR/env.inc"\n'
        'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/internal.yml")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "cmd = ['docker', 'pull', 'alpine:latest'] if flag else ['git', 'status']\n"
            "subprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = []\n"
            "cmd += ['docker', 'pull', 'alpine:latest']\nsubprocess.run(cmd)\n"
        ),
        ("import subprocess\nsubprocess.run(*[['docker', 'pull', 'alpine:latest']])\n"),
        (
            "import subprocess\n"
            "def outer(label, cmd):\n    inner(cmd)\n"
            "def inner(cmd):\n    subprocess.run(cmd)\n"
            "outer('release', ['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\n"
            "def invoke(cmd=['docker', 'pull', 'alpine:latest']):\n"
            "    subprocess.run(cmd)\n"
            "invoke()\n"
        ),
        (
            "if flag:\n    inspect = getattr\nelse:\n    inspect = print\n"
            "inspect(daemon, namespace).run('alpine:latest')\n"
        ),
    ],
    ids=[
        "if-expression-payload",
        "augmented-assignment-payload",
        "starred-call-payload",
        "forward-wrapper",
        "wrapper-docker-default",
        "branch-reflection-alias",
    ],
)
def test_gate_boundary_python_rejects_additional_dataflow_bypasses(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['docker', 'pull', 'unused']\n"
            "def benign():\n"
            "    cmd = ['git', 'status']\n"
            "    subprocess.run(cmd)\n"
        ),
        (
            "import subprocess\n"
            "def invoke(label, cmd):\n    subprocess.run(cmd)\n"
            "invoke('docker pull diagnostic', ['git', 'status'])\n"
        ),
    ],
    ids=["local-shadow", "non-payload-label"],
)
def test_gate_boundary_python_allows_benign_scoped_and_wrapper_arguments(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Runner:\n"
            "    @staticmethod\n    def invoke(cmd):\n        subprocess.run(cmd)\n"
            "Runner.invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Runner:\n"
            "    def invoke(self, cmd):\n        subprocess.run(cmd)\n"
            "Runner().invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\n"
            "invoke = lambda cmd=['docker', 'pull', 'alpine:latest']: subprocess.run(cmd)\n"
            "invoke()\n"
        ),
        (
            "import functools\nimport subprocess\n"
            "invoke = functools.partial(subprocess.run, ['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n"
        ),
        (
            "import subprocess\ndef invoke(*cmd):\n    subprocess.run(cmd)\n"
            "invoke('docker', 'pull', 'alpine:latest')\n"
        ),
        (
            "import subprocess\ndef invoke(**options):\n    subprocess.run(options['cmd'])\n"
            "invoke(cmd=['docker', 'pull', 'alpine:latest'])\n"
        ),
        ("import subprocess\nsubprocess.run(**{'args': ['docker', 'pull', 'alpine:latest']})\n"),
        (
            "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
            "invoke(**{'cmd': ['docker', 'pull', 'alpine:latest']})\n"
        ),
        (
            "import subprocess\n"
            "cmd = (flag and ['docker', 'pull', 'alpine:latest']) or ['git', 'status']\n"
            "subprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = []\n"
            "cmd.extend(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'pull', 'alpine:latest']\n"
            "cmd[0] = 'docker'\nsubprocess.run(cmd)\n"
        ),
        "def run(daemon, namespace):\n    vars(daemon).get(namespace).run('alpine:latest')\n",
        (
            "def run(daemon, namespace):\n"
            "    inspect = daemon.__getattribute__\n"
            "    inspect(namespace).run('alpine:latest')\n"
        ),
    ],
    ids=[
        "static-method-wrapper",
        "instance-method-wrapper",
        "lambda-default",
        "functools-partial",
        "varargs-wrapper",
        "kwargs-wrapper",
        "direct-expanded-kwargs",
        "wrapper-expanded-kwargs",
        "boolean-payload",
        "container-extend",
        "subscript-assignment",
        "vars-get-reflection",
        "aliased-dunder-getattribute",
    ],
)
def test_gate_boundary_python_rejects_final_callable_and_mutation_bypasses(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_maps_only_executable_wrapper_sequence_component(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def invoke(cmd, label):\n    subprocess.run([cmd, label])\n"
        "invoke('git', 'docker pull diagnostic')\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "aliases",
    [
        ["shopt -s expand_aliases", "alias p=python", "alias q=p"],
        ["shopt -s expand_aliases", "alias p='uv run python'", "alias q=p"],
    ],
    ids=["recursive-python", "recursive-uv-python"],
)
def test_gate_boundary_ci_resolves_recursive_python_aliases(
    checker: ModuleType | _MissingChecker, tmp_path: Path, aliases: list[str]
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": aliases,
                "script": [f"q -c \"import docker; docker.from_env().containers.run('{TAG}')\""],
            }
        },
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "script",
    [
        f"bash -c \"echo 'docker pull {TAG}' | bash\"",
        "bash -c 'exec </tmp/payload; bash'",
        "eval 'exec </tmp/payload'; bash",
        "RUN=exec; $RUN </tmp/payload; bash",
        "shopt -s expand_aliases; alias replace=exec; replace </tmp/payload; bash",
    ],
    ids=["nested-pipe", "nested-exec", "eval-exec", "variable-exec", "alias-exec"],
)
def test_gate_boundary_shell_rejects_nested_parent_stdin_mutations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert _errors(checker, tmp_path)


def test_gate_boundary_shell_rejects_sourced_parent_stdin_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/redirect.inc").write_text("exec </tmp/payload\n")
    (tmp_path / "scripts/runner.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/redirect.inc"\nbash\n'
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("mutation", "runtime_directory"),
    [
        ("SCRIPT_DIR[0]=0", "0"),
        ("printf -v 'SCRIPT_DIR[0]' 0", "0"),
        ("let 'SCRIPT_DIR[0]=0'", "0"),
        ("declare -n REF=SCRIPT_DIR; REF=0", "0"),
    ],
    ids=["indexed-assignment", "printf-index", "let-index", "nameref"],
)
def test_gate_boundary_shell_rejects_indexed_and_nameref_script_dir_mutations(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    mutation: str,
    runtime_directory: str,
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    _write_yaml(
        tmp_path / runtime_directory / "internal.yml",
        {"services": {"worker": {"image": "alpine:latest"}}},
    )
    (tmp_path / "scripts/runner.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{SCRIPT_DIR_ASSIGNMENT}\n"
        f"{mutation}\n"
        'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/internal.yml")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path)


def test_gate_boundary_shell_rejects_sourced_nameref_script_dir_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/env.inc").write_text("declare -n REF=SCRIPT_DIR\nREF=0\n")
    _write_yaml(
        tmp_path / "scripts/internal.yml",
        {"services": {"worker": {"image": REFERENCE}}},
    )
    (tmp_path / "scripts/runner.sh").write_text(
        "#!/usr/bin/env bash\n"
        f"{SCRIPT_DIR_ASSIGNMENT}\n"
        'source "$SCRIPT_DIR/env.inc"\n'
        'docker compose -f "$SCRIPT_DIR/internal.yml" up\n'
    )
    lock_path = tmp_path / "config/container-images.lock.yml"
    lock = yaml.safe_load(lock_path.read_text())
    lock["images"]["python-3-12-slim"]["consumers"].append("scripts/internal.yml")
    _write_yaml(lock_path, lock)

    assert _errors(checker, tmp_path)


def test_gate_boundary_ci_honours_inherit_default_false_for_before_script(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "default": {"before_script": ["cd /tmp"]},
            "job": {
                "image": REFERENCE,
                "inherit": {"default": False},
                "script": ["docker build -t worker:local -f Dockerfile ."],
            },
        },
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_allows_exec_redirection_on_non_stdin_fd(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(
        "#!/usr/bin/env bash\nexec 3</tmp/payload\nbash -c true\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/usr/bin/env -S busybox sh",
        "#!/usr/local/bin/bash",
        "#!/bin/busybox hush",
    ],
    ids=["env-busybox", "usr-local-bash", "busybox-hush"],
)
def test_gate_boundary_shell_scans_extended_shell_shebangs(
    checker: ModuleType | _MissingChecker, tmp_path: Path, shebang: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(f"{shebang}\ndocker pull {TAG}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Runner:\n"
            "    def invoke(self, cmd):\n        subprocess.run(cmd)\n"
            "runner = Runner()\n"
            "runner.invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import functools\nimport subprocess\n"
            "invoke = functools.partial(subprocess.run)\n"
            "invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ncmd = []\nalias = cmd\n"
            "alias.extend(['docker', 'pull', 'alpine:latest'])\n"
            "subprocess.run(cmd)\n"
        ),
        "def run(daemon, namespace):\n    daemon.__dict__.get(namespace).run('alpine')\n",
    ],
    ids=["bound-instance", "unbound-partial", "mutable-alias", "dunder-dict-get"],
)
def test_gate_boundary_python_rejects_adjacent_callable_and_reflection_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Runner:\n"
            "    @staticmethod\n    def invoke(cmd):\n        subprocess.run(cmd)\n"
            "Alias = Runner\n"
            "Alias.invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import builtins\nimport subprocess\nclass Runner:\n"
            "    @builtins.classmethod\n"
            "    def invoke(cls, cmd):\n        subprocess.run(cmd)\n"
            "Runner.invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Runner:\n"
            "    def __call__(self, cmd):\n        subprocess.run(cmd)\n"
            "Runner()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Runner:\n"
            "    def invoke(self, cmd):\n        subprocess.run(cmd)\n"
            "Maker = Runner\n"
            "Maker().invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ncmd = []\nmutate = cmd.extend\n"
            "mutate(['docker', 'pull', 'alpine:latest'])\n"
            "subprocess.run(cmd)\n"
        ),
        ("import subprocess\nsubprocess.run(cmd := ['docker', 'pull', 'alpine:latest'])\n"),
        ("import subprocess\nsubprocess.run([b'docker', b'pull', b'alpine:latest'])\n"),
    ],
    ids=[
        "class-alias",
        "qualified-classmethod",
        "callable-instance",
        "aliased-factory",
        "aliased-container-mutator",
        "direct-walrus",
        "bytes-command",
    ],
)
def test_gate_boundary_python_rejects_remaining_callable_and_payload_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "def invoke(**options):\n    subprocess.run(options['cmd'])\n"
            "invoke(cmd=['git', 'status'], label=['docker', 'pull', 'diagnostic'])\n"
        ),
        (
            "import subprocess\n"
            "def invoke(*parts):\n    subprocess.run(['git', *parts])\n"
            "invoke('docker', 'pull', 'diagnostic')\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'unused']\n"
            "cmd = ['git', 'status']\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'unused']\n"
            "cmd.clear()\ncmd.extend(['git', 'status'])\nsubprocess.run(cmd)\n"
        ),
    ],
    ids=[
        "kwargs-nonpayload-label",
        "varargs-under-git-executable",
        "definite-overwrite",
        "definite-clear-and-refill",
    ],
)
def test_gate_boundary_python_allows_definitely_benign_payload_state(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_rejects_transitive_sourced_nameref_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/env.inc").write_text(
        "declare -n FIRST=SCRIPT_DIR\ndeclare -n SECOND=FIRST\nSECOND=0\n"
    )
    (tmp_path / "scripts/runner.sh").write_text(
        f'#!/usr/bin/env bash\n{SCRIPT_DIR_ASSIGNMENT}\nsource "$SCRIPT_DIR/env.inc"\ntrue\n'
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['docker', 'pull', 'alpine:latest']\n"
            "flag and cmd.clear()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'alpine:latest']\n"
            "(cmd := ['git', 'status']) if flag else None\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'alpine:latest']\n"
            "unused = lambda: (cmd := ['git', 'status'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'alpine:latest']\n"
            "[cmd.clear() for _ in ()]\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef invoke(exe, command):\n"
            "    subprocess.run([exe, '-c', command])\n"
            "invoke('sh', 'docker pull alpine:latest')\n"
        ),
        (
            "import subprocess\ndef invoke(exe, *parts):\n"
            "    subprocess.run([exe, *parts])\n"
            "invoke('env', 'docker', 'pull', 'alpine:latest')\n"
        ),
        (
            "import subprocess\ndef invoke(*parts):\n"
            "    subprocess.run(['sh', *parts])\n"
            "invoke('-c', 'docker pull alpine:latest')\n"
        ),
        (
            "import asyncio\n"
            "asyncio.create_subprocess_exec('sh', '-c', 'docker pull alpine:latest')\n"
        ),
        (
            "import asyncio\n"
            "asyncio.create_subprocess_exec('env', 'docker', 'pull', 'alpine:latest')\n"
        ),
        ("import os\nos.execvp('env', ['env', 'docker', 'pull', 'alpine:latest'])\n"),
        ("import os\nos.execlp('env', 'env', 'docker', 'pull', 'alpine:latest')\n"),
        ("import os\nos.posix_spawnp('env', ['env', 'docker', 'pull', 'alpine:latest'], {})\n"),
        (
            "import subprocess\ndef factory():\n    return subprocess.run\n"
            "factory()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Base:\n"
            "    def __call__(self, cmd):\n        subprocess.run(cmd)\n"
            "class Runner(Base):\n    pass\n"
            "Runner()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef invoke(**options):\n"
            "    alias = options\n    subprocess.run(alias['cmd'])\n"
            "invoke(cmd=['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef invoke(**options):\n"
            "    alias = options\n    subprocess.run(alias.get('cmd'))\n"
            "invoke(cmd=['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\n"
            "danger = lambda: subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        "import os\nos.execvp('docker', ['alias', 'pull', 'alpine:latest'])\n",
        (
            "import asyncio\ndef factory():\n    return asyncio.create_subprocess_exec\n"
            "factory()('sh', '-c', 'docker pull alpine:latest')\n"
        ),
    ],
    ids=[
        "conditional-boolop-clear",
        "conditional-ifexp-walrus",
        "uninvoked-lambda-walrus",
        "empty-comprehension-clear",
        "wrapper-shell-command-parameter",
        "wrapper-env-varargs",
        "wrapper-fixed-shell-varargs",
        "asyncio-shell-command",
        "asyncio-env-argv",
        "os-execvp-argv",
        "os-execlp-argv",
        "os-posix-spawnp-argv",
        "returned-callable-factory",
        "inherited-callable",
        "kwargs-mapping-alias-subscript",
        "kwargs-mapping-alias-get",
        "lambda-hardcoded-command",
        "os-exec-path",
        "returned-exec-signature",
    ],
)
def test_gate_boundary_python_rejects_reviewed_dataflow_gaps(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = []\nalias = cmd\n"
            "cmd = ['docker', 'pull', 'diagnostic']\nsubprocess.run(alias)\n"
        ),
        (
            "import subprocess\ncmd = []\nmutate = cmd.extend\n"
            "cmd = ['git', 'status']\n"
            "mutate(['docker', 'pull', 'diagnostic'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'diagnostic']\n"
            "if (cmd := ['git', 'status']):\n    pass\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'pull', 'diagnostic']\n"
            "if not cmd.clear():\n    pass\n"
            "cmd.extend(['git', 'status'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef outer():\n"
            "    def inner():\n        return subprocess.run\n"
            "    return print\n"
            "outer()(['docker', 'pull', 'diagnostic'])\n"
        ),
    ],
    ids=[
        "alias-object-snapshot",
        "bound-mutator-object-snapshot",
        "definite-test-walrus",
        "definite-test-clear",
        "nested-return-is-not-factory",
    ],
)
def test_gate_boundary_python_allows_reviewed_definite_state(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def invoke():\n    subprocess.run(cmd)\n"
            "cmd = ['docker', 'pull', 'alpine:latest']\ninvoke()\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(_=(cmd := ['docker', 'pull', 'alpine:latest'])):\n    pass\n"
            "subprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef invoke(*, cmd):\n    subprocess.run(cmd)\n"
            "invoke(cmd=['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef invoke(**options):\n"
            "    command = options.pop('cmd')\n    subprocess.run(command)\n"
            "invoke(cmd=['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "box = {'cmd': cmd}\nalias = box['cmd']\n"
            "alias[0] = 'docker'\nsubprocess.run(cmd)\n"
        ),
        "import asyncio\nasyncio.create_subprocess_exec(program='docker')\n",
        (
            "import docker\nimport operator\nclient = docker.from_env()\n"
            "operator.attrgetter('containers.run')(client)('alpine:latest')\n"
        ),
        (
            "import docker\nfrom operator import attrgetter as pick\nclient = docker.from_env()\n"
            "pick('images')(client).pull('alpine:latest')\n"
        ),
    ],
    ids=[
        "closure-late-binding",
        "function-default-walrus",
        "keyword-only-wrapper",
        "kwargs-pop-wrapper",
        "nested-container-alias-mutation",
        "asyncio-program-keyword",
        "operator-attrgetter-run",
        "operator-attrgetter-images-alias",
    ],
)
def test_gate_boundary_python_rejects_final_review_findings(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_benign_method_override(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "class Base:\n"
        "    def invoke(self, cmd):\n        subprocess.run(cmd)\n"
        "class Runner(Base):\n"
        "    def invoke(self, cmd):\n        print(cmd)\n"
        "Runner().invoke(['docker', 'pull', 'diagnostic'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        ("import subprocess\nsubprocess.getoutput(cmd='docker pull alpine:latest')\n"),
        ("import subprocess\nsubprocess.getstatusoutput(cmd='docker pull alpine:latest')\n"),
        "import os\nos.popen(cmd='docker pull alpine:latest')\n",
        "import os\nos.system(command='docker pull alpine:latest')\n",
        ("import asyncio\nasyncio.create_subprocess_shell(cmd='docker pull alpine:latest')\n"),
        (
            "import subprocess\nops = {'run': subprocess.run}\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Ops:\n    run = subprocess.run\n"
            "Ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Runner:\n"
            "    def __call__(self, cmd):\n        subprocess.run(cmd)\n"
            "class Ops:\n"
            "    def __init__(self):\n        self.runner = Runner()\n"
            "    def invoke(self, cmd):\n        self.runner(cmd)\n"
            "Ops().invoke(['docker', 'pull', 'alpine:latest'])\n"
        ),
        ("import os\nos.system('{} pull alpine:latest'.format('docker'))\n"),
        "import os\nos.system('%s pull alpine:latest' % 'docker')\n",
        ("import os\nos.system(' '.join(['docker', 'pull', 'alpine:latest']))\n"),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure():\n"
            "    global cmd\n    cmd = ['docker', 'pull', 'alpine:latest']\n"
            "configure()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef outer():\n"
            "    cmd = ['git', 'status']\n"
            "    def configure():\n"
            "        nonlocal cmd\n        cmd = ['docker', 'pull', 'alpine:latest']\n"
            "    configure()\n    subprocess.run(cmd)\nouter()\n"
        ),
        (
            "import subprocess\ndef decorate(function):\n"
            "    def invoke(cmd):\n        subprocess.run(cmd)\n"
            "    return invoke\n"
            "@decorate\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Factory:\n"
            "    def make(self):\n        return subprocess.run\n"
            "Factory().make()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef invoke(*, cmd):\n"
            "    subprocess.getoutput(cmd=cmd)\n"
            "invoke(cmd='docker pull alpine:latest')\n"
        ),
        (
            "import subprocess\ndef factory():\n    return subprocess.getoutput\n"
            "factory()(cmd='docker pull alpine:latest')\n"
        ),
        ("import subprocess\nops = {}\nops.update({'run': subprocess.run})\n"),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef outer():\n    cmd = ['git', 'status']\n"
            "    def configure(value):\n        nonlocal cmd\n        cmd = value\n"
            "    configure(['docker', 'pull', 'alpine:latest'])\n"
            "    subprocess.run(cmd)\nouter()\n"
        ),
        (
            "import subprocess\ndef decorate(function):\n"
            "    def invoke(cmd):\n        subprocess.run(cmd)\n"
            "    return invoke\nalias = decorate\n"
            "@alias\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Factory:\n    @staticmethod\n"
            "    def make():\n        return subprocess.run\n"
            "Factory.make()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Base:\n"
            "    def make(self):\n        return subprocess.run\n"
            "class Factory(Base):\n    pass\n"
            "Factory().make()(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=[
        "subprocess-getoutput-cmd-keyword",
        "subprocess-getstatusoutput-cmd-keyword",
        "os-popen-cmd-keyword",
        "os-system-command-keyword",
        "asyncio-shell-cmd-keyword",
        "callable-in-mapping",
        "callable-as-class-attribute",
        "callable-instance-as-self-attribute",
        "static-string-format",
        "static-string-percent",
        "static-string-join",
        "global-payload-mutation",
        "nonlocal-payload-mutation",
        "decorator-returned-wrapper",
        "method-returned-callable",
        "wrapper-process-keyword",
        "returned-process-keyword",
        "callable-container-update",
        "global-parameter-mutation",
        "nonlocal-parameter-mutation",
        "aliased-decorator-wrapper",
        "staticmethod-returned-callable",
        "inherited-method-returned-callable",
    ],
)
def test_gate_boundary_python_rejects_second_final_review_findings(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("import_line", "callable_name"),
    [
        ("from scripts.common import invoke", "invoke"),
        ("import scripts.common as common", "common.invoke"),
        ("from scripts import common", "common.invoke"),
    ],
    ids=["direct-symbol", "qualified-module", "from-package-module"],
)
def test_gate_boundary_python_rejects_wrapper_imported_from_local_module(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    import_line: str,
    callable_name: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        f"{import_line}\n{callable_name}(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nsubprocess.getoutput(cmd='git status')\n",
        "import os\nos.system('{} {}'.format('git', 'status'))\n",
        ("ops = {'run': print}\nops['run'](['docker', 'pull', 'diagnostic'])\n"),
        (
            "def outer():\n    counter = 0\n"
            "    def increment():\n        nonlocal counter\n        counter = 1\n"
            "    increment()\nouter()\n"
        ),
        (
            "def decorate(function):\n    return print\n"
            "@decorate\ndef benign(value):\n    pass\n"
            "benign(['docker', 'pull', 'diagnostic'])\n"
        ),
        (
            "class Factory:\n    def make(self):\n        return print\n"
            "Factory().make()(['docker', 'pull', 'diagnostic'])\n"
        ),
    ],
    ids=[
        "safe-process-keyword",
        "safe-static-format",
        "benign-callable-container",
        "benign-nonlocal-state",
        "benign-decorator",
        "benign-method-factory",
    ],
)
def test_gate_boundary_python_allows_second_review_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_allows_safe_local_callable_boundary(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        "from scripts.common import invoke\ninvoke(['git', 'status'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "script",
    [
        "bash -O extglob -c 'docker pull alpine:latest'",
        "bash +O extglob -c 'docker pull alpine:latest'",
        "bash -o posix -c 'docker pull alpine:latest'",
        "bash +o posix -c 'docker pull alpine:latest'",
    ],
    ids=[
        "bash-enable-uppercase-o-option",
        "bash-disable-uppercase-o-option",
        "bash-enable-lowercase-o-option",
        "bash-disable-lowercase-o-option",
    ],
)
def test_gate_boundary_shell_rejects_command_after_option_value(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert _errors(checker, tmp_path)


def test_gate_boundary_ci_rejects_command_after_shell_option_value(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "script": ["bash -O extglob -c 'docker pull alpine:latest'"],
            }
        },
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "shebang",
    [
        '#!/usr/bin/env -S "bash -eu"',
        '#!/usr/bin/env --split-string "bash -eu"',
        '#!/usr/bin/env -S "busybox sh"',
    ],
    ids=["env-short-split-string", "env-long-split-string", "env-busybox-split-string"],
)
def test_gate_boundary_shell_scans_quoted_env_split_string_shebang(
    checker: ModuleType | _MissingChecker, tmp_path: Path, shebang: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(f"{shebang}\ndocker pull alpine:latest\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "script",
    [
        f"exec <<< 'docker pull {TAG}'\nbash",
        f"shopt -s expand_aliases\nalias invoke=eval\ninvoke 'docker pull {TAG}'",
        f"shopt -s expand_aliases\nalias invoke='bash -c'\ninvoke 'docker pull {TAG}'",
    ],
    ids=["exec-here-string", "eval-alias", "shell-command-alias"],
)
def test_gate_boundary_shell_rejects_reviewed_indirect_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, script: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{script}\n")

    assert _errors(checker, tmp_path)


def test_gate_boundary_ci_resolves_source_from_literal_variable(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/launch.sh").write_text(f"docker pull {TAG}\n")
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "before_script": ["SRC=source"],
                "script": ["$SRC ci/launch.sh"],
            }
        },
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_shell_scans_env_split_string_attached_shebang(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(
        f"#!/usr/bin/env -Sbash -eu\ndocker pull {TAG}\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_shell_allows_stdin_duplication_to_non_stdin_fd(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text("#!/usr/bin/env bash\nexec 3<&0\nbash -c true\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "shebang",
    [
        '#!/usr/bin/env -S "-C /tmp bash"',
        '#!/usr/bin/env --split-string "--chdir /tmp bash"',
    ],
    ids=["env-short-chdir", "env-long-chdir"],
)
def test_gate_boundary_shell_scans_env_chdir_split_string_shebang(
    checker: ModuleType | _MissingChecker, tmp_path: Path, shebang: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(f"{shebang}\ndocker pull {TAG}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import asyncio\nparts = ('docker', 'pull', 'alpine:latest')\n"
            "asyncio.create_subprocess_exec(*parts)\n"
        ),
        ("import os\nparts = ('docker', ['docker', 'pull', 'alpine:latest'])\nos.execvp(*parts)\n"),
        (
            "import asyncio\ndef factory():\n    return asyncio.create_subprocess_exec\n"
            "parts = ('docker', 'pull', 'alpine:latest')\nfactory()(*parts)\n"
        ),
    ],
    ids=["asyncio-static-star", "execvp-static-star", "returned-callable-static-star"],
)
def test_gate_boundary_python_rejects_static_starred_process_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        ("import os\nexecutable = 'docker'\nos.system('%s pull alpine:latest' % executable)\n"),
        "import os\nos.system(b'%s pull alpine:latest' % b'docker')\n",
    ],
    ids=["bound-percent-operand", "bytes-percent-template"],
)
def test_gate_boundary_python_rejects_resolved_percent_process_commands(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Ops:\n    pass\nops = Ops()\n"
            "setattr(ops, 'run', subprocess.run)\n"
            "ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Ops:\n    pass\nops = Ops()\n"
            "object.__setattr__(ops, 'run', subprocess.run)\n"
            "ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nops = {}\n"
            "ops.setdefault('run', subprocess.run)\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import operator\nimport subprocess\nops = {}\n"
            "operator.setitem(ops, 'run', subprocess.run)\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nops = []\nops += [subprocess.run]\n"
            "ops[0](['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["setattr", "object-setattr", "setdefault", "operator-setitem", "augassign-list"],
)
def test_gate_boundary_python_rejects_additional_callable_storage_primitives(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("import_line", "callable_name"),
    [
        ("from common import invoke", "invoke"),
        ("import common", "common.invoke"),
    ],
    ids=["sibling-from-import", "sibling-module-import"],
)
def test_gate_boundary_python_rejects_wrapper_imported_from_sibling_module(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    import_line: str,
    callable_name: str,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        f"{import_line}\n{callable_name}(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_rejects_decorator_imported_from_sibling_module(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef decorate(function):\n"
        "    def invoke(cmd):\n        subprocess.run(cmd)\n"
        "    return invoke\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        "from common import decorate\n@decorate\ndef benign(cmd):\n    print(cmd)\n"
        "benign(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure():\n    global cmd\n"
            "    cmd += ['docker', 'pull', 'alpine:latest']\n"
            "configure()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef outer(value):\n    cmd = ['git', 'status']\n"
            "    def configure():\n        nonlocal cmd\n        cmd.extend(value)\n"
            "    configure()\n    subprocess.run(cmd)\n"
            "outer(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef make():\n"
            "    return ['docker', 'pull', 'alpine:latest']\n"
            "cmd = ['git', 'status']\ndef configure():\n"
            "    global cmd\n    cmd = make()\n"
            "configure()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ndef make():\n"
            "    return ['docker', 'pull', 'alpine:latest']\n"
            "def outer():\n    cmd = ['git', 'status']\n"
            "    def configure():\n        nonlocal cmd\n        cmd = make()\n"
            "    configure()\n    subprocess.run(cmd)\nouter()\n"
        ),
    ],
    ids=["global-augassign", "nonlocal-container-mutation", "global-call", "nonlocal-call"],
)
def test_gate_boundary_python_rejects_external_scope_payload_mutations_at_sink(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_dynamic_external_state_without_process_sink(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "cache = None\ndef configure(value):\n    global cache\n    cache = value\n"
        "configure(object())\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_rejects_payload_when_uncalled_scope_clears_marker(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = []\n"
        "cmd.extend(['docker', 'ps'])\n"
        "def reset():\n    cmd.clear()\n"
        "subprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_ignores_payload_marker_from_uncalled_scope(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['echo', 'ok']\n"
        "def configure():\n    cmd[0] = 'docker'\n"
        "subprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_rejects_reflected_vars_payload_at_sink(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nclass Box:\n    pass\n"
        "box = Box()\nvars(box)['cmd'] = ['docker', 'ps']\n"
        "subprocess.run(vars(box)['cmd'])\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_safe_reflected_vars_payload_at_sink(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nclass Box:\n    pass\n"
        "box = Box()\nvars(box)['cmd'] = ['echo', 'ok']\n"
        "subprocess.run(vars(box)['cmd'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['docker', 'ps']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "configure(['git', 'status'])\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['docker', 'ps']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "def apply():\n    configure(['git', 'status'])\n"
            "apply()\nsubprocess.run(cmd)\n"
        ),
    ],
    ids=["direct-effect", "wrapper-effect"],
)
def test_gate_boundary_python_clears_stale_marker_after_safe_external_replace(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_keeps_dangerous_marker_after_external_replace(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "configure(['docker', 'ps'])\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "box.cmd = ['git', 'status']\n"
            "box.__dict__['cmd'] = ['docker', 'ps']\n"
            "subprocess.run(box.cmd)\n"
        ),
        (
            "box.cmd = ['git', 'status']\n"
            "vars(box)['cmd'] = ['docker', 'ps']\n"
            "subprocess.run(box.cmd)\n"
        ),
        (
            "box.__dict__['cmd'] = ['git', 'status']\n"
            "box.cmd = ['docker', 'ps']\n"
            "subprocess.run(box.__dict__['cmd'])\n"
        ),
    ],
    ids=["dunder-write", "vars-write", "attribute-write"],
)
def test_gate_boundary_python_invalidates_equivalent_safe_payload_paths(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nclass Box:\n    pass\nbox = Box()\n" + body
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "list.__setitem__(cmd, 0, 'docker')",
        "cmd.insert(0, 'docker')",
    ],
    ids=["unbound-setitem", "bound-insert"],
)
def test_gate_boundary_python_invalidates_safe_path_after_container_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['docker', 'ps']\ncmd[0] = 'git'\n"
        f"{mutation}\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_invalidates_safe_paths_for_all_payload_aliases(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['docker', 'ps']\ncmd[0] = 'git'\n"
        "alias = cmd\nalias[0] = 'docker'\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "selector",
    [
        "vars(subprocess).pop('run')",
        "subprocess.__dict__.pop('run')",
        "vars(subprocess).setdefault('run')",
    ],
    ids=["vars-pop", "dunder-pop", "vars-setdefault"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_handles_reflected_process_mapping_methods(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "vars(subprocess)[method]",
        "subprocess.__dict__[method]",
        "vars(subprocess)[input()]",
    ],
    ids=["vars-imported-key", "dunder-imported-key", "vars-computed-key"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_fails_closed_for_dynamic_process_mapping_keys(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("method = 'run'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nfrom runtime_config import method\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "vars(subprocess).copy()['run']",
        "dict(vars(subprocess))['run']",
        "(vars(subprocess) | {})['run']",
    ],
    ids=["copy", "dict-conversion", "mapping-union"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_transformed_process_mapping_provenance(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "subprocess.__getattribute__(method)",
        "object.__getattribute__(subprocess, method)",
    ],
    ids=["bound", "unbound"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_fails_closed_for_dynamic_process_getattribute(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("method = 'run'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nfrom runtime_config import method\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "importlib.import_module(module).run",
        "__import__(module).run",
        "getattr(importlib.import_module(module), method)",
    ],
    ids=["importlib", "dunder-import", "reflected-importlib"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_fails_closed_for_dynamic_process_import_provenance(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\nmethod = 'run'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import importlib\nfrom runtime_config import method, module\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "sys.modules['subprocess'].run",
        "sys.modules.get('subprocess').run",
        "globals()['subprocess'].run",
    ],
    ids=["sys-modules-subscript", "sys-modules-get", "globals-subscript"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_runtime_process_module_lookup_provenance(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nimport sys\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "runner = subprocess.run\n"
            "def configure():\n    global runner\n    runner = print\n"
            "configure()\nrunner(['docker', 'ps'])\n"
        ),
        (
            "def factory():\n    return subprocess.run\nrunner = factory\n"
            "def configure():\n    global runner\n    runner = lambda: print\n"
            "configure()\nrunner()(['docker', 'ps'])\n"
        ),
    ],
    ids=["direct-callable", "callable-factory"],
)
def test_gate_boundary_python_discards_stale_callable_facts_after_external_replace(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text("import subprocess\n" + body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("import_line", "selector"),
    [
        ("import sys as system", "system.modules['subprocess'].run"),
        ("from sys import modules as registry", "registry['subprocess'].run"),
    ],
    ids=["module-alias", "attribute-alias"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_aliased_runtime_module_registry(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    import_line: str,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{import_line}\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "sys.modules.copy()['subprocess'].run",
        "dict(sys.modules)['subprocess'].run",
        "(sys.modules | {})['subprocess'].run",
        "globals().copy()['subprocess'].run",
    ],
    ids=["copy", "dict-conversion", "mapping-union", "globals-copy"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_transformed_runtime_module_registry(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nimport sys\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "loader",
    [
        "loader = functools.partial(importlib.import_module, module)",
        "loader = lambda: importlib.import_module(module)",
        "def loader():\n    return importlib.import_module(module)",
    ],
    ids=["partial", "lambda", "function"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_dynamic_process_import_factories(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    loader: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport importlib\nfrom runtime_config import module\n"
        f"{loader}\nloader().run({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "operator.getitem(sys.modules, 'subprocess').run",
        "dict.__getitem__(sys.modules, 'subprocess').run",
    ],
    ids=["operator-getitem", "dict-getitem"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_unbound_runtime_module_registry_lookup(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import operator\nimport subprocess\nimport sys\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "importlib.import_module.__call__(module).run",
        "importlib.__import__(module).run",
        "functools.partial(importlib.import_module, module)().run",
    ],
    ids=["dunder-call", "importlib-dunder-import", "inline-partial"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_normalized_dynamic_process_loaders(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport importlib\nfrom runtime_config import module\n"
        f"{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "__builtins__['__import__'](module).run",
        "__builtins__.__import__(module).run",
        "globals()['__builtins__'].__import__(module).run",
    ],
    ids=["builtins-subscript", "builtins-attribute", "globals-builtins"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_reflected_builtin_process_loaders(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"from runtime_config import module\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("method", "selector"),
    [
        (
            "@staticmethod\n    def load(name):\n        return importlib.import_module(name)",
            "Loader.load(module).run",
        ),
        (
            "def load(self, name):\n        return importlib.import_module(name)",
            "Loader().load(module).run",
        ),
        (
            "def __call__(self, name):\n        return importlib.import_module(name)",
            "Loader()(module).run",
        ),
    ],
    ids=["staticmethod", "instance-method", "callable-instance"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_dynamic_process_class_factories(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    method: str,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import importlib\nfrom runtime_config import module\n"
        f"class Loader:\n    {method}\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        "globals()['print'](['docker', 'ps'])",
        "sys.modules['math'].sqrt(['docker', 'ps'])",
    ],
    ids=["globals-print", "sys-modules-math"],
)
def test_gate_boundary_python_allows_explicit_non_process_runtime_lookups(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(f"import sys\n{body}\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "selector",
    [
        "importlib.import_module(name=module).run",
        "__import__(name=module).run",
        "functools.partial(importlib.import_module, name=module)().run",
    ],
    ids=["importlib", "dunder-import", "partial"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_keyword_dynamic_process_loaders(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport importlib\nfrom runtime_config import module\n"
        f"{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "dict.get(sys.modules, 'subprocess').run",
        "dict.pop(sys.modules, 'subprocess').run",
        "dict.setdefault(sys.modules, 'subprocess').run",
    ],
    ids=["get", "pop", "setdefault"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_unbound_runtime_registry_methods(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nimport sys\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "getattr(sys, registry)[module].Popen",
        "vars(sys)[registry][module].call",
        "sys.__dict__[registry][module].check_output",
    ],
    ids=["getattr", "vars", "dunder-dict"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_dynamic_runtime_registry_attributes(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text(
        "module = 'subprocess'\nregistry = 'modules'\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import sys\nfrom runtime_config import module, registry\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("decorator", "method"),
    [
        ("property", "Popen"),
        ("functools.cached_property", "call"),
    ],
    ids=["property", "cached-property"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'ps']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_dynamic_process_module_properties(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    decorator: str,
    method: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport importlib\nfrom runtime_config import module\n"
        f"class Loader:\n    @{decorator}\n    def loaded(self):\n"
        "        return importlib.import_module(module)\n"
        f"Loader().loaded.{method}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("factory_body", "expected_errors"),
    [
        (
            "    return types.SimpleNamespace(Popen=print)\n"
            "    return importlib.import_module(module)\n",
            False,
        ),
        (
            "    if False:\n        return importlib.import_module(module)\n"
            "    return types.SimpleNamespace(Popen=print)\n",
            False,
        ),
        (
            "    if True:\n        return importlib.import_module(module)\n"
            "    return types.SimpleNamespace(Popen=print)\n",
            True,
        ),
        (
            "    if module:\n        return importlib.import_module(module)\n"
            "    return types.SimpleNamespace(Popen=print)\n",
            True,
        ),
    ],
    ids=["after-return", "if-false", "if-true", "dynamic-branch"],
)
def test_gate_boundary_python_respects_factory_return_reachability(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    factory_body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("module = 'subprocess'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import importlib\nimport types\nfrom runtime_config import module\n"
        f"def loader():\n{factory_body}"
        "loader().Popen(['docker', 'ps'])\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("dangerous", "safe"),
    [
        (
            "import os\nos.spawnlp(os.P_WAIT, 'docker', 'docker', 'run', 'alpine:latest')",
            "import os\nos.spawnlp(os.P_WAIT, 'git', 'git', 'status')",
        ),
        (
            "import os\nos.spawnl(os.P_WAIT, '/usr/bin/docker', 'docker', 'run', 'alpine:latest')",
            "import os\nos.spawnl(os.P_WAIT, '/usr/bin/git', 'git', 'status')",
        ),
        (
            "import os\nos.spawnvp(os.P_WAIT, 'docker', ['docker', 'run', 'alpine:latest'])",
            "import os\nos.spawnvp(os.P_WAIT, 'git', ['git', 'status'])",
        ),
        (
            "import os\nos.spawnle(os.P_WAIT, '/usr/bin/docker', 'docker', 'run', "
            "'alpine:latest', {})",
            "import os\nos.spawnle(os.P_WAIT, '/usr/bin/git', 'git', 'status', {})",
        ),
        (
            "import os\nos.spawnlpe(os.P_WAIT, 'docker', 'docker', 'run', 'alpine:latest', {})",
            "import os\nos.spawnlpe(os.P_WAIT, 'git', 'git', 'status', {})",
        ),
        (
            "import os\nos.spawnv(os.P_WAIT, '/usr/bin/docker', "
            "['docker', 'run', 'alpine:latest'])",
            "import os\nos.spawnv(os.P_WAIT, '/usr/bin/git', ['git', 'status'])",
        ),
        (
            "import os\nos.spawnve(os.P_WAIT, '/usr/bin/docker', "
            "['docker', 'run', 'alpine:latest'], {})",
            "import os\nos.spawnve(os.P_WAIT, '/usr/bin/git', ['git', 'status'], {})",
        ),
        (
            "import os\nos.spawnvpe(os.P_WAIT, 'docker', ['docker', 'run', 'alpine:latest'], {})",
            "import os\nos.spawnvpe(os.P_WAIT, 'git', ['git', 'status'], {})",
        ),
        (
            "import pty\npty.spawn(['docker', 'run', 'alpine:latest'])",
            "import pty\npty.spawn(['git', 'status'])",
        ),
        (
            "import asyncio\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_exec(lambda: None, 'docker', 'run', 'alpine:latest')\n"
            "asyncio.run(main())",
            "import asyncio\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_exec(lambda: None, 'git', 'status')\n"
            "asyncio.run(main())",
        ),
        (
            "import asyncio\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_shell(lambda: None, 'docker run alpine:latest')\n"
            "asyncio.run(main())",
            "import asyncio\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.subprocess_shell(lambda: None, 'git status')\n"
            "asyncio.run(main())",
        ),
        (
            "import multiprocessing\nimport os\n"
            "process = multiprocessing.Process(target=os.system, "
            "args=('docker run alpine:latest',))\nprocess.start()",
            "import multiprocessing\nimport os\n"
            "process = multiprocessing.Process(target=os.system, args=('git status',))\n"
            "process.start()",
        ),
        (
            "import os\nimport threading\n"
            "thread = threading.Thread(target=os.system, "
            "args=('docker run alpine:latest',))\nthread.start()",
            "import os\nimport threading\n"
            "thread = threading.Thread(target=os.system, args=('git status',))\nthread.start()",
        ),
        (
            "import anyio\nasync def main():\n"
            "    await anyio.run_process(['docker', 'run', 'alpine:latest'])\n"
            "anyio.run(main)",
            "import anyio\nasync def main():\n"
            "    await anyio.run_process(['git', 'status'])\nanyio.run(main)",
        ),
        (
            "import anyio\nasync def main():\n"
            "    await anyio.open_process(['docker', 'run', 'alpine:latest'])\n"
            "anyio.run(main)",
            "import anyio\nasync def main():\n"
            "    await anyio.open_process(['git', 'status'])\nanyio.run(main)",
        ),
        (
            "import asyncio\nimport os\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.run_in_executor(None, os.system, 'docker run alpine:latest')\n"
            "asyncio.run(main())",
            "import asyncio\nimport os\nasync def main():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.run_in_executor(None, os.system, 'git status')\n"
            "asyncio.run(main())",
        ),
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().apply(os.system, args=('docker run alpine:latest',))",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().apply(os.system, args=('git status',))",
        ),
        (
            "import atexit\nimport os\natexit.register(os.system, 'docker run alpine:latest')",
            "import atexit\nimport os\natexit.register(os.system, 'git status')",
        ),
    ],
    ids=[
        "spawnlp",
        "spawnl",
        "spawnvp",
        "spawnle",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvpe",
        "pty-spawn",
        "loop-subprocess-exec",
        "loop-subprocess-shell",
        "multiprocessing-process",
        "threading-thread",
        "anyio-run-process",
        "anyio-open-process",
        "loop-run-in-executor",
        "pool-apply",
        "atexit-register",
    ],
)
def test_gate_boundary_python_covers_standard_process_execution_apis(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    dangerous: str,
    safe: str,
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "services/worker.py"
    worker.write_text(dangerous + "\n")
    assert _errors(checker, tmp_path)

    worker.write_text(safe + "\n")
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("dangerous", "safe"),
    [
        (
            "import anyio\nasync def main():\n"
            "    await anyio.run_process(command=['docker', 'run', 'alpine:latest'])\n"
            "anyio.run(main)",
            "import anyio\nasync def main():\n"
            "    await anyio.run_process(command=['git', 'status'])\nanyio.run(main)",
        ),
        (
            "import anyio\nasync def main():\n"
            "    await anyio.open_process(command=['docker', 'run', 'alpine:latest'])\n"
            "anyio.run(main)",
            "import anyio\nasync def main():\n"
            "    await anyio.open_process(command=['git', 'status'])\nanyio.run(main)",
        ),
        (
            "import pty\npty.spawn(argv=['docker', 'run', 'alpine:latest'])",
            "import pty\npty.spawn(argv=['git', 'status'])",
        ),
        (
            "import os\nos.execvp(file='docker', args=['docker', 'run', 'alpine:latest'])",
            "import os\nos.execvp(file='git', args=['git', 'status'])",
        ),
        (
            "import os\nos.execvpe(file='docker', args=['docker', 'run', 'alpine:latest'], env={})",
            "import os\nos.execvpe(file='git', args=['git', 'status'], env={})",
        ),
        (
            "import os\nos.spawnv(mode=os.P_WAIT, file='/usr/bin/docker', "
            "args=['docker', 'run', 'alpine:latest'])",
            "import os\nos.spawnv(mode=os.P_WAIT, file='/usr/bin/git', args=['git', 'status'])",
        ),
        (
            "import os\nos.spawnve(mode=os.P_WAIT, file='/usr/bin/docker', "
            "args=['docker', 'run', 'alpine:latest'], env={})",
            "import os\nos.spawnve(mode=os.P_WAIT, file='/usr/bin/git', "
            "args=['git', 'status'], env={})",
        ),
        (
            "import os\nos.spawnvp(mode=os.P_WAIT, file='docker', "
            "args=['docker', 'run', 'alpine:latest'])",
            "import os\nos.spawnvp(mode=os.P_WAIT, file='git', args=['git', 'status'])",
        ),
        (
            "import os\nos.spawnvpe(mode=os.P_WAIT, file='docker', "
            "args=['docker', 'run', 'alpine:latest'], env={})",
            "import os\nos.spawnvpe(mode=os.P_WAIT, file='git', args=['git', 'status'], env={})",
        ),
    ],
    ids=[
        "anyio-run-process",
        "anyio-open-process",
        "pty-spawn",
        "execvp",
        "execvpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    ],
)
def test_gate_boundary_python_covers_keyword_process_execution_apis(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    dangerous: str,
    safe: str,
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "services/worker.py"
    worker.write_text(dangerous + "\n")
    assert _errors(checker, tmp_path)

    worker.write_text(safe + "\n")
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/usr/bin/env python3",
        "#!/usr/bin/python3",
        "#!/usr/bin/env -S python3 -I",
        "#!/usr/bin/env -S -u FOO python3 -I",
        "#!/usr/bin/env -S -C /tmp python3 -I",
        "#!/usr/bin/env --split-string=python3 -I",
        "#!/usr/bin/env -Spython3 -I",
    ],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'run', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_scans_extensionless_python_shebang_files(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    shebang: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/worker").write_text(
        f"{shebang}\nimport subprocess\nsubprocess.run({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_ci_ignores_non_job_variable_named_image(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "variables": {
                "extends": ".metadata",
                "image": "report-thumbnail",
                "include": "report.json",
                "script": "docker pull alpine:latest",
                "services": "report-helper",
            },
            "job": {"image": REFERENCE, "script": ["true"]},
        },
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "job_fragment",
    [
        {
            "variables": {
                "extends": ".metadata",
                "image": "report-thumbnail",
                "include": "report.json",
                "script": "docker pull alpine:latest",
                "services": "report-helper",
            }
        },
        {
            "rules": [
                {
                    "variables": {
                        "image": "report-thumbnail",
                        "script": "docker pull alpine:latest",
                    }
                }
            ]
        },
        {
            "parallel": {
                "matrix": [
                    {
                        "image": ["report-thumbnail"],
                        "services": ["report-helper"],
                    }
                ]
            }
        },
    ],
    ids=["job-variables", "rules-variables", "parallel-matrix"],
)
def test_gate_boundary_ci_ignores_nested_non_directive_image_keys(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    job_fragment: dict[str, object],
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {"job": {"image": REFERENCE, "script": ["true"], **job_fragment}},
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_restores_ancestral_class_facts_after_scope_analysis(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "class Runner:\n"
        "    def __call__(self, cmd):\n"
        "        subprocess.run(cmd)\n"
        "def reset(value):\n"
        "    global Runner\n"
        "    Runner = value\n"
        "def unused():\n"
        "    reset(print)\n"
        "Runner()(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_method_does_not_close_over_class_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "class Runner:\n"
        "    subprocess = None\n"
        "    def invoke(self):\n"
        "        subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
        "Runner().invoke()\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_evaluates_eager_annotations_in_enclosing_scope(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def f(x: subprocess.run(['docker', 'pull', 'alpine:latest'])):\n"
        "    pass\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_ignores_postponed_annotations(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "from __future__ import annotations\n"
        "import subprocess\n"
        "def f(x: subprocess.run(['docker', 'pull', 'alpine:latest'])):\n"
        "    print(x)\n"
        "f('safe')\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_evaluates_decorators_before_function_scope(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess as sp\n"
        "def decorate(value):\n"
        "    return lambda function: function\n"
        "@decorate(sp.run(['docker', 'pull', 'alpine:latest']))\n"
        "def f(sp):\n"
        "    return sp\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_evaluates_class_decorators_in_enclosing_scope(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess as sp\n"
        "def decorate(value):\n"
        "    return lambda cls: cls\n"
        "@decorate(sp.run(['docker', 'pull', 'alpine:latest']))\n"
        "class C:\n"
        "    sp = print\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_safe_path_lookup_skips_class_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "cmd = []\n"
        "cmd.extend(['docker', 'pull', 'alpine:latest'])\n"
        "class Runner:\n"
        "    cmd = ['docker']\n"
        "    cmd[0] = 'git'\n"
        "    def run(self):\n"
        "        subprocess.run(cmd)\n"
        "Runner().run()\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_external_effect_lookup_skips_class_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "cmd = ['git', 'status']\n"
        "def configure(value):\n"
        "    global cmd\n"
        "    cmd = value\n"
        "class Runner:\n"
        "    configure = print\n"
        "    def apply(self):\n"
        "        configure(['docker', 'pull', 'alpine:latest'])\n"
        "        subprocess.run(cmd)\n"
        "Runner().apply()\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_class_lookup_skips_enclosing_class_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "class Exec:\n"
        "    def run(self, cmd):\n"
        "        subprocess.run(cmd)\n"
        "class Runner:\n"
        "    class Exec:\n"
        "        def run(self, cmd):\n"
        "            subprocess.run(['git', 'status'])\n"
        "    def apply(self):\n"
        "        Exec().run(['docker', 'pull', 'alpine:latest'])\n"
        "Runner().apply()\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "from __future__ import annotations\nimport subprocess\n"
            "value: subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef f():\n"
            "    value: subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
            "f()\n"
        ),
    ],
    ids=["postponed-module", "unevaluated-local"],
)
def test_gate_boundary_python_ignores_unevaluated_variable_annotations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_ignores_attribute_variable_annotation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "class Box:\n"
        "    pass\n"
        "box = Box()\n"
        "box.value: subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_ignores_lazy_type_alias_value(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ntype Alias = subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("initial", "iteration", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["docker-iteration", "safe-iteration"],
)
def test_gate_boundary_python_binds_static_for_loop_targets(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    iteration: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\ncmd = {initial}\nfor cmd in [{iteration}]:\n    subprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("initial", "entered", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["docker-entered", "safe-entered"],
)
def test_gate_boundary_python_binds_static_with_targets(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    entered: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nfrom contextlib import nullcontext\n"
        f"cmd = {initial}\n"
        f"with nullcontext({entered}) as cmd:\n"
        "    subprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("initial", "subject", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["docker-capture", "safe-capture"],
)
def test_gate_boundary_python_binds_match_capture_targets(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    subject: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        f"cmd = {initial}\n"
        f"match {subject}:\n"
        "    case cmd:\n"
        "        subprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "expression",
    [
        "[subprocess.run(cmd) for cmd in [COMMAND]]",
        "{subprocess.run(cmd) for cmd in [COMMAND]}",
        "{str(cmd): subprocess.run(cmd) for cmd in [COMMAND]}",
        "tuple(subprocess.run(cmd) for cmd in [COMMAND])",
    ],
    ids=["list", "set", "dict", "generator"],
)
@pytest.mark.parametrize(
    ("initial", "iteration", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["docker-iteration", "safe-iteration"],
)
def test_gate_boundary_python_binds_comprehension_targets_in_implicit_scope(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    expression: str,
    initial: str,
    iteration: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    body = expression.replace("COMMAND", iteration)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\ncmd = {initial}\nresults = {body}\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_python_wrapper_summary_skips_uncalled_nested_scope(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def outer(cmd):\n"
        "    def inner():\n"
        "        subprocess.run(cmd)\n"
        "    print(cmd)\n"
        "outer(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "class Logger:\n"
            "    def subprocess_exec(self, callback, *values):\n"
            "        print(values)\n"
            "Logger().subprocess_exec(None, 'docker', 'run', 'alpine:latest')\n"
        ),
        (
            "import os\nclass Logger:\n"
            "    def run_in_executor(self, executor, callback, *values):\n"
            "        print(values)\n"
            "Logger().run_in_executor(None, os.system, 'docker run alpine:latest')\n"
        ),
        (
            "import os\nclass Logger:\n"
            "    def apply(self, callback, args=(), kwds=None):\n"
            "        print(args)\n"
            "Logger().apply(os.system, args=('docker run alpine:latest',))\n"
        ),
        (
            "import os\nclass Logger:\n"
            "    def submit(self, callback, *values):\n"
            "        print(values)\n"
            "Logger().submit(os.system, 'docker run alpine:latest')\n"
        ),
    ],
    ids=["subprocess-exec", "run-in-executor", "apply", "submit"],
)
def test_gate_boundary_python_ignores_non_process_method_homonyms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import multiprocessing\nimport os\n"
            "ctx = multiprocessing.get_context('spawn')\n"
            "process = ctx.Process(target=os.system, "
            "args=('docker run alpine:latest',))\nprocess.start()\n",
            True,
        ),
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.get_context('spawn').Process("
            "target=os.system, args=('git status',)).start()\n",
            False,
        ),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_models_multiprocessing_context_process(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import multiprocessing\nimport os\n"
            "pool = multiprocessing.Pool()\napply = pool.apply\n"
            "apply(os.system, args=('docker pull alpine:latest',))\n",
            True,
        ),
        (
            "import asyncio\nimport os\nloop = asyncio.get_event_loop()\n"
            "dispatch = loop.run_in_executor\n"
            "dispatch(None, os.system, 'docker pull alpine:latest')\n",
            True,
        ),
        (
            "import concurrent.futures\nimport os\n"
            "executor = concurrent.futures.ThreadPoolExecutor()\n"
            "submit = executor.submit\nsubmit(os.system, 'docker pull alpine:latest')\n",
            True,
        ),
        (
            "import concurrent.futures\nimport os\n"
            "executor = concurrent.futures.ThreadPoolExecutor()\n"
            "submit = executor.submit\nsubmit(os.system, 'git status')\n",
            False,
        ),
    ],
    ids=["pool-apply", "run-in-executor", "executor-submit", "safe-submit"],
)
def test_gate_boundary_python_models_proven_process_dispatch_aliases(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("dangerous", "safe"),
    [
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().apply_async("
            "os.system, args=('docker pull alpine:latest',))\n",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().apply_async(os.system, args=('git status',))\n",
        ),
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().map(os.system, ['docker pull alpine:latest'])\n",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().map(os.system, ['git status'])\n",
        ),
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().starmap("
            "os.system, [('docker pull alpine:latest',)])\n",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().starmap(os.system, [('git status',)])\n",
        ),
        (
            "import concurrent.futures\nimport os\n"
            "executor = concurrent.futures.ThreadPoolExecutor()\n"
            "executor.map(os.system, ['docker pull alpine:latest'])\n",
            "import concurrent.futures\nimport os\n"
            "executor = concurrent.futures.ThreadPoolExecutor()\n"
            "executor.map(os.system, ['git status'])\n",
        ),
        (
            "import os\nimport threading\n"
            "threading.Timer(1, os.system, "
            "args=('docker pull alpine:latest',)).start()\n",
            "import os\nimport threading\n"
            "threading.Timer(1, os.system, args=('git status',)).start()\n",
        ),
    ],
    ids=["pool-apply-async", "pool-map", "pool-starmap", "executor-map", "timer"],
)
def test_gate_boundary_python_covers_process_collection_adapters(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    dangerous: str,
    safe: str,
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    worker.write_text(dangerous)
    assert _errors(checker, tmp_path)

    worker.write_text(safe)
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "relative_path",
    [
        "services/worker/tests/test_probe.py",
        "services/worker/docs/probe.py",
        "scripts/helpers/bench/probe.py",
    ],
)
def test_gate_boundary_python_ignores_nested_excluded_directories(
    checker: ModuleType | _MissingChecker, tmp_path: Path, relative_path: str
) -> None:
    _write_valid_repo(tmp_path)
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import subprocess\nsubprocess.run(['docker', 'pull', 'alpine:latest'])\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ndef factory():\n"
            "    def decorate(function):\n"
            "        def invoke(cmd):\n            subprocess.run(cmd)\n"
            "        return invoke\n    return decorate\n"
            "@factory()\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Decorator:\n"
            "    def __call__(self, function):\n"
            "        def invoke(cmd):\n            subprocess.run(cmd)\n"
            "        return invoke\ndecorate = Decorator()\n"
            "@decorate\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Decorate:\n"
            "    def __init__(self, function):\n        self.function = function\n"
            "    def __call__(self, cmd):\n        subprocess.run(cmd)\n"
            "@Decorate\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import asyncio\nasync def factory():\n"
            "    return asyncio.create_subprocess_exec\n"
            "async def invoke():\n"
            "    await (await factory())('docker', 'pull', 'alpine:latest')\n"
            "asyncio.run(invoke())\n"
        ),
        (
            "import asyncio\nclass Factory:\n"
            "    async def make(self):\n        return asyncio.create_subprocess_exec\n"
            "async def invoke():\n"
            "    await (await Factory().make())('docker', 'pull', 'alpine:latest')\n"
            "asyncio.run(invoke())\n"
        ),
        (
            "import subprocess\nclass Factory:\n    @property\n"
            "    def runner(self):\n        return subprocess.run\n"
            "Factory().runner(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=[
        "second-order-decorator",
        "decorator-instance",
        "decorator-class",
        "await-function-factory",
        "await-method-factory",
        "property-factory",
    ],
)
def test_gate_boundary_python_rejects_higher_order_callable_factories(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import asyncio\ndef forward(parts):\n"
            "    asyncio.create_subprocess_exec(*parts)\n"
            "forward(('docker', 'pull', 'alpine:latest'))\n"
        ),
        (
            "import asyncio\ndef factory():\n    return asyncio.create_subprocess_exec\n"
            "def forward(parts):\n    factory()(*parts)\n"
            "forward(('docker', 'pull', 'alpine:latest'))\n"
        ),
    ],
    ids=["direct-opaque-star-forwarding", "returned-callable-opaque-star-forwarding"],
)
def test_gate_boundary_python_rejects_forwarded_opaque_starred_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        ("import asyncio\nparts = ('git', 'status')\nasyncio.create_subprocess_exec(*parts)\n"),
        (
            "import asyncio\ndef forward(parts):\n"
            "    asyncio.create_subprocess_exec(*parts)\n"
            "forward(('git', 'status'))\n"
        ),
    ],
    ids=["direct-static-git-star", "forwarded-git-star"],
)
def test_gate_boundary_python_allows_definitely_benign_starred_process_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "class Ops:\n    pass\nops = Ops()\n"
            "setattr(ops, 'run', print)\nops.run(['docker', 'pull', 'diagnostic'])\n"
        ),
        (
            "class Ops:\n    pass\nops = Ops()\n"
            "object.__setattr__(ops, 'run', print)\n"
            "ops.run(['docker', 'pull', 'diagnostic'])\n"
        ),
        "ops = {}\nops.setdefault('run', print)\nops['run'](['docker', 'diagnostic'])\n",
        (
            "import operator\nops = {}\noperator.setitem(ops, 'run', print)\n"
            "ops['run'](['docker', 'diagnostic'])\n"
        ),
        "ops = []\nops += [print]\nops[0](['docker', 'diagnostic'])\n",
        "import subprocess\nsetattr(subprocess.run, 'description', 'safe')\n",
    ],
    ids=[
        "benign-setattr",
        "benign-object-setattr",
        "benign-setdefault",
        "benign-operator-setitem",
        "benign-augassign",
        "benign-callable-object-attribute",
    ],
)
def test_gate_boundary_python_allows_benign_callable_storage_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_allows_safe_sibling_import_boundary(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        "from common import invoke\ninvoke(['git', 'status'])\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_rejects_forward_declared_nonlocal_payload_mutation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ndef outer():\n"
        "    def configure():\n        nonlocal cmd\n"
        "        cmd = ['docker', 'pull', 'alpine:latest']\n"
        "    cmd = ['git', 'status']\n    configure()\n    subprocess.run(cmd)\nouter()\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_does_not_taint_safe_local_homonym(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def taint(value):\n    global cmd\n    cmd = value\n"
        "def safe():\n    cmd = ['git', 'status']\n    subprocess.run(cmd)\nsafe()\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Decorator:\n"
            "    def __call__(self, function):\n"
            "        def invoke(cmd):\n            subprocess.run(cmd)\n"
            "        return invoke\n"
            "@Decorator()\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Decorator:\n"
            "    def __new__(cls, function):\n        return subprocess.run\n"
            "@Decorator\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["inline-decorator-instance", "decorator-new-factory"],
)
def test_gate_boundary_python_rejects_additional_decorator_factory_forms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass Factory:\n"
            "    def make(self, value):\n        return subprocess.run\n"
            "Factory().make(['docker', 'pull', 'diagnostic'])\n"
        ),
        (
            "import subprocess\ndef factory():\n"
            "    def decorate(function):\n"
            "        def invoke(cmd):\n            subprocess.run(cmd)\n"
            "        return invoke\n    return decorate\n"
            "factory()(['docker', 'pull', 'diagnostic'])\n"
        ),
    ],
    ids=["ordinary-method-factory", "unapplied-decorator-factory"],
)
def test_gate_boundary_python_preserves_callable_factory_depth_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_shell_scans_gnu_env_split_string_escaped_separator_shebang(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(
        rf"#!/usr/bin/env -S bash\_-eu{chr(10)}docker pull {TAG}{chr(10)}"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "env -C /tmp bash -c 'docker pull alpine:latest'",
        "env --chdir=/tmp bash -c 'docker pull alpine:latest'",
        "env -Sbash -c 'docker pull alpine:latest'",
        "env -uFOO bash -c 'docker pull alpine:latest'",
        "env -v bash -c 'docker pull alpine:latest'",
        "env --debug bash -c 'docker pull alpine:latest'",
        "env --block-signal=PIPE bash -c 'docker pull alpine:latest'",
        "env --unsupported-option bash -c 'docker pull alpine:latest'",
    ],
    ids=[
        "short-chdir",
        "long-chdir-attached",
        "short-split-attached",
        "short-unset-attached",
        "verbose",
        "debug",
        "block-signal-attached",
        "unknown-option-fail-closed",
    ],
)
def test_gate_boundary_shell_rejects_docker_after_gnu_env_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "env -C /tmp bash -c true",
        "env --chdir=/tmp bash -c true",
        "env -uFOO bash -c true",
        "env -v bash -c true",
        "env --debug bash -c true",
        "env --block-signal=PIPE bash -c true",
    ],
    ids=[
        "safe-short-chdir",
        "safe-long-chdir-attached",
        "safe-short-unset-attached",
        "safe-verbose",
        "safe-debug",
        "safe-block-signal-attached",
    ],
)
def test_gate_boundary_shell_allows_safe_command_after_known_gnu_env_options(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runner.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ndef make():\n"
            "    return (['docker', 'pull', 'alpine:latest'],)\n"
            "subprocess.run(*make())\n"
        ),
        ("import os\ndef make():\n    return ('docker pull alpine:latest',)\nos.system(*make())\n"),
        (
            "import subprocess\ndef make():\n"
            "    return ('docker pull alpine:latest',)\nsubprocess.getoutput(*make())\n"
        ),
    ],
    ids=["subprocess-run", "os-system", "subprocess-getoutput"],
)
def test_gate_boundary_python_rejects_terminal_opaque_starred_process_arguments(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        ("import os\nexe = 'docker'\nos.system('%(exe)s pull alpine:latest' % {'exe': exe})\n"),
        (
            "import os\nexe = 'docker'\naction = 'pull'\n"
            "os.system('%s %s alpine:latest' % (exe, action))\n"
        ),
        ("import os\nexe = b'docker'\nos.system(b'%(exe)s pull alpine:latest' % {b'exe': exe})\n"),
    ],
    ids=["mapping-name", "tuple-names", "bytes-mapping-name"],
)
def test_gate_boundary_python_rejects_nested_resolved_percent_process_commands(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_nested_resolved_safe_percent_command(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import os\nexe = 'git'\naction = 'status'\nos.system('%s %s' % (exe, action))\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nops = {}\n"
            "dict.__setitem__(ops, 'run', subprocess.run)\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nops = {}\n"
            "dict.setdefault(ops, 'run', subprocess.run)\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Ops:\n    pass\nops = Ops()\n"
            "setattr(*(ops, 'run', subprocess.run))\n"
            "ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nclass Ops:\n    pass\nops = Ops()\n"
            "getattr(ops, '__setattr__')('run', subprocess.run)\n"
            "ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["dict-setitem-unbound", "dict-setdefault-unbound", "starred-setattr", "reflected-setattr"],
)
def test_gate_boundary_python_rejects_unbound_and_reflected_callable_storage(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import operator\nimport subprocess\n"
            "operator.methodcaller('run', ['docker', 'pull', 'alpine:latest'])"
            "(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "def choose():\n    return 'run'\n"
            "operator.methodcaller(choose(), ['docker', 'pull', 'alpine:latest'])"
            "(subprocess)\n",
            True,
        ),
        (
            "import subprocess\nimport types\n"
            "holder = types.SimpleNamespace(runner=subprocess.run)\n"
            "holder.runner(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\nfrom types import SimpleNamespace\n"
            "holder = SimpleNamespace(runner=subprocess.run)\n"
            "holder.runner(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\nfrom types import SimpleNamespace as NS\n"
            "holder = NS(runner=subprocess.run)\n"
            "holder.runner(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "class Runner:\n"
            "    def __init__(self):\n"
            "        self.runner = subprocess.run\n"
            "    def __call__(self, command):\n"
            "        self.runner(command)\n"
            "Runner()(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "class Runner:\n"
            "    def __init__(self, runner):\n"
            "        self.runner = runner\n"
            "    def __call__(self, command):\n"
            "        self.runner(command)\n"
            "Runner(subprocess.run)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "class Runner:\n"
            "    def __init__(self, runner):\n"
            "        alias = runner\n"
            "        self.runner = alias\n"
            "    def __call__(self, command):\n"
            "        self.runner(command)\n"
            "Runner(subprocess.run)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "class Runner:\n"
            "    def __init__(self, runner):\n"
            "        setattr(self, 'runner', runner)\n"
            "    def __call__(self, command):\n"
            "        self.runner(command)\n"
            "Runner(subprocess.run)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, command):\n"
            "        print(command)\n"
            "operator.methodcaller('run', ['docker', 'diagnostic'])(LocalModule())\n",
            False,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.methodcaller('run', ['git', 'status'])(subprocess)\n",
            False,
        ),
        (
            "import types\n"
            "holder = types.SimpleNamespace(runner=print)\n"
            "holder.runner(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "class Runner:\n"
            "    def __init__(self, runner):\n"
            "        self.runner = runner\n"
            "    def __call__(self, command):\n"
            "        self.runner(command)\n"
            "Runner(print)(['docker', 'diagnostic'])\n",
            False,
        ),
    ],
    ids=[
        "methodcaller-process",
        "methodcaller-dynamic-process",
        "simple-namespace-process",
        "simple-namespace-imported",
        "simple-namespace-aliased",
        "direct-instance-storage",
        "parameter-instance-storage",
        "aliased-parameter-storage",
        "setattr-parameter-storage",
        "methodcaller-safe-owner",
        "methodcaller-safe-payload",
        "simple-namespace-safe",
        "parameter-instance-safe",
    ],
)
def test_gate_boundary_python_tracks_callables_stored_behind_attributes(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\nrunner = subprocess.run\n"
            "vars()['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import builtins\nimport subprocess\n"
            "def invoke():\n"
            "    runner = subprocess.run\n"
            "    builtins.vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n",
            True,
        ),
        (
            "import subprocess\nrunner = subprocess.run\n"
            "vars.__call__()['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import builtins\nimport subprocess\nrunner = subprocess.run\n"
            "builtins.vars.__call__()['runner']"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "def invoke():\n"
            "    runner = subprocess.run\n"
            "    locals.__call__()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n",
            True,
        ),
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "def invoke(runner):\n    vars()['runner'](['docker', 'diagnostic'])\ninvoke(print)\n",
            False,
        ),
        (
            "import subprocess\n"
            "def invoke(vars):\n"
            "    runner = subprocess.run\n"
            "    vars()['runner'](['docker', 'diagnostic'])\n"
            "invoke(lambda: {'runner': print})\n",
            False,
        ),
        (
            "runner = print\nvars()['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import subprocess\nrunner = subprocess.run\nvars()['runner'](['git', 'status'])\n",
            False,
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    runner = subprocess.run\n"
            "    def inner():\n"
            "        runner\n"
            "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n",
            True,
        ),
        (
            "def outer():\n"
            "    runner = print\n"
            "    def inner():\n"
            "        runner\n"
            "        locals()['runner'](['docker', 'diagnostic'])\n"
            "    inner()\n"
            "outer()\n",
            False,
        ),
        (
            "import subprocess\nrunner = subprocess.run\n"
            "def invoke():\n"
            "    locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n",
            False,
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    runner = subprocess.run\n"
            "    def inner():\n"
            "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n",
            False,
        ),
        (
            "import subprocess\n"
            "list(filter(subprocess.run, "
            "[['docker', 'pull', 'alpine:latest']]))\n",
            True,
        ),
        (
            "import subprocess\n"
            "sorted([['docker', 'pull', 'alpine:latest']], key=subprocess.run)\n",
            True,
        ),
        (
            "list(filter(print, [['docker', 'diagnostic']]))\n",
            False,
        ),
        (
            "import subprocess\nsorted([['git', 'status']], key=subprocess.run)\n",
            False,
        ),
        (
            "import subprocess\n"
            "holder = dict(runner=subprocess.run)\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = tuple([subprocess.run])\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "holder = dict(runner=print)\nholder['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.methodcaller('run', ['docker', 'pull', 'alpine:latest'])"
            ".__call__(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.methodcaller('__call__', "
            "['docker', 'pull', 'alpine:latest'])(subprocess.run)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "getrun = operator.attrgetter('run')\n"
            "getrun(subprocess)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.attrgetter('run').__call__(subprocess)"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, command):\n"
            "        print(command)\n"
            "getrun = operator.attrgetter('run')\n"
            "getrun(LocalModule())(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, command):\n"
            "        print(command)\n"
            "operator.attrgetter('run').__call__(LocalModule())"
            "(['docker', 'diagnostic'])\n",
            False,
        ),
    ],
    ids=[
        "vars-process",
        "builtins-vars-process",
        "vars-dunder-call-process",
        "builtins-vars-dunder-call-process",
        "locals-dunder-call-process",
        "higher-order-vars-process",
        "higher-order-vars-safe",
        "shadowed-vars-safe",
        "vars-safe-callable",
        "vars-safe-payload",
        "locals-free-process",
        "locals-free-safe",
        "locals-global-not-captured-safe",
        "locals-outer-not-captured-safe",
        "filter-process",
        "sorted-process",
        "filter-safe-callable",
        "sorted-safe-payload",
        "dict-constructor-process",
        "tuple-constructor-process",
        "dict-constructor-safe",
        "methodcaller-dunder-call",
        "methodcaller-calls-process-callable",
        "attrgetter-process",
        "attrgetter-dunder-call",
        "attrgetter-safe-owner",
        "attrgetter-dunder-call-safe",
    ],
)
def test_gate_boundary_python_covers_remaining_dynamic_callable_forms(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import operator\nimport subprocess\n"
            "operator.methodcaller('run', ['docker', 'pull', 'alpine:latest'])"
            ".__call__.__call__(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.call(operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest']), subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "list(map(operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest']), [subprocess]))\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operator.attrgetter('run').__call__.__call__(subprocess)"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "getrun = operator.attrgetter('run')\n"
            "operator.call(getrun, subprocess)"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "list(filter.__call__(subprocess.run, "
            "[['docker', 'pull', 'alpine:latest']]))\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "list(operator.call(filter, subprocess.run, "
            "[['docker', 'pull', 'alpine:latest']]))\n",
            True,
        ),
        (
            "import subprocess\n"
            "sorted.__call__([['docker', 'pull', 'alpine:latest']], "
            "key=subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "sorted([['docker', 'pull', 'alpine:latest']], "
            "**{'key': subprocess.run})\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = dict.__call__(runner=subprocess.run)\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "holder = operator.call(dict, runner=subprocess.run)\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = dict.fromkeys(['runner'], subprocess.run)\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = tuple(iter([subprocess.run]))\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        ("list(filter.__call__(print, [['docker', 'diagnostic']]))\n", False),
        (
            "sorted.__call__([['git', 'status']], key=print)\n",
            False,
        ),
        (
            "sorted([['git', 'status']], **{'key': print})\n",
            False,
        ),
        (
            "holder = dict.__call__(runner=print)\nholder['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = dict.fromkeys(['runner'], print)\n"
            "holder['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = tuple(iter([print]))\nholder[0](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "operator.attrgetter('run').__call__.__call__(LocalModule())"
            "(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "operator.call(operator.methodcaller('run', "
            "['docker', 'diagnostic']), LocalModule())\n",
            False,
        ),
    ],
    ids=[
        "methodcaller-double-dunder-call",
        "operator-call-methodcaller",
        "map-methodcaller",
        "attrgetter-double-dunder-call",
        "operator-call-attrgetter",
        "filter-dunder-call",
        "operator-call-filter",
        "sorted-dunder-call",
        "sorted-expanded-keyword",
        "dict-dunder-call",
        "operator-call-dict",
        "dict-fromkeys",
        "tuple-iter",
        "filter-dunder-call-safe",
        "sorted-dunder-call-safe",
        "sorted-expanded-keyword-safe",
        "dict-dunder-call-safe",
        "dict-fromkeys-safe",
        "tuple-iter-safe",
        "attrgetter-double-dunder-call-safe",
        "operator-call-methodcaller-safe",
    ],
)
def test_gate_boundary_python_normalizes_nested_builtin_callables(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\nruntime_vars = vars\n"
            "def invoke(runner):\n"
            "    runtime_vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import builtins\nimport subprocess\nruntime_vars = builtins.vars\n"
            "def invoke(runner):\n"
            "    runtime_vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\nruntime_locals = locals\n"
            "def invoke(runner):\n"
            "    runtime_locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    runner = subprocess.run\n"
            "    def inner():\n"
            "        nonlocal runner\n"
            "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n",
            True,
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    runner = subprocess.run\n"
            "    def inner():\n"
            "        def nested():\n"
            "            return runner\n"
            "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n",
            True,
        ),
        (
            "import subprocess\n"
            "def options():\n"
            "    return {}\n"
            "sorted([['docker', 'pull', 'alpine:latest']], "
            "**options(), **{'key': subprocess.run})\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = tuple(map(lambda value: value, [subprocess.run]))\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = list(filter(None, [subprocess.run]))\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = dict(zip(['runner'], "
            "map(lambda value: value, [subprocess.run])))\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "def make():\n"
            "    return operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest'])\n"
            "make()(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "def make():\n"
            "    return operator.attrgetter('run')\n"
            "make()(subprocess)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operations = [operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest'])]\n"
            "operations[0](subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operations = [operator.attrgetter('run')]\n"
            "operations[0](subprocess)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "runtime_vars = vars\n"
            "def invoke(runner):\n"
            "    runtime_vars()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "def outer():\n"
            "    runner = print\n"
            "    def inner():\n"
            "        nonlocal runner\n"
            "        locals()['runner'](['docker', 'diagnostic'])\n"
            "    inner()\n"
            "outer()\n",
            False,
        ),
        (
            "def outer():\n"
            "    runner = print\n"
            "    def inner():\n"
            "        def nested():\n"
            "            return runner\n"
            "        locals()['runner'](['docker', 'diagnostic'])\n"
            "    inner()\n"
            "outer()\n",
            False,
        ),
        (
            "def options():\n"
            "    return {}\n"
            "sorted([['git', 'status']], **options(), **{'key': print})\n",
            False,
        ),
        (
            "holder = tuple(map(lambda value: value, [print]))\n"
            "holder[0](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = list(filter(None, [print]))\nholder[0](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = dict(zip(['runner'], map(lambda value: value, [print])))\n"
            "holder['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "def make():\n"
            "    return operator.methodcaller('run', ['docker', 'diagnostic'])\n"
            "make()(LocalModule())\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "def make():\n"
            "    return operator.attrgetter('run')\n"
            "make()(LocalModule())(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "operations = [operator.methodcaller('run', ['docker', 'diagnostic'])]\n"
            "operations[0](LocalModule())\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "operations = [operator.attrgetter('run')]\n"
            "operations[0](LocalModule())(['docker', 'diagnostic'])\n",
            False,
        ),
    ],
    ids=[
        "aliased-vars",
        "aliased-builtins-vars",
        "aliased-locals",
        "nonlocal-cell",
        "nested-relayed-cell",
        "sorted-multiple-expanded-keywords",
        "tuple-map",
        "list-filter",
        "dict-zip-map",
        "returned-methodcaller",
        "returned-attrgetter",
        "stored-methodcaller",
        "stored-attrgetter",
        "aliased-vars-safe",
        "nonlocal-cell-safe",
        "nested-relayed-cell-safe",
        "sorted-multiple-expanded-keywords-safe",
        "tuple-map-safe",
        "list-filter-safe",
        "dict-zip-map-safe",
        "returned-methodcaller-safe",
        "returned-attrgetter-safe",
        "stored-methodcaller-safe",
        "stored-attrgetter-safe",
    ],
)
def test_gate_boundary_python_covers_aliased_and_transformed_callables(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import operator\nimport subprocess\n"
            "invoke = operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest']).__call__\n"
            "invoke(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "getrun = operator.attrgetter('run').__call__\n"
            "getrun(subprocess)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = tuple(subprocess.run for _ in [0])\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "holder = dict((('runner', subprocess.run) for _ in [0]))\n"
            "holder['runner'](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "invoke = operator.methodcaller('run', ['docker', 'diagnostic']).__call__\n"
            "invoke(LocalModule())\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "getrun = operator.attrgetter('run').__call__\n"
            "getrun(LocalModule())(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = tuple(print for _ in [0])\nholder[0](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "holder = dict((('runner', print) for _ in [0]))\n"
            "holder['runner'](['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import subprocess\n"
            "def filter(callback, values):\n"
            "    return []\n"
            "list(filter(subprocess.run, "
            "[['docker', 'pull', 'alpine:latest']]))\n",
            False,
        ),
        (
            "import subprocess\n"
            "def sorted(values, *, key):\n"
            "    return values\n"
            "sorted([['docker', 'pull', 'alpine:latest']], key=subprocess.run)\n",
            False,
        ),
    ],
    ids=[
        "aliased-methodcaller-dunder-call",
        "aliased-attrgetter-dunder-call",
        "tuple-generator",
        "dict-generator",
        "aliased-methodcaller-dunder-call-safe",
        "aliased-attrgetter-dunder-call-safe",
        "tuple-generator-safe",
        "dict-generator-safe",
        "shadowed-filter-safe",
        "shadowed-sorted-safe",
    ],
)
def test_gate_boundary_python_handles_operator_aliases_and_generators(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    runtime_vars = vars\n"
            "    runtime_vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import builtins\nimport subprocess\n"
            "def invoke(runner):\n"
            "    runtime_vars = builtins.vars\n"
            "    runtime_vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    runtime_locals = locals\n"
            "    runtime_locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def options():\n"
            "    return {'key': subprocess.run}\n"
            "sorted([['docker', 'pull', 'alpine:latest']], **options())\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "def make1():\n"
            "    return operator.methodcaller('run', "
            "['docker', 'pull', 'alpine:latest'])\n"
            "def make2():\n"
            "    return make1()\n"
            "make2()(subprocess)\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "def make1():\n"
            "    return operator.attrgetter('run')\n"
            "def make2():\n"
            "    return make1()\n"
            "make2()(subprocess)(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def invoke(runner):\n"
            "    runtime_vars = vars\n"
            "    runtime_vars()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "import builtins\n"
            "def invoke(runner):\n"
            "    runtime_vars = builtins.vars\n"
            "    runtime_vars()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "def invoke(runner):\n"
            "    runtime_locals = locals\n"
            "    runtime_locals()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "def options():\n    return {'key': print}\nsorted([['git', 'status']], **options())\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "def make1():\n"
            "    return operator.methodcaller('run', ['docker', 'diagnostic'])\n"
            "def make2():\n"
            "    return make1()\n"
            "make2()(LocalModule())\n",
            False,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "def make1():\n"
            "    return operator.attrgetter('run')\n"
            "def make2():\n"
            "    return make1()\n"
            "make2()(LocalModule())(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "import subprocess\nholder = tuple(map(repr, [subprocess.run]))\nprint(holder[0])\n",
            False,
        ),
        (
            "import subprocess\n"
            "holder = list(filter(lambda value: False, [subprocess.run]))\n"
            "print(holder)\n",
            False,
        ),
    ],
    ids=[
        "local-aliased-vars",
        "local-aliased-builtins-vars",
        "local-aliased-locals",
        "sorted-returned-options",
        "chained-methodcaller-factory",
        "chained-attrgetter-factory",
        "local-aliased-vars-safe",
        "local-aliased-builtins-vars-safe",
        "local-aliased-locals-safe",
        "sorted-returned-options-safe",
        "chained-methodcaller-factory-safe",
        "chained-attrgetter-factory-safe",
        "map-repr-safe",
        "filter-false-safe",
    ],
)
def test_gate_boundary_python_handles_local_aliases_and_static_transforms(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    runtime_vars = vars\n"
            "    namespace = runtime_vars\n"
            "    namespace()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def invoke(namespace, runner):\n"
            "    namespace()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(vars, subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    runner = subprocess.run\n"
            "    def inner():\n"
            "        class Relay:\n"
            "            runner = print\n"
            "            def nested(self):\n"
            "                return runner\n"
            "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n",
            True,
        ),
        (
            "def invoke(runner):\n"
            "    runtime_vars = vars\n"
            "    namespace = runtime_vars\n"
            "    namespace()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "def invoke(namespace, runner):\n"
            "    namespace()['runner'](['docker', 'diagnostic'])\n"
            "invoke(vars, print)\n",
            False,
        ),
        (
            "def outer():\n"
            "    runner = print\n"
            "    def inner():\n"
            "        class Relay:\n"
            "            runner = print\n"
            "            def nested(self):\n"
            "                return runner\n"
            "        locals()['runner'](['docker', 'diagnostic'])\n"
            "    inner()\n"
            "outer()\n",
            False,
        ),
    ],
    ids=[
        "local-namespace-alias-chain",
        "local-namespace-parameter",
        "class-relayed-cell",
        "local-namespace-alias-chain-safe",
        "local-namespace-parameter-safe",
        "class-relayed-cell-safe",
    ],
)
def test_gate_boundary_python_models_namespace_alias_chains_and_class_cells(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "def invoke():\n"
            "    [None for filter in ()]\n"
            "    list(filter(subprocess.run, "
            "[['docker', 'pull', 'alpine:latest']]))\n"
            "invoke()\n"
        ),
        (
            "import subprocess\n"
            "def invoke():\n"
            "    [None for sorted in ()]\n"
            "    sorted([['docker', 'pull', 'alpine:latest']], key=subprocess.run)\n"
            "invoke()\n"
        ),
        (
            "import subprocess\n"
            "def invoke():\n"
            "    [None for vars in ()]\n"
            "    runner = subprocess.run\n"
            "    vars()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n"
        ),
        (
            "import subprocess\n"
            "runner = subprocess.run\n"
            "def invoke():\n"
            "    [None for globals in ()]\n"
            "    globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n"
        ),
    ],
    ids=["filter", "sorted", "vars", "globals"],
)
def test_gate_boundary_python_does_not_leak_comprehension_targets(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "binding",
    ["filter = safe_filter", "[(filter := safe_filter) for _ in [0]]"],
    ids=["assignment", "comprehension-named-expression"],
)
def test_gate_boundary_python_preserves_real_local_filter_shadowing(
    checker: ModuleType | _MissingChecker, tmp_path: Path, binding: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def safe_filter(callback, values):\n"
        "    return ()\n"
        "def invoke():\n"
        f"    {binding}\n"
        "    list(filter(subprocess.run, "
        "[['docker', 'pull', 'alpine:latest']]))\n"
        "invoke()\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize("element", ["runner", "None"], ids=["shadowed-load", "control"])
def test_gate_boundary_python_does_not_capture_comprehension_targets_as_freevars(
    checker: ModuleType | _MissingChecker, tmp_path: Path, element: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def outer():\n"
        "    runner = subprocess.run\n"
        "    def inner():\n"
        f"        [{element} for runner in ()]\n"
        "        locals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        "    inner()\n"
        "outer()\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "[(runner := subprocess.run) for _ in [0]]\n"
            "runner(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\n"
            "def invoke():\n"
            "    [(runner := subprocess.run) for _ in [0]]\n"
            "    runner(['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n"
        ),
        (
            "import subprocess\n"
            "def outer():\n"
            "    def inner():\n"
            "        [(runner := subprocess.run) for _ in [0]]\n"
            "        runner(['docker', 'pull', 'alpine:latest'])\n"
            "    inner()\n"
            "outer()\n"
        ),
    ],
    ids=["module", "function", "nested-function"],
)
def test_gate_boundary_python_preserves_comprehension_named_expression_bindings(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import operator\nimport subprocess\n"
            "operator.attrgetter('call', 'run')(subprocess)[-1]"
            "(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import operator\nimport subprocess\n"
            "factory = operator.attrgetter\n"
            "factory('call', 'run')(subprocess)[-1]"
            "(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["direct", "factory-alias"],
)
def test_gate_boundary_python_tracks_negative_multiselect_attrgetter_indexes(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\n"
            "def invoke():\n"
            "    runner = subprocess.run\n"
            "    (namespace := vars)()['runner']"
            "(['docker', 'pull', 'alpine:latest'])\n"
            "invoke()\n",
            True,
        ),
        (
            "import subprocess\n"
            "runner = subprocess.run\n"
            "(namespace := vars)()['runner']"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def invoke():\n"
            "    runner = print\n"
            "    (namespace := vars)()['runner'](['docker', 'diagnostic'])\n"
            "invoke()\n",
            False,
        ),
    ],
    ids=["local", "module", "safe"],
)
def test_gate_boundary_python_tracks_walrus_runtime_namespace_aliases(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("constructor", "condition"),
    [
        ("tuple", "[]"),
        ("tuple", "()"),
        ("tuple", "{}"),
        ("tuple", "set()"),
        ("list", "[]"),
        ("list", "()"),
        ("list", "{}"),
        ("list", "set()"),
    ],
    ids=[
        "generator-list",
        "generator-tuple",
        "generator-dict",
        "generator-set-call",
        "listcomp-list",
        "listcomp-tuple",
        "listcomp-dict",
        "listcomp-set-call",
    ],
)
def test_gate_boundary_python_allows_statically_false_comprehension_filters(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    constructor: str,
    condition: str,
) -> None:
    _write_valid_repo(tmp_path)
    comprehension = f"subprocess.run for _ in [0] if {condition}"
    holder = f"tuple({comprehension})" if constructor == "tuple" else f"tuple([{comprehension}])"
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nholder = {holder}\nprint(holder)\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_fails_closed_on_opaque_comprehension_filters(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "from runtime_config import enabled\n"
        "holder = tuple(subprocess.run for _ in [0] if enabled())\n"
        "print(holder)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "definition",
    [
        "        callback = lambda value=runner: value\n",
        "        callback = lambda *, value=runner: value\n",
        "        def callback(value=runner):\n            return value\n",
        "        def callback(*, value=runner):\n            return value\n",
        "        def callback(value: runner):\n            return value\n",
        "        @capture(runner)\n        def callback():\n            return None\n",
    ],
    ids=[
        "lambda-default",
        "lambda-kw-default",
        "nested-def-default",
        "nested-def-kw-default",
        "nested-def-annotation",
        "nested-def-decorator",
    ],
)
@pytest.mark.parametrize(
    ("runner", "payload", "expected_errors"),
    [
        ("subprocess.run", "['docker', 'pull', 'alpine:latest']", True),
        ("print", "['docker', 'diagnostic']", False),
    ],
    ids=["dangerous", "safe"],
)
def test_gate_boundary_python_tracks_definition_time_freevars_in_nested_scopes(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    definition: str,
    runner: str,
    payload: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "def capture(value):\n"
        "    def decorate(function):\n"
        "        return function\n"
        "    return decorate\n"
        "def outer():\n"
        f"    runner = {runner}\n"
        "    def inner():\n"
        f"{definition}"
        f"        locals()['runner']({payload})\n"
        "    inner()\n"
        "outer()\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("comprehension", "expected_errors"),
    [
        ("[(runner := subprocess.run) for _ in []]", False),
        ("[(runner := subprocess.run) for _ in [0] if False]", False),
        ("[None for _ in [0] if (runner := subprocess.run) if False]", True),
    ],
    ids=["empty-iterable", "false-before-walrus", "walrus-before-false"],
)
def test_gate_boundary_python_respects_comprehension_walrus_reachability(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    comprehension: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{comprehension}\nrunner(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("preamble", "condition", "expected_errors"),
    [
        ("", "list()", False),
        ("", "tuple()", False),
        ("", "dict()", False),
        ("", "frozenset()", False),
        ("def list():\n    return [1]\n", "list()", True),
        ("def tuple():\n    return (1,)\n", "tuple()", True),
        ("def dict():\n    return {'value': 1}\n", "dict()", True),
        ("def frozenset():\n    return {1}\n", "frozenset()", True),
        ("", "list([1])", True),
        ("", "tuple([1])", True),
        ("", "dict(value=1)", True),
        ("", "frozenset([1])", True),
    ],
    ids=[
        "empty-list",
        "empty-tuple",
        "empty-dict",
        "empty-frozenset",
        "shadowed-list",
        "shadowed-tuple",
        "shadowed-dict",
        "shadowed-frozenset",
        "argument-list",
        "argument-tuple",
        "argument-dict",
        "argument-frozenset",
    ],
)
def test_gate_boundary_python_models_empty_builtin_comprehension_conditions(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    preamble: str,
    condition: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import builtins\nimport subprocess\n"
        f"{preamble}"
        f"holder = builtins.tuple(subprocess.run for _ in [0] if {condition})\n"
        "print(holder)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("preamble", "iterable", "expected_errors"),
    [
        ("", "list()", False),
        ("", "tuple()", False),
        ("", "dict()", False),
        ("", "set()", False),
        ("", "frozenset()", False),
        ("import builtins\n", "builtins.list()", False),
        ("import builtins\n", "builtins.tuple()", False),
        ("import builtins\n", "builtins.dict()", False),
        ("import builtins\n", "builtins.set()", False),
        ("import builtins\n", "builtins.frozenset()", False),
        ("from builtins import list as factory\n", "factory()", False),
        ("from builtins import tuple as factory\n", "factory()", False),
        ("from builtins import dict as factory\n", "factory()", False),
        ("from builtins import set as factory\n", "factory()", False),
        ("from builtins import frozenset as factory\n", "factory()", False),
        ("def list():\n    return [0]\n", "list()", True),
        ("def tuple():\n    return (0,)\n", "tuple()", True),
        ("def dict():\n    return {'value': 0}\n", "dict()", True),
        ("def set():\n    return {0}\n", "set()", True),
        ("def frozenset():\n    return {0}\n", "frozenset()", True),
        ("", "list([0])", True),
        ("", "tuple([0])", True),
        ("", "dict(value=0)", True),
        ("", "set([0])", True),
        ("", "frozenset([0])", True),
    ],
    ids=[
        "plain-list",
        "plain-tuple",
        "plain-dict",
        "plain-set",
        "plain-frozenset",
        "builtins-list",
        "builtins-tuple",
        "builtins-dict",
        "builtins-set",
        "builtins-frozenset",
        "imported-list",
        "imported-tuple",
        "imported-dict",
        "imported-set",
        "imported-frozenset",
        "shadowed-list",
        "shadowed-tuple",
        "shadowed-dict",
        "shadowed-set",
        "shadowed-frozenset",
        "argument-list",
        "argument-tuple",
        "argument-dict",
        "argument-set",
        "argument-frozenset",
    ],
)
def test_gate_boundary_python_models_empty_builtin_comprehension_iterables(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    preamble: str,
    iterable: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        f"{preamble}"
        f"[(runner := subprocess.run) for _ in {iterable}]\n"
        "runner(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import operator\nimport subprocess\n"
            "operator.attrgetter('run', 'call')(subprocess)[0]"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\nimport subprocess\n"
            "operations = operator.attrgetter('run', 'call')(subprocess)\n"
            "operations[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import operator\n"
            "class LocalModule:\n"
            "    def run(self, value):\n"
            "        print(value)\n"
            "    def other(self, value):\n"
            "        print(value)\n"
            "operator.attrgetter('run', 'other')(LocalModule())[0]"
            "(['docker', 'diagnostic'])\n",
            False,
        ),
    ],
    ids=["direct", "stored", "safe-owner"],
)
def test_gate_boundary_python_tracks_multiselect_attrgetter_results(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    from builtins import vars as namespace\n"
            "    namespace()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "import subprocess\n"
            "def invoke(runner):\n"
            "    namespace, other = vars, print\n"
            "    namespace()['runner'](['docker', 'pull', 'alpine:latest'])\n"
            "invoke(subprocess.run)\n",
            True,
        ),
        (
            "def invoke(runner):\n"
            "    from builtins import vars as namespace\n"
            "    namespace()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "def invoke(runner):\n"
            "    namespace, other = vars, print\n"
            "    namespace()['runner'](['docker', 'diagnostic'])\n"
            "invoke(print)\n",
            False,
        ),
        (
            "import subprocess\n"
            "holder = tuple(value for value in [subprocess.run])\n"
            "holder[0](['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "import subprocess\n"
            "def options():\n"
            "    return {**{'key': subprocess.run}}\n"
            "sorted([['docker', 'pull', 'alpine:latest']], **options())\n",
            True,
        ),
        (
            "from runtime_config import options\n"
            "sorted([['docker', 'pull', 'alpine:latest']], **options)\n",
            True,
        ),
    ],
    ids=[
        "imported-namespace",
        "unpacked-namespace",
        "imported-namespace-safe",
        "unpacked-namespace-safe",
        "generator-identity",
        "nested-expanded-keyword",
        "opaque-expanded-keyword",
    ],
)
def test_gate_boundary_python_tracks_additional_runtime_storage_forms(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nholder = tuple(subprocess.run for _ in [])\nprint(holder)\n",
        "import subprocess\nholder = tuple([subprocess.run for _ in []])\nprint(holder)\n",
        (
            "import subprocess\n"
            "holder = tuple(subprocess.run for _ in [0] if False)\n"
            "print(holder)\n"
        ),
        "import subprocess\nholder = tuple(map(bool, [subprocess.run]))\nprint(holder)\n",
        (
            "import subprocess\n"
            "holder = list(filter(lambda value: 0, [subprocess.run]))\n"
            "print(holder)\n"
        ),
    ],
    ids=["empty-generator", "empty-listcomp", "false-filter", "map-bool", "filter-zero"],
)
def test_gate_boundary_python_allows_statically_empty_callable_transforms(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_fails_closed_on_excessive_callable_factory_depth(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    definitions = ["def factory_0():\n    return subprocess.run\n"]
    definitions.extend(
        f"def factory_{index}():\n    return factory_{index - 1}()\n" for index in range(1, 400)
    )
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        + "".join(definitions)
        + "factory_399()(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nops = []\n"
            "ops.extend({subprocess.run: None})\n"
            "ops[0](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nops = []\nops += {subprocess.run: None}\n"
            "ops[0](['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["extend-dict-keys", "augassign-dict-keys"],
)
def test_gate_boundary_python_rejects_callable_dict_keys_stored_as_iterable(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        ("import common\nrunner = common.invoke\nrunner(['docker', 'pull', 'alpine:latest'])\n"),
        (
            "from common import invoke\nops = {'run': invoke}\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "from common import invoke\nops = [invoke]\n"
            "ops[0](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "from common import invoke\nclass Ops:\n    pass\nops = Ops()\n"
            "ops.run = invoke\nops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "from common import invoke\nops = {}\nops.setdefault('run', invoke)\n"
            "ops['run'](['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["attribute-alias", "mapping", "list", "stored-attribute", "setdefault"],
)
def test_gate_boundary_python_rejects_stored_local_import_callable_boundary(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text(
        "import subprocess\ndef invoke(cmd):\n    subprocess.run(cmd)\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\nclass State:\n    pass\nstate = State()\n"
            "state.cmd = ['git', 'status']\n"
            "def configure(value):\n    global state\n    state.cmd = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(state.cmd)\n"
        ),
        (
            "import subprocess\nstate = {'cmd': ['git', 'status']}\n"
            "def configure(value):\n    global state\n    state['cmd'] = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\n"
            "subprocess.run(state['cmd'])\n"
        ),
        (
            "import subprocess\ndef outer():\n    class State:\n        pass\n"
            "    state = State()\n    state.cmd = ['git', 'status']\n"
            "    def configure(value):\n        nonlocal state\n"
            "        state.cmd = value\n"
            "    configure(['docker', 'pull', 'alpine:latest'])\n"
            "    subprocess.run(state.cmd)\nouter()\n"
        ),
    ],
    ids=["global-attribute", "global-subscript", "nonlocal-attribute"],
)
def test_gate_boundary_python_rejects_external_scope_path_mutations_at_sink(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ndef factory():\n"
            "    def make():\n        return subprocess.run\n    return make\n"
            "factory()()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nfactory = lambda: subprocess.run\n"
            "factory()(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef factory(flag):\n"
            "    return subprocess.run if flag else print\n"
            "factory(True)(['docker', 'pull', 'alpine:latest'])\n"
        ),
        "import subprocess\n(subprocess.run,)[0](['docker', 'pull', 'alpine:latest'])\n",
        (
            "import subprocess\nfrom functools import cached_property\nclass Factory:\n"
            "    @cached_property\n    def runner(self):\n        return subprocess.run\n"
            "Factory().runner(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\ndef factory():\n"
            "    def make():\n        def decorate(function):\n"
            "            def invoke(cmd):\n                subprocess.run(cmd)\n"
            "            return invoke\n        return decorate\n    return make\n"
            "@factory()()\ndef benign(cmd):\n    print(cmd)\n"
            "benign(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=[
        "nested-ordinary-factory",
        "lambda-factory",
        "ifexp-return",
        "tuple-subscript",
        "cached-property",
        "third-order-decorator",
    ],
)
def test_gate_boundary_python_rejects_recursive_structured_callable_factories(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "factory = lambda: print\nfactory()(['docker', 'diagnostic'])\n",
        "(print,)[0](['docker', 'diagnostic'])\n",
        (
            "from functools import cached_property\nclass Factory:\n"
            "    @cached_property\n    def runner(self):\n        return print\n"
            "Factory().runner(['docker', 'diagnostic'])\n"
        ),
    ],
    ids=["benign-lambda-factory", "benign-tuple-subscript", "benign-cached-property"],
)
def test_gate_boundary_python_allows_benign_structured_callable_factory_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def unused():\n    global cmd\n"
            "    cmd = ['docker', 'pull', 'alpine:latest']\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\n"
            "cmd = ['git', 'status']\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "configure(['git', 'status'])\nsubprocess.run(cmd)\n"
        ),
    ],
    ids=["unused-effect", "safe-overwrite-after-effect", "safe-effect-payload"],
)
def test_gate_boundary_python_applies_external_scope_effects_only_when_reachable(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("shebang", "body", "expected_errors"),
    [
        ("#!/usr/bin/env -S FOO='x y' bash", "docker pull alpine:latest", True),
        (r"#!/usr/bin/env -S bash\c ignored", "docker pull alpine:latest", True),
        ("#!/usr/bin/env -S FOO='x bash' python3", "print('safe')", False),
    ],
    ids=["quoted-assignment-shell", "stop-escape-shell", "quoted-assignment-python"],
)
def test_gate_boundary_shell_preserves_raw_gnu_env_split_string_shebang(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    shebang: str,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release.command").write_text(f"{shebang}\n{body}\n")

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\ndef make():\n"
            "    return {'args': ['docker', 'pull', 'alpine:latest']}\n"
            "subprocess.run(**make())\n"
        ),
        (
            "import subprocess\ndef identity(value):\n    return value\n"
            "identity(subprocess.run)(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nimport shlex\n"
            "subprocess.run(shlex.split('docker pull alpine:latest'))\n"
        ),
    ],
    ids=["opaque-kwargs", "identity-callable", "shlex-split"],
)
def test_gate_boundary_python_rejects_terminal_dynamic_process_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nsubprocess.run(**{'args': ['git', 'status']})\n",
        ("def identity(value):\n    return value\nidentity(print)(['docker', 'diagnostic'])\n"),
        "import shlex\nimport subprocess\nsubprocess.run(shlex.split('git status'))\n",
    ],
    ids=["literal-safe-kwargs", "safe-identity-callable", "safe-shlex-split"],
)
def test_gate_boundary_python_allows_safe_terminal_dynamic_process_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


def _deep_callable_factory_body(terminal: str, levels: int = 10) -> str:
    definitions = [f"def f0():\n    return {terminal}\n"]
    definitions.extend(f"def f{level}():\n    return f{level - 1}\n" for level in range(1, levels))
    invocation = f"f{levels - 1}" + "()" * levels
    return "".join(definitions) + f"{invocation}(['docker', 'pull', 'alpine:latest'])\n"


def test_gate_boundary_python_rejects_deep_callable_factory_without_truncation(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    body = "import subprocess\n" + _deep_callable_factory_body("subprocess.run")
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_deep_benign_callable_factory_control(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(_deep_callable_factory_body("print"))

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import functools\nimport subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "functools.partial(configure, ['docker', 'pull', 'alpine:latest'])()\n"
            "subprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "apply = lambda: configure(['docker', 'pull', 'alpine:latest'])\n"
            "apply()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def apply():\n    configure(['docker', 'pull', 'alpine:latest'])\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "apply()\nsubprocess.run(cmd)\n"
        ),
    ],
    ids=["partial-effect", "lambda-effect", "forward-wrapper-effect"],
)
def test_gate_boundary_python_composes_external_effect_wrappers(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import functools\nimport subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "functools.partial(configure, ['git', 'status'])()\nsubprocess.run(cmd)\n"
        ),
        (
            "import subprocess\ncmd = ['git', 'status']\n"
            "def configure(value):\n    global cmd\n    cmd = value\n"
            "apply = lambda: configure(['docker', 'pull', 'alpine:latest'])\n"
            "subprocess.run(cmd)\n"
        ),
    ],
    ids=["safe-partial-effect", "unused-lambda-effect"],
)
def test_gate_boundary_python_allows_safe_external_effect_wrapper_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import operator\nimport subprocess\nclass Ops:\n    pass\nops = Ops()\n"
            "operator.attrgetter('__setattr__')(ops)('run', subprocess.run)\n"
            "ops.run(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import subprocess\nops = []\nops.__iadd__([subprocess.run])\n"
            "ops[0](['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["attrgetter-setattr", "dunder-iadd"],
)
def test_gate_boundary_python_rejects_remaining_reflected_callable_storage(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        (
            "import operator\nclass Ops:\n    pass\nops = Ops()\n"
            "operator.attrgetter('__setattr__')(ops)('run', print)\n"
            "ops.run(['docker', 'diagnostic'])\n"
        ),
        "ops = []\nops.__iadd__([print])\nops[0](['docker', 'diagnostic'])\n",
    ],
    ids=["safe-attrgetter-setattr", "safe-dunder-iadd"],
)
def test_gate_boundary_python_allows_reflected_callable_storage_controls(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("payload", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker-payload", "safe-payload"],
)
def test_gate_boundary_python_tracks_reflected_local_import_callable_boundary(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    payload: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text("def invoke(cmd):\n    print(cmd)\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import common\ngetattr(common, 'invoke')({payload})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        ("import os\nexe = '%s%s' % ('doc', 'ker')\nos.system('%s pull alpine:latest' % exe)\n"),
        ("import os\nparts = ('docker',)\nos.system('%s pull alpine:latest' % (*parts,))\n"),
    ],
    ids=["nested-percent-binop", "starred-percent-tuple"],
)
def test_gate_boundary_python_rejects_recursive_percent_operands(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_allows_recursive_safe_percent_operand_control(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import os\nexe = '%s%s' % ('g', 'it')\nos.system('%s status' % exe)\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_composes_direct_and_forward_external_effects(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\nother = ['git', 'status']\n"
        "def apply(value):\n    global other\n    other = value\n    configure(value)\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "apply(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("first", "second", "expected_errors"),
    [
        ("cmd = ['docker', 'pull', 'alpine:latest']", "configure(['git', 'status'])", False),
        ("configure(['git', 'status'])", "cmd = ['docker', 'pull', 'alpine:latest']", True),
    ],
    ids=["direct-then-forward", "forward-then-direct"],
)
def test_gate_boundary_python_preserves_interleaved_direct_and_forward_effect_order(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    first: str,
    second: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        f"def apply():\n    global cmd\n    {first}\n    {second}\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "apply()\nsubprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("first", "second", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
    ],
    ids=["dangerous-then-safe", "safe-then-dangerous"],
)
def test_gate_boundary_python_preserves_forward_external_effect_order(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    first: str,
    second: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        f"def apply():\n    configure({first})\n    configure({second})\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "apply()\nsubprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_python_allows_benign_recursive_callable_factory_cycle(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "def loop(value):\n    return loop(value)\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "import subprocess\nfrom runtime_config import options\nsubprocess.run(**options)\n",
            True,
        ),
        (
            "import subprocess\ndef invoke(options):\n    subprocess.run(**options)\n"
            "invoke({'args': ['git', 'status']})\n",
            False,
        ),
    ],
    ids=["terminal-imported-kwargs", "deferred-wrapper-parameter"],
)
def test_gate_boundary_python_rejects_terminal_opaque_kwargs_but_defers_wrapper_parameters(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_python_keeps_dangerous_effect_when_conditional_forward_cannot_overwrite(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def apply(flag):\n"
        "    configure(['docker', 'pull', 'alpine:latest'])\n"
        "    if flag:\n        configure(['git', 'status'])\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "apply(False)\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        "cmd = []\nlist.extend(cmd, ['docker', 'pull', 'alpine:latest'])\nsubprocess.run(cmd)",
        "options = {}\ndict.update(options, {'args': ['docker', 'pull', 'alpine:latest']})\n"
        "subprocess.run(**options)",
        "class Holder:\n    pass\nobj = Holder()\n"
        "setattr(obj, 'cmd', ['docker', 'pull', 'alpine:latest'])\nsubprocess.run(obj.cmd)",
    ],
    ids=["unbound-list-extend", "unbound-dict-update", "builtin-setattr"],
)
def test_gate_boundary_python_rejects_unbound_and_reflected_payload_mutations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, mutation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(f"import subprocess\n{mutation}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "invocation",
    [
        "subprocess.run.__call__(COMMAND)",
        "asyncio.to_thread(subprocess.run, COMMAND)",
        "list(map(subprocess.run, [COMMAND]))",
        "concurrent.futures.ThreadPoolExecutor().submit(subprocess.run, COMMAND)",
    ],
    ids=["dunder-call", "asyncio-to-thread", "map", "executor-submit"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_indirect_process_callables(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    invocation: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    body = invocation.replace("COMMAND", command)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import asyncio\nimport concurrent.futures\nimport subprocess\n{body}\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "invocation",
    [
        "__import__('subprocess').run(COMMAND)",
        "importlib.import_module('subprocess').run(COMMAND)",
        "getattr(subprocess, 'r' + 'un')(COMMAND)",
        "operator.attrgetter('run')(subprocess)(COMMAND)",
    ],
    ids=["dunder-import", "importlib", "getattr", "attrgetter"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_dynamic_process_module_access(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    invocation: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    body = invocation.replace("COMMAND", command)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import importlib\nimport operator\nimport subprocess\n{body}\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        ("trap 'docker pull alpine:latest' EXIT", True),
        ("trap -- 'docker run --rm alpine:latest true' ERR", True),
        ("payload='docker pull alpine:latest'\ntrap \"$payload\" EXIT", True),
        ("trap 'git status' EXIT", False),
        ("trap - EXIT", False),
    ],
    ids=["literal", "double-dash", "variable", "safe", "reset"],
)
def test_gate_boundary_shell_scans_trap_payloads(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.sh").write_text(f"#!/usr/bin/env bash\n{body}\n")

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "command",
    [
        "printf x | xargs -d '\\n' sh -c 'docker pull alpine:latest'",
        "printf x | xargs --delimiter '\\n' sh -c 'docker pull alpine:latest'",
        "setsid -f sh -c 'docker pull alpine:latest'",
        "setsid --fork sh -c 'docker pull alpine:latest'",
        "setsid -w sh -c 'docker pull alpine:latest'",
        "setsid --wait sh -c 'docker pull alpine:latest'",
        "sudo -D /tmp sh -c 'docker pull alpine:latest'",
        "sudo --chdir /tmp sh -c 'docker pull alpine:latest'",
        "sudo -p prompt sh -c 'docker pull alpine:latest'",
        "sudo -C 3 sh -c 'docker pull alpine:latest'",
        "sudo -R / sh -c 'docker pull alpine:latest'",
        "sudo -r role sh -c 'docker pull alpine:latest'",
        "sudo -T 30 sh -c 'docker pull alpine:latest'",
        "sudo -s 'docker pull alpine:latest'",
        "sudo -i 'docker pull alpine:latest'",
        "exec -ca custom sh -c 'docker pull alpine:latest'",
        "exec -la custom sh -c 'docker pull alpine:latest'",
        "exec -cla custom sh -c 'docker pull alpine:latest'",
    ],
    ids=[
        "xargs-short-delimiter",
        "xargs-long-delimiter",
        "setsid-fork-short",
        "setsid-fork-long",
        "setsid-wait-short",
        "setsid-wait-long",
        "sudo-chdir-short",
        "sudo-chdir-long",
        "sudo-prompt",
        "sudo-close-from",
        "sudo-chroot",
        "sudo-role",
        "sudo-timeout",
        "sudo-shell",
        "sudo-login-shell",
        "exec-ca",
        "exec-la",
        "exec-cla",
    ],
)
def test_gate_boundary_shell_keeps_wrapper_option_arity_aligned(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.sh").write_text(f"#!/usr/bin/env bash\n{command}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "invocation",
    [
        "vars(subprocess)['run'](COMMAND)",
        "subprocess.__getattribute__('run')(COMMAND)",
        "object.__getattribute__(subprocess, 'run')(COMMAND)",
        "importlib.import_module('subprocess').run(COMMAND)",
        "il.import_module('subprocess').run(COMMAND)",
        "load('subprocess').run(COMMAND)",
    ],
    ids=[
        "vars",
        "dunder-getattribute",
        "object-getattribute",
        "importlib",
        "import-alias",
        "from-alias",
    ],
)
def test_gate_boundary_python_tracks_additional_reflected_process_access(
    checker: ModuleType | _MissingChecker, tmp_path: Path, invocation: str
) -> None:
    _write_valid_repo(tmp_path)
    body = invocation.replace("COMMAND", "['docker', 'pull', 'alpine:latest']")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import importlib\nimport importlib as il\nimport subprocess\n"
        "from importlib import import_module as load\n"
        f"{body}\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("import_line", "callable_name"),
    [
        ("import shlex as sx", "sx.split"),
        ("from shlex import split", "split"),
    ],
    ids=["module-alias", "from-alias"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [("docker pull alpine:latest", True), ("git status", False)],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_shlex_import_aliases(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    import_line: str,
    callable_name: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{import_line}\nsubprocess.run({callable_name}({command!r}))\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "invocation",
    ["common.__dict__['invoke'](COMMAND)", "vars(common)['invoke'](COMMAND)"],
    ids=["dunder-dict", "vars"],
)
def test_gate_boundary_python_tracks_reflected_local_module_boundaries(
    checker: ModuleType | _MissingChecker, tmp_path: Path, invocation: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text("def invoke(command):\n    print(command)\n")
    body = invocation.replace("COMMAND", "['docker', 'pull', 'alpine:latest']")
    (tmp_path / "scripts/release_worker.py").write_text(f"import common\n{body}\n")

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "selector",
    ["([subprocess.run][0:1])[0]", "[subprocess.run].pop()"],
    ids=["slice-then-index", "pop"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_inline_callable_selection(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        "cmd = ['git', 'status']\nlist.__setitem__(cmd, 0, 'docker')\nsubprocess.run(cmd)",
        "class Ops:\n    pass\ntype.__setattr__(Ops, 'run', subprocess.run)\n"
        "Ops.run(['docker', 'pull', 'alpine:latest'])",
        "class Box:\n    pass\nbox = Box()\n"
        "vars(box)['cmd'] = ['docker', 'pull', 'alpine:latest']\nsubprocess.run(box.cmd)",
    ],
    ids=["list-setitem", "type-setattr", "vars-assignment"],
)
def test_gate_boundary_python_tracks_additional_reflected_mutations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(f"import subprocess\n{body}\n")

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_preserves_effect_order_with_outer_binding_snapshot(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ndanger = ['docker', 'pull', 'alpine:latest']\n"
        "cmd = ['git', 'status']\n"
        "def apply():\n    global cmd\n    configure(['git', 'status'])\n    cmd = danger\n"
        "def configure(value):\n    global cmd\n    cmd = value\n"
        "apply()\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_applies_rhs_effects_before_assignment(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def configure():\n"
        "    global cmd\n    cmd = ['git', 'status']\n"
        "    return ['docker', 'pull', 'alpine:latest']\n"
        "cmd = configure()\nsubprocess.run(cmd)\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("invocation", "expected_errors"),
    [
        ("set_danger(set_safe())", True),
        ("set_safe(set_danger())", False),
    ],
    ids=["outer-danger", "outer-safe"],
)
def test_gate_boundary_python_preserves_nested_call_effect_order(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    invocation: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def set_safe(value=None):\n"
        "    global cmd\n    cmd = ['git', 'status']\n    return value\n"
        "def set_danger(value=None):\n"
        "    global cmd\n    cmd = ['docker', 'pull', 'alpine:latest']\n    return value\n"
        f"{invocation}\nsubprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("before_return", "after_return", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", True),
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", False),
    ],
    ids=["unreachable-safe", "unreachable-danger"],
)
def test_gate_boundary_python_ignores_effects_after_unconditional_return(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    before_return: str,
    after_return: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ncmd = ['git', 'status']\n"
        "def apply():\n"
        f"    global cmd\n    cmd = {before_return}\n    return\n    cmd = {after_return}\n"
        "apply()\nsubprocess.run(cmd)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "subprocess.__dict__.__getitem__('run')",
        "vars(subprocess).__getitem__('run')",
        "dict.__getitem__(vars(subprocess), 'run')",
        "operator.getitem(vars(subprocess), 'run')",
    ],
    ids=["dunder-dict-method", "vars-method", "dict-unbound", "operator-getitem"],
)
@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_tracks_reflected_mapping_getitem_process_access(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import operator\nimport subprocess\n{selector}({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    [
        "next(iter([TARGET]))",
        "([TARGET] * 2)[0]",
        "[TARGET].copy()[0]",
        "tuple([TARGET])[0]",
    ],
    ids=["next-iter", "multiplied-list", "copied-list", "tuple-conversion"],
)
@pytest.mark.parametrize(
    ("target", "expected_errors"),
    [("subprocess.run", True), ("print", False)],
    ids=["process", "safe"],
)
def test_gate_boundary_python_rejects_opaque_process_callable_transformations(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    selector: str,
    target: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    invocation = selector.replace("TARGET", target)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\n{invocation}(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "selector",
    ["getattr(subprocess, method)", "getattr(subprocess, input())"],
    ids=["imported-selector", "computed-selector"],
)
def test_gate_boundary_python_rejects_opaque_process_module_reflection(
    checker: ModuleType | _MissingChecker, tmp_path: Path, selector: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text("method = 'run'\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nfrom runtime_config import method\n"
        f"{selector}(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "selector",
    [
        "importlib.import_module('scripts.common').invoke",
        "__import__('scripts.common', fromlist=['invoke']).invoke",
        "getattr(importlib.import_module('scripts.common'), 'invoke')",
    ],
    ids=["importlib", "dunder-import", "getattr-importlib"],
)
def test_gate_boundary_python_tracks_dynamic_local_module_boundaries(
    checker: ModuleType | _MissingChecker, tmp_path: Path, selector: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/common.py").write_text("def invoke(command):\n    print(command)\n")
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import importlib\n{selector}(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "cmd = ['git', 'status']\n"
            "def configure():\n"
            "    global cmd\n    cmd = ['git', 'status']\n    return 'docker'\n"
            "cmd[0] = configure()\nsubprocess.run(cmd)\n",
            True,
        ),
        (
            "cmd = ['docker', 'status']\n"
            "def configure():\n"
            "    global cmd\n    cmd = ['docker', 'status']\n    return 'git'\n"
            "cmd[0] = configure()\nsubprocess.run(cmd)\n",
            False,
        ),
        (
            "class Box:\n    pass\nbox = Box()\nbox.cmd = ['git', 'status']\n"
            "def configure():\n"
            "    box.cmd = ['git', 'status']\n"
            "    return ['docker', 'pull', 'alpine:latest']\n"
            "box.cmd = configure()\nsubprocess.run(box.cmd)\n",
            True,
        ),
        (
            "class Box:\n    pass\nbox = Box()\nbox.cmd = ['docker', 'status']\n"
            "def configure():\n"
            "    box.cmd = ['docker', 'status']\n    return ['git', 'status']\n"
            "box.cmd = configure()\nsubprocess.run(box.cmd)\n",
            False,
        ),
    ],
    ids=["subscript-danger", "subscript-safe", "attribute-danger", "attribute-safe"],
)
def test_gate_boundary_python_applies_rhs_effects_before_path_mutations(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(f"import subprocess\n{body}")

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        "import subprocess\nfrom runtime_config import COMMAND\nsubprocess.run(COMMAND)\n",
        "import subprocess\nfrom runtime_config import COMMAND\nsubprocess.run(args=COMMAND)\n",
        "import os\nfrom runtime_config import COMMAND\nos.system(COMMAND)\n",
        (
            "import subprocess\nfrom runtime_config import COMMAND\n"
            "def main(command):\n    subprocess.run(command)\nmain(COMMAND)\n"
        ),
    ],
    ids=["subprocess-positional", "subprocess-keyword", "os-system", "forwarded"],
)
def test_gate_boundary_python_rejects_unproved_terminal_process_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/runtime_config.py").write_text(
        "COMMAND = ['docker', 'pull', 'alpine:latest']\n"
    )
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    ("command", "expected_errors"),
    [
        ("['docker', 'pull', 'alpine:latest']", True),
        ("['git', 'status']", False),
    ],
    ids=["docker", "safe"],
)
def test_gate_boundary_python_resolves_computed_positional_process_payloads(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\ndef make():\n    return {command}\nsubprocess.run(make())\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "class State:\n    pass\nstate = State()\nstate.cmd = ['git', 'status']\n"
            "def configure(value):\n    state.cmd = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(state.cmd)\n"
        ),
        (
            "state = {'cmd': ['git', 'status']}\n"
            "def configure(value):\n    state['cmd'] = value\n"
            "configure(['docker', 'pull', 'alpine:latest'])\nsubprocess.run(state['cmd'])\n"
        ),
        (
            "def outer():\n    class State:\n        pass\n    state = State()\n"
            "    state.cmd = ['git', 'status']\n"
            "    def configure(value):\n        state.cmd = value\n"
            "    configure(['docker', 'pull', 'alpine:latest'])\n"
            "    subprocess.run(state.cmd)\nouter()\n"
        ),
    ],
    ids=["module-attribute", "module-subscript", "enclosing-attribute"],
)
def test_gate_boundary_python_tracks_external_path_mutations_without_declarations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(f"import subprocess\n{body}")

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_rejects_opaque_map_process_payloads(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\ndef commands():\n    return iter(())\nmap(subprocess.run, commands())\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_rejects_analysis_recursion_without_crashing(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    definitions = ["import subprocess", "def f0():\n    return subprocess.run"]
    definitions.extend(f"def f{index}():\n    return f{index - 1}()" for index in range(1, 601))
    (tmp_path / "scripts/release_worker.py").write_text("\n".join(definitions))

    errors = _errors(checker, tmp_path)

    assert any("recursion" in error.lower() for error in errors)


@pytest.mark.parametrize(
    ("dangerous", "safe"),
    [
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().map("
            "func=os.system, iterable=['docker pull alpine:latest'])\n",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().map(func=os.system, iterable=['git status'])\n",
        ),
        (
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().starmap("
            "func=os.system, iterable=[('docker pull alpine:latest',)])\n",
            "import multiprocessing\nimport os\n"
            "multiprocessing.Pool().starmap(func=os.system, iterable=[('git status',)])\n",
        ),
        (
            "import asyncio\nimport functools\nimport os\n"
            "loop = asyncio.new_event_loop()\n"
            "loop.run_in_executor("
            "executor=None, func=functools.partial(os.system, 'docker pull alpine:latest'))\n",
            "import asyncio\nimport functools\nimport os\n"
            "loop = asyncio.new_event_loop()\n"
            "loop.run_in_executor("
            "executor=None, func=functools.partial(os.system, 'git status'))\n",
        ),
        (
            "import multiprocessing\nimport os\n"
            "list(multiprocessing.Pool().imap(os.system, ['docker pull alpine:latest']))\n",
            "import multiprocessing\nimport os\n"
            "list(multiprocessing.Pool().imap(os.system, ['git status']))\n",
        ),
        (
            "import multiprocessing\nimport os\n"
            "list(multiprocessing.Pool().imap_unordered("
            "os.system, ['docker pull alpine:latest']))\n",
            "import multiprocessing\nimport os\n"
            "list(multiprocessing.Pool().imap_unordered(os.system, ['git status']))\n",
        ),
    ],
    ids=[
        "pool-map-keywords",
        "pool-starmap-keywords",
        "executor-keywords",
        "imap",
        "imap-unordered",
    ],
)
def test_gate_boundary_python_covers_keyword_and_lazy_process_adapters(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    dangerous: str,
    safe: str,
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    worker.write_text(dangerous)
    assert _errors(checker, tmp_path)

    worker.write_text(safe)
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import asyncio\nimport os\n"
            "class Runner:\n"
            "    def __init__(self):\n        self.loop = asyncio.new_event_loop()\n"
            "    def run(self):\n"
            "        self.loop.run_in_executor("
            "None, os.system, 'docker pull alpine:latest')\n"
            "Runner().run()\n"
        ),
        (
            "import concurrent.futures\nimport os\n"
            "class Runner:\n"
            "    def __init__(self):\n"
            "        self.executor = concurrent.futures.ThreadPoolExecutor()\n"
            "    def run(self):\n"
            "        self.executor.submit(os.system, 'docker pull alpine:latest')\n"
            "Runner().run()\n"
        ),
        (
            "import multiprocessing\nimport os\n"
            "class Runner:\n"
            "    def __init__(self):\n        self.pool = multiprocessing.Pool()\n"
            "    def run(self):\n"
            "        self.pool.apply("
            "os.system, args=('docker pull alpine:latest',))\n"
            "Runner().run()\n"
        ),
    ],
    ids=["loop-attribute", "executor-attribute", "pool-attribute"],
)
def test_gate_boundary_python_fails_closed_for_sensitive_instance_dispatch(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "import multiprocessing\n"
        "multiprocessing.Process(None, functools.partial(os.system, {command}))\n",
        "import multiprocessing\n"
        "multiprocessing.Process(target=functools.partial(os.system, {command}))\n",
        "import multiprocessing\n"
        "multiprocessing.Process(None, functools.partial(os.system), None, ({command},))\n",
        "import multiprocessing\n"
        "multiprocessing.Process(target=functools.partial(os.system), args=({command},))\n",
        "import threading\nthreading.Thread(None, functools.partial(os.system, {command}))\n",
        "import threading\nthreading.Thread(target=functools.partial(os.system, {command}))\n",
        "import threading\n"
        "threading.Thread(None, functools.partial(os.system), None, ({command},))\n",
        "import threading\n"
        "threading.Thread(target=functools.partial(os.system), args=({command},))\n",
        "import threading\nthreading.Timer(1, functools.partial(os.system, {command}))\n",
        "import threading\n"
        "threading.Timer(interval=1, function=functools.partial(os.system, {command}))\n",
        "import threading\nthreading.Timer(1, functools.partial(os.system), ({command},))\n",
        "import threading\n"
        "threading.Timer(interval=1, function=functools.partial(os.system), args=({command},))\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().apply(functools.partial(os.system, {command}))\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().apply(func=functools.partial(os.system, {command}))\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().apply(functools.partial(os.system), ({command},))\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().apply("
        "func=functools.partial(os.system), args=({command},))\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().map(functools.partial(os.system), [{command}])\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().map("
        "func=functools.partial(os.system), iterable=[{command}])\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().starmap(functools.partial(os.system), [({command},)])\n",
        "import multiprocessing\n"
        "multiprocessing.Pool().starmap("
        "func=functools.partial(os.system), iterable=[({command},)])\n",
        "import concurrent.futures\n"
        "concurrent.futures.ThreadPoolExecutor().submit("
        "functools.partial(os.system, {command}))\n",
        "import concurrent.futures\n"
        "concurrent.futures.ThreadPoolExecutor().submit("
        "functools.partial(os.system), {command})\n",
        "import concurrent.futures\n"
        "list(concurrent.futures.ThreadPoolExecutor().map("
        "functools.partial(os.system), [{command}]))\n",
    ],
)
def test_gate_boundary_python_expands_inline_partial_process_callbacks(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    prefix = "import functools\nimport os\n"

    worker.write_text(prefix + body.format(command=repr("docker pull alpine:latest")))
    assert _errors(checker, tmp_path)

    worker.write_text(prefix + body.format(command=repr("git status")))
    assert _errors(checker, tmp_path) == []


def test_gate_boundary_ci_scans_run_step_scripts(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "run": [{"name": "image", "script": f"docker pull {TAG}"}],
            }
        },
    )
    assert any(
        "root.job.run[0].script" in error and TAG in error for error in _errors(checker, tmp_path)
    )

    _write_yaml(
        tmp_path / ".gitlab-ci.yml",
        {
            "job": {
                "image": REFERENCE,
                "run": [{"name": "safe", "script": "echo safe"}],
            }
        },
    )
    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_scans_python_shebang_with_arbitrary_suffix(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release.pyw"
    worker.write_text(
        "#!/usr/bin/env python3\nimport subprocess\n"
        "subprocess.run(['docker', 'pull', 'alpine:latest'])\n"
    )
    assert _errors(checker, tmp_path)

    worker.write_text("#!/usr/bin/env python3\nprint('safe')\n")
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        (
            "import subprocess\n"
            "def invoke(function, argument):\n    return function(argument)\n"
            "invoke(subprocess.run, {command})\n"
        ),
        (
            "import subprocess\nimport threading\n"
            "def invoke(function, argument):\n    return function(argument)\n"
            "threading.Thread(target=invoke, args=(subprocess.run, {command})).start()\n"
        ),
    ],
    ids=["direct", "thread-callback"],
)
def test_gate_boundary_python_expands_local_higher_order_invocations(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    worker.write_text(body.format(command=repr(["docker", "pull", "alpine:latest"])))
    assert _errors(checker, tmp_path)

    worker.write_text(body.format(command=repr(["git", "status"])))
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "lookup",
    [
        "locals()['runner']",
        "locals().get('runner')",
        "dict.get(locals(), 'runner')",
        "operator.getitem(locals(), 'runner')",
    ],
    ids=["subscript", "bound-get", "unbound-get", "operator-getitem"],
)
@pytest.mark.parametrize(
    ("runner", "command", "expected_errors"),
    [
        ("subprocess.run", "['docker', 'pull', 'alpine:latest']", True),
        ("subprocess.run", "['git', 'status']", False),
        ("print", "['docker', 'diagnostic']", False),
    ],
    ids=["process-docker", "safe-payload", "safe-callable"],
)
def test_gate_boundary_python_preserves_locals_scope_in_higher_order_expansion(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    lookup: str,
    runner: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import operator\nimport subprocess\n"
        f"def invoke(runner, command):\n    {lookup}(command)\n"
        f"invoke({runner}, {command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "lookup",
    [
        "locals()['runner']",
        "locals().get('runner')",
        "dict.get(locals(), 'runner')",
        "operator.getitem(locals(), 'runner')",
    ],
    ids=["subscript", "bound-get", "unbound-get", "operator-getitem"],
)
def test_gate_boundary_python_expands_string_only_local_callable_parameters(
    checker: ModuleType | _MissingChecker, tmp_path: Path, lookup: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import operator\nimport subprocess\n"
        f"def invoke(runner):\n    {lookup}"
        "(['docker', 'pull', 'alpine:latest'])\n"
        "invoke(subprocess.run)\n"
    )

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_keeps_globals_distinct_in_higher_order_expansion(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        "runner = print\n"
        "def invoke(runner, command):\n    globals()['runner'](command)\n"
        "invoke(subprocess.run, ['docker', 'diagnostic'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "def wrapper(command):\n    globals()['runner'](command)\n"
            "import subprocess\nrunner = subprocess.run\n"
            "wrapper(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def wrapper(command):\n"
            "    getattr(globals()['module'], 'run')(command)\n"
            "import subprocess\nmodule = subprocess\n"
            "wrapper(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def wrapper(command):\n    globals()['runner'](command)\n"
            "runner = print\nwrapper(['docker', 'diagnostic'])\n",
            False,
        ),
        (
            "def wrapper(command):\n    globals()['runner'](command)\n"
            "import subprocess\nrunner = subprocess.run\n"
            "wrapper(['git', 'status'])\n",
            False,
        ),
        (
            "def wrapper(command):\n"
            "    getattr(globals()['module'], 'run')(command)\n"
            "class LocalModule:\n    def run(self, command):\n        print(command)\n"
            "module = LocalModule()\nwrapper(['docker', 'diagnostic'])\n",
            False,
        ),
    ],
    ids=[
        "globals-process",
        "getattr-process",
        "globals-safe-callable",
        "globals-safe-payload",
        "getattr-safe",
    ],
)
def test_gate_boundary_python_rechecks_synthesized_wrapper_callable_provenance(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(body)

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("runner", "command", "expected_errors"),
    [
        ("subprocess.run", "['docker', 'pull', 'alpine:latest']", True),
        ("subprocess.run", "['git', 'status']", False),
        ("print", "['docker', 'diagnostic']", False),
    ],
    ids=["process-docker", "process-safe-payload", "safe-callable"],
)
def test_gate_boundary_python_preserves_opaque_provenance_through_decorators(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    runner: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        f"runner = {runner}\n"
        "def decorate(function):\n    return globals()['runner']\n"
        "@decorate\ndef wrapped(command):\n    print(command)\n"
        f"wrapped({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("module_setup", "returned", "command", "expected_errors"),
    [
        (
            "import subprocess\nmodule = subprocess\n",
            "getattr(globals()['module'], 'run')",
            "['docker', 'pull', 'alpine:latest']",
            True,
        ),
        (
            "import subprocess\nmodule = subprocess\n",
            "globals()['module'].run",
            "['docker', 'pull', 'alpine:latest']",
            True,
        ),
        (
            "import subprocess\nmodule = subprocess\n",
            "getattr(globals().get('module'), 'run')",
            "['docker', 'pull', 'alpine:latest']",
            True,
        ),
        (
            "import subprocess\nmodule = subprocess\n",
            "getattr(globals()['module'], 'run')",
            "['git', 'status']",
            False,
        ),
        (
            "class LocalModule:\n"
            "    def run(self, command):\n"
            "        print(command)\n"
            "module = LocalModule()\n",
            "getattr(globals()['module'], 'run')",
            "['docker', 'diagnostic']",
            False,
        ),
    ],
    ids=[
        "getattr-process",
        "attribute-process",
        "mapping-get-process",
        "safe-payload",
        "safe-owner",
    ],
)
def test_gate_boundary_python_preserves_runtime_owner_provenance_through_decorators(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    module_setup: str,
    returned: str,
    command: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"{module_setup}"
        "def decorate(function):\n"
        f"    return {returned}\n"
        "@decorate\ndef wrapped(command):\n    print(command)\n"
        f"wrapped({command})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("body", "expected_errors"),
    [
        (
            "selector = choose()\n"
            "getattr(globals()['module'], selector)"
            "(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def factory(selector):\n"
            "    return getattr(globals()['module'], selector)\n"
            "runner = factory(choose())\n"
            "runner(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def decorate(function):\n"
            "    return getattr(globals()['module'], choose())\n"
            "@decorate\n"
            "def wrapped(command):\n"
            "    print(command)\n"
            "wrapped(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def decorate(selector):\n"
            "    def apply(function):\n"
            "        return getattr(globals()['module'], selector)\n"
            "    return apply\n"
            "@decorate(choose())\n"
            "def wrapped(command):\n"
            "    print(command)\n"
            "wrapped(['docker', 'pull', 'alpine:latest'])\n",
            True,
        ),
        (
            "def decorate(function):\n"
            "    return getattr(globals()['module'], choose())\n"
            "@decorate\n"
            "def wrapped(command):\n"
            "    print(command)\n"
            "wrapped(['git', 'status'])\n",
            False,
        ),
    ],
    ids=["direct", "factory", "decorator", "decorator-factory", "safe-payload"],
)
def test_gate_boundary_python_fails_closed_for_dynamic_runtime_process_members(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    body: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        f"import subprocess\nmodule = subprocess\ndef choose():\n    return 'run'\n{body}"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_python_keeps_dynamic_runtime_safe_owners_benign(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "class LocalModule:\n"
        "    def run(self, command):\n"
        "        print(command)\n"
        "module = LocalModule()\n"
        "def choose():\n    return 'run'\n"
        "def decorate(function):\n"
        "    return getattr(globals()['module'], choose())\n"
        "@decorate\n"
        "def wrapped(command):\n"
        "    print(command)\n"
        "wrapped(['docker', 'diagnostic'])\n"
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("initial", "applied", "through_wrapper", "expected_errors"),
    [
        (
            "['git', 'status']",
            "['docker', 'pull', 'alpine:latest']",
            False,
            True,
        ),
        (
            "['git', 'status']",
            "['docker', 'pull', 'alpine:latest']",
            True,
            True,
        ),
        (
            "['docker', 'pull', 'alpine:latest']",
            "['git', 'status']",
            False,
            False,
        ),
    ],
    ids=["direct-dangerous", "wrapped-dangerous", "direct-safe-replacement"],
)
def test_gate_boundary_python_applies_external_effects_through_runtime_mapping_lookup(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    applied: str,
    through_wrapper: bool,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    invocation = "wrapper" if through_wrapper else "globals()['configure']"
    wrapper = "def wrapper(value):\n    globals()['configure'](value)\n" if through_wrapper else ""
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        f"command = {initial}\n"
        f"{wrapper}"
        "def configure(value):\n    global command\n    command = value\n"
        f"{invocation}({applied})\n"
        "subprocess.run(command)\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("initial", "replacement", "expected_errors"),
    [
        ("print", "process", True),
        ("print", "functools.partial(subprocess.run)", True),
        ("process", "print", False),
    ],
    ids=["wrapper", "partial", "safe-replacement"],
)
def test_gate_boundary_python_refreshes_runtime_callable_facts_after_external_effects(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    replacement: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport subprocess\n"
        "def process(command):\n    subprocess.run(command)\n"
        f"runner = {initial}\n"
        "def configure():\n    global runner\n"
        f"    runner = {replacement}\n"
        "configure()\n"
        "globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("mapping", "module_runner", "local_runner", "expected_errors"),
    [
        ("globals", "subprocess.run", "print", True),
        ("globals", "print", "subprocess.run", False),
        ("locals", "print", "subprocess.run", True),
        ("locals", "subprocess.run", "print", False),
    ],
    ids=["globals-module", "globals-ignores-local", "locals-current", "locals-shadows-module"],
)
def test_gate_boundary_python_resolves_runtime_mapping_in_the_correct_scope(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    mapping: str,
    module_runner: str,
    local_runner: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\n"
        f"runner = {module_runner}\n"
        f"def invoke():\n    runner = {local_runner}\n"
        f"    {mapping}()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        "invoke()\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    "body",
    [
        (
            "def runner(command):\n    subprocess.run(command)\n"
            "globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "runner = lambda command: subprocess.run(command)\n"
            "globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "import functools\nrunner = functools.partial(subprocess.run)\n"
            "globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "def factory():\n    return subprocess.run\nrunner = factory()\n"
            "globals()['runner'](['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "runner = subprocess.run\n"
            "globals().__getitem__('runner')(['docker', 'pull', 'alpine:latest'])\n"
        ),
        (
            "runner = subprocess.run\n"
            "dict.get(globals(), 'runner')(['docker', 'pull', 'alpine:latest'])\n"
        ),
    ],
    ids=["function", "lambda", "partial", "factory", "dunder-getitem", "dict-get"],
)
def test_gate_boundary_python_resolves_callable_facts_from_runtime_namespace(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text("import subprocess\n" + body)

    assert _errors(checker, tmp_path)


def test_gate_boundary_python_does_not_resolve_sys_modules_keys_as_variable_names(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import subprocess\nimport sys\nrunner = subprocess.run\n"
        "sys.modules['runner'].whatever(['docker', 'diagnostic'])\n"
    )

    assert _errors(checker, tmp_path) == []


def test_gate_boundary_python_resolves_runtime_module_alias_lookup(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    worker.write_text(
        "import subprocess as sp\nglobals()['sp'].run(['docker', 'pull', 'alpine:latest'])\n"
    )
    assert _errors(checker, tmp_path)

    worker.write_text("import subprocess as sp\nglobals()['sp'].run(['git', 'status'])\n")
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        "import operator\noperator.call(subprocess.run, {command})\n",
        (
            "import asyncio\nloop = asyncio.new_event_loop()\n"
            "loop.call_soon(subprocess.run, {command})\n"
        ),
        (
            "import functools\nimport threading\n"
            "threading.Barrier(1, action=functools.partial(subprocess.run, {command})).wait()\n"
        ),
    ],
    ids=["operator-call", "loop-call-soon", "barrier-action"],
)
def test_gate_boundary_python_models_additional_callback_executors(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    prefix = "import subprocess\n"
    worker.write_text(prefix + body.format(command=repr(["docker", "pull", "alpine:latest"])))
    assert _errors(checker, tmp_path)

    worker.write_text(prefix + body.format(command=repr(["git", "status"])))
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    "command",
    [
        (f"python -c \"import subprocess; subprocess.run(['docker','run','{REFERENCE}'])\""),
        f"perl -e 'exec q(docker), q(run), q({REFERENCE})'",
    ],
    ids=["python", "unmodelled-interpreter"],
)
def test_gate_boundary_shell_rejects_inline_interpreter_docker_execution(
    checker: ModuleType | _MissingChecker, tmp_path: Path, command: str
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release.sh"
    worker.write_text(f"#!/usr/bin/env bash\n{command}\n")
    assert _errors(checker, tmp_path)

    worker.write_text("#!/usr/bin/env bash\npython -c \"print('safe')\"\n")
    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("initial", "override", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["safe-to-docker", "docker-to-safe"],
)
def test_gate_boundary_python_models_partial_keyword_overrides(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    override: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport subprocess\n"
        f"runner = functools.partial(subprocess.run, args={initial})\n"
        f"runner(args={override})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


@pytest.mark.parametrize(
    ("initial", "override", "expected_errors"),
    [
        ("['git', 'status']", "['docker', 'pull', 'alpine:latest']", True),
        ("['docker', 'pull', 'alpine:latest']", "['git', 'status']", False),
    ],
    ids=["safe-to-docker", "docker-to-safe"],
)
def test_gate_boundary_python_models_partial_variadic_keyword_overrides(
    checker: ModuleType | _MissingChecker,
    tmp_path: Path,
    initial: str,
    override: str,
    expected_errors: bool,
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport subprocess\n"
        "def invoke(**options):\n    subprocess.run(options['args'])\n"
        f"runner = functools.partial(invoke, args={initial})\n"
        f"runner(args={override})\n"
    )

    assert bool(_errors(checker, tmp_path)) is expected_errors


def test_gate_boundary_python_rejects_functools_partial_placeholder(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/release_worker.py").write_text(
        "import functools\nimport subprocess\n"
        "runner = functools.partial(subprocess.run, functools.Placeholder)\n"
        "runner(['docker', 'pull', 'alpine:latest'])\n"
    )

    assert _errors(checker, tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "multiprocessing.Process(**{'target': functools.partial(os.system, COMMAND)})\n",
        "threading.Thread(**{'target': functools.partial(os.system, COMMAND)})\n",
        ("threading.Timer(**{'interval': 1, 'function': functools.partial(os.system, COMMAND)})\n"),
        ("multiprocessing.Pool().apply(**{'func': functools.partial(os.system, COMMAND)})\n"),
        (
            "multiprocessing.Pool().map(**{"
            "'func': functools.partial(os.system), 'iterable': [COMMAND]})\n"
        ),
        (
            "multiprocessing.Pool().starmap(**{"
            "'func': functools.partial(os.system), 'iterable': [(COMMAND,)]})\n"
        ),
    ],
    ids=["process", "thread", "timer", "apply", "map", "starmap"],
)
def test_gate_boundary_python_expands_static_callback_keyword_mappings(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str
) -> None:
    _write_valid_repo(tmp_path)
    worker = tmp_path / "scripts/release_worker.py"
    prefix = "import functools\nimport multiprocessing\nimport os\nimport threading\n"

    worker.write_text(prefix + body.replace("COMMAND", repr("docker pull alpine:latest")))
    assert _errors(checker, tmp_path)

    worker.write_text(prefix + body.replace("COMMAND", repr("git status")))
    assert _errors(checker, tmp_path) == []


WORKFLOW_SOURCE = ".github/workflows/continuous-integration.yml"


def _write_workflow(root: Path, body: str, *, name: str = "continuous-integration.yml") -> None:
    path = root / ".github/workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def _relock(root: Path, sources: list[str]) -> None:
    _write_yaml(root / "config/container-images.lock.yml", _catalog(sources))


def test_github_workflow_container_image_must_be_pinned(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    sources = _write_valid_repo(tmp_path)
    _write_workflow(tmp_path, f"jobs:\n  verify:\n    container: {TAG}\n    steps: []\n")
    _relock(tmp_path, [*sources, WORKFLOW_SOURCE])

    assert [
        error for error in _errors(checker, tmp_path) if error.startswith(f"{WORKFLOW_SOURCE}:")
    ] == [f"{WORKFLOW_SOURCE}:jobs.verify.container: {TAG} is not pinned; expected {REFERENCE}"]


def test_github_workflow_service_image_must_be_pinned(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    sources = _write_valid_repo(tmp_path)
    _write_workflow(
        tmp_path,
        f"jobs:\n  verify:\n    services:\n      postgres:\n        image: {TAG}\n    steps: []\n",
    )
    _relock(tmp_path, [*sources, WORKFLOW_SOURCE])

    assert any(
        "jobs.verify.services.postgres.image" in error and "is not pinned" in error
        for error in _errors(checker, tmp_path)
    )


def test_github_workflow_discovers_pinned_container_service_and_run_images(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    sources = _write_valid_repo(tmp_path)
    _write_workflow(
        tmp_path,
        "jobs:\n"
        "  verify:\n"
        f"    container:\n      image: {REFERENCE}\n"
        "    services:\n"
        "      postgres:\n"
        f"        image: {REFERENCE}\n"
        "    steps:\n"
        "      - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803\n"
        f"      - run: docker run --rm {REFERENCE} true\n",
    )
    _relock(tmp_path, [*sources, WORKFLOW_SOURCE])

    assert _errors(checker, tmp_path) == []


def test_github_workflow_run_step_build_provenance_uses_the_workspace_inventory(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    sources = _write_valid_repo(tmp_path)
    _write_workflow(
        tmp_path,
        "jobs:\n  build:\n    steps:\n"
        "      - run: docker build -f Absent.Dockerfile -t local:build .\n",
    )
    _relock(tmp_path, sources)

    assert any("not in the operational inventory" in error for error in _errors(checker, tmp_path))

    _write_workflow(
        tmp_path,
        "jobs:\n  build:\n    steps:\n      - run: docker build -f Dockerfile -t local:build .\n",
    )

    assert _errors(checker, tmp_path) == []


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (
            "jobs:\n  verify:\n    steps:\n"
            "      - run: docker build .\n"
            "        working-directory: services\n",
            "working-directory",
        ),
        (
            "jobs:\n  verify:\n    steps:\n      - run: echo hi\n        shell: python\n",
            "shell",
        ),
        (
            "jobs:\n  verify:\n    uses: ./.github/workflows/reusable.yml\n",
            "reusable workflow",
        ),
    ],
    ids=["working-directory", "shell", "reusable-workflow"],
)
def test_github_workflow_fails_closed_on_unverifiable_step_context(
    checker: ModuleType | _MissingChecker, tmp_path: Path, body: str, fragment: str
) -> None:
    sources = _write_valid_repo(tmp_path)
    _write_workflow(tmp_path, body)
    _relock(tmp_path, sources)

    assert any(fragment in error for error in _errors(checker, tmp_path))


def test_docker_exec_is_modelled_as_a_non_ingress_verb(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """``docker exec`` names a CONTAINER, never an image — nothing to pin.

    The gate refused it as an "unsupported Docker command", which was not a policy
    but a hole in the model: ``DOCKER_COMPOSE_VERBS`` already contains ``exec``, so
    ``docker compose exec`` passed and the bare form did not. And the "no ingress"
    set already contains ``load``, which does bring an image in from an archive —
    ``exec`` is strictly further from that.

    The hole's cost was not cosmetic: ``check_container_image_pins.py`` runs BEFORE
    ``pytest`` in the ``test:unit`` job, so a refused script prevented the whole unit
    suite from running.
    """
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/query-db.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'docker exec brain_v42_postgres psql -U brain -d brain -Atc "select 1;"\n'
    )

    assert not [error for error in _errors(checker, tmp_path) if "query-db.sh" in error]


def test_a_verb_that_really_pulls_an_image_is_still_refused(
    checker: ModuleType | _MissingChecker, tmp_path: Path
) -> None:
    """The NEGATIVE probe: modelling ``exec`` must open nothing else.

    Without it, we would not know whether the test above passes because ``exec`` is
    correctly modelled or because the gate stopped inspecting scripts.
    """
    _write_valid_repo(tmp_path)
    (tmp_path / "scripts/pull-unpinned.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\ndocker pull postgres:16\n"
    )

    assert [error for error in _errors(checker, tmp_path) if "pull-unpinned.sh" in error]
