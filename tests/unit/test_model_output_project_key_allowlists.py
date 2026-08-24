"""Census the sites where MODEL OUTPUT is membership-tested against project keys.

The class this closes
---------------------
A dream phase parses a model's JSON, reads a project key out of it, and tests
that key against a set of allowed projects.  If the key is not run through
``canonicalize_project_key`` first, the model's ``brain_v42`` (the repo's
underscore spelling) fails a membership test against ``brain-v42`` — and the
rejected ticket stays ``pending``, so the same night replays the same failure
forever.  That happened: ``extract`` failed the nights of 2026-08-19 and
2026-08-20 on ``target_project 'brain_v42' not in ['brain-v42', 'red-shrik']``,
and ``3777f33`` closed it by canonicalizing before the test.

The instance is fixed.  Nothing stopped a second site from appearing and
replaying it, which is what this module is for.

Why the assertions are asymmetric
---------------------------------
A census that demanded every site be declared would redden on a *correct* new
site — punishing the good gesture and freezing today's map as a specification.
So the two directions are not symmetric, and deliberately so:

* an UNCANONICALIZED site that nobody declared reddens.  That is the class.
* a declared uncanonicalized site that is gone — fixed or deleted — reddens, so
  the list cannot rot.
* a declared canonicalized site that REGRESSES to raw reddens.  That is the
  regression guard on ``3777f33`` itself.
* a NEW canonicalized site is silent.  Adding a correctly-written site costs
  nothing, which is the whole point.

What this scanner does NOT see
------------------------------
Stated because a scanner that hides its blind spots reads as exhaustive:

1. **Detection is by NAME.**  A project key carried under an operand that does
   not contain "project" — ``key``, ``pk``, ``owner`` — is invisible.  This is
   the same shape of blind spot as W18-b's ``pg_insert(table_map[kind])``.
2. **The parse boundary is ``ResponseParseError``.**  A membership test in a
   helper called *from* a parser, rather than inside it, escapes.
3. **Canonicalization is read in the same function.**  A helper that
   canonicalizes and returns is read as raw — over-reporting, never under.
4. **Only ``in``/``not in`` compares.**  ``any(k == x for k in keys)`` or a
   ``.keys()`` lookup escapes.
5. The allowlist itself is never inspected.  It is often not a constant — in
   ``ticket_extract`` it is ``sorted(participants)``, computed per ticket — so
   anchoring on the right-hand operand would have found nothing at all.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]

MODEL_OUTPUT_SCAN_ROOTS: Final[tuple[str, ...]] = ("src", "scripts")

#: The marker of a model-output parse boundary on this project.
PARSE_ERROR = "ResponseParseError"

#: An operand denotes a project key when its name says so.  See blind spot 1.
_PROJECT_OPERAND = re.compile(r"project", re.IGNORECASE)

CANONICALIZER = "canonicalize_project_key"

#: Sites reading a project key out of model output WITHOUT canonicalizing it.
#: Empty is the healthy state: it means the class has no open instance.  A line
#: here is a declared, reviewed exception — never a target to preserve.
DECLARED_UNCANONICALIZED_SITES: Final[frozenset[str]] = frozenset()

#: Sites that DO canonicalize.  Listed to be held to it: each must still exist
#: and still canonicalize.  This list is not exhaustive by design and must not
#: become so — a new correct site is not required to appear here.
DECLARED_CANONICALIZED_SITES: Final[frozenset[str]] = frozenset(
    {
        # The site the class is named after. Canonicalized since 3777f33, in
        # `strict=False` — the only admissible mode, because `strict=True`
        # raises ValueError outside the caller's `except ResponseParseError`
        # and would kill the corrective re-prompt instead of rejecting one item.
        "src/brain_v42/scripts/ticket_extract.py::parse_and_validate::tproject",
    }
)


def _operand_name(node: ast.expr) -> str | None:
    """The readable name of a compared operand, however it was written."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, str):
            return index.value
        return _operand_name(node.value)
    if isinstance(node, ast.Call):
        name = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", None)
        )
        if name == CANONICALIZER and node.args:
            return _operand_name(node.args[0])
        return name
    return None


def _mentions_canonicalizer(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = (
            child.func.attr
            if isinstance(child.func, ast.Attribute)
            else getattr(child.func, "id", None)
        )
        if name == CANONICALIZER:
            return True
    return False


def _raises_parse_error(func: ast.AST) -> bool:
    """True when this function is a model-output parse boundary."""
    return any(
        isinstance(node, ast.Raise) and PARSE_ERROR in ast.dump(node) for node in ast.walk(func)
    )


def _canonicalized_names(func: ast.AST) -> frozenset[str]:
    """Names bound, anywhere in this function, from a canonicalizing expression."""
    names: set[str] = set()
    for node in ast.walk(func):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = node.value
        if value is None or not _mentions_canonicalizer(value):
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


class _AllowlistTestVisitor(ast.NodeVisitor):
    """Collect ``qualname::operand -> status`` for project-key membership tests."""

    def __init__(self) -> None:
        self.found: dict[str, str] = {}
        self._scope: list[str] = []
        self._canonicalized: list[frozenset[str]] = []

    @property
    def _qualname(self) -> str:
        return ".".join(self._scope) if self._scope else "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        boundary = _raises_parse_error(node)
        if boundary:
            self._canonicalized.append(_canonicalized_names(node))
        self.generic_visit(node)
        if boundary:
            self._canonicalized.pop()
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if self._canonicalized and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            name = _operand_name(node.left)
            if name and _PROJECT_OPERAND.search(name):
                site = f"{self._qualname}::{name}"
                canonical = name in self._canonicalized[-1] or _mentions_canonicalizer(node.left)
                status = "canonicalized" if canonical else "raw"
                # A name tested twice keeps the worse reading.
                if self.found.get(site) != "raw":
                    self.found[site] = status
        self.generic_visit(node)


def _candidate_files(roots: Iterable[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.is_file():
                yield path


def scan_model_output_allowlist_sites(roots: Iterable[Path], repo_root: Path) -> Mapping[str, str]:
    """Map ``"<relpath>::<enclosing def>::<operand>"`` to ``canonicalized``/``raw``."""
    sites: dict[str, str] = {}
    for path in _candidate_files(roots):
        text = path.read_text(encoding="utf-8", errors="replace")
        if PARSE_ERROR not in text:
            continue
        relative = path.relative_to(repo_root).as_posix()
        visitor = _AllowlistTestVisitor()
        visitor.visit(ast.parse(text, filename=str(path)))
        for entry, status in visitor.found.items():
            sites[f"{relative}::{entry}"] = status
    return sites


def _observed() -> Mapping[str, str]:
    return scan_model_output_allowlist_sites(
        [REPO_ROOT / name for name in MODEL_OUTPUT_SCAN_ROOTS], REPO_ROOT
    )


def test_no_undeclared_uncanonicalized_site() -> None:
    """A model-supplied project key tested raw is the poisoned-night class."""
    raw = {site for site, status in _observed().items() if status == "raw"}
    undeclared = raw - DECLARED_UNCANONICALIZED_SITES
    assert not undeclared, (
        "model output is membership-tested against project keys WITHOUT "
        f"{CANONICALIZER}. The model spells the repo's key with an underscore "
        "(brain_v42) and the test rejects it; a rejected ticket stays pending, so "
        "the failure replays every night. Canonicalize with strict=False before "
        "the test -- or declare the site in DECLARED_UNCANONICALIZED_SITES with "
        f"the reason: {sorted(undeclared)}"
    )


def test_declared_uncanonicalized_sites_still_exist() -> None:
    """A declared exception that was fixed or deleted must leave the list."""
    raw = {site for site, status in _observed().items() if status == "raw"}
    vanished = DECLARED_UNCANONICALIZED_SITES - raw
    assert not vanished, (
        "DECLARED_UNCANONICALIZED_SITES names site(s) that are no longer raw -- "
        f"canonicalized, or gone. Drop the line: {sorted(vanished)}"
    )


def test_declared_canonicalized_sites_have_not_regressed() -> None:
    """The guard on 3777f33: a site that canonicalizes must keep doing so."""
    observed = _observed()
    regressed = sorted(
        f"{site} (now: {observed.get(site, 'ABSENT')})"
        for site in DECLARED_CANONICALIZED_SITES
        if observed.get(site) != "canonicalized"
    )
    assert not regressed, (
        "site(s) that used to canonicalize a model-supplied project key before the "
        "membership test no longer do, or no longer exist. Dropping the "
        f"canonicalization replays the poisoned night of 2026-08-19: {regressed}"
    )


def test_scan_roots_all_exist() -> None:
    """A typo in a scan root would silently shrink the census to nothing."""
    for name in MODEL_OUTPUT_SCAN_ROOTS:
        assert (REPO_ROOT / name).is_dir(), f"declared scan root is missing: {name}"
