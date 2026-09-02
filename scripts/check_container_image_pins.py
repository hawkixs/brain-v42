#!/usr/bin/env python3
"""Fail-closed, offline validation of repository container image references."""

from __future__ import annotations

import argparse
import ast
import copy
import re
import shlex
import sys
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypeGuard, cast

import yaml

DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
IDENTIFIER_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
PLATFORM_PATTERN = re.compile(r"[a-z0-9]+/[a-z0-9_]+(?:/[a-z0-9_.-]+)?\Z")
SHELL_VARIABLE_PATTERN = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<plain>[A-Za-z_][A-Za-z0-9_]*))\Z"
)

LOCK_KEYS = {"schema_version", "images", "local_images"}
CI_TOP_LEVEL_DATA_KEYS = {
    "after_script",
    "before_script",
    "cache",
    "image",
    "include",
    "services",
    "spec",
    "stages",
    "types",
    "variables",
    "workflow",
}
IMAGE_KEYS = {
    "reference",
    "registry",
    "tag",
    "digest",
    "media_type",
    "platforms",
    "resolved_at",
    "resolution",
    "consumers",
}
LOCAL_IMAGE_KEYS = {"compose", "service", "context"}
MANIFEST_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}

# Deliberately limited to forms used by repository-managed operational scripts.
# A new option fails closed until its argument semantics receive a fixture.
BUILD_FLAGS_WITH_VALUE = {
    "--add-host",
    "--build-arg",
    "--cache-from",
    "--cache-to",
    "-f",
    "--file",
    "--iidfile",
    "--label",
    "--network",
    "-o",
    "--output",
    "--platform",
    "--secret",
    "--shm-size",
    "--ssh",
    "-t",
    "--tag",
    "--target",
    "--ulimit",
}
BUILD_BOOLEAN_FLAGS = {
    "--check",
    "--load",
    "--no-cache",
    "--pull",
    "--push",
    "-q",
    "--quiet",
    "--rm",
    "--squash",
}
RUN_FLAGS_WITH_VALUE = {"-v", "--volume", "--gpus", "--platform", "--pull", "-w", "--workdir"}
RUN_BOOLEAN_FLAGS = {"--rm"}
CREATE_FLAGS_WITH_VALUE = RUN_FLAGS_WITH_VALUE | {"--name"}
DOCKER_GLOBAL_FLAGS_WITH_VALUE = {
    "--config",
    "-c",
    "--context",
    "-H",
    "--host",
    "-l",
    "--log-level",
    "--tlscacert",
    "--tlscert",
    "--tlskey",
}
DOCKER_GLOBAL_BOOLEAN_FLAGS = {
    "-D",
    "--debug",
    "--tls",
    "--tlsverify",
    "-v",
    "--version",
}
SHELL_INTERPRETERS = {"ash", "bash", "dash", "hush", "sh", "zsh"}
CI_SMOKE_IMAGE = "brain-v42-ci-smoke:${CI_COMMIT_SHA}"
SHELL_CONTROL_CHARACTERS = frozenset(";&|()")
SHELL_OPEN_KEYWORDS = {"case", "for", "if", "select", "until", "while"}
SHELL_BRANCH_KEYWORDS = {"do", "elif", "else", "then"}
SHELL_CLOSE_KEYWORDS = {"done", "esac", "fi"}
SHELL_DATA_COMMANDS = {"echo", "log", "printf", "proof", "warn"}
SHELL_UNSAFE_OPERATORS = {"&", "&&", "|", "|&", "||"}
SHELL_PENDING_OPERATORS = {"!", "&&", "|", "|&", "||"}
DOCKER_NON_INGRESS_VERBS = {
    "exec",
    "info",
    "inspect",
    "load",
    "login",
    # ``network`` (create/inspect/rm/…) manipulates networks, never images:
    # it is the churn bench's teardown and its name-collision guard.
    "network",
    "ps",
    "push",
    "rm",
    "rmi",
    "save",
}
# ``exec`` names a CONTAINER already running, never an image: there is nothing
# to pin. Its absence was a hole in the model and not a policy —
# ``DOCKER_COMPOSE_VERBS`` already contains ``exec``, so that
# ``docker compose exec`` passed where the bare form was refused. The set above
# admits ``load`` besides, which genuinely brings an image in from an
# archive.
#
# The hole was expensive: this gate runs BEFORE ``pytest`` in ``test:unit``, so
# a single refused script stopped the ENTIRE unit suite from running. ``down``
# destroys an existing deployment without bringing an image in; it is the
# teardown gesture of the disposable benches (HNSW churn), just like ``exec``.
DOCKER_COMPOSE_VERBS = {"build", "config", "down", "exec", "up"}
GITLAB_CI_SOURCE = ".gitlab-ci.yml"
GITHUB_WORKFLOW_DIRECTORY = ".github/workflows"
GITHUB_WORKFLOW_SUFFIXES = {".yaml", ".yml"}
# GitHub only guarantees a POSIX shell semantic for these; anything else (python,
# pwsh, a custom "{0}" template) would need its own parser to stay verifiable.
GITHUB_WORKFLOW_SHELLS = {"bash", "sh"}
# GitLab reserves the CI smoke tag to its build:docker script; the GitHub delivery
# rail reserves it to the single job that builds and pushes the same image.
GITHUB_SMOKE_JOB = "build-docker"
OPERATIONAL_DIRECTORIES = {"deploy", "ops", "scripts", "services"}
DISCOVERY_EXCLUDED_PARTS = {".claude", ".git", "bench", "docs", "tests"}
DOCKER_SDK_IMAGE_OPERATIONS = {
    "api.build",
    "api.create_container",
    "api.pull",
    "containers.create",
    "images.build",
    "images.get",
    "images.pull",
    "images.push",
    "services.create",
}
PYTHON_DOCKER_CLI_CALLABLES = frozenset(
    {
        "anyio.open_process",
        "anyio.run_process",
        "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.popen",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.system",
        "pty.spawn",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "subprocess.run",
    }
)
PROCESS_MODULE_NAMES = frozenset(
    {
        "anyio",
        "asyncio",
        "atexit",
        "concurrent",
        "multiprocessing",
        "os",
        "pty",
        "subprocess",
        "threading",
    }
)
MAX_CALLABLE_PROVENANCE_DEPTH = 64
ASYNCIO_LOOP_FACTORIES = frozenset(
    {
        "asyncio.get_event_loop",
        "asyncio.get_running_loop",
        "asyncio.new_event_loop",
    }
)
MULTIPROCESSING_POOL_FACTORIES = frozenset(
    {
        "multiprocessing.Pool",
        "multiprocessing.context.Pool",
        "multiprocessing.pool.Pool",
    }
)
CONCURRENT_EXECUTOR_FACTORIES = frozenset(
    {
        "concurrent.futures.ProcessPoolExecutor",
        "concurrent.futures.ThreadPoolExecutor",
    }
)
PROCESS_DISPATCH_FACTORIES = {
    "add_reader": ASYNCIO_LOOP_FACTORIES,
    "add_writer": ASYNCIO_LOOP_FACTORIES,
    "call_at": ASYNCIO_LOOP_FACTORIES,
    "call_later": ASYNCIO_LOOP_FACTORIES,
    "call_soon": ASYNCIO_LOOP_FACTORIES,
    "call_soon_threadsafe": ASYNCIO_LOOP_FACTORIES,
    "run_in_executor": ASYNCIO_LOOP_FACTORIES,
    "subprocess_exec": ASYNCIO_LOOP_FACTORIES,
    "subprocess_shell": ASYNCIO_LOOP_FACTORIES,
    "apply": MULTIPROCESSING_POOL_FACTORIES,
    "apply_async": MULTIPROCESSING_POOL_FACTORIES,
    "imap": MULTIPROCESSING_POOL_FACTORIES,
    "imap_unordered": MULTIPROCESSING_POOL_FACTORIES,
    "map_async": MULTIPROCESSING_POOL_FACTORIES,
    "starmap": MULTIPROCESSING_POOL_FACTORIES,
    "starmap_async": MULTIPROCESSING_POOL_FACTORIES,
    "map": MULTIPROCESSING_POOL_FACTORIES | CONCURRENT_EXECUTOR_FACTORIES,
    "submit": CONCURRENT_EXECUTOR_FACTORIES,
}

DYNAMIC_PYTHON_CALLABLES = frozenset({"builtins.eval", "builtins.exec", "eval", "exec"})
PYTHON_CONDITIONAL_NODES = (
    ast.AsyncFor,
    ast.BoolOp,
    ast.comprehension,
    ast.For,
    ast.If,
    ast.IfExp,
    ast.Match,
    ast.Try,
    ast.TryStar,
    ast.While,
)
PYTHON_FLOW_TERMINATORS = (ast.Break, ast.Continue, ast.Raise, ast.Return)
PYTHON_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
UNBOUND_CONTAINER_MUTATION_PAYLOAD_INDEX = {
    "builtins.setattr": 2,
    "dict.__setitem__": 2,
    "dict.clear": 1,
    "dict.setdefault": 2,
    "dict.update": 1,
    "list.__iadd__": 1,
    "list.__setitem__": 2,
    "list.append": 1,
    "list.clear": 1,
    "list.extend": 1,
    "list.insert": 2,
    "object.__setattr__": 2,
    "operator.iadd": 1,
    "operator.setitem": 2,
    "setattr": 2,
    "type.__setattr__": 2,
}
TRUSTED_SCRIPT_DIR_ASSIGNMENT = 'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"'


class GateError(ValueError):
    """A configuration construct that cannot be validated safely."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects keys PyYAML would silently overwrite."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class ImageEntry:
    reference: str
    registry: str
    tag: str
    digest: str
    media_type: str
    platforms: tuple[str, ...]
    resolved_at: str
    resolution: str
    consumers: tuple[str, ...]


@dataclass(frozen=True)
class LocalImageEntry:
    compose: str
    service: str
    context: str


@dataclass(frozen=True)
class Catalog:
    images: dict[str, ImageEntry]
    local_images: dict[str, LocalImageEntry]


@dataclass(frozen=True)
class ImageUse:
    reference: str
    source: str
    location: str
    kind: str
    local: bool = False
    service: str | None = None
    build: bool = False
    build_context: str | None = None
    build_error: str | None = None
    pull_policy_build: bool = False


@dataclass(frozen=True)
class ComposeBuild:
    context: str | None = None
    dockerfile: Path | None = None
    error: str | None = None


@dataclass
class DockerfileScanState:
    stages: set[str]
    stage_count: int = 0


@dataclass(frozen=True)
class DockerInvocation:
    verb: str | None = None
    image: str | None = None
    tags: tuple[str, ...] = ()
    build_context: str | None = None
    build_files: tuple[str, ...] = ()
    build_modes: tuple[str, ...] = ()
    compose_files: tuple[str, ...] = ()
    pull_policy: str | None = None
    error: str | None = None


@dataclass
class DockerBuildState:
    tags: list[str]
    contexts: list[str]
    files: list[str]
    modes: list[str]


@dataclass(frozen=True)
class ShellSegment:
    location: str
    tokens: tuple[str, ...]
    controlled: bool
    order: int
    operator_before: str | None = None
    operator_after: str | None = None


@dataclass
class ShellInventory:
    root: Path
    dockerfiles: set[Path]
    compose_files: set[Path]
    trusted_script_dirs: dict[str, int]


@dataclass
class ShellVariableAnalysis:
    relevant_uses: dict[str, list[ShellSegment]]
    occurrences: dict[str, list[tuple[str, bool, int]]]
    executable_variables: set[str]


@dataclass(frozen=True)
class DockerCliWrapperSpec:
    positional_parameters: tuple[str, ...]
    keyword_only_parameters: frozenset[str]
    vararg_parameter: str | None
    kwarg_parameter: str | None
    payload_parameters: frozenset[str]
    dangerous_defaults: frozenset[str]
    always_dangerous: bool = False
    kwarg_payload_keys: frozenset[str] = frozenset()
    kwarg_payload_unbounded: bool = False
    payload_templates: tuple[ast.expr, ...] = ()


@dataclass(frozen=True)
class CallableLayers:
    """Callable wrapper specifications keyed by remaining call depth."""

    specs: tuple[DockerCliWrapperSpec | None, ...] = ()

    def at(self, depth: int) -> DockerCliWrapperSpec | None:
        return self.specs[depth] if 0 <= depth < len(self.specs) else None


@dataclass(frozen=True)
class ExternalPayloadEffect:
    target_scope: int
    name: str
    value: ast.expr
    replace: bool
    conditional: bool = False
    source_line: int = 10**9
    source_col: int = 10**9


@dataclass(frozen=True)
class FunctionExternalEffects:
    definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    effects: tuple[ExternalPayloadEffect, ...]


@dataclass(frozen=True)
class PythonBindingFacts:
    instance_class: str | None
    class_alias: str | None
    wrapper_spec: DockerCliWrapperSpec | None
    callable_factory_spec: DockerCliWrapperSpec | None
    decorator_factory_spec: DockerCliWrapperSpec | None
    callable_layers: CallableLayers
    external_effects: FunctionExternalEffects | None
    markers: tuple[bool, ...]


@dataclass(frozen=True)
class ContainerMutation:
    name: str
    owner: ast.expr
    payloads: tuple[ast.expr, ...]


@dataclass
class ShellScanState:
    smoke_build_order: int | None = None
    smoke_build_count: int = 0


@dataclass(frozen=True)
class EnvCommandParse:
    tokens: tuple[str, ...]
    command_index: int | None
    error: str | None
    split_used: bool = False
    terminal: bool = False


def _load_yaml(path: Path) -> object:
    if not path.is_file():
        raise GateError(f"{path}: file is missing")
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise GateError(f"{path}: YAML parse error: {exc}") from exc


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise GateError(f"{context}: expected a string-keyed mapping")
    return value


def _exact_keys(mapping: Mapping[str, object], expected: set[str], context: str) -> None:
    missing = expected - set(mapping)
    unknown = set(mapping) - expected
    if missing:
        raise GateError(f"{context}: missing fields {sorted(missing)}")
    if unknown:
        raise GateError(f"{context}: unknown fields {sorted(unknown)}")


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GateError(f"{context}: expected a non-empty trimmed string")
    return value


def _string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GateError(f"{context}: expected a non-empty list")
    items = tuple(_string(item, context) for item in value)
    if len(set(items)) != len(items):
        raise GateError(f"{context}: duplicate values are forbidden")
    return items


def _has_readable_tag(reference: str) -> bool:
    final_component = reference.rsplit("/", 1)[-1]
    return ":" in final_component and not final_component.endswith(":")


def _canonical_registry(tag: str) -> str:
    image_name = tag.rsplit(":", 1)[0]
    first_component = image_name.split("/", 1)[0]
    if "." in first_component or ":" in first_component or first_component == "localhost":
        return first_component
    return "registry-1.docker.io"


def _reference_parts(reference: str) -> tuple[str | None, str | None, str | None]:
    if not reference or any(character.isspace() for character in reference):
        return None, None, "reference must be a non-empty token"
    if "$" in reference:
        return None, None, "variable image references are forbidden"
    if reference.count("@") > 1:
        return None, None, "reference has multiple digest separators"
    if "@" in reference:
        tag, digest = reference.rsplit("@", 1)
    else:
        tag, digest = reference, None
    if not _has_readable_tag(tag):
        return None, digest, "reference must retain a readable tag"
    if digest is not None and not DIGEST_PATTERN.fullmatch(digest):
        return tag, digest, "digest must match sha256 followed by 64 lowercase hex characters"
    return tag, digest, None


def load_catalog(path: Path) -> Catalog:
    """Load and strictly validate the canonical offline image catalogue."""
    root = _mapping(_load_yaml(path), str(path))
    _exact_keys(root, LOCK_KEYS, str(path))
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise GateError(f"{path}: schema_version must be integer 1")

    image_values = _mapping(root["images"], f"{path}: images")
    if not image_values:
        raise GateError(f"{path}: images must not be empty")
    images: dict[str, ImageEntry] = {}
    seen_tags: dict[str, str] = {}
    for identifier, raw_entry in image_values.items():
        if not IDENTIFIER_PATTERN.fullmatch(identifier):
            raise GateError(f"{path}: invalid image identifier {identifier!r}")
        context = f"{path}: images.{identifier}"
        entry = _mapping(raw_entry, context)
        _exact_keys(entry, IMAGE_KEYS, context)
        reference = _string(entry["reference"], f"{context}.reference")
        registry = _string(entry["registry"], f"{context}.registry")
        tag = _string(entry["tag"], f"{context}.tag")
        digest = _string(entry["digest"], f"{context}.digest")
        media_type = _string(entry["media_type"], f"{context}.media_type")
        platforms = _string_list(entry["platforms"], f"{context}.platforms")
        resolved_at = _string(entry["resolved_at"], f"{context}.resolved_at")
        resolution = _string(entry["resolution"], f"{context}.resolution")
        consumers = _string_list(entry["consumers"], f"{context}.consumers")
        parsed_tag, parsed_digest, reference_error = _reference_parts(reference)
        if reference_error or parsed_tag != tag or parsed_digest != digest:
            raise GateError(
                f"{context}: reference must equal tag@digest and retain a valid tag/digest"
            )
        if not DIGEST_PATTERN.fullmatch(digest):
            raise GateError(f"{context}.digest: invalid digest {digest!r}")
        if tag in seen_tags:
            raise GateError(
                f"{context}: duplicate tag {tag!r}; already declared by {seen_tags[tag]!r}"
            )
        seen_tags[tag] = identifier
        if "://" in registry or "@" in registry or any(char.isspace() for char in registry):
            raise GateError(f"{context}.registry: expected a canonical registry hostname")
        canonical_registry = _canonical_registry(tag)
        if registry != canonical_registry:
            raise GateError(f"{context}.registry: expected {canonical_registry!r} for tag {tag!r}")
        if media_type not in MANIFEST_MEDIA_TYPES:
            raise GateError(f"{context}.media_type: unsupported manifest media type")
        if any(not PLATFORM_PATTERN.fullmatch(platform) for platform in platforms):
            raise GateError(f"{context}.platforms: invalid platform descriptor")
        if "linux/amd64" not in platforms:
            raise GateError(f"{context}.platforms: linux/amd64 is required")
        try:
            date.fromisoformat(resolved_at)
        except ValueError as exc:
            raise GateError(f"{context}.resolved_at: expected ISO date") from exc
        if any(
            Path(consumer).is_absolute() or ".." in Path(consumer).parts for consumer in consumers
        ):
            raise GateError(f"{context}.consumers: paths must stay repository-relative")
        images[identifier] = ImageEntry(
            reference,
            registry,
            tag,
            digest,
            media_type,
            platforms,
            resolved_at,
            resolution,
            consumers,
        )

    local_values = _mapping(root["local_images"], f"{path}: local_images")
    local_images: dict[str, LocalImageEntry] = {}
    for reference, raw_entry in local_values.items():
        context = f"{path}: local_images.{reference}"
        if not reference.endswith(":local") or "@" in reference or "$" in reference:
            raise GateError(f"{context}: local image must use a literal :local tag")
        entry = _mapping(raw_entry, context)
        _exact_keys(entry, LOCAL_IMAGE_KEYS, context)
        compose = _string(entry["compose"], f"{context}.compose")
        service = _string(entry["service"], f"{context}.service")
        build_context = _string(entry["context"], f"{context}.context")
        if Path(compose).is_absolute() or ".." in Path(compose).parts:
            raise GateError(f"{context}.compose: path must stay repository-relative")
        context_path = Path(build_context)
        if (
            context_path.is_absolute()
            or ".." in context_path.parts
            or context_path.as_posix() != build_context
        ):
            raise GateError(f"{context}.context: path must be normalized and repository-relative")
        local_images[reference] = LocalImageEntry(compose, service, build_context)
    return Catalog(images, local_images)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _add_use(
    uses: list[ImageUse],
    errors: list[str],
    reference: object,
    source: str,
    location: str,
    kind: str,
    *,
    service: str | None = None,
    build: bool = False,
    build_context: str | None = None,
    build_error: str | None = None,
    pull_policy_build: bool = False,
) -> None:
    if not isinstance(reference, str) or not reference.strip():
        errors.append(f"{source}:{location}: image reference must be a literal string")
        return
    if "$" in reference:
        errors.append(f"{source}:{location}: variable image reference is forbidden: {reference}")
        return
    uses.append(
        ImageUse(
            reference,
            source,
            location,
            kind,
            local=reference.endswith(":local") and "@" not in reference,
            service=service,
            build=build,
            build_context=build_context,
            build_error=build_error,
            pull_policy_build=pull_policy_build,
        )
    )


def _scan_ci_image(
    value: Mapping[object, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if "image" not in value:
        return
    image_value = value["image"]
    if isinstance(image_value, Mapping):
        _add_use(
            uses,
            errors,
            image_value.get("name"),
            source,
            f"{location}.image.name",
            "ci",
        )
        return
    _add_use(uses, errors, image_value, source, f"{location}.image", "ci")


def _scan_ci_services(
    value: Mapping[object, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if "services" not in value:
        return
    services = value["services"]
    if not isinstance(services, list):
        errors.append(f"{source}:{location}.services: expected a list")
        return
    for index, service_value in enumerate(services):
        reference = (
            service_value.get("name") if isinstance(service_value, Mapping) else service_value
        )
        _add_use(
            uses,
            errors,
            reference,
            source,
            f"{location}.services[{index}]",
            "ci-service",
        )


def _ci_inherits_default_before_script(value: Mapping[object, object]) -> bool:
    inherit = value.get("inherit")
    if not isinstance(inherit, Mapping) or "default" not in inherit:
        return True
    default = inherit["default"]
    if default is False:
        return False
    if isinstance(default, list):
        return "before_script" in default
    return True


def _scan_ci_lifecycle(
    value: Mapping[object, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
    inherited_before_script: tuple[str, object] | None = None,
) -> set[str]:
    if "script" not in value:
        return set()
    units: list[tuple[str, str]] = []
    lifecycle_keys = {"script"}
    if "before_script" in value:
        units.extend(
            _ci_script_units(value["before_script"], source, f"{location}.before_script", errors)
        )
        lifecycle_keys.add("before_script")
    elif inherited_before_script is not None and _ci_inherits_default_before_script(value):
        inherited_location, inherited_value = inherited_before_script
        units.extend(_ci_script_units(inherited_value, source, inherited_location, errors))
    units.extend(_ci_script_units(value["script"], source, f"{location}.script", errors))
    _scan_ci_script_units(
        units,
        source,
        uses,
        errors,
        inventory,
        allow_ci_smoke=location == "root.build:docker",
    )
    return lifecycle_keys


def _ci_child_directive_context(parent_location: str, key: object) -> bool:
    return (
        parent_location == "root"
        and isinstance(key, str)
        and (key == "default" or key not in CI_TOP_LEVEL_DATA_KEYS)
    )


def _scan_ci_directives(
    value: Mapping[object, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
    inherited_before_script: tuple[str, object] | None,
    *,
    enabled: bool,
) -> set[str]:
    if not enabled:
        return set()
    if "include" in value:
        errors.append(f"{source}:{location}: include is forbidden; CI must be self-contained")
    if "extends" in value:
        errors.append(f"{source}:{location}: extends is forbidden; image resolution must be local")
    _scan_ci_image(value, source, location, uses, errors)
    _scan_ci_services(value, source, location, uses, errors)
    return _scan_ci_lifecycle(
        value,
        source,
        location,
        uses,
        errors,
        inventory,
        inherited_before_script,
    )


def _scan_ci_hooks(
    value: object,
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{source}:{location}: expected a hooks mapping")
        return
    if "pre_get_sources_script" in value:
        _scan_ci_script(
            value["pre_get_sources_script"],
            source,
            f"{location}.pre_get_sources_script",
            uses,
            errors,
            inventory,
        )


def _scan_ci_run(
    value: object,
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    if not isinstance(value, list):
        errors.append(f"{source}:{location}: expected a list of run steps")
        return
    for index, step in enumerate(value):
        step_location = f"{location}[{index}]"
        if not isinstance(step, Mapping):
            errors.append(f"{source}:{step_location}: expected a run step mapping")
            continue
        has_script = "script" in step
        has_predefined_step = "step" in step
        if has_script == has_predefined_step:
            errors.append(
                f"{source}:{step_location}: run step requires exactly one of script or step"
            )
            continue
        if has_predefined_step:
            errors.append(
                f"{source}:{step_location}.step: predefined CI steps are not verifiable offline"
            )
            continue
        _scan_ci_script(step["script"], source, f"{step_location}.script", uses, errors, inventory)


def _scan_ci_value(
    value: object,
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
    inherited_before_script: tuple[str, object] | None = None,
    *,
    directive_context: bool = True,
) -> None:
    if isinstance(value, list):
        for index, child in enumerate(value):
            _scan_ci_value(
                child,
                source,
                f"{location}[{index}]",
                uses,
                errors,
                inventory,
                directive_context=False,
            )
        return
    if not isinstance(value, Mapping):
        return
    lifecycle_keys = _scan_ci_directives(
        value,
        source,
        location,
        uses,
        errors,
        inventory,
        inherited_before_script,
        enabled=directive_context,
    )
    script_keys = {"script", "before_script", "after_script", "pre_get_sources_script"}
    for key, child in value.items():
        if directive_context and (key in lifecycle_keys or key in {"image", "services"}):
            continue
        if (
            directive_context
            and key == "before_script"
            and location
            in {
                "root",
                "root.default",
            }
        ):
            continue
        child_location = f"{location}.{key}"
        if directive_context and key == "hooks":
            _scan_ci_hooks(child, source, child_location, uses, errors, inventory)
        elif directive_context and key == "run":
            _scan_ci_run(child, source, child_location, uses, errors, inventory)
        elif directive_context and key in script_keys:
            _scan_ci_script(child, source, child_location, uses, errors, inventory)
        else:
            _scan_ci_value(
                child,
                source,
                child_location,
                uses,
                errors,
                inventory,
                inherited_before_script if location == "root" else None,
                directive_context=_ci_child_directive_context(location, key),
            )


def _ci_inherited_before_script(
    config: Mapping[str, object], source: str, errors: list[str]
) -> tuple[str, object] | None:
    default = config.get("default")
    default_before = (
        ("root.default.before_script", default["before_script"])
        if isinstance(default, Mapping) and "before_script" in default
        else None
    )
    legacy_before = (
        ("root.before_script", config["before_script"]) if "before_script" in config else None
    )
    if default_before is not None and legacy_before is not None:
        errors.append(
            f"{source}:root: default.before_script and legacy root before_script cannot both be set"
        )
    return default_before or legacy_before


def _scan_ci(
    root: Path,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    path = root / ".gitlab-ci.yml"
    if not path.exists():
        return
    source = _relative(root, path)
    try:
        config = _mapping(_load_yaml(path), source)
    except GateError as exc:
        errors.append(str(exc))
        return
    inherited_before_script = _ci_inherited_before_script(config, source, errors)
    _scan_ci_value(
        config,
        source,
        "root",
        uses,
        errors,
        inventory,
        inherited_before_script,
    )


def _ci_script_units(
    value: object,
    source: str,
    location: str,
    errors: list[str],
) -> list[tuple[str, str]]:
    units: list[tuple[str, str]] = []
    if isinstance(value, str):
        units.append((location, value))
    elif isinstance(value, list):
        for index, command in enumerate(value):
            if not isinstance(command, str):
                errors.append(f"{source}:{location}[{index}]: expected a shell string")
                continue
            units.append((f"{location}[{index}]", command))
    else:
        errors.append(f"{source}:{location}: expected a shell string or list of strings")
    return units


def _scan_ci_script_units(
    units: Sequence[tuple[str, str]],
    source: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
    *,
    allow_ci_smoke: bool = False,
) -> None:
    if not units:
        return

    segments = _shell_segments(source, units, errors)
    _scan_ci_dependencies(source, segments, uses, errors)
    _scan_shell_unit(
        source,
        units,
        uses,
        errors,
        allow_ci_smoke=allow_ci_smoke,
        inventory=inventory,
        parsed_segments=segments,
        scan_inline_interpreters=False,
    )


def _scan_ci_script(
    value: object,
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    units = _ci_script_units(value, source, location, errors)
    _scan_ci_script_units(units, source, uses, errors, inventory)


def _is_github_workflow_source(source: str) -> bool:
    return source.startswith(f"{GITHUB_WORKFLOW_DIRECTORY}/")


def _is_workspace_ci_source(source: str) -> bool:
    """Report whether a CI source runs its shell from the repository root.

    GitLab CI clones into ``$CI_PROJECT_DIR`` and GitHub Actions checks out into
    ``$GITHUB_WORKSPACE``; both make a relative path in a job script resolve against
    the repository root. Any other source keeps CWD-dependent provenance and stays
    unverifiable offline.
    """
    return source == GITLAB_CI_SOURCE or _is_github_workflow_source(source)


def _is_ci_smoke_location(source: str, location: str) -> bool:
    if _is_github_workflow_source(source):
        return location.startswith(f"jobs.{GITHUB_SMOKE_JOB}.steps[") and ".run" in location
    return location.startswith("root.build:docker.script")


def _scan_workflow_container(
    job: Mapping[str, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if "container" not in job:
        return
    container = job["container"]
    if isinstance(container, Mapping):
        _add_use(uses, errors, container.get("image"), source, f"{location}.container.image", "ci")
        return
    _add_use(uses, errors, container, source, f"{location}.container", "ci")


def _scan_workflow_services(
    job: Mapping[str, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if "services" not in job:
        return
    services = job["services"]
    if not isinstance(services, Mapping):
        errors.append(f"{source}:{location}.services: expected a mapping of service identifiers")
        return
    for name, service in services.items():
        service_location = f"{location}.services.{name}"
        if not isinstance(service, Mapping):
            errors.append(f"{source}:{service_location}: expected a service mapping")
            continue
        _add_use(
            uses,
            errors,
            service.get("image"),
            source,
            f"{service_location}.image",
            "ci-service",
            service=str(name),
        )


def _workflow_run_context_error(value: Mapping[str, object]) -> str | None:
    if "working-directory" in value:
        return "working-directory makes repository path provenance unverifiable"
    shell = value.get("shell")
    if shell is not None and shell not in GITHUB_WORKFLOW_SHELLS:
        return f"shell {shell!r} is not verifiable offline"
    return None


def _scan_workflow_defaults(
    value: Mapping[str, object],
    source: str,
    location: str,
    errors: list[str],
) -> None:
    defaults = value.get("defaults")
    if defaults is None:
        return
    if not isinstance(defaults, Mapping):
        errors.append(f"{source}:{location}.defaults: expected a defaults mapping")
        return
    run_defaults = defaults.get("run")
    if run_defaults is None:
        return
    if not isinstance(run_defaults, Mapping):
        errors.append(f"{source}:{location}.defaults.run: expected a run defaults mapping")
        return
    issue = _workflow_run_context_error(run_defaults)
    if issue is not None:
        errors.append(f"{source}:{location}.defaults.run: {issue}")


def _scan_workflow_steps(
    job: Mapping[str, object],
    source: str,
    location: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
    *,
    allow_ci_smoke: bool,
) -> None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        errors.append(f"{source}:{location}.steps: expected a list of steps")
        return
    for index, step in enumerate(steps):
        step_location = f"{location}.steps[{index}]"
        if not isinstance(step, Mapping):
            errors.append(f"{source}:{step_location}: expected a step mapping")
            continue
        issue = _workflow_run_context_error(step)
        if issue is not None:
            errors.append(f"{source}:{step_location}: {issue}")
            continue
        action = step.get("uses")
        if isinstance(action, str) and action.startswith("docker://"):
            _add_use(
                uses,
                errors,
                action.removeprefix("docker://"),
                source,
                f"{step_location}.uses",
                "ci",
            )
        run = step.get("run")
        if run is None:
            continue
        if not isinstance(run, str):
            errors.append(f"{source}:{step_location}.run: expected a shell string")
            continue
        # Each step is its own shell process: scanning them separately keeps a
        # variable assigned in one step from resolving a reference in another.
        _scan_ci_script_units(
            [(f"{step_location}.run", run)],
            source,
            uses,
            errors,
            inventory,
            allow_ci_smoke=allow_ci_smoke,
        )


def _scan_workflow(
    config: Mapping[str, object],
    source: str,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    _scan_workflow_defaults(config, source, "root", errors)
    jobs = config.get("jobs")
    if not isinstance(jobs, Mapping):
        errors.append(f"{source}:jobs: expected a mapping of job identifiers")
        return
    for name, job in jobs.items():
        location = f"jobs.{name}"
        if not isinstance(job, Mapping):
            errors.append(f"{source}:{location}: expected a job mapping")
            continue
        if "uses" in job:
            errors.append(f"{source}:{location}: reusable workflow jobs are not verifiable offline")
            continue
        _scan_workflow_defaults(job, source, location, errors)
        _scan_workflow_container(job, source, location, uses, errors)
        _scan_workflow_services(job, source, location, uses, errors)
        _scan_workflow_steps(
            job,
            source,
            location,
            uses,
            errors,
            inventory,
            allow_ci_smoke=name == GITHUB_SMOKE_JOB,
        )


def _scan_workflows(
    root: Path,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    directory = root / GITHUB_WORKFLOW_DIRECTORY
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in GITHUB_WORKFLOW_SUFFIXES:
            continue
        source = _relative(root, path)
        try:
            config = _load_yaml(path)
        except GateError as exc:
            errors.append(str(exc))
            continue
        # A bare ``on:`` key parses as the YAML 1.1 boolean True, so the workflow
        # mapping cannot be required to be string-keyed the way GitLab CI is.
        if not isinstance(config, Mapping):
            errors.append(f"{source}: expected a workflow mapping")
            continue
        _scan_workflow(cast(Mapping[str, object], config), source, uses, errors, inventory)


def _compose_build_fields(value: object) -> tuple[object, object, str | None]:
    if isinstance(value, str):
        return value, "Dockerfile", None
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        return None, None, "build must be a local context string or mapping"
    unknown = set(value) - {"context", "dockerfile", "target"}
    if unknown:
        return None, None, f"build mapping has unsupported fields {sorted(unknown)}"
    if "context" not in value:
        return None, None, "build mapping must contain a context"
    target = value.get("target")
    if target is not None and (
        not isinstance(target, str) or not target.strip() or target != target.strip()
    ):
        return None, None, "build target must be a non-empty literal string"
    return value["context"], value.get("dockerfile", "Dockerfile"), None


def _resolve_compose_context(
    root: Path, compose_path: Path, context: object
) -> tuple[Path | None, str | None, str | None]:
    if not isinstance(context, str) or not context.strip() or context != context.strip():
        return None, None, "build context must be a non-empty repository-relative path"
    if "$" in context or "`" in context:
        return None, None, "Compose build context must be a literal path without variables"
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", context) or context.startswith("git@"):
        return None, None, "build context must be local to the repository"
    context_path = Path(context)
    if context_path.is_absolute():
        return None, None, "build context must be repository-relative"
    resolved_context = (compose_path.parent / context_path).resolve()
    try:
        repository_context = resolved_context.relative_to(root.resolve())
    except ValueError:
        return None, None, "build context resolves outside the repository"
    return resolved_context, repository_context.as_posix(), None


def _resolve_compose_dockerfile(
    root: Path, resolved_context: Path, dockerfile: object
) -> tuple[Path | None, str | None]:
    if (
        not isinstance(dockerfile, str)
        or not dockerfile.strip()
        or dockerfile != dockerfile.strip()
        or dockerfile == "-"
        or Path(dockerfile).is_absolute()
    ):
        return None, "Compose build Dockerfile must be a local literal path"
    if "$" in dockerfile or "`" in dockerfile:
        return None, "Compose build Dockerfile must be a literal path without variables"
    resolved_dockerfile = (resolved_context / dockerfile).resolve()
    try:
        resolved_dockerfile.relative_to(root.resolve())
    except ValueError:
        return None, "Compose build Dockerfile resolves outside the repository"
    if not resolved_dockerfile.is_file():
        return None, f"Compose build Dockerfile {dockerfile!r} does not resolve to a file"
    return resolved_dockerfile, None


def _compose_build_context(root: Path, compose_path: Path, value: object) -> ComposeBuild:
    context, dockerfile, fields_error = _compose_build_fields(value)
    if fields_error is not None:
        return ComposeBuild(error=fields_error)
    resolved_context, repository_context, context_error = _resolve_compose_context(
        root, compose_path, context
    )
    if context_error is not None or resolved_context is None:
        return ComposeBuild(error=context_error)
    resolved_dockerfile, dockerfile_error = _resolve_compose_dockerfile(
        root, resolved_context, dockerfile
    )
    if dockerfile_error is not None:
        return ComposeBuild(error=dockerfile_error)
    return ComposeBuild(repository_context, resolved_dockerfile)


def _scan_compose_file(
    root: Path,
    path: Path,
    uses: list[ImageUse],
    errors: list[str],
    build_dockerfiles: set[Path],
) -> None:
    source = _relative(root, path)
    try:
        config = _mapping(_load_yaml(path), source)
        services = _mapping(config.get("services"), f"{source}: services")
    except GateError as exc:
        errors.append(str(exc))
        return
    if "include" in config:
        errors.append(
            f"{source}:root.include: include is forbidden; Compose must be self-contained"
        )
    for service_name, raw_service in services.items():
        try:
            service = _mapping(raw_service, f"{source}: services.{service_name}")
        except GateError as exc:
            errors.append(str(exc))
            continue
        if "extends" in service:
            errors.append(
                f"{source}:services.{service_name}.extends: extends is forbidden; "
                "Compose must be self-contained"
            )
        has_build = "build" in service
        build_context: str | None = None
        build_error: str | None = None
        if has_build:
            build = _compose_build_context(root, path, service["build"])
            build_context, build_error = build.context, build.error
            if build.error is None and build.dockerfile is not None:
                build_dockerfiles.add(build.dockerfile)
        image_value = service.get("image")
        is_local_image = (
            isinstance(image_value, str)
            and image_value.endswith(":local")
            and "@" not in image_value
        )
        if build_error and not is_local_image:
            errors.append(f"{source}:services.{service_name}.build: {build_error}")
        if "image" not in service:
            continue
        _add_use(
            uses,
            errors,
            service["image"],
            source,
            f"services.{service_name}.image",
            "compose",
            service=service_name,
            build=has_build and build_error is None,
            build_context=build_context,
            build_error=build_error,
            pull_policy_build=service.get("pull_policy") == "build",
        )


def _operational_files(root: Path) -> set[Path]:
    candidates = {path for path in root.iterdir() if path.is_file()}
    for directory_name in OPERATIONAL_DIRECTORIES:
        directory = root / directory_name
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not (
                set(path.relative_to(root).parts) & DISCOVERY_EXCLUDED_PARTS
            ):
                candidates.add(path)
    return candidates


def _is_compose_file(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in {".yaml", ".yml"} and name.startswith(
        ("compose", "docker-compose", "stack")
    )


def _is_dockerfile(path: Path) -> bool:
    return path.name.startswith(("Containerfile", "Dockerfile"))


def _scan_compose(
    root: Path,
    uses: list[ImageUse],
    errors: list[str],
    build_dockerfiles: set[Path],
    candidates: set[Path] | None = None,
) -> None:
    selected = candidates
    if selected is None:
        selected = {path for path in _operational_files(root) if _is_compose_file(path)}
    for path in sorted(selected):
        _scan_compose_file(root, path, uses, errors, build_dockerfiles)


def _dockerfile_instructions(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    instructions: list[tuple[int, str]] = []
    errors: list[str] = []
    pending = ""
    start_line = 0
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if re.match(r"#\s*syntax\s*=", stripped, flags=re.IGNORECASE):
            errors.append(f"{path}:{line_number}: Dockerfile syntax directive is unsupported")
            continue
        if re.match(r"#\s*escape\s*=", stripped, flags=re.IGNORECASE):
            errors.append(f"{path}:{line_number}: Dockerfile escape directive is unsupported")
            continue
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            start_line = line_number
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        instructions.append((start_line, pending))
        pending = ""
    if pending:
        errors.append(f"{path}:{start_line}: dangling Dockerfile continuation")
    return instructions, errors


def _dockerfile_mount_source(
    source: str,
    line_number: int,
    mount: str,
    errors: list[str],
) -> str | None:
    options: dict[str, str] = {}
    boolean_options = {"readonly", "readwrite", "required", "ro", "rw"}
    for item in mount.split(","):
        if "=" not in item:
            if item not in boolean_options:
                errors.append(f"{source}:{line_number}: malformed RUN --mount option {item!r}")
                return None
            name, value = item, "true"
        else:
            name, value = item.split("=", 1)
        if not name or not value:
            errors.append(f"{source}:{line_number}: malformed RUN --mount option {item!r}")
            return None
        if name in options:
            errors.append(f"{source}:{line_number}: duplicate RUN --mount option {name!r}")
            return None
        options[name] = value
    return options.get("from")


def _record_dockerfile_external_source(
    reference: str,
    *,
    source: str,
    line_number: int,
    kind: str,
    stage_count: int,
    stages: set[str],
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if reference.isdigit():
        if int(reference) >= stage_count:
            errors.append(f"{source}:{line_number}: {kind} stage index {reference} does not exist")
        return
    if reference.lower() in stages:
        return
    _add_use(uses, errors, reference, source, str(line_number), kind)


def _parse_dockerfile_tokens(
    instruction: str, source: str, line_number: int, errors: list[str]
) -> list[str] | None:
    try:
        return shlex.split(instruction, comments=True, posix=True)
    except ValueError as exc:
        errors.append(f"{source}:{line_number}: cannot parse Dockerfile instruction: {exc}")
        return None


def _unwrap_onbuild(
    tokens: list[str], source: str, line_number: int, errors: list[str]
) -> tuple[list[str], str] | None:
    if not tokens or tokens[0].lower() != "onbuild":
        return tokens, ""
    nested = tokens[1:]
    if not nested:
        errors.append(f"{source}:{line_number}: ONBUILD instruction is missing")
        return None
    if nested[0].lower() == "onbuild":
        errors.append(f"{source}:{line_number}: nested ONBUILD is unsupported")
        return None
    return nested, "ONBUILD "


def _dockerfile_copy_source(
    tokens: Sequence[str], source: str, line_number: int, errors: list[str]
) -> str | None:
    for index, token in enumerate(tokens[1:], 1):
        if token.startswith("--from="):
            return token.split("=", 1)[1]
        if token == "--from":
            if index + 1 < len(tokens):
                return tokens[index + 1]
            errors.append(f"{source}:{line_number}: COPY --from value is missing")
            return None
    return None


def _record_dockerfile_source(
    reference: str,
    *,
    source: str,
    line_number: int,
    kind: str,
    state: DockerfileScanState,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    _record_dockerfile_external_source(
        reference,
        source=source,
        line_number=line_number,
        kind=kind,
        stage_count=state.stage_count,
        stages=state.stages,
        uses=uses,
        errors=errors,
    )


def _dockerfile_run_mounts(
    tokens: Sequence[str], source: str, line_number: int, errors: list[str]
) -> list[str]:
    mounts: list[str] = []
    index = 1
    while index < len(tokens) and tokens[index].startswith("--"):
        option = tokens[index]
        if option.startswith("--mount="):
            mounts.append(option.split("=", 1)[1])
        elif option == "--mount":
            if index + 1 >= len(tokens):
                errors.append(f"{source}:{line_number}: RUN --mount value is missing")
                break
            index += 1
            mounts.append(tokens[index])
        index += 1
    return mounts


def _scan_dockerfile_run(
    tokens: Sequence[str],
    *,
    source: str,
    line_number: int,
    kind_prefix: str,
    state: DockerfileScanState,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    for mount in _dockerfile_run_mounts(tokens, source, line_number, errors):
        mount_source = _dockerfile_mount_source(source, line_number, mount, errors)
        if mount_source is not None:
            _record_dockerfile_source(
                mount_source,
                source=source,
                line_number=line_number,
                kind=f"{kind_prefix}RUN --mount from",
                state=state,
                uses=uses,
                errors=errors,
            )


def _dockerfile_from_parts(
    tokens: Sequence[str], source: str, line_number: int, errors: list[str]
) -> tuple[str, str | None] | None:
    index = 1
    while index < len(tokens) and tokens[index].startswith("--"):
        if not tokens[index].startswith("--platform="):
            errors.append(f"{source}:{line_number}: unsupported FROM option {tokens[index]!r}")
        index += 1
    if index >= len(tokens):
        errors.append(f"{source}:{line_number}: FROM image is missing")
        return None
    reference = tokens[index]
    remainder = tokens[index + 1 :]
    if not remainder:
        return reference, None
    if len(remainder) != 2 or remainder[0].lower() != "as":
        errors.append(f"{source}:{line_number}: unsupported FROM construction")
        return None
    return reference, remainder[1].lower()


def _scan_dockerfile_from(
    tokens: Sequence[str],
    *,
    source: str,
    line_number: int,
    state: DockerfileScanState,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    parts = _dockerfile_from_parts(tokens, source, line_number, errors)
    if parts is None:
        return
    reference, stage = parts
    if reference.lower() not in state.stages and reference.lower() != "scratch":
        _add_use(uses, errors, reference, source, str(line_number), "dockerfile")
    if stage:
        state.stages.add(stage)
    state.stage_count += 1


def _scan_dockerfile_instruction(
    tokens: list[str],
    *,
    source: str,
    line_number: int,
    state: DockerfileScanState,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    unwrapped = _unwrap_onbuild(tokens, source, line_number, errors)
    if unwrapped is None or not unwrapped[0]:
        return
    tokens, kind_prefix = unwrapped
    keyword = tokens[0].lower()
    if keyword == "copy":
        copy_source = _dockerfile_copy_source(tokens, source, line_number, errors)
        if copy_source is not None:
            _record_dockerfile_source(
                copy_source,
                source=source,
                line_number=line_number,
                kind=f"{kind_prefix}COPY --from",
                state=state,
                uses=uses,
                errors=errors,
            )
    elif keyword == "run":
        _scan_dockerfile_run(
            tokens,
            source=source,
            line_number=line_number,
            kind_prefix=kind_prefix,
            state=state,
            uses=uses,
            errors=errors,
        )
    elif keyword == "from":
        _scan_dockerfile_from(
            tokens,
            source=source,
            line_number=line_number,
            state=state,
            uses=uses,
            errors=errors,
        )


def _scan_dockerfile(root: Path, path: Path, uses: list[ImageUse], errors: list[str]) -> None:
    source = _relative(root, path)
    state = DockerfileScanState(set())
    instructions, syntax_errors = _dockerfile_instructions(path)
    errors.extend(error.replace(str(path), source) for error in syntax_errors)
    for line_number, instruction in instructions:
        tokens = _parse_dockerfile_tokens(instruction, source, line_number, errors)
        if tokens:
            _scan_dockerfile_instruction(
                tokens,
                source=source,
                line_number=line_number,
                state=state,
                uses=uses,
                errors=errors,
            )


def _scan_dockerfiles(
    root: Path,
    uses: list[ImageUse],
    errors: list[str],
    build_dockerfiles: set[Path],
    candidates: set[Path] | None = None,
) -> None:
    selected = (
        {path for path in _operational_files(root) if _is_dockerfile(path)}
        if candidates is None
        else set(candidates)
    )
    selected.update(build_dockerfiles)
    for path in sorted(selected):
        _scan_dockerfile(root, path, uses, errors)


def _heredoc_markers(statement: str) -> list[tuple[str, bool, bool]]:
    markers: list[tuple[str, bool, bool]] = []
    for match in re.finditer(
        r"(?<!<)<<(?P<strip>-)?\s*"
        r"(?P<delimiter>'[^']+'|\"[^\"]+\"|\\?[A-Za-z_][A-Za-z0-9_]*)",
        statement,
    ):
        token = match.group("delimiter")
        quoted = token.startswith(("'", '"', "\\"))
        delimiter = token[1:-1] if token[:1] in {"'", '"'} else token.lstrip("\\")
        markers.append((delimiter, bool(match.group("strip")), quoted))
    return markers


def _shell_statements_from_text(
    content: str,
) -> tuple[list[tuple[int, str, bool]], list[tuple[int, str]]]:
    statements: list[tuple[int, str, bool]] = []
    syntax_errors: list[tuple[int, str]] = []
    heredocs: list[tuple[str, bool, bool, int]] = []
    pending = ""
    start_line = 0
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        if heredocs:
            delimiter, strip_tabs, quoted, _ = heredocs[0]
            candidate = raw_line.lstrip("\t") if strip_tabs else raw_line
            if candidate == delimiter:
                heredocs.pop(0)
            elif not quoted:
                statements.append((line_number, raw_line, False))
            continue
        stripped = raw_line.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            start_line = line_number
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        try:
            shlex.split(pending, comments=True, posix=True)
        except ValueError as exc:
            if "No closing quotation" in str(exc):
                continue
        statements.append((start_line, pending, True))
        markers = _heredoc_markers(pending)
        opening_count = len(re.findall(r"(?<!<)<<(?!<)", pending))
        if opening_count != len(markers):
            syntax_errors.append((start_line, "shell heredoc delimiter must be literal"))
        else:
            heredocs.extend((*marker, start_line) for marker in markers)
        pending = ""
    if pending:
        statements.append((start_line, pending, True))
    if heredocs:
        delimiter, _, _, opening_line = heredocs[0]
        syntax_errors.append((opening_line, f"shell heredoc {delimiter!r} is not terminated"))
    return statements, syntax_errors


def _partial_shell_tokens(statement: str) -> tuple[list[str], bool]:
    lexer = shlex.shlex(statement, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    tokens: list[str] = []
    try:
        while token := lexer.get_token():
            tokens.append(token)
    except ValueError:
        return tokens, False
    return tokens, True


def _resolve_shell_reference(
    token: str, variables: Mapping[str, str]
) -> tuple[str | None, str | None]:
    match = SHELL_VARIABLE_PATTERN.fullmatch(token)
    if match:
        name = match.group("braced") or match.group("plain")
        if name in variables:
            return variables[name], None
        return None, f"unresolved variable image reference {token}"
    if "$" in token:
        return None, f"variable image reference is forbidden: {token}"
    return token, None


def _is_docker_executable(token: str) -> bool:
    return Path(token).name in {"docker", "docker-compose"}


def _docker_global_options_end(
    tokens: Sequence[str], docker_index: int
) -> tuple[int | None, str | None]:
    index = docker_index + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        base_option = option.split("=", 1)[0]
        if "=" in option and base_option in DOCKER_GLOBAL_FLAGS_WITH_VALUE:
            index += 1
        elif option in DOCKER_GLOBAL_FLAGS_WITH_VALUE:
            if index + 1 >= len(tokens):
                return None, f"docker global option {option!r} is missing its value"
            index += 2
        elif option in DOCKER_GLOBAL_BOOLEAN_FLAGS:
            index += 1
        elif any(token in {"pull", "run"} for token in tokens[index + 1 :]):
            return None, f"unsupported docker global option {option!r}"
        else:
            return None, None
    return index, None


def _docker_verb(tokens: Sequence[str], index: int) -> tuple[str | None, int, str | None]:
    if index >= len(tokens):
        return None, index, None
    verb = tokens[index]
    index += 1
    if "$" in verb or "`" in verb:
        return None, index, f"dynamic docker command {verb!r} is forbidden"
    if verb in {"container", "image"}:
        if index >= len(tokens):
            return None, index, None
        namespaced_verb = tokens[index]
        index += 1
        if "$" in namespaced_verb:
            return None, index, f"dynamic docker {verb} subcommand is forbidden"
        expected_verbs = {"create", "run"} if verb == "container" else {"pull"}
        if namespaced_verb not in expected_verbs:
            return namespaced_verb, index, None
        verb = namespaced_verb
    return verb, index, None


def _record_build_value(state: DockerBuildState, option: str, value: str) -> None:
    if option in {"-t", "--tag"}:
        state.tags.append(value)
    elif option in {"-f", "--file"}:
        state.files.append(value)
    elif option in {"-o", "--output"}:
        state.modes.append("--output")


def _consume_docker_build_option(
    tokens: Sequence[str], index: int, state: DockerBuildState
) -> tuple[int, str | None] | None:
    option = tokens[index]
    if option.startswith("--tag="):
        state.tags.append(option.split("=", 1)[1])
        return index + 1, None
    if option.startswith("-t") and option != "-t":
        state.tags.append(option[2:])
        return index + 1, None
    base_option = option.split("=", 1)[0]
    if option in BUILD_FLAGS_WITH_VALUE:
        if index + 1 >= len(tokens):
            return index, f"docker build {option} value is missing"
        _record_build_value(state, option, tokens[index + 1])
        return index + 2, None
    if "=" in option and base_option in BUILD_FLAGS_WITH_VALUE:
        _record_build_value(state, base_option, option.split("=", 1)[1])
        return index + 1, None
    if option in BUILD_BOOLEAN_FLAGS:
        if option in {"--check", "--push"}:
            state.modes.append(option)
        return index + 1, None
    if option.startswith("-"):
        return index, f"unsupported docker build option {option!r}"
    return None


def _docker_build_result(
    state: DockerBuildState,
    *,
    context: str | None = None,
    error: str | None = None,
) -> DockerInvocation:
    return DockerInvocation(
        verb="build",
        tags=tuple(state.tags),
        build_context=context,
        build_files=tuple(state.files),
        build_modes=tuple(state.modes),
        error=error,
    )


def _non_local_build_context(context: str) -> bool:
    return bool(
        context == "-"
        or Path(context).is_absolute()
        or re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", context)
        or context.startswith("git@")
    )


def _docker_build_invocation(tokens: Sequence[str], index: int) -> DockerInvocation:
    state = DockerBuildState([], [], [], [])
    while index < len(tokens):
        consumed = _consume_docker_build_option(tokens, index, state)
        if consumed is None:
            state.contexts.append(tokens[index])
            index += 1
            continue
        index, error = consumed
        if error is not None:
            return _docker_build_result(state, error=error)
    if len(state.contexts) != 1:
        return DockerInvocation(
            verb="build",
            tags=tuple(state.tags),
            build_files=tuple(state.files),
            build_modes=tuple(state.modes),
            error=f"docker build requires one context, got {state.contexts!r}",
        )
    context = state.contexts[0]
    if _non_local_build_context(context):
        return _docker_build_result(
            state,
            context=context,
            error="unsupported Docker command: docker build context must be local",
        )
    return _docker_build_result(state, context=context)


def _docker_pull_invocation(tokens: Sequence[str], index: int) -> DockerInvocation:
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--platform":
            index += 2
        elif option.startswith("--platform=") or option in {"-q", "--quiet"}:
            index += 1
        else:
            return DockerInvocation(verb="pull", error=f"unsupported docker pull option {option!r}")
    if index >= len(tokens):
        return DockerInvocation(verb="pull", error="docker pull image is missing")
    return DockerInvocation(verb="pull", image=tokens[index])


def _docker_container_invocation(tokens: Sequence[str], index: int, verb: str) -> DockerInvocation:
    pull_policy: str | None = None
    value_flags = RUN_FLAGS_WITH_VALUE if verb == "run" else CREATE_FLAGS_WITH_VALUE
    boolean_flags = RUN_BOOLEAN_FLAGS if verb == "run" else set()
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        base_option = option.split("=", 1)[0]
        if "=" in option and base_option in value_flags:
            if verb == "run" and base_option == "--pull":
                if pull_policy is not None:
                    return DockerInvocation(verb=verb, error="docker run repeats --pull")
                pull_policy = option.split("=", 1)[1]
            index += 1
        elif option in value_flags:
            if index + 1 >= len(tokens):
                return DockerInvocation(verb=verb, error=f"docker {verb} {option} value is missing")
            if verb == "run" and option == "--pull":
                if pull_policy is not None:
                    return DockerInvocation(verb=verb, error="docker run repeats --pull")
                pull_policy = tokens[index + 1]
            index += 2
        elif option in boolean_flags:
            index += 1
        else:
            return DockerInvocation(verb=verb, error=f"unsupported docker {verb} option {option!r}")
    if index >= len(tokens):
        return DockerInvocation(verb=verb, error=f"docker {verb} image is missing")
    return DockerInvocation(verb=verb, image=tokens[index], pull_policy=pull_policy)


def _docker_compose_invocation(tokens: Sequence[str], index: int) -> DockerInvocation:
    files: list[str] = []
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option in {"-f", "--file"}:
            if index + 1 >= len(tokens):
                return DockerInvocation(
                    verb="compose", error=f"docker compose {option} value is missing"
                )
            files.append(tokens[index + 1])
            index += 2
            continue
        if option.startswith("--file="):
            files.append(option.split("=", 1)[1])
            index += 1
            continue
        # ``-p`` names the project: without it, compose derives the project
        # from the file's folder and two disposable benches with different
        # names recreate one another. A project name changes no image.
        if option in {"-p", "--project-name"}:
            if index + 1 >= len(tokens):
                return DockerInvocation(
                    verb="compose", error=f"docker compose {option} value is missing"
                )
            index += 2
            continue
        if option.startswith("--project-name="):
            index += 1
            continue
        return DockerInvocation(
            verb="compose", error=f"unsupported Docker command: docker compose option {option!r}"
        )
    if index >= len(tokens):
        return DockerInvocation(verb="compose", error="docker compose subcommand is missing")
    subcommand = tokens[index]
    if subcommand not in DOCKER_COMPOSE_VERBS:
        return DockerInvocation(
            verb="compose",
            compose_files=tuple(files),
            error=f"unsupported Docker command: docker compose {subcommand}",
        )
    return DockerInvocation(verb=f"compose:{subcommand}", compose_files=tuple(files))


def _docker_command(tokens: Sequence[str], docker_index: int) -> DockerInvocation:
    if Path(tokens[docker_index]).name == "docker-compose":
        return _docker_compose_invocation(tokens, docker_index + 1)
    index, option_error = _docker_global_options_end(tokens, docker_index)
    if option_error is not None:
        return DockerInvocation(error=option_error)
    if index is None:
        return DockerInvocation()
    verb, index, verb_error = _docker_verb(tokens, index)
    if verb_error is not None:
        return DockerInvocation(error=verb_error)
    if verb == "build":
        return _docker_build_invocation(tokens, index)
    if verb == "pull":
        return _docker_pull_invocation(tokens, index)
    if verb in {"create", "run"}:
        return _docker_container_invocation(tokens, index, verb)
    if verb == "compose":
        return _docker_compose_invocation(tokens, index)
    if verb in DOCKER_NON_INGRESS_VERBS:
        return DockerInvocation(verb=verb)
    return DockerInvocation(
        verb=verb,
        error=f"unsupported Docker command {verb!r}",
    )


def _contains_docker_execution(command: str) -> bool:
    scan_errors: list[str] = []
    segments = _shell_segments("indirect", [("", command)], scan_errors)
    if scan_errors:
        return True
    variables, opaque = _shell_variable_state("indirect", segments, scan_errors)
    if scan_errors:
        return True
    for segment in segments:
        executable_index, resolution_error = _resolve_command_position(segment.tokens)
        if resolution_error:
            return True
        if executable_index is None:
            continue
        variable = _shell_variable_name(segment.tokens[executable_index])
        if variable in opaque:
            return True
        is_docker, executable_error = _resolve_docker_executable(
            segment.tokens, executable_index, variables
        )
        if executable_error:
            return True
        if not is_docker:
            continue
        invocation = _docker_command(segment.tokens, executable_index)
        if (
            invocation.image is not None
            or invocation.error is not None
            or invocation.verb == "build"
            or (invocation.verb or "").startswith("compose:")
        ):
            return True
    return False


def _interpreter_command(tokens: Sequence[str], interpreter_index: int) -> str | None:
    index = interpreter_index + 1
    while index < len(tokens):
        option = tokens[index]
        if option == "--rcfile":
            index += 2
            continue
        if option.startswith("--rcfile="):
            index += 1
            continue
        if option in {"-O", "+O", "-o", "+o"}:
            index += 2
            continue
        if option.startswith("--"):
            index += 1
            continue
        if option.startswith("-"):
            if "c" in option[1:]:
                return tokens[index + 1] if index + 1 < len(tokens) else None
            index += 1
            continue
        return None
    return None


def _resolve_indirect_command(
    command: str, variables: Mapping[str, str]
) -> tuple[str | None, str | None]:
    match = SHELL_VARIABLE_PATTERN.fullmatch(command)
    if match:
        name = match.group("braced") or match.group("plain")
        if name in variables:
            return variables[name], None
        return None, f"unresolved indirect command variable {command}"
    if "$" in command:
        return None, f"dynamic indirect shell command is forbidden: {command}"
    return command, None


def _has_indirect_docker_execution(
    tokens: Sequence[str], executable_index: int, variables: Mapping[str, str]
) -> tuple[bool, str | None]:
    executable_token = tokens[executable_index]
    executable_name = _shell_variable_name(executable_token)
    resolved_executable = variables.get(executable_name) if executable_name is not None else None
    executable = Path(resolved_executable or executable_token).name
    sudo_shell_command = _sudo_shell_command(tokens)
    if sudo_shell_command is not None:
        command, resolution_error = _resolve_indirect_command(sudo_shell_command, variables)
        if resolution_error:
            return False, resolution_error
        assert command is not None
        return _contains_docker_execution(command), None
    if executable not in SHELL_INTERPRETERS | {"eval", "trap"}:
        return False, None
    raw_command: str | None
    if executable == "eval":
        raw_command = " ".join(tokens[executable_index + 1 :])
    elif executable == "trap":
        raw_command = _trap_command(tokens, executable_index)
    else:
        raw_command = _interpreter_command(tokens, executable_index)
        if raw_command is None and "<<<" in tokens[executable_index + 1 :]:
            redirect_index = tokens.index("<<<", executable_index + 1)
            raw_command = tokens[redirect_index + 1] if redirect_index + 1 < len(tokens) else None
    if raw_command is None:
        return False, None
    command, resolution_error = _resolve_indirect_command(raw_command, variables)
    if resolution_error:
        return False, resolution_error
    assert command is not None
    return _contains_docker_execution(command), None


def _trap_command(tokens: Sequence[str], trap_index: int) -> str | None:
    index = trap_index + 1
    if index < len(tokens) and tokens[index] == "--":
        index += 1
    if index >= len(tokens) or tokens[index] in {"", "-", "-l", "-p"}:
        return None
    return tokens[index]


def _sudo_shell_command(tokens: Sequence[str]) -> str | None:
    for index, token in enumerate(tokens):
        if Path(token).name != "sudo":
            continue
        command_index, shell_mode = _sudo_command_position(tokens, index)
        if shell_mode and command_index < len(tokens):
            return " ".join(tokens[command_index:])
    return None


def _assignment(token: str) -> tuple[str, str] | None:
    if "=" not in token:
        return None
    name, value = token.split("=", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return None
    return name, value


def _shell_variable_name(token: str) -> str | None:
    match = SHELL_VARIABLE_PATTERN.fullmatch(token)
    return (match.group("braced") or match.group("plain")) if match else None


def _active_shell_code(statement: str) -> str:
    """Remove comments and single-quoted data while retaining executable substitutions."""
    active: list[str] = []
    quote: str | None = None
    escaped = False
    for index, character in enumerate(statement):
        if escaped:
            active.append(character if quote != "'" else " ")
            escaped = False
            continue
        if character == "\\" and quote != "'":
            active.append(character)
            escaped = True
            continue
        if quote == "'":
            active.append(" ")
            if character == "'":
                quote = None
            continue
        if character == "'" and quote is None:
            active.append(" ")
            quote = "'"
            continue
        if character == '"':
            quote = None if quote == '"' else '"'
            active.append(character)
            continue
        if (
            character == "#"
            and quote is None
            and (index == 0 or statement[index - 1].isspace() or statement[index - 1] in ";&|()")
        ):
            break
        active.append(character)
    return "".join(active)


def _contains_docker_run_text(value: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[\s;&|])(?:[^\s;&|]*/)?docker\s+"
            r"(?:container\s+run|image\s+pull|run|pull)\b",
            value,
        )
    )


def _forbidden_docker_substitution(statement: str) -> str | None:
    active = _active_shell_code(statement)
    if re.search(
        r"(?:^|[;&|]\s*)(?<!\\)\$\([^)]*\)\s+"
        r"(?:container\s+(?:create|run)|image\s+pull|create|pull|run)\b",
        active,
    ):
        return "Docker executable command substitution is forbidden"
    for command in _shell_substitution_commands(statement):
        if _contains_docker_execution(command):
            return "Docker execution in command substitution is forbidden"
    return None


def _mask_shell_substitutions(statement: str) -> str:
    masked = re.sub(r"`[^`]*`", "$__SHELL_SUBSTITUTION", statement)
    previous = ""
    while previous != masked:
        previous = masked
        masked = re.sub(r"\$\((?!\()[^()]*\)", "$__SHELL_SUBSTITUTION", masked)
    return masked


class _ShellSegmentParser:
    def __init__(self, source: str, errors: list[str]) -> None:
        self.source = source
        self.errors = errors
        self.segments: list[ShellSegment] = []
        self.contexts: list[str] = []
        self.order = 0
        self.pending_operator: str | None = None
        self.case_waiting_for_pattern = False

    def close_context(self, expected: set[str]) -> None:
        for index in range(len(self.contexts) - 1, -1, -1):
            if self.contexts[index] in expected:
                del self.contexts[index:]
                return

    def _trim_close_keywords(self, raw_tokens: Sequence[str]) -> list[str]:
        tokens = list(raw_tokens)
        expected_context = {"fi": {"if"}, "esac": {"case"}, "done": {"loop"}}
        while tokens and tokens[0] in SHELL_CLOSE_KEYWORDS:
            self.close_context(expected_context[tokens.pop(0)])
        return tokens

    def _control_tokens(self, tokens: list[str], controlled: bool) -> tuple[list[str], bool]:
        if tokens[0] in SHELL_OPEN_KEYWORDS:
            keyword = tokens.pop(0)
            self.contexts.append("if" if keyword == "if" else "loop")
            if keyword in {"for", "select"}:
                loop_variable = tokens[0] if tokens else ""
                return (
                    [f"{loop_variable}+="]
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", loop_variable)
                    else []
                ), True
            return ([] if keyword == "case" else tokens), True
        if tokens[0] in SHELL_BRANCH_KEYWORDS:
            tokens.pop(0)
            return tokens, True
        return tokens, controlled

    def emit(
        self,
        raw_tokens: Sequence[str],
        location: str,
        operator_after: str | None = None,
    ) -> None:
        tokens = self._trim_close_keywords(raw_tokens)
        if not tokens:
            self.pending_operator = (
                operator_after if operator_after in SHELL_PENDING_OPERATORS else None
            )
            return
        controlled = bool(self.contexts) or self.pending_operator in SHELL_PENDING_OPERATORS
        tokens, controlled = self._control_tokens(tokens, controlled)
        if tokens:
            self.segments.append(
                ShellSegment(
                    location,
                    tuple(tokens),
                    controlled or operator_after in SHELL_UNSAFE_OPERATORS,
                    self.order,
                    self.pending_operator,
                    operator_after,
                )
            )
            self.order += 1
        self.pending_operator = (
            operator_after if operator_after in SHELL_PENDING_OPERATORS else None
        )

    def _consume_case_token(self, token: str, current: list[str], location: str) -> bool:
        if token == "esac":
            self.emit(current, location)
            current.clear()
            self.close_context({"case"})
            self.case_waiting_for_pattern = False
            self.pending_operator = None
        elif token == ")" and current and current[0] == "case":
            self.contexts.append("case")
            current.clear()
            self.case_waiting_for_pattern = False
            self.pending_operator = None
        elif token == ")" and self.case_waiting_for_pattern:
            current.clear()
            self.case_waiting_for_pattern = False
            self.pending_operator = None
        elif token in {";;", ";&", ";;&"} and "case" in self.contexts:
            self.emit(current, location, token)
            current.clear()
            self.case_waiting_for_pattern = True
        elif self.case_waiting_for_pattern or (current and current[0] == "case"):
            current.append(token)
        else:
            return False
        return True

    def _consume_group_token(self, token: str, current: list[str], location: str) -> bool:
        if token == "()":
            return True
        if token in {"{", "("}:
            self.emit(current, location)
            current.clear()
            self.contexts.append("group")
            return True
        if token in {"}", ")"}:
            self.emit(current, location)
            current.clear()
            self.close_context({"group"})
            return True
        return False

    def _consume_statement(self, statement: str, location: str, executable: bool) -> None:
        substitution_error = _forbidden_docker_substitution(statement)
        if substitution_error:
            self.errors.append(f"{self.source}:{location}: {substitution_error}: {statement}")
            return
        if not executable:
            return
        tokens, complete = _partial_shell_tokens(_mask_shell_substitutions(statement))
        if tokens == ["!"]:
            self.emit((), location, "!")
            return
        current: list[str] = []
        for token in tokens:
            if self._consume_case_token(token, current, location):
                continue
            if self._consume_group_token(token, current, location):
                continue
            if token in {";", "&", "&&", "|", "|&", "||"}:
                self.emit(current, location, token)
                current.clear()
            else:
                current.append(token)
        if current or not (tokens and tokens[-1] in SHELL_UNSAFE_OPERATORS):
            self.emit(current, location)
        if not complete and _contains_docker_run_text(_active_shell_code(statement)):
            self.errors.append(
                f"{self.source}:{location}: cannot parse Docker shell command safely"
            )

    def consume(self, location_prefix: str, content: str) -> None:
        statements, statement_errors = _shell_statements_from_text(content)
        for line_number, message in statement_errors:
            location = f"{location_prefix}:{line_number}" if location_prefix else str(line_number)
            self.errors.append(f"{self.source}:{location}: {message}")
        for line_number, statement, executable in statements:
            location = f"{location_prefix}:{line_number}" if location_prefix else str(line_number)
            self._consume_statement(statement, location, executable)


def _shell_segments(
    source: str,
    units: Sequence[tuple[str, str]],
    errors: list[str],
) -> list[ShellSegment]:
    parser = _ShellSegmentParser(source, errors)
    for location_prefix, content in units:
        parser.consume(location_prefix, content)
    return parser.segments


def _standalone_assignments(tokens: Sequence[str]) -> list[tuple[str, str]] | None:
    candidates = list(tokens)
    if candidates and candidates[0] in {
        "declare",
        "export",
        "local",
        "readonly",
        "typeset",
    }:
        candidates.pop(0)
        while candidates and candidates[0].startswith("-"):
            candidates.pop(0)
    assignments = [_assignment(token) for token in candidates]
    if not candidates or any(assignment is None for assignment in assignments):
        return None
    return [assignment for assignment in assignments if assignment is not None]


def _valid_shell_name(candidate: str) -> set[str]:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]+\])?", candidate)
    return {match.group(1)} if match else set()


def _printf_mutation_target(tokens: Sequence[str], index: int) -> set[str]:
    while index < len(tokens):
        option = tokens[index]
        if option == "-v" and index + 1 < len(tokens):
            return _valid_shell_name(tokens[index + 1])
        if option.startswith("-v") and len(option) > 2:
            return _valid_shell_name(option[2:])
        if not option.startswith("-"):
            break
        index += 1
    return set()


def _read_mutation_targets(tokens: Sequence[str], index: int) -> set[str]:
    value_options = {"-d", "-i", "-n", "-N", "-p", "-t", "-u"}
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 1
        if option == "-a" and index < len(tokens):
            return _valid_shell_name(tokens[index])
        if option in value_options:
            index += 1
    targets: set[str] = set()
    for candidate in tokens[index:]:
        if candidate.startswith(("<", ">")):
            break
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
            targets.add(candidate)
    return targets


def _let_mutation_targets(tokens: Sequence[str], index: int) -> set[str]:
    targets: set[str] = set()
    for expression in tokens[index:]:
        targets.update(
            match.group(1)
            for match in re.finditer(
                r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*"
                r"(?:\[[^\]]+\])?\s*(?:\+\+|--|<<=|>>=|[-+*/%&|^]?=)",
                expression,
            )
        )
    return targets


def _mapfile_mutation_target(tokens: Sequence[str], index: int) -> set[str]:
    value_options = {"-c", "-C", "-d", "-n", "-O", "-s", "-u"}
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        index += 1
        if option in value_options:
            index += 1
    if index >= len(tokens) or tokens[index].startswith(("<", ">")):
        return set()
    return _valid_shell_name(tokens[index])


def _shell_mutation_targets(tokens: Sequence[str]) -> set[str]:
    augmented = {
        match.group(1)
        for token in tokens
        if (match := re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]+\])?\+=.*", token))
    }
    augmented.update(
        match.group(1)
        for token in tokens
        if (match := re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\[[^\]]+\]=.*", token))
    )
    executable_index, position_error = _resolve_command_position(tokens)
    if executable_index is None or position_error:
        return augmented
    executable = Path(tokens[executable_index]).name
    index = executable_index + 1
    if executable == "eval":
        command = " ".join(tokens[index:])
        return augmented | set(
            re.findall(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*=", command)
        )
    if executable == "printf":
        return augmented | _printf_mutation_target(tokens, index)
    if executable == "read":
        return augmented | _read_mutation_targets(tokens, index)
    if executable == "let":
        return augmented | _let_mutation_targets(tokens, index)
    if executable in {"mapfile", "readarray"}:
        return augmented | _mapfile_mutation_target(tokens, index)
    if executable == "getopts" and index + 1 < len(tokens):
        return augmented | _valid_shell_name(tokens[index + 1])
    if executable == "unset":
        return augmented | {
            name
            for candidate in tokens[index:]
            if not candidate.startswith("-")
            for name in _valid_shell_name(candidate)
        }
    return augmented


def _looks_like_docker_invocation(tokens: Sequence[str], executable_index: int) -> bool:
    return executable_index + 1 < len(tokens) and tokens[executable_index + 1] in {
        "build",
        "compose",
        "container",
        "create",
        "image",
        "pull",
        "run",
    }


def _options_end(tokens: Sequence[str], index: int, value_options: set[str]) -> int:
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        index += 1
        if option in value_options:
            index += 1
    return index


_ENV_SPLIT_OUTSIDE_SPACE = "\ue000"
_ENV_SPLIT_QUOTED_SPACE = "\ue001"
_ENV_SPLIT_LITERAL_UNDERSCORE = "\ue002"
_ENV_SPLIT_ESCAPED_VALUES = {
    "#": "#",
    "$": "$",
    '"': '"',
    "'": "'",
    "\\": "\\",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


@dataclass
class EnvSplitState:
    tokens: list[str]
    current: list[str]
    quote: str | None = None
    token_started: bool = False
    index: int = 0


def _finish_env_split_token(state: EnvSplitState) -> None:
    if not state.token_started:
        return
    state.tokens.append("".join(state.current))
    state.current.clear()
    state.token_started = False


def _consume_env_split_marker(state: EnvSplitState, character: str) -> bool:
    if character == _ENV_SPLIT_OUTSIDE_SPACE:
        _finish_env_split_token(state)
    elif character == _ENV_SPLIT_QUOTED_SPACE:
        state.current.append(" ")
        state.token_started = True
    elif character == _ENV_SPLIT_LITERAL_UNDERSCORE:
        state.current.append(r"\_")
        state.token_started = True
    else:
        return False
    state.index += 1
    return True


def _consume_env_split_quoted_character(
    value: str, state: EnvSplitState, character: str
) -> tuple[bool, str | None]:
    if state.quote == "'":
        if character == "'":
            state.quote = None
        else:
            state.current.append(character)
        state.token_started = True
        state.index += 1
        return True, None
    if character == '"':
        state.quote = None
        state.token_started = True
        state.index += 1
        return True, None
    if character == "\\":
        return False, None
    if character == "$" and value[state.index : state.index + 2] == "${":
        return True, "env split-string variable expansion is unsupported"
    state.current.append(character)
    state.token_started = True
    state.index += 1
    return True, None


def _consume_env_split_unquoted_character(
    value: str, state: EnvSplitState, character: str
) -> tuple[bool, str | None]:
    if character in {" ", "\t"}:
        _finish_env_split_token(state)
    elif character in {"'", '"'}:
        state.quote = character
        state.token_started = True
    elif character == "$" and value[state.index : state.index + 2] == "${":
        return True, "env split-string variable expansion is unsupported"
    elif character == "\\":
        return False, None
    else:
        state.current.append(character)
        state.token_started = True
    state.index += 1
    return True, None


def _consume_env_split_escape(value: str, state: EnvSplitState) -> tuple[bool, str | None]:
    state.index += 1
    if state.index >= len(value):
        return False, "env split-string ends with an incomplete escape"
    escaped = value[state.index]
    if escaped == "c":
        _finish_env_split_token(state)
        return True, None
    if escaped == "_":
        if state.quote == '"':
            state.current.append(" ")
            state.token_started = True
        else:
            _finish_env_split_token(state)
        state.index += 1
        return False, None
    replacement = _ENV_SPLIT_ESCAPED_VALUES.get(escaped)
    if replacement is None:
        return False, f"unsupported env split-string escape {escaped!r}"
    state.current.append(replacement)
    state.token_started = True
    state.index += 1
    return False, None


def _consume_env_split_step(value: str, state: EnvSplitState) -> tuple[bool, str | None]:
    character = value[state.index]
    if _consume_env_split_marker(state, character):
        return False, None
    consumer = (
        _consume_env_split_quoted_character
        if state.quote is not None
        else _consume_env_split_unquoted_character
    )
    handled, error = consumer(value, state, character)
    if handled or error is not None:
        return False, error
    return _consume_env_split_escape(value, state)


def _gnu_env_split_string(value: str) -> tuple[tuple[str, ...] | None, str | None]:
    state = EnvSplitState([], [])
    while state.index < len(value):
        terminal, error = _consume_env_split_step(value, state)
        if error is not None:
            return None, error
        if terminal:
            return tuple(state.tokens), None
    if state.quote is not None:
        return None, "env split-string contains an unterminated quote"
    _finish_env_split_token(state)
    return tuple(state.tokens), None


_ENV_NO_VALUE_LONG_OPTIONS = {
    "--block-signal",
    "--debug",
    "--default-signal",
    "--ignore-environment",
    "--ignore-signal",
    "--list-signal-handling",
    "--null",
}
_ENV_OPTIONAL_VALUE_PREFIXES = (
    "--block-signal=",
    "--default-signal=",
    "--ignore-signal=",
)
_ENV_REQUIRED_LONG_OPTIONS = {"--chdir", "--split-string", "--unset"}
_ENV_NO_VALUE_SHORT_OPTIONS = {"0", "i", "v"}
_ENV_REQUIRED_SHORT_OPTIONS = {"C", "S", "u"}


@dataclass
class EnvCommandState:
    normalized: list[str]
    index: int
    split_used: bool = False
    expansions: int = 0
    options_allowed: bool = True


def _env_command_parse_result(
    state: EnvCommandState,
    command_index: int | None = None,
    error: str | None = None,
    *,
    split_attempted: bool = False,
    terminal: bool = False,
) -> EnvCommandParse:
    return EnvCommandParse(
        tuple(state.normalized),
        command_index,
        error,
        state.split_used or split_attempted,
        terminal,
    )


def _env_required_option_value(
    state: EnvCommandState, attached: str | None
) -> tuple[str | None, int]:
    if attached is not None:
        return attached, 1
    if state.index + 1 >= len(state.normalized):
        return None, 0
    return state.normalized[state.index + 1], 2


def _consume_env_split_option(
    state: EnvCommandState, value: str, consumed: int
) -> EnvCommandParse | None:
    split_tokens, split_error = _gnu_env_split_string(value)
    if split_error is not None or split_tokens is None:
        return _env_command_parse_result(state, error=split_error, split_attempted=True)
    state.expansions += 1
    if state.expansions > 32 or len(state.normalized) + len(split_tokens) > 256:
        return _env_command_parse_result(
            state,
            error="env split-string expansion is too large",
            split_attempted=True,
        )
    state.normalized[state.index : state.index + consumed] = split_tokens
    state.split_used = True
    return None


def _consume_env_long_option(state: EnvCommandState) -> EnvCommandParse | None:
    option = state.normalized[state.index]
    long_option, separator, attached = option.partition("=")
    if long_option not in _ENV_REQUIRED_LONG_OPTIONS:
        return _env_command_parse_result(state, error=f"unsupported env option {option!r}")
    value, consumed = _env_required_option_value(state, attached if separator else None)
    if value is None:
        return _env_command_parse_result(
            state, error=f"env option {long_option!r} is missing its value"
        )
    if long_option != "--split-string":
        state.index += consumed
        return None
    return _consume_env_split_option(state, value, consumed)


def _env_short_value_option(cluster: str) -> tuple[str | None, str]:
    for cluster_index, short_option in enumerate(cluster):
        if short_option in _ENV_NO_VALUE_SHORT_OPTIONS:
            continue
        return short_option, cluster[cluster_index + 1 :]
    return None, ""


def _consume_env_short_option(state: EnvCommandState) -> EnvCommandParse | None:
    option = state.normalized[state.index]
    short_option, attached = _env_short_value_option(option[1:])
    if short_option is None:
        state.index += 1
        return None
    if short_option not in _ENV_REQUIRED_SHORT_OPTIONS:
        return _env_command_parse_result(state, error=f"unsupported env option {option!r}")
    value, consumed = _env_required_option_value(state, attached or None)
    if value is None:
        return _env_command_parse_result(
            state, error=f"env option '-{short_option}' is missing its value"
        )
    if short_option != "S":
        state.index += consumed
        return None
    return _consume_env_split_option(state, value, consumed)


def _consume_env_command_operand(state: EnvCommandState) -> EnvCommandParse | None:
    option = state.normalized[state.index]
    if "=" in option and not option.startswith("="):
        state.index += 1
        return None
    return _env_command_parse_result(state, command_index=state.index)


def _consume_env_command_token(state: EnvCommandState) -> EnvCommandParse | None:
    option = state.normalized[state.index]
    if not state.options_allowed:
        return _consume_env_command_operand(state)
    if option == "--":
        state.options_allowed = False
        state.index += 1
        return None
    if "=" in option and not option.startswith(("-", "=")):
        state.options_allowed = False
        state.index += 1
        return None
    if option in {"--help", "--version"}:
        return _env_command_parse_result(state, terminal=True)
    if option in _ENV_NO_VALUE_LONG_OPTIONS or option.startswith(_ENV_OPTIONAL_VALUE_PREFIXES):
        state.index += 1
        return None
    if option.startswith("--"):
        return _consume_env_long_option(state)
    if option == "-":
        state.index += 1
        return None
    if not option.startswith("-"):
        return _env_command_parse_result(state, command_index=state.index)
    return _consume_env_short_option(state)


def _parse_gnu_env_command(tokens: Sequence[str], env_index: int) -> EnvCommandParse:
    state = EnvCommandState(list(tokens), env_index + 1)
    while state.index < len(state.normalized):
        result = _consume_env_command_token(state)
        if result is not None:
            return result
    return _env_command_parse_result(state)


def _env_command_position(tokens: Sequence[str], index: int) -> tuple[int | None, str | None]:
    parsed = _parse_gnu_env_command(tokens, index - 1)
    if parsed.error is not None:
        return None, parsed.error
    if parsed.split_used:
        return None, "unsupported env split-string wrapper around shell execution"
    return parsed.command_index, None


def _stdbuf_command_position(tokens: Sequence[str], index: int) -> tuple[int | None, str | None]:
    value_options = {"-e", "-i", "-o", "--error", "--input", "--output"}
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        base_option = option.split("=", 1)[0]
        if option in value_options:
            index += 2
        elif "=" in option and base_option in value_options:
            index += 1
        elif re.fullmatch(r"-[eio].+", option):
            index += 1
        elif option == "--":
            return index + 1, None
        else:
            return None, f"unsupported stdbuf option {option!r}"
    return index, None


def _builtin_command_position(tokens: Sequence[str], index: int) -> int:
    while index < len(tokens) and tokens[index] == "--":
        index += 1
    return index


SUDO_VALUE_OPTIONS = {
    "-C",
    "--close-from",
    "-D",
    "--chdir",
    "-g",
    "--group",
    "-h",
    "--host",
    "-p",
    "--prompt",
    "-R",
    "--chroot",
    "-r",
    "--role",
    "-t",
    "--type",
    "-T",
    "--command-timeout",
    "-u",
    "--user",
}
WRAPPER_VALUE_OPTIONS = {
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "setsid": set(),
    "time": {"-f", "--format", "-o", "--output"},
    "xargs": {
        "-a",
        "--arg-file",
        "-E",
        "--eof",
        "-d",
        "--delimiter",
        "-I",
        "--replace",
        "-L",
        "--max-lines",
        "-n",
        "--max-args",
        "-P",
        "--max-procs",
        "-s",
        "--max-chars",
        "--process-slot-var",
    },
}


def _sudo_command_position(tokens: Sequence[str], sudo_index: int) -> tuple[int, bool]:
    index = sudo_index + 1
    shell_mode = False
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            return index + 1, shell_mode
        base_option = option.split("=", 1)[0]
        shell_mode = shell_mode or option in {"-i", "--login", "-s", "--shell"}
        index += 1
        if option in SUDO_VALUE_OPTIONS:
            index += 1
        elif "=" in option and base_option in SUDO_VALUE_OPTIONS:
            continue
    return index, shell_mode


def _exec_command_position(tokens: Sequence[str], exec_index: int) -> tuple[int | None, str | None]:
    index = exec_index + 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            return index + 1, None
        if option.startswith("--argv0="):
            index += 1
            continue
        if option == "--argv0" or (
            option.startswith("-")
            and not option.startswith("--")
            and "a" in option[1:]
            and set(option[1:]) <= {"a", "c", "l"}
        ):
            index += 2
            continue
        if option.startswith("-") and not option.startswith("--") and set(option[1:]) <= {"c", "l"}:
            index += 1
            continue
        return None, f"unsupported exec option {option!r}"
    return index, None


def _command_builtin_command_position(tokens: Sequence[str], command_index: int) -> int | None:
    index = command_index + 1
    if index < len(tokens) and tokens[index] in {"-V", "-v"}:
        return None
    while index < len(tokens) and tokens[index] in {"--", "-p"}:
        index += 1
    return index


def _secondary_wrapped_command_position(
    tokens: Sequence[str], index: int, executable: str
) -> tuple[int | None, str | None]:
    if executable == "timeout":
        option_end = _options_end(tokens, index + 1, {"-k", "--kill-after", "-s", "--signal"})
        return option_end + 1, None
    if executable == "env":
        return _env_command_position(tokens, index + 1)
    if executable == "busybox":
        return index + 1, None
    if executable == "builtin":
        return _builtin_command_position(tokens, index + 1), None
    if executable == "stdbuf":
        return _stdbuf_command_position(tokens, index + 1)
    if executable == "command":
        return _command_builtin_command_position(tokens, index), None
    return index, None


def _wrapped_command_position(
    tokens: Sequence[str], index: int, executable: str
) -> tuple[int | None, str | None]:
    if executable == "exec":
        return _exec_command_position(tokens, index)
    if executable == "sudo":
        command_index, _ = _sudo_command_position(tokens, index)
        return command_index, None
    if executable in WRAPPER_VALUE_OPTIONS:
        return _options_end(tokens, index + 1, WRAPPER_VALUE_OPTIONS[executable]), None
    return _secondary_wrapped_command_position(tokens, index, executable)


def _resolve_command_position(tokens: Sequence[str]) -> tuple[int | None, str | None]:
    index = 0
    while index < len(tokens) and (tokens[index] == "!" or _assignment(tokens[index])):
        index += 1
    while index < len(tokens):
        executable = Path(tokens[index]).name
        if executable in SHELL_DATA_COMMANDS:
            return index, None
        if executable in SHELL_INTERPRETERS | {"eval"} or _is_docker_executable(tokens[index]):
            return index, None
        if _shell_variable_name(tokens[index]) and _looks_like_docker_invocation(tokens, index):
            return index, None
        if executable in {
            "builtin",
            "busybox",
            "command",
            "env",
            "exec",
            "nice",
            "nohup",
            "setsid",
            "sudo",
            "stdbuf",
            "time",
            "timeout",
            "xargs",
        }:
            wrapped_index, wrapper_error = _wrapped_command_position(tokens, index, executable)
            if wrapped_index is None or wrapper_error:
                return wrapped_index, wrapper_error
            index = wrapped_index
            continue

        for candidate_index in range(index + 1, len(tokens)):
            if (
                _is_docker_executable(tokens[candidate_index])
                or _shell_variable_name(tokens[candidate_index])
            ) and _looks_like_docker_invocation(tokens, candidate_index):
                return None, f"unsupported wrapper {tokens[index]!r} around Docker"
        return index, None
    return None, None


def _resolve_python_argv_command_position(
    tokens: Sequence[str],
) -> tuple[int | None, str | None]:
    """Resolve only modeled argv wrappers; ordinary executable arguments stay data."""
    wrappers = {
        "builtin",
        "busybox",
        "command",
        "env",
        "exec",
        "nice",
        "nohup",
        "setsid",
        "sudo",
        "stdbuf",
        "time",
        "timeout",
        "xargs",
    }
    index = 0
    while index < len(tokens):
        executable = Path(tokens[index]).name
        if executable not in wrappers:
            return index, None
        wrapped_index, wrapper_error = _wrapped_command_position(tokens, index, executable)
        if wrapped_index is None or wrapper_error is not None:
            return wrapped_index, wrapper_error
        index = wrapped_index
    return None, None


def _resolve_docker_executable(
    tokens: Sequence[str], executable_index: int, variables: Mapping[str, str]
) -> tuple[bool, str | None]:
    token = tokens[executable_index]
    if _is_docker_executable(token):
        return True, None
    name = _shell_variable_name(token)
    if name is not None:
        resolved = variables.get(name)
        if resolved is not None:
            return _is_docker_executable(resolved), None
        if _looks_like_docker_invocation(tokens, executable_index):
            return False, f"unresolved docker executable variable {token}"
    elif "$" in token and _looks_like_docker_invocation(tokens, executable_index):
        return False, f"dynamic docker executable is forbidden: {token}"
    return False, None


def _docker_executable_alias(tokens: Sequence[str], executable_index: int) -> str | None:
    executable = Path(tokens[executable_index]).name
    arguments = tokens[executable_index + 1 :]
    if executable == "alias":
        for argument in arguments:
            assignment = _assignment(argument)
            if assignment is None:
                continue
            try:
                alias_tokens = shlex.split(assignment[1], comments=False, posix=True)
            except ValueError:
                alias_tokens = []
            alias_index, alias_error = _resolve_command_position(alias_tokens)
            if alias_error or (
                alias_index is not None
                and alias_index < len(alias_tokens)
                and _is_docker_executable(alias_tokens[alias_index])
            ):
                return assignment[0]
        return None
    if executable != "hash":
        return None
    for index, argument in enumerate(arguments):
        if argument == "-p" and index + 2 < len(arguments):
            if _is_docker_executable(arguments[index + 1]):
                return arguments[index + 2]
    return None


def _unambiguous_shell_assignments(
    segments: Sequence[ShellSegment],
) -> dict[str, tuple[str, int]]:
    occurrences: dict[str, list[tuple[str, bool, int]]] = {}
    namerefs = _shell_namerefs(segments)
    for segment in segments:
        _record_shell_occurrences(segment, occurrences, namerefs)
    return {
        name: (values[0][0], values[0][2])
        for name, values in occurrences.items()
        if len(values) == 1 and values[0][0] and not values[0][1]
    }


def _resolve_inventory_path(
    token: str,
    *,
    source: str,
    order: int,
    assignments: Mapping[str, tuple[str, int]],
    inventory: ShellInventory,
) -> Path | None:
    value = token
    variable = _shell_variable_name(token)
    if variable is not None:
        assignment = assignments.get(variable)
        if assignment is None or assignment[1] >= order:
            return None
        value = assignment[0]
    if "$" not in value and "`" not in value:
        if not _is_workspace_ci_source(source):
            return None
        candidate = inventory.root / value
    elif (
        value.startswith("$SCRIPT_DIR/")
        and value.count("$") == 1
        and (trusted_order := inventory.trusted_script_dirs.get(source)) is not None
        and trusted_order < order
    ):
        candidate = inventory.root / Path(source).parent / value.removeprefix("$SCRIPT_DIR/")
    else:
        return None
    resolved = candidate.resolve()
    try:
        resolved.relative_to(inventory.root)
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def _shell_build_inventory_error(
    invocation: DockerInvocation,
    inventory: ShellInventory,
    source: str,
) -> str | None:
    assert invocation.build_context is not None
    if not _is_workspace_ci_source(source):
        return "docker build Dockerfile provenance is CWD-dependent outside a CI workspace"
    context = (inventory.root / invocation.build_context).resolve()
    try:
        context.relative_to(inventory.root)
    except ValueError:
        return "docker build Dockerfile context resolves outside the repository"
    if invocation.build_files:
        if len(invocation.build_files) != 1:
            return "docker build Dockerfile option must occur at most once"
        token = invocation.build_files[0]
        if token == "-" or Path(token).is_absolute() or ".." in Path(token).parts:
            return "docker build Dockerfile must be a repository-local file"
        dockerfile = (inventory.root / token).resolve()
    else:
        dockerfile = context / "Dockerfile"
    if dockerfile not in inventory.dockerfiles or not dockerfile.is_file():
        return "docker build Dockerfile is not in the operational inventory"
    return None


def _shell_compose_inventory_error(
    invocation: DockerInvocation,
    *,
    source: str,
    order: int,
    assignments: Mapping[str, tuple[str, int]],
    inventory: ShellInventory,
) -> str | None:
    tokens = invocation.compose_files
    if not tokens:
        return "Compose file must be explicit with -f; COMPOSE_FILE is unsupported"
    resolved_files: set[Path] = set()
    for token in tokens:
        resolved = _resolve_inventory_path(
            token,
            source=source,
            order=order,
            assignments=assignments,
            inventory=inventory,
        )
        if resolved is None or resolved.suffix.lower() not in {".yaml", ".yml"}:
            return f"Compose file {token!r} is not a literal internal YAML file"
        resolved_files.add(resolved)
    inventory.compose_files.update(resolved_files)
    return None


def _segment_nameref_definitions(segment: ShellSegment) -> dict[str, str]:
    executable_index, position_error = _resolve_command_position(segment.tokens)
    if executable_index is None or position_error:
        return {}
    executable = Path(segment.tokens[executable_index]).name
    arguments = segment.tokens[executable_index + 1 :]
    if executable not in {"declare", "local", "typeset"} or not any(
        "n" in option[1:] for option in arguments if option.startswith("-")
    ):
        return {}
    definitions: dict[str, str] = {}
    for argument in arguments:
        assignment = _assignment(argument)
        if assignment is None:
            continue
        alias, target = assignment
        if _valid_shell_name(target):
            definitions[alias] = next(iter(_valid_shell_name(target)))
    return definitions


def _shell_namerefs(segments: Sequence[ShellSegment]) -> dict[str, str]:
    occurrences: dict[str, list[tuple[str, bool]]] = {}
    for segment in segments:
        for alias, target in _segment_nameref_definitions(segment).items():
            occurrences.setdefault(alias, []).append((target, segment.controlled))
    references = {
        alias: definitions[0][0]
        for alias, definitions in occurrences.items()
        if len(definitions) == 1 and not definitions[0][1]
    }
    for alias in references:
        target = references[alias]
        visited = {alias}
        while target in references and target not in visited:
            visited.add(target)
            target = references[target]
        references[alias] = target
    return references


def _record_shell_occurrences(
    segment: ShellSegment,
    occurrences: dict[str, list[tuple[str, bool, int]]],
    namerefs: Mapping[str, str] | None = None,
) -> None:
    references = {} if namerefs is None else namerefs
    definitions = _segment_nameref_definitions(segment)
    standalone = _standalone_assignments(segment.tokens)
    if standalone is not None:
        for name, value in standalone:
            resolved_name = name if name in definitions else references.get(name, name)
            occurrences.setdefault(resolved_name, []).append(
                (value, segment.controlled, segment.order)
            )
    for name in _shell_mutation_targets(segment.tokens):
        occurrences.setdefault(references.get(name, name), []).append(("", True, segment.order))


def _record_executable_variable_use(
    segment: ShellSegment,
    analysis: ShellVariableAnalysis,
) -> None:
    executable_index, _ = _resolve_command_position(segment.tokens)
    if executable_index is None:
        return
    executable_name = _shell_variable_name(segment.tokens[executable_index])
    indirect_interpreter = any(
        token == "<<<" or (token.startswith("-") and "c" in token[1:])
        for token in segment.tokens[executable_index + 1 :]
    )
    if executable_name and (
        _looks_like_docker_invocation(segment.tokens, executable_index) or indirect_interpreter
    ):
        analysis.relevant_uses.setdefault(executable_name, []).append(segment)
        analysis.executable_variables.add(executable_name)
    executable = Path(segment.tokens[executable_index]).name
    if executable not in SHELL_INTERPRETERS | {"eval"}:
        return
    raw_command = (
        " ".join(segment.tokens[executable_index + 1 :])
        if executable == "eval"
        else _interpreter_command(segment.tokens, executable_index)
    )
    command_name = _shell_variable_name(raw_command or "")
    if command_name:
        analysis.relevant_uses.setdefault(command_name, []).append(segment)


def _literal_shell_assignment(
    name: str,
    use_segments: Sequence[ShellSegment],
    occurrences: Mapping[str, Sequence[tuple[str, bool, int]]],
) -> str | None:
    assignments = occurrences.get(name, [])
    if len(assignments) != 1:
        return None
    value, controlled, order = assignments[0]
    if not value or "$" in value or "`" in value or controlled:
        return None
    return value if order < min(segment.order for segment in use_segments) else None


def _record_image_variable_uses(
    segments: Sequence[ShellSegment], analysis: ShellVariableAnalysis
) -> None:
    executable_values = {
        name: value
        for name in analysis.executable_variables
        if (
            value := _literal_shell_assignment(
                name,
                analysis.relevant_uses[name],
                analysis.occurrences,
            )
        )
        is not None
    }
    for segment in segments:
        executable_index, _ = _resolve_command_position(segment.tokens)
        if executable_index is None:
            continue
        executable_name = _shell_variable_name(segment.tokens[executable_index])
        if not (
            _is_docker_executable(segment.tokens[executable_index])
            or (
                executable_name is not None
                and _is_docker_executable(executable_values.get(executable_name, ""))
            )
        ):
            continue
        invocation = _docker_command(segment.tokens, executable_index)
        image_name = _shell_variable_name(invocation.image or "")
        if image_name:
            analysis.relevant_uses.setdefault(image_name, []).append(segment)


def _resolve_shell_variable_analysis(
    source: str,
    analysis: ShellVariableAnalysis,
    errors: list[str],
) -> tuple[dict[str, str], set[str]]:
    variables: dict[str, str] = {}
    opaque: set[str] = set()
    for name, use_segments in analysis.relevant_uses.items():
        assignments = analysis.occurrences.get(name, [])
        value = _literal_shell_assignment(name, use_segments, analysis.occurrences)
        if value is not None:
            variables[name] = value
        elif assignments:
            opaque.add(name)
            variable_kind = (
                "docker executable variable"
                if name in analysis.executable_variables
                else "shell variable"
            )
            errors.append(
                f"{source}:{use_segments[0].location}: opaque or unresolved {variable_kind} "
                f"{name}: expected one top-level standalone literal assignment before use"
            )
    return variables, opaque


def _shell_variable_state(
    source: str,
    segments: Sequence[ShellSegment],
    errors: list[str],
) -> tuple[dict[str, str], set[str]]:
    analysis = ShellVariableAnalysis({}, {}, set())
    namerefs = _shell_namerefs(segments)
    for segment in segments:
        _record_shell_occurrences(segment, analysis.occurrences, namerefs)
        _record_executable_variable_use(segment, analysis)
    _record_image_variable_uses(segments, analysis)
    return _resolve_shell_variable_analysis(source, analysis, errors)


def _segment_docker_invocation(
    source: str,
    segment: ShellSegment,
    variables: Mapping[str, str],
    opaque: set[str],
    errors: list[str],
) -> tuple[DockerInvocation, int] | None:
    executable_index, position_error = _resolve_command_position(segment.tokens)
    if position_error:
        errors.append(f"{source}:{segment.location}: {position_error}")
        return None
    if executable_index is None:
        return None
    docker_alias = _docker_executable_alias(segment.tokens, executable_index)
    if docker_alias is not None:
        errors.append(
            f"{source}:{segment.location}: Docker executable alias {docker_alias!r} is forbidden"
        )
        return None
    indirect_docker, indirect_error = _has_indirect_docker_execution(
        segment.tokens, executable_index, variables
    )
    if indirect_error:
        errors.append(f"{source}:{segment.location}: {indirect_error}")
    elif indirect_docker:
        errors.append(
            f"{source}:{segment.location}: indirect shell execution contains docker run/pull"
        )
    executable_variable = _shell_variable_name(segment.tokens[executable_index])
    if executable_variable in opaque:
        return None
    is_docker, executable_error = _resolve_docker_executable(
        segment.tokens, executable_index, variables
    )
    if executable_error:
        errors.append(f"{source}:{segment.location}: {executable_error}")
        return None
    if not is_docker:
        return None
    invocation = _docker_command(segment.tokens, executable_index)
    if invocation.error:
        errors.append(f"{source}:{segment.location}: {invocation.error}")
        return None
    return invocation, executable_index


def _direct_shell_invocation(segment: ShellSegment, executable_index: int) -> bool:
    return (
        executable_index == 0
        and not segment.controlled
        and segment.operator_before not in SHELL_UNSAFE_OPERATORS
        and segment.operator_after not in SHELL_UNSAFE_OPERATORS
    )


def _scan_smoke_build(
    source: str,
    segment: ShellSegment,
    invocation: DockerInvocation,
    direct: bool,
    allow_ci_smoke: bool,
    state: ShellScanState,
    errors: list[str],
) -> bool:
    if invocation.verb != "build" or CI_SMOKE_IMAGE not in invocation.tags:
        return False
    state.smoke_build_count += 1
    invalid_file = invocation.build_files not in {(), ("Dockerfile",)}
    if invalid_file:
        errors.append(
            f"{source}:{segment.location}: CI smoke docker build Dockerfile must be "
            "the inventoried Dockerfile"
        )
    if invocation.build_modes:
        errors.append(
            f"{source}:{segment.location}: CI smoke docker build forbids "
            f"{invocation.build_modes[0]}"
        )
    valid = (
        allow_ci_smoke
        and _is_ci_smoke_location(source, segment.location)
        and direct
        and invocation.build_context == "."
        and not invalid_file
        and not invocation.build_modes
        and invocation.tags.count(CI_SMOKE_IMAGE) == 1
        and state.smoke_build_count == 1
    )
    state.smoke_build_order = segment.order if valid else None
    return True


def _scan_non_image_invocation(
    source: str,
    segment: ShellSegment,
    invocation: DockerInvocation,
    assignments: Mapping[str, tuple[str, int]],
    inventory: ShellInventory | None,
    errors: list[str],
) -> bool:
    if invocation.verb == "build":
        inventory_error = (
            _shell_build_inventory_error(invocation, inventory, source)
            if inventory is not None
            else None
        )
    elif (invocation.verb or "").startswith("compose:"):
        inventory_error = (
            _shell_compose_inventory_error(
                invocation,
                source=source,
                order=segment.order,
                assignments=assignments,
                inventory=inventory,
            )
            if inventory is not None
            else None
        )
    elif invocation.verb in DOCKER_NON_INGRESS_VERBS:
        return True
    else:
        return False
    if inventory_error is not None:
        errors.append(f"{source}:{segment.location}: {inventory_error}")
    return True


def _scan_smoke_image(
    source: str,
    segment: ShellSegment,
    invocation: DockerInvocation,
    direct: bool,
    allow_ci_smoke: bool,
    state: ShellScanState,
    errors: list[str],
) -> None:
    if not allow_ci_smoke:
        message = "CI smoke image is only allowed in build:docker script"
    elif invocation.verb != "run":
        message = "CI smoke image is allowed only for docker run"
    elif not direct or state.smoke_build_order != segment.order - 1:
        message = (
            "CI smoke image must be built with its exact tag by the immediately preceding "
            "direct local build before docker run"
        )
    elif invocation.pull_policy != "never":
        message = "CI smoke docker run must set --pull=never"
    else:
        return
    errors.append(f"{source}:{segment.location}: {message}")


def _scan_shell_image(
    source: str,
    segment: ShellSegment,
    invocation: DockerInvocation,
    direct: bool,
    allow_ci_smoke: bool,
    state: ShellScanState,
    variables: Mapping[str, str],
    opaque: set[str],
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    if invocation.image is None:
        errors.append(
            f"{source}:{segment.location}: unsupported Docker command has no modelled image"
        )
        return
    if invocation.image == CI_SMOKE_IMAGE:
        _scan_smoke_image(source, segment, invocation, direct, allow_ci_smoke, state, errors)
        return
    if CI_SMOKE_IMAGE in segment.tokens:
        errors.append(
            f"{source}:{segment.location}: CI smoke image is reserved for one direct local "
            "build and its immediate docker run"
        )
        return
    if _shell_variable_name(invocation.image) in opaque:
        return
    reference, resolution_error = _resolve_shell_reference(invocation.image, variables)
    if resolution_error:
        errors.append(f"{source}:{segment.location}: {resolution_error}")
        return
    _add_use(uses, errors, reference, source, segment.location, "shell")


def _cwd_aliases(segments: Sequence[ShellSegment]) -> set[str]:
    aliases: set[str] = set()
    for segment in segments:
        executable_index, position_error = _resolve_command_position(segment.tokens)
        if executable_index is None or position_error:
            continue
        if Path(segment.tokens[executable_index]).name != "alias":
            continue
        for token in segment.tokens[executable_index + 1 :]:
            assignment = _assignment(token)
            if assignment is None:
                continue
            name, value = assignment
            try:
                alias_tokens = shlex.split(value, comments=False, posix=True)
            except ValueError:
                aliases.add(name)
                continue
            alias_index, alias_error = _resolve_command_position(alias_tokens)
            if alias_error or (
                alias_index is not None
                and Path(alias_tokens[alias_index]).name in {"cd", "eval", "popd", "pushd"}
            ):
                aliases.add(name)
    return aliases


def _cwd_mutation_orders(
    segments: Sequence[ShellSegment], assignments: Mapping[str, tuple[str, int]]
) -> set[int]:
    orders: set[int] = set()
    cwd_aliases = _cwd_aliases(segments)
    for segment in segments:
        executable, executable_index, executable_error = _resolved_segment_executable(
            segment, assignments
        )
        if executable is None or executable_index is None or executable_error:
            continue
        executable_name = Path(executable).name
        if executable_name in {"cd", "eval", "popd", "pushd"} or executable_name in cwd_aliases:
            orders.add(segment.order)
    return orders


def _segment_has_indirect_payload(segment: ShellSegment, executable_index: int | None) -> bool:
    return any(
        token == "<<<" or (token.startswith("-") and "c" in token[1:])
        for token in segment.tokens[(executable_index or 0) + 1 :]
    )


def _has_shell_input_redirection(
    tokens: Sequence[str], *, include_here_string: bool = False
) -> bool:
    for index, token in enumerate(tokens):
        if token in {"<", "<<", "<<<", "<&", "<>"}:
            if token == "<<<" and not include_here_string:
                continue
            descriptor = tokens[index - 1] if index > 0 and tokens[index - 1].isdigit() else None
            if descriptor is None or descriptor == "0":
                return True
            continue
        match = re.match(r"(?:(\d+))?(<<<|<<|<&|<>|<)", token)
        if match and match.group(2) == "<<<" and not include_here_string:
            continue
        if match and (match.group(1) in {None, "0"}):
            return True
    return False


def _persistent_exec_index(
    segment: ShellSegment, assignments: Mapping[str, tuple[str, int]]
) -> int | None:
    index = 0
    while index < len(segment.tokens) and (
        segment.tokens[index] == "!" or _assignment(segment.tokens[index])
    ):
        index += 1
    while index < len(segment.tokens):
        executable = Path(segment.tokens[index]).name
        variable = _shell_variable_name(segment.tokens[index])
        if variable is not None:
            assignment = assignments.get(variable)
            return index if assignment is not None and assignment[0] == "exec" else None
        if executable == "exec":
            return index
        if executable not in {"builtin", "command"}:
            return None
        wrapped_index, wrapper_error = _wrapped_command_position(segment.tokens, index, executable)
        if wrapped_index is None or wrapper_error:
            return None
        index = wrapped_index
    return None


def _persistent_stdin_mutation_orders(segments: Sequence[ShellSegment], depth: int = 0) -> set[int]:
    orders: set[int] = set()
    assignments = _unambiguous_shell_assignments(segments)
    aliases = _ci_executable_aliases(segments)
    for original in segments:
        segment, _ = _expanded_ci_alias_segment(original, aliases)
        exec_index = _persistent_exec_index(segment, assignments)
        if exec_index is not None and _has_shell_input_redirection(
            segment.tokens[exec_index + 1 :], include_here_string=True
        ):
            orders.add(segment.order)
            continue
        executable, executable_index, executable_error = _resolved_segment_executable(
            segment, assignments
        )
        if (
            depth < 5
            and executable_error is None
            and executable is not None
            and executable_index is not None
            and Path(executable).name == "eval"
            and (payload := _shell_execution_payload(segment.tokens, executable_index, executable))
            is not None
        ):
            nested_errors: list[str] = []
            nested = _shell_segments("indirect", [("", payload)], nested_errors)
            if not nested_errors and _persistent_stdin_mutation_orders(nested, depth + 1):
                orders.add(segment.order)
    return orders


def _segment_indirect_dependency_issue(
    segment: ShellSegment,
    assignments: Mapping[str, tuple[str, int]],
    stdin_mutations: set[int],
) -> str | None:
    executable, executable_index, executable_error = _resolved_segment_executable(
        segment, assignments
    )
    if executable_error:
        if _segment_has_indirect_payload(segment, executable_index) and _shell_variable_name(
            segment.tokens[executable_index or 0]
        ):
            return executable_error
        return None
    if executable is None or executable_index is None:
        return None
    if Path(executable).name in SHELL_INTERPRETERS:
        if segment.operator_before in {"|", "|&"}:
            return "shell interpreter piped stdin execution is not verifiable"
        if _has_shell_input_redirection(segment.tokens[executable_index + 1 :]) or any(
            order < segment.order for order in stdin_mutations
        ):
            return "shell interpreter stdin redirection is not verifiable"
    payload = _shell_execution_payload(segment.tokens, executable_index, executable)
    return _nested_shell_dependency_issue(payload) if payload is not None else None


def _inline_code_mentions_process_execution(command: str) -> bool:
    return any(
        marker in command
        for marker in (
            "docker",
            "subprocess",
            "os.exec",
            "os.popen",
            "os.posix_spawn",
            "os.spawn",
            "os.system",
            "pty.spawn",
        )
    )


def _scan_shell_inline_interpreter(
    source: str,
    segment: ShellSegment,
    assignments: Mapping[str, tuple[str, int]],
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    executable, executable_index, executable_error = _resolved_segment_executable(
        segment, assignments
    )
    if executable_error is not None or executable is None or executable_index is None:
        return
    python_index, python_error = _python_executable_index(
        segment.tokens, executable_index, executable
    )
    if python_error is not None:
        errors.append(f"{source}:{segment.location}: Python executable {python_error}")
        return
    if python_index is not None:
        arguments = segment.tokens[python_index + 1 :]
        command, command_error = _ci_inline_python_command(arguments)
        if command is None:
            if command_error is not None and _inline_code_mentions_process_execution(
                " ".join(arguments)
            ):
                errors.append(f"{source}:{segment.location}: {command_error}")
            return
        try:
            tree = ast.parse(command, filename=source)
        except SyntaxError as exc:
            if _inline_code_mentions_process_execution(command):
                errors.append(f"{source}:{segment.location}: inline Python parse error: {exc.msg}")
            return
        _visit_python_tree(
            _DockerRunVisitor(
                source,
                uses,
                errors,
                diagnostic_prefix=f"{segment.location}.python-c",
            ),
            tree,
            errors,
            f"{source}:{segment.location}.python-c",
        )
        return
    inline_flags = {
        "node": {"-e", "--eval"},
        "perl": {"-e", "-E"},
        "php": {"-r"},
        "ruby": {"-e"},
    }.get(Path(executable).name)
    arguments = segment.tokens[executable_index + 1 :]
    if inline_flags is not None and any(flag in arguments for flag in inline_flags):
        command = " ".join(arguments)
        if _inline_code_mentions_process_execution(command):
            errors.append(
                f"{source}:{segment.location}: inline {Path(executable).name} execution "
                "is not modelled"
            )


def _scan_shell_unit(
    source: str,
    units: Sequence[tuple[str, str]],
    uses: list[ImageUse],
    errors: list[str],
    *,
    allow_ci_smoke: bool = False,
    inventory: ShellInventory | None = None,
    parsed_segments: Sequence[ShellSegment] | None = None,
    scan_inline_interpreters: bool = True,
) -> None:
    segments = (
        list(parsed_segments)
        if parsed_segments is not None
        else _shell_segments(source, units, errors)
    )
    aliases = _ci_executable_aliases(segments)
    expanded_segments: list[ShellSegment] = []
    for segment in segments:
        expanded, alias_error = _expanded_ci_alias_segment(segment, aliases)
        if alias_error is not None:
            errors.append(f"{source}:{segment.location}: {alias_error}")
            continue
        expanded_segments.append(expanded)
    segments = expanded_segments
    variables, opaque = _shell_variable_state(source, segments, errors)
    assignments = _unambiguous_shell_assignments(segments)
    cwd_mutations = _cwd_mutation_orders(segments, assignments)
    stdin_mutations = _persistent_stdin_mutation_orders(segments)
    state = ShellScanState()
    for segment in segments:
        if scan_inline_interpreters:
            _scan_shell_inline_interpreter(source, segment, assignments, uses, errors)
        indirect_dependency = _segment_indirect_dependency_issue(
            segment, assignments, stdin_mutations
        )
        if indirect_dependency is not None:
            errors.append(f"{source}:{segment.location}: {indirect_dependency}")
        resolved = _segment_docker_invocation(source, segment, variables, opaque, errors)
        if resolved is None:
            continue
        invocation, executable_index = resolved
        direct = _direct_shell_invocation(segment, executable_index)
        if (invocation.verb == "build" or (invocation.verb or "").startswith("compose:")) and any(
            order < segment.order for order in cwd_mutations
        ):
            operation = "docker build" if invocation.verb == "build" else "Docker Compose"
            errors.append(
                f"{source}:{segment.location}: working directory mutation before {operation} "
                "repository path use is forbidden"
            )
            continue
        if _scan_smoke_build(source, segment, invocation, direct, allow_ci_smoke, state, errors):
            continue
        if _scan_non_image_invocation(source, segment, invocation, assignments, inventory, errors):
            continue
        _scan_shell_image(
            source,
            segment,
            invocation,
            direct,
            allow_ci_smoke,
            state,
            variables,
            opaque,
            uses,
            errors,
        )


def _scan_shell_text(
    source: str,
    content: str,
    uses: list[ImageUse],
    errors: list[str],
    *,
    location_prefix: str = "",
    allow_ci_smoke: bool = False,
    inventory: ShellInventory | None = None,
    parsed_segments: Sequence[ShellSegment] | None = None,
) -> None:
    _scan_shell_unit(
        source,
        [(location_prefix, content)],
        uses,
        errors,
        allow_ci_smoke=allow_ci_smoke,
        inventory=inventory,
        parsed_segments=parsed_segments,
    )


def _interpreter_script_dependencies(
    tokens: Sequence[str], interpreter_index: int
) -> tuple[str, ...]:
    index = interpreter_index + 1
    dependencies: list[str] = []
    value_options = {"--rcfile", "-O", "+O", "-o", "+o"}
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            if index + 1 < len(tokens):
                dependencies.append(tokens[index + 1])
            return tuple(dependencies)
        if option in value_options:
            if option == "--rcfile" and index + 1 < len(tokens):
                dependencies.append(tokens[index + 1])
            index += 2
            continue
        if option.startswith("--rcfile="):
            dependencies.append(option.split("=", 1)[1])
            index += 1
            continue
        if option == "<<<":
            return tuple(dependencies)
        if option.startswith(("-", "+")):
            if "c" in option[1:]:
                return tuple(dependencies)
            index += 1
            continue
        dependencies.append(option)
        return tuple(dependencies)
    return tuple(dependencies)


def _shell_dependency_tokens(segment: ShellSegment) -> tuple[str, ...]:
    executable_index, position_error = _resolve_command_position(segment.tokens)
    if executable_index is None or position_error:
        return ()
    executable_token = segment.tokens[executable_index]
    executable = Path(executable_token).name
    if executable_token in {".", "source"}:
        index = executable_index + 1
        if index < len(segment.tokens) and segment.tokens[index] == "--":
            index += 1
        return (segment.tokens[index],) if index < len(segment.tokens) else ()
    if executable in SHELL_INTERPRETERS:
        return _interpreter_script_dependencies(segment.tokens, executable_index)
    if _is_docker_executable(executable_token):
        return ()
    if executable_token.startswith(("./", "../", "$SCRIPT_DIR/")) or Path(
        executable_token
    ).suffix in {".bash", ".dash", ".sh", ".zsh"}:
        return (executable_token,)
    return ()


def _resolved_segment_executable(
    segment: ShellSegment,
    assignments: Mapping[str, tuple[str, int]],
) -> tuple[str | None, int | None, str | None]:
    executable_index, position_error = _resolve_command_position(segment.tokens)
    if executable_index is None or position_error:
        return None, executable_index, position_error
    token = segment.tokens[executable_index]
    variable = _shell_variable_name(token)
    if variable is None:
        return token, executable_index, None
    assignment = assignments.get(variable)
    if assignment is None or assignment[1] >= segment.order:
        return None, executable_index, f"unresolved executable variable {token}"
    value = assignment[0]
    if not value or "$" in value or "`" in value:
        return None, executable_index, f"dynamic executable variable {token}"
    return value, executable_index, None


def _uv_run_command_index(tokens: Sequence[str], index: int) -> tuple[int | None, str | None]:
    value_options = {"--directory", "--package", "--project", "--python"}
    boolean_options = {"--active", "--exact", "--isolated", "--locked", "--no-project"}
    while index < len(tokens):
        option = tokens[index]
        base_option = option.split("=", 1)[0]
        if "=" in option and base_option in value_options:
            index += 1
            continue
        if option in value_options:
            if index + 1 >= len(tokens):
                return None, f"uv run option {option!r} is missing its value"
            index += 2
            continue
        if option in boolean_options:
            index += 1
            continue
        if option == "--":
            index += 1
            break
        if option.startswith("-"):
            return None, f"unsupported uv run option {option!r}"
        break
    if index >= len(tokens):
        return None, "uv run Python executable is missing"
    return index, None


def _python_executable_index(
    tokens: Sequence[str], executable_index: int, executable: str | None = None
) -> tuple[int | None, str | None]:
    executable_name = Path(executable or tokens[executable_index]).name
    if _is_python_executable(executable_name):
        return executable_index, None
    if executable_name != "uv":
        return None, None
    command_index = executable_index + 1
    if command_index >= len(tokens) or tokens[command_index] != "run":
        return None, None
    index, option_error = _uv_run_command_index(tokens, command_index + 1)
    if index is None or option_error is not None:
        return None, option_error
    if not _is_python_executable(tokens[index]):
        return None, None
    return index, None


def _ci_inline_python_command(arguments: Sequence[str]) -> tuple[str | None, str | None]:
    if not arguments:
        return None, "CI Python stdin execution is not modelled"
    for index, token in enumerate(arguments):
        if token == "-c":
            if index + 1 >= len(arguments):
                return None, "python -c command is missing"
            return arguments[index + 1], None
        if token.startswith("-c") and token != "-c":
            return token[2:], None
        if token == "-m" or token.startswith("-m"):
            return None, "CI Python module dependency is not modelled"
        if token in {"-", "<", "<<", "<<<"} or token.startswith("<"):
            return None, "CI Python stdin execution is not modelled"
        if token == "--":
            script = arguments[index + 1] if index + 1 < len(arguments) else "<missing>"
            return None, f"CI Python dependency {script!r} is not modelled"
        if token.startswith("-"):
            return None, f"CI Python option {token!r} is not modelled"
        path = Path(token)
        if (
            path.suffix == ".py"
            and path.parts[:1] == ("scripts",)
            and not path.is_absolute()
            and ".." not in path.parts
            and "$" not in token
            and "`" not in token
        ):
            return None, None
        return None, f"CI Python dependency {token!r} is not modelled"
    return None, "CI Python stdin execution is not modelled"


def _shell_execution_payload(
    tokens: Sequence[str], executable_index: int, executable: str
) -> str | None:
    executable_name = Path(executable).name
    if executable_name == "eval":
        return " ".join(tokens[executable_index + 1 :])
    if executable_name not in SHELL_INTERPRETERS:
        return None
    command = _interpreter_command(tokens, executable_index)
    if command is not None:
        return command
    if "<<<" not in tokens[executable_index + 1 :]:
        return None
    redirect_index = tokens.index("<<<", executable_index + 1)
    return tokens[redirect_index + 1] if redirect_index + 1 < len(tokens) else None


def _nested_segment_dependency_issue(
    segment: ShellSegment,
    assignments: Mapping[str, tuple[str, int]],
    stdin_mutations: set[int],
    depth: int,
) -> str | None:
    dependencies = _shell_dependency_tokens(segment)
    if dependencies:
        return f"shell dependency {dependencies[0]!r} inside indirect execution is not verifiable"
    executable, executable_index, executable_error = _resolved_segment_executable(
        segment, assignments
    )
    if executable_error:
        return (
            executable_error if _segment_has_indirect_payload(segment, executable_index) else None
        )
    if executable is None or executable_index is None:
        return None
    if Path(executable).name in SHELL_INTERPRETERS:
        if segment.operator_before in {"|", "|&"}:
            return "shell interpreter piped stdin execution is not verifiable"
        if _has_shell_input_redirection(segment.tokens[executable_index + 1 :]) or any(
            order < segment.order for order in stdin_mutations
        ):
            return "shell interpreter stdin redirection is not verifiable"
    python_index, python_error = _python_executable_index(
        segment.tokens, executable_index, executable
    )
    if python_error is not None:
        return python_error
    if python_index is not None:
        return None
    payload = _shell_execution_payload(segment.tokens, executable_index, executable)
    return _nested_shell_dependency_issue(payload, depth + 1) if payload is not None else None


def _nested_shell_dependency_issue(command: str, depth: int = 0) -> str | None:
    if depth >= 5:
        return "indirect shell execution nesting exceeds the supported depth"
    nested_errors: list[str] = []
    segments = _shell_segments("indirect", [("", command)], nested_errors)
    if nested_errors:
        return "indirect shell execution cannot be parsed safely"
    assignments = _unambiguous_shell_assignments(segments)
    stdin_mutations = _persistent_stdin_mutation_orders(segments, depth)
    for segment in segments:
        issue = _nested_segment_dependency_issue(segment, assignments, stdin_mutations, depth)
        if issue is not None:
            return issue
    return None


def _is_python_executable(token: str) -> bool:
    return bool(re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(token).name))


def _visit_python_tree(
    visitor: ast.NodeVisitor,
    tree: ast.AST,
    errors: list[str],
    diagnostic_source: str,
) -> None:
    try:
        visitor.visit(tree)
    except RecursionError:
        errors.append(f"{diagnostic_source}: Python analysis recursion limit exceeded")


def _snapshot_mapping_scopes[ScopeKey, ScopeValue](
    scopes: Sequence[Mapping[ScopeKey, ScopeValue]],
) -> list[dict[ScopeKey, ScopeValue]]:
    return [dict(scope) for scope in scopes]


def _restore_mapping_scopes[ScopeKey, ScopeValue](
    scopes: Sequence[MutableMapping[ScopeKey, ScopeValue]],
    snapshots: Sequence[Mapping[ScopeKey, ScopeValue]],
) -> None:
    for scope, snapshot in zip(scopes[: len(snapshots)], snapshots, strict=True):
        scope.clear()
        scope.update(snapshot)


def _scan_ci_inline_python(
    source: str,
    segment: ShellSegment,
    executable_index: int,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    arguments = segment.tokens[executable_index + 1 :]
    command, command_error = _ci_inline_python_command(arguments)
    if command_error is not None:
        errors.append(f"{source}:{segment.location}: {command_error}")
        return
    if command is None:
        return
    assert command is not None
    try:
        tree = ast.parse(command, filename=source)
    except SyntaxError as exc:
        errors.append(f"{source}:{segment.location}: inline Python parse error: {exc.msg}")
        return
    _visit_python_tree(
        _DockerRunVisitor(
            source,
            uses,
            errors,
            diagnostic_prefix=f"{segment.location}.python-c",
        ),
        tree,
        errors,
        f"{source}:{segment.location}.python-c",
    )


def _executable_alias_definitions(segment: ShellSegment) -> list[tuple[str, tuple[str, ...]]]:
    executable_index, position_error = _resolve_command_position(segment.tokens)
    if executable_index is None or position_error:
        return []
    executable = Path(segment.tokens[executable_index]).name
    arguments = segment.tokens[executable_index + 1 :]
    definitions: list[tuple[str, tuple[str, ...]]] = []
    if executable == "alias":
        for argument in arguments:
            assignment = _assignment(argument)
            if assignment is None:
                continue
            name, value = assignment
            try:
                alias_tokens = tuple(shlex.split(value, comments=False, posix=True))
            except ValueError:
                alias_tokens = ()
            definitions.append((name, alias_tokens))
        return definitions
    if executable != "hash":
        return definitions
    for index, argument in enumerate(arguments):
        if argument == "-p" and index + 2 < len(arguments):
            definitions.append((arguments[index + 2], (arguments[index + 1],)))
    return definitions


def _ci_executable_aliases(
    segments: Sequence[ShellSegment],
) -> dict[str, tuple[tuple[str, ...], int] | None]:
    occurrences: dict[str, list[tuple[tuple[str, ...], bool, int]]] = {}
    for segment in segments:
        for name, tokens in _executable_alias_definitions(segment):
            occurrences.setdefault(name, []).append((tokens, segment.controlled, segment.order))
    aliases: dict[str, tuple[tuple[str, ...], int] | None] = {}
    for name, definitions in occurrences.items():
        tokens, controlled, order = definitions[0]
        aliases[name] = (
            (tokens, order)
            if len(definitions) == 1
            and tokens
            and not controlled
            and not any("$" in token or "`" in token for token in tokens)
            else None
        )
    return aliases


def _expanded_ci_alias_segment(
    segment: ShellSegment,
    aliases: Mapping[str, tuple[tuple[str, ...], int] | None],
) -> tuple[ShellSegment, str | None]:
    expanded = segment
    visited: set[str] = set()
    while True:
        executable_index, position_error = _resolve_command_position(expanded.tokens)
        if executable_index is None or position_error:
            return expanded, None
        alias_name = expanded.tokens[executable_index]
        if alias_name not in aliases:
            return expanded, None
        if alias_name in visited:
            return expanded, f"CI executable alias cycle involving {alias_name!r}"
        visited.add(alias_name)
        alias = aliases[alias_name]
        if alias is None or alias[1] >= segment.order:
            return (
                expanded,
                f"CI executable alias {alias_name!r} is ambiguous or not defined before use",
            )
        alias_tokens, _ = alias
        expanded = ShellSegment(
            expanded.location,
            (
                *expanded.tokens[:executable_index],
                *alias_tokens,
                *expanded.tokens[executable_index + 1 :],
            ),
            expanded.controlled,
            expanded.order,
            expanded.operator_before,
            expanded.operator_after,
        )


def _record_ci_shell_dependencies(source: str, segment: ShellSegment, errors: list[str]) -> None:
    for dependency in _shell_dependency_tokens(segment):
        errors.append(
            f"{source}:{segment.location}: CI shell dependency {dependency!r} is not modelled"
        )


def _ci_executable_dependency_error(segment: ShellSegment, executable: str) -> str | None:
    executable_index, _ = _resolve_command_position(segment.tokens)
    if executable_index is None:
        return None
    raw_executable = segment.tokens[executable_index]
    if _shell_variable_name(raw_executable) and (
        executable.startswith(("./", "../")) or Path(executable).suffix
    ):
        return f"CI executable dependency {executable!r} is not modelled"
    return None


def _scan_ci_executable_payload(
    source: str,
    segment: ShellSegment,
    executable: str,
    executable_index: int,
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    python_index, python_error = _python_executable_index(
        segment.tokens, executable_index, executable
    )
    if python_error is not None:
        errors.append(f"{source}:{segment.location}: CI Python executable {python_error}")
        return
    if python_index is not None:
        _scan_ci_inline_python(source, segment, python_index, uses, errors)
    payload = _shell_execution_payload(segment.tokens, executable_index, executable)
    if payload is None:
        return
    nested_errors: list[str] = []
    nested = _shell_segments(
        source,
        [(f"{segment.location}.indirect", payload)],
        nested_errors,
    )
    errors.extend(nested_errors)
    if nested:
        _scan_ci_dependencies(source, nested, uses, errors)


def _scan_ci_dependencies(
    source: str,
    segments: Sequence[ShellSegment],
    uses: list[ImageUse],
    errors: list[str],
) -> None:
    assignments = _unambiguous_shell_assignments(segments)
    aliases = _ci_executable_aliases(segments)
    for original_segment in segments:
        segment, alias_error = _expanded_ci_alias_segment(original_segment, aliases)
        if alias_error is not None:
            errors.append(f"{source}:{segment.location}: {alias_error}")
            continue
        executable, executable_index, executable_error = _resolved_segment_executable(
            segment, assignments
        )
        if executable_error:
            errors.append(f"{source}:{segment.location}: CI executable {executable_error}")
            continue
        if executable is None or executable_index is None:
            continue
        dependency_error = _ci_executable_dependency_error(segment, executable)
        if dependency_error is not None:
            errors.append(f"{source}:{segment.location}: {dependency_error}")
            continue
        resolved_segment = ShellSegment(
            segment.location,
            (
                *segment.tokens[:executable_index],
                executable,
                *segment.tokens[executable_index + 1 :],
            ),
            segment.controlled,
            segment.order,
            segment.operator_before,
            segment.operator_after,
        )
        _record_ci_shell_dependencies(source, resolved_segment, errors)
        _scan_ci_executable_payload(
            source,
            resolved_segment,
            executable,
            executable_index,
            uses,
            errors,
        )


def _resolve_shell_dependency(
    caller: Path,
    token: str,
    location: str,
    order: int,
    inventory: ShellInventory,
    errors: list[str],
) -> Path | None:
    root = inventory.root
    source = _relative(root, caller)
    if token == "/etc/os-release":
        return None
    if not token or any(character in token for character in "`*?[]{}") or token.startswith("~"):
        errors.append(
            f"{source}:{location}: shell dependency {token!r} must be a trusted local path"
        )
        return None
    if Path(token).is_absolute():
        errors.append(f"{source}:{location}: shell dependency {token!r} is outside the repository")
        return None
    resolved = _resolve_inventory_path(
        token,
        source=source,
        order=order,
        assignments={},
        inventory=inventory,
    )
    if resolved is None:
        errors.append(
            f"{source}:{location}: shell dependency {token!r} must use a trusted "
            "$SCRIPT_DIR path assigned once before use"
        )
        return None
    return resolved


def _trusted_script_dir_order(content: str, segments: Sequence[ShellSegment]) -> int | None:
    canonical_lines = [
        line_number
        for line_number, line in enumerate(content.splitlines(), 1)
        if line.strip() == TRUSTED_SCRIPT_DIR_ASSIGNMENT
    ]
    if len(canonical_lines) != 1:
        return None
    prefix = "\n".join(content.splitlines()[: canonical_lines[0] - 1])
    if re.search(
        r"(?:^|[;\n])\s*(?:function\s+)?(?:cd|dirname|pwd)\s*(?:\(\s*\))?\s*\{",
        prefix,
    ):
        return None
    for segment in segments:
        executable_index, position_error = _resolve_command_position(segment.tokens)
        if executable_index is None or position_error:
            continue
        executable = Path(segment.tokens[executable_index]).name
        if executable in {"alias", "eval"}:
            return None
    occurrences: dict[str, list[tuple[str, bool, int]]] = {}
    namerefs = _shell_namerefs(segments)
    for segment in segments:
        _record_shell_occurrences(segment, occurrences, namerefs)
    script_dir_occurrences = occurrences.get("SCRIPT_DIR", [])
    if len(script_dir_occurrences) != 1:
        return None
    _, controlled, order = script_dir_occurrences[0]
    if controlled:
        return None
    expected_location = str(canonical_lines[0])
    return (
        order
        if any(
            segment.order == order and segment.location == expected_location for segment in segments
        )
        else None
    )


def _backtick_end(value: str, start: int) -> int | None:
    escaped = False
    for index in range(start + 1, len(value)):
        character = value[index]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "`":
            return index
    return None


def _shell_quote_step(
    character: str,
    index: int,
    quote: str | None,
    escaped: bool,
) -> tuple[int, str | None, bool] | None:
    if escaped:
        return index + 1, quote, False
    if character == "\\" and quote != "'":
        return index + 1, quote, True
    if quote == "'":
        return index + 1, None if character == "'" else quote, False
    if character == "'" and quote is None:
        return index + 1, "'", False
    if character == '"':
        return index + 1, None if quote == '"' else '"', False
    return None


def _nested_shell_construct_step(value: str, index: int) -> tuple[int, int] | None:
    if value.startswith("$((", index):
        return index + 3, 2
    if value.startswith("$(", index):
        nested_end = _command_substitution_end(value, index)
        return (-1, 0) if nested_end is None else (nested_end + 1, 0)
    if value[index] == "`":
        nested_end = _backtick_end(value, index)
        return (-1, 0) if nested_end is None else (nested_end + 1, 0)
    return None


def _command_substitution_end(value: str, start: int) -> int | None:
    depth = 1
    quote: str | None = None
    escaped = False
    index = start + 2
    while index < len(value):
        character = value[index]
        quote_step = _shell_quote_step(character, index, quote, escaped)
        if quote_step is not None:
            index, quote, escaped = quote_step
            continue
        nested_step = _nested_shell_construct_step(value, index)
        if nested_step is not None:
            index, depth_delta = nested_step
            if index < 0:
                return None
            depth += depth_delta
            continue
        if quote is None and character == "(":
            depth += 1
        elif quote is None and character == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _shell_substitution_commands(statement: str) -> list[str]:
    active = _active_shell_code(statement)
    commands: list[str] = []

    def collect(value: str) -> None:
        index = 0
        while index < len(value):
            if value[index] == "\\":
                index += 2
                continue
            if value.startswith("$((", index):
                index += 3
                continue
            if value.startswith("$(", index):
                end = _command_substitution_end(value, index)
                if end is None:
                    return
                command = value[index + 2 : end]
                commands.append(command)
                collect(command)
                index = end + 1
                continue
            if value[index] == "`":
                end = _backtick_end(value, index)
                if end is None:
                    return
                command = value[index + 1 : end]
                commands.append(command)
                collect(command)
                index = end + 1
                continue
            index += 1

    collect(active)
    return commands


def _scan_shell_substitution_dependencies(
    source: str,
    content: str,
    errors: list[str],
) -> None:
    reported: set[tuple[int, str]] = set()
    statements, _ = _shell_statements_from_text(content)
    for line_number, statement, _ in statements:
        for command in _shell_substitution_commands(statement):
            nested_errors: list[str] = []
            segments = _shell_segments(
                source,
                [(f"{line_number}.substitution", command)],
                nested_errors,
            )
            errors.extend(nested_errors)
            direct_dependency = False
            for segment in segments:
                for dependency in _shell_dependency_tokens(segment):
                    direct_dependency = True
                    if dependency == "/etc/os-release":
                        continue
                    key = (line_number, dependency)
                    if key in reported:
                        continue
                    reported.add(key)
                    errors.append(
                        f"{source}:{segment.location}: shell dependency {dependency!r} "
                        "inside command substitution is not verifiable"
                    )
            if not direct_dependency:
                issue = _nested_shell_dependency_issue(command)
                if issue is not None:
                    errors.append(f"{source}:{line_number}.substitution: {issue}")


def _shebang_parts(line: str) -> tuple[str, str] | None:
    if not line.startswith("#!"):
        return None
    body = line[2:].strip()
    if not body:
        return None
    parts = re.split(r"[ \t]+", body, maxsplit=1)
    return parts[0], parts[1] if len(parts) == 2 else ""


def _short_env_shebang_split(
    raw_argument: str,
) -> tuple[list[str], str | None, str | None]:
    if not raw_argument.startswith("-") or raw_argument.startswith("--"):
        return [], None, None
    split_index = raw_argument.find("S", 1)
    if split_index < 1:
        return [], None, None
    prefix = raw_argument[1:split_index]
    if any(option not in {"0", "i", "v"} for option in prefix):
        return [], None, "unsupported option before env -S"
    prefix_tokens = [f"-{prefix}"] if prefix else []
    return prefix_tokens, raw_argument[split_index + 1 :], None


def _env_shebang_split(
    raw_argument: str,
) -> tuple[list[str], str | None, str | None]:
    if raw_argument.startswith("--split-string="):
        return [], raw_argument.split("=", 1)[1], None
    if raw_argument.startswith("--split-string"):
        return [], None, "env --split-string requires an attached value"
    return _short_env_shebang_split(raw_argument)


def _raw_env_shebang_parse(interpreter: str, raw_argument: str) -> EnvCommandParse:
    if not raw_argument:
        return EnvCommandParse((interpreter,), None, None)
    prefix_tokens, split_value, split_error = _env_shebang_split(raw_argument)
    if split_error is not None:
        return EnvCommandParse((interpreter,), None, split_error)
    if split_value is None:
        if any(character.isspace() for character in raw_argument):
            return EnvCommandParse(
                (interpreter,), None, "env shebang has multiple arguments without -S"
            )
        return _parse_gnu_env_command((interpreter, raw_argument), 0)
    split_tokens, split_error = _gnu_env_split_string(split_value)
    if split_error is not None or split_tokens is None:
        return EnvCommandParse((interpreter,), None, split_error, split_used=True)
    parsed = _parse_gnu_env_command((interpreter, *prefix_tokens, *split_tokens), 0)
    return EnvCommandParse(
        parsed.tokens,
        parsed.command_index,
        parsed.error,
        split_used=True,
        terminal=parsed.terminal,
    )


def _shell_shebang_status(line: str) -> tuple[bool, str | None]:
    parts = _shebang_parts(line)
    if parts is None:
        return False, None
    interpreter, raw_argument = parts
    tokens = [interpreter, *([raw_argument] if raw_argument else [])]
    index = 0
    if Path(interpreter).name == "env":
        parsed = _raw_env_shebang_parse(interpreter, raw_argument)
        if parsed.error is not None:
            return True, f"unsupported env shebang: {parsed.error}"
        tokens = list(parsed.tokens)
        if parsed.command_index is None:
            if parsed.split_used and not parsed.terminal:
                return True, "unsupported env shebang: split-string selects no interpreter"
            return False, None
        index = parsed.command_index
    if index >= len(tokens):
        return False, None
    if any(character.isspace() for character in tokens[index]):
        return True, "unsupported env shebang: executable contains whitespace"
    executable = Path(tokens[index]).name
    if executable == "busybox":
        return index + 1 < len(tokens) and tokens[index + 1] in SHELL_INTERPRETERS, None
    return executable in SHELL_INTERPRETERS, None


def _shell_shebang_is_supported(line: str) -> bool:
    supported, _ = _shell_shebang_status(line)
    return supported


def _is_shell_candidate(path: Path) -> bool:
    if path.suffix in {".ash", ".bash", ".dash", ".hush", ".sh", ".zsh"}:
        return True
    first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
    return bool(first_line and _shell_shebang_is_supported(first_line[0]))


def _shell_candidate_status(path: Path) -> tuple[bool, str | None]:
    if path.suffix in {".ash", ".bash", ".dash", ".hush", ".sh", ".zsh"}:
        return True, None
    first_line = path.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
    return _shell_shebang_status(first_line[0]) if first_line else (False, None)


def _sourced_parent_state_issue(path: Path, caller: Path) -> str | None:
    content = path.read_text(encoding="utf-8")
    parse_errors: list[str] = []
    segments = _shell_segments(path.as_posix(), [("", content)], parse_errors)
    if parse_errors:
        return "sourced dependency cannot be parsed safely"
    if _persistent_stdin_mutation_orders(segments):
        return "sourced dependency mutates parent shell stdin"
    occurrences: dict[str, list[tuple[str, bool, int]]] = {}
    namerefs = _shell_namerefs(segments)
    for segment in segments:
        _record_shell_occurrences(segment, occurrences, namerefs)
        executable_index, position_error = _resolve_command_position(segment.tokens)
        if executable_index is None or position_error:
            continue
        if Path(segment.tokens[executable_index]).name in {"cd", "eval", "popd", "pushd"}:
            return "sourced dependency mutates parent shell state"
    if "SCRIPT_DIR" in occurrences and not (
        path.parent == caller.parent and _trusted_script_dir_order(content, segments) is not None
    ):
        return "sourced dependency mutates SCRIPT_DIR"
    if re.search(r"(?:^|[;\n])\s*(?:function\s+)?(?:cd|dirname|pwd)\s*(?:\(\s*\))?\s*\{", content):
        return "sourced dependency shadows trusted path commands"
    return None


def _shell_candidates(root: Path, errors: list[str]) -> set[Path]:
    candidates: set[Path] = set()
    for path in _operational_files(root):
        is_candidate, shebang_error = _shell_candidate_status(path)
        if shebang_error is not None:
            errors.append(f"{_relative(root, path)}:1: {shebang_error}")
        if is_candidate:
            candidates.add(path)
    return candidates


def _scan_shell(
    root: Path,
    uses: list[ImageUse],
    errors: list[str],
    inventory: ShellInventory,
) -> None:
    candidates = _shell_candidates(root, errors)
    visited: set[Path] = set()

    def visit(path: Path, stack: tuple[Path, ...]) -> None:
        path = path.resolve()
        if path in stack or path in visited:
            return
        visited.add(path)
        content = path.read_text(encoding="utf-8")
        source = _relative(root, path)
        segments = _shell_segments(source, [("", content)], errors)
        trusted_order = _trusted_script_dir_order(content, segments)
        if trusted_order is not None:
            inventory.trusted_script_dirs[source] = trusted_order
        _scan_shell_substitution_dependencies(source, content, errors)
        _scan_shell_text(
            source,
            content,
            uses,
            errors,
            inventory=inventory,
            parsed_segments=segments,
        )
        for segment in segments:
            for token in _shell_dependency_tokens(segment):
                dependency = _resolve_shell_dependency(
                    path,
                    token,
                    segment.location,
                    segment.order,
                    inventory,
                    errors,
                )
                if dependency is not None:
                    executable_index, _ = _resolve_command_position(segment.tokens)
                    if (
                        executable_index is not None
                        and segment.tokens[executable_index] in {".", "source"}
                        and (parent_state_issue := _sourced_parent_state_issue(dependency, path))
                        is not None
                    ):
                        errors.append(f"{source}:{segment.location}: {parent_state_issue}")
                    visit(dependency, (*stack, path))

    for path in sorted(candidates):
        visit(path, ())


def _is_containers_run(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "run"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "containers"
    )


def _is_containers_get(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "get"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "containers"
    )


def _is_asyncio_to_thread(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "to_thread"
        and isinstance(node.value, ast.Name)
        and node.value.id == "asyncio"
    )


def _is_getattr_containers_run(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "getattr")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "getattr"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "builtins"
            )
        )
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "containers"
    )


def _is_getattr_call(node: ast.AST) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "getattr")
        or (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "getattr"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "builtins"
        )
    )


def _getattr_docker_construct(node: ast.AST) -> str | None:
    if not _is_getattr_call(node) or len(node.args) < 2:
        return None
    target, attribute = node.args[:2]
    attribute_name = (
        attribute.value
        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str)
        else "*"
    )
    namespaces = {"api", "containers", "images"}
    if isinstance(target, ast.Attribute) and target.attr in namespaces:
        return f"{target.attr}.{attribute_name}"
    if attribute_name in namespaces:
        return attribute_name
    return None


def _docker_namespace_extraction(node: ast.AST) -> str | None:
    namespaces = {"api", "containers", "images"}
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and key.value in namespaces:
            source = node.value
            if isinstance(source, ast.Attribute) and source.attr == "__dict__":
                return str(key.value)
            if isinstance(source, ast.Call) and (
                (isinstance(source.func, ast.Name) and source.func.id == "vars")
                or (
                    isinstance(source.func, ast.Attribute)
                    and source.func.attr == "vars"
                    and isinstance(source.func.value, ast.Name)
                    and source.func.value.id == "builtins"
                )
            ):
                return str(key.value)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "__getattribute__"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in namespaces
    ):
        return str(node.args[0].value)
    return None


def _obscured_docker_namespace(node: ast.AST) -> str | None:
    for descendant in ast.walk(node):
        namespace = _docker_namespace_extraction(descendant)
        if namespace is not None:
            return namespace
    return None


def _unsupported_docker_sdk_api(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
        return None
    namespace = node.value.attr
    api_name = f"{namespace}.{node.attr}"
    if namespace not in {"api", "containers", "images", "services"}:
        return None
    return None if api_name in {"containers.get", "containers.run"} else api_name


def _call_image_expression(call: ast.Call, argument_offset: int) -> ast.expr | None:
    if len(call.args) > argument_offset:
        expression = call.args[argument_offset]
        if isinstance(expression, ast.Starred):
            return None
        return expression
    for keyword in call.keywords:
        if keyword.arg == "image":
            return keyword.value
    return None


def _dotted_expression(name: str) -> ast.expr:
    parts = name.split(".")
    expression: ast.expr = ast.Name(id=parts[0], ctx=ast.Load())
    for part in parts[1:]:
        expression = ast.Attribute(value=expression, attr=part, ctx=ast.Load())
    return expression


class _ExpressionSubstituter(ast.NodeTransformer):
    def __init__(self, bindings: Mapping[str, ast.expr]) -> None:
        self._bindings = bindings

    def visit_Name(self, node: ast.Name) -> ast.expr:  # noqa: N802 - ast API
        replacement = self._bindings.get(node.id)
        return copy.deepcopy(replacement) if replacement is not None else node


class _HigherOrderSubstituter(_ExpressionSubstituter):
    def __init__(
        self,
        bindings: Mapping[str, ast.expr],
        *,
        plain_locals_enabled: bool,
        plain_vars_enabled: bool,
        local_namespace_aliases: frozenset[str],
    ) -> None:
        super().__init__(bindings)
        self._plain_locals_enabled = plain_locals_enabled
        self._plain_vars_enabled = plain_vars_enabled
        self._local_namespace_aliases = local_namespace_aliases

    @classmethod
    def _syntactic_qualified_name(cls, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.Attribute):
            owner = cls._syntactic_qualified_name(expression.value)
            return f"{owner}.{expression.attr}" if owner is not None else None
        return None

    def _locals_mapping(self, expression: ast.expr) -> bool:
        if not (
            isinstance(expression, ast.Call) and not expression.args and not expression.keywords
        ):
            return False
        qualified = self._syntactic_qualified_name(expression.func)
        while qualified is not None and qualified.endswith(".__call__"):
            qualified = qualified.removesuffix(".__call__")
        return (
            qualified in {"builtins.locals", "builtins.vars"}
            or qualified in self._local_namespace_aliases
            or (qualified == "locals" and self._plain_locals_enabled)
            or (qualified == "vars" and self._plain_vars_enabled)
        )

    def _runtime_local_name(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Subscript):
            mapping, key = expression.value, expression.slice
        elif isinstance(expression, ast.Call):
            function = expression.func
            qualified = self._syntactic_qualified_name(function)
            if (
                qualified in {"dict.__getitem__", "dict.get", "operator.getitem"}
                and len(expression.args) >= 2
            ):
                mapping, key = expression.args[0], expression.args[1]
            elif (
                isinstance(function, ast.Attribute)
                and function.attr in {"__getitem__", "get"}
                and expression.args
            ):
                mapping, key = function.value, expression.args[0]
            else:
                return None
        else:
            return None
        return (
            str(key.value)
            if self._locals_mapping(mapping)
            and isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            else None
        )

    def _replace_runtime_local(self, expression: ast.expr) -> ast.expr:
        name = self._runtime_local_name(expression)
        replacement = self._bindings.get(name) if name is not None else None
        return copy.deepcopy(replacement) if replacement is not None else expression

    def references_runtime_local(self, node: ast.AST) -> bool:
        return any(
            isinstance(descendant, ast.expr)
            and (name := self._runtime_local_name(descendant)) is not None
            and name in self._bindings
            for descendant in ast.walk(node)
        )

    def visit_Subscript(self, node: ast.Subscript) -> ast.expr:  # noqa: N802 - ast API
        self.generic_visit(node)
        return self._replace_runtime_local(node)

    def visit_Call(self, node: ast.Call) -> ast.expr:  # noqa: N802 - ast API
        self.generic_visit(node)
        return self._replace_runtime_local(node)


class _DockerRunVisitor(ast.NodeVisitor):
    def __init__(
        self,
        source: str,
        uses: list[ImageUse],
        errors: list[str],
        *,
        diagnostic_prefix: str | None = None,
        local_modules: frozenset[str] = frozenset(),
    ) -> None:
        self._source = source
        self._diagnostic_source = (
            source if diagnostic_prefix is None else f"{source}:{diagnostic_prefix}"
        )
        self._diagnostic_prefix = diagnostic_prefix
        self._uses = uses
        self._errors = errors
        self._local_modules = local_modules
        self._recognised_sdk_attributes: set[int] = set()
        self._bindings: list[dict[str, ast.expr]] = [{}]
        self._docker_cli_aliases: list[set[str]] = [set()]
        self._docker_cli_payload_aliases: list[set[str]] = [set()]
        self._dynamic_python_aliases: list[set[str]] = [set()]
        self._getattr_aliases: list[set[str]] = [set()]
        self._vars_aliases: list[set[str]] = [set()]
        self._getattribute_aliases: list[set[str]] = [set()]
        self._local_callable_aliases: list[set[str]] = [set()]
        self._local_module_aliases: list[set[str]] = [set()]
        self._function_definitions: list[dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = [{}]
        self._function_wrapper_dependencies: list[dict[str, set[str]]] = [{}]
        # Definitions stay structurally immutable for one visitor run; every expression
        # substitution operates on a deep copy. Invalidate this cache if that changes.
        self._owned_calls_cache: dict[
            ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
            tuple[tuple[ast.Call, bool], ...],
        ] = {}
        self._class_definitions: list[dict[str, ast.ClassDef]] = [{}]
        self._docker_cli_wrapper_specs: list[dict[str, DockerCliWrapperSpec]] = [{}]
        self._class_wrapper_specs: list[dict[tuple[str, str], tuple[DockerCliWrapperSpec, str]]] = [
            {}
        ]
        self._class_callable_factory_specs: list[dict[tuple[str, str], DockerCliWrapperSpec]] = [{}]
        self._class_property_factory_specs: list[dict[tuple[str, str], DockerCliWrapperSpec]] = [{}]
        self._instance_classes: list[dict[str, str]] = [{}]
        self._class_aliases: list[dict[str, str]] = [{}]
        self._class_bases: list[dict[str, tuple[str, ...]]] = [{}]
        self._callable_factory_specs: list[dict[str, DockerCliWrapperSpec]] = [{}]
        self._callable_decorator_factory_specs: list[dict[str, DockerCliWrapperSpec]] = [{}]
        self._callable_layers: list[dict[str, CallableLayers]] = [{}]
        self._scope_kinds = ["module"]
        self._scope_local_names = [set[str]()]
        self._scope_loaded_names = [set[str]()]
        self._external_payload_taints = [set[str]()]
        self._safe_payload_paths = [set[str]()]
        self._function_external_effects: list[dict[str, FunctionExternalEffects]] = [{}]
        self._pending_external_effects: list[list[ExternalPayloadEffect]] = [[]]
        self._global_declarations = [set[str]()]
        self._nonlocal_declarations = [set[str]()]
        self._conditional_depth = 0
        self._active_callable_definitions: set[int] = set()
        self._postponed_annotations = False

    def _visible_scope_indexes(self, *, include_current: bool = True) -> tuple[int, ...]:
        skip_class_scopes = self._scope_kinds[-1] == "function"
        start = len(self._bindings) - (1 if include_current else 2)
        return tuple(
            index
            for index in range(start, -1, -1)
            if not (skip_class_scopes and self._scope_kinds[index] == "class")
        )

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802 - ast API
        previous = self._postponed_annotations
        self._postponed_annotations = any(
            isinstance(statement, ast.ImportFrom)
            and statement.module == "__future__"
            and any(alias.name == "annotations" for alias in statement.names)
            for statement in node.body
        )
        self.generic_visit(node)
        self._postponed_annotations = previous

    def _binding(self, name: str) -> ast.expr | None:
        for index in self._visible_scope_indexes():
            scope = self._bindings[index]
            if name in scope:
                return scope[name]
        return None

    def _resolve_expression(self, expression: ast.expr, seen: set[str] | None = None) -> ast.expr:
        if isinstance(expression, ast.NamedExpr):
            return self._resolve_expression(expression.value, seen)
        if not isinstance(expression, ast.Name):
            return expression
        visited = set() if seen is None else seen
        if expression.id in visited:
            return expression
        bound = self._binding(expression.id)
        if bound is None:
            return expression
        visited.add(expression.id)
        return self._resolve_expression(bound, visited)

    def _snapshot_expression(self, expression: ast.expr) -> ast.expr:
        if isinstance(expression, ast.Name):
            return self._resolve_expression(expression)
        if isinstance(expression, ast.Attribute):
            return ast.Attribute(
                value=self._snapshot_expression(expression.value),
                attr=expression.attr,
                ctx=expression.ctx,
            )
        if isinstance(expression, ast.Subscript):
            owner = self._snapshot_expression(expression.value)
            key = self._snapshot_expression(expression.slice)
            mapped = self._static_dict_value(owner, key)
            return (
                self._snapshot_expression(mapped)
                if mapped is not None
                else ast.Subscript(value=owner, slice=key, ctx=expression.ctx)
            )
        return expression

    def _resolved_value_expression(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> ast.expr:
        resolved = self._resolve_expression(expression)
        visited = set() if seen is None else seen
        if id(resolved) in visited:
            return resolved
        nested_seen = visited | {id(resolved)}
        if isinstance(resolved, ast.Attribute):
            return ast.Attribute(
                value=self._resolved_value_expression(resolved.value, nested_seen),
                attr=resolved.attr,
                ctx=resolved.ctx,
            )
        if isinstance(resolved, ast.Subscript):
            owner = self._resolved_value_expression(resolved.value, nested_seen)
            key = self._resolved_value_expression(resolved.slice, nested_seen)
            mapped = self._static_dict_value(owner, key) or self._static_sequence_value(owner, key)
            return (
                self._resolved_value_expression(mapped, nested_seen)
                if mapped is not None
                else ast.Subscript(
                    value=owner,
                    slice=key,
                    ctx=resolved.ctx,
                )
            )
        if isinstance(resolved, ast.Call):
            function = self._resolved_value_expression(resolved.func, nested_seen)
            arguments = [
                self._resolved_value_expression(argument, nested_seen) for argument in resolved.args
            ]
            if (
                isinstance(function, ast.Attribute)
                and function.attr in {"get", "pop", "__getitem__"}
                and arguments
            ):
                mapped = self._static_dict_value(function.value, arguments[0])
                if mapped is not None:
                    return self._resolved_value_expression(mapped, nested_seen)
            selected = self._static_sequence_call_value(function, arguments, resolved.keywords)
            if selected is not None:
                return self._resolved_value_expression(selected, nested_seen)
            return ast.Call(
                func=function,
                args=arguments,
                keywords=list(resolved.keywords),
            )
        return resolved

    @staticmethod
    def _static_dict_value(mapping: ast.expr, key: ast.expr) -> ast.expr | None:
        if not (
            isinstance(mapping, ast.Dict)
            and isinstance(key, ast.Constant)
            and isinstance(key.value, str)
        ):
            return None
        for candidate, value in zip(mapping.keys, mapping.values, strict=True):
            if isinstance(candidate, ast.Constant) and candidate.value == key.value:
                return value
        return None

    @staticmethod
    def _static_sequence_value(sequence: ast.expr, key: ast.expr) -> ast.expr | None:
        if not isinstance(sequence, (ast.List, ast.Tuple)):
            return None
        if isinstance(key, ast.Constant) and isinstance(key.value, int):
            try:
                return sequence.elts[key.value]
            except IndexError:
                return None
        if not isinstance(key, ast.Slice):
            return None
        bounds: list[int | None] = []
        for bound in (key.lower, key.upper, key.step):
            if bound is None:
                bounds.append(None)
                continue
            if not isinstance(bound, ast.Constant) or not isinstance(bound.value, int):
                return None
            bounds.append(bound.value)
        try:
            elements = sequence.elts[slice(*bounds)]
        except ValueError:
            return None
        selected: ast.List | ast.Tuple
        if isinstance(sequence, ast.List):
            selected = ast.List(elts=list(elements), ctx=ast.Load())
        else:
            selected = ast.Tuple(elts=list(elements), ctx=ast.Load())
        return ast.copy_location(selected, sequence)

    @classmethod
    def _static_sequence_call_value(
        cls,
        function: ast.expr,
        arguments: Sequence[ast.expr],
        keywords: Sequence[ast.keyword],
    ) -> ast.expr | None:
        if keywords or not isinstance(function, ast.Attribute):
            return None
        owner = function.value
        if function.attr == "pop" and isinstance(owner, ast.List) and not arguments:
            return owner.elts[-1] if owner.elts else None
        if function.attr not in {"pop", "__getitem__"} or len(arguments) != 1:
            return None
        if function.attr == "pop" and not isinstance(owner, ast.List):
            return None
        return cls._static_sequence_value(owner, arguments[0])

    def _qualified_name(self, expression: ast.expr, seen: set[int] | None = None) -> str | None:
        resolved = self._resolve_expression(expression)
        visited = set() if seen is None else seen
        if id(resolved) in visited:
            return None
        visited.add(id(resolved))
        if isinstance(resolved, ast.Name):
            return resolved.id
        if isinstance(resolved, ast.Attribute):
            owner = self._qualified_name(resolved.value, visited)
            return f"{owner}.{resolved.attr}" if owner is not None else None
        imported_module = self._import_call_qualified_name(resolved, set(visited))
        if imported_module is not None:
            return imported_module
        attrgetter_target = self._attrgetter_qualified_name(resolved, set(visited))
        if attrgetter_target is not None:
            return attrgetter_target
        getitem_target = self._mapping_getitem_qualified_name(resolved, set(visited))
        if getitem_target is not None:
            return getitem_target
        getattribute_target = self._getattribute_qualified_name(resolved, set(visited))
        if getattribute_target is not None:
            return getattribute_target
        if self._is_resolved_getattr_call(resolved, set(visited)) and len(resolved.args) >= 2:
            owner = self._qualified_name(resolved.args[0], set(visited))
            attribute = self._static_string(resolved.args[1])
            if owner is not None and attribute is not None:
                return f"{owner}.{attribute}"
            return None
        return self._subscript_qualified_name(resolved, visited)

    @staticmethod
    def _without_terminal_dunder_calls(qualified: str | None) -> str | None:
        while qualified is not None and qualified.endswith(".__call__"):
            qualified = qualified.removesuffix(".__call__")
        return qualified

    @staticmethod
    def _without_terminal_dunder_call_attributes(expression: ast.expr) -> ast.expr:
        while isinstance(expression, ast.Attribute) and expression.attr == "__call__":
            expression = expression.value
        return expression

    def _plain_builtin_name_available(self, name: str) -> bool:
        for scope in self._visible_scope_indexes():
            binding = self._bindings[scope].get(name)
            if binding is not None:
                return self._syntactic_qualified_name(binding) == f"builtins.{name}"
            if name in self._scope_local_names[scope]:
                return False
        return True

    def _runtime_namespace_callable_name(self, expression: ast.expr) -> str | None:
        qualified = self._without_terminal_dunder_calls(self._qualified_name(expression))
        if qualified in {"globals", "locals", "vars"} and not self._plain_builtin_name_available(
            qualified
        ):
            return None
        return qualified

    def _subscript_qualified_name(self, expression: ast.expr, seen: set[int]) -> str | None:
        if isinstance(expression, ast.Subscript):
            reflected_owner = self._reflection_mapping_owner(expression.value)
            owner = (
                self._qualified_name(reflected_owner, set(seen))
                if reflected_owner is not None
                else None
            )
            key_expression = self._resolve_expression(expression.slice)
            if (
                owner is not None
                and isinstance(key_expression, ast.Constant)
                and isinstance(key_expression.value, str)
            ):
                return f"{owner}.{key_expression.value}"
        if isinstance(expression, ast.Subscript):
            owner = self._qualified_name(expression.value, seen)
            key_expression = self._resolve_expression(expression.slice)
            if (
                owner == "__builtins__"
                and isinstance(key_expression, ast.Constant)
                and key_expression.value in {"eval", "exec"}
            ):
                return f"builtins.{key_expression.value}"
        return None

    def _reflection_mapping_owner(self, expression: ast.expr) -> ast.expr | None:
        resolved = self._resolve_expression(expression)
        if isinstance(resolved, ast.Attribute) and resolved.attr == "__dict__":
            return resolved.value
        if (
            isinstance(resolved, ast.Call)
            and len(resolved.args) == 1
            and not resolved.keywords
            and self._vars_callable(resolved.func)
        ):
            return resolved.args[0]
        return None

    def _getattribute_qualified_name(self, expression: ast.expr, seen: set[int]) -> str | None:
        if not isinstance(expression, ast.Call):
            return None
        function = self._resolve_expression(expression.func)
        if not isinstance(function, ast.Attribute) or function.attr != "__getattribute__":
            return None
        function_owner = self._qualified_name(function.value, set(seen))
        if function_owner in {"builtins.object", "object"}:
            if len(expression.args) < 2:
                return None
            owner_expression = expression.args[0]
            attribute_expression = expression.args[1]
        else:
            if not expression.args:
                return None
            owner_expression = function.value
            attribute_expression = expression.args[0]
        owner = self._qualified_name(owner_expression, set(seen))
        attribute = self._static_string(attribute_expression)
        return f"{owner}.{attribute}" if owner is not None and attribute is not None else None

    def _mapping_getitem_qualified_name(self, expression: ast.expr, seen: set[int]) -> str | None:
        if not isinstance(expression, ast.Call):
            return None
        function = self._resolve_expression(expression.func)
        qualified_function = self._qualified_name(function, set(seen))
        if (
            qualified_function in {"dict.__getitem__", "operator.getitem"}
            and len(expression.args) >= 2
        ):
            mapping = expression.args[0]
            key = expression.args[1]
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "__getitem__"
            and expression.args
        ):
            mapping = function.value
            key = expression.args[0]
        else:
            return None
        reflected_owner = self._reflection_mapping_owner(mapping)
        if reflected_owner is None:
            return None
        owner = self._qualified_name(reflected_owner, set(seen))
        attribute = self._static_string(key)
        return f"{owner}.{attribute}" if owner is not None and attribute is not None else None

    def _import_call_qualified_name(self, expression: ast.expr, seen: set[int]) -> str | None:
        if not isinstance(expression, ast.Call) or not expression.args:
            return None
        callable_name = self._qualified_name(expression.func, seen)
        if callable_name not in {
            "__import__",
            "builtins.__import__",
            "importlib.import_module",
        }:
            return None
        return self._static_string(expression.args[0])

    def _attrgetter_qualified_name(self, expression: ast.expr, seen: set[int]) -> str | None:
        if not (
            isinstance(expression, ast.Call)
            and len(expression.args) == 1
            and not expression.keywords
            and isinstance(expression.func, ast.Call)
            and len(expression.func.args) == 1
            and not expression.func.keywords
            and self._attrgetter_callable(expression.func.func)
        ):
            return None
        owner = self._qualified_name(expression.args[0], seen)
        attribute = self._static_string(expression.func.args[0])
        return f"{owner}.{attribute}" if owner is not None and attribute is not None else None

    def _is_resolved_getattr_call(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> TypeGuard[ast.Call]:
        if not isinstance(expression, ast.Call):
            return False
        visited = {id(expression)} if seen is None else seen
        return self._marked_name(expression.func, self._getattr_aliases) or self._qualified_name(
            expression.func, visited
        ) in {
            "builtins.getattr",
            "getattr",
        }

    def _marked_name(self, expression: ast.expr, scopes: Sequence[set[str]]) -> bool:
        if not isinstance(expression, ast.Name):
            return False
        for index in self._visible_scope_indexes():
            if expression.id in scopes[index]:
                return True
            if expression.id in self._bindings[index]:
                return False
        return False

    def _getattr_callable(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._getattr_aliases) or (
            self._qualified_name(expression) in {"builtins.getattr", "getattr"}
        )

    def _attrgetter_callable(self, expression: ast.expr) -> bool:
        return self._qualified_name(expression) == "operator.attrgetter"

    def _vars_callable(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._vars_aliases) or (
            self._qualified_name(expression) in {"builtins.vars", "vars"}
        )

    def _getattribute_callable(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._getattribute_aliases) or (
            isinstance(expression, ast.Attribute) and expression.attr == "__getattribute__"
        )

    def _local_callable(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._local_callable_aliases)

    def _local_module(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._local_module_aliases)

    def _local_callable_boundary(self, expression: ast.expr) -> bool:
        if self._local_callable(expression):
            return True
        resolved = self._resolve_expression(expression)
        if self._is_resolved_getattr_call(resolved) and resolved.args:
            if self._local_module(resolved.args[0]):
                return True
        if isinstance(resolved, ast.Subscript):
            reflected_owner = self._reflection_mapping_owner(resolved.value)
            if reflected_owner is not None and self._local_module(reflected_owner):
                return True
        qualified = self._qualified_name(resolved)
        if qualified is not None and any(
            qualified == module or qualified.startswith(f"{module}.")
            for module in self._local_modules
        ):
            return True
        owner = resolved
        while isinstance(owner, ast.Attribute):
            owner = owner.value
        return self._local_module(owner)

    def _local_python_module(self, module: str) -> bool:
        if not module:
            return False
        candidates = {module}
        parent_parts = Path(self._source).parent.parts
        if parent_parts:
            candidates.add(".".join((*parent_parts, *module.split("."))))
        return any(
            local_module == candidate or local_module.startswith(f"{candidate}.")
            for candidate in candidates
            for local_module in self._local_modules
        )

    def _known_class_name(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            for index in self._visible_scope_indexes():
                alias = self._class_aliases[index].get(expression.id)
                if alias is not None:
                    return alias
                if expression.id in self._bindings[index]:
                    break
        resolved = self._resolve_expression(expression)
        if not isinstance(resolved, ast.Name):
            return None
        if any(
            class_name == resolved.id
            for index in self._visible_scope_indexes()
            for class_name, _ in self._class_wrapper_specs[index]
        ) or any(
            resolved.id in self._class_bases[index] for index in self._visible_scope_indexes()
        ):
            return resolved.id
        return None

    def _forget_local_name(self, name: str) -> None:
        if self._conditional_depth:
            return
        self._forget_binding_facts(-1, name)

    def _forget_binding_facts(self, scope: int, name: str) -> None:
        self._bindings[scope].pop(name, None)
        for facts in (
            self._function_definitions,
            self._class_definitions,
            self._docker_cli_wrapper_specs,
            self._instance_classes,
            self._class_aliases,
            self._class_bases,
            self._callable_factory_specs,
            self._callable_decorator_factory_specs,
            self._callable_layers,
            self._function_external_effects,
        ):
            facts[scope].pop(name, None)
        self._class_wrapper_specs[scope] = {
            key: value for key, value in self._class_wrapper_specs[scope].items() if key[0] != name
        }
        self._class_callable_factory_specs[scope] = {
            key: value
            for key, value in self._class_callable_factory_specs[scope].items()
            if key[0] != name
        }
        self._class_property_factory_specs[scope] = {
            key: value
            for key, value in self._class_property_factory_specs[scope].items()
            if key[0] != name
        }
        self._discard_safe_payload_paths(scope, name)
        self._discard_binding_markers(scope, name)

    def _external_target_scope(self, name: str, *, implicit_path: bool = False) -> int | None:
        if name in self._global_declarations[-1]:
            return 0
        if name in self._nonlocal_declarations[-1]:
            fallback: int | None = None
            for index in range(len(self._scope_kinds) - 2, 0, -1):
                if self._scope_kinds[index] != "function":
                    continue
                if fallback is None:
                    fallback = index
                if name in self._scope_local_names[index]:
                    return index
            return fallback
        if implicit_path:
            for index in self._visible_scope_indexes(include_current=False):
                if name in self._bindings[index] or name in self._scope_local_names[index]:
                    return index
        return None

    def _external_payload_candidate(self, value: ast.expr) -> bool:
        if (
            self._docker_cli_expression(value)
            or self._docker_executable_expression(value)
            or self._contains_dynamic_local_reference(value)
        ):
            return True
        resolved = self._resolved_value_expression(value)
        return isinstance(resolved, (ast.Attribute, ast.Call, ast.Starred, ast.Subscript))

    def _reject_external_payload_mutation(
        self,
        name: str,
        value: ast.expr,
        *,
        replace: bool = False,
        implicit_path: bool = False,
    ) -> None:
        target_scope = self._external_target_scope(name, implicit_path=implicit_path)
        candidate = self._external_payload_candidate(value)
        if target_scope is None:
            if replace and not self._conditional_depth and not candidate:
                self._external_payload_taints[-1].discard(name)
            return
        effect = ExternalPayloadEffect(
            target_scope,
            name,
            self._snapshot_expression(value),
            replace,
            bool(self._conditional_depth),
            getattr(value, "end_lineno", getattr(value, "lineno", 10**9)),
            getattr(value, "end_col_offset", getattr(value, "col_offset", 10**9)),
        )
        self._pending_external_effects[-1].append(effect)
        if replace and not self._conditional_depth:
            self._external_payload_taints[target_scope].discard(name)
        if candidate:
            self._discard_safe_payload_paths(target_scope, name)
            self._external_payload_taints[target_scope].add(name)

    def _external_payload_tainted(self, name: str) -> bool:
        for index in self._visible_scope_indexes():
            if name in self._external_payload_taints[index]:
                return True
            if name in self._bindings[index] or name in self._scope_local_names[index]:
                return False
        return False

    def _payload_root_name(self, expression: ast.expr) -> str | None:
        current = expression
        seen: set[int] = set()
        while isinstance(current, (ast.Attribute, ast.Subscript)):
            if id(current) in seen:
                return None
            seen.add(id(current))
            reflected_owner = (
                self._reflection_mapping_owner(current.value)
                if isinstance(current, ast.Subscript)
                else None
            )
            current = reflected_owner or current.value
        return current.id if isinstance(current, ast.Name) else None

    def _payload_path(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.Attribute):
            owner = self._payload_path(expression.value)
            return f"{owner}.{expression.attr}" if owner is not None else None
        if isinstance(expression, ast.Subscript):
            reflected_owner = self._reflection_mapping_owner(expression.value)
            owner = self._payload_path(reflected_owner or expression.value)
            key = self._resolve_expression(expression.slice)
            if (
                owner is None
                or not isinstance(key, ast.Constant)
                or not isinstance(key.value, (int, str))
            ):
                return None
            return f"{owner}[{key.value!r}]"
        return None

    def _safe_payload_path(self, expression: ast.expr) -> bool:
        path = self._payload_path(expression)
        if path is None:
            return False
        root = self._payload_root_name(expression)
        for index in self._visible_scope_indexes():
            if path in self._safe_payload_paths[index]:
                return True
            if root is not None and (
                root in self._bindings[index] or root in self._scope_local_names[index]
            ):
                return False
        return False

    def _discard_safe_payload_paths(self, scope: int, name: str) -> None:
        self._safe_payload_paths[scope] = {
            path
            for path in self._safe_payload_paths[scope]
            if not (path == name or path.startswith(f"{name}.") or path.startswith(f"{name}["))
        }

    def _function_external_effect_spec(
        self, expression: ast.expr
    ) -> FunctionExternalEffects | None:
        runtime_symbol = (
            self._runtime_namespace_symbol(expression)
            if isinstance(expression, (ast.Call, ast.Subscript))
            else None
        )
        if runtime_symbol is not None:
            scope, name = runtime_symbol
            return self._function_external_effects[scope].get(name)
        candidates: list[str] = []
        if isinstance(expression, ast.Name):
            candidates.append(expression.id)
        resolved = self._resolve_expression(expression)
        if isinstance(resolved, ast.Name) and resolved.id not in candidates:
            candidates.append(resolved.id)
        for name in candidates:
            for index in self._visible_scope_indexes():
                spec = self._function_external_effects[index].get(name)
                if spec is not None:
                    return spec
                if name in self._bindings[index]:
                    break
        return None

    def _external_effect_call_bindings(
        self,
        node: ast.Call,
        definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> dict[str, ast.expr]:
        signature = DockerCliWrapperSpec(
            self._positional_parameter_names(definition.args),
            frozenset(parameter.arg for parameter in definition.args.kwonlyargs),
            definition.args.vararg.arg if definition.args.vararg is not None else None,
            definition.args.kwarg.arg if definition.args.kwarg is not None else None,
            frozenset(),
            frozenset(),
        )
        bindings, _ = self._wrapper_call_bindings(node, signature)
        for name, default in self._function_default_expressions(definition).items():
            bindings.setdefault(name, default)
        return bindings

    @classmethod
    def _collect_owned_call_tree(
        cls,
        current: ast.AST,
        conditional: bool,
        calls: list[tuple[ast.Call, bool]],
    ) -> None:
        if isinstance(current, PYTHON_SCOPE_BOUNDARIES):
            return
        if isinstance(current, ast.AnnAssign):
            cls._collect_owned_call_tree(current.target, conditional, calls)
            if current.value is not None:
                cls._collect_owned_call_tree(current.value, conditional, calls)
            return
        child_conditional = conditional or isinstance(current, PYTHON_CONDITIONAL_NODES)
        for _, value in ast.iter_fields(current):
            cls._collect_owned_call_field(value, child_conditional, calls)
        if isinstance(current, ast.Call):
            calls.append((current, conditional))

    @classmethod
    def _collect_owned_call_field(
        cls,
        value: object,
        conditional: bool,
        calls: list[tuple[ast.Call, bool]],
    ) -> None:
        if isinstance(value, list):
            cls._collect_owned_call_sequence(value, conditional, calls)
        elif isinstance(value, ast.AST):
            cls._collect_owned_call_tree(value, conditional, calls)

    @classmethod
    def _collect_owned_call_sequence(
        cls,
        nodes: Sequence[object],
        conditional: bool,
        calls: list[tuple[ast.Call, bool]],
    ) -> None:
        for child in nodes:
            if not isinstance(child, ast.AST):
                continue
            cls._collect_owned_call_tree(child, conditional, calls)
            if isinstance(child, PYTHON_FLOW_TERMINATORS):
                break

    def _owned_calls(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> list[tuple[ast.Call, bool]]:
        cached = self._owned_calls_cache.get(node)
        if cached is not None:
            return list(cached)
        calls: list[tuple[ast.Call, bool]] = []
        roots = [node.body] if isinstance(node, ast.Lambda) else node.body
        self._collect_owned_call_sequence(roots, False, calls)
        cached = tuple(calls)
        self._owned_calls_cache[node] = cached
        return list(cached)

    def _instantiate_external_effects(
        self, spec: FunctionExternalEffects, node: ast.Call
    ) -> tuple[ExternalPayloadEffect, ...]:
        bindings = self._external_effect_call_bindings(node, spec.definition)
        return tuple(
            ExternalPayloadEffect(
                effect.target_scope,
                effect.name,
                _ExpressionSubstituter(bindings).visit(copy.deepcopy(effect.value)),
                effect.replace,
                effect.conditional,
                effect.source_line,
                effect.source_col,
            )
            for effect in spec.effects
        )

    @staticmethod
    def _external_effect_position(effect: ExternalPayloadEffect) -> tuple[int, int]:
        return effect.source_line, effect.source_col

    def _nested_external_effect_events(
        self,
        definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        seen: set[int],
        sequence: int,
    ) -> list[tuple[int, int, int, int, ExternalPayloadEffect]]:
        events: list[tuple[int, int, int, int, ExternalPayloadEffect]] = []
        for call, conditional in self._owned_calls(definition):
            invocation = self._external_effect_invocation(call, seen)
            if invocation is None:
                continue
            nested_spec, nested_call = invocation
            for effect in self._instantiate_external_effects(nested_spec, nested_call):
                if conditional and not effect.conditional:
                    effect = ExternalPayloadEffect(
                        effect.target_scope,
                        effect.name,
                        effect.value,
                        effect.replace,
                        conditional=True,
                        source_line=effect.source_line,
                        source_col=effect.source_col,
                    )
                events.append(
                    (
                        getattr(call, "end_lineno", call.lineno),
                        getattr(call, "end_col_offset", call.col_offset),
                        0,
                        sequence,
                        effect,
                    )
                )
                sequence += 1
        return events

    def _resolved_external_effect_spec(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> FunctionExternalEffects | None:
        direct = self._function_external_effect_spec(expression)
        definition = (
            direct.definition
            if direct is not None
            else self._function_definition_for_callable(expression)
        )
        if definition is None:
            return None
        visited = set() if seen is None else seen
        if id(definition) in visited:
            return None
        nested_seen = visited | {id(definition)}
        direct_effects = direct.effects if direct is not None else ()
        events = [
            (*self._external_effect_position(effect), 1, sequence, effect)
            for sequence, effect in enumerate(direct_effects)
        ]
        events.extend(
            self._nested_external_effect_events(definition, nested_seen, len(direct_effects))
        )
        events.sort(key=lambda event: event[:4])
        effects = [event[4] for event in events]
        return FunctionExternalEffects(definition, tuple(effects)) if effects else None

    def _external_effect_invocation(
        self, node: ast.Call, seen: set[int] | None = None
    ) -> tuple[FunctionExternalEffects, ast.Call] | None:
        resolved_callable = self._resolve_expression(node.func)
        if (
            isinstance(resolved_callable, ast.Call)
            and self._qualified_name(resolved_callable.func) == "functools.partial"
            and resolved_callable.args
        ):
            target = resolved_callable.args[0]
            spec = self._resolved_external_effect_spec(target, seen)
            if spec is None:
                return None
            return spec, ast.Call(
                func=target,
                args=[*resolved_callable.args[1:], *node.args],
                keywords=[*resolved_callable.keywords, *node.keywords],
            )
        spec = self._resolved_external_effect_spec(node.func, seen)
        return (spec, node) if spec is not None else None

    def _apply_external_payload_effects(self, node: ast.Call) -> None:
        invocation = self._external_effect_invocation(node)
        if invocation is None:
            return
        spec, effective_call = invocation
        parameters = set(self._function_parameter_names(spec.definition.args))
        for effect in self._instantiate_external_effects(spec, effective_call):
            if effect.target_scope >= len(self._external_payload_taints):
                self._errors.append(
                    f"{self._diagnostic_source}:{node.lineno}: unresolved external payload scope"
                )
                continue
            value = effect.value
            unresolved_parameter = bool(self._parameter_references(value, parameters))
            dangerous = unresolved_parameter or self._external_payload_candidate(value)
            conditional = effect.conditional or bool(self._conditional_depth)
            if effect.replace and not conditional:
                self._external_payload_taints[effect.target_scope].discard(effect.name)
                self._forget_binding_facts(effect.target_scope, effect.name)
                self._bindings[effect.target_scope][effect.name] = self._snapshot_expression(value)
            if dangerous:
                self._discard_safe_payload_paths(effect.target_scope, effect.name)
                self._external_payload_taints[effect.target_scope].add(effect.name)

    def _contains_dynamic_local_reference(self, expression: ast.expr) -> bool:
        for descendant in ast.walk(expression):
            if not isinstance(descendant, ast.Name):
                continue
            resolved = self._resolve_expression(descendant)
            if not isinstance(resolved, ast.Name):
                continue
            for index in self._visible_scope_indexes():
                bindings = self._bindings[index]
                binding = bindings.get(resolved.id)
                if binding is None:
                    continue
                if isinstance(binding, ast.Name) and binding.id == resolved.id:
                    return True
                break
        return False

    def _binding_marker_scopes(self) -> tuple[list[set[str]], ...]:
        return (
            self._docker_cli_aliases,
            self._docker_cli_payload_aliases,
            self._dynamic_python_aliases,
            self._getattr_aliases,
            self._vars_aliases,
            self._getattribute_aliases,
            self._local_callable_aliases,
            self._local_module_aliases,
        )

    def _discard_binding_markers(self, scope: int, name: str) -> None:
        for marker_scopes in self._binding_marker_scopes():
            marker_scopes[scope].discard(name)

    def _binding_facts(self, value: ast.expr) -> PythonBindingFacts:
        instance_class = self._known_class_name(value.func) if isinstance(value, ast.Call) else None
        class_alias = self._known_class_name(value)
        wrapper_spec = self._wrapper_spec_for_callable(value)
        if isinstance(value, ast.Lambda):
            wrapper_spec = self._function_wrapper_spec(value, {})
        elif wrapper_spec is None:
            wrapper_spec = self._partial_wrapper_spec(value)
        predicates = (
            self._docker_cli_callable,
            self._docker_cli_expression,
            self._dynamic_python_callable,
            self._getattr_callable,
            self._vars_callable,
            self._getattribute_callable,
            self._local_callable_boundary,
            self._local_module,
        )
        return PythonBindingFacts(
            instance_class,
            class_alias,
            wrapper_spec,
            self._callable_factory_spec(value),
            self._callable_decorator_factory_spec(value),
            self._expression_callable_layers(value),
            self._resolved_external_effect_spec(value),
            tuple(predicate(value) for predicate in predicates),
        )

    def _install_binding_facts(self, name: str, facts: PythonBindingFacts) -> None:
        if facts.instance_class is not None:
            self._instance_classes[-1][name] = facts.instance_class
        if facts.class_alias is not None:
            self._class_aliases[-1][name] = facts.class_alias
        if facts.wrapper_spec is not None:
            self._docker_cli_wrapper_specs[-1][name] = facts.wrapper_spec
        if facts.callable_layers.specs:
            self._callable_layers[-1][name] = facts.callable_layers
        if facts.external_effects is not None:
            self._function_external_effects[-1][name] = facts.external_effects
        self._record_callable_factory_alias(name, facts.callable_factory_spec)
        self._record_callable_decorator_factory_alias(name, facts.decorator_factory_spec)
        for marked, scopes in zip(facts.markers, self._binding_marker_scopes(), strict=True):
            if marked:
                scopes[-1].add(name)

    def _bind_name(self, name: str, value: ast.expr) -> None:
        self._reject_external_payload_mutation(name, value, replace=True)
        facts = self._binding_facts(value)
        self._forget_local_name(name)
        self._install_binding_facts(name, facts)
        if not any(
            isinstance(descendant, ast.Name) and descendant.id == name
            for descendant in ast.walk(value)
        ):
            self._bindings[-1][name] = self._snapshot_expression(value)
        if self._function_wrapper_dependencies[-1].get(name):
            self._refresh_function_wrapper_specs({name})

    def _record_callable_factory_alias(self, name: str, spec: DockerCliWrapperSpec | None) -> None:
        if spec is not None:
            self._callable_factory_specs[-1][name] = spec

    def _record_callable_decorator_factory_alias(
        self, name: str, spec: DockerCliWrapperSpec | None
    ) -> None:
        if spec is not None:
            self._callable_decorator_factory_specs[-1][name] = spec

    def _bind(self, target: ast.expr, value: ast.expr | None) -> None:
        if isinstance(target, ast.Name) and value is not None:
            self._bind_name(target.id, value)
            return
        if isinstance(target, (ast.List, ast.Tuple)) and isinstance(value, (ast.List, ast.Tuple)):
            if len(target.elts) == len(value.elts):
                for nested_target, nested_value in zip(target.elts, value.elts, strict=True):
                    self._bind(nested_target, nested_value)

    def _contains_docker_cli_sink(self, node: ast.AST) -> bool:
        return any(
            isinstance(descendant, ast.Call) and self._docker_cli_callable(descendant.func)
            for descendant in ast.walk(node)
            if descendant is not node
        )

    @staticmethod
    def _function_parameter_names(arguments: ast.arguments) -> tuple[str, ...]:
        parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        names = [parameter.arg for parameter in parameters]
        if arguments.vararg is not None:
            names.append(arguments.vararg.arg)
        if arguments.kwarg is not None:
            names.append(arguments.kwarg.arg)
        return tuple(names)

    @staticmethod
    def _positional_parameter_names(arguments: ast.arguments) -> tuple[str, ...]:
        return tuple(parameter.arg for parameter in (*arguments.posonlyargs, *arguments.args))

    @staticmethod
    def _parameter_references(expression: ast.expr, parameters: set[str]) -> set[str]:
        return {
            descendant.id
            for descendant in ast.walk(expression)
            if isinstance(descendant, ast.Name) and descendant.id in parameters
        }

    def _payload_parameter_references(self, expression: ast.expr, parameters: set[str]) -> set[str]:
        resolved = self._resolved_value_expression(expression)
        if not isinstance(resolved, (ast.List, ast.Tuple)):
            return self._parameter_references(resolved, parameters)
        all_references = self._parameter_references(resolved, parameters)
        static_prefix: list[str] = []
        for element in resolved.elts:
            references = self._parameter_references(element, parameters)
            if references:
                return all_references
            value = self._static_string(element)
            if value is None:
                return all_references
            static_prefix.append(value)
            executable_index, position_error = _resolve_command_position(static_prefix)
            if position_error:
                return all_references
            if executable_index is None or executable_index >= len(static_prefix):
                continue
            executable = static_prefix[executable_index]
            if _is_docker_executable(executable) or Path(executable).name in SHELL_INTERPRETERS:
                continue
            return set()
        return set()

    @staticmethod
    def _static_mapping_payload_keys(expression: ast.expr, parameter: str) -> frozenset[str] | None:
        references = [
            descendant
            for descendant in ast.walk(expression)
            if isinstance(descendant, ast.Name) and descendant.id == parameter
        ]
        if not references:
            return frozenset()
        supported_reference_ids: set[int] = set()
        keys: set[str] = set()
        for descendant in ast.walk(expression):
            if (
                isinstance(descendant, ast.Subscript)
                and isinstance(descendant.value, ast.Name)
                and descendant.value.id == parameter
                and isinstance(descendant.slice, ast.Constant)
                and isinstance(descendant.slice.value, str)
            ):
                supported_reference_ids.add(id(descendant.value))
                keys.add(descendant.slice.value)
                continue
            if not (
                isinstance(descendant, ast.Call)
                and isinstance(descendant.func, ast.Attribute)
                and descendant.func.attr in {"get", "pop", "__getitem__"}
                and isinstance(descendant.func.value, ast.Name)
                and descendant.func.value.id == parameter
                and descendant.args
                and isinstance(descendant.args[0], ast.Constant)
                and isinstance(descendant.args[0].value, str)
            ):
                continue
            supported_reference_ids.add(id(descendant.func.value))
            keys.add(descendant.args[0].value)
        if any(id(reference) not in supported_reference_ids for reference in references):
            return None
        return frozenset(keys)

    @staticmethod
    def _shift_callable_layers(layers: CallableLayers) -> CallableLayers:
        if not layers.specs:
            return layers
        return CallableLayers((None, *layers.specs))

    @staticmethod
    def _advance_callable_layers(layers: CallableLayers) -> CallableLayers:
        return CallableLayers(layers.specs[1:]) if len(layers.specs) > 1 else CallableLayers()

    def _merge_callable_layers(self, *layers: CallableLayers) -> CallableLayers:
        width = max((len(layer.specs) for layer in layers), default=0)
        merged: list[DockerCliWrapperSpec | None] = []
        for depth in range(width):
            spec: DockerCliWrapperSpec | None = None
            for layer in layers:
                spec = self._merge_wrapper_specs(spec, layer.at(depth))
            merged.append(spec)
        while merged and merged[-1] is None:
            merged.pop()
        return CallableLayers(tuple(merged))

    @staticmethod
    def _callable_layer(spec: DockerCliWrapperSpec | None, depth: int) -> CallableLayers:
        if spec is None or depth < 0:
            return CallableLayers()
        return CallableLayers((*([None] * depth), spec))

    @staticmethod
    def _opaque_process_wrapper_spec() -> DockerCliWrapperSpec:
        return DockerCliWrapperSpec(
            ("payload",),
            frozenset(),
            "args",
            "kwargs",
            frozenset({"payload", "args", "kwargs"}),
            frozenset(),
            kwarg_payload_unbounded=True,
        )

    def _static_selected_expression(self, expression: ast.Subscript) -> ast.expr | None:
        owner = self._resolved_value_expression(expression.value)
        key = self._resolved_value_expression(expression.slice)
        mapped = self._static_dict_value(owner, key)
        if mapped is not None:
            return mapped
        return self._static_sequence_value(owner, key)

    def _function_definition_for_callable(
        self, expression: ast.expr
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | None:
        if isinstance(expression, ast.Lambda):
            return expression
        runtime_symbol = (
            self._runtime_namespace_symbol(expression)
            if isinstance(expression, (ast.Call, ast.Subscript))
            else None
        )
        if runtime_symbol is not None:
            scope, name = runtime_symbol
            return self._function_definitions[scope].get(name)
        candidates: list[str] = []
        if isinstance(expression, ast.Name):
            candidates.append(expression.id)
        resolved = self._resolve_expression(expression)
        if isinstance(resolved, ast.Lambda):
            return resolved
        if isinstance(resolved, ast.Name) and resolved.id not in candidates:
            candidates.append(resolved.id)
        for name in candidates:
            for index in self._visible_scope_indexes():
                definition = self._function_definitions[index].get(name)
                if definition is not None:
                    return definition
                if name in self._bindings[index]:
                    break
        return None

    def _call_result_callable_layers(self, node: ast.Call, seen: set[int]) -> CallableLayers:
        definition = self._function_definition_for_callable(node.func)
        if definition is None:
            return CallableLayers()
        definition_id = id(definition)
        if definition_id in self._active_callable_definitions:
            return CallableLayers()
        self._active_callable_definitions.add(definition_id)
        try:
            bindings = self._external_effect_call_bindings(node, definition)
            values = (
                [definition.body]
                if isinstance(definition, ast.Lambda)
                else [
                    returned.value
                    for returned in self._owned_returns(definition)
                    if returned.value is not None
                ]
            )
            return self._merge_callable_layers(
                *(
                    self._expression_callable_layers(
                        _ExpressionSubstituter(bindings).visit(copy.deepcopy(value)), seen
                    )
                    for value in values
                )
            )
        finally:
            self._active_callable_definitions.discard(definition_id)

    def _conditional_callable_layers(
        self, expression: ast.expr, seen: set[int]
    ) -> CallableLayers | None:
        if isinstance(expression, (ast.Await, ast.NamedExpr)):
            return self._expression_callable_layers(expression.value, seen)
        if isinstance(expression, ast.IfExp):
            return self._merge_callable_layers(
                self._expression_callable_layers(expression.body, seen),
                self._expression_callable_layers(expression.orelse, seen),
            )
        if isinstance(expression, ast.BoolOp):
            return self._merge_callable_layers(
                *(self._expression_callable_layers(value, seen) for value in expression.values)
            )
        if isinstance(expression, ast.Subscript):
            selected = self._static_selected_expression(expression)
            return (
                self._expression_callable_layers(selected, seen)
                if selected is not None
                else CallableLayers()
            )
        return None

    def _terminal_callable_layers(
        self, expression: ast.expr, seen: set[int]
    ) -> CallableLayers | None:
        if isinstance(expression, ast.Lambda):
            direct_layers = self._callable_layer(self._function_wrapper_spec(expression, {}), 0)
            returned = self._shift_callable_layers(
                self._expression_callable_layers(expression.body, seen)
            )
            return self._merge_callable_layers(direct_layers, returned)
        if isinstance(expression, ast.Call):
            selected = self._resolved_value_expression(expression)
            selected_layers = (
                self._expression_callable_layers(selected, seen)
                if not isinstance(selected, ast.Call)
                else CallableLayers()
            )
            callable_layers = self._expression_callable_layers(expression.func, seen)
            advanced = self._advance_callable_layers(callable_layers)
            returned = (
                CallableLayers()
                if callable_layers.specs
                else self._call_result_callable_layers(expression, seen)
            )
            instance = self._callable_layer(self._callable_instance_wrapper_spec(expression), 0)
            return self._merge_callable_layers(selected_layers, advanced, returned, instance)
        if isinstance(expression, ast.Attribute):
            direct_spec = self._method_wrapper_spec(expression)
            if direct_spec is None and self._direct_docker_cli_callable(expression):
                direct_spec = self._direct_callable_wrapper_spec(expression)
            factory = self._callable_factory_spec(expression)
            return self._merge_callable_layers(
                self._callable_layer(direct_spec, 0),
                self._callable_layer(factory, 1),
            )
        return None

    def _name_callable_layers(self, expression: ast.Name, seen: set[int]) -> CallableLayers:
        for index in self._visible_scope_indexes():
            layers = self._callable_layers[index].get(expression.id)
            if layers is not None:
                return layers
            if expression.id in self._bindings[index]:
                bound = self._bindings[index][expression.id]
                if not (isinstance(bound, ast.Name) and bound.id == expression.id):
                    return self._expression_callable_layers(bound, seen)
                break
        direct: DockerCliWrapperSpec | None = None
        for index in self._visible_scope_indexes():
            direct = self._docker_cli_wrapper_specs[index].get(expression.id)
            if direct is not None or expression.id in self._bindings[index]:
                break
        qualified = self._qualified_name(expression)
        if direct is None and qualified in PYTHON_DOCKER_CLI_CALLABLES:
            direct = self._direct_callable_wrapper_spec(expression)
        return self._merge_callable_layers(
            self._callable_layer(direct, 0),
            self._callable_layer(self._callable_factory_spec(expression), 1),
            self._callable_layer(self._callable_decorator_factory_spec(expression), 2),
        )

    def _scope_name_callable_layers(self, scope: int, name: str, seen: set[int]) -> CallableLayers:
        layers = self._callable_layers[scope].get(name)
        if layers is not None:
            return layers
        binding = self._bindings[scope].get(name)
        if (
            binding is not None
            and not (isinstance(binding, ast.Name) and binding.id == name)
            and id(binding) not in seen
        ):
            bound_layers = self._expression_callable_layers(binding, seen)
            bound_layers = self._merge_callable_layers(
                bound_layers,
                self._callable_layer(self._partial_wrapper_spec(binding), 0),
            )
            if bound_layers.specs:
                return bound_layers
        return self._merge_callable_layers(
            self._callable_layer(self._docker_cli_wrapper_specs[scope].get(name), 0),
            self._callable_layer(self._callable_factory_specs[scope].get(name), 1),
            self._callable_layer(self._callable_decorator_factory_specs[scope].get(name), 2),
        )

    def _expression_callable_layers(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> CallableLayers:
        visited = set() if seen is None else seen
        if self._stop_callable_provenance_expansion(expression, visited):
            return CallableLayers()
        nested_seen = visited | {id(expression)}
        if isinstance(expression, (ast.Call, ast.Subscript)):
            runtime_lookup = self._runtime_namespace_lookup(expression)
            if runtime_lookup is not None:
                scopes, name = runtime_lookup
            else:
                scopes, name = frozenset(), None
            if len(scopes) == 1 and name is not None:
                runtime_layers = self._scope_name_callable_layers(
                    next(iter(scopes)), name, nested_seen
                )
                if runtime_layers.specs:
                    return runtime_layers
            if scopes and (
                name is None
                or any(self._scope_name_has_process_provenance(scope, name) for scope in scopes)
            ):
                return self._callable_layer(self._opaque_process_wrapper_spec(), 0)
        for resolver in (
            self._runtime_namespace_member_callable_layers,
            self._conditional_callable_layers,
            self._terminal_callable_layers,
        ):
            layers = resolver(expression, nested_seen)
            if layers is not None:
                return layers
        if isinstance(expression, ast.Name):
            return self._name_callable_layers(expression, nested_seen)
        return (
            self._callable_layer(self._direct_callable_wrapper_spec(expression), 0)
            if self._direct_docker_cli_callable(expression)
            else CallableLayers()
        )

    def _stop_callable_provenance_expansion(self, expression: ast.expr, visited: set[int]) -> bool:
        if id(expression) in visited:
            return True
        if len(visited) < MAX_CALLABLE_PROVENANCE_DEPTH:
            return False
        issue = "callable provenance exceeds the supported depth"
        if not any(issue in error for error in self._errors):
            self._errors.append(
                f"{self._diagnostic_source}:{getattr(expression, 'lineno', 1)}: {issue}"
            )
        return True

    def _returned_callable_layers(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> CallableLayers:
        returned = self._merge_callable_layers(
            *(
                self._expression_callable_layers(statement.value)
                for statement in self._owned_returns(node)
                if statement.value is not None
            )
        )
        return self._shift_callable_layers(returned)

    def _wrapper_spec_for_callable(
        self,
        expression: ast.expr,
        local_specs: Mapping[str, DockerCliWrapperSpec] | None = None,
    ) -> DockerCliWrapperSpec | None:
        layered = self._expression_callable_layers(expression).at(0)
        return layered or self._legacy_wrapper_spec_for_callable(expression, local_specs)

    def _legacy_wrapper_spec_for_callable(
        self,
        expression: ast.expr,
        local_specs: Mapping[str, DockerCliWrapperSpec] | None,
    ) -> DockerCliWrapperSpec | None:
        if isinstance(expression, ast.Await):
            return self._wrapper_spec_for_callable(expression.value, local_specs)
        if isinstance(expression, ast.Attribute):
            return self._method_wrapper_spec(expression)
        if isinstance(expression, ast.Call):
            return self._callable_factory_spec(
                expression.func
            ) or self._callable_instance_wrapper_spec(expression)
        if isinstance(expression, ast.Name):
            instance_spec = self._callable_instance_wrapper_spec(expression)
            if instance_spec is not None:
                return instance_spec
            if local_specs is not None and expression.id in local_specs:
                return local_specs[expression.id]
            for index in self._visible_scope_indexes():
                spec = self._docker_cli_wrapper_specs[index].get(expression.id)
                if spec is not None:
                    return spec
                if expression.id in self._bindings[index]:
                    return None
        return None

    def _callable_factory_spec(self, expression: ast.expr) -> DockerCliWrapperSpec | None:
        if isinstance(expression, ast.Await):
            return self._callable_factory_spec(expression.value)
        if isinstance(expression, ast.Attribute):
            class_name = self._instance_class_name(expression.value) or self._known_class_name(
                expression.value
            )
            if class_name is None:
                return None
            return self._class_method_callable_factory_spec(class_name, expression.attr)
        if not isinstance(expression, ast.Name):
            return None
        instance_class = self._instance_class_name(expression)
        if instance_class is not None:
            instance_factory = self._class_method_callable_factory_spec(instance_class, "__call__")
            if instance_factory is not None:
                return instance_factory
        for index in self._visible_scope_indexes():
            spec = self._callable_factory_specs[index].get(expression.id)
            if spec is not None:
                return spec
            if expression.id in self._bindings[index]:
                return None
        return None

    def _callable_decorator_factory_spec(self, expression: ast.expr) -> DockerCliWrapperSpec | None:
        if isinstance(expression, ast.Await):
            return self._callable_decorator_factory_spec(expression.value)
        if not isinstance(expression, ast.Name):
            return None
        for index in self._visible_scope_indexes():
            spec = self._callable_decorator_factory_specs[index].get(expression.id)
            if spec is not None:
                return spec
            if expression.id in self._bindings[index]:
                return None
        return None

    def _class_method_callable_factory_spec(
        self, class_name: str, method_name: str, seen: set[str] | None = None
    ) -> DockerCliWrapperSpec | None:
        visited = set() if seen is None else seen
        if class_name in visited:
            return None
        visited.add(class_name)
        key = (class_name, method_name)
        for index in self._visible_scope_indexes():
            scope = self._class_callable_factory_specs[index]
            spec = scope.get(key)
            if spec is not None:
                return spec
        if any(key in self._class_wrapper_specs[index] for index in self._visible_scope_indexes()):
            return None
        for index in self._visible_scope_indexes():
            base_scope = self._class_bases[index]
            bases = base_scope.get(class_name)
            if bases is None:
                continue
            for base in bases:
                spec = self._class_method_callable_factory_spec(base, method_name, visited)
                if spec is not None:
                    return spec
        return None

    def _multiprocessing_context(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        return (
            isinstance(resolved, ast.Call)
            and self._qualified_name(resolved.func) == "multiprocessing.get_context"
        )

    def _process_owner_factory(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        if not isinstance(resolved, ast.Call):
            return None
        qualified = self._qualified_name(resolved.func)
        if qualified is not None:
            return qualified
        if isinstance(resolved.func, ast.Attribute) and self._multiprocessing_context(
            resolved.func.value
        ):
            return f"multiprocessing.context.{resolved.func.attr}"
        return None

    def _process_dispatch_kind(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        if not isinstance(resolved, ast.Attribute):
            return None
        method = resolved.attr
        if method == "Process" and self._multiprocessing_context(resolved.value):
            return "context_process"
        expected_factories = PROCESS_DISPATCH_FACTORIES.get(method)
        if expected_factories is None:
            return None
        if self._process_owner_factory(resolved.value) in expected_factories:
            return method
        class_name = self._instance_class_name(resolved.value) or self._known_class_name(
            resolved.value
        )
        if class_name is not None and self._class_method_definition(class_name, method) is not None:
            return None
        return method

    def _process_dispatch_owner_factory(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        return (
            self._process_owner_factory(resolved.value)
            if isinstance(resolved, ast.Attribute)
            else None
        )

    def _event_loop_process_callable(self, expression: ast.expr) -> bool:
        return self._process_dispatch_kind(expression) in {
            "subprocess_exec",
            "subprocess_shell",
        }

    def _modeled_process_constructor_callable(self, expression: ast.expr) -> bool:
        return (
            self._qualified_name(expression)
            in {
                "multiprocessing.Process",
                "threading.Barrier",
                "threading.Timer",
                "threading.Thread",
            }
            or self._process_dispatch_kind(expression) == "context_process"
        )

    def _modeled_process_instance(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        return isinstance(resolved, ast.Call) and self._modeled_process_constructor_callable(
            resolved.func
        )

    @staticmethod
    def _asyncio_wrapper_spec(
        qualified: str | None, event_loop_method: str | None
    ) -> DockerCliWrapperSpec | None:
        if qualified == "asyncio.create_subprocess_exec" or event_loop_method == "subprocess_exec":
            positional_parameters = (
                ("protocol_factory", "program") if event_loop_method is not None else ("program",)
            )
            return DockerCliWrapperSpec(
                positional_parameters,
                frozenset(),
                "argv",
                None,
                frozenset({"program", "argv"}),
                frozenset(),
                payload_templates=(
                    ast.Tuple(
                        elts=[
                            ast.Name(id="program", ctx=ast.Load()),
                            ast.Starred(
                                value=ast.Name(id="argv", ctx=ast.Load()),
                                ctx=ast.Load(),
                            ),
                        ],
                        ctx=ast.Load(),
                    ),
                ),
            )
        if not (
            qualified == "asyncio.create_subprocess_shell"
            or event_loop_method == "subprocess_shell"
        ):
            return None
        positional_parameters = (
            ("protocol_factory", "cmd") if event_loop_method is not None else ("cmd",)
        )
        return DockerCliWrapperSpec(
            positional_parameters,
            frozenset({"executable"}),
            None,
            None,
            frozenset({"cmd", "executable"}),
            frozenset(),
        )

    @staticmethod
    def _simple_process_wrapper_spec(qualified: str | None) -> DockerCliWrapperSpec | None:
        parameter = next(
            (
                name
                for names, name in (
                    ({"anyio.open_process", "anyio.run_process", "os.system"}, "command"),
                    ({"pty.spawn"}, "argv"),
                    (
                        {"os.popen", "subprocess.getoutput", "subprocess.getstatusoutput"},
                        "cmd",
                    ),
                )
                if qualified in names
            ),
            None,
        )
        return (
            DockerCliWrapperSpec(
                (parameter,),
                frozenset(),
                None,
                None,
                frozenset({parameter}),
                frozenset(),
            )
            if parameter is not None
            else None
        )

    @staticmethod
    def _os_exec_wrapper_spec(qualified: str | None) -> DockerCliWrapperSpec | None:
        positional: tuple[str, ...]
        executable: str
        arguments: str
        vararg: str | None
        if qualified in {"os.execv", "os.execve"}:
            positional, executable, arguments, vararg = ("path", "argv"), "path", "argv", None
        elif qualified in {"os.execvp", "os.execvpe"}:
            positional, executable, arguments, vararg = ("file", "args"), "file", "args", None
        elif qualified in {"os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe"}:
            positional, executable, arguments, vararg = (
                ("mode", "file", "args"),
                "file",
                "args",
                None,
            )
        elif qualified in {"os.execl", "os.execle", "os.execlp", "os.execlpe"}:
            positional, executable, arguments, vararg = ("path",), "path", "argv", "argv"
        elif qualified in {"os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe"}:
            positional, executable, arguments, vararg = ("mode", "file"), "file", "args", "args"
        elif qualified in {"os.posix_spawn", "os.posix_spawnp"}:
            positional, executable, arguments, vararg = (
                ("path", "argv", "env"),
                "path",
                "argv",
                None,
            )
        else:
            return None
        return DockerCliWrapperSpec(
            positional,
            frozenset(),
            vararg,
            None,
            frozenset({executable, arguments}),
            frozenset(),
            payload_templates=(
                ast.Name(id=executable, ctx=ast.Load()),
                ast.Name(id=arguments, ctx=ast.Load()),
            ),
        )

    def _direct_callable_wrapper_spec(self, expression: ast.expr) -> DockerCliWrapperSpec:
        qualified = self._qualified_name(expression)
        dispatch_kind = self._process_dispatch_kind(expression)
        event_loop_method = (
            dispatch_kind if dispatch_kind in {"subprocess_exec", "subprocess_shell"} else None
        )
        spec = self._asyncio_wrapper_spec(qualified, event_loop_method)
        if spec is None:
            spec = self._simple_process_wrapper_spec(qualified)
        if spec is None:
            spec = self._os_exec_wrapper_spec(qualified)
        if spec is not None:
            return spec
        return DockerCliWrapperSpec(
            ("args",),
            frozenset({"executable"}),
            None,
            None,
            frozenset({"args", "executable"}),
            frozenset(),
        )

    def _returned_callable_spec(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> DockerCliWrapperSpec | None:
        result: DockerCliWrapperSpec | None = None
        for returned in self._owned_returns(node):
            value = returned.value
            if value is None:
                continue
            candidate = self._wrapper_spec_for_callable(value)
            if candidate is None and self._docker_cli_callable(value):
                candidate = self._direct_callable_wrapper_spec(value)
            result = self._merge_wrapper_specs(result, candidate)
        return result

    def _returned_callable_factory_spec(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> DockerCliWrapperSpec | None:
        result: DockerCliWrapperSpec | None = None
        for returned in self._owned_returns(node):
            if returned.value is None:
                continue
            result = self._merge_wrapper_specs(result, self._callable_factory_spec(returned.value))
        return result

    @staticmethod
    def _owned_returns(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[ast.Return]:
        returns: list[ast.Return] = []
        pending: list[ast.AST] = list(node.body)
        while pending:
            current = pending.pop()
            if isinstance(current, ast.Return):
                if current.value is not None:
                    returns.append(current)
                continue
            if isinstance(
                current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            pending.extend(ast.iter_child_nodes(current))
        return returns

    @classmethod
    def _reachable_return_sequence(
        cls, statements: Sequence[ast.stmt]
    ) -> tuple[list[ast.Return], bool]:
        returns: list[ast.Return] = []
        for statement in statements:
            if isinstance(statement, ast.Return):
                returns.append(statement)
                return returns, True
            if isinstance(statement, (ast.Break, ast.Continue, ast.Raise)):
                return returns, True
            if isinstance(statement, ast.If):
                truth = cls._static_condition_truth(statement.test)
                if truth is not None:
                    branch = statement.body if truth else statement.orelse
                    branch_returns, terminates = cls._reachable_return_sequence(branch)
                else:
                    body_returns, body_terminates = cls._reachable_return_sequence(statement.body)
                    else_returns, else_terminates = cls._reachable_return_sequence(statement.orelse)
                    branch_returns = [*body_returns, *else_returns]
                    terminates = bool(statement.orelse) and body_terminates and else_terminates
                returns.extend(branch_returns)
                if terminates:
                    return returns, True
                continue
            nested_returns, terminates = cls._reachable_statement_returns(statement)
            returns.extend(nested_returns)
            if terminates:
                return returns, True
        return returns, False

    @classmethod
    def _reachable_statement_returns(cls, statement: ast.stmt) -> tuple[list[ast.Return], bool]:
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            return cls._reachable_return_sequence(statement.body)
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            body_returns, _ = cls._reachable_return_sequence(statement.body)
            else_returns, _ = cls._reachable_return_sequence(statement.orelse)
            return [*body_returns, *else_returns], False
        if isinstance(statement, (ast.Try, ast.TryStar)):
            groups = [statement.body, statement.orelse, statement.finalbody]
            groups.extend(handler.body for handler in statement.handlers)
            returns = [
                returned
                for group in groups
                for returned in cls._reachable_return_sequence(group)[0]
            ]
            final_terminates = cls._reachable_return_sequence(statement.finalbody)[1]
            return returns, final_terminates
        if isinstance(statement, ast.Match):
            returns = [
                returned
                for case in statement.cases
                for returned in cls._reachable_return_sequence(case.body)[0]
            ]
            return returns, False
        return [], False

    @classmethod
    def _reachable_owned_returns(
        cls, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[ast.Return]:
        return cls._reachable_return_sequence(node.body)[0]

    @classmethod
    def _static_condition_truth(cls, expression: ast.expr) -> bool | None:
        if isinstance(expression, ast.Constant):
            return bool(expression.value)
        if isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
            return bool(expression.elts)
        if isinstance(expression, ast.Dict):
            return bool(expression.keys)
        if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
            value = cls._static_condition_truth(expression.operand)
            return None if value is None else not value
        return None

    @staticmethod
    def _owned_scope_children(node: ast.AST) -> tuple[ast.AST, ...]:
        return (
            (node.iter, *node.ifs)
            if isinstance(node, ast.comprehension)
            else tuple(ast.iter_child_nodes(node))
        )

    def _owned_scope_names(self, node: ast.AST) -> set[str]:
        names: set[str] = set()
        external_names: set[str] = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names.update(self._function_parameter_names(node.args))
        pending = list(ast.iter_child_nodes(node))
        while pending:
            current = pending.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(current.name)
                continue
            if isinstance(current, ast.Lambda):
                continue
            if isinstance(current, (ast.Global, ast.Nonlocal)):
                external_names.update(current.names)
                continue
            if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store):
                names.add(current.id)
            elif isinstance(current, (ast.Import, ast.ImportFrom)):
                for alias in current.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name.split(".", 1)[0])
            pending.extend(self._owned_scope_children(current))
        return names - external_names

    @staticmethod
    def _scope_loaded_roots(scope_node: ast.AST) -> tuple[ast.AST, ...]:
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return tuple(scope_node.body)
        if isinstance(scope_node, ast.Lambda):
            return (scope_node.body,)
        return tuple(ast.iter_child_nodes(scope_node))

    def _scope_definition_time_expressions(self, scope_node: ast.AST) -> tuple[ast.expr, ...]:
        if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            expressions = [
                default
                for default in (*scope_node.args.defaults, *scope_node.args.kw_defaults)
                if default is not None
            ]
            if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                expressions.extend(scope_node.decorator_list)
            if not self._postponed_annotations:
                expressions.extend(self._function_annotations(scope_node))
            return tuple(expressions)
        if isinstance(scope_node, ast.ClassDef):
            return (
                *scope_node.decorator_list,
                *scope_node.bases,
                *(keyword.value for keyword in scope_node.keywords),
            )
        return ()

    def _free_scope_loaded_names(self, scope_node: ast.AST) -> set[str]:
        pending = list(self._scope_loaded_roots(scope_node))
        referenced: set[str] = set()
        nested_referenced: set[str] = set()
        global_names: set[str] = set()
        while pending:
            current = pending.pop()
            if isinstance(current, (ast.DictComp, ast.GeneratorExp, ast.ListComp, ast.SetComp)):
                pending.append(current.generators[0].iter)
                nested_referenced.update(
                    self._free_scope_loaded_names(
                        self._comprehension_scope_node(current, static_bindings=True)
                    )
                )
                continue
            if isinstance(current, PYTHON_SCOPE_BOUNDARIES):
                pending.extend(self._scope_definition_time_expressions(current))
                nested_referenced.update(self._free_scope_loaded_names(current))
                continue
            if isinstance(current, ast.Global):
                global_names.update(current.names)
            elif isinstance(current, ast.Nonlocal):
                referenced.update(current.names)
            elif isinstance(current, ast.Name) and isinstance(current.ctx, ast.Load):
                referenced.add(current.id)
            pending.extend(ast.iter_child_nodes(current))
        local_names = self._owned_scope_names(scope_node)
        if isinstance(scope_node, ast.ClassDef):
            return (referenced - local_names - global_names) | nested_referenced
        return (referenced | nested_referenced) - local_names - global_names

    def _owned_scope_loaded_names(self, node: ast.AST) -> set[str]:
        return self._free_scope_loaded_names(node)

    def _class_method_wrapper_spec(
        self,
        class_name: str,
        method_name: str,
        *,
        bound_instance: bool,
        seen: set[str] | None = None,
    ) -> DockerCliWrapperSpec | None:
        visited = set() if seen is None else seen
        if class_name in visited:
            return None
        visited.add(class_name)
        for index in self._visible_scope_indexes():
            scope = self._class_wrapper_specs[index]
            entry = scope.get((class_name, method_name))
            if entry is None:
                continue
            spec, mode = entry
            if mode == "static" or (mode == "instance" and not bound_instance):
                return spec
            return self._bound_method_wrapper_spec(spec)
        for index in self._visible_scope_indexes():
            base_scope = self._class_bases[index]
            bases = base_scope.get(class_name)
            if bases is None:
                continue
            for base in bases:
                inherited = self._class_method_wrapper_spec(
                    base,
                    method_name,
                    bound_instance=bound_instance,
                    seen=visited,
                )
                if inherited is not None:
                    return inherited
        return None

    def _instance_class_name(self, expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            for index in self._visible_scope_indexes():
                instance_class = self._instance_classes[index].get(expression.id)
                if instance_class is not None:
                    return instance_class
                if expression.id in self._bindings[index]:
                    break
            return None
        if isinstance(expression, ast.Call):
            return self._known_class_name(expression.func)
        return None

    def _callable_instance_wrapper_spec(self, expression: ast.expr) -> DockerCliWrapperSpec | None:
        class_name = self._instance_class_name(expression)
        if class_name is None:
            return None
        return self._class_method_wrapper_spec(class_name, "__call__", bound_instance=True)

    def _method_wrapper_spec(self, expression: ast.Attribute) -> DockerCliWrapperSpec | None:
        owner = expression.value
        class_name = self._instance_class_name(owner)
        bound_instance = class_name is not None
        if class_name is None:
            class_name = self._known_class_name(owner)
        if class_name is None:
            return None
        if bound_instance:
            property_factory = self._class_property_callable_factory_spec(
                class_name, expression.attr
            )
            if property_factory is not None:
                return property_factory
        return self._class_method_wrapper_spec(
            class_name, expression.attr, bound_instance=bound_instance
        )

    def _class_property_callable_factory_spec(
        self, class_name: str, property_name: str, seen: set[str] | None = None
    ) -> DockerCliWrapperSpec | None:
        visited = set() if seen is None else seen
        if class_name in visited:
            return None
        visited.add(class_name)
        key = (class_name, property_name)
        for index in self._visible_scope_indexes():
            scope = self._class_property_factory_specs[index]
            spec = scope.get(key)
            if spec is not None:
                return spec
        for index in self._visible_scope_indexes():
            base_scope = self._class_bases[index]
            bases = base_scope.get(class_name)
            if bases is None:
                continue
            for base in bases:
                spec = self._class_property_callable_factory_spec(base, property_name, visited)
                if spec is not None:
                    return spec
        return None

    @staticmethod
    def _bound_method_wrapper_spec(spec: DockerCliWrapperSpec) -> DockerCliWrapperSpec:
        receiver = spec.positional_parameters[0] if spec.positional_parameters else None
        return DockerCliWrapperSpec(
            spec.positional_parameters[1:],
            spec.keyword_only_parameters,
            spec.vararg_parameter,
            spec.kwarg_parameter,
            spec.payload_parameters - ({receiver} if receiver is not None else set()),
            spec.dangerous_defaults - ({receiver} if receiver is not None else set()),
            spec.always_dangerous,
            spec.kwarg_payload_keys,
            spec.kwarg_payload_unbounded,
            spec.payload_templates,
        )

    def _deferred_scope_parameter(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        if not isinstance(resolved, ast.Name) or self._scope_kinds[-1] != "function":
            return False
        binding = self._bindings[-1].get(resolved.id)
        return isinstance(binding, ast.Name) and binding.id == resolved.id

    def _wrapper_payload_arguments(
        self, node: ast.Call, spec: DockerCliWrapperSpec
    ) -> tuple[list[ast.expr], bool]:
        opaque_keyword_expansion = any(
            keyword.arg is None
            and not isinstance(self._resolved_value_expression(keyword.value), ast.Dict)
            and not self._deferred_scope_parameter(keyword.value)
            for keyword in node.keywords
        )
        if spec.payload_templates:
            bindings, opaque_starred_arguments = self._wrapper_call_bindings(node, spec)
            template_payloads = [
                _ExpressionSubstituter(bindings).visit(copy.deepcopy(template))
                for template in spec.payload_templates
            ]
            template_payloads.extend(opaque_starred_arguments)
            dangerous = (
                spec.always_dangerous
                or opaque_keyword_expansion
                or any(
                    not isinstance(self._resolve_expression(argument), ast.Name)
                    for argument in opaque_starred_arguments
                )
                or any(
                    parameter in spec.dangerous_defaults and parameter not in bindings
                    for parameter in spec.payload_parameters
                )
            )
            return template_payloads, dangerous
        payloads: list[ast.expr] = []
        dangerous_without_argument = (
            spec.always_dangerous
            or opaque_keyword_expansion
            or any(
                isinstance(argument, ast.Starred)
                and not isinstance(self._resolve_expression(argument.value), ast.Name)
                for argument in node.args
            )
        )
        positional = {name: index for index, name in enumerate(spec.positional_parameters)}
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        starred = [argument for argument in node.args if isinstance(argument, ast.Starred)]
        expanded_keywords = [keyword.value for keyword in node.keywords if keyword.arg is None]
        for parameter in spec.payload_parameters:
            parameter_payloads, uses_dangerous_default = self._wrapper_parameter_payloads(
                node,
                spec,
                parameter,
                positional,
                keywords,
                starred,
                expanded_keywords,
            )
            payloads.extend(parameter_payloads)
            dangerous_without_argument = dangerous_without_argument or uses_dangerous_default
        return payloads, dangerous_without_argument

    def _wrapper_call_bindings(
        self, node: ast.Call, spec: DockerCliWrapperSpec
    ) -> tuple[dict[str, ast.expr], list[ast.expr]]:
        bindings: dict[str, ast.expr] = {}
        positional_arguments, opaque_starred_arguments = self._expanded_positional_arguments(
            node.args
        )
        direct_keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        for position, parameter in enumerate(spec.positional_parameters):
            if position < len(positional_arguments) and not isinstance(
                positional_arguments[position], ast.Starred
            ):
                bindings[parameter] = positional_arguments[position]
            else:
                binding = self._keyword_call_binding(node, direct_keywords, parameter)
                if binding is not None:
                    bindings[parameter] = binding
        for parameter in spec.keyword_only_parameters:
            binding = self._keyword_call_binding(node, direct_keywords, parameter)
            if binding is not None:
                bindings[parameter] = binding
        if spec.vararg_parameter is not None:
            bindings[spec.vararg_parameter] = ast.Tuple(
                elts=list(positional_arguments[len(spec.positional_parameters) :]),
                ctx=ast.Load(),
            )
        expanded_kwargs = self._expanded_kwargs_binding(node, spec)
        if expanded_kwargs is not None:
            name, mapping = expanded_kwargs
            bindings[name] = mapping
        return bindings, opaque_starred_arguments

    def _expanded_positional_arguments(
        self, arguments: Sequence[ast.expr]
    ) -> tuple[list[ast.expr], list[ast.expr]]:
        expanded_arguments: list[ast.expr] = []
        opaque_starred_arguments: list[ast.expr] = []
        for argument in arguments:
            if not isinstance(argument, ast.Starred):
                expanded_arguments.append(argument)
                continue
            resolved = self._resolved_value_expression(argument.value)
            if not isinstance(resolved, (ast.List, ast.Tuple)):
                expanded_arguments.append(argument)
                opaque_starred_arguments.append(argument.value)
                continue
            nested_arguments, nested_opaque = self._expanded_positional_arguments(resolved.elts)
            expanded_arguments.extend(nested_arguments)
            opaque_starred_arguments.extend(nested_opaque)
        return expanded_arguments, opaque_starred_arguments

    @staticmethod
    def _expanded_kwargs_binding(
        node: ast.Call, spec: DockerCliWrapperSpec
    ) -> tuple[str, ast.Dict] | None:
        if spec.kwarg_parameter is None:
            return None
        named = set(spec.positional_parameters) | set(spec.keyword_only_parameters)
        keys: list[ast.expr | None] = []
        values: list[ast.expr] = []
        for keyword in node.keywords:
            if keyword.arg is None:
                keys.append(None)
                values.append(keyword.value)
            elif keyword.arg not in named:
                keys.append(ast.Constant(value=keyword.arg))
                values.append(keyword.value)
        return spec.kwarg_parameter, ast.Dict(keys=keys, values=values)

    def _keyword_call_binding(
        self, node: ast.Call, direct_keywords: Mapping[str, ast.expr], parameter: str
    ) -> ast.expr | None:
        if parameter in direct_keywords:
            return direct_keywords[parameter]
        expanded = self._expanded_keyword_values(node, parameter)
        return expanded[0] if expanded else None

    def _wrapper_parameter_payloads(
        self,
        node: ast.Call,
        spec: DockerCliWrapperSpec,
        parameter: str,
        positional: Mapping[str, int],
        keywords: Mapping[str, ast.expr],
        starred: Sequence[ast.Starred],
        expanded_keywords: Sequence[ast.expr],
    ) -> tuple[list[ast.expr], bool]:
        if parameter == spec.vararg_parameter:
            return [
                ast.Tuple(
                    elts=list(node.args[len(spec.positional_parameters) :]),
                    ctx=ast.Load(),
                )
            ], False
        if parameter == spec.kwarg_parameter:
            if spec.kwarg_payload_unbounded or not spec.kwarg_payload_keys:
                return [keyword.value for keyword in node.keywords], False
            payloads: list[ast.expr] = []
            for key in spec.kwarg_payload_keys:
                if key in keywords:
                    payloads.append(keywords[key])
                payloads.extend(self._expanded_keyword_values(node, key))
            return payloads, False
        if parameter in keywords:
            return [keywords[parameter]], False
        expanded = self._expanded_keyword_values(node, parameter)
        if expanded:
            return expanded, False
        position = positional.get(parameter)
        if position is not None and position < len(node.args) and not starred:
            return [node.args[position]], False
        if starred or expanded_keywords:
            return [*starred, *expanded_keywords], False
        return [], parameter in spec.dangerous_defaults

    def _expanded_keyword_values(self, node: ast.Call, name: str) -> list[ast.expr]:
        values: list[ast.expr] = []
        for keyword in node.keywords:
            if keyword.arg is not None:
                continue
            mapping = self._resolved_process_value(keyword.value)
            expanded, opaque = self._static_expanded_keyword_values(mapping, name)
            values.extend(expanded)
            if opaque and name == "key":
                self._errors.append(
                    f"{self._diagnostic_source}:{keyword.value.lineno}: opaque expanded keyword "
                    "mapping is unsupported"
                )
            if opaque:
                values.append(keyword.value)
        return values

    def _static_expanded_keyword_values(
        self, mapping: ast.expr | None, name: str
    ) -> tuple[list[ast.expr], bool]:
        if not isinstance(mapping, ast.Dict):
            return [], True
        values: list[ast.expr] = []
        opaque = False
        for key, value in zip(mapping.keys, mapping.values, strict=True):
            if key is None:
                nested = self._resolved_process_value(value)
                nested_values, nested_opaque = self._static_expanded_keyword_values(nested, name)
                values.extend(nested_values)
                opaque = opaque or nested_opaque
            elif isinstance(key, ast.Constant) and key.value == name:
                values.append(value)
        return values, opaque

    def _partial_wrapper_spec(self, expression: ast.expr) -> DockerCliWrapperSpec | None:
        if not (
            isinstance(expression, ast.Call)
            and self._qualified_name(expression.func) == "functools.partial"
            and expression.args
        ):
            return None
        target = expression.args[0]
        target_spec = self._wrapper_spec_for_callable(target)
        if target_spec is None and self._docker_cli_callable(target):
            target_spec = self._direct_callable_wrapper_spec(target)
        if target_spec is None:
            return None
        bound_arguments = list(expression.args[1:])
        if any(
            self._qualified_name(argument) == "functools.Placeholder"
            for argument in bound_arguments
        ):
            return DockerCliWrapperSpec(
                target_spec.positional_parameters,
                target_spec.keyword_only_parameters,
                target_spec.vararg_parameter,
                target_spec.kwarg_parameter,
                target_spec.payload_parameters,
                target_spec.dangerous_defaults,
                True,
                target_spec.kwarg_payload_keys,
                target_spec.kwarg_payload_unbounded,
                target_spec.payload_templates,
            )
        positional_bindings = dict(
            zip(target_spec.positional_parameters, bound_arguments, strict=False)
        )
        positional_bound_names = set(positional_bindings)
        keyword_bindings = {
            keyword.arg: keyword.value for keyword in expression.keywords if keyword.arg is not None
        }
        remaining_positional = list(target_spec.positional_parameters[len(bound_arguments) :])
        keyword_cut = next(
            (
                index
                for index, parameter in enumerate(remaining_positional)
                if parameter in keyword_bindings
            ),
            len(remaining_positional),
        )
        keyword_only_parameters = target_spec.keyword_only_parameters | set(
            remaining_positional[keyword_cut:]
        )
        dangerous_defaults = set(target_spec.dangerous_defaults) - positional_bound_names
        for name, value in keyword_bindings.items():
            if name not in target_spec.payload_parameters:
                continue
            if self._docker_cli_expression(value) or self._docker_executable_expression(value):
                dangerous_defaults.add(name)
            else:
                dangerous_defaults.discard(name)
        fixed_payloads = [
            value
            for name, value in positional_bindings.items()
            if name in target_spec.payload_parameters
        ]
        if target_spec.vararg_parameter in target_spec.payload_parameters and len(
            bound_arguments
        ) > len(target_spec.positional_parameters):
            fixed_payloads.extend(bound_arguments[len(target_spec.positional_parameters) :])
        always_dangerous = target_spec.always_dangerous or any(
            self._docker_cli_expression(payload) or self._docker_executable_expression(payload)
            for payload in fixed_payloads
        )
        templates = tuple(
            _ExpressionSubstituter(positional_bindings).visit(copy.deepcopy(template))
            for template in target_spec.payload_templates
        )
        keyword_bound_names = {
            keyword.arg for keyword in expression.keywords if keyword.arg is not None
        }
        return DockerCliWrapperSpec(
            tuple(remaining_positional[:keyword_cut]),
            keyword_only_parameters,
            target_spec.vararg_parameter,
            target_spec.kwarg_parameter,
            target_spec.payload_parameters - positional_bound_names,
            frozenset(dangerous_defaults),
            always_dangerous,
            target_spec.kwarg_payload_keys - keyword_bound_names,
            target_spec.kwarg_payload_unbounded,
            templates,
        )

    def _function_default_expressions(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> dict[str, ast.expr]:
        positional = self._positional_parameter_names(node.args)
        defaults = dict(
            zip(
                positional[len(positional) - len(node.args.defaults) :],
                node.args.defaults,
                strict=True,
            )
        )
        defaults.update(
            {
                parameter.arg: value
                for parameter, value in zip(
                    node.args.kwonlyargs, node.args.kw_defaults, strict=True
                )
                if value is not None
            }
        )
        return defaults

    def _function_wrapper_spec(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        local_specs: Mapping[str, DockerCliWrapperSpec],
    ) -> DockerCliWrapperSpec | None:
        parameters = set(self._function_parameter_names(node.args))
        payload_parameters: set[str] = set()
        always_dangerous = False
        kwarg_payload_keys: set[str] = set()
        kwarg_payload_unbounded = False
        payload_templates: list[ast.expr] = []
        for descendant, _ in self._owned_calls(node):
            call_payloads = self._function_call_payloads(descendant, local_specs)
            if call_payloads is None:
                continue
            payloads, nested_danger = call_payloads
            always_dangerous = always_dangerous or nested_danger
            for payload in payloads:
                resolved_payload = self._resolved_value_expression(payload)
                references, keys, unbounded, dangerous = self._wrapper_payload_metadata(
                    resolved_payload,
                    parameters,
                    node.args.kwarg.arg if node.args.kwarg is not None else None,
                )
                payload_parameters.update(references)
                if references:
                    payload_templates.append(resolved_payload)
                kwarg_payload_keys.update(keys)
                kwarg_payload_unbounded = kwarg_payload_unbounded or unbounded
                always_dangerous = always_dangerous or dangerous
        defaults = self._function_default_expressions(node)
        dangerous_defaults = frozenset(
            parameter
            for parameter in payload_parameters
            if parameter in defaults and self._docker_cli_expression(defaults[parameter])
        )
        if not payload_parameters and not always_dangerous:
            return None
        unique_templates = {
            ast.dump(template, include_attributes=False): template for template in payload_templates
        }
        return DockerCliWrapperSpec(
            self._positional_parameter_names(node.args),
            frozenset(parameter.arg for parameter in node.args.kwonlyargs),
            node.args.vararg.arg if node.args.vararg is not None else None,
            node.args.kwarg.arg if node.args.kwarg is not None else None,
            frozenset(payload_parameters),
            dangerous_defaults,
            always_dangerous,
            frozenset(kwarg_payload_keys),
            kwarg_payload_unbounded,
            tuple(unique_templates.values()),
        )

    def _function_call_payloads(
        self,
        node: ast.Call,
        local_specs: Mapping[str, DockerCliWrapperSpec],
    ) -> tuple[list[ast.expr], bool] | None:
        nested_spec = self._wrapper_spec_for_callable(node.func, local_specs)
        if nested_spec is not None:
            return self._wrapper_payload_arguments(node, nested_spec)
        if self._direct_docker_cli_callable(node.func):
            return self._wrapper_payload_arguments(
                node, self._direct_callable_wrapper_spec(node.func)
            )
        return None

    def _wrapper_payload_metadata(
        self,
        payload: ast.expr,
        parameters: set[str],
        kwarg_parameter: str | None,
    ) -> tuple[set[str], frozenset[str], bool, bool]:
        references = self._payload_parameter_references(payload, parameters)
        if not references:
            return set(), frozenset(), False, self._docker_cli_expression(payload)
        if kwarg_parameter is None or kwarg_parameter not in references:
            return references, frozenset(), False, False
        keys = self._static_mapping_payload_keys(payload, kwarg_parameter)
        if keys is None:
            return references, frozenset(), True, False
        return references, keys, False, False

    def _refresh_function_wrapper_specs(self, changed_names: set[str]) -> None:
        definitions = self._function_definitions[-1]
        specs = self._docker_cli_wrapper_specs[-1]
        dependencies = self._function_wrapper_dependencies[-1]
        pending = sorted(
            {
                dependent
                for changed_name in changed_names
                for dependent in dependencies.get(changed_name, set())
            },
            reverse=True,
        )
        queued = set(pending)
        while pending:
            name = pending.pop()
            queued.discard(name)
            definition = definitions.get(name)
            if definition is None:
                continue
            candidate = self._function_wrapper_spec(definition, specs)
            current = specs.get(name)
            merged = self._merge_wrapper_specs(current, candidate)
            if merged is None or current == merged:
                continue
            specs[name] = merged
            self._docker_cli_aliases[-1].add(name)
            for dependent in sorted(dependencies.get(name, set()), reverse=True):
                if dependent not in queued:
                    pending.append(dependent)
                    queued.add(dependent)

    @staticmethod
    def _merge_wrapper_specs(
        current: DockerCliWrapperSpec | None,
        candidate: DockerCliWrapperSpec | None,
    ) -> DockerCliWrapperSpec | None:
        if current is None:
            return candidate
        if candidate is None:
            return current
        templates = list(current.payload_templates)
        template_keys = {ast.dump(template, include_attributes=False) for template in templates}
        templates.extend(
            template
            for template in candidate.payload_templates
            if ast.dump(template, include_attributes=False) not in template_keys
        )
        return DockerCliWrapperSpec(
            current.positional_parameters,
            current.keyword_only_parameters | candidate.keyword_only_parameters,
            current.vararg_parameter or candidate.vararg_parameter,
            current.kwarg_parameter or candidate.kwarg_parameter,
            current.payload_parameters | candidate.payload_parameters,
            current.dangerous_defaults | candidate.dangerous_defaults,
            current.always_dangerous or candidate.always_dangerous,
            current.kwarg_payload_keys | candidate.kwarg_payload_keys,
            current.kwarg_payload_unbounded or candidate.kwarg_payload_unbounded,
            tuple(templates),
        )

    def _register_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        dependencies = self._function_wrapper_dependencies[-1]
        for dependency, dependents in tuple(dependencies.items()):
            dependents.discard(node.name)
            if not dependents:
                dependencies.pop(dependency)
        for descendant in ast.walk(node):
            if isinstance(descendant, ast.Name) and isinstance(descendant.ctx, ast.Load):
                dependencies.setdefault(descendant.id, set()).add(node.name)
        self._function_definitions[-1][node.name] = node
        self._docker_cli_wrapper_specs[-1].pop(node.name, None)
        self._callable_factory_specs[-1].pop(node.name, None)
        self._callable_decorator_factory_specs[-1].pop(node.name, None)
        self._callable_layers[-1].pop(node.name, None)
        self._function_external_effects[-1].pop(node.name, None)
        self._bindings[-1][node.name] = ast.Name(id=node.name, ctx=ast.Load())

    def _visit_reachable_scope(self, node: ast.AST) -> None:
        if isinstance(node, ast.Lambda):
            self.visit(node.body)
            return
        if isinstance(node, ast.ClassDef):
            for statement in node.body:
                self.visit(statement)
            return
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.generic_visit(node)
            return
        for statement in node.body:
            self.visit(statement)
            if isinstance(statement, (ast.Raise, ast.Return)):
                break

    def _visit_scoped(
        self, node: ast.AST
    ) -> tuple[
        DockerCliWrapperSpec | None,
        dict[str, DockerCliWrapperSpec],
        DockerCliWrapperSpec | None,
        dict[str, DockerCliWrapperSpec],
        DockerCliWrapperSpec | None,
        tuple[ExternalPayloadEffect, ...],
        CallableLayers,
    ]:
        ancestor_bindings = _snapshot_mapping_scopes(self._bindings)
        ancestor_function_definitions = _snapshot_mapping_scopes(self._function_definitions)
        ancestor_class_definitions = _snapshot_mapping_scopes(self._class_definitions)
        ancestor_wrapper_specs = _snapshot_mapping_scopes(self._docker_cli_wrapper_specs)
        ancestor_class_wrapper_specs = _snapshot_mapping_scopes(self._class_wrapper_specs)
        ancestor_class_factory_specs = _snapshot_mapping_scopes(self._class_callable_factory_specs)
        ancestor_class_property_specs = _snapshot_mapping_scopes(self._class_property_factory_specs)
        ancestor_instance_classes = _snapshot_mapping_scopes(self._instance_classes)
        ancestor_class_aliases = _snapshot_mapping_scopes(self._class_aliases)
        ancestor_class_bases = _snapshot_mapping_scopes(self._class_bases)
        ancestor_callable_factories = _snapshot_mapping_scopes(self._callable_factory_specs)
        ancestor_decorator_factories = _snapshot_mapping_scopes(
            self._callable_decorator_factory_specs
        )
        ancestor_callable_layers = _snapshot_mapping_scopes(self._callable_layers)
        ancestor_function_effects = _snapshot_mapping_scopes(self._function_external_effects)
        ancestor_external_taints = [set(taints) for taints in self._external_payload_taints]
        ancestor_safe_paths = [set(paths) for paths in self._safe_payload_paths]
        ancestor_binding_markers = tuple(
            [set(markers) for markers in marker_scopes]
            for marker_scopes in self._binding_marker_scopes()
        )
        self._bindings.append({})
        self._docker_cli_aliases.append(set())
        self._docker_cli_payload_aliases.append(set())
        self._dynamic_python_aliases.append(set())
        self._getattr_aliases.append(set())
        self._vars_aliases.append(set())
        self._getattribute_aliases.append(set())
        self._local_callable_aliases.append(set())
        self._local_module_aliases.append(set())
        self._function_definitions.append({})
        self._function_wrapper_dependencies.append({})
        self._class_definitions.append({})
        self._docker_cli_wrapper_specs.append({})
        self._class_wrapper_specs.append({})
        self._class_callable_factory_specs.append({})
        self._class_property_factory_specs.append({})
        self._instance_classes.append({})
        self._class_aliases.append({})
        self._class_bases.append({})
        self._callable_factory_specs.append({})
        self._callable_decorator_factory_specs.append({})
        self._callable_layers.append({})
        self._function_external_effects.append({})
        self._pending_external_effects.append([])
        self._scope_kinds.append("class" if isinstance(node, ast.ClassDef) else "function")
        self._scope_local_names.append(self._owned_scope_names(node))
        self._scope_loaded_names.append(self._owned_scope_loaded_names(node))
        self._external_payload_taints.append(set())
        self._safe_payload_paths.append(set())
        self._global_declarations.append(set())
        self._nonlocal_declarations.append(set())
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for name in self._function_parameter_names(node.args):
                self._bindings[-1][name] = ast.Name(id=name, ctx=ast.Load())
        self._visit_reachable_scope(node)
        scoped_spec = (
            self._function_wrapper_spec(node, self._docker_cli_wrapper_specs[-1])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            else None
        )
        returned_spec = (
            self._returned_callable_spec(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else None
        )
        returned_decorator_factory_spec = (
            self._returned_callable_factory_spec(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else None
        )
        returned_callable_layers = (
            self._returned_callable_layers(node)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            else CallableLayers()
        )
        scoped_wrappers = dict(self._docker_cli_wrapper_specs[-1])
        scoped_factories = dict(self._callable_factory_specs[-1])
        scoped_external_effects = tuple(self._pending_external_effects[-1])
        _restore_mapping_scopes(self._bindings, ancestor_bindings)
        _restore_mapping_scopes(self._function_definitions, ancestor_function_definitions)
        _restore_mapping_scopes(self._class_definitions, ancestor_class_definitions)
        _restore_mapping_scopes(self._docker_cli_wrapper_specs, ancestor_wrapper_specs)
        _restore_mapping_scopes(self._class_wrapper_specs, ancestor_class_wrapper_specs)
        _restore_mapping_scopes(self._class_callable_factory_specs, ancestor_class_factory_specs)
        _restore_mapping_scopes(self._class_property_factory_specs, ancestor_class_property_specs)
        _restore_mapping_scopes(self._instance_classes, ancestor_instance_classes)
        _restore_mapping_scopes(self._class_aliases, ancestor_class_aliases)
        _restore_mapping_scopes(self._class_bases, ancestor_class_bases)
        _restore_mapping_scopes(self._callable_factory_specs, ancestor_callable_factories)
        _restore_mapping_scopes(
            self._callable_decorator_factory_specs, ancestor_decorator_factories
        )
        _restore_mapping_scopes(self._callable_layers, ancestor_callable_layers)
        _restore_mapping_scopes(self._function_external_effects, ancestor_function_effects)
        for index, taints in enumerate(ancestor_external_taints):
            self._external_payload_taints[index].clear()
            self._external_payload_taints[index].update(taints)
        for index, paths in enumerate(ancestor_safe_paths):
            self._safe_payload_paths[index].clear()
            self._safe_payload_paths[index].update(paths)
        for marker_scopes, ancestor_markers in zip(
            self._binding_marker_scopes(), ancestor_binding_markers, strict=True
        ):
            for index, markers in enumerate(ancestor_markers):
                marker_scopes[index].clear()
                marker_scopes[index].update(markers)
        self._nonlocal_declarations.pop()
        self._global_declarations.pop()
        self._safe_payload_paths.pop()
        self._external_payload_taints.pop()
        self._scope_loaded_names.pop()
        self._scope_local_names.pop()
        self._scope_kinds.pop()
        self._callable_decorator_factory_specs.pop()
        self._callable_factory_specs.pop()
        self._callable_layers.pop()
        self._pending_external_effects.pop()
        self._function_external_effects.pop()
        self._class_bases.pop()
        self._class_aliases.pop()
        self._instance_classes.pop()
        self._class_property_factory_specs.pop()
        self._class_callable_factory_specs.pop()
        self._class_wrapper_specs.pop()
        self._docker_cli_wrapper_specs.pop()
        self._class_definitions.pop()
        self._function_wrapper_dependencies.pop()
        self._function_definitions.pop()
        self._local_module_aliases.pop()
        self._local_callable_aliases.pop()
        self._getattribute_aliases.pop()
        self._vars_aliases.pop()
        self._getattr_aliases.pop()
        self._dynamic_python_aliases.pop()
        self._docker_cli_payload_aliases.pop()
        self._docker_cli_aliases.pop()
        self._bindings.pop()
        return (
            scoped_spec,
            scoped_wrappers,
            returned_spec,
            scoped_factories,
            returned_decorator_factory_spec,
            scoped_external_effects,
            returned_callable_layers,
        )

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802 - ast API
        self._global_declarations[-1].update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802 - ast API
        self._nonlocal_declarations[-1].update(node.names)

    def _stored_docker_cli_callable(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        if (
            self._docker_cli_callable(resolved)
            or self._partial_wrapper_spec(resolved) is not None
            or self._local_callable_boundary(resolved)
        ):
            return True
        if isinstance(resolved, ast.Dict):
            return any(
                self._stored_docker_cli_callable(value)
                for value in [*(key for key in resolved.keys if key is not None), *resolved.values]
            )
        if isinstance(resolved, (ast.List, ast.Set, ast.Tuple)):
            return any(self._stored_docker_cli_callable(value) for value in resolved.elts)
        if isinstance(resolved, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return self._comprehension_stores_docker_callable(resolved, (resolved.elt,))
        if isinstance(resolved, ast.DictComp):
            return self._comprehension_stores_docker_callable(
                resolved, (resolved.key, resolved.value)
            )
        if not isinstance(resolved, ast.Call):
            return False
        qualified = self._without_terminal_dunder_calls(self._qualified_name(resolved.func))
        transformed = self._iterable_transform_stores_docker_callable(resolved, qualified)
        if transformed is not None:
            return transformed
        if qualified in {
            "builtins.classmethod",
            "builtins.staticmethod",
            "classmethod",
            "staticmethod",
        }:
            return bool(resolved.args) and self._stored_docker_cli_callable(resolved.args[0])
        iterable_wrappers = {"enumerate", "iter", "reversed", "zip"}
        if qualified in iterable_wrappers and not self._plain_builtin_name_available(qualified):
            return False
        return qualified in iterable_wrappers | {
            f"builtins.{name}" for name in iterable_wrappers
        } and any(self._stored_docker_cli_callable(argument) for argument in resolved.args)

    @staticmethod
    def _comprehension_target_bindings(
        target: ast.expr, value: ast.expr
    ) -> dict[str, ast.expr] | None:
        if isinstance(target, ast.Name):
            return {target.id: value}
        if not (
            isinstance(target, (ast.List, ast.Tuple))
            and isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
        ):
            return None
        bindings: dict[str, ast.expr] = {}
        for nested_target, nested_value in zip(target.elts, value.elts, strict=True):
            nested = _DockerRunVisitor._comprehension_target_bindings(nested_target, nested_value)
            if nested is None:
                return None
            bindings.update(nested)
        return bindings

    def _comprehension_stores_docker_callable(
        self,
        node: ast.GeneratorExp | ast.ListComp | ast.SetComp | ast.DictComp,
        outputs: Sequence[ast.expr],
    ) -> bool:
        states: list[dict[str, ast.expr]] = [{}]
        for generator in node.generators:
            next_states: list[dict[str, ast.expr]] = []
            for state in states:
                substituter = _ExpressionSubstituter(state)
                iterable = self._resolved_value_expression(
                    substituter.visit(copy.deepcopy(generator.iter))
                )
                if not isinstance(iterable, (ast.List, ast.Set, ast.Tuple)):
                    substituted_outputs = (
                        substituter.visit(copy.deepcopy(output)) for output in outputs
                    )
                    return self._stored_docker_cli_callable(iterable) or any(
                        self._stored_docker_cli_callable(output) for output in substituted_outputs
                    )
                for value in iterable.elts:
                    bound = self._comprehension_target_bindings(generator.target, value)
                    if bound is None:
                        return self._stored_docker_cli_callable(iterable)
                    candidate = {**state, **bound}
                    candidate_substituter = _ExpressionSubstituter(candidate)
                    conditions = [
                        candidate_substituter.visit(copy.deepcopy(condition))
                        for condition in generator.ifs
                    ]
                    if any(
                        self._static_comprehension_condition_truth(condition) is False
                        for condition in conditions
                    ):
                        continue
                    next_states.append(candidate)
            states = next_states
            if not states:
                return False
        return any(
            self._stored_docker_cli_callable(
                _ExpressionSubstituter(state).visit(copy.deepcopy(output))
            )
            for state in states
            for output in outputs
        )

    def _static_comprehension_condition_truth(self, expression: ast.expr) -> bool | None:
        resolved = self._resolved_value_expression(expression)
        truth = self._static_condition_truth(resolved)
        if truth is not None:
            return truth
        if not (isinstance(resolved, ast.Call) and not resolved.args and not resolved.keywords):
            return None
        qualified = self._without_terminal_dunder_calls(self._qualified_name(resolved.func))
        empty_constructors = {"dict", "frozenset", "list", "set", "tuple"}
        builtin = qualified.removeprefix("builtins.") if qualified is not None else None
        return (
            False
            if builtin in empty_constructors
            and (
                qualified is not None
                and qualified.startswith("builtins.")
                or self._plain_builtin_name_available(builtin)
            )
            else None
        )

    def _iterable_transform_stores_docker_callable(
        self, node: ast.Call, qualified: str | None
    ) -> bool | None:
        builtin = qualified.removeprefix("builtins.") if qualified is not None else None
        if builtin not in {"filter", "map"}:
            return None
        if qualified == builtin and not self._plain_builtin_name_available(builtin):
            return None
        if len(node.args) < 2:
            return False
        iterable_arguments = node.args[1:]
        if builtin == "filter":
            return self._filtered_iterable_stores_docker_callable(node)
        source_has_callable = any(
            self._stored_docker_cli_callable(argument) for argument in iterable_arguments
        )
        target_qualified = self._without_terminal_dunder_calls(self._qualified_name(node.args[0]))
        if target_qualified in {
            "bool",
            "builtins.bool",
            "builtins.repr",
            "builtins.str",
            "repr",
            "str",
        } and (
            target_qualified.startswith("builtins.")
            or self._plain_builtin_name_available(target_qualified)
        ):
            return False
        invocations = self._static_map_invocations(node.args[0], iterable_arguments)
        if invocations is None:
            return source_has_callable
        resolved_result = False
        for invocation in invocations:
            definition = self._function_definition_for_callable(invocation.func)
            if definition is None:
                continue
            values = self._expanded_process_return_values(invocation, definition)
            resolved_result = resolved_result or bool(values)
            if any(self._stored_docker_cli_callable(value) for value in values):
                return True
        return source_has_callable and not resolved_result

    def _filtered_iterable_stores_docker_callable(self, node: ast.Call) -> bool:
        iterable = self._resolved_value_expression(node.args[1])
        if not isinstance(iterable, (ast.List, ast.Tuple)):
            return self._stored_docker_cli_callable(node.args[1])
        callable_elements = [
            element for element in iterable.elts if self._stored_docker_cli_callable(element)
        ]
        if not callable_elements:
            return False
        predicate = self._resolved_value_expression(node.args[0])
        if isinstance(predicate, ast.Constant) and predicate.value is None:
            return True
        invocations = [self._synthetic_call(predicate, [element]) for element in callable_elements]
        for invocation in invocations:
            definition = self._function_definition_for_callable(invocation.func)
            if definition is None:
                return True
            values = self._expanded_process_return_values(invocation, definition)
            if not values or any(
                self._static_condition_truth(value) is not False for value in values
            ):
                return True
        return False

    def _reject_stored_callable_assignment(
        self, targets: Sequence[ast.expr], value: ast.expr | None, line_number: int
    ) -> None:
        if value is None or not self._stored_docker_cli_callable(value):
            return
        resolved = self._resolve_expression(value)
        stores_container = isinstance(resolved, (ast.Dict, ast.List, ast.Set, ast.Tuple))
        has_unmodelled_target = self._scope_kinds[-1] == "class" or any(
            not isinstance(target, ast.Name) for target in targets
        )
        if stores_container or has_unmodelled_target:
            self._errors.append(
                f"{self._diagnostic_source}:{line_number}: storing a Docker CLI callable is "
                "unsupported"
            )

    def _mark_docker_payload_name(self, name: str) -> None:
        self._mark_docker_payload_object(ast.Name(id=name, ctx=ast.Load()))

    def _mark_docker_payload_object(self, expression: ast.expr) -> None:
        resolved = self._resolve_expression(expression)
        if isinstance(expression, ast.Name):
            self._docker_cli_payload_aliases[-1].add(expression.id)
            self._discard_safe_payload_paths(len(self._safe_payload_paths) - 1, expression.id)
        for index, scope in enumerate(self._bindings):
            for candidate in scope:
                candidate_value = self._resolve_expression(ast.Name(id=candidate, ctx=ast.Load()))
                if id(candidate_value) == id(resolved):
                    self._docker_cli_payload_aliases[index].add(candidate)
                    self._discard_safe_payload_paths(index, candidate)

    def _clear_docker_payload_object(self, expression: ast.expr) -> None:
        if self._conditional_depth:
            return
        resolved = self._resolve_expression(expression)
        aliases: list[tuple[int, str]] = []
        for index, scope in enumerate(self._bindings):
            for candidate in scope:
                candidate_value = self._resolve_expression(ast.Name(id=candidate, ctx=ast.Load()))
                if id(candidate_value) == id(resolved):
                    aliases.append((index, candidate))
        empty = ast.List(elts=[], ctx=ast.Load())
        for index, alias in aliases:
            self._docker_cli_payload_aliases[index].discard(alias)
            self._bindings[index][alias] = empty
        if isinstance(expression, ast.Name):
            self._docker_cli_payload_aliases[-1].discard(expression.id)

    def _record_subscript_mutation(self, target: ast.expr, value: ast.expr) -> None:
        if not isinstance(target, (ast.Attribute, ast.Subscript)):
            return
        root_name = self._mutation_root_name(target)
        if root_name is None:
            return
        self._reject_external_payload_mutation(root_name, value, implicit_path=True)
        path = self._payload_path(target)
        dangerous = self._process_payload_is_dangerous(value)
        if dangerous:
            if path is not None:
                self._safe_payload_paths[-1].discard(path)
            self._safe_payload_paths[-1].discard(root_name)
            self._mark_docker_payload_name(root_name)
            self._update_static_subscript_binding(target, value, root_name)
            return
        self._update_static_subscript_binding(target, value, root_name)
        if self._conditional_depth or path is None:
            return
        self._safe_payload_paths[-1].add(path)
        key = self._resolve_expression(target.slice) if isinstance(target, ast.Subscript) else None
        if isinstance(target, ast.Subscript) and isinstance(key, ast.Constant) and key.value == 0:
            self._safe_payload_paths[-1].add(root_name)

    def _mutation_root_name(self, target: ast.expr) -> str | None:
        if isinstance(target, ast.Subscript):
            reflected_owner = self._reflection_mapping_owner(target.value)
            if reflected_owner is not None:
                return self._payload_root_name(reflected_owner)
        return self._payload_root_name(target)

    def _update_static_subscript_binding(
        self, target: ast.expr, value: ast.expr, root_name: str
    ) -> None:
        if not isinstance(target, ast.Subscript):
            return
        key = self._resolve_expression(target.slice)
        replacement = self._resolved_process_value(value)
        if replacement is None:
            return
        for index in self._visible_scope_indexes():
            owner = self._bindings[index].get(root_name)
            if owner is None:
                continue
            updated = copy.deepcopy(owner)
            if (
                isinstance(updated, (ast.List, ast.Tuple))
                and isinstance(key, ast.Constant)
                and isinstance(key.value, int)
            ):
                try:
                    updated.elts[key.value] = replacement
                except IndexError:
                    return
                self._bindings[index][root_name] = updated
            elif isinstance(updated, ast.Dict):
                for position, candidate in enumerate(updated.keys):
                    if (
                        isinstance(candidate, ast.Constant)
                        and isinstance(key, ast.Constant)
                        and candidate.value == key.value
                    ):
                        updated.values[position] = replacement
                        self._bindings[index][root_name] = updated
                        break
            return

    def _reflected_mutation_target(self, expression: ast.expr) -> tuple[str, ast.expr] | None:
        if self._is_resolved_getattr_call(expression) and len(expression.args) >= 2:
            selector = self._resolve_expression(expression.args[1])
            if isinstance(selector, ast.Constant) and isinstance(selector.value, str):
                return selector.value, expression.args[0]
        if not (
            isinstance(expression, ast.Call)
            and expression.args
            and isinstance(expression.func, ast.Call)
            and self._attrgetter_callable(expression.func.func)
            and len(expression.func.args) == 1
        ):
            return None
        selector = self._resolve_expression(expression.func.args[0])
        if not isinstance(selector, ast.Constant) or not isinstance(selector.value, str):
            return None
        return selector.value, expression.args[0]

    @staticmethod
    def _container_mutation_payloads(
        node: ast.Call, name: str, qualified: str | None
    ) -> tuple[ast.expr, ...]:
        payload_index = UNBOUND_CONTAINER_MUTATION_PAYLOAD_INDEX.get(qualified or "")
        if payload_index is not None:
            return (*node.args[payload_index:], *(keyword.value for keyword in node.keywords))
        if name == "setdefault" and len(node.args) >= 2:
            return (node.args[1],)
        if name in {"__setattr__", "__setitem__"} and len(node.args) >= 2:
            return (node.args[1],)
        return (*node.args, *(keyword.value for keyword in node.keywords))

    def _container_mutation(self, node: ast.Call) -> ContainerMutation | None:
        callable_expression = self._resolved_value_expression(node.func)
        qualified = self._qualified_name(node.func)
        reflected_mutation = self._reflected_mutation_target(node.func)
        name = (
            callable_expression.attr
            if isinstance(callable_expression, ast.Attribute)
            else reflected_mutation[0]
            if reflected_mutation is not None
            else qualified.rsplit(".", 1)[-1]
            if qualified is not None
            else None
        )
        if name not in {
            "append",
            "clear",
            "extend",
            "insert",
            "setdefault",
            "setattr",
            "setitem",
            "update",
            "iadd",
            "__iadd__",
            "__setattr__",
            "__setitem__",
        }:
            return None
        unbound = qualified in UNBOUND_CONTAINER_MUTATION_PAYLOAD_INDEX
        owner = (
            node.args[0]
            if unbound and node.args
            else reflected_mutation[1]
            if reflected_mutation is not None
            else node.func.value
            if isinstance(node.func, ast.Attribute)
            else callable_expression.value
            if isinstance(callable_expression, ast.Attribute)
            else None
        )
        if owner is None:
            return None
        return ContainerMutation(
            name,
            owner,
            self._container_mutation_payloads(node, name, qualified),
        )

    def _record_container_mutation(self, node: ast.Call) -> None:
        mutation = self._container_mutation(node)
        if mutation is None:
            return
        if mutation.name == "clear" and not mutation.payloads:
            self._clear_docker_payload_object(mutation.owner)
            return
        owner_root = self._payload_root_name(mutation.owner)
        if owner_root is not None and any(
            self._external_payload_candidate(payload) for payload in mutation.payloads
        ):
            self._reject_external_payload_mutation(
                owner_root,
                ast.copy_location(ast.Tuple(elts=list(mutation.payloads), ctx=ast.Load()), node),
                implicit_path=True,
            )
        if any(self._stored_docker_cli_callable(payload) for payload in mutation.payloads):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: storing a Docker CLI callable through "
                f"{mutation.name} is unsupported"
            )
            return
        if any(
            self._docker_cli_expression(payload) or self._docker_executable_expression(payload)
            for payload in mutation.payloads
        ):
            self._mark_docker_payload_object(mutation.owner)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - ast API
        self.visit(node.value)
        aliases = [target.id for target in node.targets if isinstance(target, ast.Name)]
        self._reject_containers_alias(node.value, aliases, node.lineno)
        self._reject_stored_callable_assignment(node.targets, node.value, node.lineno)
        for target in node.targets:
            self.visit(target)
            self._record_subscript_mutation(target, node.value)
            self._bind(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - ast API
        if (
            isinstance(node.target, ast.Name)
            and self._scope_kinds[-1] != "function"
            and not self._postponed_annotations
        ):
            self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        aliases = [node.target.id] if isinstance(node.target, ast.Name) else []
        self._reject_containers_alias(node.value, aliases, node.lineno)
        self._reject_stored_callable_assignment([node.target], node.value, node.lineno)
        self.visit(node.target)
        if node.value is not None:
            self._record_subscript_mutation(node.target, node.value)
        self._bind(node.target, node.value)

    def visit_TypeAlias(self, node: ast.TypeAlias) -> None:  # noqa: N802 - ast API
        self.visit(node.name)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802 - ast API
        self.visit(node.value)
        self._reject_stored_callable_assignment([node.target], node.value, node.lineno)
        self.visit(node.target)
        self._bind(node.target, node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802 - ast API
        self.visit(node.target)
        current = self._resolve_expression(node.target)
        self.visit(node.value)
        self._reject_stored_callable_assignment([node.target], node.value, node.lineno)
        target_name = self._payload_root_name(node.target)
        if target_name is not None:
            combined = ast.copy_location(
                ast.BinOp(left=current, op=node.op, right=node.value), node
            )
            implicit_path = isinstance(node.target, (ast.Attribute, ast.Subscript))
            self._reject_external_payload_mutation(
                target_name, combined, implicit_path=implicit_path
            )
            self._reject_external_payload_mutation(
                target_name, node.value, implicit_path=implicit_path
            )
            if self._docker_cli_expression(combined) or self._docker_cli_expression(node.value):
                self._mark_docker_payload_name(target_name)

    def _visit_conditional_node(self, node: ast.AST) -> None:
        self._conditional_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self._conditional_depth -= 1

    def _visit_conditionally(self, nodes: Sequence[ast.AST]) -> None:
        self._conditional_depth += 1
        try:
            for node in nodes:
                self.visit(node)
                if isinstance(node, (ast.Break, ast.Continue, ast.Raise, ast.Return)):
                    break
        finally:
            self._conditional_depth -= 1

    def visit_If(self, node: ast.If) -> None:  # noqa: N802 - ast API
        self.visit(node.test)
        self._visit_conditionally(node.body)
        self._visit_conditionally(node.orelse)

    def visit_IfExp(self, node: ast.IfExp) -> None:  # noqa: N802 - ast API
        self.visit(node.test)
        self._visit_conditionally([node.body, node.orelse])

    def visit_BoolOp(self, node: ast.BoolOp) -> None:  # noqa: N802 - ast API
        if not node.values:
            return
        self.visit(node.values[0])
        self._visit_conditionally(node.values[1:])

    def _iteration_binding_expression(self, iterable: ast.expr) -> ast.expr:
        resolved = self._resolved_value_expression(iterable)
        if isinstance(resolved, ast.Dict):
            values = [key for key in resolved.keys if key is not None]
        elif isinstance(resolved, (ast.List, ast.Set, ast.Tuple)):
            values = list(resolved.elts)
        else:
            values = []
        if not values:
            return ast.Call(
                func=ast.Name(id="__unknown_iteration_value__", ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        result = values[-1]
        for value in reversed(values[:-1]):
            result = ast.IfExp(
                test=ast.Name(id="__iteration_choice__", ctx=ast.Load()),
                body=value,
                orelse=result,
            )
        return result

    def _visit_for_loop(self, node: ast.For | ast.AsyncFor) -> None:
        self.visit(node.iter)
        self._bind(node.target, self._iteration_binding_expression(node.iter))
        self._visit_conditionally(node.body)
        self._visit_conditionally(node.orelse)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802 - ast API
        self._visit_for_loop(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802 - ast API
        self._visit_for_loop(node)

    def _with_binding_expression(self, expression: ast.expr) -> ast.expr:
        resolved = self._resolved_value_expression(expression)
        if isinstance(resolved, ast.Call) and self._qualified_name(resolved.func) in {
            "contextlib.nullcontext",
            "nullcontext",
        }:
            return (
                resolved.args[0]
                if resolved.args
                else self._keyword_expression(resolved, "enter_result") or ast.Constant(value=None)
            )
        return ast.Call(
            func=ast.Name(id="__unknown_context_value__", ctx=ast.Load()),
            args=[],
            keywords=[],
        )

    def _visit_with_statement(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind(
                    item.optional_vars,
                    self._with_binding_expression(item.context_expr),
                )
        self._visit_conditionally(node.body)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802 - ast API
        self._visit_with_statement(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802 - ast API
        self._visit_with_statement(node)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802 - ast API
        self.visit(node.test)
        self._visit_conditionally(node.body)
        self._visit_conditionally(node.orelse)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802 - ast API
        self._visit_conditional_node(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:  # noqa: N802 - ast API
        self._visit_conditional_node(node)

    @staticmethod
    def _unknown_runtime_value() -> ast.Call:
        return ast.Call(
            func=ast.Name(id="__unknown_runtime_value__", ctx=ast.Load()),
            args=[],
            keywords=[],
        )

    def _bind_match_name(self, name: str | None, value: ast.expr) -> None:
        if name is not None:
            self._bind(ast.Name(id=name, ctx=ast.Store()), value)

    def _bind_match_sequence(self, pattern: ast.MatchSequence, value: ast.expr) -> None:
        resolved = self._resolved_value_expression(value)
        values = list(resolved.elts) if isinstance(resolved, (ast.List, ast.Tuple)) else []
        for index, nested in enumerate(pattern.patterns):
            nested_value = values[index] if index < len(values) else self._unknown_runtime_value()
            self._bind_match_pattern(nested, nested_value)

    def _bind_match_mapping(self, pattern: ast.MatchMapping, value: ast.expr) -> None:
        resolved = self._resolved_value_expression(value)
        for key, nested in zip(pattern.keys, pattern.patterns, strict=True):
            nested_value: ast.expr | None = (
                self._static_dict_value(resolved, key) if isinstance(resolved, ast.Dict) else None
            )
            self._bind_match_pattern(nested, nested_value or self._unknown_runtime_value())
        self._bind_match_name(pattern.rest, value)

    def _bind_match_pattern(self, pattern: ast.pattern, value: ast.expr) -> None:
        if isinstance(pattern, ast.MatchAs):
            self._bind_match_name(pattern.name, value)
            if pattern.pattern is not None:
                self._bind_match_pattern(pattern.pattern, value)
            return
        if isinstance(pattern, ast.MatchStar):
            self._bind_match_name(pattern.name, value)
            return
        if isinstance(pattern, ast.MatchOr):
            for nested in pattern.patterns:
                self._bind_match_pattern(nested, value)
            return
        if isinstance(pattern, ast.MatchSequence):
            self._bind_match_sequence(pattern, value)
            return
        if isinstance(pattern, ast.MatchMapping):
            self._bind_match_mapping(pattern, value)
            return
        if isinstance(pattern, ast.MatchClass):
            for nested in [*pattern.patterns, *pattern.kwd_patterns]:
                self._bind_match_pattern(nested, self._unknown_runtime_value())

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802 - ast API
        self.visit(node.subject)
        for case in node.cases:
            self._bind_match_pattern(case.pattern, node.subject)
            if case.guard is not None:
                self._visit_conditionally([case.guard])
            self._visit_conditionally(case.body)

    @staticmethod
    def _function_annotations(
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> tuple[ast.expr, ...]:
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        parameters.extend(
            parameter for parameter in (node.args.vararg, node.args.kwarg) if parameter is not None
        )
        annotations = [
            parameter.annotation for parameter in parameters if parameter.annotation is not None
        ]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        return tuple(annotations)

    def _visit_function_defaults(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)
        if not self._postponed_annotations:
            for annotation in self._function_annotations(node):
                self.visit(annotation)

    def _decorated_wrapper_spec(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> DockerCliWrapperSpec | None:
        result: DockerCliWrapperSpec | None = None
        for decorator in reversed(node.decorator_list):
            decorator_callable = decorator.func if isinstance(decorator, ast.Call) else decorator
            candidate = self._merge_wrapper_specs(
                self._callable_factory_spec(decorator),
                self._expression_callable_layers(decorator).at(1),
            )
            if isinstance(decorator, ast.Call):
                candidate = self._merge_wrapper_specs(
                    candidate,
                    self._callable_decorator_factory_spec(decorator.func),
                )
                decorator_class = self._known_class_name(decorator.func)
                if decorator_class is not None:
                    candidate = self._merge_wrapper_specs(
                        candidate,
                        self._class_method_callable_factory_spec(decorator_class, "__call__"),
                    )
            else:
                decorator_class = self._known_class_name(decorator)
                if decorator_class is not None:
                    candidate = self._merge_wrapper_specs(
                        candidate,
                        self._class_method_callable_factory_spec(decorator_class, "__new__"),
                    )
                    candidate = self._merge_wrapper_specs(
                        candidate,
                        self._class_method_wrapper_spec(
                            decorator_class,
                            "__call__",
                            bound_instance=True,
                        ),
                    )
            if candidate is None and self._local_callable_boundary(decorator_callable):
                parameters = frozenset(self._function_parameter_names(node.args))
                candidate = DockerCliWrapperSpec(
                    self._positional_parameter_names(node.args),
                    frozenset(parameter.arg for parameter in node.args.kwonlyargs),
                    node.args.vararg.arg if node.args.vararg is not None else None,
                    node.args.kwarg.arg if node.args.kwarg is not None else None,
                    parameters,
                    frozenset(),
                )
            result = self._merge_wrapper_specs(result, candidate)
        return result

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast API
        self._visit_function_defaults(node)
        self._visit_scoped(node)

    @staticmethod
    def _empty_arguments() -> ast.arguments:
        return ast.arguments(
            posonlyargs=[],
            args=[],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        )

    def _comprehension_scope_node(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        *,
        static_bindings: bool = False,
    ) -> ast.Lambda:
        expressions: list[ast.expr] = []
        for index, generator in enumerate(node.generators):
            if index:
                expressions.append(generator.iter)
            binding = (
                ast.Constant(value=None)
                if static_bindings
                else self._iteration_binding_expression(generator.iter)
            )
            expressions.append(
                ast.copy_location(
                    ast.NamedExpr(target=cast(ast.Name, generator.target), value=binding),
                    generator.target,
                )
            )
            expressions.extend(generator.ifs)
        if isinstance(node, ast.DictComp):
            expressions.extend((node.key, node.value))
        else:
            expressions.append(node.elt)
        return ast.Lambda(
            args=self._empty_arguments(),
            body=ast.Tuple(elts=expressions, ctx=ast.Load()),
        )

    @staticmethod
    def _comprehension_named_expressions(
        node: ast.AST,
    ) -> tuple[ast.NamedExpr, ...]:
        expressions: list[ast.NamedExpr] = []
        pending = [node]
        while pending:
            current = pending.pop()
            if isinstance(current, PYTHON_SCOPE_BOUNDARIES):
                continue
            if isinstance(current, ast.NamedExpr):
                expressions.append(current)
                pending.append(current.value)
                continue
            pending.extend(ast.iter_child_nodes(current))
        return tuple(expressions)

    def _comprehension_iteration_values(self, expression: ast.expr) -> tuple[ast.expr, ...]:
        resolved = self._resolved_value_expression(expression)
        if self._static_comprehension_condition_truth(resolved) is False:
            return ()
        if isinstance(resolved, ast.Dict):
            return tuple(key for key in resolved.keys if key is not None)
        if isinstance(resolved, (ast.List, ast.Set, ast.Tuple)):
            return tuple(resolved.elts)
        return (self._unknown_runtime_value(),)

    @staticmethod
    def _deduplicate_named_expressions(
        expressions: Sequence[ast.NamedExpr],
    ) -> tuple[ast.NamedExpr, ...]:
        seen: set[int] = set()
        unique: list[ast.NamedExpr] = []
        for expression in expressions:
            if id(expression) in seen:
                continue
            seen.add(id(expression))
            unique.append(expression)
        return tuple(unique)

    def _executed_comprehension_named_expressions(
        self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp
    ) -> tuple[ast.NamedExpr, ...]:
        executed: list[ast.NamedExpr] = []
        states: list[dict[str, ast.expr]] = [{}]
        for index, generator in enumerate(node.generators):
            next_states: list[dict[str, ast.expr]] = []
            for state in states:
                substituter = _ExpressionSubstituter(state)
                iterable = substituter.visit(copy.deepcopy(generator.iter))
                if index:
                    executed.extend(self._comprehension_named_expressions(generator.iter))
                for value in self._comprehension_iteration_values(iterable):
                    bound = self._comprehension_target_bindings(generator.target, value) or {}
                    candidate = {**state, **bound}
                    candidate_substituter = _ExpressionSubstituter(candidate)
                    for condition in generator.ifs:
                        executed.extend(self._comprehension_named_expressions(condition))
                        resolved_condition = candidate_substituter.visit(copy.deepcopy(condition))
                        if self._static_comprehension_condition_truth(resolved_condition) is False:
                            break
                    else:
                        next_states.append(candidate)
            states = next_states
            if not states:
                break
        if states:
            outputs = (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
            for output in outputs:
                executed.extend(self._comprehension_named_expressions(output))
        return self._deduplicate_named_expressions(executed)

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
    ) -> None:
        if not node.generators:
            return
        self.visit(node.generators[0].iter)
        synthetic_scope = ast.fix_missing_locations(
            ast.copy_location(self._comprehension_scope_node(node), node)
        )
        self._visit_scoped(synthetic_scope)
        for assignment in self._executed_comprehension_named_expressions(node):
            self.visit(assignment)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802 - ast API
        self._visit_comprehension(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast API
        for alias in node.names:
            root_name = alias.name.split(".", 1)[0]
            if root_name in {"scripts", "services"} or self._local_python_module(alias.name):
                self._local_module_aliases[-1].add(alias.asname or root_name)
            if root_name not in {
                "anyio",
                "asyncio",
                "atexit",
                "builtins",
                "concurrent",
                "contextlib",
                "docker",
                "functools",
                "importlib",
                "multiprocessing",
                "operator",
                "os",
                "pathlib",
                "pty",
                "shlex",
                "subprocess",
                "sys",
                "threading",
                "types",
            }:
                continue
            local_name = alias.asname or root_name
            expression = (
                _dotted_expression(alias.name)
                if alias.asname
                else ast.Name(id=root_name, ctx=ast.Load())
            )
            self._bind(
                ast.Name(id=local_name, ctx=ast.Load()),
                expression,
            )
            if local_name not in self._bindings[-1]:
                self._bindings[-1][local_name] = expression
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - ast API
        root_name = (node.module or "").split(".", 1)[0]
        if (
            node.level
            or root_name in {"scripts", "services"}
            or self._local_python_module(node.module or "")
        ):
            for alias in node.names:
                if alias.name == "*":
                    self._errors.append(
                        f"{self._diagnostic_source}:{node.lineno}: unsupported local Python star "
                        "import"
                    )
                    continue
                local_name = alias.asname or alias.name
                self._local_callable_aliases[-1].add(local_name)
                self._local_module_aliases[-1].add(local_name)
        sensitive_roots = {
            "anyio",
            "asyncio",
            "atexit",
            "builtins",
            "concurrent",
            "contextlib",
            "docker",
            "functools",
            "importlib",
            "multiprocessing",
            "operator",
            "os",
            "pty",
            "shlex",
            "subprocess",
            "sys",
            "threading",
            "types",
        }
        if root_name in sensitive_roots and any(alias.name == "*" for alias in node.names):
            marker = (
                "Docker SDK"
                if root_name == "docker"
                else "dynamic Python execution"
                if root_name == "builtins"
                else "dynamic Python reflection"
                if root_name == "operator"
                else "Docker CLI"
            )
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported {marker} star import"
            )
        if root_name in sensitive_roots | {"pathlib"} and node.module:
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                self._bind(
                    ast.Name(id=local_name, ctx=ast.Load()),
                    ast.Attribute(
                        value=_dotted_expression(node.module),
                        attr=alias.name,
                        ctx=ast.Load(),
                    ),
                )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API
        self._visit_function_defaults(node)
        self._register_function(node)
        (
            scoped_spec,
            _,
            returned_spec,
            _,
            returned_decorator_factory_spec,
            external_effects,
            returned_callable_layers,
        ) = self._visit_scoped(node)
        scoped_spec = self._merge_wrapper_specs(scoped_spec, self._decorated_wrapper_spec(node))
        callable_layers = self._merge_callable_layers(
            self._callable_layer(scoped_spec, 0), returned_callable_layers
        )
        if callable_layers.specs:
            self._callable_layers[-1][node.name] = callable_layers
        if external_effects:
            self._function_external_effects[-1][node.name] = FunctionExternalEffects(
                node, external_effects
            )
        if returned_spec is not None:
            self._callable_factory_specs[-1][node.name] = returned_spec
        if returned_decorator_factory_spec is not None:
            self._callable_decorator_factory_specs[-1][node.name] = returned_decorator_factory_spec
        merged = self._merge_wrapper_specs(
            self._docker_cli_wrapper_specs[-1].get(node.name), scoped_spec
        )
        if merged is not None:
            self._docker_cli_wrapper_specs[-1][node.name] = merged
            self._docker_cli_aliases[-1].add(node.name)
        if (
            merged is not None
            or returned_spec is not None
            or returned_decorator_factory_spec is not None
            or callable_layers.specs
        ):
            self._refresh_function_wrapper_specs({node.name})

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        self._visit_function_defaults(node)
        self._register_function(node)
        (
            scoped_spec,
            _,
            returned_spec,
            _,
            returned_decorator_factory_spec,
            external_effects,
            returned_callable_layers,
        ) = self._visit_scoped(node)
        scoped_spec = self._merge_wrapper_specs(scoped_spec, self._decorated_wrapper_spec(node))
        callable_layers = self._merge_callable_layers(
            self._callable_layer(scoped_spec, 0), returned_callable_layers
        )
        if callable_layers.specs:
            self._callable_layers[-1][node.name] = callable_layers
        if external_effects:
            self._function_external_effects[-1][node.name] = FunctionExternalEffects(
                node, external_effects
            )
        if returned_spec is not None:
            self._callable_factory_specs[-1][node.name] = returned_spec
        if returned_decorator_factory_spec is not None:
            self._callable_decorator_factory_specs[-1][node.name] = returned_decorator_factory_spec
        merged = self._merge_wrapper_specs(
            self._docker_cli_wrapper_specs[-1].get(node.name), scoped_spec
        )
        if merged is not None:
            self._docker_cli_wrapper_specs[-1][node.name] = merged
            self._docker_cli_aliases[-1].add(node.name)
        if (
            merged is not None
            or returned_spec is not None
            or returned_decorator_factory_spec is not None
            or callable_layers.specs
        ):
            self._refresh_function_wrapper_specs({node.name})

    def _reject_containers_alias(
        self, value: ast.expr | None, aliases: Sequence[str], line_number: int
    ) -> None:
        if value is not None and self._docker_api_client_constructor(value):
            for alias in aliases:
                self._errors.append(
                    f"{self._diagnostic_source}:{line_number}: Docker SDK APIClient alias "
                    f"{alias!r} is forbidden"
                )
            return
        if not isinstance(value, ast.Attribute) or value.attr not in {
            "api",
            "containers",
            "images",
        }:
            return
        if value.attr != "containers" and not self._docker_client_owner(value.value):
            return
        for alias in aliases:
            self._errors.append(
                f"{self._diagnostic_source}:{line_number}: Docker SDK {value.attr} alias "
                f"{alias!r} is forbidden"
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - ast API
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._class_definitions[-1][node.name] = node
        self._bindings[-1][node.name] = ast.Name(id=node.name, ctx=ast.Load())
        self._class_bases[-1][node.name] = tuple(
            base_name
            for expression in node.bases
            if (base_name := self._known_class_name(expression)) is not None
        )
        _, method_specs, _, method_factories, _, _, _ = self._visit_scoped(node)
        for child in node.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            factory_spec = method_factories.get(child.name)
            if factory_spec is not None:
                self._class_callable_factory_specs[-1][(node.name, child.name)] = factory_spec
            spec = method_specs.get(child.name)
            if spec is None:
                spec = DockerCliWrapperSpec(
                    self._positional_parameter_names(child.args),
                    frozenset(parameter.arg for parameter in child.args.kwonlyargs),
                    child.args.vararg.arg if child.args.vararg is not None else None,
                    child.args.kwarg.arg if child.args.kwarg is not None else None,
                    frozenset(),
                    frozenset(),
                )
            decorators = {
                qualified
                for decorator in child.decorator_list
                if (qualified := self._qualified_name(decorator)) is not None
            }
            if factory_spec is not None and decorators & {
                "builtins.property",
                "cached_property",
                "functools.cached_property",
                "property",
            }:
                self._class_property_factory_specs[-1][(node.name, child.name)] = factory_spec
            mode = (
                "static"
                if decorators & {"builtins.staticmethod", "staticmethod"}
                else "class"
                if decorators & {"builtins.classmethod", "classmethod"}
                else "instance"
            )
            self._class_wrapper_specs[-1][(node.name, child.name)] = (spec, mode)

    def _docker_cli_callable(self, expression: ast.expr) -> bool:
        if isinstance(expression, ast.Lambda):
            return self._function_wrapper_spec(expression, {}) is not None
        if self._wrapper_spec_for_callable(expression) is not None:
            return True
        if self._marked_name(expression, self._docker_cli_aliases):
            return True
        return self._direct_docker_cli_callable(expression)

    def _direct_docker_cli_callable(self, expression: ast.expr) -> bool:
        return self._event_loop_process_callable(expression) or (
            self._qualified_name(expression) in PYTHON_DOCKER_CLI_CALLABLES
        )

    def _dynamic_python_callable(self, expression: ast.expr) -> bool:
        return self._marked_name(expression, self._dynamic_python_aliases) or (
            self._qualified_name(expression) in DYNAMIC_PYTHON_CALLABLES
        )

    def _opaque_process_module_reflection(self, node: ast.Call) -> bool:
        if not (self._is_resolved_getattr_call(node) and len(node.args) >= 2):
            return False
        owner = self._qualified_name(node.args[0])
        selector = self._static_string(node.args[1])
        return owner in PROCESS_MODULE_NAMES and selector is None

    def _process_module_key(self, expression: ast.expr) -> bool:
        value = self._static_string(expression)
        if value is None or value.split(".", 1)[0] in PROCESS_MODULE_NAMES:
            return True
        binding = self._binding(value)
        qualified = self._qualified_name(binding) if binding is not None else None
        return qualified is not None and qualified.split(".", 1)[0] in PROCESS_MODULE_NAMES

    def _dynamic_import_callable_name(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        callable_name = self._qualified_name(resolved)
        if callable_name is not None and callable_name.endswith(".__call__"):
            callable_name = callable_name.removesuffix(".__call__")
        if callable_name in {
            "__builtins__.__import__",
            "__import__",
            "builtins.__import__",
            "importlib.__import__",
            "importlib.import_module",
        }:
            return callable_name
        if isinstance(resolved, ast.Attribute) and resolved.attr == "__import__":
            return "builtins.__import__"
        if not isinstance(resolved, ast.Subscript):
            return None
        owner = self._qualified_name(resolved.value)
        key = self._static_string(resolved.slice)
        return (
            "builtins.__import__"
            if owner in {"__builtins__", "builtins"} and key == "__import__"
            else None
        )

    def _dynamic_process_import_source(self, expression: ast.Call) -> bool:
        module = (
            expression.args[0] if expression.args else self._keyword_expression(expression, "name")
        )
        if module is None:
            return False
        return self._dynamic_import_callable_name(
            expression.func
        ) is not None and self._process_module_key(module)

    def _runtime_module_mapping(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        if self._qualified_name(resolved) == "sys.modules":
            return True
        if self._dynamic_sys_registry_mapping(resolved):
            return True
        return (
            isinstance(resolved, ast.Call)
            and not resolved.args
            and not resolved.keywords
            and self._runtime_namespace_callable_name(resolved.func)
            in {
                "builtins.globals",
                "builtins.locals",
                "builtins.vars",
                "globals",
                "locals",
                "vars",
            }
        )

    def _runtime_mapping_lookup_parts(
        self, expression: ast.expr
    ) -> tuple[ast.expr, ast.expr] | None:
        resolved = self._resolve_expression(expression)
        if isinstance(resolved, ast.Subscript):
            return resolved.value, resolved.slice
        if not isinstance(resolved, ast.Call):
            return None
        function = self._resolve_expression(resolved.func)
        if not isinstance(function, ast.Attribute) or function.attr not in {
            "__getitem__",
            "get",
            "getitem",
            "pop",
            "setdefault",
        }:
            return None
        qualified = self._qualified_name(function)
        if (
            qualified
            in {
                "dict.__getitem__",
                "dict.get",
                "dict.pop",
                "dict.setdefault",
                "operator.getitem",
            }
            and len(resolved.args) >= 2
        ):
            return resolved.args[0], resolved.args[1]
        if (
            isinstance(function, ast.Attribute)
            and function.attr in {"__getitem__", "get", "pop", "setdefault"}
            and resolved.args
        ):
            return function.value, resolved.args[0]
        return None

    def _runtime_namespace_scopes(self, expression: ast.expr) -> frozenset[int]:
        resolved = self._resolve_expression(expression)
        roots = [expression] if resolved is expression else [expression, resolved]
        scopes: set[int] = set()
        for root in roots:
            for descendant in ast.walk(root):
                if not (
                    isinstance(descendant, ast.Call)
                    and not descendant.args
                    and not descendant.keywords
                ):
                    continue
                qualified = self._runtime_namespace_callable_name(descendant.func)
                if qualified in {"builtins.globals", "globals"}:
                    scopes.add(0)
                elif qualified in {"builtins.locals", "builtins.vars", "locals", "vars"}:
                    scopes.add(len(self._bindings) - 1)
        return frozenset(scopes)

    def _runtime_local_namespace_mapping(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        return (
            isinstance(resolved, ast.Call)
            and not resolved.args
            and not resolved.keywords
            and self._runtime_namespace_callable_name(resolved.func)
            in {"builtins.locals", "builtins.vars", "locals", "vars"}
        )

    def _runtime_namespace_name_scope(self, name: str) -> int:
        current_scope = len(self._bindings) - 1
        tracked_scopes = (
            self._bindings,
            self._function_definitions,
            self._docker_cli_wrapper_specs,
            self._callable_factory_specs,
            self._callable_decorator_factory_specs,
            self._callable_layers,
        )
        current_has_name = name in self._scope_local_names[current_scope] or any(
            name in tracked[current_scope] for tracked in tracked_scopes
        )
        if (
            self._scope_kinds[current_scope] == "function"
            and not current_has_name
            and name not in self._scope_loaded_names[current_scope]
        ):
            return current_scope
        for scope in self._visible_scope_indexes():
            if name in self._scope_local_names[scope] or any(
                name in tracked[scope] for tracked in tracked_scopes
            ):
                if scope != current_scope and not (
                    self._scope_kinds[current_scope] == "function"
                    and self._scope_kinds[scope] == "function"
                ):
                    return current_scope
                return scope
        return current_scope

    def _runtime_namespace_lookup(
        self, expression: ast.expr
    ) -> tuple[frozenset[int], str | None] | None:
        parts = self._runtime_mapping_lookup_parts(expression)
        if parts is None:
            return None
        mapping, key = parts
        scopes = self._runtime_namespace_scopes(mapping)
        name = self._static_string(key)
        if scopes and name is not None and self._runtime_local_namespace_mapping(mapping):
            scopes = frozenset({self._runtime_namespace_name_scope(name)})
        return (scopes, name) if scopes else None

    def _runtime_namespace_symbol(self, expression: ast.expr) -> tuple[int, str] | None:
        lookup = self._runtime_namespace_lookup(expression)
        if lookup is None:
            return None
        scopes, name = lookup
        if len(scopes) != 1 or name is None:
            return None
        return next(iter(scopes)), name

    def _runtime_namespace_qualified_name(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> str | None:
        visited = set() if seen is None else seen
        resolved = self._resolve_expression(expression)
        if id(resolved) in visited:
            return None
        nested_seen = visited | {id(resolved)}
        symbol = self._runtime_namespace_symbol(resolved)
        if symbol is not None:
            scope, name = symbol
            binding = self._bindings[scope].get(name)
            if binding is None:
                return None
            qualified = self._syntactic_qualified_name(binding)
            return (
                qualified
                if qualified is not None
                else self._runtime_namespace_qualified_name(binding, nested_seen)
            )
        if isinstance(resolved, ast.Attribute):
            owner = self._runtime_namespace_qualified_name(resolved.value, nested_seen)
            return f"{owner}.{resolved.attr}" if owner is not None else None
        if self._is_resolved_getattr_call(resolved) and len(resolved.args) >= 2:
            owner = self._runtime_namespace_qualified_name(resolved.args[0], nested_seen)
            attribute = self._static_string(resolved.args[1])
            if owner is not None and attribute is not None:
                return f"{owner}.{attribute}"
        return None

    def _runtime_namespace_member_callable_layers(
        self, expression: ast.expr, _seen: set[int]
    ) -> CallableLayers | None:
        if not isinstance(expression, (ast.Attribute, ast.Call)):
            return None
        resolved = self._resolve_expression(expression)
        owner: ast.expr
        attribute: str | None
        if isinstance(resolved, ast.Attribute):
            owner, attribute = resolved.value, resolved.attr
        elif self._is_resolved_getattr_call(resolved) and len(resolved.args) >= 2:
            owner, attribute = resolved.args[0], self._static_string(resolved.args[1])
        else:
            return None
        qualified = self._runtime_namespace_qualified_name(resolved)
        if qualified in PYTHON_DOCKER_CLI_CALLABLES:
            return self._callable_layer(
                self._direct_callable_wrapper_spec(_dotted_expression(qualified)), 0
            )
        if attribute is not None:
            return None
        symbol = self._runtime_namespace_symbol(owner)
        if symbol is not None:
            scope, name = symbol
            if self._scope_name_has_process_provenance(scope, name):
                return self._callable_layer(self._opaque_process_wrapper_spec(), 0)
        owner_qualified = self._runtime_namespace_qualified_name(owner)
        if owner_qualified is not None and owner_qualified.split(".", 1)[0] in PROCESS_MODULE_NAMES:
            return self._callable_layer(self._opaque_process_wrapper_spec(), 0)
        return None

    @staticmethod
    def _syntactic_qualified_name(expression: ast.expr) -> str | None:
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.Attribute):
            owner = _DockerRunVisitor._syntactic_qualified_name(expression.value)
            return f"{owner}.{expression.attr}" if owner is not None else None
        return None

    def _scope_name_has_process_provenance(self, scope: int, name: str) -> bool:
        layers = self._scope_name_callable_layers(scope, name, set())
        if any(spec is not None for spec in layers.specs):
            return True
        binding = self._bindings[scope].get(name)
        qualified = self._syntactic_qualified_name(binding) if binding is not None else None
        return qualified is not None and qualified.split(".", 1)[0] in PROCESS_MODULE_NAMES

    def _dynamic_sys_registry_mapping(self, expression: ast.expr) -> bool:
        if self._is_resolved_getattr_call(expression) and len(expression.args) >= 2:
            owner, selector = expression.args[:2]
        elif isinstance(expression, ast.Subscript):
            reflected_owner = self._reflection_mapping_owner(expression.value)
            if reflected_owner is None:
                return False
            owner, selector = reflected_owner, expression.slice
        else:
            return False
        registry = self._static_string(selector)
        return self._qualified_name(owner) == "sys" and registry in {None, "modules"}

    def _runtime_process_module_lookup(self, expression: ast.expr) -> bool:
        parts = self._runtime_mapping_lookup_parts(expression)
        if parts is None:
            return False
        mapping, key = parts
        namespace_scopes = self._runtime_namespace_scopes(mapping)
        if namespace_scopes:
            name = self._static_string(key)
            return name is None or any(
                self._scope_name_has_process_provenance(scope, name) for scope in namespace_scopes
            )
        if not self._contains_runtime_module_mapping(mapping):
            return False
        module = self._static_string(key)
        return module is None or module.split(".", 1)[0] in PROCESS_MODULE_NAMES

    def _contains_runtime_module_mapping(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        roots = [expression] if resolved is expression else [expression, resolved]
        return any(
            isinstance(descendant, ast.expr) and self._runtime_module_mapping(descendant)
            for root in roots
            for descendant in ast.walk(root)
        )

    def _dynamic_process_getattribute_source(self, expression: ast.Call) -> bool:
        resolved = self._resolve_expression(expression)
        if not isinstance(resolved, ast.Call):
            return False
        if self._is_resolved_getattr_call(resolved) and len(resolved.args) >= 2:
            owner, selector = resolved.args[:2]
        else:
            function = self._resolve_expression(resolved.func)
            if not isinstance(function, ast.Attribute) or function.attr != "__getattribute__":
                return False
            function_owner = self._qualified_name(function.value)
            if function_owner in {"builtins.object", "object"}:
                if len(resolved.args) < 2:
                    return False
                owner, selector = resolved.args[:2]
            else:
                if not resolved.args:
                    return False
                owner, selector = function.value, resolved.args[0]
        return (
            self._qualified_name(owner) in PROCESS_MODULE_NAMES
            and self._static_string(selector) is None
        )

    def _partial_process_invocation(self, expression: ast.Call) -> ast.Call | None:
        partial = self._resolve_expression(expression.func)
        if not (
            isinstance(partial, ast.Call)
            and partial.args
            and self._qualified_name(partial.func) == "functools.partial"
        ):
            return None
        return ast.Call(
            func=partial.args[0],
            args=[*partial.args[1:], *expression.args],
            keywords=[*partial.keywords, *expression.keywords],
        )

    def _class_method_definition(
        self, class_name: str, method_name: str, seen: set[str] | None = None
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        visited = set() if seen is None else seen
        if class_name in visited:
            return None
        visited.add(class_name)
        for index in self._visible_scope_indexes():
            scope = self._class_definitions[index]
            class_definition = scope.get(class_name)
            if class_definition is None:
                continue
            for statement in class_definition.body:
                if (
                    isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and statement.name == method_name
                ):
                    return statement
            break
        for index in self._visible_scope_indexes():
            bases = self._class_bases[index]
            for base in bases.get(class_name, ()):
                method_definition = self._class_method_definition(base, method_name, visited)
                if method_definition is not None:
                    return method_definition
        return None

    def _provenance_call_definition(
        self, expression: ast.Call
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | None:
        definition = self._function_definition_for_callable(expression.func)
        if definition is not None:
            return definition
        if isinstance(expression.func, ast.Attribute):
            class_name = self._instance_class_name(expression.func.value) or self._known_class_name(
                expression.func.value
            )
            method_name = expression.func.attr
        elif isinstance(expression.func, ast.Call):
            class_name = self._instance_class_name(expression.func)
            method_name = "__call__"
        else:
            return None
        return (
            self._class_method_definition(class_name, method_name)
            if class_name is not None
            else None
        )

    def _provenance_property_definition(
        self, expression: ast.Attribute
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        class_name = self._instance_class_name(expression.value)
        if class_name is None:
            return None
        definition = self._class_method_definition(class_name, expression.attr)
        if definition is None:
            return None
        decorators = {
            qualified
            for decorator in definition.decorator_list
            if (qualified := self._qualified_name(decorator)) is not None
        }
        return (
            definition
            if decorators
            & {
                "builtins.property",
                "cached_property",
                "functools.cached_property",
                "property",
            }
            else None
        )

    def _property_has_process_provenance(self, expression: ast.Attribute, seen: set[int]) -> bool:
        definition = self._provenance_property_definition(expression)
        if definition is None or id(definition) in seen:
            return False
        invocation = ast.Call(func=expression, args=[], keywords=[])
        return any(
            self._opaque_process_callable_provenance(value, seen | {id(definition)})
            for value in self._expanded_process_return_values(invocation, definition)
        )

    def _expression_has_process_provenance(self, expression: ast.expr, seen: set[int]) -> bool:
        reflected_owner = self._reflection_mapping_owner(expression)
        if (
            reflected_owner is not None
            and self._qualified_name(reflected_owner) in PROCESS_MODULE_NAMES
        ):
            return True
        if self._runtime_process_module_lookup(expression):
            return True
        return isinstance(expression, ast.Attribute) and self._property_has_process_provenance(
            expression, seen
        )

    def _call_has_process_provenance(self, expression: ast.Call, seen: set[int]) -> bool:
        if self._dynamic_process_import_source(
            expression
        ) or self._dynamic_process_getattribute_source(expression):
            return True
        partial_invocation = self._partial_process_invocation(expression)
        if partial_invocation is not None and self._opaque_process_callable_provenance(
            partial_invocation, seen
        ):
            return True
        call_definition = self._provenance_call_definition(expression)
        if call_definition is None or id(call_definition) in seen:
            return False
        return any(
            self._opaque_process_callable_provenance(value, seen | {id(call_definition)})
            for value in self._expanded_process_return_values(expression, call_definition)
        )

    def _opaque_process_callable_provenance(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> bool:
        resolved = self._resolve_expression(expression)
        roots = [expression] if resolved is expression else [expression, resolved]
        visited = set() if seen is None else seen
        for root in roots:
            if id(root) in visited:
                continue
            nested_seen = visited | {id(root)}
            for descendant in ast.walk(root):
                if not isinstance(descendant, ast.expr):
                    continue
                if self._expression_has_process_provenance(descendant, nested_seen):
                    return True
                if isinstance(descendant, ast.Call) and self._call_has_process_provenance(
                    descendant, nested_seen
                ):
                    return True
        return False

    def _opaque_process_callable_expression(self, expression: ast.expr) -> bool:
        if self._docker_cli_callable(expression):
            return False
        if isinstance(expression, ast.Attribute) and self._modeled_process_instance(
            expression.value
        ):
            return False
        if (
            isinstance(expression, ast.Attribute)
            and expression.attr == "__call__"
            and self._docker_cli_callable(expression.value)
        ):
            return False
        resolved = self._resolved_value_expression(expression)
        if not isinstance(resolved, ast.Call) and self._docker_cli_callable(resolved):
            return False
        return any(
            isinstance(descendant, ast.expr)
            and descendant is not expression
            and self._direct_docker_cli_callable(descendant)
            for descendant in ast.walk(expression)
        )

    def _static_format_call(self, expression: ast.expr) -> str | None:
        if not (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "format"
            and all(not isinstance(argument, ast.Starred) for argument in expression.args)
            and all(keyword.arg is not None for keyword in expression.keywords)
        ):
            return None
        template = self._static_string(expression.func.value)
        arguments = [self._static_string(argument) for argument in expression.args]
        keywords = {
            keyword.arg: self._static_string(keyword.value)
            for keyword in expression.keywords
            if keyword.arg is not None
        }
        if template is None or any(value is None for value in [*arguments, *keywords.values()]):
            return None
        try:
            return template.format(*arguments, **keywords)
        except (IndexError, KeyError, ValueError):
            return None

    def _resolved_literal_collection(
        self, expression: ast.List | ast.Set | ast.Tuple, seen: set[int]
    ) -> ast.expr:
        elements: list[ast.expr] = []
        for element in expression.elts:
            if isinstance(element, ast.Starred):
                expanded = self._resolved_literal_expression(element.value, seen)
                if isinstance(expanded, (ast.List, ast.Tuple)):
                    elements.extend(expanded.elts)
                    continue
            elements.append(self._resolved_literal_expression(element, seen))
        if isinstance(expression, ast.List):
            return ast.List(elts=elements, ctx=expression.ctx)
        if isinstance(expression, ast.Set):
            return ast.Set(elts=elements)
        return ast.Tuple(elts=elements, ctx=expression.ctx)

    def _resolved_literal_expression(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> ast.expr:
        resolved = self._resolve_expression(expression)
        visited = set() if seen is None else seen
        if id(resolved) in visited:
            return resolved
        nested_seen = visited | {id(resolved)}
        if isinstance(resolved, ast.Dict):
            keys = [
                self._resolved_literal_expression(key, nested_seen) if key is not None else None
                for key in resolved.keys
            ]
            values = [
                self._resolved_literal_expression(value, nested_seen) for value in resolved.values
            ]
            return ast.Dict(keys=keys, values=values)
        if isinstance(resolved, (ast.List, ast.Set, ast.Tuple)):
            return self._resolved_literal_collection(resolved, nested_seen)
        if isinstance(resolved, ast.UnaryOp):
            return ast.UnaryOp(
                op=resolved.op,
                operand=self._resolved_literal_expression(resolved.operand, nested_seen),
            )
        if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Mod):
            formatted = self._static_percent_format(resolved)
            return ast.Constant(value=formatted) if formatted is not None else resolved
        return resolved

    def _static_percent_format(self, expression: ast.expr) -> str | None:
        if not (isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Mod)):
            return None
        template_expression = self._resolved_literal_expression(expression.left)
        operand_expression = self._resolved_literal_expression(expression.right)
        try:
            template = ast.literal_eval(template_expression)
            operand = ast.literal_eval(operand_expression)
        except (SyntaxError, TypeError, ValueError):
            return None
        if not isinstance(template, (bytes, str)):
            return None
        try:
            result = template % operand
        except (KeyError, TypeError, ValueError):
            return None
        if isinstance(result, str):
            return result
        if not isinstance(result, bytes):
            return None
        try:
            return result.decode()
        except UnicodeDecodeError:
            return None

    def _static_string_sequence_exact(self, expression: ast.expr) -> list[str] | None:
        resolved = self._resolved_value_expression(expression)
        if not isinstance(resolved, (ast.List, ast.Tuple)):
            return None
        values: list[str] = []
        for element in resolved.elts:
            if isinstance(element, ast.Starred):
                expanded = self._static_string_sequence_exact(element.value)
                if expanded is None:
                    return None
                values.extend(expanded)
                continue
            value = self._static_string(element)
            if value is None:
                return None
            values.append(value)
        return values

    def _static_join_call(self, expression: ast.expr) -> str | None:
        if not (
            isinstance(expression, ast.Call)
            and isinstance(expression.func, ast.Attribute)
            and expression.func.attr == "join"
            and len(expression.args) == 1
            and not expression.keywords
        ):
            return None
        separator = self._static_string(expression.func.value)
        values = self._static_string_sequence_exact(expression.args[0])
        return separator.join(values) if separator is not None and values is not None else None

    def _static_transformed_string(self, expression: ast.expr) -> str | None:
        for resolver in (
            self._static_format_call,
            self._static_percent_format,
            self._static_join_call,
        ):
            value = resolver(expression)
            if value is not None:
                return value
        return None

    def _static_string(self, expression: ast.expr) -> str | None:
        resolved = self._resolved_value_expression(expression)
        constant = self._constant_text(resolved)
        if constant is not None:
            return constant
        transformed = self._static_transformed_string(resolved)
        if transformed is not None:
            return transformed
        if (
            isinstance(resolved, ast.Call)
            and self._qualified_name(resolved.func) in {"pathlib.Path", "pathlib.PurePath"}
            and len(resolved.args) == 1
            and not resolved.keywords
        ):
            return self._static_string(resolved.args[0])
        if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
            left = self._static_string(resolved.left)
            right = self._static_string(resolved.right)
            return left + right if left is not None and right is not None else None
        if isinstance(resolved, ast.JoinedStr):
            parts: list[str] = []
            for value in resolved.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                    continue
                if isinstance(value, ast.FormattedValue) and value.format_spec is None:
                    formatted = self._static_string(value.value)
                    if formatted is not None:
                        parts.append(formatted)
                        continue
                return None
            return "".join(parts)
        return None

    @staticmethod
    def _constant_text(expression: ast.expr) -> str | None:
        if not isinstance(expression, ast.Constant):
            return None
        if isinstance(expression.value, str):
            return expression.value
        if not isinstance(expression.value, bytes):
            return None
        try:
            return expression.value.decode()
        except UnicodeDecodeError:
            return None

    def _static_string_prefix(self, expression: ast.expr) -> str:
        exact = self._static_string(expression)
        if exact is not None:
            return exact
        resolved = self._resolved_value_expression(expression)
        if isinstance(resolved, ast.JoinedStr):
            parts: list[str] = []
            for value in resolved.values:
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    break
                parts.append(value.value)
            return "".join(parts)
        if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
            return self._static_string_prefix(resolved.left)
        return ""

    def _static_collection_prefix(self, resolved: ast.List | ast.Tuple) -> list[str]:
        tokens: list[str] = []
        for element in resolved.elts:
            if isinstance(element, ast.Starred):
                expanded = self._static_sequence_prefix(element.value)
                if expanded is None:
                    break
                tokens.extend(expanded)
                continue
            value = self._static_string(element)
            if value is None:
                break
            tokens.append(value)
        return tokens

    def _static_method_split_prefix(self, resolved: ast.expr) -> list[str] | None:
        if not (
            isinstance(resolved, ast.Call)
            and isinstance(resolved.func, ast.Attribute)
            and resolved.func.attr == "split"
            and not resolved.keywords
            and len(resolved.args) <= 1
        ):
            return None
        value = self._static_string(resolved.func.value)
        separator = self._static_string(resolved.args[0]) if resolved.args else None
        return (
            value.split(separator)
            if value is not None and (not resolved.args or separator is not None)
            else None
        )

    def _static_shlex_split_prefix(self, resolved: ast.expr) -> list[str] | None:
        if not (
            isinstance(resolved, ast.Call)
            and self._qualified_name(resolved.func) == "shlex.split"
            and len(resolved.args) == 1
            and not resolved.keywords
        ):
            return None
        value = self._static_string(resolved.args[0])
        if value is None:
            return None
        try:
            return shlex.split(value)
        except ValueError:
            return None

    def _static_sequence_prefix(self, expression: ast.expr) -> list[str] | None:
        resolved = self._resolved_value_expression(expression)
        if isinstance(resolved, (ast.List, ast.Tuple)):
            return self._static_collection_prefix(resolved)
        for resolver in (self._static_method_split_prefix, self._static_shlex_split_prefix):
            if (tokens := resolver(resolved)) is not None:
                return tokens
        if isinstance(resolved, ast.BinOp) and isinstance(resolved.op, ast.Add):
            left = self._static_sequence_prefix(resolved.left)
            if left is None:
                return None
            right = self._static_sequence_prefix(resolved.right)
            return left + (right or [])
        return None

    def _structured_docker_cli_expression(self, expression: ast.expr) -> bool | None:
        if isinstance(expression, ast.NamedExpr):
            return self._docker_cli_expression(expression.value)
        if isinstance(expression, ast.IfExp):
            return self._docker_cli_expression(expression.body) or self._docker_cli_expression(
                expression.orelse
            )
        if isinstance(expression, (ast.BoolOp, ast.Dict)):
            return any(self._docker_cli_expression(value) for value in expression.values)
        if isinstance(expression, ast.Starred):
            expanded = self._resolve_expression(expression.value)
            return (
                isinstance(expanded, (ast.List, ast.Tuple))
                and any(self._docker_cli_expression(element) for element in expanded.elts)
            ) or self._docker_cli_expression(expression.value)
        return None

    def _marked_payload_root(self, root_name: str | None) -> bool:
        return root_name is not None and self._marked_name(
            ast.Name(id=root_name, ctx=ast.Load()), self._docker_cli_payload_aliases
        )

    def _static_tokens_contain_docker_cli(self, tokens: Sequence[str]) -> bool:
        if not tokens:
            return False
        executable_index, error = _resolve_python_argv_command_position(tokens)
        if error:
            return True
        if executable_index is None or executable_index >= len(tokens):
            return False
        if _is_docker_executable(tokens[executable_index]):
            return True
        if Path(tokens[executable_index]).name not in SHELL_INTERPRETERS:
            return False
        command = _interpreter_command(tokens, executable_index)
        return bool(command and _contains_docker_execution(command))

    def _docker_cli_expression(self, expression: ast.expr) -> bool:
        root_name = self._payload_root_name(expression)
        safe_path = self._safe_payload_path(expression)
        if not safe_path and (
            (root_name is not None and self._external_payload_tainted(root_name))
            or self._marked_payload_root(root_name)
        ):
            return True
        if not safe_path and self._marked_name(expression, self._docker_cli_payload_aliases):
            return True
        resolved = self._resolved_value_expression(expression)
        structured = self._structured_docker_cli_expression(resolved)
        if structured is not None:
            return structured
        tokens = self._static_sequence_prefix(resolved)
        if tokens is not None:
            return self._static_tokens_contain_docker_cli(tokens)
        prefix = self._static_string_prefix(resolved)
        return bool(prefix and _contains_docker_execution(prefix))

    def _keyword_expression(self, node: ast.Call, name: str) -> ast.expr | None:
        return next((keyword.value for keyword in node.keywords if keyword.arg == name), None)

    def _docker_executable_expression(self, expression: ast.expr) -> bool:
        value = self._static_string_prefix(expression)
        return bool(value and _is_docker_executable(value))

    def _synthetic_call(
        self,
        target: ast.expr,
        arguments: Sequence[ast.expr],
        keywords: Sequence[ast.keyword] = (),
    ) -> ast.Call:
        invocation = ast.copy_location(
            ast.Call(func=target, args=list(arguments), keywords=list(keywords)), target
        )
        seen: set[int] = set()
        while id(invocation.func) not in seen:
            seen.add(id(invocation.func))
            expanded = self._partial_process_invocation(invocation)
            if expanded is None:
                break
            invocation = expanded
        return invocation

    def _static_map_invocations(
        self,
        target: ast.expr,
        iterable_expressions: Sequence[ast.expr],
    ) -> list[ast.Call] | None:
        iterables: list[list[ast.expr]] = []
        for expression in iterable_expressions:
            resolved = self._resolved_value_expression(expression)
            if not isinstance(resolved, (ast.List, ast.Tuple)):
                return None
            iterables.append(list(resolved.elts))
        return [
            self._synthetic_call(target, arguments) for arguments in zip(*iterables, strict=False)
        ]

    def _static_starmap_invocations(
        self, target: ast.expr, iterable_expression: ast.expr
    ) -> list[ast.Call] | None:
        iterable = self._resolved_value_expression(iterable_expression)
        if not isinstance(iterable, (ast.List, ast.Tuple)):
            return None
        invocations: list[ast.Call] = []
        for item in iterable.elts:
            arguments = self._resolved_value_expression(item)
            if not isinstance(arguments, (ast.List, ast.Tuple)):
                return None
            invocations.append(self._synthetic_call(target, arguments.elts))
        return invocations

    def _call_argument(self, node: ast.Call, name: str, position: int) -> ast.expr | None:
        keyword = self._keyword_expression(node, name)
        expanded = self._expanded_keyword_values(node, name)
        return (
            keyword
            if keyword is not None
            else expanded[0]
            if expanded
            else node.args[position]
            if position < len(node.args)
            else None
        )

    def _callback_arguments(self, expression: ast.expr | None) -> tuple[list[ast.expr], bool]:
        if expression is None:
            return [], False
        resolved = self._resolved_value_expression(expression)
        if isinstance(resolved, (ast.List, ast.Tuple)):
            return list(resolved.elts), False
        return [ast.Starred(value=expression, ctx=ast.Load())], True

    def _callback_keywords(self, expression: ast.expr | None) -> tuple[list[ast.keyword], bool]:
        if expression is None:
            return [], False
        resolved = self._resolved_value_expression(expression)
        if not isinstance(resolved, ast.Dict):
            return [ast.keyword(arg=None, value=expression)], True
        keywords: list[ast.keyword] = []
        for key, value in zip(resolved.keys, resolved.values, strict=True):
            resolved_key = self._resolve_expression(key) if key is not None else None
            if not (isinstance(resolved_key, ast.Constant) and isinstance(resolved_key.value, str)):
                return [ast.keyword(arg=None, value=expression)], True
            keywords.append(ast.keyword(arg=resolved_key.value, value=value))
        return keywords, False

    def _configured_callback_invocation(
        self,
        node: ast.Call,
        *,
        target_name: str,
        target_position: int,
        args_name: str,
        args_position: int,
        kwargs_name: str,
        kwargs_position: int,
    ) -> tuple[list[ast.Call], bool] | None:
        target = self._call_argument(node, target_name, target_position)
        if target is None:
            return None
        arguments, opaque_arguments = self._callback_arguments(
            self._call_argument(node, args_name, args_position)
        )
        keywords, opaque_keywords = self._callback_keywords(
            self._call_argument(node, kwargs_name, kwargs_position)
        )
        return [self._synthetic_call(target, arguments, keywords)], (
            opaque_arguments or opaque_keywords
        )

    def _static_process_invocation_result(
        self, target: ast.expr, invocations: list[ast.Call] | None
    ) -> tuple[list[ast.Call], bool]:
        return (
            [self._synthetic_call(target, [])] if invocations is None else invocations,
            invocations is None,
        )

    def _basic_process_callable_invocations(
        self, node: ast.Call, qualified: str | None
    ) -> tuple[list[ast.Call], bool] | None:
        qualified = self._without_terminal_dunder_calls(qualified)
        methodcaller = self._methodcaller_process_invocation(node)
        if methodcaller is not None:
            return [methodcaller], False
        attrgetter = self._attrgetter_process_invocation(node)
        if attrgetter is not None:
            return [attrgetter], False
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__call__":
            return [self._synthetic_call(node.func.value, node.args, node.keywords)], False
        if qualified == "asyncio.to_thread" and node.args:
            return [self._synthetic_call(node.args[0], node.args[1:], node.keywords)], False
        if qualified == "operator.call" and node.args:
            return [self._synthetic_call(node.args[0], node.args[1:], node.keywords)], False
        iterable_callback = self._builtin_iterable_process_callable_invocations(node, qualified)
        if iterable_callback is not None:
            return iterable_callback
        if qualified == "atexit.register" and node.args:
            return [self._synthetic_call(node.args[0], node.args[1:], node.keywords)], False
        return None

    def _builtin_iterable_process_callable_invocations(
        self, node: ast.Call, qualified: str | None
    ) -> tuple[list[ast.Call], bool] | None:
        if qualified in {"filter", "map", "sorted"} and not self._plain_builtin_name_available(
            qualified
        ):
            return None
        if qualified in {"builtins.map", "map"} and len(node.args) >= 2:
            return self._static_process_invocation_result(
                node.args[0], self._static_map_invocations(node.args[0], node.args[1:])
            )
        if qualified in {"builtins.filter", "filter"} and len(node.args) >= 2:
            return self._static_process_invocation_result(
                node.args[0], self._static_map_invocations(node.args[0], [node.args[1]])
            )
        if qualified in {"builtins.sorted", "sorted"}:
            return self._sorted_process_callable_invocations(node)
        return None

    def _sorted_process_callable_invocations(
        self, node: ast.Call
    ) -> tuple[list[ast.Call], bool] | None:
        if not node.args:
            return None
        direct_target = self._keyword_expression(node, "key")
        targets = (
            [direct_target]
            if direct_target is not None
            else self._expanded_keyword_values(node, "key")
        )
        if not targets:
            return None
        invocations: list[ast.Call] = []
        opaque = False
        for target in targets:
            target_invocations, target_opaque = self._static_process_invocation_result(
                target, self._static_map_invocations(target, [node.args[0]])
            )
            invocations.extend(target_invocations)
            opaque = opaque or target_opaque
        return invocations, opaque

    def _resolved_operator_factory_expression(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> ast.expr:
        expression = self._without_terminal_dunder_call_attributes(expression)
        resolved = self._resolved_value_expression(expression)
        resolved = self._without_terminal_dunder_call_attributes(resolved)
        if not isinstance(resolved, ast.Call):
            return resolved
        definition = self._function_definition_for_callable(resolved.func)
        if definition is None:
            return resolved
        visited = set() if seen is None else seen
        if id(definition) in visited or len(visited) >= MAX_CALLABLE_PROVENANCE_DEPTH:
            return resolved
        values = self._expanded_process_return_values(resolved, definition)
        return (
            self._resolved_operator_factory_expression(values[0], visited | {id(definition)})
            if len(values) == 1
            else resolved
        )

    def _methodcaller_process_invocation(self, node: ast.Call) -> ast.Call | None:
        factory = self._resolved_operator_factory_expression(node.func)
        if not (
            isinstance(factory, ast.Call)
            and factory.args
            and node.args
            and self._qualified_name(factory.func) == "operator.methodcaller"
        ):
            return None
        owner = node.args[0]
        selector = self._static_string(factory.args[0])
        callable_expression: ast.expr = (
            owner
            if selector == "__call__"
            else ast.Attribute(value=owner, attr=selector, ctx=ast.Load())
            if selector is not None
            else ast.Call(
                func=ast.Name(id="getattr", ctx=ast.Load()),
                args=[owner, factory.args[0]],
                keywords=[],
            )
        )
        return ast.copy_location(
            self._synthetic_call(callable_expression, factory.args[1:], factory.keywords),
            node,
        )

    def _selected_attrgetter_invocation(
        self, expression: ast.expr
    ) -> tuple[ast.Call, int | None] | None:
        callable_reference = self._resolved_value_expression(expression)
        selected_index: int | None = None
        if isinstance(callable_reference, ast.Subscript):
            selected_index = self._static_signed_integer(callable_reference.slice)
            if selected_index is None:
                return None
            callable_reference = self._resolved_value_expression(callable_reference.value)
        return (
            (callable_reference, selected_index)
            if isinstance(callable_reference, ast.Call) and callable_reference.args
            else None
        )

    def _static_signed_integer(self, expression: ast.expr) -> int | None:
        resolved = self._resolved_value_expression(expression)
        if isinstance(resolved, ast.Constant) and isinstance(resolved.value, int):
            return resolved.value
        if not (
            isinstance(resolved, ast.UnaryOp) and isinstance(resolved.op, (ast.UAdd, ast.USub))
        ):
            return None
        operand = self._resolved_value_expression(resolved.operand)
        if not isinstance(operand, ast.Constant) or not isinstance(operand.value, int):
            return None
        return -operand.value if isinstance(resolved.op, ast.USub) else operand.value

    def _attrgetter_process_invocation(self, node: ast.Call) -> ast.Call | None:
        selected = self._selected_attrgetter_invocation(node.func)
        if selected is None:
            return None
        selection, selected_index = selected
        normalized_selections, _ = self._process_callable_invocations(selection)
        if len(normalized_selections) == 1 and normalized_selections[0] is not selection:
            selection = normalized_selections[0]
        factory = self._resolved_operator_factory_expression(selection.func)
        if not (
            isinstance(factory, ast.Call)
            and factory.args
            and self._qualified_name(factory.func) == "operator.attrgetter"
        ):
            return None
        if selected_index is None and len(factory.args) != 1:
            return None
        if selected_index is not None:
            try:
                selector_expression = factory.args[selected_index]
            except IndexError:
                return None
        else:
            selector_expression = factory.args[0]
        owner = selection.args[0]
        selector = self._static_string(selector_expression)
        callable_expression: ast.expr
        if selector is None:
            callable_expression = ast.Call(
                func=ast.Name(id="getattr", ctx=ast.Load()),
                args=[owner, selector_expression],
                keywords=[],
            )
        else:
            callable_expression = owner
            for attribute in selector.split("."):
                callable_expression = ast.Attribute(
                    value=callable_expression, attr=attribute, ctx=ast.Load()
                )
        return ast.copy_location(
            self._synthetic_call(callable_expression, node.args, node.keywords), node
        )

    def _configured_process_callable_invocations(
        self, node: ast.Call, qualified: str | None, dispatch_kind: str | None
    ) -> tuple[list[ast.Call], bool] | None:
        if (
            qualified in {"multiprocessing.Process", "threading.Thread"}
            or dispatch_kind == "context_process"
        ):
            invocation = self._configured_callback_invocation(
                node,
                target_name="target",
                target_position=1,
                args_name="args",
                args_position=3,
                kwargs_name="kwargs",
                kwargs_position=4,
            )
            return invocation if invocation is not None else ([node], False)
        if qualified == "threading.Timer":
            invocation = self._configured_callback_invocation(
                node,
                target_name="function",
                target_position=1,
                args_name="args",
                args_position=2,
                kwargs_name="kwargs",
                kwargs_position=3,
            )
            return invocation if invocation is not None else ([node], False)
        if qualified == "threading.Barrier":
            target = self._call_argument(node, "action", 1)
            return (
                ([node], False) if target is None else ([self._synthetic_call(target, [])], False)
            )
        if dispatch_kind in {"apply", "apply_async"}:
            invocation = self._configured_callback_invocation(
                node,
                target_name="func",
                target_position=0,
                args_name="args",
                args_position=1,
                kwargs_name="kwds",
                kwargs_position=2,
            )
            return invocation if invocation is not None else ([node], False)
        return None

    def _map_process_callable_invocations(
        self, node: ast.Call, dispatch_kind: str | None
    ) -> tuple[list[ast.Call], bool] | None:
        if dispatch_kind not in {
            "imap",
            "imap_unordered",
            "map",
            "map_async",
            "starmap",
            "starmap_async",
        }:
            return None
        target = self._call_argument(node, "func", 0) or self._call_argument(node, "fn", 0)
        iterable = self._call_argument(node, "iterable", 1)
        if target is None or iterable is None:
            return [node], True
        if dispatch_kind in {"starmap", "starmap_async"}:
            return self._static_process_invocation_result(
                target, self._static_starmap_invocations(target, iterable)
            )
        owner_factory = self._process_dispatch_owner_factory(node.func)
        iterable_expressions = (
            node.args[1:]
            if owner_factory
            in {
                "concurrent.futures.ProcessPoolExecutor",
                "concurrent.futures.ThreadPoolExecutor",
            }
            else [iterable]
        )
        return self._static_process_invocation_result(
            target, self._static_map_invocations(target, iterable_expressions)
        )

    def _dispatch_process_callable_invocations(
        self, node: ast.Call, dispatch_kind: str | None
    ) -> tuple[list[ast.Call], bool] | None:
        if dispatch_kind == "run_in_executor":
            target = self._call_argument(node, "func", 1)
            if target is None:
                return [node], True
            invocation = self._synthetic_call(target, node.args[2:])
            return [self._partial_process_invocation(invocation) or invocation], False
        callback_position = {
            "add_reader": 1,
            "add_writer": 1,
            "call_at": 1,
            "call_later": 1,
            "call_soon": 0,
            "call_soon_threadsafe": 0,
        }.get(dispatch_kind or "")
        if callback_position is not None:
            target = self._call_argument(node, "callback", callback_position)
            if target is None:
                return [node], True
            return [self._synthetic_call(target, node.args[callback_position + 1 :])], False
        map_invocations = self._map_process_callable_invocations(node, dispatch_kind)
        if map_invocations is not None:
            return map_invocations
        if dispatch_kind == "submit" and node.args:
            return [self._synthetic_call(node.args[0], node.args[1:], node.keywords)], False
        return None

    def _process_callable_invocations(self, node: ast.Call) -> tuple[list[ast.Call], bool]:
        qualified = self._qualified_name(node.func)
        dispatch_kind = self._process_dispatch_kind(node.func)
        for candidate in (
            self._basic_process_callable_invocations(node, qualified),
            self._configured_process_callable_invocations(node, qualified, dispatch_kind),
            self._dispatch_process_callable_invocations(node, dispatch_kind),
        ):
            if candidate is not None:
                return candidate
        return [node], False

    def _process_invocation_has_docker_payload(self, node: ast.Call) -> bool:
        direct_callable = self._direct_docker_cli_callable(node.func)
        wrapper_spec = self._wrapper_spec_for_callable(node.func)
        if direct_callable:
            wrapper_spec = self._direct_callable_wrapper_spec(node.func)
        if wrapper_spec is None:
            return any(
                self._process_payload_is_dangerous(argument) for argument in node.args
            ) or any(self._process_payload_is_dangerous(keyword.value) for keyword in node.keywords)
        payloads, dangerous_without_argument = self._wrapper_payload_arguments(node, wrapper_spec)
        return dangerous_without_argument or any(
            self._process_payload_is_dangerous(payload) for payload in payloads
        )

    def _resolved_process_value(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> ast.expr | None:
        resolved = self._resolved_value_expression(expression)
        if not isinstance(resolved, ast.Call):
            return resolved
        tokens = self._static_sequence_prefix(resolved)
        if tokens is not None:
            return ast.List(elts=[ast.Constant(value=token) for token in tokens], ctx=ast.Load())
        definition = self._function_definition_for_callable(resolved.func)
        if definition is None:
            return None
        visited = set() if seen is None else seen
        if id(definition) in visited:
            return None
        values = self._expanded_process_return_values(resolved, definition)
        if len(values) != 1:
            return None
        return self._resolved_process_value(values[0], visited | {id(definition)})

    def _process_payload_is_dangerous(
        self, expression: ast.expr, seen: set[int] | None = None
    ) -> bool:
        if self._docker_cli_expression(expression) or self._docker_executable_expression(
            expression
        ):
            return True
        resolved = self._resolved_value_expression(expression)
        if (
            self._static_sequence_prefix(resolved) is not None
            or self._static_string(resolved) is not None
        ):
            return False
        if not isinstance(resolved, ast.Call):
            return self._local_callable_boundary(resolved)
        definition = self._function_definition_for_callable(resolved.func)
        if definition is None:
            return True
        visited = set() if seen is None else seen
        if id(definition) in visited:
            return True
        values = self._expanded_process_return_values(resolved, definition)
        if not values:
            return True
        nested_seen = visited | {id(definition)}
        return any(self._process_payload_is_dangerous(value, nested_seen) for value in values)

    def _expanded_process_return_values(
        self,
        node: ast.Call,
        definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> list[ast.expr]:
        bindings = self._external_effect_call_bindings(node, definition)
        if isinstance(definition, ast.Lambda):
            return [_ExpressionSubstituter(bindings).visit(copy.deepcopy(definition.body))]
        assignments = self._owned_name_assignments(definition)
        local_names = self._owned_scope_names(definition)

        def expand(value: ast.expr, seen_names: set[str]) -> list[ast.expr]:
            if isinstance(value, ast.Name) and value.id in bindings:
                return [copy.deepcopy(bindings[value.id])]
            substituted = _ExpressionSubstituter(bindings).visit(copy.deepcopy(value))
            if not isinstance(substituted, ast.Name) or substituted.id not in local_names:
                return [substituted]
            if substituted.id in seen_names:
                return []
            candidates = assignments.get(substituted.id, ())
            return [
                expanded
                for candidate in candidates
                for expanded in expand(candidate, seen_names | {substituted.id})
            ]

        return [
            expanded
            for returned in self._reachable_owned_returns(definition)
            if returned.value is not None
            for expanded in expand(returned.value, set())
        ]

    @staticmethod
    def _record_owned_name_assignment(
        assignments: dict[str, list[ast.expr]], target: ast.expr, value: ast.expr
    ) -> None:
        if isinstance(target, ast.Name):
            assignments.setdefault(target.id, []).append(value)
            return
        if not (
            isinstance(target, (ast.List, ast.Tuple))
            and isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
        ):
            return
        for nested_target, nested_value in zip(target.elts, value.elts, strict=True):
            _DockerRunVisitor._record_owned_name_assignment(
                assignments, nested_target, nested_value
            )

    @staticmethod
    def _record_owned_assignment(assignments: dict[str, list[ast.expr]], current: ast.AST) -> None:
        if isinstance(current, ast.Assign):
            for target in current.targets:
                _DockerRunVisitor._record_owned_name_assignment(assignments, target, current.value)
        elif isinstance(current, ast.AnnAssign) and current.value is not None:
            _DockerRunVisitor._record_owned_name_assignment(
                assignments, current.target, current.value
            )
        elif isinstance(current, ast.NamedExpr):
            _DockerRunVisitor._record_owned_name_assignment(
                assignments, current.target, current.value
            )
        elif isinstance(current, ast.ImportFrom) and current.module == "builtins":
            for alias in current.names:
                if alias.name not in {"locals", "vars"}:
                    continue
                assignments.setdefault(alias.asname or alias.name, []).append(
                    ast.copy_location(
                        ast.Attribute(
                            value=ast.Name(id="builtins", ctx=ast.Load()),
                            attr=alias.name,
                            ctx=ast.Load(),
                        ),
                        current,
                    )
                )

    @staticmethod
    def _owned_name_assignments(
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> dict[str, list[ast.expr]]:
        assignments: dict[str, list[ast.expr]] = {}

        pending: list[ast.AST] = list(definition.body)
        while pending:
            current = pending.pop()
            if isinstance(
                current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            _DockerRunVisitor._record_owned_assignment(assignments, current)
            pending.extend(ast.iter_child_nodes(current))
        return assignments

    def _local_higher_order_invocations(
        self,
        node: ast.Call,
        seen: set[int],
    ) -> tuple[list[ast.Call], set[int]] | None:
        definition = self._function_definition_for_callable(node.func)
        if definition is None or id(definition) in seen:
            return None
        parameters = set(self._function_parameter_names(definition.args))
        bindings = self._external_effect_call_bindings(node, definition)
        owned_names = self._owned_scope_names(definition)
        owned_calls = self._owned_calls(definition)
        plain_locals_enabled = "locals" not in owned_names and self._qualified_name(
            ast.Name(id="locals", ctx=ast.Load())
        ) in {
            "builtins.locals",
            "locals",
        }
        plain_vars_enabled = "vars" not in owned_names and self._qualified_name(
            ast.Name(id="vars", ctx=ast.Load())
        ) in {"builtins.vars", "vars"}
        local_namespace_aliases = self._local_runtime_namespace_aliases(
            definition, owned_calls, owned_names
        )
        invocations: list[ast.Call] = []
        for call, _ in owned_calls:
            substituter = _HigherOrderSubstituter(
                bindings,
                plain_locals_enabled=plain_locals_enabled,
                plain_vars_enabled=plain_vars_enabled,
                local_namespace_aliases=local_namespace_aliases,
            )
            if not self._parameter_references(
                call, parameters
            ) and not substituter.references_runtime_local(call):
                continue
            substituted = substituter.visit(copy.deepcopy(call))
            if isinstance(substituted, ast.Call):
                invocations.append(substituted)
        return (invocations, seen | {id(definition)}) if invocations else None

    def _local_runtime_namespace_aliases(
        self,
        definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
        owned_calls: Sequence[tuple[ast.Call, bool]],
        owned_names: set[str],
    ) -> frozenset[str]:
        aliases = {
            syntactic
            for call, _ in owned_calls
            if not call.args
            and not call.keywords
            and (
                syntactic := self._without_terminal_dunder_calls(
                    self._syntactic_qualified_name(call.func)
                )
            )
            is not None
            and syntactic.split(".", 1)[0] not in owned_names
            and self._runtime_namespace_callable_name(call.func)
            in {"builtins.locals", "builtins.vars", "locals", "vars"}
        }
        if isinstance(definition, ast.Lambda):
            return frozenset(aliases)
        assignments = self._owned_name_assignments(definition)
        changed = True
        while changed:
            changed = False
            for name, values in assignments.items():
                if name in aliases:
                    continue
                if not any(
                    self._runtime_namespace_callable_name(value)
                    in {"builtins.locals", "builtins.vars", "locals", "vars"}
                    or self._without_terminal_dunder_calls(self._syntactic_qualified_name(value))
                    in aliases
                    for value in values
                ):
                    continue
                aliases.add(name)
                changed = True
        return frozenset(aliases)

    def _reject_synthesized_process_call(self, node: ast.Call) -> bool:
        resolved_callable = self._resolve_expression(node.func)
        roots = (node.func,) if resolved_callable is node.func else (node.func, resolved_callable)
        complex_provenance = any(
            isinstance(descendant, (ast.Call, ast.Subscript))
            for root in roots
            for descendant in ast.walk(root)
        )
        return (
            (complex_provenance and self._reject_opaque_process_call(node))
            or self._reject_callable_storage_constructor(node)
            or self._reject_local_or_dynamic_process_call(node)
            or self._reject_unsupported_sdk_call(node)
        )

    def _process_invocation_depth_exceeded(self, node: ast.Call, depth: int) -> bool:
        if depth < MAX_CALLABLE_PROVENANCE_DEPTH:
            return False
        issue = "process invocation expansion exceeds the supported depth"
        if not any(issue in error for error in self._errors):
            self._errors.append(f"{self._diagnostic_source}:{node.lineno}: {issue}")
        return True

    def _expanded_process_callable_invocations(
        self,
        node: ast.Call,
        seen: set[int] | None = None,
        *,
        synthesized: bool = False,
        depth: int = 0,
    ) -> tuple[list[ast.Call], bool]:
        if self._process_invocation_depth_exceeded(node, depth):
            return [], True
        invocations, opaque = self._process_callable_invocations(node)
        expanded: list[ast.Call] = []
        visited = set() if seen is None else seen
        for invocation in invocations:
            if self._docker_cli_callable(invocation.func):
                expanded.append(invocation)
                continue
            if (synthesized or invocation is not node) and self._reject_synthesized_process_call(
                invocation
            ):
                continue
            if invocation is not node:
                nested_expanded, nested_opaque = self._expanded_process_callable_invocations(
                    invocation, visited, synthesized=True, depth=depth + 1
                )
                expanded.extend(nested_expanded)
                opaque = opaque or nested_opaque
                continue
            local = self._local_higher_order_invocations(invocation, visited)
            if local is None:
                expanded.append(invocation)
                continue
            nested_invocations, nested_seen = local
            for nested in nested_invocations:
                nested_expanded, nested_opaque = self._expanded_process_callable_invocations(
                    nested, nested_seen, synthesized=True, depth=depth + 1
                )
                expanded.extend(nested_expanded)
                opaque = opaque or nested_opaque
        return expanded, opaque

    def _reject_docker_cli_call(self, node: ast.Call) -> bool:
        invocations, opaque = self._expanded_process_callable_invocations(node)
        process_invocations = [
            invocation for invocation in invocations if self._docker_cli_callable(invocation.func)
        ]
        if not process_invocations:
            return False
        has_docker_payload = opaque or any(
            self._process_invocation_has_docker_payload(invocation)
            for invocation in process_invocations
        )
        if not has_docker_payload:
            return False
        if (
            not opaque
            and process_invocations == [node]
            and self._is_static_compose_exec_invocation(node)
        ):
            return False
        self._errors.append(
            f"{self._diagnostic_source}:{node.lineno}: unsupported Docker CLI execution"
        )
        return True

    def _is_static_compose_exec_invocation(self, node: ast.Call) -> bool:
        if not (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.List)
        ):
            return False
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        if len(keywords) != len(node.keywords) or set(keywords) != {
            "input",
            "capture_output",
            "check",
            "text",
            "cwd",
            "env",
        }:
            return False
        if not (
            self._diagnostic_source == "scripts/rotate_codex_gateway_credentials.py"
            and isinstance(keywords["capture_output"], ast.Constant)
            and keywords["capture_output"].value is True
            and isinstance(keywords["check"], ast.Constant)
            and keywords["check"].value is False
            and isinstance(keywords["text"], ast.Constant)
            and keywords["text"].value is True
            and self._is_gateway_probe_input(keywords["input"])
            and isinstance(keywords["cwd"], ast.Attribute)
            and isinstance(keywords["cwd"].value, ast.Name)
            and keywords["cwd"].value.id == "self"
            and keywords["cwd"].attr == "brain_root"
            and self._is_static_compose_probe_environment_filter(keywords["env"])
        ):
            return False
        tokens = self._static_string_sequence_exact(node.args[0])
        if tokens is None or len(tokens) != 12:
            return False
        return (
            tuple(tokens[:11])
            == (
                "docker",
                "compose",
                "--project-name",
                "brain-v42",
                "-f",
                "docker-compose.yml",
                "exec",
                "-T",
                "brain-codex-gateway",
                "python",
                "-c",
            )
            and isinstance(node.args[0].elts[-1], ast.Name)
            and node.args[0].elts[-1].id == ("_GATEWAY_PROBE_SCRIPT")
        )

    @staticmethod
    def _is_static_compose_probe_environment_filter(expression: ast.expr) -> bool:
        """Accept only the probe's inline filtering of the child environment."""
        if not isinstance(expression, ast.DictComp) or len(expression.generators) != 1:
            return False
        generator = expression.generators[0]
        if not (
            generator.is_async == 0
            and isinstance(generator.target, ast.Tuple)
            and isinstance(generator.target.ctx, ast.Store)
            and len(generator.target.elts) == 2
            and all(
                isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store)
                for target in generator.target.elts
            )
        ):
            return False
        key_target, value_target = generator.target.elts
        assert isinstance(key_target, ast.Name)
        assert isinstance(value_target, ast.Name)
        if not (
            isinstance(expression.key, ast.Name)
            and isinstance(expression.key.ctx, ast.Load)
            and expression.key.id == key_target.id
            and isinstance(expression.value, ast.Name)
            and isinstance(expression.value.ctx, ast.Load)
            and expression.value.id == value_target.id
            and isinstance(generator.iter, ast.Call)
            and not generator.iter.args
            and not generator.iter.keywords
            and isinstance(generator.iter.func, ast.Attribute)
            and generator.iter.func.attr == "items"
            and isinstance(generator.iter.func.value, ast.Attribute)
            and generator.iter.func.value.attr == "environ"
            and isinstance(generator.iter.func.value.value, ast.Name)
            and generator.iter.func.value.value.id == "os"
            and len(generator.ifs) == 1
        ):
            return False
        condition = generator.ifs[0]
        return (
            isinstance(condition, ast.UnaryOp)
            and isinstance(condition.op, ast.Not)
            and isinstance(condition.operand, ast.Call)
            and not condition.operand.keywords
            and len(condition.operand.args) == 1
            and isinstance(condition.operand.args[0], ast.Constant)
            and condition.operand.args[0].value == "COMPOSE_"
            and isinstance(condition.operand.func, ast.Attribute)
            and condition.operand.func.attr == "startswith"
            and isinstance(condition.operand.func.value, ast.Name)
            and condition.operand.func.value.id == key_target.id
        )

    def _is_gateway_probe_input(self, expression: ast.expr) -> bool:
        if not (
            isinstance(expression, ast.Call)
            and self._qualified_name(expression.func) == "json.dumps"
            and len(expression.args) == 1
            and not expression.keywords
            and isinstance(expression.args[0], ast.Dict)
        ):
            return False
        payload = expression.args[0]
        if not all(
            isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Name)
            for key, value in zip(payload.keys, payload.values, strict=True)
        ):
            return False
        values_by_key = {
            key.value: value.id
            for key, value in zip(payload.keys, payload.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(value, ast.Name)
        }
        return len(payload.keys) == 2 and values_by_key == {"new": "new_token", "old": "old_token"}

    def _recognise_sdk_attribute(self, node: ast.Attribute) -> None:
        self._recognised_sdk_attributes.add(id(node))
        if isinstance(node.value, ast.Attribute):
            self._recognised_sdk_attributes.add(id(node.value))

    def _docker_client_owner(self, expression: ast.expr) -> bool:
        resolved = self._resolve_expression(expression)
        if isinstance(resolved, ast.Name):
            return (
                resolved.id == "client" or resolved.id.endswith("_client") or resolved.id == "sdk"
            )
        if isinstance(resolved, ast.Attribute):
            return resolved.attr == "client" or resolved.attr.endswith("_client")
        return (
            isinstance(resolved, ast.Call)
            and isinstance(resolved.func, ast.Attribute)
            and resolved.func.attr == "from_env"
            and isinstance(resolved.func.value, ast.Name)
            and resolved.func.value.id == "docker"
        )

    def _linked_docker_namespace(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr in {"api", "containers", "images"}
            and (node.attr == "containers" or self._docker_client_owner(node.value))
        )

    def _linked_getattr_construct(self, node: ast.Call) -> str | None:
        construct = _getattr_docker_construct(node)
        if construct is None or len(node.args) < 2:
            return None
        target, attribute = node.args[:2]
        if self._linked_docker_namespace(target):
            return construct
        if isinstance(attribute, ast.Constant) and attribute.value in {
            "api",
            "containers",
            "images",
        }:
            return construct
        return None

    def _docker_api_client_constructor(self, expression: ast.expr) -> bool:
        return self._qualified_name(expression) in {
            "docker.APIClient",
            "docker.api.APIClient",
            "docker.api.client.APIClient",
        }

    def _contains_dynamic_reflection(self, expression: ast.expr) -> bool:
        for descendant in ast.walk(expression):
            if isinstance(descendant, ast.Attribute) and descendant.attr == "__dict__":
                return True
            if not isinstance(descendant, ast.Call):
                continue
            if (
                self._getattr_callable(descendant.func)
                or self._attrgetter_callable(descendant.func)
                or self._vars_callable(descendant.func)
                or self._getattribute_callable(descendant.func)
            ):
                return True
        return False

    def _attrgetter_sdk_construct(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        if not (
            isinstance(resolved, ast.Call)
            and resolved.args
            and isinstance(resolved.func, ast.Call)
            and self._attrgetter_callable(resolved.func.func)
            and self._docker_client_owner(resolved.args[0])
        ):
            return None
        selectors = resolved.func.args
        if not selectors or resolved.func.keywords:
            return "<dynamic>"
        values = [self._resolve_expression(selector) for selector in selectors]
        if not all(
            isinstance(value, ast.Constant) and isinstance(value.value, str) for value in values
        ):
            return "<dynamic>"
        for value in values:
            assert isinstance(value, ast.Constant) and isinstance(value.value, str)
            if value.value.split(".", 1)[0] in {"api", "containers", "images", "services"}:
                return value.value
        return None

    def _reflected_sdk_namespace(self, expression: ast.expr) -> str | None:
        resolved = self._resolve_expression(expression)
        attrgetter_construct = self._attrgetter_sdk_construct(resolved)
        if attrgetter_construct is not None:
            return attrgetter_construct
        namespace = self._reflected_namespace_expression(resolved)
        if namespace is None:
            return "<dynamic>" if self._contains_dynamic_reflection(resolved) else None
        value = self._resolve_expression(namespace)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            return "<dynamic>"
        return value.value if value.value in {"api", "containers", "images", "services"} else None

    def _reflected_namespace_expression(self, resolved: ast.expr) -> ast.expr | None:
        if self._is_resolved_getattr_call(resolved) and len(resolved.args) >= 2:
            return resolved.args[1]
        if (
            isinstance(resolved, ast.Call)
            and self._getattribute_callable(resolved.func)
            and resolved.args
        ):
            return resolved.args[0]
        if (
            isinstance(resolved, ast.Call)
            and isinstance(resolved.func, ast.Attribute)
            and resolved.func.attr == "get"
            and isinstance(resolved.func.value, ast.Call)
            and self._vars_callable(resolved.func.value.func)
            and resolved.args
        ):
            return resolved.args[0]
        if not isinstance(resolved, ast.Subscript):
            return None
        mapping = resolved.value
        if isinstance(mapping, ast.Call) and self._vars_callable(mapping.func) and mapping.args:
            return resolved.slice
        if isinstance(mapping, ast.Attribute) and mapping.attr == "__dict__":
            return resolved.slice
        return None

    def _reject_unsupported_sdk_call(self, node: ast.Call) -> bool:
        if self._docker_api_client_constructor(node.func):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK APIClient"
            )
            return True
        if (construct := self._attrgetter_sdk_construct(node.func)) is not None:
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK attrgetter "
                f"construct {construct}"
            )
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"build", "create", "create_container", "get", "pull", "push", "run"}
            and (reflected_namespace := self._reflected_sdk_namespace(node.func.value)) is not None
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK reflected namespace "
                f"{reflected_namespace}"
            )
            return True
        obscured_namespace = _obscured_docker_namespace(node.func)
        if obscured_namespace is not None:
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK construct "
                f"{obscured_namespace}"
            )
            return True
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"api", "containers", "images"}
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK construct "
                f"{node.func.value.id}.{node.func.attr}"
            )
            return True
        unsupported_api = (
            _unsupported_docker_sdk_api(node.func) if isinstance(node.func, ast.Attribute) else None
        )
        if unsupported_api not in DOCKER_SDK_IMAGE_OPERATIONS and not (
            isinstance(node.func, ast.Attribute) and self._linked_docker_namespace(node.func.value)
        ):
            unsupported_api = None
        if unsupported_api is not None:
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK API {unsupported_api}"
            )
            return True
        if (
            _is_getattr_containers_run(node)
            and isinstance(node.args[0], ast.Attribute)
            and self._linked_docker_namespace(node.args[0])
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: getattr containers.run aliases are forbidden"
            )
            return True
        getattr_construct = self._linked_getattr_construct(node)
        if getattr_construct is None:
            return False
        self._errors.append(
            f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK construct "
            f"getattr {getattr_construct}"
        )
        return True

    def _docker_run_call_modes(self, node: ast.Call) -> tuple[bool, bool]:
        direct_containers_run = (
            _is_containers_run(node.func)
            and isinstance(node.func, ast.Attribute)
            and self._linked_docker_namespace(node.func.value)
        )
        threaded_containers_run = (
            _is_asyncio_to_thread(node.func)
            and bool(node.args)
            and _is_containers_run(node.args[0])
            and isinstance(node.args[0], ast.Attribute)
            and self._linked_docker_namespace(node.args[0].value)
        )
        return direct_containers_run, threaded_containers_run

    def _visit_call_operands(
        self, node: ast.Call, direct_containers_run: bool, threaded_containers_run: bool
    ) -> None:
        if direct_containers_run:
            assert isinstance(node.func, ast.Attribute)
            self._recognise_sdk_attribute(node.func)
        elif threaded_containers_run:
            assert isinstance(node.args[0], ast.Attribute)
            self._recognise_sdk_attribute(node.args[0])
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def _reject_opaque_process_call(self, node: ast.Call) -> bool:
        if self._opaque_process_module_reflection(node):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: opaque process module reflection is "
                "unsupported"
            )
            return True
        if (
            not self._docker_cli_callable(node.func)
            and not self._dynamic_process_import_source(node)
            and self._opaque_process_callable_provenance(node.func)
            and self._process_invocation_has_docker_payload(node)
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: opaque process callable provenance is "
                "unsupported"
            )
            return True
        resolved_call_result = self._resolved_value_expression(node)
        modeled_process_result = not isinstance(
            resolved_call_result, ast.Call
        ) and self._docker_cli_callable(resolved_call_result)
        if not modeled_process_result and self._opaque_process_callable_expression(node.func):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: opaque Docker CLI callable "
                "transformation is unsupported"
            )
            return True
        return False

    def _reject_stored_setattr_call(self, node: ast.Call) -> None:
        positional_arguments, _ = self._expanded_positional_arguments(node.args)
        if (
            self._qualified_name(node.func) in {"builtins.setattr", "setattr"}
            and len(positional_arguments) >= 3
            and self._stored_docker_cli_callable(positional_arguments[2])
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: storing a Docker CLI callable "
                "through setattr is unsupported"
            )

    @staticmethod
    def _owned_storage_values(
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[ast.expr]:
        values: list[ast.expr] = []
        pending: list[ast.AST] = list(definition.body)
        while pending:
            current = pending.pop()
            if isinstance(
                current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                continue
            if isinstance(current, ast.Assign) and any(
                not isinstance(target, ast.Name) for target in current.targets
            ):
                values.append(current.value)
            elif (
                isinstance(current, ast.AnnAssign)
                and not isinstance(current.target, ast.Name)
                and current.value is not None
            ):
                values.append(current.value)
            pending.extend(ast.iter_child_nodes(current))
        return values

    def _constructor_storage_value_has_callable(
        self,
        value: ast.expr,
        bindings: Mapping[str, ast.expr],
        assignments: Mapping[str, Sequence[ast.expr]],
        seen: set[str] | None = None,
    ) -> bool:
        substituted = _ExpressionSubstituter(bindings).visit(copy.deepcopy(value))
        if self._stored_docker_cli_callable(substituted):
            return True
        if not isinstance(substituted, ast.Name):
            return False
        visited = set() if seen is None else seen
        if substituted.id in visited:
            return False
        return any(
            self._constructor_storage_value_has_callable(
                candidate, bindings, assignments, visited | {substituted.id}
            )
            for candidate in assignments.get(substituted.id, ())
        )

    def _constructor_stores_docker_callable(
        self,
        node: ast.Call,
        definition: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        effective_call = ast.Call(
            func=node.func,
            args=[ast.Name(id="__instance__", ctx=ast.Load()), *node.args],
            keywords=list(node.keywords),
        )
        bindings = self._external_effect_call_bindings(effective_call, definition)
        assignments = self._owned_name_assignments(definition)
        if any(
            self._constructor_storage_value_has_callable(value, bindings, assignments)
            for value in self._owned_storage_values(definition)
        ):
            return True
        for call, _ in self._owned_calls(definition):
            substituted = _ExpressionSubstituter(bindings).visit(copy.deepcopy(call))
            if not isinstance(substituted, ast.Call):
                continue
            mutation = self._container_mutation(substituted)
            if mutation is not None and any(
                self._constructor_storage_value_has_callable(payload, bindings, assignments)
                for payload in mutation.payloads
            ):
                return True
        return False

    def _reject_callable_storage_constructor(self, node: ast.Call) -> bool:
        class_name = self._known_class_name(node.func)
        if class_name is not None:
            definition = self._class_method_definition(class_name, "__init__")
            stores_callable = definition is not None and self._constructor_stores_docker_callable(
                node, definition
            )
        else:
            qualified = self._without_terminal_dunder_calls(self._qualified_name(node.func))
            storage_constructors = {
                "builtins.dict",
                "builtins.dict.fromkeys",
                "builtins.frozenset",
                "builtins.list",
                "builtins.set",
                "builtins.tuple",
                "dict",
                "dict.fromkeys",
                "frozenset",
                "list",
                "set",
                "tuple",
            }
            plain_constructor = qualified.split(".", 1)[0] if qualified is not None else None
            builtin_constructor_available = plain_constructor not in {
                "dict",
                "frozenset",
                "list",
                "set",
                "tuple",
            } or self._plain_builtin_name_available(plain_constructor)
            constructor_is_storage = qualified in {
                "SimpleNamespace",
                "types.SimpleNamespace",
                *storage_constructors,
            } and (
                qualified not in storage_constructors
                or (
                    builtin_constructor_available
                    and self._function_definition_for_callable(node.func) is None
                )
            )
            stores_callable = constructor_is_storage and any(
                self._stored_docker_cli_callable(payload)
                for payload in [*node.args, *(keyword.value for keyword in node.keywords)]
            )
        if not stores_callable:
            return False
        self._errors.append(
            f"{self._diagnostic_source}:{node.lineno}: storing a Docker CLI callable through "
            "a constructor is unsupported"
        )
        return True

    def _reject_local_or_dynamic_process_call(self, node: ast.Call) -> bool:
        if self._local_callable_boundary(node.func) and any(
            self._docker_cli_expression(payload) or self._docker_executable_expression(payload)
            for payload in [*node.args, *(keyword.value for keyword in node.keywords)]
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: Docker CLI payload across a local "
                "Python import is unsupported"
            )
            return True
        if self._dynamic_python_callable(node.func):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported dynamic Python execution in "
                "Docker image gate"
            )
            return True
        return False

    def _record_docker_run_call(
        self, node: ast.Call, direct_containers_run: bool, threaded_containers_run: bool
    ) -> None:
        if direct_containers_run:
            image_expression = _call_image_expression(node, 0)
        elif threaded_containers_run:
            image_expression = _call_image_expression(node, 1)
        else:
            return
        if image_expression is None:
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: containers.run image must be explicit"
            )
        else:
            self._record_image(image_expression, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        direct_containers_run, threaded_containers_run = self._docker_run_call_modes(node)
        self._visit_call_operands(node, direct_containers_run, threaded_containers_run)
        if self._reject_opaque_process_call(node):
            return
        if self._reject_callable_storage_constructor(node):
            return
        self._apply_external_payload_effects(node)
        self._reject_stored_setattr_call(node)
        self._record_container_mutation(node)
        if self._reject_local_or_dynamic_process_call(node):
            return
        if self._reject_docker_cli_call(node) or self._reject_unsupported_sdk_call(node):
            return
        self._record_docker_run_call(node, direct_containers_run, threaded_containers_run)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 - ast API
        if _is_containers_get(node) and self._linked_docker_namespace(node.value):
            self._recognise_sdk_attribute(node)
        elif (
            _is_containers_run(node)
            and self._linked_docker_namespace(node.value)
            and id(node) not in self._recognised_sdk_attributes
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: containers.run alias or unsupported use is forbidden"
            )
        elif (
            node.attr in {"api", "containers", "images"}
            and self._linked_docker_namespace(node)
            and id(node) not in self._recognised_sdk_attributes
        ):
            self._errors.append(
                f"{self._diagnostic_source}:{node.lineno}: unsupported Docker SDK construct {node.attr}"
            )
        self.generic_visit(node)

    def _record_image(self, expression: ast.expr, line_number: int) -> None:
        value: object | None = expression.value if isinstance(expression, ast.Constant) else None
        if not isinstance(value, str):
            self._errors.append(
                f"{self._diagnostic_source}:{line_number}: containers.run requires a literal image expression"
            )
            return
        _add_use(
            self._uses,
            self._errors,
            value,
            self._source,
            (
                str(line_number)
                if self._diagnostic_prefix is None
                else f"{self._diagnostic_prefix}:{line_number}"
            ),
            "python-ast",
        )


def _env_shebang_executable(tokens: Sequence[str]) -> str | None:
    arguments = list(tokens)
    index = 0
    options_with_value = {"-a", "--argv0", "-C", "--chdir", "-u", "--unset"}
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            return arguments[index + 1] if index + 1 < len(arguments) else None
        if token in options_with_value:
            index += 2
            continue
        if token in {"-S", "--split-string"}:
            index += 1
            continue
        split_string = (
            token.removeprefix("--split-string=")
            if token.startswith("--split-string=")
            else token[2:]
            if token.startswith("-S") and len(token) > 2
            else None
        )
        if split_string is not None:
            try:
                split_arguments = shlex.split(split_string, comments=False, posix=True)
            except ValueError:
                return None
            arguments[index : index + 1] = split_arguments
            continue
        if token.startswith(("--argv0=", "--chdir=", "--unset=")):
            index += 1
            continue
        if token.startswith("-") or _assignment(token) is not None:
            index += 1
            continue
        return token
    return None


def _python_shebang(line: str) -> bool:
    if not line.startswith("#!"):
        return False
    try:
        tokens = shlex.split(line[2:].strip(), comments=False, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    if Path(tokens[0]).name != "env":
        return _is_python_executable(tokens[0])
    executable = _env_shebang_executable(tokens[1:])
    return executable is not None and _is_python_executable(executable)


def _python_source_candidate(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix == ".py":
        return True
    try:
        with path.open(encoding="utf-8", errors="replace") as source:
            first_line = source.readline()
    except OSError:
        return False
    return _python_shebang(first_line)


def _scan_supervisor(root: Path, uses: list[ImageUse], errors: list[str]) -> None:
    """Scan supported high-level docker-py image execution forms.

    The model intentionally covers direct ``containers.run`` calls and the
    repository's ``asyncio.to_thread`` callback form. Low-level Docker SDK APIs
    remain unsupported and must extend this gate before operational use.
    """
    candidates: set[Path] = set()
    for directory_name in ("scripts", "services"):
        directory = root / directory_name
        if directory.exists():
            candidates.update(
                path
                for path in directory.rglob("*")
                if not DISCOVERY_EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
                and _python_source_candidate(path)
            )
    local_modules = frozenset(
        ".".join(
            path.relative_to(root)
            .with_suffix("")
            .parts[: -1 if path.name == "__init__.py" else None]
        )
        for path in candidates
    )
    for path in sorted(candidates):
        source = _relative(root, path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=source)
        except SyntaxError as exc:
            errors.append(f"{source}:{exc.lineno}: Python parse error: {exc.msg}")
            continue
        _visit_python_tree(
            _DockerRunVisitor(source, uses, errors, local_modules=local_modules),
            tree,
            errors,
            source,
        )


def _discover(root: Path) -> tuple[list[ImageUse], list[str]]:
    uses: list[ImageUse] = []
    errors: list[str] = []
    operational_files = _operational_files(root)
    standard_compose_files = {path for path in operational_files if _is_compose_file(path)}
    standard_dockerfiles = {path for path in operational_files if _is_dockerfile(path)}
    build_dockerfiles: set[Path] = set()
    inventory = ShellInventory(root, set(standard_dockerfiles), set(), {})
    _scan_ci(root, uses, errors, inventory)
    _scan_workflows(root, uses, errors, inventory)
    _scan_compose(
        root,
        uses,
        errors,
        build_dockerfiles,
        candidates=standard_compose_files,
    )
    inventory.dockerfiles.update(build_dockerfiles)
    _scan_dockerfiles(
        root,
        uses,
        errors,
        build_dockerfiles,
        candidates=inventory.dockerfiles,
    )
    _scan_shell(root, uses, errors, inventory)
    extra_compose_files = inventory.compose_files - standard_compose_files
    if extra_compose_files:
        previous_build_dockerfiles = set(build_dockerfiles)
        _scan_compose(
            root,
            uses,
            errors,
            build_dockerfiles,
            candidates=extra_compose_files,
        )
        extra_dockerfiles = build_dockerfiles - previous_build_dockerfiles
        if extra_dockerfiles:
            _scan_dockerfiles(
                root,
                uses,
                errors,
                extra_dockerfiles,
                candidates=extra_dockerfiles,
            )
    _scan_supervisor(root, uses, errors)
    return uses, errors


def discover_images(root: Path) -> list[ImageUse]:
    """Discover all structurally supported image consumers under ``root``."""
    uses, errors = _discover(root.resolve())
    if errors:
        raise GateError("\n".join(errors))
    return uses


def _deduplicate(messages: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(messages))


def validate_repository(
    root: Path,
    lock_path: Path,
    *,
    discovery: tuple[list[ImageUse], list[str]] | None = None,
) -> list[str]:
    """Return every offline catalogue/discovery violation for a repository."""
    root = root.resolve()
    uses, discovery_errors = _discover(root) if discovery is None else discovery
    errors = list(discovery_errors)
    catalog: Catalog | None
    try:
        catalog = load_catalog(lock_path.resolve())
    except GateError as exc:
        catalog = None
        errors.append(str(exc))

    if catalog is None:
        for use in uses:
            if use.local:
                continue
            tag, digest, reference_error = _reference_parts(use.reference)
            if reference_error:
                errors.append(f"{use.source}:{use.location}: {reference_error}: {use.reference}")
            elif digest is None:
                errors.append(f"{use.source}:{use.location}: {tag} is not pinned by digest")
            else:
                errors.append(f"{use.source}:{use.location}: no catalog for {tag}@{digest}")
        return _deduplicate(errors)

    local_seen: set[tuple[str, str, str]] = set()
    for use in (item for item in uses if item.local):
        if use.build_error is not None:
            errors.append(
                f"{use.source}:{use.location}: local image {use.reference} build is invalid: "
                f"{use.build_error}"
            )
        local_entry = catalog.local_images.get(use.reference)
        if local_entry is None:
            errors.append(
                f"{use.source}:{use.location}: local image {use.reference} is not allowlisted"
            )
            continue
        local_seen.add((use.reference, use.source, use.service or ""))
        if use.source != local_entry.compose or use.service != local_entry.service:
            errors.append(
                f"{use.source}:{use.location}: local image {use.reference} does not match its allowlist"
            )
        if use.build_error is None and use.build_context != local_entry.context:
            errors.append(
                f"{use.source}:{use.location}: local image {use.reference} build context "
                f"must resolve to {local_entry.context!r}, got {use.build_context!r}"
            )
        if not use.pull_policy_build:
            errors.append(
                f"{use.source}:{use.location}: local image {use.reference} "
                "must set pull_policy exactly to 'build'"
            )
    for reference, local_entry in catalog.local_images.items():
        if (reference, local_entry.compose, local_entry.service) not in local_seen:
            errors.append(f"catalog local image {reference} is orphaned")

    entries_by_tag = {
        entry.tag: (identifier, entry) for identifier, entry in catalog.images.items()
    }
    actual_consumers: dict[str, set[str]] = {tag: set() for tag in entries_by_tag}
    for use in (item for item in uses if not item.local):
        tag, digest, reference_error = _reference_parts(use.reference)
        if reference_error:
            errors.append(f"{use.source}:{use.location}: {reference_error}: {use.reference}")
            continue
        assert tag is not None
        catalog_match = entries_by_tag.get(tag)
        if catalog_match is None:
            errors.append(f"{use.source}:{use.location}: image {use.reference} is not in catalog")
            continue
        _, image_entry = catalog_match
        actual_consumers[tag].add(use.source)
        if digest is None:
            errors.append(
                f"{use.source}:{use.location}: {tag} is not pinned; expected {image_entry.reference}"
            )
        elif digest != image_entry.digest:
            errors.append(
                f"{use.source}:{use.location}: divergent digest for {tag}; "
                f"expected {image_entry.digest}, got {digest}"
            )

    for tag, (identifier, image_entry) in entries_by_tag.items():
        actual = actual_consumers[tag]
        declared = set(image_entry.consumers)
        if not actual:
            errors.append(f"catalog entry {identifier} ({tag}) is orphaned")
        for source in sorted(declared - actual):
            errors.append(f"catalog consumer {source} for {tag} was not discovered")
        for source in sorted(actual - declared):
            errors.append(
                f"discovered consumer {source} for {tag} is missing from catalog metadata"
            )
    return _deduplicate(errors)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", type=Path, default=default_root)
    parser.add_argument("--lock", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    lock_path = args.lock.resolve() if args.lock else root / "config/container-images.lock.yml"
    discovery = _discover(root)
    errors = validate_repository(root, lock_path, discovery=discovery)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    uses, _ = discovery
    print(f"container image pins: OK ({len(uses)} consumers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
