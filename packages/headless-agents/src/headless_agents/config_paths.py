"""Where ``ha`` reads its configuration and keeps its state (spec 0.5.0 §3.3, §3.8).

Configuration comes from the operator's directory only -- never from a
repository or the working directory: a ``roles.toml`` shipped in a cloned
repository could arm ``write`` and ``shell``. ``XDG_CONFIG_HOME`` and
``XDG_STATE_HOME`` count only when absolute, as the XDG specification
requires; a relative value is ignored, never resolved against the working
directory. The directory is resolved once with its links followed, and every
file must then resolve inside it: a file that is, or links to, anything
outside is refused -- a dangling link included, which would otherwise read as
"no file" and silently drop the operator's configuration.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class ConfigPathError(ValueError):
    """A configuration file resolves outside the configuration directory."""


def _xdg(environ: Mapping[str, str], name: str, default: Path) -> Path:
    value = environ.get(name, "")
    return Path(value) if value and os.path.isabs(value) else default


def config_dir(environ: Mapping[str, str], *, home: Path) -> Path:
    """``$XDG_CONFIG_HOME/ha`` when that variable is absolute, else ``~/.config/ha``."""
    return (_xdg(environ, "XDG_CONFIG_HOME", home / ".config") / "ha").resolve()


def state_dir(environ: Mapping[str, str], *, home: Path) -> Path:
    """``$XDG_STATE_HOME/ha`` when that variable is absolute, else ``~/.local/state/ha``."""
    return (_xdg(environ, "XDG_STATE_HOME", home / ".local" / "state") / "ha").resolve()


def config_file(name: str, environ: Mapping[str, str], *, home: Path) -> Path | None:
    """The resolved path of configuration file ``name``, ``None`` when there is none.

    Raises :class:`ConfigPathError` when the file is, or links to, anything
    outside :func:`config_dir`.
    """
    directory = config_dir(environ, home=home)
    candidate = directory / name
    if not candidate.exists() and not candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(directory):
        raise ConfigPathError(
            f"{candidate}: points to {resolved}, outside the configuration directory "
            f"{directory}; configuration is read only from there"
        )
    return resolved


__all__ = ["ConfigPathError", "config_dir", "config_file", "state_dir"]
