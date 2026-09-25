"""Where an HTTP preset finds its API key (decision 0612592e, ticket 4e16c0f4).

The process environment keeps priority. Otherwise ``keys.toml``, read from the
operator's configuration directory only (:mod:`headless_agents.config_paths`, spec
0.5.0 §3.3), names per preset the ``.env`` file that carries its key:

.. code-block:: toml

    openrouter = "~/.config/red/openrouter.env"
    mistral    = "~/.config/red/mistral.env"

From that file ha reads the preset's own variable only, and only when the file is
the user's and readable by no one else (``0600``): a key file others can read is a
leaked key, and ha refuses to use it rather than spread it. Before this, every
session sourced the file by hand, which exported the key to every process of the
shell.

The value leaves this module only towards the HTTP request; no message, repr or
log line carries it. ``HOME`` comes from the caller's environment and is never
guessed: without it, no file is read.
"""

from __future__ import annotations

import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .config_paths import ConfigPathError, config_file
from .providers.openai_compat import PRESETS

KEYS_FILE_NAME: Final = "keys.toml"


class KeysError(ValueError):
    """``keys.toml`` or a key file it names cannot be used; the message says which and why."""


@dataclass(frozen=True)
class PresetKey:
    #: The key itself: never printed, never in a repr.
    value: str = field(repr=False)
    #: ``environment``, or the path of the file it was read from.
    source: str


def _declared(home: Path, environ: Mapping[str, str]) -> dict[str, Path]:
    """Every preset ``keys.toml`` declares, with its key file's path; ``{}`` without a file."""
    try:
        path = config_file(KEYS_FILE_NAME, environ, home=home)
    except ConfigPathError as exc:
        raise KeysError(str(exc)) from None
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise KeysError(f"{path}: {exc}") from None
    declared: dict[str, Path] = {}
    for name, value in document.items():
        if name not in PRESETS:
            raise KeysError(
                f"{path}: [{name}] is not an HTTP preset; valid names: {', '.join(PRESETS)}"
            )
        if not isinstance(value, str) or not value:
            raise KeysError(f"{path}: [{name}] must be a path to a .env file")
        if value.startswith("~/"):
            declared[name] = home / value[2:]
        elif Path(value).is_absolute():
            declared[name] = Path(value)
        else:
            raise KeysError(f"{path}: [{name}] must be absolute or start with ~/, not {value!r}")
    return declared


def _read_variable(path: Path, variable: str) -> str:
    """``variable`` from the ``.env`` file ``path``, after checking who can read the file."""
    try:
        info = path.stat()
    except OSError as exc:
        raise KeysError(f"{path}: {exc.strerror or exc}") from None
    if not stat.S_ISREG(info.st_mode):
        raise KeysError(f"{path}: not a regular file")
    if info.st_uid != os.getuid():
        raise KeysError(f"{path}: not owned by the user running ha")
    if info.st_mode & 0o077:
        raise KeysError(
            f"{path}: mode {stat.S_IMODE(info.st_mode):04o}; a key file must be 0600 "
            "(readable by you only)"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KeysError(f"{path}: unreadable ({type(exc).__name__})") from None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != variable:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            return value
    raise KeysError(f"{path}: does not define {variable}")


def preset_key(name: str, environ: Mapping[str, str]) -> PresetKey | None:
    """The key of HTTP preset ``name``: the environment's, else the file ``keys.toml``
    declares for it; ``None`` when neither has one. :class:`KeysError` when the
    declaration or the file cannot be used -- a broken declaration is never a silent
    "no key"."""
    preset = PRESETS.get(name)
    if preset is None:
        raise ValueError(f"unknown HTTP preset {name!r}; valid names: {', '.join(PRESETS)}")
    value = environ.get(preset.key_env)
    if value:
        return PresetKey(value=value, source="environment")
    home = environ.get("HOME")
    if not home:
        return None
    path = _declared(Path(home), environ).get(name)
    if path is None:
        return None
    return PresetKey(value=_read_variable(path, preset.key_env), source=str(path))


__all__ = ["KEYS_FILE_NAME", "KeysError", "PresetKey", "preset_key"]
