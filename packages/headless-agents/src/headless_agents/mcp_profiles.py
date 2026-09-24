"""Named MCP profiles for ``ha run --mcp NAME`` (spec 3.4).

One TOML file, ``$XDG_CONFIG_HOME/ha/mcp.toml`` (default
``~/.config/ha/mcp.toml``), read with :mod:`tomllib`. One table per profile:

.. code-block:: toml

    [brain-read]
    url = "http://127.0.0.1:8765/mcp"
    bearer_env = "BRAIN_TOKEN"   # the variable NAME; the value never sits in this file
    tools = ["brain_search", "brain_get", "brain_recall", "brain_ticket_get"]
    # optional:
    # name = "brain"                         # the server name the CLI declares (default: the table's)
    # headers = { "X-Brain-Agent" = "ha" }
    # allowed_networks = ["10.8.0.0/24"]     # default: loopback only; "any" = no restriction

The file holds names, never secrets: a key that looks like a secret value
(``bearer``, ``token``, ``api_key``, ``password``, ``secret``) is refused.
The bearer VALUE is read from the named variable at run time, when it is set;
a rail that reads it from its environment (codex, claude) finds it there, the
rail that must write it literally (agy) gets it from :attr:`McpServer.bearer`.
No MCP unless asked: the package knows no server by name.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr, ValidationError

from .profile import McpServer

ALLOWED_KEYS: Final = frozenset(
    {"name", "url", "bearer_env", "tools", "headers", "allowed_networks"}
)
SECRET_LOOKING_KEYS: Final = frozenset({"bearer", "token", "api_key", "password", "secret"})
#: ``allowed_networks = "any"``: TOML has no null, so "no restriction" is spelled out.
UNRESTRICTED: Final = "any"


class McpProfileError(ValueError):
    """The profile file or one of its profiles cannot be used; the message says why."""


def default_profiles_path(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None
) -> Path:
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or str((home or Path.home()) / ".config")
    return Path(base) / "ha" / "mcp.toml"


def _validated(name: str, table: object) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise McpProfileError(f"MCP profile {name!r} must be a table")
    secret = SECRET_LOOKING_KEYS & table.keys()
    if secret:
        raise McpProfileError(
            f"MCP profile {name!r} holds {', '.join(sorted(secret))}: the file names the "
            "variable holding the bearer (bearer_env), never its value"
        )
    unknown = table.keys() - ALLOWED_KEYS
    if unknown:
        raise McpProfileError(f"MCP profile {name!r}: unknown keys {', '.join(sorted(unknown))}")
    for required in ("url", "bearer_env"):
        if not isinstance(table.get(required), str) or not table[required].strip():
            raise McpProfileError(f"MCP profile {name!r} needs {required}")
    networks = table.get("allowed_networks")
    if isinstance(networks, str) and networks != UNRESTRICTED:
        raise McpProfileError(
            f"MCP profile {name!r}: allowed_networks is a list of networks, or {UNRESTRICTED!r}"
        )
    return table


def load_profiles(path: Path) -> dict[str, dict[str, Any]]:
    """Every profile in ``path``, validated as data (the URL is checked by :func:`mcp_server`)."""
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except FileNotFoundError:
        raise McpProfileError(f"no MCP profile file at {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise McpProfileError(f"{path} is not valid TOML: {exc}") from None
    profiles = {name: _validated(name, table) for name, table in document.items()}
    for name, table in profiles.items():
        _build(name, table, environ={})  # fail on a bad URL or network now, not at run time
    return profiles


def _build(name: str, table: Mapping[str, Any], *, environ: Mapping[str, str]) -> McpServer:
    networks = table.get("allowed_networks")
    bearer_env = str(table["bearer_env"])
    value = environ.get(bearer_env)
    fields: dict[str, Any] = {
        "name": table.get("name", name),
        "url": table["url"],
        "bearer_env_var": bearer_env,
        "bearer": SecretStr(value) if value else None,
        "tools": tuple(table.get("tools", ())),
        "headers": dict(table.get("headers", {})),
    }
    if networks == UNRESTRICTED:
        fields["allowed_networks"] = None
    elif networks is not None:
        fields["allowed_networks"] = tuple(networks)
    try:
        return McpServer(**fields)
    except ValidationError as exc:
        reasons = "; ".join(str(error["msg"]) for error in exc.errors())
        raise McpProfileError(f"MCP profile {name!r}: {reasons}") from None


def mcp_server(
    name: str,
    *,
    path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> McpServer:
    """The :class:`McpServer` for profile ``name``, its bearer read from ``environ``."""
    environ = os.environ if environ is None else environ
    path = path if path is not None else default_profiles_path(environ)
    profiles = load_profiles(path)
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise McpProfileError(f"no MCP profile {name!r} in {path}; known: {known}")
    return _build(name, profiles[name], environ=environ)
