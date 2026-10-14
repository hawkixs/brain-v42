"""Expose claim domains separately from measurements of supplied snapshots."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from brain_v42.knowledge.ranks import RANKS

REGISTRY_VERSION = "v1"


@dataclass(frozen=True)
class FactInputs:
    files: Mapping[str, str]
    tree: frozenset[str]
    brain_facts: Mapping[str, str]
    decisions: Mapping[str, str]


@dataclass(frozen=True)
class FactDefinition:
    key: str
    domain: frozenset[str] | None
    source_kind: Literal["measured", "config", "decision"]
    source: str
    measure: Callable[[FactInputs], str | None]

    @property
    def source_rank(self) -> int:
        return RANKS[self.source_kind]


def _scalar(text: str, key: str) -> str | None:
    values = set()
    for match in re.finditer(rf"(?m)^\s*(?:-\s*)?{re.escape(key)}\s*[:=]\s*(.*?)\s*$", text):
        value = re.split(r"\s+#", match[1], maxsplit=1)[0].strip().strip("\"'")
        if not value or any(marker in value for marker in ("$", "*", "{", "}")):
            return None
        values.add(value)
    return values.pop() if len(values) == 1 else None


def _block(text: str, key: str) -> str:
    """Limit a measurement to its named YAML block, excluding sibling services."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^\s*{re.escape(key)}:\s*", line):
            indent = len(line) - len(line.lstrip())
            end = index + 1
            while end < len(lines):
                following = lines[end]
                if following.strip() and not following.lstrip().startswith("#"):
                    if len(following) - len(following.lstrip()) <= indent:
                        break
                end += 1
            return "\n".join(lines[index:end])
    return ""


def _compose_value(inputs: FactInputs, key: str, shared: str) -> str | None:
    """Read explicit MCP overrides before the repository's shared anchor block.

    Only literal scalars are measurable; environment substitutions need I/O and
    therefore remain unknown in this pure snapshot parser.
    """
    text = inputs.files.get("deploy/compose.yaml", "")
    services = _block(text, "services")
    service = _block(services, "mcp")
    if services and not service:
        return None  # a services file without mcp: never read a sibling's value
    environment = _block(service, "environment")
    if re.search(rf"(?m)^\s*(?:-\s*)?{re.escape(key)}\s*[:=]", environment):
        return _scalar(environment, key)
    block = _block(text, shared)
    if service and not block:
        return None
    if block and environment:
        anchor = re.search(r"&([\w-]+)", block.splitlines()[0])
        if anchor is None or not re.search(rf"\*{re.escape(anchor[1])}\b", environment):
            return None
    return _scalar(block or text, key)


def _host(inputs: FactInputs) -> str | None:
    text = inputs.files.get("rail.yaml", "")
    return _scalar(_block(text, "deploy") or text, "site")


def _embedding(inputs: FactInputs) -> str | None:
    backend = _compose_value(inputs, "EMBEDDING_BACKEND", "x-embedding-env")
    if backend in {"mistral-api", "local-gpu-shim", "dev-pc-supervisor", "pytorch-service"}:
        return backend
    url = _compose_value(inputs, "EMBEDDING_SERVICE_URL", "x-embedding-env")
    if url is None:
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if host == "api.mistral.ai":
        return "mistral-api"
    if host in {"localhost", "127.0.0.1", "embedding-shim", "brain_v42_embedding_shim"}:
        return "local-gpu-shim"
    if host == "dev-pc-supervisor":
        return "dev-pc-supervisor"
    if host in {"embedding", "brain_v42_embedding", "pytorch-service"}:
        return "pytorch-service"
    return None


def _workflow(path: str) -> bool:
    return (
        path.startswith(".github/workflows/")
        and path.endswith((".yml", ".yaml"))
        and "/" not in path.removeprefix(".github/workflows/")
    )


def _ci_system(inputs: FactInputs) -> str | None:
    github = any(_workflow(path) for path in inputs.tree)
    gitlab = ".gitlab-ci.yml" in inputs.tree
    if github and not gitlab:
        return "github-actions"
    if gitlab and not github:
        return "gitlab-ci"
    return None


def _runner(inputs: FactInputs) -> str | None:
    runners: list[str] = []
    for path, text in inputs.files.items():
        if not _workflow(path):
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^\s*runs-on:\s*(.*?)\s*$", line)
            if match:
                value = match[1] or _block("\n".join(lines[index:]), "runs-on").partition("\n")[2]
                value = "\n".join(
                    re.split(r"\s+#", part, maxsplit=1)[0] for part in value.splitlines()
                )
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    labels = value[1:-1].split(",")
                elif not match[1]:
                    labels = [
                        part.strip().removeprefix("-").strip()
                        for part in value.splitlines()
                        if part.strip()
                    ]
                else:
                    labels = [value]
                runners.extend(label.strip().strip("\"'") for label in (labels or [""]))
    if "self-hosted" in runners:
        return "self-hosted"
    if any(_workflow(path) and path not in inputs.files for path in inputs.tree):
        return None
    if runners and all(
        re.fullmatch(r"(?:ubuntu|windows|macos)-[\w.-]+", value) for value in runners
    ):
        return "hosted"
    return None


def _graph(inputs: FactInputs) -> str | None:
    flag = _compose_value(inputs, "GRAPH_PROJECTOR_ENABLED", "x-graph-env")
    return {"true": "enabled", "false": "disabled"}.get(flag or "")


def _rerank(inputs: FactInputs) -> str | None:
    backend = _compose_value(inputs, "BRAIN_RERANK_BACKEND", "x-rerank-env")
    return "local-onnx" if backend == "shim" else backend


REGISTRY: Mapping[str, FactDefinition] = MappingProxyType(
    {
        "production.host": FactDefinition(
            "production.host",
            frozenset({"red-base", "pc-server", "dev-pc"}),
            "config",
            "rail.yaml site",
            _host,
        ),
        "embedding.provider": FactDefinition(
            "embedding.provider",
            frozenset({"mistral-api", "local-gpu-shim", "dev-pc-supervisor", "pytorch-service"}),
            "config",
            "deploy/compose.yaml EMBEDDING_BACKEND / EMBEDDING_SERVICE_URL",
            _embedding,
        ),
        "rerank.provider": FactDefinition(
            "rerank.provider",
            frozenset({"voyage", "local-onnx", "local-gpu", "none"}),
            "config",
            "deploy/compose.yaml BRAIN_RERANK_BACKEND",
            _rerank,
        ),
        "ci.system": FactDefinition(
            "ci.system",
            frozenset({"github-actions", "gitlab-ci"}),
            "config",
            "origin/main workflow tree",
            _ci_system,
        ),
        "ci.runner": FactDefinition(
            "ci.runner",
            frozenset({"hosted", "self-hosted"}),
            "config",
            ".github/workflows runs-on",
            _runner,
        ),
        "graph.neo4j": FactDefinition(
            "graph.neo4j",
            frozenset({"enabled", "disabled"}),
            "config",
            "deploy/compose.yaml GRAPH_PROJECTOR_ENABLED",
            _graph,
        ),
        "release.live_sha": FactDefinition(
            "release.live_sha",
            None,
            "measured",
            "brain fact live_release_sha",
            lambda inputs: inputs.brain_facts.get("live_release_sha"),
        ),
        "schema.alembic_head": FactDefinition(
            "schema.alembic_head",
            None,
            "measured",
            "brain fact alembic_head",
            lambda inputs: inputs.brain_facts.get("alembic_head"),
        ),
        "dream.nightly": FactDefinition(
            "dream.nightly",
            frozenset({"running", "suspended"}),
            "decision",
            "operator decision dream",
            lambda inputs: inputs.decisions.get("dream"),
        ),
    }
)


def measure_all(inputs: FactInputs) -> dict[str, str | None]:
    """Remeasure every key; missing evidence must not retain a previous value."""
    values = {}
    for key, definition in REGISTRY.items():
        value = definition.measure(inputs)
        values[key] = value if definition.domain is None or value in definition.domain else None
    return values


def prompt_sheet() -> str:
    """Keep current values out of the extraction prompt to avoid answer leakage."""
    return "\n".join(
        f"{key}: {{{', '.join(sorted(definition.domain))}}}"
        if definition.domain is not None
        else f"{key}: open string"
        for key, definition in REGISTRY.items()
    )
