"""The README synopsis must name every subcommand and every long option the parser
accepts, and name no option the parser lacks (0.5.2 lot 5, Task 3).

CLAUDE.md: a copied value ages without turning anything red. The CLI synopsis is
exactly that kind of copy -- these tests pin it to ``cli._parser()`` itself, so a
future flag added to the parser and forgotten in the README (or removed from the
parser and left stale in the README) fails here, not in a person's hands.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from headless_agents import cli

REPO_ROOT = Path(__file__).resolve().parents[3]
README_PATH = REPO_ROOT / "packages" / "headless-agents" / "README.md"

_HEADING_AND_FENCE = re.compile(r"## The `ha` CLI\n.*?```text\n(.*?)```", re.DOTALL)
_LONG_OPTION = re.compile(r"--[a-z][a-z-]*")


def _long_options(actions: list[argparse.Action]) -> set[str]:
    """Every ``--long`` option name these actions accept a user could actually pass:
    ``--help`` and anything ``argparse.SUPPRESS``-hidden (the 0.5.0-removed ``-p``/
    ``--provider`` and ``--chain``, kept only so their use gets a message, never a
    thing to document) are excluded."""
    found: set[str] = set()
    for action in actions:
        if action.help == argparse.SUPPRESS:
            continue
        for option in action.option_strings:
            if option.startswith("--") and option != "--help":
                found.add(option)
    return found


def _parser_surface() -> dict[str, set[str]]:
    """``{"": top-level options, subcommand: its own --long options}``."""
    parser = cli._parser()  # noqa: SLF001
    surface: dict[str, set[str]] = {"": _long_options(parser._actions)}  # noqa: SLF001
    subparsers = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    )
    for name, sub in subparsers.choices.items():
        surface[name] = _long_options(sub._actions)  # noqa: SLF001
    return surface


def _synopsis() -> dict[str, set[str]]:
    """The same shape as :func:`_parser_surface`, read from the README's first
    ```` ```text ```` block after ``## The `ha` CLI``. Each ``ha <sub> ...`` line
    starts a subcommand entry; an indented line continues the entry above it (a
    ``run`` invocation wraps over several lines); ``ha --version`` is the top-level
    entry, keyed ``""``."""
    match = _HEADING_AND_FENCE.search(README_PATH.read_text(encoding="utf-8"))
    assert match, f"no ```text synopsis found after '## The `ha` CLI' in {README_PATH}"
    surface: dict[str, set[str]] = {}
    current: str | None = None
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        if line == line.lstrip() and line.startswith("ha "):
            rest = line[len("ha ") :]
            first_word = rest.split(maxsplit=1)[0]
            current = "" if first_word.startswith("--") else first_word
            surface.setdefault(current, set())
        assert current is not None, f"a continuation line before any 'ha ...' line: {line!r}"
        surface[current].update(_LONG_OPTION.findall(line))
    return surface


def test_every_subcommand_is_in_the_readme_synopsis() -> None:
    parser_subs = set(_parser_surface()) - {""}
    readme_subs = set(_synopsis()) - {""}
    missing = parser_subs - readme_subs
    assert not missing, f"subcommands in the parser but not the README synopsis: {sorted(missing)}"


def test_every_long_option_is_in_the_readme_synopsis() -> None:
    parser_surface = _parser_surface()
    readme_surface = _synopsis()
    failures = []
    for sub, options in parser_surface.items():
        missing = options - readme_surface.get(sub, set())
        if missing:
            label = "ha --version" if sub == "" else f"ha {sub}"
            failures.append(f"{label}: parser has {sorted(missing)}, missing from the README")
    assert not failures, "\n".join(failures)


def test_the_readme_synopsis_names_no_option_the_parser_lacks() -> None:
    parser_surface = _parser_surface()
    readme_surface = _synopsis()
    failures = []
    for sub, options in readme_surface.items():
        if sub not in parser_surface:
            failures.append(f"ha {sub}: in the README synopsis but not a real subcommand")
            continue
        stray = options - parser_surface[sub]
        if stray:
            label = "ha --version" if sub == "" else f"ha {sub}"
            failures.append(f"{label}: README names {sorted(stray)}, which the parser lacks")
    assert not failures, "\n".join(failures)
