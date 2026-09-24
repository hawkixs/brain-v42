"""Roles: named executors with optional instructions (spec 0.5.0 §3.1).

One TOML table per role in ``roles.toml``, read from the operator's
configuration directory only (:mod:`headless_agents.config_paths`, §3.3):

.. code-block:: toml

    [implementer]
    chain   = ["opencode:opencode-go/deepseek-v4.1-flash", "codex:gpt-6-luna"]
    write   = true
    timeout = 4800

    [reviewer-codex]
    provider = "codex"
    effort   = "high"
    context  = "full"
    instructions = "Review the change you are given."

Every provider name is also an implicit role with every default and no
instructions, so ``ha run codex "..."`` needs no declaration.

Validation happens before anything runs, and every refusal names the file,
the entry and the rule. A role grants nothing a workflow can widen: ``write``,
``shell`` and ``mcp`` are the operator's declaration, checked by the shapes
(§3.3). The collision of a role name with a workflow name is checked where
``workflows.toml`` is read (lot 3).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .context import ContextLevel
from .providers.codex import REASONING_EFFORTS
from .providers.openai_compat import GENERIC_NAME
from .registry import HTTP_PROVIDER_NAMES, PROVIDER_NAMES

NAME_PATTERN: Final = re.compile(r"[a-z][a-z0-9-]{0,63}")
CONTEXT_LEVELS: Final[tuple[ContextLevel, ...]] = ("full", "global", "none")
DEFAULT_EFFORT: Final = "medium"
DEFAULT_TIMEOUT_SECONDS: Final = 300.0

_FIELDS: Final = frozenset(
    {
        "provider",
        "chain",
        "model",
        "effort",
        "timeout",
        "context",
        "context_parents",
        "mcp",
        "write",
        "shell",
        "base_url",
        "key_env",
        "instructions",
    }
)


class RolesError(ValueError):
    """``roles.toml`` or one of its roles cannot be used; the message says where and why."""


@dataclass(frozen=True)
class Link:
    """One link of a role: a provider and the model it names (``""`` when none)."""

    provider: str
    model: str


@dataclass(frozen=True)
class Role:
    name: str
    links: tuple[Link, ...]
    model: str
    effort: str
    timeout: float
    context: ContextLevel
    context_parents: bool
    mcp: str | None
    write: bool
    shell: bool
    base_url: str | None
    key_env: str | None
    instructions: str | None
    implicit: bool = False

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(link.provider for link in self.links)


def implicit_role(provider: str) -> Role:
    """The role every provider name is: one link, every default, no instructions."""
    return Role(
        name=provider,
        links=(Link(provider, ""),),
        model="",
        effort=DEFAULT_EFFORT,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        context="global",
        context_parents=False,
        mcp=None,
        write=False,
        shell=False,
        base_url=None,
        key_env=None,
        instructions=None,
        implicit=True,
    )


class _Entry:
    """Refusals for one table, each naming the file, the entry and the rule."""

    def __init__(self, path: Path, name: str) -> None:
        self.path = path
        self.name = name

    def refuse(self, rule: str) -> RolesError:
        return RolesError(f"{self.path}: [{self.name}] {rule}")

    def string(self, table: Mapping[str, object], key: str, rule: str) -> str | None:
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise self.refuse(rule)
        return value

    def boolean(self, table: Mapping[str, object], key: str) -> bool:
        value = table.get(key, False)
        if not isinstance(value, bool):
            raise self.refuse(f"{key} must be a boolean")
        return value


def _links(entry: _Entry, table: Mapping[str, object]) -> tuple[Link, ...]:
    provider = entry.string(table, "provider", "provider must be a string")
    chain = table.get("chain")
    if chain is not None and (
        not isinstance(chain, list) or not all(isinstance(item, str) for item in chain)
    ):
        raise entry.refuse("chain must be a list of strings")
    if (provider is None) == (chain is None):
        raise entry.refuse("exactly one of provider and chain")
    if provider is not None:
        links: tuple[Link, ...] = (Link(provider, ""),)
    else:
        entries = cast("list[str]", chain)
        if not entries:
            raise entry.refuse("chain must name at least one provider")
        pairs = [item.strip().partition(":") for item in entries]
        links = tuple(Link(name.strip(), model.strip()) for name, _, model in pairs)
    seen: set[str] = set()
    for link in links:
        if link.provider not in PROVIDER_NAMES:
            raise entry.refuse(
                f"unknown provider {link.provider!r}; valid names: {', '.join(PROVIDER_NAMES)}"
            )
        if link.provider in seen:
            raise entry.refuse(f"{link.provider} appears twice in chain")
        seen.add(link.provider)
    return links


def _role(path: Path, name: str, table: object, mcp_profiles: Mapping[str, object]) -> Role:
    entry = _Entry(path, name)
    if not isinstance(table, dict):
        raise entry.refuse("a role must be a table")
    if not NAME_PATTERN.fullmatch(name):
        raise entry.refuse(
            "invalid role name: lowercase letters, digits and '-', starting with a letter, "
            "at most 64 characters"
        )
    if name in PROVIDER_NAMES:
        raise entry.refuse("collides with a provider name; a provider is already a role")
    unknown = sorted(table.keys() - _FIELDS)
    if unknown:
        raise entry.refuse(f"unknown field {unknown[0]!r}")

    links = _links(entry, table)
    is_chain = "chain" in table

    model = table.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise entry.refuse("model must be a non-empty string")
    if model is not None and is_chain:
        raise entry.refuse("model is refused on a chain role: each link names its own")

    effort = table.get("effort", DEFAULT_EFFORT)
    if not isinstance(effort, str) or not effort.strip():
        raise entry.refuse("effort must be a string")
    if any(link.provider == "codex" for link in links) and effort not in REASONING_EFFORTS:
        raise entry.refuse(
            f"effort {effort!r} is not a codex effort; valid: {', '.join(sorted(REASONING_EFFORTS))}"
        )

    timeout = table.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise entry.refuse("timeout must be a positive number of seconds")

    context_parents = entry.boolean(table, "context_parents")
    mcp = table.get("mcp")
    if mcp is not None and not isinstance(mcp, str):
        raise entry.refuse("mcp must be a profile name")
    write = entry.boolean(table, "write")
    shell = entry.boolean(table, "shell")
    base_url = entry.string(table, "base_url", "base_url must be a string")
    key_env = entry.string(table, "key_env", "key_env must be a string")
    instructions = entry.string(table, "instructions", "instructions must be a string")

    context = table.get("context", "full" if write else "global")
    if context not in CONTEXT_LEVELS:
        raise entry.refuse("context must be one of full, global, none")

    if shell and not write:
        raise entry.refuse("shell requires write: a shell can write what read tools cannot")
    http = [link.provider for link in links if link.provider in HTTP_PROVIDER_NAMES]
    if http and (write or shell):
        raise entry.refuse(f"write needs a CLI rail; {', '.join(http)} has no tool to edit with")
    if http and mcp is not None:
        raise entry.refuse(f"mcp needs a CLI rail; {', '.join(http)} has no tools")
    if mcp is not None and mcp not in mcp_profiles:
        raise entry.refuse(f"mcp profile {mcp!r} is not in mcp.toml")
    generic = any(link.provider == GENERIC_NAME for link in links)
    if generic and (base_url is None or key_env is None):
        raise entry.refuse("openai-compat needs base_url and key_env")
    if not generic and (base_url is not None or key_env is not None):
        raise entry.refuse("base_url and key_env apply to openai-compat only")

    return Role(
        name=name,
        links=links,
        model=cast("str", model or ""),
        effort=effort,
        timeout=float(timeout),
        context=cast("ContextLevel", context),
        context_parents=context_parents,
        mcp=mcp,
        write=write,
        shell=shell,
        base_url=base_url,
        key_env=key_env,
        instructions=instructions,
    )


def load_roles(path: Path | None, *, mcp_profiles: Mapping[str, object]) -> dict[str, Role]:
    """Every role declared in ``path``, validated; ``{}`` when there is no file.

    ``mcp_profiles`` names the profiles ``mcp.toml`` declares, so a role naming
    one that does not exist is refused here, before anything runs.
    """
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RolesError(f"{path}: {exc}") from None
    except RecursionError:
        # tomllib recurses per nesting level: a value nested hundreds deep is a
        # broken file, not a crash (as cli_models.load_models).
        raise RolesError(f"{path}: nested too deeply to be a roles file") from None
    return {name: _role(path, name, table, mcp_profiles) for name, table in document.items()}


def resolve_role(name: str, declared: Mapping[str, Role]) -> Role:
    """A declared role, else the implicit role of a provider; refused otherwise."""
    if name in declared:
        return declared[name]
    if name in PROVIDER_NAMES:
        return implicit_role(name)
    known = sorted({*declared, *PROVIDER_NAMES})
    raise RolesError(f"unknown target {name!r}; roles and providers: {', '.join(known)}")


__all__ = [
    "CONTEXT_LEVELS",
    "NAME_PATTERN",
    "Link",
    "Role",
    "RolesError",
    "implicit_role",
    "load_roles",
    "resolve_role",
]
