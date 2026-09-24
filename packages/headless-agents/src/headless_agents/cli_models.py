"""Which model each ``ha run`` link gets.

The rails never pick a model for the caller -- claude, codex and opencode
refuse an empty one (opencode's own default can be a contributor model its
vendor trains on), only agy chooses safely. The e2e run of 2026-09-24 found
the CLI handing them ``-m ""`` whenever ``-m`` was omitted, and one ``-m``
shared by every link of a chain -- which made ``--chain codex,claude``
unusable, since no model name is valid on both rails.

A link's model, first match wins:

1. its own, in the chain: ``--chain codex:gpt-6-luna,claude:sonnet`` (the
   model is everything after the FIRST colon: ``openrouter:meta/llama:free``);
2. ``-m MODEL``;
3. the role's ``model`` (``roles.toml``, spec 0.5.0 §3.1);
4. the operator's declared default, ``$XDG_CONFIG_HOME/ha/models.toml``
   (default ``~/.config/ha/models.toml``): one ``provider = "model"`` line
   per provider, no model name ever hard-coded in this package;
5. none: refused before anything runs (exit ``2``), except for agy.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from .config_paths import ConfigPathError, config_dir, config_file
from .registry import PROVIDER_NAMES

#: The rails that choose a safe model of their own when given none.
MODEL_OPTIONAL: Final = frozenset({"agy"})
MODELS_FILE_NAME: Final = "models.toml"


class ModelsError(ValueError):
    """A link or the models file cannot be used; the message says why."""


def default_models_path(environ: Mapping[str, str], *, home: Path) -> Path:
    """Where the models file is expected (spec 0.5.0 §3.3: absolute XDG only)."""
    return config_dir(environ, home=home) / MODELS_FILE_NAME


def load_models(path: Path) -> dict[str, str]:
    """The declared defaults, ``{}`` when the file does not exist."""
    try:
        with path.open("rb") as stream:
            table = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ModelsError(f"{path}: {exc}") from None
    except RecursionError:
        # tomllib recurses per nesting level: a value nested hundreds deep is
        # a broken file, not a crash (independent review of PR #198, P2).
        raise ModelsError(f"{path}: nested too deeply to be a models file") from None
    models: dict[str, str] = {}
    for name, model in table.items():
        if name not in PROVIDER_NAMES:
            raise ModelsError(
                f"{path}: unknown provider {name!r}; valid names: {', '.join(PROVIDER_NAMES)}"
            )
        if not isinstance(model, str) or not model.strip():
            raise ModelsError(f"{path}: {name} must be a non-empty model name string")
        models[name] = model.strip()
    return models


def parse_chain(chain: str) -> tuple[tuple[str, str], ...]:
    """``--chain`` as ``(provider, model)`` pairs, model ``""`` when unnamed."""
    links: list[tuple[str, str]] = []
    for entry in chain.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, model = entry.partition(":")
        links.append((name.strip(), model.strip()))
    seen: set[str] = set()
    for name, _ in links:
        if name in seen:
            # Links are keyed by provider (their run directory, their result):
            # the same rail twice is a different chain, not a retry.
            raise ModelsError(f"{name} appears more than once in --chain")
        seen.add(name)
    return tuple(links)


def resolve_models(
    links: tuple[tuple[str, str], ...],
    *,
    default: str,
    declared: Mapping[str, str],
    declared_path: Path,
    role_model: str = "",
) -> dict[str, str]:
    """Each link's model by the precedence above; refuses a link left without one."""
    models: dict[str, str] = {}
    for name, own in links:
        model = own or default.strip() or role_model.strip() or declared.get(name, "")
        if not model and name not in MODEL_OPTIONAL:
            raise ModelsError(
                f"{name} needs a model: pass -m MODEL, name it in the chain as "
                f'{name}:MODEL, or declare it in {declared_path} ({name} = "MODEL")'
            )
        models[name] = model
    return models


def models_for(
    links: tuple[tuple[str, str], ...],
    *,
    default: str,
    environ: Mapping[str, str],
    home: Path,
    role_model: str = "",
) -> dict[str, str]:
    """The model each link gets; the caller validated the links first."""
    path = default_models_path(environ, home=home)
    # Read only when some link is left without a model: a broken file must not
    # fail a run that never needed it.
    needs_file = not default.strip() and not role_model.strip() and any(not own for _, own in links)
    declared: dict[str, str] = {}
    if needs_file:
        try:
            found = config_file(MODELS_FILE_NAME, environ, home=home)
        except ConfigPathError as exc:
            raise ModelsError(str(exc)) from None
        declared = load_models(found) if found is not None else {}
    return resolve_models(
        links, default=default, role_model=role_model, declared=declared, declared_path=path
    )


__all__ = [
    "MODEL_OPTIONAL",
    "ModelsError",
    "default_models_path",
    "load_models",
    "models_for",
    "parse_chain",
    "resolve_models",
]
