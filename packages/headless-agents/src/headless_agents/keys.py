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

import errno
import grp
import os
import pwd
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .config_paths import ConfigPathError, config_dir, config_file
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


def _open_checked(path: Path, *, what: str) -> tuple[int, os.stat_result]:
    """Look up a name once and check its descriptor; a FIFO must never block ha."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KeysError(f"{what} {path}: a symbolic link") from None
        raise KeysError(f"{what} {path}: {exc.strerror or type(exc).__name__}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise KeysError(f"{what} {path}: not a regular file")
        if info.st_uid != os.getuid():
            raise KeysError(f"{what} {path}: not owned by the user running ha")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


def _private_group(gid: int) -> bool:
    """Primary members are absent from gr_mem, so check the account database too."""
    try:
        user = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(gid)
        accounts = pwd.getpwall()
    except (KeyError, OSError):
        return False
    return (
        group.gr_name == user.pw_name
        and all(member == user.pw_name for member in group.gr_mem)
        and all(account.pw_name == user.pw_name or account.pw_gid != gid for account in accounts)
    )


def _declared(home: Path, environ: Mapping[str, str]) -> dict[str, Path]:
    """Every preset ``keys.toml`` declares, with its key file's path; ``{}`` without a file."""
    try:
        checked = config_file(KEYS_FILE_NAME, environ, home=home)
    except ConfigPathError as exc:
        raise KeysError(str(exc)) from None
    if checked is None:
        return {}
    # Keep config_file's boundary check but open the original name to reject a link.
    path = config_dir(environ, home=home) / KEYS_FILE_NAME
    fd, info = _open_checked(path, what="keys.toml")
    # keys.toml holds paths, never a key: reading it reveals nothing, but whoever can
    # EDIT it chooses which file ha reads a key from (codex review of #215).
    try:
        if info.st_mode & 0o002 or (info.st_mode & 0o020 and not _private_group(info.st_gid)):
            raise KeysError(
                f"{path}: mode {stat.S_IMODE(info.st_mode):04o} is writable by others; "
                "only you may edit the file that says where your keys are"
            )
        stream = os.fdopen(fd, "rb")
        fd = -1
        with stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise KeysError(f"{path}: {exc}") from None
    finally:
        if fd >= 0:
            os.close(fd)
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
    """``variable`` from the ``.env`` file ``path``, after checking who can read the file.

    The file is opened without following a link and checked through the descriptor
    it was read from: a link would be judged by its target, and whoever controls the
    link's directory could swap one between the check and the read (codex review of
    #215).
    """
    fd, info = _open_checked(path, what="key file")
    with os.fdopen(fd, "rb") as stream:
        if info.st_mode & 0o077:
            raise KeysError(
                f"{path}: mode {stat.S_IMODE(info.st_mode):04o}; a key file must be 0600 "
                "(readable by you only)"
            )
        try:
            text = stream.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise KeysError(f"{path}: unreadable ({type(exc).__name__})") from None
    definitions = [
        (number, value)
        for number, raw in enumerate(text.splitlines(), 1)
        if (value := _parse_definition(raw, variable, path, number)) is not None
    ]
    if len(definitions) > 1:
        lines = ", ".join(str(number) for number, _ in definitions)
        raise KeysError(
            f"{path}: {variable} is defined {len(definitions)} times (lines {lines}); define it once"
        )
    if not definitions or not definitions[0][1]:
        raise KeysError(f"{path}: does not define {variable}")
    return definitions[0][1]


def _parse_definition(raw: str, variable: str, path: Path, number: int) -> str | None:
    """Read one requested definition without carrying its value into an error."""
    line = raw.strip()
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    name, sep, value = line.partition("=")
    if not sep or name.strip() != variable:
        return None
    stripped = value.strip()
    if stripped.startswith(('"', "'")):
        closing = stripped.find(stripped[0], 1)
        if closing < 0 or (tail := stripped[closing + 1 :].strip()) and not tail.startswith("#"):
            raise KeysError(f"{path}:{number}: invalid quoted value for {variable}")
        return stripped[1:closing]
    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].strip()
    return stripped


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
