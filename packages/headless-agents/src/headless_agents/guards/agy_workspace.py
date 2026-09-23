"""The package-owned ``PreToolUse`` guard confining an agy workspace run.

WHY IT EXISTS. This is the workspace half of the agy tool guard (spec
2026-09-23-headless-agents-0.4.0-design.md section 3.3, decision 9): where
``scripts/dream/agy_tool_guard.sh`` is Dream's project-scoped allowlist,
this module is the package-owned guard a `CapabilityProfile.workspace`
composes on any rail. It runs standalone -- Task 7 copies this file, on its
own shebang, into the ephemeral ``HOME`` the sandbox builds -- so it imports
nothing from ``headless_agents`` and nothing beyond the standard library.

WHAT WAS MEASURED (agy 1.2.9, 2026-09-23). The hook payload arrives on stdin
as ``{"toolCall": {"name": ..., "args": {...}}, "artifactDirectoryPath",
"conversationId", "modelName", "stepIdx"}``; the decision is written to
stdout, and ONLY the decision -- ``{"decision":"allow"}`` or
``{"decision":"deny","reason":"..."}`` -- since anything else on stdout
breaks agy's parsing of it. Every tool that names a path uses an ABSOLUTE
one: ``view_file.AbsolutePath``, ``write_to_file.TargetFile``,
``replace_file_content.TargetFile``, ``run_command.CommandLine``/``Cwd``.

THIS IS A SECURITY BOUNDARY: a missing, unreadable or malformed
configuration denies EVERYTHING, and so does a payload that does not parse
into a recognisable ``toolCall``. ``decide`` never raises for any input
shape -- a config that is a list, a path argument that is an int, or args
that are not a mapping all fall through to a deny, not an exception the
caller would have to catch.

``run_command`` IS NOT A SANDBOX. When ``shell`` is armed, ``CommandLine``
itself is never inspected -- the shell it runs under is unconfined on this
rail (spec 3.3) and no allowlist of commands would hold against it. The only
thing this guard confines on ``run_command`` is a *present* ``Cwd``: if the
caller states a working directory outside the workspace root, the call is
denied; an absent ``Cwd`` is allowed through, because agy's own process
``cwd`` (set by the sandbox, not by this guard) governs it instead.

TIME-OF-CHECK / TIME-OF-USE. This guard hands back a decision before agy
opens anything, never a file descriptor -- there is nothing here for a later
swap to race against. With ``write`` off, none of the confined tools can
create the symlink or do the rename a TOCTOU race would need; with ``write``
or ``shell`` on, ``run_command``'s shell is already unconfined (see above),
so a race through it adds no new capability beyond what that already grants.
A pre-existing hardlink inside the workspace root is invisible to
``os.path.realpath`` (it does not resolve hardlinks the way it resolves
symlinks) -- this is an accepted residual: the operator who chooses the
workspace root is the one in a position to create, or avoid, one.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

GUARD_CONFIG_NAME = "workspace-guard.json"

# Tools whose MCP/answer perimeter belongs to the bearer, not to this guard --
# the same reasoning as scripts/dream/agy_tool_guard.sh: duplicating a check
# the server already makes would only create two lists to keep agreeing by
# hand.
_ALWAYS_ALLOWED = frozenset(
    {"finish", "send_message", "call_mcp_tool", "list_resources", "read_resource"}
)

# Tools confined to the workspace root unconditionally (no write flag needed).
_READ_CONFINED = {"view_file": "AbsolutePath"}

# Tools confined to the workspace root, and only when the config arms writes.
_WRITE_CONFINED = {
    "write_to_file": "TargetFile",
    "replace_file_content": "TargetFile",
    "multi_replace_file_content": "TargetFile",
}


def _allow() -> dict[str, str]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason}


def _as_dict(value: object) -> dict[object, object]:
    """Coerce a value of unknown shape into a dict, defensively.

    ``config`` and nested payload fields are attacker- or bug-controlled: a
    config file can be edited by hand, a payload comes from the CLI's own
    JSON encoder. Both are typed as mappings in the interface but nothing
    stops either from arriving as a list or a scalar, so every access goes
    through this instead of a bare ``.get`` that would raise ``AttributeError``.
    """
    return value if isinstance(value, dict) else {}


def _root_realpath(config: object) -> str | None:
    """Resolve the workspace root, or ``None`` if the config cannot ground one.

    A missing config, a non-string ``root``, a relative ``root``, or a
    ``root`` that ``os.path.realpath`` chokes on all mean the same thing
    here: there is no workspace to confine anything to, so every tool that
    needs confinement must deny.
    """
    root = _as_dict(config).get("root")
    if not isinstance(root, str) or not root or not os.path.isabs(root):
        return None
    try:
        return os.path.realpath(root)
    except (OSError, ValueError):
        return None


def _flag(config: object, key: str) -> bool:
    """True iff ``config[key]`` is the literal JSON boolean ``true``.

    Truthiness is the wrong test here (measured: ``{"write": "false"}`` is a
    truthy Python string) -- only ``is True`` tells an armed flag from any
    other JSON value, string, int or otherwise, that a hand-edited config
    could hold.
    """
    return _as_dict(config).get(key) is True


def _confined(value: object, root_realpath: str) -> bool:
    """True iff ``value`` is a non-empty absolute path inside the workspace root."""
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return False
    try:
        resolved = os.path.realpath(value)
    except (OSError, ValueError):
        return False
    return resolved == root_realpath or resolved.startswith(root_realpath + os.sep)


def _names_git(value: str, root_realpath: str) -> bool:
    """True iff ``value`` -- raw OR resolved, relative to the root -- has a ``.git`` component.

    A write under ``.git`` plants a hook or a config that git runs LATER,
    outside any sandbox, when anyone runs git in that checkout; a linked
    worktree's ``.git`` is a FILE naming the gitdir, so rewriting it
    redirects git itself. Both spellings are checked: the raw path catches a
    ``.git`` reached through a symlink that resolves elsewhere inside the
    root, the resolved one a symlink whose own name says nothing. Compared
    with ``casefold()``, as the argument keys are (see `_case_variant`): a
    case-insensitive filesystem would open ``.GIT`` as ``.git``.
    """
    for path in (value, os.path.realpath(value)):
        relative = os.path.relpath(path, root_realpath)
        if any(part.casefold() == ".git" for part in relative.split(os.sep)):
            return True
    return False


def _case_variant(fields: dict[object, object], exact_key: str) -> str | None:
    """Return an offending key if ``fields`` holds a case-fold duplicate of ``exact_key``.

    A key differing from the one this guard reads only by case is either an
    adversarial probe for a parser differential (this guard reads one
    casing, agy or a future parser might read another) or a silent rename
    under our feet -- this guard cannot tell the two apart, so both deny.

    Compared with ``casefold()``, not ``lower()``: agy's Go implementation
    matches JSON object keys against struct fields with ``bytes.EqualFold``,
    which folds Unicode look-alikes plain ASCII lowercasing does not --
    measured: U+017F ("ſ", LATIN SMALL LETTER LONG S) folds to "s" and
    U+212A ("K", KELVIN SIGN) folds to "k", so a key differing only by one of
    those would have compared as case-DIFFERENT under ``lower()`` while agy's
    own parser would still, or also, read it as the ASCII key. ``casefold()``
    can over-match relative to Go's fold in rarer cases (e.g. "ß" folds to
    "ss" here); that only ever turns a would-be allow into a deny, which is
    the safe direction for a boundary that fails closed.
    """
    folded = exact_key.casefold()
    for key in fields:
        if isinstance(key, str) and key != exact_key and key.casefold() == folded:
            return key
    return None


def _tool_call(payload: str) -> tuple[str, dict[object, object]] | None:
    """Parse the hook payload into ``(tool name, args)``, or ``None`` if unreadable.

    The envelope keys -- ``toolCall`` at the top level, ``name`` and ``args``
    inside it -- are read exactly, the same as a tool's own argument keys
    are; a case-fold duplicate of any of them is the same parser-differential
    risk `_case_variant` guards against below, so it is checked here too.
    """
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    envelope = _as_dict(parsed)
    if _case_variant(envelope, "toolCall") is not None:
        return None
    call = envelope.get("toolCall")
    if not isinstance(call, dict):
        return None
    if _case_variant(call, "name") is not None or _case_variant(call, "args") is not None:
        return None
    name = call.get("name")
    if not isinstance(name, str):
        return None
    return name, _as_dict(call.get("args"))


def _decide(payload: str, config: Mapping[str, object]) -> dict[str, str]:
    """The actual decision logic -- see `decide` for the fail-closed wrapper.

    Most malformed shapes already fall through to a deny by construction
    (``_as_dict``, ``_root_realpath``, ``_confined`` all degrade instead of
    raising), but this body can still raise on an input `decide` has to
    catch regardless -- e.g. `json.loads` itself hits `RecursionError` on a
    sufficiently deeply nested payload, long before any of the helpers above
    run.
    """
    call = _tool_call(payload)
    if call is None:
        return _deny("hook payload is missing, unreadable, or names no tool")
    name, args = call

    root_realpath = _root_realpath(config)
    if root_realpath is None:
        return _deny("workspace guard config is missing, unreadable, or its root is not absolute")

    if name in _ALWAYS_ALLOWED:
        return _allow()

    if name in _READ_CONFINED:
        arg_name = _READ_CONFINED[name]
        variant = _case_variant(args, arg_name)
        if variant is not None:
            return _deny(f"{name} args carry both {arg_name!r} and case-variant {variant!r}")
        if _confined(args.get(arg_name), root_realpath):
            return _allow()
        return _deny(f"{name}.{arg_name} is outside the workspace")

    if name in _WRITE_CONFINED:
        if not _flag(config, "write"):
            return _deny(f"{name} is denied: writes are not armed for this run")
        arg_name = _WRITE_CONFINED[name]
        variant = _case_variant(args, arg_name)
        if variant is not None:
            return _deny(f"{name} args carry both {arg_name!r} and case-variant {variant!r}")
        target = args.get(arg_name)
        if not _confined(target, root_realpath):
            return _deny(f"{name}.{arg_name} is outside the workspace")
        # `_confined` proved `target` an absolute str `realpath` accepts.
        if _names_git(cast("str", target), root_realpath):
            return _deny(f"{name}.{arg_name} is under .git: git would run what it plants")
        return _allow()

    if name == "run_command":
        if not _flag(config, "shell"):
            return _deny("run_command is denied: shell is not armed for this run")
        variant = _case_variant(args, "Cwd")
        if variant is not None:
            return _deny(f"run_command args carry both 'Cwd' and case-variant {variant!r}")
        cwd = args.get("Cwd")
        if cwd is not None and not _confined(cwd, root_realpath):
            return _deny("run_command.Cwd is outside the workspace")
        return _allow()

    return _deny(f"{name} is outside the workspace guard's allowlist")


def decide(payload: str, config: Mapping[str, object]) -> dict[str, str]:
    """Decide allow/deny for one agy ``PreToolUse`` hook call. Never raises.

    ``config`` is declared as a mapping, but nothing enforces that at
    runtime -- a hand-edited config file can hold a list, a string, or
    anything JSON allows. This is the fail-closed boundary: it must survive
    a config, or a payload field, of any shape -- including one that makes
    ``_decide`` itself raise (a deeply nested payload blows the parser's
    recursion limit before any of the shape checks run) -- without an
    exception escaping to the caller.
    """
    try:
        return _decide(payload, config)
    except Exception:
        return _deny("workspace guard internal error")


def main() -> int:
    """Read one hook call from stdin, write the decision to stdout, exit 0.

    Exits 0 on both allow and deny: a deny IS the guard doing its job, not a
    failure of the guard itself. The config path is fixed --
    ``$HOME/.gemini/config/workspace-guard.json`` -- because agy's ephemeral
    HOME (built by :mod:`headless_agents.sandbox`) is the only place this
    guard, itself copied there, can read a run-specific configuration from.

    EVERYTHING here -- including the stdin read itself, which can raise
    ``UnicodeDecodeError`` on non-UTF-8 bytes before ``decide`` is ever
    reached -- is wrapped fail-closed. An uncaught exception here would print
    a traceback instead of a decision: empty stdout plus a non-JSON stderr
    dump is exactly what an agy release that reads "no decision" as an allow
    would do the wrong thing with (the Dream guard,
    ``scripts/dream/agy_tool_guard.sh``, documents the same risk and prints a
    deny even when its own Python helper crashes). ``BaseException``, not
    ``Exception``: this is the outermost boundary of a CLI script, not a
    library call a caller might want to interrupt.
    """
    try:
        payload = sys.stdin.read()
        config: object = {}
        try:
            config_path = (
                Path(os.environ.get("HOME", "")) / ".gemini" / "config" / GUARD_CONFIG_NAME
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            config = {}
        # `json.loads` returns `Any`: the file on disk could just as well
        # hold a JSON list or scalar. The cast satisfies the declared
        # interface; `decide` itself re-validates the actual runtime shape
        # rather than trusting it.
        result = decide(payload, cast("Mapping[str, object]", config))
    except BaseException:
        result = _deny("workspace guard internal error")
    try:
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
    except BaseException:
        # The contract is unconditional: main() ALWAYS returns 0. A reader
        # that hung up early (a broken pipe) must not turn into a non-zero
        # exit or an uncaught exception -- there would be nowhere left for
        # either to be reported to anyway.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
