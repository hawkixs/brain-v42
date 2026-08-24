"""Drift gates for repository and production-facing documentation."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from alembic.config import Config
from alembic.script import ScriptDirectory

from brain_v42.config import Settings

REPO_ROOT = Path(__file__).parent.parent.parent
README = (REPO_ROOT / "README.md").read_text()
OPERATIONS = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
# CLAUDE.md is tracked only in the private archive -- absent from the
# eventual public repository (decision 14856555). Every assertion that
# needs its content guards on CLAUDE being non-empty (or the whole test is
# skipped, for tests that are fundamentally about CLAUDE.md's own
# consistency); assertions about the public docs (README, ARCHITECTURE,
# MCP_TOOLS, SCHEMA) must keep passing either way.
_CLAUDE_PATH = REPO_ROOT / "CLAUDE.md"
CLAUDE = _CLAUDE_PATH.read_text() if _CLAUDE_PATH.exists() else ""
ARCHITECTURE = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text()
MCP_TOOLS = (REPO_ROOT / "docs" / "MCP_TOOLS.md").read_text()
SCHEMA = (REPO_ROOT / "docs" / "SCHEMA.md").read_text()
GRAPH_RUNBOOK = (REPO_ROOT / "docs" / "GRAPH_LEDGER_RUNBOOK.md").read_text()
CODEX_GATEWAY = (REPO_ROOT / "deploy" / "CODEX_GATEWAY.md").read_text()
ROADMAP = (REPO_ROOT / "docs" / "plans" / "2026-07-11-sol-ultra-audit-roadmap-plan.md").read_text()
DR_IMPLEMENTATION_PLAN = (
    REPO_ROOT / "docs" / "plans" / "2026-07-11-disaster-recovery-verified-implementation-plan.md"
).read_text()
DR_B2_HANDOFF = (
    REPO_ROOT / "docs" / "plans" / "2026-07-11-disaster-recovery-b2-session-handoff.md"
).read_text()
DR_B3_EVIDENCE = (
    REPO_ROOT / "docs" / "plans" / "2026-07-12-disaster-recovery-b3-operational-evidence.md"
).read_text()
DEV_PC_RUNBOOK = (REPO_ROOT / "deploy" / "dev-pc" / "README.md").read_text()
DREAM_EXTRACT_RECOVERY_RUNBOOK = (
    REPO_ROOT / "docs" / "runbooks" / "2026-08-01-dream-extract-recovery-canary.md"
).read_text()
SERVER = (REPO_ROOT / "src" / "brain_v42" / "mcp" / "server.py").read_text()
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

requires_claude = pytest.mark.skipif(
    not CLAUDE, reason="CLAUDE.md is a private file, absent from the public repository"
)


def _docs_including_claude(*others: str) -> tuple[str, ...]:
    """Documents to check, adding CLAUDE.md only when it exists.

    CLAUDE.md is private and won't ship in the public repository. A positive
    "contract in document" assertion over a tuple that includes CLAUDE would
    fail on an empty read; dropping it from the tuple when absent keeps every
    OTHER document's check exactly as strict, instead of skipping the whole
    assertion. Negative ("not in") assertions don't need this helper: an
    empty string trivially satisfies them.
    """
    return (*others, CLAUDE) if CLAUDE else others


def _dream_extract_backfill_guard() -> str:
    match = re.search(
        r"<!-- backfill-recovery-guard:start -->\n```bash\n(.*?)```\n"
        r"<!-- backfill-recovery-guard:end -->",
        DREAM_EXTRACT_RECOVERY_RUNBOOK,
        flags=re.DOTALL,
    )
    assert match is not None, "missing executable Dream EXTRACT backfill recovery guard"
    return match.group(1)


@pytest.mark.parametrize(
    ("snapshot_url", "restore_db", "restore_url", "expected_error"),
    [
        (
            "postgresql://brain:sentinel-runbook-secret@db.example:5432/wrong_snapshot",
            "brain_restore",
            "postgresql://brain:sentinel-runbook-secret@db.example:5432/brain_restore",
            "snapshot target identity mismatch",
        ),
        (
            "postgresql://brain:sentinel-runbook-secret@db.example:5432/brain_operated",
            "brain_operated",
            "postgresql://brain:sentinel-runbook-secret@db.example:5432/brain_operated",
            "restore target must differ from operated database",
        ),
    ],
    ids=["snapshot-target-mismatch", "restore-target-mismatch"],
)
def test_dream_extract_backfill_guard_stops_before_external_commands(
    tmp_path: Path,
    snapshot_url: str,
    restore_db: str,
    restore_url: str,
    expected_error: str,
) -> None:
    """A mismatched identity must fail before dump, restore, or backfill."""
    guard_path = tmp_path / "backfill-recovery-guard.sh"
    guard_path.write_text(_dream_extract_backfill_guard())
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "external-command-called"
    for command in ("pg_dump", "pg_restore", "createdb", "psql"):
        fake_command = fake_bin / command
        fake_command.write_text('#!/bin/sh\ntouch "$BACKFILL_GUARD_MARKER"\n')
        fake_command.chmod(0o755)

    target_url = "postgresql+asyncpg://brain:sentinel-runbook-secret@db.example:5432/brain_operated"
    env = os.environ | {
        "POSTGRES_URL": target_url,
        "BACKFILL_PGURL": snapshot_url,
        "BACKFILL_PROJECT": "test-project",
        "BACKFILL_SNAPSHOT_DIR": str(tmp_path / "snapshots"),
        "BACKFILL_RESTORE_DB": restore_db,
        "BACKFILL_RESTORE_ADMIN_PGURL": (
            "postgresql://brain:sentinel-runbook-secret@db.example:5432/postgres"
        ),
        "BACKFILL_RESTORE_PGURL": restore_url,
        "BACKFILL_PYTHON": sys.executable,
        "BACKFILL_GUARD_MARKER": str(marker),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(guard_path)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not marker.exists()
    assert "sentinel-runbook-secret" not in result.stdout
    assert "sentinel-runbook-secret" not in result.stderr


def _repository_head() -> str:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a linear Alembic chain, got heads={heads}"
    head = heads[0]
    assert head.isdecimal(), f"expected a numeric Alembic head, got {head}"

    revisions = list(script.walk_revisions(base="base", head="heads"))
    expected = {f"{number:03d}" for number in range(1, int(head) + 1)}
    assert {revision.revision for revision in revisions} == expected
    for revision in revisions:
        number = int(revision.revision)
        expected_parent = None if number == 1 else f"{number - 1:03d}"
        assert revision.down_revision == expected_parent
    return head


def _tool_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not isinstance(target, ast.Attribute) or target.attr != "tool":
            continue
        if isinstance(decorator, ast.Call):
            public_name: str | None = None
            if decorator.args:
                assert len(decorator.args) == 1, (
                    "MCP tool decorators accept at most one positional public name"
                )
                positional_name = decorator.args[0]
                assert isinstance(positional_name, ast.Constant) and isinstance(
                    positional_name.value,
                    str,
                ), "public MCP tool name must be a literal string"
                public_name = positional_name.value
            for keyword in decorator.keywords:
                assert keyword.arg is not None, (
                    "expanded MCP tool decorator options are unsupported"
                )
                if keyword.arg not in {"name", "name_or_fn"}:
                    continue
                assert public_name is None, (
                    "public MCP tool name cannot be both positional and keyword"
                )
                assert isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value,
                    str,
                ), "public MCP tool name must be a literal string"
                public_name = keyword.value.value
            if public_name is not None:
                return public_name
        return node.name
    return None


def _assigned_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _assigned_names(element)}
    if isinstance(target, ast.Starred):
        return _assigned_names(target.value)
    return set()


def _is_registration_name(name: str) -> bool:
    return name == "register_tools" or (name.startswith("register_") and name.endswith("_tools"))


def _statement_bindings(statement: ast.stmt) -> tuple[set[str], set[str]]:
    bound_names: set[str] = set()
    mutated_bases: set[str] = set()

    class BindingVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            bound_names.add(node.name)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            bound_names.add(node.name)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            bound_names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound_names.add(node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                root: ast.expr = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    mutated_bases.add(root.id)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for imported in node.names:
                bound_names.add(imported.asname or imported.name.split(".", maxsplit=1)[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for imported in node.names:
                bound_names.add(imported.asname or imported.name)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            # Comprehension targets have their own scope in Python 3. Their
            # iterables and guards can still contain outer-scope mutations.
            self.visit(node.iter)
            for condition in node.ifs:
                self.visit(condition)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name is not None:
                bound_names.add(node.name)
            self.generic_visit(node)

        def visit_MatchAs(self, node: ast.MatchAs) -> None:
            if node.name is not None:
                bound_names.add(node.name)
            self.generic_visit(node)

        def visit_MatchStar(self, node: ast.MatchStar) -> None:
            if node.name is not None:
                bound_names.add(node.name)

        def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
            if node.rest is not None:
                bound_names.add(node.rest)
            self.generic_visit(node)

    BindingVisitor().visit(statement)
    return bound_names, mutated_bases


def _local_registration_rebindings(
    statements: list[ast.stmt],
    local_registrations: set[str],
    registration_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> set[str]:
    rebound_names: set[str] = set()
    for statement in statements:
        bound_names, _ = _statement_bindings(statement)
        for registration_name, registration_node in registration_nodes.items():
            if statement is registration_node:
                bound_names.discard(registration_name)
        rebound_names.update(bound_names & local_registrations)
    return rebound_names


def _bind_registration_import(
    node: ast.Import | ast.ImportFrom,
    registration_modules: dict[str, str],
    name_bindings: dict[str, str],
    module_bindings: dict[str, str],
    *,
    conditional: bool,
) -> None:
    known_modules = set(registration_modules.values())
    if isinstance(node, ast.ImportFrom):
        for imported in node.names:
            expected_module = registration_modules.get(imported.name)
            if _is_registration_name(imported.name):
                assert expected_module is not None, f"unresolved registrar import: {imported.name}"
            bound_name = imported.asname or imported.name
            collisions = {bound_name} & (name_bindings.keys() | module_bindings.keys())
            assert not collisions, f"registration aliases reassigned: {collisions}"
            if expected_module is None:
                continue
            assert node.level == 0 and node.module == expected_module, (
                f"registrar {imported.name} imported from unexpected module {node.module}"
            )
            assert not conditional, "registration imports under dynamic control flow are unsafe"
            name_bindings[bound_name] = imported.name
        return

    for imported in node.names:
        bound_name = imported.asname or imported.name.split(".", maxsplit=1)[0]
        collisions = {bound_name} & (name_bindings.keys() | module_bindings.keys())
        assert not collisions, f"registration aliases reassigned: {collisions}"
        if imported.name.startswith("brain_v42.mcp.tools."):
            assert imported.name in known_modules, f"unresolved registrar module: {imported.name}"
        if imported.name not in known_modules:
            continue
        assert imported.asname is not None, (
            "qualified registration-module imports require an explicit alias"
        )
        assert not conditional, "registration imports under dynamic control flow are unsafe"
        module_bindings[imported.asname] = imported.name


def _resolve_registration_call(
    call: ast.Call,
    registration_modules: dict[str, str],
    name_bindings: dict[str, str],
    module_bindings: dict[str, str],
) -> str | None:
    if isinstance(call.func, ast.Name):
        registration_name = name_bindings.get(call.func.id)
        if registration_name in registration_modules:
            return registration_name
        assert not _is_registration_name(call.func.id), f"unresolved registrar call: {call.func.id}"
        return None
    if not isinstance(call.func, ast.Attribute):
        return None
    if not isinstance(call.func.value, ast.Name):
        assert not _is_registration_name(call.func.attr), (
            f"unresolved registrar call: {call.func.attr}"
        )
        return None
    module_name = module_bindings.get(call.func.value.id)
    if module_name is not None and registration_modules.get(call.func.attr) == module_name:
        return call.func.attr
    assert not _is_registration_name(call.func.attr), f"unresolved registrar call: {call.func.attr}"
    return None


def _resolved_registration_calls(
    node: ast.AST | None,
    registration_modules: dict[str, str],
    name_bindings: dict[str, str],
    module_bindings: dict[str, str],
    *,
    resolved_reference_nodes: set[int] | None = None,
) -> list[str]:
    if node is None:
        return []
    calls: list[str] = []

    class CallVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            registration_name = _resolve_registration_call(
                node,
                registration_modules,
                name_bindings,
                module_bindings,
            )
            if registration_name is not None:
                calls.append(registration_name)
                if resolved_reference_nodes is not None:
                    resolved_reference_nodes.add(id(node.func))
            self.generic_visit(node)

    CallVisitor().visit(node)
    return calls


def _registration_value_references(
    node: ast.AST | None,
    registration_modules: dict[str, str],
    name_bindings: dict[str, str],
    module_bindings: dict[str, str],
    *,
    allowed_reference_nodes: set[int] | None = None,
) -> set[str]:
    if node is None:
        return set()
    references: set[str] = set()
    allowed_nodes = allowed_reference_nodes or set()

    class ReferenceVisitor(ast.NodeVisitor):
        def _visit_arguments(self, arguments: ast.arguments) -> None:
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ):
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            for argument in (arguments.vararg, arguments.kwarg):
                if argument is not None and argument.annotation is not None:
                    self.visit(argument.annotation)
            for default in (*arguments.defaults, *arguments.kw_defaults):
                if default is not None:
                    self.visit(default)

        def _visit_function_definition(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            self._visit_arguments(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for type_parameter in getattr(node, "type_params", ()):
                self.visit(type_parameter)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function_definition(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function_definition(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            for decorator in node.decorator_list:
                self.visit(decorator)
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
            for type_parameter in getattr(node, "type_params", ()):
                self.visit(type_parameter)
            for statement in node.body:
                self.visit(statement)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            self._visit_arguments(node.args)

        def visit_Name(self, node: ast.Name) -> None:
            if (
                id(node) not in allowed_nodes
                and isinstance(node.ctx, ast.Load)
                and node.id in name_bindings
            ):
                references.add(node.id)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            if id(node) not in allowed_nodes and isinstance(node.value, ast.Name):
                module_name = module_bindings.get(node.value.id)
                if module_name is not None and registration_modules.get(node.attr) == module_name:
                    references.add(f"{node.value.id}.{node.attr}")
            self.generic_visit(node)

    ReferenceVisitor().visit(node)
    return references


def _top_level_registration_bindings(
    statements: list[ast.stmt],
    registration_modules: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    name_bindings: dict[str, str] = {}
    module_bindings: dict[str, str] = {}
    for statement in statements:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            _bind_registration_import(
                statement,
                registration_modules,
                name_bindings,
                module_bindings,
                conditional=False,
            )
            continue
        rebound, mutated_bases = _statement_bindings(statement)
        collisions = rebound & (name_bindings.keys() | module_bindings.keys())
        assert not collisions, f"registration aliases reassigned at module scope: {collisions}"
        mutations = mutated_bases & module_bindings.keys()
        assert not mutations, f"registration modules mutated at module scope: {mutations}"
    return name_bindings, module_bindings


def _registration_calls(
    statements: list[ast.stmt],
    registration_modules: dict[str, str],
    name_bindings: dict[str, str],
    module_bindings: dict[str, str] | None = None,
) -> list[str]:
    active_names = name_bindings.copy()
    active_modules = (module_bindings or {}).copy()

    def analyze_block(
        block: list[ast.stmt],
        names: dict[str, str],
        modules: dict[str, str],
        *,
        conditional: bool,
    ) -> list[str]:
        calls: list[str] = []
        terminated = False
        for statement in block:
            if not isinstance(statement, (ast.Import, ast.ImportFrom)):
                rebound, mutated_bases = _statement_bindings(statement)
                collisions = rebound & (names.keys() | modules.keys())
                assert not collisions, f"registration aliases reassigned: {collisions}"
                mutations = mutated_bases & modules.keys()
                assert not mutations, f"registration modules mutated: {mutations}"
            direct_registration_name = None
            allowed_reference_nodes: set[int] = set()
            _resolved_registration_calls(
                statement,
                registration_modules,
                names,
                modules,
                resolved_reference_nodes=allowed_reference_nodes,
            )
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                direct_registration_name = _resolve_registration_call(
                    statement.value,
                    registration_modules,
                    names,
                    modules,
                )
            value_references = _registration_value_references(
                statement,
                registration_modules,
                names,
                modules,
                allowed_reference_nodes=allowed_reference_nodes,
            )
            assert not value_references, (
                f"registrar cannot be used as a value: {sorted(value_references)}"
            )
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                assert not terminated, "registration import appears after unconditional exit"
                _bind_registration_import(
                    statement,
                    registration_modules,
                    names,
                    modules,
                    conditional=conditional,
                )
                continue

            values: list[ast.AST | None] = []
            targets: list[ast.expr] = []
            if isinstance(statement, ast.Assign):
                values = [statement.value]
                targets = statement.targets
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                values = [statement.value]
                targets = [statement.target]
            elif isinstance(statement, ast.Delete):
                targets = statement.targets
            if targets:
                nested_calls = [
                    name
                    for value in values
                    for name in _resolved_registration_calls(
                        value,
                        registration_modules,
                        names,
                        modules,
                    )
                ]
                assert not nested_calls, "registration calls must be direct statements"
                rebound = {name for target in targets for name in _assigned_names(target)}
                collisions = rebound & (names.keys() | modules.keys())
                assert not collisions, f"registration aliases reassigned: {collisions}"
                continue

            if isinstance(statement, ast.Expr):
                resolved = _resolved_registration_calls(
                    statement.value,
                    registration_modules,
                    names,
                    modules,
                )
                if not resolved:
                    continue
                direct = direct_registration_name
                assert not terminated, "registration call appears after unconditional exit"
                assert direct is not None and resolved == [direct], (
                    "registration calls must be direct standalone statements"
                )
                calls.append(direct)
                continue

            if isinstance(statement, ast.If):
                condition_calls = _resolved_registration_calls(
                    statement.test,
                    registration_modules,
                    names,
                    modules,
                )
                assert not condition_calls, "registration calls in conditions are unsupported"
                if isinstance(statement.test, ast.Constant):
                    selected = statement.body if bool(statement.test.value) else statement.orelse
                    calls.extend(
                        analyze_block(
                            selected,
                            names,
                            modules,
                            conditional=conditional,
                        )
                    )
                    continue
                assert not _contains_scope_exit(statement), (
                    "dynamic control flow can bypass registration calls"
                )
                branch_calls: list[str] = []
                for branch in (statement.body, statement.orelse):
                    branch_calls.extend(
                        analyze_block(
                            branch,
                            names.copy(),
                            modules.copy(),
                            conditional=True,
                        )
                    )
                assert not branch_calls, (
                    "registration calls under dynamic control flow are unsupported"
                )
                continue

            if isinstance(statement, ast.While) and isinstance(statement.test, ast.Constant):
                if not bool(statement.test.value):
                    calls.extend(
                        analyze_block(
                            statement.orelse,
                            names,
                            modules,
                            conditional=conditional,
                        )
                    )
                    continue

            control_blocks: list[list[ast.stmt]] = []
            control_expressions: list[ast.AST | None] = []
            if isinstance(statement, (ast.For, ast.AsyncFor)):
                control_expressions = [statement.iter]
                control_blocks = [statement.body, statement.orelse]
            elif isinstance(statement, ast.While):
                control_expressions = [statement.test]
                control_blocks = [statement.body, statement.orelse]
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                control_expressions = [item.context_expr for item in statement.items]
                control_blocks = [statement.body]
            elif isinstance(statement, (ast.Try, ast.TryStar)):
                control_blocks = [
                    statement.body,
                    *(handler.body for handler in statement.handlers),
                    statement.orelse,
                    statement.finalbody,
                ]
            elif isinstance(statement, ast.Match):
                control_expressions = [statement.subject]
                control_blocks = [case.body for case in statement.cases]
            if control_blocks:
                assert not _contains_scope_exit(statement), (
                    "dynamic control flow can bypass registration calls"
                )
                expression_calls = [
                    name
                    for expression in control_expressions
                    for name in _resolved_registration_calls(
                        expression,
                        registration_modules,
                        names,
                        modules,
                    )
                ]
                assert not expression_calls, (
                    "registration calls in control expressions are unsupported"
                )
                controlled_calls: list[str] = []
                for controlled_block in control_blocks:
                    controlled_calls.extend(
                        analyze_block(
                            controlled_block,
                            names.copy(),
                            modules.copy(),
                            conditional=True,
                        )
                    )
                assert not controlled_calls, (
                    "registration calls under dynamic control flow are unsupported"
                )
                continue

            if isinstance(statement, (ast.Return, ast.Raise)):
                exit_value = statement.value if isinstance(statement, ast.Return) else statement.exc
                exit_calls = _resolved_registration_calls(
                    exit_value,
                    registration_modules,
                    names,
                    modules,
                )
                assert not exit_calls, "registration calls in exits are unsupported"
                terminated = True
                continue

            unexpected_calls = _resolved_registration_calls(
                statement,
                registration_modules,
                names,
                modules,
            )
            assert not unexpected_calls, "registration call occurs in unsupported syntax"
        return calls

    return analyze_block(
        statements,
        active_names,
        active_modules,
        conditional=False,
    )


def _is_main_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name)
        and right.id == "__name__"
        and isinstance(left, ast.Constant)
        and left.value == "__main__"
    )


def _is_positive_graph_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.IsNot):
        return False
    left, right = test.left, test.comparators[0]
    return (
        isinstance(left, ast.Name)
        and left.id in {"graph_svc", "graph_service"}
        and isinstance(right, ast.Constant)
        and right.value is None
    ) or (
        isinstance(right, ast.Name)
        and right.id in {"graph_svc", "graph_service"}
        and isinstance(left, ast.Constant)
        and left.value is None
    )


def _tool_names_in_statements(statements: list[ast.stmt]) -> list[str]:
    names: list[str] = []

    class ToolVisitor(ast.NodeVisitor):
        def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            hidden = [
                name
                for child in ast.walk(node)
                if child is not node
                and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (name := _tool_name(child)) is not None
            ]
            assert not hidden, f"tool decorators hidden in unsupported helper scope: {hidden}"
            name = _tool_name(node)
            if name is not None:
                names.append(name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            hidden = [
                name
                for child in ast.walk(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (name := _tool_name(child)) is not None
            ]
            assert not hidden, f"tool decorators hidden in unsupported class scope: {hidden}"

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

    visitor = ToolVisitor()
    for statement in statements:
        visitor.visit(statement)
    return names


def _contains_scope_exit(node: ast.AST) -> bool:
    found = False

    class ExitVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Return(self, node: ast.Return) -> None:
            nonlocal found
            found = True

        def visit_Raise(self, node: ast.Raise) -> None:
            nonlocal found
            found = True

    ExitVisitor().visit(node)
    return found


def _direct_tool_registration_methods(node: ast.AST) -> set[str]:
    methods: set[str] = set()

    class RegistrationVisitor(ast.NodeVisitor):
        def _visit_arguments(self, arguments: ast.arguments) -> None:
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
            ):
                if argument.annotation is not None:
                    self.visit(argument.annotation)
            for argument in (arguments.vararg, arguments.kwarg):
                if argument is not None and argument.annotation is not None:
                    self.visit(argument.annotation)
            for default in (*arguments.defaults, *arguments.kw_defaults):
                if default is not None:
                    self.visit(default)

        def _visit_decorator(self, decorator: ast.expr) -> None:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if isinstance(target, ast.Attribute) and target.attr == "tool":
                if isinstance(decorator, ast.Call):
                    for argument in decorator.args:
                        self.visit(argument)
                    for keyword in decorator.keywords:
                        self.visit(keyword.value)
                return
            self.visit(decorator)

        def _visit_function_body(
            self,
            node: ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> None:
            for decorator in node.decorator_list:
                self._visit_decorator(decorator)
            self._visit_arguments(node.args)
            if node.returns is not None:
                self.visit(node.returns)
            for type_parameter in getattr(node, "type_params", ()):
                self.visit(type_parameter)
            for statement in node.body:
                self.visit(statement)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function_body(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function_body(node)

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "add_tool",
                "tool",
            }:
                methods.add(node.func.attr)
            self.generic_visit(node)

    RegistrationVisitor().visit(node)
    return methods


def _registration_method_alias_uses(node: ast.AST) -> set[str]:
    aliases: dict[str, str] = {}
    replaces_tool_method = False
    bindings: list[tuple[ast.expr, ast.AST]] = []

    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            bindings.extend((target, child.value) for target in child.targets)
            if any(
                isinstance(target, ast.Attribute) and target.attr == "tool"
                for target in child.targets
            ) and isinstance(child.value, ast.Name):
                replaces_tool_method = replaces_tool_method or child.value.id == "instrumented_tool"
        elif isinstance(child, ast.AnnAssign) and child.value is not None:
            bindings.append((child.target, child.value))
        elif isinstance(child, ast.NamedExpr):
            bindings.append((child.target, child.value))

    def bind_alias(target: ast.expr, value: ast.AST) -> bool:
        changed = False
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(value, (ast.Tuple, ast.List))
            and len(target.elts) == len(value.elts)
        ):
            for nested_target, nested_value in zip(target.elts, value.elts, strict=True):
                changed = bind_alias(nested_target, nested_value) or changed
            return changed

        method = None
        if isinstance(value, ast.Attribute) and value.attr in {"add_tool", "tool"}:
            method = value.attr
        elif isinstance(value, ast.Name):
            method = aliases.get(value.id)
        if method is None:
            return False
        for name in _assigned_names(target):
            if aliases.get(name) != method:
                aliases[name] = method
                changed = True
        return changed

    changed = True
    while changed:
        changed = False
        for target, value in bindings:
            changed = bind_alias(target, value) or changed

    safe_factory_aliases = (
        {"_original_tool"}
        if replaces_tool_method and aliases.get("_original_tool") == "tool"
        else set()
    )
    uses: set[str] = set()
    parents = {
        id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)
    }
    for child in ast.walk(node):
        if (
            not isinstance(child, ast.Name)
            or not isinstance(child.ctx, ast.Load)
            or child.id not in aliases
        ):
            continue
        alias = child.id
        parent = parents.get(id(child))
        is_safe_factory_call = (
            alias in safe_factory_aliases
            and isinstance(parent, ast.Call)
            and parent.func is child
            and bool(parent.args)
            and all(isinstance(argument, ast.Starred) for argument in parent.args)
            and bool(parent.keywords)
            and all(keyword.arg is None for keyword in parent.keywords)
        )
        if not is_safe_factory_call:
            uses.add(alias)
    return uses


def _registered_tools_in_scope(
    registration: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[str], set[str]]:
    names: list[str] = []
    graph_gated: set[str] = set()
    alias_uses = _registration_method_alias_uses(registration)
    assert not alias_uses, (
        f"direct MCP tool registration aliases are unsupported: {sorted(alias_uses)}"
    )

    def visit_block(statements: list[ast.stmt], *, graph_guarded: bool) -> None:
        terminated = False
        for index, statement in enumerate(statements):
            direct_registration_methods = _direct_tool_registration_methods(statement)
            assert not direct_registration_methods, (
                "direct MCP tool registration calls are unsupported: "
                f"{sorted(direct_registration_methods)}"
            )
            future_tools = _tool_names_in_statements(statements[index + 1 :])
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _tool_names_in_statements([statement])
                name = _tool_name(statement)
                if name is None:
                    continue
                assert not terminated, f"tool {name} appears after an unconditional scope exit"
                names.append(name)
                if graph_guarded:
                    graph_gated.add(name)
                continue
            if isinstance(statement, ast.ClassDef):
                _tool_names_in_statements([statement])
                continue
            if isinstance(statement, ast.If):
                if isinstance(statement.test, ast.Constant):
                    selected = statement.body if bool(statement.test.value) else statement.orelse
                    visit_block(selected, graph_guarded=graph_guarded)
                    continue
                body_tools = _tool_names_in_statements(statement.body)
                negative_tools = _tool_names_in_statements(statement.orelse)
                if future_tools and _contains_scope_exit(statement):
                    raise AssertionError("dynamic control flow can bypass later tool registration")
                if body_tools or negative_tools:
                    assert not terminated, "tool registration appears after a scope exit"
                    if _is_positive_graph_guard(statement.test):
                        assert not negative_tools, (
                            "tools in the negative graph branch are unsupported"
                        )
                        visit_block(statement.body, graph_guarded=True)
                        continue
                    raise AssertionError(
                        "tool registration under dynamic control flow is unsupported"
                    )
                continue
            if isinstance(statement, ast.While) and isinstance(statement.test, ast.Constant):
                if not bool(statement.test.value):
                    visit_block(statement.orelse, graph_guarded=graph_guarded)
                    continue
            if isinstance(
                statement,
                (
                    ast.For,
                    ast.AsyncFor,
                    ast.While,
                    ast.With,
                    ast.AsyncWith,
                    ast.Try,
                    ast.TryStar,
                    ast.Match,
                ),
            ):
                controlled_tools = _tool_names_in_statements([statement])
                if controlled_tools:
                    raise AssertionError(
                        "tool registration under dynamic control flow is unsupported"
                    )
                if future_tools and _contains_scope_exit(statement):
                    raise AssertionError("dynamic control flow can bypass later tool registration")
                continue
            if isinstance(statement, (ast.Return, ast.Raise)):
                terminated = True

    visit_block(registration.body, graph_guarded=False)
    return names, graph_gated


def _assert_no_tools_outside_registrations(
    tree: ast.Module,
    registration_nodes: set[ast.FunctionDef | ast.AsyncFunctionDef],
) -> None:
    alias_uses = _registration_method_alias_uses(tree)
    assert not alias_uses, (
        "direct MCP tool registration aliases outside a registration function "
        f"are unsupported: {sorted(alias_uses)}"
    )
    for statement in tree.body:
        if statement in registration_nodes:
            continue
        direct_registration_methods = _direct_tool_registration_methods(statement)
        assert not direct_registration_methods, (
            "direct MCP tool registration calls outside a registration function "
            f"are unsupported: {sorted(direct_registration_methods)}"
        )
        tool_names = _tool_names_in_statements([statement])
        assert not tool_names, (
            f"MCP tool decorators outside a registration function are unsupported: {tool_names}"
        )


def _registered_tool_names() -> tuple[set[str], set[str]]:
    module_trees: list[tuple[str, ast.Module]] = []
    registration_modules: dict[str, str] = {}
    registration_nodes: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    source_root = REPO_ROOT / "src"
    tools_root = source_root / "brain_v42" / "mcp" / "tools"
    for path in sorted(tools_root.rglob("*.py")):
        module_name = ".".join(path.relative_to(source_root).with_suffix("").parts)
        tree = ast.parse(path.read_text())
        module_trees.append((module_name, tree))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not (
                node.name == "register_tools"
                or (node.name.startswith("register_") and node.name.endswith("_tools"))
            ):
                continue
            assert node.name not in registration_nodes, (
                f"duplicate registration function {node.name}"
            )
            registration_nodes[node.name] = node
            registration_modules[node.name] = module_name
        _assert_no_tools_outside_registrations(
            tree,
            {
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and registration_nodes.get(node.name) is node
            },
        )

    registrations: dict[
        str,
        tuple[
            ast.FunctionDef | ast.AsyncFunctionDef,
            dict[str, str],
            dict[str, str],
        ],
    ] = {}
    for module_name, tree in module_trees:
        name_bindings, module_bindings = _top_level_registration_bindings(
            tree.body,
            registration_modules,
        )
        local_registrations = {
            name
            for name, defining_module in registration_modules.items()
            if defining_module == module_name
        }
        rebound_names = _local_registration_rebindings(
            tree.body,
            local_registrations,
            registration_nodes,
        )
        assert not local_registrations & rebound_names, (
            "local registration functions must not be rebound"
        )
        name_bindings.update({name: name for name in local_registrations})
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if registration_nodes.get(node.name) is not node:
                continue
            registrations[node.name] = (
                node,
                name_bindings,
                module_bindings,
            )

    server_tree = ast.parse(SERVER)
    main_guards = [
        node for node in server_tree.body if isinstance(node, ast.If) and _is_main_guard(node.test)
    ]
    assert len(main_guards) == 1, f"expected one exact __main__ guard, got {len(main_guards)}"
    main_guard = main_guards[0]
    # The entrypoint delegates its whole wiring to ``build_server()`` so the e2e
    # harness can CALL it instead of reproducing it. Seed from both bodies: the
    # union keeps this census true if a registration is ever re-added to
    # ``__main__``, instead of silently dropping it.
    build_server_defs = [
        node
        for node in server_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_server"
    ]
    assert len(build_server_defs) == 1, (
        f"expected exactly one build_server definition, got {len(build_server_defs)}"
    )
    wiring_body = [*main_guard.body, *build_server_defs[0].body]
    _assert_no_tools_outside_registrations(
        ast.Module(body=wiring_body, type_ignores=[]),
        set(),
    )
    server_name_bindings, server_module_bindings = _top_level_registration_bindings(
        server_tree.body,
        registration_modules,
    )
    pending = _registration_calls(
        wiring_body,
        registration_modules,
        server_name_bindings,
        server_module_bindings,
    )
    reachable: set[str] = set()
    while pending:
        registration_name = pending.pop()
        if registration_name in reachable:
            continue
        reachable.add(registration_name)
        registration, name_bindings, module_bindings = registrations[registration_name]
        arguments = [
            *registration.args.posonlyargs,
            *registration.args.args,
            *registration.args.kwonlyargs,
        ]
        shadowed = {argument.arg for argument in arguments}
        if registration.args.vararg is not None:
            shadowed.add(registration.args.vararg.arg)
        if registration.args.kwarg is not None:
            shadowed.add(registration.args.kwarg.arg)
        pending.extend(
            _registration_calls(
                registration.body,
                registration_modules,
                {alias: target for alias, target in name_bindings.items() if alias not in shadowed},
                {
                    alias: target
                    for alias, target in module_bindings.items()
                    if alias not in shadowed
                },
            )
        )

    names: list[str] = []
    graph_gated: set[str] = set()
    for registration_name in reachable:
        registration, _, _ = registrations[registration_name]
        registration_names_in_scope, graph_names_in_scope = _registered_tools_in_scope(registration)
        names.extend(registration_names_in_scope)
        graph_gated.update(graph_names_in_scope)

    assert len(names) == len(set(names)), "registered MCP tool names must be unique"
    return set(names), graph_gated


def _locked_version(package_name: str) -> str:
    with (REPO_ROOT / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)
    versions = {
        package["version"] for package in lock["package"] if package["name"] == package_name
    }
    assert len(versions) == 1
    return versions.pop()


def _environment_assignment_keys(block: str) -> list[str]:
    return re.findall(
        r"^[ \t]*(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=",
        block,
        flags=re.MULTILINE,
    )


def _synthetic_registration(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    function = ast.parse(source).body[0]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    return function


def test_registration_call_analysis_rejects_unbound_attribute_registrar() -> None:
    statements = ast.parse("object.register_tools()").body

    with pytest.raises(AssertionError, match="unresolved registrar call"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {},
        )


def test_registration_call_analysis_rejects_nested_unbound_attribute_registrar() -> None:
    statements = ast.parse("provider.tools.register_tools()").body

    with pytest.raises(AssertionError, match="unresolved registrar call"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {},
        )


def test_registration_call_analysis_rejects_alias_reassignment() -> None:
    statements = ast.parse(
        """
from brain_v42.mcp.tools.brain_tools import register_tools as register
register = object()
register()
"""
    ).body

    with pytest.raises(AssertionError, match="reassigned"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {},
        )


def test_registration_call_analysis_rejects_registrar_used_as_a_value() -> None:
    statements = ast.parse(
        """
registrar_alias = register_tools
registrar_alias()
"""
    ).body

    with pytest.raises(AssertionError, match="registrar.*value"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {"register_tools": "register_tools"},
        )


def test_registration_call_analysis_rejects_registrar_passed_to_a_helper() -> None:
    statements = ast.parse("invoke(register_tools)").body

    with pytest.raises(AssertionError, match="registrar.*value"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {"register_tools": "register_tools"},
        )


@pytest.mark.parametrize(
    "source",
    (
        "def helper(value=register_tools()):\n    pass",
        "@decorate(register_tools)\ndef helper():\n    pass",
        "class Helper(register_tools):\n    pass",
        "class Helper:\n    register_tools()",
    ),
)
def test_registration_call_analysis_rejects_definition_time_registrar_use(
    source: str,
) -> None:
    statements = ast.parse(source).body

    with pytest.raises(AssertionError, match="registrar.*value"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {"register_tools": "register_tools"},
        )


def test_registration_call_analysis_rejects_local_helper_shadowing_alias() -> None:
    statements = ast.parse(
        """
from brain_v42.mcp.tools.brain_tools import register_tools as register
def register():
    return None
register()
"""
    ).body

    with pytest.raises(AssertionError, match="reassigned"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {},
        )


@pytest.mark.parametrize(
    "shadow",
    (
        "def register():\n    return None",
        "class register:\n    pass",
        "for register in []:\n    pass",
    ),
)
def test_top_level_binding_analysis_rejects_alias_shadowing(shadow: str) -> None:
    statements = ast.parse(
        f"from brain_v42.mcp.tools.brain_tools import register_tools as register\n{shadow}\n"
    ).body

    with pytest.raises(AssertionError, match="reassigned"):
        _top_level_registration_bindings(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
        )


def test_top_level_binding_analysis_ignores_comprehension_local_target() -> None:
    statements = ast.parse(
        "from brain_v42.mcp.tools.brain_tools import register_tools as register\n"
        "values = [register for register in []]\n"
    ).body

    name_bindings, module_bindings = _top_level_registration_bindings(
        statements,
        {"register_tools": "brain_v42.mcp.tools.brain_tools"},
    )

    assert name_bindings == {"register": "register_tools"}
    assert module_bindings == {}


def test_top_level_binding_analysis_rejects_qualified_registrar_mutation() -> None:
    statements = ast.parse(
        "import brain_v42.mcp.tools.brain_tools as brain_tools\n"
        "brain_tools.register_tools = replacement\n"
    ).body

    with pytest.raises(AssertionError, match="mutated"):
        _top_level_registration_bindings(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
        )


def test_registration_analysis_rejects_unresolved_registrar_import() -> None:
    statements = ast.parse("from brain_v42.mcp.tools.missing import register_missing_tools\n").body

    with pytest.raises(AssertionError, match="unresolved registrar"):
        _top_level_registration_bindings(statements, {})


def test_registration_call_analysis_rejects_dynamic_control_flow() -> None:
    statements = ast.parse(
        """
if settings.experimental:
    register_tools()
"""
    ).body

    with pytest.raises(AssertionError, match="control flow"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {"register_tools": "register_tools"},
        )


def test_registration_call_analysis_accepts_valid_qualified_import() -> None:
    statements = ast.parse(
        """
import brain_v42.mcp.tools.brain_tools as brain_tools
brain_tools.register_tools()
"""
    ).body

    assert _registration_calls(
        statements,
        {"register_tools": "brain_v42.mcp.tools.brain_tools"},
        {},
    ) == ["register_tools"]


def test_registration_call_analysis_rejects_conditional_early_exit() -> None:
    statements = ast.parse(
        """
if settings.experimental:
    return_value = None
    raise RuntimeError
register_tools()
"""
    ).body

    with pytest.raises(AssertionError, match="bypass registration"):
        _registration_calls(
            statements,
            {"register_tools": "brain_v42.mcp.tools.brain_tools"},
            {"register_tools": "register_tools"},
        )


def test_tool_analysis_rejects_non_graph_conditional_registration() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp, settings):
    if settings.experimental:
        @mcp.tool()
        def brain_experimental():
            return None
"""
    )

    with pytest.raises(AssertionError, match="control flow"):
        _registered_tools_in_scope(registration)


def test_tool_analysis_rejects_decorator_hidden_in_helper_scope() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp):
    def helper():
        @mcp.tool()
        def brain_hidden():
            return None
    helper()
"""
    )

    with pytest.raises(AssertionError, match="hidden"):
        _registered_tools_in_scope(registration)


def test_module_tool_analysis_rejects_top_level_helper_registration() -> None:
    tree = ast.parse(
        """
def helper(mcp):
    @mcp.tool()
    def brain_hidden():
        return None

def register_tools(mcp):
    helper(mcp)
"""
    )
    registration = tree.body[1]
    assert isinstance(registration, ast.FunctionDef)

    with pytest.raises(AssertionError, match="hidden|outside a registration function"):
        _assert_no_tools_outside_registrations(tree, {registration})


def test_server_guard_analysis_rejects_direct_tool_decorator() -> None:
    guard = ast.parse(
        """
if __name__ == "__main__":
    @mcp.tool()
    def brain_hidden():
        return None
"""
    ).body[0]
    assert isinstance(guard, ast.If)

    with pytest.raises(AssertionError, match="outside a registration function"):
        _assert_no_tools_outside_registrations(
            ast.Module(body=guard.body, type_ignores=[]),
            set(),
        )


@pytest.mark.parametrize(
    "registration_call",
    ("mcp.add_tool(handler)", "mcp.tool(handler)"),
)
def test_tool_analysis_rejects_direct_registration_calls(
    registration_call: str,
) -> None:
    registration = _synthetic_registration(
        f"""
def register_tools(mcp, handler):
    {registration_call}
"""
    )

    with pytest.raises(AssertionError, match="direct MCP tool registration"):
        _registered_tools_in_scope(registration)


@pytest.mark.parametrize(
    "source",
    (
        "tool_alias = mcp.tool\n    @tool_alias()\n    def brain_hidden():\n        pass",
        "tool_alias: object = mcp.tool\n    @tool_alias()\n    def brain_hidden():\n        pass",
        (
            "tool_alias, other = mcp.tool, handler\n"
            "    @tool_alias()\n"
            "    def brain_hidden():\n"
            "        pass"
        ),
        "(tool_alias := mcp.tool)\n    @tool_alias()\n    def brain_hidden():\n        pass",
        "add_alias = mcp.add_tool\n    add_alias(handler)",
    ),
)
def test_tool_analysis_rejects_registration_method_aliases(source: str) -> None:
    registration = _synthetic_registration(
        f"""
def register_tools(mcp, handler):
    {source}
"""
    )

    with pytest.raises(AssertionError, match="direct MCP tool registration"):
        _registered_tools_in_scope(registration)


def test_module_tool_analysis_rejects_alias_used_in_a_later_statement() -> None:
    tree = ast.parse(
        """
tool_alias = mcp.tool

@tool_alias()
def brain_hidden():
    pass
"""
    )

    with pytest.raises(AssertionError, match="direct MCP tool registration"):
        _assert_no_tools_outside_registrations(tree, set())


@pytest.mark.parametrize(
    "source",
    (
        "def helper(value=mcp.add_tool(handler)):\n    pass",
        "@mcp.add_tool(handler)\ndef helper():\n    pass",
    ),
)
def test_module_tool_analysis_rejects_definition_time_direct_registration(
    source: str,
) -> None:
    tree = ast.parse(source)

    with pytest.raises(AssertionError, match="direct MCP tool registration"):
        _assert_no_tools_outside_registrations(tree, set())


def test_tool_analysis_rejects_dynamic_public_name() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp):
    @mcp.tool(name=PUBLIC_NAME)
    def brain_dynamic():
        return None
"""
    )

    with pytest.raises(AssertionError, match="literal string"):
        _registered_tools_in_scope(registration)


@pytest.mark.parametrize(
    "decorator",
    (
        "@mcp.tool(PUBLIC_NAME)",
        "@mcp.tool(name_or_fn=PUBLIC_NAME)",
        "@mcp.tool(**OPTIONS)",
    ),
)
def test_tool_analysis_rejects_unresolved_decorator_arguments(decorator: str) -> None:
    registration = _synthetic_registration(
        f"""
def register_tools(mcp):
    {decorator}
    def brain_dynamic():
        return None
"""
    )

    with pytest.raises(AssertionError, match="literal|string|unsupported"):
        _registered_tools_in_scope(registration)


def test_tool_analysis_accepts_literal_positional_public_name() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp):
    @mcp.tool("brain_public")
    def brain_internal():
        return None
"""
    )

    assert _registered_tools_in_scope(registration) == (["brain_public"], set())


def test_tool_analysis_accepts_literal_name_or_fn_public_name() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp):
    @mcp.tool(name_or_fn="brain_public")
    def brain_internal():
        return None
"""
    )

    assert _registered_tools_in_scope(registration) == (["brain_public"], set())


@pytest.mark.parametrize(
    "shadow",
    (
        "for register_tools in []:\n    pass",
        "with context() as register_tools:\n    pass",
        "try:\n    pass\nexcept Error as register_tools:\n    pass",
        "match value:\n    case register_tools:\n        pass",
        "import replacement as register_tools",
        "class register_tools:\n    pass",
    ),
)
def test_local_registration_rebinding_analysis_covers_all_bindings(shadow: str) -> None:
    tree = ast.parse(f"def register_tools():\n    pass\n{shadow}\n")
    registration = tree.body[0]
    assert isinstance(registration, ast.FunctionDef)

    rebound = _local_registration_rebindings(
        tree.body,
        {"register_tools"},
        {"register_tools": registration},
    )

    assert rebound == {"register_tools"}


def test_tool_analysis_ignores_graph_checks_without_registrations() -> None:
    registration = _synthetic_registration(
        """
def register_tools(graph_svc):
    if graph_svc is None:
        log_disabled()
"""
    )

    assert _registered_tools_in_scope(registration) == ([], set())


def test_tool_analysis_rejects_negative_graph_registration() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp, graph_svc):
    if graph_svc is not None:
        log_enabled()
    else:
        @mcp.tool()
        def brain_without_graph():
            return None
"""
    )

    with pytest.raises(AssertionError, match="negative graph branch"):
        _registered_tools_in_scope(registration)


def test_tool_analysis_rejects_compound_graph_guard() -> None:
    registration = _synthetic_registration(
        """
def register_tools(mcp, graph_svc, settings):
    if graph_svc is not None and settings.experimental:
        @mcp.tool()
        def brain_experimental_graph():
            return None
"""
    )

    with pytest.raises(AssertionError, match="control flow"):
        _registered_tools_in_scope(registration)


def test_main_guard_requires_exact_module_equality() -> None:
    exact = ast.parse('if __name__ == "__main__":\n    run()').body[0]
    substring = ast.parse('if "__name__" in marker:\n    run()').body[0]
    assert isinstance(exact, ast.If)
    assert isinstance(substring, ast.If)

    assert _is_main_guard(exact.test)
    assert not _is_main_guard(substring.test)


def test_environment_assignment_parser_preserves_duplicates_and_indentation() -> None:
    assignments = _environment_assignment_keys(
        "  GRAPH_PROJECTOR_ENABLED=true\nexport GRAPH_PROJECTOR_ENABLED=false\n"
    )

    assert assignments == ["GRAPH_PROJECTOR_ENABLED", "GRAPH_PROJECTOR_ENABLED"]


def test_repository_head_047_is_documented_without_claiming_a_deployed_head() -> None:
    """The repository head is a fact this repository owns. The deployed head is not.

    Until 2026-08-04 these docs asserted a production head of `037` while the
    database had been on `039` for three days. No page can prove a live head, so
    the docs now name the repository target and send the reader to measure the
    rest.

    The head in this test's NAME is deliberate: bumping the repository head cannot
    be done without renaming the guard, which is what stops it from drifting
    silently. Bumped to 045 on 2026-08-16 — production measured at 045 the same
    day, right after applying it: column at 120, `codex_dream_run_v1` recreated
    with its `codex_ro` grant, 32 tables unchanged. Previously bumped to 043 on
    2026-08-10 — production measured at 043 the same day, right after applying
    it, with zero rows stamped (no backfill). Before that, 042 on 2026-08-08 —
    production was still measured at 041 at that moment, and SCHEMA.md says so
    in the same breath.
    """
    assert _repository_head() == "047"
    assert "47 révisions (001 → 047)" in SCHEMA
    assert "| 038 |" in SCHEMA
    assert "| 039 |" in SCHEMA
    assert "| 040 |" in SCHEMA
    assert "| 041 |" in SCHEMA
    assert "| 044 |" in SCHEMA
    assert "| 046 |" in SCHEMA
    assert "| 047 |" in SCHEMA
    assert "Un schéma neuf au head 047 contient 32 tables `public`" in SCHEMA

    schema_normalized = " ".join(SCHEMA.split())
    assert "La cible du dépôt est 047." in schema_normalized
    assert "La révision 047 est la tête du dépôt." in schema_normalized
    assert "select version_num from alembic_version" in schema_normalized

    # The retired claim must not come back in any of the core documents.
    for document in (README, ARCHITECTURE, MCP_TOOLS, SCHEMA):
        normalized = " ".join(document.split())
        assert "la production reste à 037" not in normalized
        assert "Production remains at 037" not in normalized


def test_documented_migration_head_matches_repository() -> None:
    head = _repository_head()

    assert f"migrations 001–{head} defined" in ARCHITECTURE
    assert f"migration {head}" in README.lower()
    if CLAUDE:
        assert f"migration {head}" in CLAUDE.lower()
    assert f"migration {head}" in MCP_TOOLS.lower()


def test_documented_tool_totals_match_registered_tools() -> None:
    names, graph_gated_names = _registered_tool_names()
    always_on = len(names - graph_gated_names)
    graph_gated = len(graph_gated_names)
    total = len(names)
    contract = f"{always_on} always-on + {graph_gated} graph-gated = {total}"

    readme_table = README.split("## MCP tools", maxsplit=1)[1].split("Full catalog", maxsplit=1)[0]
    documented_name_list = re.findall(r"`(brain_[a-z0-9_]+)`", readme_table)
    documented_names = set(documented_name_list)
    registry_line = next(
        line for line in MCP_TOOLS.splitlines() if line.startswith("**Repository registry:**")
    )
    documented_graph_gated = set(re.findall(r"`(brain_[a-z0-9_]+)`", registry_line))

    assert contract in MCP_TOOLS
    assert MCP_TOOLS.count(contract) == 2
    assert ARCHITECTURE.count(contract) == 4
    assert len(documented_name_list) == len(documented_names)
    assert documented_names == names
    assert documented_graph_gated == graph_gated_names


def test_workflow_guide_is_discoverable_by_the_documentation_contract() -> None:
    """A workflow-guide decorator must stay in a recognized registrar scope."""
    names, _ = _registered_tool_names()

    assert "brain_workflow_guide" in names


def test_documented_transport_matches_default_and_production_client() -> None:
    transport_default = Settings.model_fields["brain_mcp_transport"].default
    mcp_config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    production = mcp_config["mcpServers"]["brain-v42"]

    assert transport_default == "stdio"
    assert production["type"] == "http"
    production_url = production["url"]
    parsed_url = urlsplit(production_url)
    assert parsed_url.scheme == "http"
    assert parsed_url.hostname in LOOPBACK_HOSTS
    assert parsed_url.port == Settings.model_fields["mcp_http_port"].default
    assert parsed_url.path == "/mcp"
    assert not parsed_url.username
    assert not parsed_url.password
    assert not parsed_url.query
    assert not parsed_url.fragment
    transport_section = ARCHITECTURE.split("## Transport", maxsplit=1)[1]
    json_block = re.search(r"```json\s*(.*?)\s*```", transport_section, re.DOTALL)
    assert json_block is not None
    documented_client = json.loads(json_block.group(1))["mcpServers"]["brain-v42"]
    assert documented_client == production
    contract = (
        f"**MCP transport**: production = HTTP loopback `{production_url}`; "
        "configuration default and dev/fallback = `stdio`."
    )
    assert contract in README
    if CLAUDE:
        assert contract in CLAUDE
    assert production_url in ARCHITECTURE
    assert "### Dev/fallback: stdio" in ARCHITECTURE
    for stale_claim in ("in-process MCP server", "MCP server in-process"):
        assert stale_claim not in ARCHITECTURE
    assert "mode http dormant" not in README.lower()
    assert "dormant http" not in ARCHITECTURE.lower()
    assert "dormant http" not in MCP_TOOLS.lower()


def test_documented_reranker_uses_unified_embedding_endpoint() -> None:
    reranker_default = Settings.model_fields["reranker_url"].default
    embedding_default = Settings.model_fields["embedding_service_url"].default
    assert isinstance(reranker_default, str)
    assert isinstance(embedding_default, str)
    assert reranker_default.rstrip("/") == embedding_default.rstrip("/")
    port = urlsplit(reranker_default).port
    assert port is not None
    contract = f"unified embedding endpoint `:{port}/rerank`"

    assert contract in README
    if CLAUDE:
        assert contract in CLAUDE
    assert f"| Reranker | {port} |" in ARCHITECTURE
    for document in (README, CLAUDE, ARCHITECTURE):
        assert "reranker service :8004" not in document.lower()
        assert "reranker**: shared cross-encoder http service on :8004" not in document.lower()


def test_documented_embedding_topology_matches_restored_local_default() -> None:
    endpoint = Settings.model_fields["embedding_service_url"].default
    assert endpoint == "http://localhost:8003"
    contract = (
        f"**Embedding topology**: production/default = local unified endpoint `{endpoint}`; "
        "`deploy/dev-pc` is a superseded rollback/reference path."
    )

    for document in _docs_including_claude(README, ARCHITECTURE):
        assert contract in document
        assert "192.168.1.11:8003" not in document
    assert endpoint in SCHEMA
    assert "192.168.1.11:8003" not in SCHEMA
    assert "SUPERSEDED FOR ACTIVE BRAIN TRAFFIC" in DEV_PC_RUNBOOK
    assert "is the backbone of the whole ReD ecosystem" not in DEV_PC_RUNBOOK
    assert "(and every `brain_*` consumer) keeps" not in DEV_PC_RUNBOOK


def test_documented_network_boundary_matches_tracked_bindings() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())

    published_ports = {
        (service_name, str(binding))
        for service_name, service in compose["services"].items()
        for binding in service.get("ports", [])
    }
    assert published_ports == {
        ("postgres", "127.0.0.1:5433:5432"),
        ("embedding", "127.0.0.1:8003:8003"),
        ("embedding-shim", "127.0.0.1:8003:8003"),
        ("neo4j", "127.0.0.1:7474:7474"),
        ("neo4j", "127.0.0.1:7687:7687"),
    }
    assert all(service.get("network_mode") != "host" for service in compose["services"].values())
    for field_name in ("mcp_http_host", "metrics_host", "automation_host"):
        assert Settings.model_fields[field_name].default in LOOPBACK_HOSTS

    target_contract = (
        "**Tracked network boundary** (replayed 2026-08-23): MCP, PostgreSQL and Neo4j bind to "
        "loopback; metrics and automation default to loopback. The versioned Compose target "
        "binds the embedding host publish to loopback and the live runtime matches it — "
        "measured `127.0.0.1:8003`, with the host's own LAN address refusing the connection. "
        "Application bearer authentication is armed and enforcing: `MCP_HTTP_TOKEN` is set and "
        "non-empty in the live server process, and `POST /mcp` answers `401` both without a "
        "bearer and with a wrong one. The dedicated Docker client network exists and carries "
        "the clients: `brain-net` holds the embedding shim and both `auto-discord` containers. "
        "Repository-managed WAN isolation remains unproven — the repository manages no "
        "firewall rule at all. What would make this paragraph false again, and is watched by "
        "no test: a host-publish override reopening `:8003`, `METRICS_HOST` set off-loopback "
        "(no validator guards it), or `MCP_HTTP_TOKEN` cleared. Re-measure with `ss -ltnp`, "
        "`docker port` and an unauthenticated `POST /mcp` — do not copy this line forward."
    )
    limits_contract = (
        "**Embedding shim limits (ROLLED OUT 2026-08-21, temps 1)**: 8 MiB body, 5 s body-read "
        "timeout, 8 concurrent ingress reads, 100 embed texts, 128 rerank candidates, maximum "
        "JSON depth 64, one embedding calculation and one rerank calculation per worker. "
        "Saturation returns short `503` JSON with `Retry-After: 1`."
    )
    residual_contract = (
        "**SEC2 residuals** (replayed 2026-08-23): bearer authentication and the dedicated "
        "Docker client network are done — the coordinated `auto-discord` cutover happened, and "
        "both `auto-discord` containers sit on `brain-net`. One residual stands, and it is "
        "wider than previously written: the versioned legacy PyTorch profile remains unbounded "
        "— `services/embedding/main.py` carries no body cap, no read deadline, no concurrency "
        "semaphore and no `413`/`503` — and it preserves neither of the two DNS names its "
        "clients use. A `--profile legacy` rollback publishes `embedding` and "
        "`brain_v42_embedding` on `brain-net`, while the compose sets "
        "`EMBEDDING_URL=http://embedding-shim:8003` and the running bot, carrying no "
        "`EMBEDDING_URL` of its own, falls back to the code default "
        "`http://brain_v42_embedding_shim:8003`. Two names break, not one."
    )
    # The full three-contract detail moved to docs/OPERATIONS.md when README
    # was replaced by the open-source-facing draft (ticket bdc4db73):
    # README's own Network trust model section keeps a shorter summary, not
    # this exact operator-facing wording.
    for document in _docs_including_claude(ARCHITECTURE):
        assert target_contract in document
        assert limits_contract in document
        assert residual_contract in document
    for contract in (target_contract, limits_contract, residual_contract):
        assert contract in OPERATIONS
    if CLAUDE:
        claude_normalized = " ".join(CLAUDE.split())
        assert "le runtime automation dédié sont contraints au loopback" in claude_normalized
        assert (
            "Les métriques y sont liées par défaut mais leur bind reste configurable"
            in claude_normalized
        )
        assert "sa route `/gitlab/webhook` partage ce bind" in claude_normalized
        assert (
            "métriques et automation y sont liées par défaut mais restent" not in claude_normalized
        )


def test_documented_fastmcp_major_matches_lock() -> None:
    major = _locked_version("fastmcp").split(".", maxsplit=1)[0]

    assert f"FastMCP {major}.x" in README
    if CLAUDE:
        assert f"FastMCP {major}.x" in CLAUDE


def test_graph_cutover_summary_matches_authoritative_runbook() -> None:
    assert "**Statut : ACTIF — CUTOVER PRODUCTION VALIDÉ LE 22 JUILLET 2026**" in GRAPH_RUNBOOK
    assert "canonical path is active in production since 22 July 2026" in README
    assert (
        "**Production graph state:** cutover validated at head 035 on 22 July 2026" in ARCHITECTURE
    )

    stale_claims = (
        "repository migrations 033–035, dormant",
        "The implementation is **not authorized for activation**",
        "live instance observed on 21 July 2026 is\n  still at head 032",
    )
    for claim in stale_claims:
        assert claim not in ARCHITECTURE

    if CLAUDE:
        claude_configuration = CLAUDE.split("## Configuration", maxsplit=1)[1]
        config_blocks = re.findall(r"```bash\n(.*?)```", claude_configuration, flags=re.DOTALL)
        assert len(config_blocks) >= 2
        shared_config, private_projector = config_blocks[:2]

        assert "GRAPH_ENABLED=true" in shared_config
        assert "GRAPH_LEDGER_WRITE_ENABLED=true" in shared_config
        assert "\nNEO4J_URL=" not in shared_config
        assert "\nNEO4J_USER=" not in shared_config
        assert "\nNEO4J_PASSWORD=" not in shared_config
        shared_key_list = _environment_assignment_keys(shared_config)
        shared_keys = set(shared_key_list)
        assert len(shared_key_list) == len(shared_keys)
        assert not {key for key in shared_keys if key.startswith("GRAPH_PROJECTOR_")}
        private_key_list = _environment_assignment_keys(private_projector)
        private_keys = set(private_key_list)
        assert len(private_key_list) == len(private_keys)
        assert private_keys == {
            "GRAPH_PROJECTOR_ENABLED",
            "GRAPH_PROJECTOR_NEO4J_PASSWORD",
            "GRAPH_PROJECTOR_NEO4J_URL",
            "GRAPH_PROJECTOR_NEO4J_USER",
        }


def test_post_037_production_truth_is_consistent_and_fail_closed() -> None:
    # The four core documents state what 24 July 2026 actually established —
    # lifecycle v4 is live — and stop short of naming a deployed Alembic head,
    # which none of them can prove. They pinned `037` for three days after
    # production had moved to `039`; the head is now measured, not narrated.
    for document in (README, ARCHITECTURE, MCP_TOOLS):
        assert "Alembic head `037` is active" not in document
        assert "Production runs Alembic head `037`" not in document
    assert "measure it, do not read it here" in README
    assert "select version_num from alembic_version" in ARCHITECTURE
    assert "Lifecycle v4 has run in production since 24 July 2026" in MCP_TOOLS

    schema_normalized = " ".join(SCHEMA.split())
    if CLAUDE:
        claude_normalized = " ".join(CLAUDE.split())
        assert "Il est actif en production depuis le 24 juillet 2026" in claude_normalized
    assert (
        "Le lifecycle v4 qu'elle porte tourne en production depuis le 24 juillet 2026"
        in schema_normalized
    )

    # 2026-08-04 corrected README/ARCHITECTURE/MCP_TOOLS/SCHEMA to stop naming a deployed
    # head. The gateway and graph runbooks encode operational *gates* (commands an operator
    # runs and compares against a hard-coded number), not descriptive prose: a hard-coded
    # gate goes stale at the next migration and, worse, tells an operator today's `040` is an
    # anomaly. These gates must now measure the deployed head instead of asserting `037`.
    gateway_normalized = " ".join(CODEX_GATEWAY.split())
    assert (
        "La tête du dépôt est `039`; la production observée reste à `037`."
        not in gateway_normalized
    )
    assert (
        "la production actuelle doit annoncer le head Alembic effectivement déployé, mesuré "
        "immédiatement avant la procédure" in gateway_normalized
    )
    assert (
        "la production actuelle doit annoncer exactement `037`, son descendant validé"
        not in gateway_normalized
    )
    assert (
        "`alembic current` doit annoncer le head déployé, mesuré avant la procédure"
        in gateway_normalized
    )
    assert "sans marqueur `(head)`" not in gateway_normalized
    assert (
        "La migration 037 descend de 036 et conserve les dix vues requises par la gateway."
        in gateway_normalized
    )
    assert (
        "Ce runbook n'applique aucune migration Alembic au-delà de ce que la production porte "
        "déjà." in gateway_normalized
    )
    assert (
        "Ce runbook n'applique aucune migration Alembic, ni 038 ni 039." not in gateway_normalized
    )
    assert "doit annoncer exactement `037 (head)`" not in CODEX_GATEWAY
    assert "ne downgradez jamais vers 036" in CODEX_GATEWAY
    assert (
        "Conservez le head déployé — mesuré avant le rollback, jamais recopié d'une exécution "
        "précédente — pendant ce rollback" in gateway_normalized
    )
    assert "Conservez le head déployé `037` pendant ce rollback" not in gateway_normalized
    assert "Le rollback de la gateway n'autorise aucune migration Alembic" in CODEX_GATEWAY
    assert "sans annoncer `037`" not in CODEX_GATEWAY
    assert "Conservez la migration `036` pendant ce rollback" not in CODEX_GATEWAY

    # Ticket 8285215c : le head n'est plus une constante NULLE PART, ni en prose ni
    # dans le CLI. Ces deux phrases étaient exactes tant que le script encodait
    # `037` ; elles sont devenues fausses le jour où il a cessé de le faire.
    assert "la production exactement à `037`" not in gateway_normalized
    assert "`alembic_revision=037`" not in gateway_normalized
    assert "n'accepte ni `038` ni `039` implicitement" not in gateway_normalized
    assert (
        "la production exactement au head que VOUS avez déclaré — mesuré immédiatement "
        "avant la procédure, jamais recopié" in gateway_normalized
    )
    # La procédure est INEXÉCUTABLE si le runbook oublie l'argument requis.
    # La forme d'INVOCATION, pas la mention : la prose cite aussi l'argument, et
    # compter les mentions ferait passer le test avec trois blocs sur quatre corrigés.
    assert gateway_normalized.count('--expected-alembic-revision "$DEPLOYED_HEAD"') == 4, (
        "les quatre invocations du CLI doivent toutes déclarer le head mesuré"
    )

    graph_normalized = " ".join(GRAPH_RUNBOOK.split())
    assert "**Acquis au head 037.**" in GRAPH_RUNBOOK
    assert "DR-v5 `20260724_150315`" in GRAPH_RUNBOOK
    assert "**Ouvert pour le head 037.**" not in GRAPH_RUNBOOK
    assert "ne jamais downgrader pour fermer ce gate" in graph_normalized
    assert "Un rollback du runtime graph n'autorise aucun downgrade Alembic" in graph_normalized
    assert "Toute nouvelle recovery ou tout nouveau rebuild doit revalider" in graph_normalized
    assert "interdit une nouvelle recovery ou un nouveau rebuild" not in graph_normalized
    assert "Preuve historique au head 035" in graph_normalized
    assert "ne ferment pas le rebuild Neo4j dédié de DR-v5" in graph_normalized

    # The DR-v5 drill's own proof stays pinned to the head it was actually run against
    # ("alors courante" — the production that was current *then*) — rewriting it to a later
    # number would fabricate a restore that never happened. Only the *procedural templates*
    # (Cutover futur / Restore / Rollback) that told a future operator to expect `037` on
    # "la production courante" (today) are wrong and must become measure-before-you-act.
    assert "pour la production alors courante, au head 037" in graph_normalized
    assert graph_normalized.count("pour la production alors courante, au head 037") == 2
    assert "pour la production courante au head 037" not in graph_normalized
    assert "(`037` sur la production courante)" not in GRAPH_RUNBOOK
    assert (
        "le head exactement déployé — mesuré avant la fenêtre, jamais recopié d'une exécution "
        "précédente" in graph_normalized
    )
    assert (
        "un restore PostgreSQL au head exactement déployé — mesuré avant la procédure, jamais "
        "recopié d'une exécution précédente" in graph_normalized
    )
    assert (
        "Exiger le head exactement déployé — mesuré avant la procédure, jamais recopié d'une "
        "exécution précédente — et vérifier le catalogue" in graph_normalized
    )
    assert (
        "Conserver le schéma au head exactement déployé — mesuré avant le rollback, jamais "
        "recopié d'une exécution précédente" in graph_normalized
    )

    assert "Upgrade initial vers le head 035 — procédure historique" in GRAPH_RUNBOOK
    assert "Ne pas la rejouer sur la production actuelle au head 037" not in graph_normalized
    assert (
        "Ne pas la rejouer sur la production actuelle : mesurez son head avant toute action, "
        "il dépasse déjà 035" in graph_normalized
    )
    assert (
        "régénérer le service systemd depuis le template compatible avec le head 035"
        not in GRAPH_RUNBOOK
    )

    architecture_normalized = " ".join(ARCHITECTURE.split())
    assert "a tested PostgreSQL restore at the exact deployed head" in architecture_normalized
    assert "DR-v5 run `20260724_150315`" in architecture_normalized

    assert "DR-v5 `20260724_150315`" in SCHEMA
    assert "un contrat et un drill isolé au head 037 restent requis" not in schema_normalized

    assert "`a857705f…` migrations 036/037 et restart MCP | **clos le 24 juillet**" in ROADMAP
    assert "les trois unités MCP sont live et canariées" in ROADMAP
    assert "cinq fragments restent non publiés" in ROADMAP
    roadmap_normalized = " ".join(ROADMAP.split())
    assert "Le restore PostgreSQL isolé au head 037 est acquis" in roadmap_normalized
    assert "cleanup jetable du drill est complet" in roadmap_normalized
    assert (
        "`1c6911a4…` statuts `planned` rejetés par plan_indexer | `in_progress`; code livré à "
        "`223fc1f`" in ROADMAP
    )
    assert "`44ee7643…` chemins `plan_scan_paths` relatifs" in ROADMAP
    next_work = ROADMAP.split("## Prochain chantier recommandé", maxsplit=1)[1]
    next_work_normalized = " ".join(next_work.split())
    av1_position = next_work_normalized.index("dernière preuve d'intégration du linking")
    ci_security_position = next_work_normalized.index("31d68c06…")
    coverage_position = next_work_normalized.index("5619c851…")
    assert av1_position < ci_security_position < coverage_position

    dr_active_normalized = " ".join((DR_IMPLEMENTATION_PLAN + "\n" + DR_B3_EVIDENCE).split())
    assert "DR-v5 `20260724_150315`" in dr_active_normalized
    assert "24/24 contrôles" in dr_active_normalized
    assert "preuve opérationnelle B3" in dr_active_normalized
    assert "Ne jamais downgrader pour" in dr_active_normalized
    assert "restore PostgreSQL au head 035" not in dr_active_normalized
    assert "Tant que ce drill n'est pas livré" not in dr_active_normalized
    assert "Le timer DR-v3 est installé, persistant et actif" in dr_active_normalized
    assert "Restent à activer DR-v5" in dr_active_normalized
    assert "le cron live restent inchangés" not in dr_active_normalized
    assert "Cycles DR-v1 historiques authentifiés" in DR_B3_EVIDENCE
    for residual_gate in (
        "rôles, propriétaires et ACL",
        "rebuild Neo4j dédié",
        "off-host chiffré",
        "alerte Discord",
        "activer DR-v5",
    ):
        assert residual_gate in dr_active_normalized

    dr_b2_normalized = " ".join(DR_B2_HANDOFF.split())
    assert "status: completed" in DR_B2_HANDOFF
    assert "Ce checkpoint ne constitue plus un point de reprise" in dr_b2_normalized
    assert "aucun downgrade n'est autorisé" in dr_b2_normalized

    stale_live_claims = (
        "Il n'est pas encore déployé sur le Brain live",
        "Il n'est pas encore déployé live",
        "This document does not assert that migrations 036–037 are applied live",
        "L'instance live observée le 21 juillet 2026 reste au head 032",
    )
    live_documents = "\n".join((README, CLAUDE, ARCHITECTURE, MCP_TOOLS, SCHEMA))
    for claim in stale_live_claims:
        assert claim not in live_documents


def test_graph_timer_runbook_requires_effective_read_only_service() -> None:
    service = (REPO_ROOT / "deploy" / "systemd" / "brain-v42-graph-recon.service.tmpl").read_text()
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))

    assert exec_start == (
        "ExecStart=__REPO_ROOT__/.venv/bin/python __REPO_ROOT__/scripts/rebuild_graph_projection.py"
    )
    assert "systemctl --user cat brain-v42-graph-recon.service" in GRAPH_RUNBOOK
    assert "fragment effectif" in GRAPH_RUNBOOK
    assert "`brain-v42-graph-recon.service`" in GRAPH_RUNBOOK
    assert "sans `--fix`, `reconcile_graph_drift` ni" in GRAPH_RUNBOOK
    assert "`recover_graph_projection.py`" in GRAPH_RUNBOOK


def test_production_mcp_secrets_are_documented_as_private_and_required() -> None:
    readme_shared = README.split("## Configuration (.env)", maxsplit=1)[1]
    readme_shared = readme_shared.split("```bash", maxsplit=1)[1].split("```", maxsplit=1)[0]
    architecture_shared = ARCHITECTURE.split("Environment variables", maxsplit=1)[1]
    architecture_shared = architecture_shared.split("```", maxsplit=2)[1]
    private_keys = {"MCP_HTTP_TOKEN", "MCP_HTTP_DREAM_TOKENS"}

    assert not private_keys.intersection(_environment_assignment_keys(readme_shared))
    assert not private_keys.intersection(_environment_assignment_keys(architecture_shared))
    assert "Never place" in README
    assert "n'appartiennent jamais" in ARCHITECTURE
    assert "mcp-token.env" in README
    assert "mcp-token.env" in ARCHITECTURE
    assert "MCP_HTTP_TOKEN` non vide" in ARCHITECTURE
    # The "must be non-empty" precision moved to docs/OPERATIONS.md with the
    # rest of the private-secret-file detail when README became the
    # open-source draft (ticket bdc4db73).
    assert "non-empty `MCP_HTTP_TOKEN`" in OPERATIONS
    assert "mcp-token.env" in OPERATIONS

    service = (REPO_ROOT / "deploy" / "systemd" / "brain-mcp-http.service.tmpl").read_text()
    runbook = (REPO_ROOT / "deploy" / "systemd" / "MCP_HTTP_RUNBOOK.md").read_text()
    assert "EnvironmentFile=%h/.config/brain-v42/mcp-token.env" in service
    assert "EnvironmentFile=-%h/.config/brain-v42/mcp-token.env" not in service
    assert "--require-effective-runtime-settings" in service
    assert "--require-effective-token" in runbook
    assert "MIGRATION REQUIRED" in runbook


@requires_claude
def test_documented_ci_rails_match_the_workflow_files() -> None:
    """The CI/CD section must name the GitHub rails the way they are wired.

    GitHub Actions is the sole CI/CD authority since the GitLab rail was retired
    (decision 218028c7): a section still describing `.gitlab-ci.yml` would send a
    reader to a rail whose project has pipelines disabled. The reverse also holds —
    the retired file must not be presented as live wiring.
    """
    ci_workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/continuous-integration.yml").read_text()
    )
    cd_workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/continuous-delivery.yml").read_text()
    )
    release_workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/release.yml").read_text())
    section = CLAUDE.split("## CI/CD", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    normalized = " ".join(section.split())

    assert ".github/workflows/continuous-integration.yml" in normalized
    assert ".github/workflows/continuous-delivery.yml" in normalized
    assert ".github/workflows/release.yml" in normalized
    assert not (REPO_ROOT / ".gitlab-ci.yml").exists()
    # Le rail de release existe pour tourner runner ÉTEINT : une section qui le
    # décrirait sur le runner à la demande enverrait un opérateur poser un tag
    # qui n'aboutirait jamais. Le déclencheur et le runner sont donc cités depuis
    # le câblage, pas récrits à la main.
    release_job = release_workflow["jobs"]["release"]
    assert str(release_job["runs-on"]) in normalized
    # PyYAML 1.1 lit une clé `on:` nue comme le booléen True.
    for glob in release_workflow[True]["push"]["tags"]:
        assert glob in normalized
    # The runner boundary of decision c62a98c1, quoted from the wiring itself.
    assert str(ci_workflow["jobs"]["lint-ruff"]["runs-on"]) in normalized
    assert ", ".join(cd_workflow["jobs"]["build-docker"]["runs-on"]) in normalized
    for secret in ("REGISTRY_USER", "REGISTRY_PASSWORD"):
        assert secret in normalized


def _documented_integration_commands(document: str) -> list[str]:
    """Return the shell lines of a plan that invoke the integration suite.

    Only lines inside ``bash`` fences count: prose that merely names the command
    is not a gate an operator will paste.
    """
    blocks = re.findall(r"^```bash\n(.*?)^```$", document, flags=re.DOTALL | re.MULTILINE)
    return [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if "pytest tests/integration" in line
    ]


def test_sweep_plan_integration_gates_cannot_pass_by_skipping_everything() -> None:
    """A documented integration gate must be able to fail.

    ``tests/integration/conftest.py`` resolves its database from
    ``BRAIN_V42_TEST_DB_URL`` alone and skips the whole suite when the variable
    is unset or points at the prod ``brain`` database. A plan command without it
    exits green having executed nothing — measured on this very plan:
    ``288 skipped in 1.38s`` unprefixed against ``256 passed, 32 skipped`` with
    the variable. Task 1 already ran into it.
    """
    plan = (
        REPO_ROOT / "docs" / "superpowers" / "plans" / "2026-08-07-session-lifecycle-sweep.md"
    ).read_text()
    commands = _documented_integration_commands(plan)

    assert commands, "the sweep plan must document at least one integration gate"
    for command in commands:
        assert "BRAIN_V42_TEST_DB_URL=" in command, (
            f"integration gate skips the whole suite without the test DB URL: {command}"
        )
    for command in commands:
        url = re.search(r"BRAIN_V42_TEST_DB_URL=(\S+)", command)
        assert url is not None, f"integration gate declares an empty test DB URL: {command}"
        db_name = unquote(urlsplit(url.group(1)).path).lstrip("/")
        assert db_name != "brain", (
            f"integration gate targets the prod database and skips: {command}"
        )
    assert (
        "Sans `BRAIN_V42_TEST_DB_URL`, `pytest tests/integration` skippe la suite en totalité "
        "et sort en vert : exiger un compte `passed` non nul, jamais « tout vert »."
    ) in " ".join(plan.split())


def _unfenced(document: str) -> str:
    """Drop every fenced code block, keeping the surrounding line structure.

    A doctrinal sentence deleted from its blockquote and re-emitted inside a
    ```text fence introduced by « sans portée normative » satisfies a naive
    substring pin while the doctrine an agent actually reads has disappeared.
    Fenced content is example material, so it is removed before any doctrinal
    statement is looked up.
    """
    kept: list[str] = []
    fenced = False
    for line in document.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def _narrative(document: str) -> str:
    """Strip blockquote and list markers so a wrapped sentence stays one sentence.

    A doctrinal statement wrapped across quoted lines is unpinnable with the
    naive ``" ".join(document.split())`` used elsewhere in this module: the
    leading ``>`` of each continuation line lands in the middle of the
    sentence. Bullet markers do the same to a bulleted doctrine.
    """
    lines = _unfenced(document).splitlines()
    return "\n".join(re.sub(r"^[ \t]*(?:>[ \t]?|[-*+][ \t])+", "", line) for line in lines)


def _prose(document: str) -> str:
    """Flatten a document — or one bounded fragment of it — to a line of prose."""
    return " ".join(_narrative(document).split())


def _section(document: str, heading: str) -> str:
    """Return one Markdown section: its heading up to the next one of equal or higher rank.

    Pinning a sentence against a whole file only proves the string exists
    somewhere. Bounding the lookup to the section that governs the reader is
    what makes a relocation to an appendix — the realistic decay — fail.
    """
    lines = _unfenced(document).splitlines()
    rank = len(heading) - len(heading.lstrip("#"))
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    assert start is not None, f"section introuvable, la doctrine a été déplacée: {heading}"
    for end in range(start + 1, len(lines)):
        candidate = lines[end].strip()
        depth = len(candidate) - len(candidate.lstrip("#"))
        if 0 < depth <= rank and candidate[depth : depth + 1] == " ":
            return "\n".join(lines[start:end])
    return "\n".join(lines[start:])


def _blockquote_holding(document: str, marker: str) -> str:
    """Return the whole blockquote that carries ``marker``."""
    lines = _unfenced(document).splitlines()
    hit = next(
        (
            index
            for index, line in enumerate(lines)
            if marker in line and line.lstrip().startswith(">")
        ),
        None,
    )
    assert hit is not None, f"blockquote introuvable, la doctrine a été déplacée: {marker}"
    start, end = hit, hit + 1
    while start > 0 and lines[start - 1].lstrip().startswith(">"):
        start -= 1
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    return "\n".join(lines[start:end])


# Une concession de fermeture automatique réunit trois traits : la phrase parle
# d'une session, elle la ferme, et elle le fait sans commande de l'utilisateur.
# Chercher un vocabulaire littéral (« ferme automatiquement ») ne détecte que la
# formulation qu'avait en tête l'auteur de la garde ; la conjonction des trois
# traits détecte aussi « clore », « abandonner de lui-même » ou « auto-close ».
_MENTIONS_A_SESSION = re.compile(r"session", re.IGNORECASE)
_CLOSES_THE_SESSION = re.compile(
    r"ferm\w*|cl[oô](?:re|s\w*|t\w*)|abandon\w*|balay\w*|sweep|brain_session_(?:end|abandon)",
    re.IGNORECASE,
)
_WITHOUT_A_USER_COMMAND = re.compile(
    r"auto-close\w*|auto_stale_7d|automatiq\w*|automatic\w*"
    r"|sans (?:commande|demande|instruction|attendre|activité|signe de vie)"
    r"|de lui-même|d'elle-même|de sa propre initiative|de son propre chef"
    r"|hook|background|nocturne|timer|cron|livraison|fin de réponse"
    r"|without an explicit command|without being asked|on its own|by itself"
    r"|côté serveur|server-side|agents?\b",
    re.IGNORECASE,
)

_SESSION_CLOSURE_PROHIBITION = (
    "Aucun hook, auto-close, livraison de travail ou fin de réponse ne ferme une session "
    "côté agent ou client."
)
# « sans heartbeat », pas « sans signe de vie » : le prédicat du sweep porte sur
# `last_heartbeat_at`, que seuls `capture` et `heartbeat` rafraîchissent. Un
# `resume` quotidien est un signe de vie et ne repousse pourtant pas l'abandon.
_SERVER_SIDE_SWEEP_EXCEPTION = (
    "la phase Dream `sweep` — livrée fermée et dry (`BRAIN_DREAM_SWEEP_ENABLED=false`, "
    "`BRAIN_DREAM_SWEEP_DRY_RUN=true`) — abandonne une session ouverte sans heartbeat "
    "depuis 7 jours, avec `abandonment_reason='auto_stale_7d'`."
)
_SWEEP_LEAVES_THE_FOCUS_ALONE = (
    "Elle n'écrit ni summary ni `next_focus` et ne touche jamais le focus du projet."
)
_SWEEP_GRANTS_NOTHING_TO_THE_CLIENT = "Aucun agent, aucun hook et aucun client ne gagne ce droit"
_SESSION_COMMANDS_STAY_EXPLICIT = (
    "`start`, `resume`, `end` et `abandon` restent des commandes explicites de l'utilisateur."
)
_THRESHOLD_DISAMBIGUATION = (
    "seul le balayage serveur de 7 jours abandonne une session sans commande explicite."
)
_CLAUDE_THRESHOLD_SENTENCE = f"Le statut reste `open`, et {_THRESHOLD_DISAMBIGUATION}"

# README.md became the English open-source draft (ticket bdc4db73); its short
# "## Sessions" section keeps two doctrinal sentences verbatim, the full
# detail (exact sweep killswitch names, focus-intact, no-client-right,
# explicit-commands) moved to docs/OPERATIONS.md.
_README_SESSION_BOUNDARY_SENTENCE = (
    "The user controls every session boundary: `start`, `resume`, `end` and `abandon` are "
    "explicit commands, never inferred by a hook, an agent or a client."
)
_README_STALE_THRESHOLD_SENTENCE = (
    "After 24 hours without a heartbeat, an open session exposes `is_stale=true`; the marker "
    "is derived, the persistent status stays `open`, and only the 7-day server-side sweep "
    "ever abandons a session without an explicit user command."
)

_ENGLISH_USER_CONTROL = (
    "Session lifecycle actions remain under exclusive user control on the agent and client side."
)
_ENGLISH_AGENTS_AND_HOOKS = (
    "Agents and hooks must not start, capture, heartbeat, resume, end, list, or abandon a "
    "session unless the user explicitly requests that command."
)
_ENGLISH_SWEEP_POINTER = (
    "The only server-side exception is the seven-day sweep documented under `brain_session_list`."
)
_ENGLISH_STALE_FLAG = (
    "An open session becomes `is_stale=true` when its last heartbeat is at least 24 hours old."
)
_ENGLISH_STALE_CLOSES_NOTHING = (
    "this derived flag never changes the persisted `status` and never auto-closes a session"
)
_ENGLISH_STALE_SENTENCE = (
    f'`status="stale"` selects that subset of open sessions; {_ENGLISH_STALE_CLOSES_NOTHING}.'
)
_ENGLISH_THRESHOLD_DISAMBIGUATION = (
    "Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, "
    "which is the only mechanism that moves an open session to `abandoned` without an explicit "
    "command (`abandonment_reason = 'auto_stale_7d'`)."
)
_ARCHITECTURE_USER_CONTROLLED_BOUNDARIES = (
    "Only an explicit user command may start, capture, heartbeat, list, resume, end, or "
    "abandon a session on the agent and client side."
)
_ARCHITECTURE_BOUNDARIES_SENTENCE = (
    f"**User-controlled boundaries.** {_ARCHITECTURE_USER_CONTROLLED_BOUNDARIES}"
)
_HOOKS_AND_AGENTS_NEVER_CLOSE = "Hooks and agents never infer a boundary or close a stale session."
_ARCHITECTURE_SWEEP_EXCEPTION = (
    "The only server-side exception is the Dream `sweep` phase, shipped disabled and dry, "
    "which abandons an open session with no heartbeat for seven days "
    "(`abandonment_reason = 'auto_stale_7d'`) without touching project focus."
)
_ARCHITECTURE_STALENESS_CLOSES_NOTHING = (
    "Staleness is a list filter over open rows; it never changes the persisted `status` and "
    "never auto-closes a session."
)

# Chaque énoncé est ancré à la section qui gouverne son lecteur, pas au fichier.
_DOCTRINE_SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    *(
        (
            (
                "CLAUDE.md#exception-stricte",
                _blockquote_holding(CLAUDE, "**Exception stricte — cycle de session :**"),
                (
                    ("prohibition", _SESSION_CLOSURE_PROHIBITION),
                    (
                        "exception",
                        f"**Seule exception, côté serveur :** {_SERVER_SIDE_SWEEP_EXCEPTION}",
                    ),
                    ("focus-intact", _SWEEP_LEAVES_THE_FOCUS_ALONE),
                    ("aucun-droit-client", _SWEEP_GRANTS_NOTHING_TO_THE_CLIENT),
                    ("commandes-explicites", _SESSION_COMMANDS_STAY_EXPLICIT),
                ),
            ),
            (
                "CLAUDE.md#cycle-de-session-explicite",
                _section(CLAUDE, "### Cycle de session explicite"),
                (("seuils-24h-vs-7j", _CLAUDE_THRESHOLD_SENTENCE),),
            ),
        )
        if CLAUDE
        else ()
    ),
    (
        "README.md#sessions",
        _section(README, "## Sessions"),
        (
            ("user-control", _README_SESSION_BOUNDARY_SENTENCE),
            ("seuils-24h-vs-7j", _README_STALE_THRESHOLD_SENTENCE),
        ),
    ),
    (
        "docs/OPERATIONS.md#session-lifecycle",
        _section(OPERATIONS, "## Session lifecycle (full v4 contract)"),
        (
            ("user-controlled-boundaries", _ARCHITECTURE_USER_CONTROLLED_BOUNDARIES),
            ("hooks-never-close", _HOOKS_AND_AGENTS_NEVER_CLOSE),
            ("sweep-exception", _ARCHITECTURE_SWEEP_EXCEPTION),
            ("stale-closes-nothing", _ARCHITECTURE_STALENESS_CLOSES_NOTHING),
            ("seuils-24h-vs-7j", _ENGLISH_THRESHOLD_DISAMBIGUATION),
        ),
    ),
    (
        "MCP_TOOLS.md#brain_workflow_guide",
        _section(MCP_TOOLS, "### brain_workflow_guide (`workflow_guide_tools.py`)"),
        (
            ("user-control", _ENGLISH_USER_CONTROL),
            ("agents-and-hooks", _ENGLISH_AGENTS_AND_HOOKS),
            ("sweep-pointer", _ENGLISH_SWEEP_POINTER),
        ),
    ),
    (
        "MCP_TOOLS.md#brain_session_list",
        _section(MCP_TOOLS, "### brain_session_list"),
        (
            ("stale-flag", _ENGLISH_STALE_FLAG),
            ("stale-closes-nothing", _ENGLISH_STALE_CLOSES_NOTHING),
            ("seuils-24h-vs-7j", _ENGLISH_THRESHOLD_DISAMBIGUATION),
        ),
    ),
    (
        "ARCHITECTURE.md#persistent-session-lifecycle",
        _section(
            ARCHITECTURE, "### Persistent session lifecycle (repository migrations 032 and 037)"
        ),
        (
            ("user-controlled-boundaries", _ARCHITECTURE_USER_CONTROLLED_BOUNDARIES),
            ("hooks-never-close", _HOOKS_AND_AGENTS_NEVER_CLOSE),
            ("sweep-exception", _ARCHITECTURE_SWEEP_EXCEPTION),
            ("stale-closes-nothing", _ARCHITECTURE_STALENESS_CLOSES_NOTHING),
            ("seuils-24h-vs-7j", _ENGLISH_THRESHOLD_DISAMBIGUATION),
        ),
    ),
)

# Le scan anti-élargissement, lui, balaie le document entier : c'est justement
# une concession écrite ailleurs qu'il doit attraper.
_DOCTRINE_DOCUMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    *(
        (
            (
                "CLAUDE.md",
                CLAUDE,
                (
                    _SESSION_CLOSURE_PROHIBITION,
                    f"**Seule exception, côté serveur :** {_SERVER_SIDE_SWEEP_EXCEPTION}",
                    _CLAUDE_THRESHOLD_SENTENCE,
                ),
            ),
        )
        if CLAUDE
        else ()
    ),
    (
        "README.md",
        README,
        (
            _README_SESSION_BOUNDARY_SENTENCE,
            _README_STALE_THRESHOLD_SENTENCE,
            # False positive of the heuristic: "session-sweep jobs" here names
            # a category of nightly Dream job, not a closure grant. "Nightly"
            # + "server-side" + "session-sweep" trips all three patterns.
            "Nightly agent pipeline (`scripts/dream.sh`: scan → clean → connect → synth → "
            "promote → reorg) plus server-side ticket-extraction, roadmap-curation and "
            "session-sweep jobs.",
        ),
    ),
    (
        "docs/MCP_TOOLS.md",
        MCP_TOOLS,
        (
            _ENGLISH_AGENTS_AND_HOOKS,
            _ENGLISH_SWEEP_POINTER,
            _ENGLISH_STALE_SENTENCE,
            _ENGLISH_THRESHOLD_DISAMBIGUATION,
        ),
    ),
    (
        "docs/ARCHITECTURE.md",
        ARCHITECTURE,
        (
            _ARCHITECTURE_BOUNDARIES_SENTENCE,
            _HOOKS_AND_AGENTS_NEVER_CLOSE,
            _ARCHITECTURE_SWEEP_EXCEPTION,
            _ARCHITECTURE_STALENESS_CLOSES_NOTHING,
            _ENGLISH_THRESHOLD_DISAMBIGUATION,
        ),
    ),
    (
        "docs/OPERATIONS.md",
        OPERATIONS,
        (
            _ARCHITECTURE_USER_CONTROLLED_BOUNDARIES,
            _HOOKS_AND_AGENTS_NEVER_CLOSE,
            _ARCHITECTURE_SWEEP_EXCEPTION,
            _ARCHITECTURE_STALENESS_CLOSES_NOTHING,
            _ENGLISH_THRESHOLD_DISAMBIGUATION,
            # brain_session_heartbeat paragraph restating the same sanctioned
            # 24h/7d threshold doctrine in different words.
            "After 24 hours without a heartbeat, an open session exposes `is_stale=true`; "
            "this marker is derived, its persisted status stays `open`, and only the 7-day "
            "server-side sweep ever abandons a session without an explicit command.",
        ),
    ),
)

# Reformulations plausibles d'un même élargissement. Aucune n'emploie le
# vocabulaire de la garde d'origine : c'est tout l'intérêt.
_REWORDED_CLOSURE_GRANTS = (
    "Un hook de fin de réponse peut clore une session inactive au bout de trois jours.",
    "L'agent abandonne de lui-même une session livrée.",
    "Le client ferme la session dès la livraison du travail.",
    "La phase sweep peut aussi être déclenchée par l'agent sur une session vivante.",
    "L'agent peut abandonner une session sans demande de l'utilisateur.",
    "Le serveur peut aussi fermer une session ouverte après 48 heures sans activité.",
    "Un hook de fin de réponse peut appeler `brain_session_end` dès que la tâche est livrée, "
    "sans attendre la demande de l'utilisateur.",
    "Le client ferme la session de lui-même à la fin de la réponse.",
    "A background job may abandon any open session it judges idle.",
    "A client hook may auto-close a session automatically when the task is delivered.",
)


def _sentences(document: str) -> list[str]:
    """Split narrative prose into sentences, block by block.

    Flattening the whole file first turns a table without a full stop into one
    monstrous « sentence » that matches almost anything. Paragraph boundaries
    are sentence boundaries too.
    """
    collected: list[str] = []
    for block in re.split(r"\n[ \t]*\n", _narrative(document)):
        flattened = " ".join(block.split())
        if flattened:
            collected.extend(re.split(r"(?<=\.)\s+", flattened))
    return collected


def _automatic_closure_statements(document: str) -> list[str]:
    """Return every sentence that closes a session without an explicit command."""
    return [
        sentence
        for sentence in _sentences(document)
        if _MENTIONS_A_SESSION.search(sentence)
        and _CLOSES_THE_SESSION.search(sentence)
        and _WITHOUT_A_USER_COMMAND.search(sentence)
    ]


@pytest.mark.parametrize(
    ("fragment", "statement"),
    [
        pytest.param(fragment, statement, id=f"{anchor}-{label}")
        for anchor, fragment, statements in _DOCTRINE_SECTIONS
        for label, statement in statements
    ],
)
def test_session_closure_doctrine_is_pinned_inside_its_own_section(
    fragment: str,
    statement: str,
) -> None:
    """Each doctrinal statement must live in the section that governs its reader.

    Tasks 1 to 3 shipped a phase that abandons sessions nobody asked to close.
    The honest amendment keeps the ban, names who it still binds — the agent
    and the client — and states the single server-side exception with its
    bounds. Pinning those sentences against the whole file would let a
    reorganisation move them to an appendix, or into a fenced example, with the
    contract still green: the lookup is bounded to the doctrinal fragment, and
    fenced code is stripped before it.
    """
    assert statement in _prose(fragment)


@pytest.mark.parametrize(
    ("name", "sanctioned"),
    [(name, sanctioned) for name, _, sanctioned in _DOCTRINE_DOCUMENTS],
)
def test_no_other_passage_grants_automatic_session_closure(
    name: str,
    sanctioned: tuple[str, ...],
) -> None:
    """Pinning the amendment is not enough: it must also stay the only one.

    Asserting a sentence is present cannot stop a second, wider grant from
    being written three paragraphs down. So every sentence of the document that
    talks about closing a session without a command is collected, and each one
    has to be a sanctioned statement.
    """
    document = next(text for candidate, text, _ in _DOCTRINE_DOCUMENTS if candidate == name)

    for statement in _automatic_closure_statements(document):
        assert statement in sanctioned, (
            f"{name} accorde une fermeture automatique hors de l'amendement borné: {statement}"
        )


@pytest.mark.parametrize("grant", _REWORDED_CLOSURE_GRANTS)
def test_a_reworded_grant_of_automatic_closure_is_still_detected(grant: str) -> None:
    """The scan must key on meaning, not on the phrasing its author had in mind.

    A guard that only recognises « ferme automatiquement » proves nothing about
    « clore », « abandonner de lui-même » or « auto-close ». Each rewording is
    appended to the real CLAUDE.md and must come back from the scan, so that
    the anti-widening gate above would reject it.
    """
    widened = f"{CLAUDE}\n\n{grant}\n"

    assert grant in _automatic_closure_statements(widened)


@requires_claude
def test_sweep_killswitches_are_documented_in_the_shared_environment() -> None:
    """The sweep is off and dry in the shared `.env` an operator copies."""
    configuration = CLAUDE.split("## Configuration", maxsplit=1)[1]
    shared_config = re.findall(r"```bash\n(.*?)```", configuration, flags=re.DOTALL)[0]

    assert "BRAIN_DREAM_SWEEP_ENABLED=false" in shared_config
    assert "BRAIN_DREAM_SWEEP_DRY_RUN=true" in shared_config
    documented_keys = _environment_assignment_keys(shared_config)
    assert len(documented_keys) == len(set(documented_keys)), (
        f"clé d'environnement dupliquée dans le .env partagé: {sorted(documented_keys)}"
    )


def test_readme_versioning_contract_matches_the_shipped_version() -> None:
    """Le numéro annoncé au lecteur est celui que la distribution portera.

    Sans cette porte, la section survivrait à un bump en affirmant l'ancien
    numéro — la forme exacte de dérive que ce dépôt a déjà payée sur une tête
    de migration.
    """
    manifest = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    version = manifest["project"]["version"]
    section = _section(README, "## Versioning")

    assert not version.startswith("1."), (
        "une 1.x promettrait une stabilité et un chemin de retour que 037 et 039 interdisent"
    )
    assert f"**{version}**" in section


def test_readme_versioning_contract_refuses_to_promise_a_downgrade() -> None:
    """Les deux révisions citées doivent VRAIMENT refuser leur downgrade."""
    section = _section(README, "## Versioning")

    assert "No lossless downgrade is promised" in section
    for revision in ("037", "039"):
        assert f"**{revision}**" in section
        source = next((REPO_ROOT / "alembic" / "versions").glob(f"{revision}_*.py")).read_text()
        downgrade = source.split("def downgrade()", maxsplit=1)[1]
        assert "RAISE EXCEPTION" in downgrade or "raise " in downgrade, (
            f"la révision {revision} ne refuse plus son downgrade, le README ment"
        )


def test_readme_versioning_contract_points_at_the_measured_health_fields() -> None:
    """La prose doit nommer les champs que `/health` expose réellement.

    The open-source draft README (ticket bdc4db73) moved this sentence from
    "## Versioning" to "## Production state" -- "measure it, do not read it
    here" governs the same claim as the downgrade-refusal doctrine, so it
    now lives next to that other measured-not-narrated instruction instead.
    """
    section = _section(README, "## Production state")

    for field in ("version", "alembic_head"):
        assert f"`{field}`" in section
        assert f'"{field}"' in SERVER
