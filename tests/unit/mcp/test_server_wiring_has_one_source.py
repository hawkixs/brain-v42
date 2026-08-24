"""The entrypoint and the e2e harness must build and MOUNT the same server.

``server.py`` used to register every tool root inside ``if __name__ ==
"__main__":``.  A ``__main__`` block cannot be imported, so the e2e harness
reproduced that wiring instead of calling it — and a double is worse than no
test at all: a middleware or a tool root added on one side and not the other
leaves the harness green about a server that exists nowhere.  The wiring the
next change touches is a middleware, so this had to be closed first.

``build_server()`` is now the single source of the WIRING, and
``prepare_tools_for_transport()`` / ``plan_http_transport()`` the single source
of the MOUNT.  Proving those functions *exist* would prove nothing; what this
module asserts is that **no second wiring and no second mount exist** — that
neither caller registers anything of its own, and that the harness applies the
plan rather than inventing its own arguments.

The entrypoint check is an exact-set comparison, not a blocklist of forbidden
names.  A blocklist only catches wiring someone spells the way we guessed;
``ENTRYPOINT_CALLS`` reddens on ANY call added to ``__main__``, whatever it is
called.  That is the property that is hard to falsify, and it is why this is a
declared list rather than a rule.

That ``build_server()`` is not merely *called* but actually wires a working
server is proven elsewhere, by construction:
``tests/integration/mcp/test_session_capture_e2e.py`` drives three real tools
through the server this function returns.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER = REPO_ROOT / "src" / "brain_v42" / "mcp" / "server.py"
E2E_HARNESS = REPO_ROOT / "tests" / "integration" / "mcp" / "test_session_capture_e2e.py"

# Everything ``if __name__ == "__main__":`` is allowed to call. Adding a line to
# the entrypoint means adding it here, in the same commit — a reviewed gesture,
# which is the whole point.
ENTRYPOINT_CALLS = frozenset(
    {
        "_configure_stdio_logging",
        "_setup_parent_death_signal",
        "_apply_http_server_arg",
        "build_server",
        "app_lifecycle",
        "_run_mcp",
        "log_server_starting",
        "run",  # asyncio.run
        "run_server",
    }
)

# Everything ``_run_mcp`` is allowed to call. Same contract as the entrypoint:
# a step added here is a step the harness does NOT get, so it has to move into a
# shared function or be declared. A plain blocklist would miss it.
RUN_MCP_CALLS = frozenset(
    {
        "prepare_tools_for_transport",
        "plan_http_transport",
        "run_http_async",
        "get_running_loop",
        "Event",
        "_install_signal_handlers",
        "create_task",
        "run_async",
        "wait",
        "cancel",
        "exception",
    }
)

# Wiring vocabulary. Neither caller may perform any of it directly.
_WIRING_PREFIX = "register_"
_WIRING_NAMES = frozenset({"build_services", "apply_tool_catalog_profile", "maybe_apply_code_mode"})


def _called_names(node: ast.AST) -> set[str]:
    """Names of everything called under ``node``, attribute calls included."""
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _is_dunder_main(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators)
    )


def _entrypoint_node() -> ast.If:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))
    entrypoints = [node for node in tree.body if _is_dunder_main(node)]
    assert len(entrypoints) == 1, f"expected exactly one __main__ block, found {len(entrypoints)}"
    return entrypoints[0]  # type: ignore[return-value]


def _wiring_calls(names: set[str]) -> set[str]:
    return {name for name in names if name.startswith(_WIRING_PREFIX) or name in _WIRING_NAMES}


def test_entrypoint_delegates_its_whole_wiring_to_build_server() -> None:
    """``__main__`` calls exactly the declared set — nothing more, nothing less."""
    called = _called_names(_entrypoint_node())

    undeclared = called - ENTRYPOINT_CALLS
    assert not undeclared, (
        "the server entrypoint gained call(s) that live outside build_server(); "
        "either move them into build_server() so the e2e harness gets them too, "
        f"or declare them in ENTRYPOINT_CALLS in the same commit: {sorted(undeclared)}"
    )

    vanished = ENTRYPOINT_CALLS - called
    assert not vanished, (
        f"ENTRYPOINT_CALLS declares call(s) the entrypoint no longer makes: {sorted(vanished)}"
    )


def test_entrypoint_registers_nothing_itself() -> None:
    """Redundant with the set above by construction, explicit about the WHY."""
    assert _wiring_calls(_called_names(_entrypoint_node())) == set()
    assert "build_server" in _called_names(_entrypoint_node())


def test_e2e_harness_calls_the_wiring_instead_of_reproducing_it() -> None:
    """The harness must obtain its server from the same function production does."""
    tree = ast.parse(E2E_HARNESS.read_text(encoding="utf-8"), filename=str(E2E_HARNESS))
    called = _called_names(tree)

    assert "build_server" in called, (
        "the e2e harness no longer calls build_server(); if it builds its own "
        "server it is testing a double again"
    )
    reproduced = _wiring_calls(called)
    assert not reproduced, (
        "the e2e harness performs wiring of its own — that is the double this "
        f"module exists to prevent: {sorted(reproduced)}"
    )


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(found) == 1, f"expected exactly one {name} definition, got {len(found)}"
    return found[0]


def test_run_mcp_delegates_its_whole_mount_to_the_shared_plan() -> None:
    """``_run_mcp`` calls exactly the declared set — a new step cannot hide."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"), filename=str(SERVER))
    called = _called_names(_function_def(tree, "_run_mcp"))

    undeclared = called - RUN_MCP_CALLS
    assert not undeclared, (
        "_run_mcp gained call(s) the e2e harness will not get, because it mounts "
        "the app itself; move them into prepare_tools_for_transport() or "
        f"plan_http_transport(), or declare them in RUN_MCP_CALLS: {sorted(undeclared)}"
    )
    vanished = RUN_MCP_CALLS - called
    assert not vanished, (
        f"RUN_MCP_CALLS declares call(s) _run_mcp no longer makes: {sorted(vanished)}"
    )


def test_e2e_harness_mounts_from_the_plan_and_invents_no_argument() -> None:
    """Every ``http_app`` keyword must come from the plan, never from a literal.

    This is the falsifiable half of "same mount". Re-hardcoding
    ``stateless_http=True`` is exactly how the harness drifted the first time,
    and a name-based check would not have caught it — the call is spelled the
    same either way. So the assertion is on the ARGUMENTS, not the callee.
    """
    tree = ast.parse(E2E_HARNESS.read_text(encoding="utf-8"), filename=str(E2E_HARNESS))
    called = _called_names(tree)
    for required in ("prepare_tools_for_transport", "plan_http_transport"):
        assert required in called, f"the e2e harness no longer goes through {required}()"

    mounts = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "http_app"
    ]
    assert len(mounts) == 1, f"expected exactly one http_app mount, got {len(mounts)}"
    mount = mounts[0]
    assert not mount.args, "http_app must be called with keywords only, so each is checkable"

    invented = [
        keyword.arg
        for keyword in mount.keywords
        if not (
            isinstance(keyword.value, ast.Attribute)
            and isinstance(keyword.value.value, ast.Name)
            and keyword.value.value.id == "plan"
        )
    ]
    assert not invented, (
        "the e2e harness passes mount argument(s) of its own instead of the ones "
        f"plan_http_transport() decided: {invented}"
    )
