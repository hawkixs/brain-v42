"""Prevent caught exception payloads from crossing the MCP response boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Keys identify an exact expression in a handler, rather than drifting line numbers.
# Each exception below is produced by caller-facing code, not a wrapped driver error.
CALLER_SAFE_SITES: dict[tuple[str, str, str, str], str] = {
    (
        "mcp/admin_elevations.py",
        "handle",
        "CredentialRefused",
        "return self._transport_refusal(request, exc.reason, None)",
    ): "CredentialRefused carries a closed reason code, never an underlying error payload.",
    (
        "mcp/argument_guidance.py",
        "on_call_tool",
        "ValidationError",
        "ToolError(_guidance(name, exc, parameters, context.message.arguments or {}))",
    ): "Guidance reads errors(include_input=False): field paths and expected types, never values.",
    (
        "mcp/business_errors.py",
        "surfaced",
        "SURFACED_BUSINESS_ERRORS",
        "ToolError(str(exc))",
    ): "Audited business families carry curated refusals; proposal failures are sanitized at source.",
    (
        "mcp/delivery_transport.py",
        "run",
        "DeliveryError",
        "ToolError(str(exc))",
    ): "DeliveryError carries stable domain codes and validation messages, never driver failures.",
    (
        "mcp/tools/crud_tools.py",
        "brain_delete",
        "KnowledgeCapturedError",
        "format_error(_captured_refusal(entity_type, entity_id, refused.session_id))",
    ): "Only the capture session UUID is read; exception text is never rendered.",
    (
        "mcp/tools/project_context_tools.py",
        "brain_focus_history",
        "ValueError",
        "format_error(str(exc))",
    ): "The isolated canonicalize_project_key call raises a curated invalid-key hint.",
    (
        "mcp/tools/project_focus_read_tools.py",
        "brain_project_focus_get",
        "ValueError",
        "format_error(str(exc))",
    ): "The isolated canonicalize_project_key call raises a curated invalid-key hint.",
    (
        "mcp/tools/project_context_tools.py",
        "brain_set_project_context",
        "FocusSlotError",
        "format_error(str(exc))",
    ): "FocusSlotError carries a stable code and a constant caller-safe message (7eab4e35).",
    (
        "mcp/tools/project_context_tools.py",
        "brain_update_project_focus",
        "FocusSlotError",
        "format_error(str(exc))",
    ): "The elevation check raises a constant focus_shrink_override_refused refusal (1c3136ef).",
    (
        "mcp/tools/project_context_tools.py",
        "brain_update_project_focus",
        "ProjectFocusShrinkError",
        "format_error(f'base_focus_shrink: {exc}')",
    ): "The shrink guard supplies a curated refusal, not an underlying exception.",
    (
        "mcp/tools/project_context_tools.py",
        "brain_update_project_focus",
        "ProjectFocusConflictError",
        "format_error(f'Focus conflict: current revision {exc.current_revision}, current focus: {exc.current_focus!r}. Resume the session and retry.')",
    ): "Only declared focus and its revision are rendered; no exception text is included.",
    (
        "mcp/tools/project_context_tools.py",
        "brain_update_project_focus",
        "(ProjectFocusNotFoundError, ProjectFocusValidationError)",
        "format_error(str(exc))",
    ): "These service guards construct actionable project/field refusals without driver text.",
    (
        "mcp/tools/roadmap_tools.py",
        "brain_feature_create",
        "FeatureCreationError",
        "format_error(str(exc))",
    ): "Feature creation wraps embedding failures with a fixed message and curates other refusals.",
    (
        "mcp/tools/decay_tools.py",
        "brain_merge_entities",
        "(ConsolidationEntityNotFoundError, ConsolidationEndpointMergedError, ConsolidationChildrenUnstableError)",
        "format_error(str(exc))",
    ): "Consolidation constructs a missing, already-merged or unstable endpoint message from entity type and UUID.",
    (
        "mcp/tools/brain_tools.py",
        "brain_supersede_decision",
        "DecisionNotSupersedableError",
        "format_error(str(exc))",
    ): "Supersession refuses a missing or already-superseded decision by its UUID only.",
    (
        "mcp/tools/crud_tools.py",
        "brain_update",
        "(ClaimMutationError, ClaimVerificationError)",
        "format_error(str(exc))",
    ): "Claim mutation guards curate key/state refusals without driver text; a "
    "verification error renders a closed code and its constant text.",
    (
        "mcp/tools/dream_tools.py",
        "brain_apply_curation_proposal",
        "ProposalServiceError",
        "format_error(str(exc))",
    ): "Proposal guards are curated and unexpected apply/reject failures are sanitized at source.",
    (
        "mcp/tools/dream_tools.py",
        "brain_reject_curation_proposals",
        "ProposalServiceError",
        "lines.append(f'- **#{proposal_id}** — {exc}')",
    ): "Proposal guards are curated and unexpected rejection failures are sanitized at source.",
}

for _function in (
    "brain_ticket_create",
    "brain_ticket_reply",
    "brain_ticket_transition",
    "brain_ticket_plan",
    "brain_ticket_list",
):
    CALLER_SAFE_SITES[
        ("mcp/tools/ticket_tools.py", _function, "TicketError", "format_error(str(exc))")
    ] = "Ticket domain guards curate permission, state and project refusals without driver text."

for _function in ("_validate_ticket_inputs", "brain_ticket_list"):
    CALLER_SAFE_SITES[
        ("mcp/tools/ticket_tools.py", _function, "ValueError", "format_error(str(exc))")
    ] = "Only our key canonicalizer or release parser runs in this isolated input handler."


def raw_exception_sites(source: str) -> list[tuple[int, str, str, str]]:
    """Inspect handler scopes, including result lines, without mistaking logs for errors."""
    sites: list[tuple[int, str, str, str]] = []

    def references_exception(node: ast.AST, name: str) -> bool:
        # Sanitized validation messages and the class name contain no raw payload.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {
                "format_validation_errors",
                "build_internal_error",
                "format_internal_error",
            }:
                return False
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__name__"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "type"
        ):
            return False
        return (isinstance(node, ast.Name) and node.id == name) or any(
            references_exception(child, name) for child in ast.iter_child_nodes(node)
        )

    class Visitor(ast.NodeVisitor):
        function = "<module>"
        handlers: list[ast.ExceptHandler] = []

        def record(self, node: ast.AST, values: list[ast.AST]) -> None:
            for handler in self.handlers:
                if handler.name and any(
                    references_exception(value, handler.name) for value in values
                ):
                    sites.append(
                        (
                            node.lineno,
                            self.function,
                            ast.unparse(handler.type) if handler.type else "<bare>",
                            ast.unparse(node),
                        )
                    )

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            self.handlers.append(node)
            for statement in node.body:
                self.visit(statement)
            self.handlers.pop()

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            result_line = isinstance(node.func, ast.Attribute) and node.func.attr in {
                "append",
                "extend",
            }
            if name == "format_error" or name.endswith("Error") or result_line:
                self.record(node, [*node.args, *(kw.value for kw in node.keywords)])
            self.generic_visit(node)

        def visit_Return(self, node: ast.Return) -> None:
            # Calls such as `return format_error(str(exc))` are recorded once,
            # by visit_Call; plain text, objects and unguarded calls are returns.
            before = len(sites)
            self.generic_visit(node)
            if node.value is not None and len(sites) == before:
                self.record(node, [node.value])

        def visit_Raise(self, node: ast.Raise) -> None:
            # Keep existing call sites stable and ignore bare raises and causes.
            before = len(sites)
            self.generic_visit(node)
            if isinstance(node.exc, ast.Call) and len(sites) == before:
                self.record(
                    node.exc,
                    [*node.exc.args, *(kw.value for kw in node.exc.keywords)],
                )

        def visit_AugAssign(self, node: ast.AugAssign) -> None:
            self.record(node, [node.value])
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return sites


@pytest.mark.parametrize(
    "statement",
    [
        'format_error(f"x {exc}")',
        "raise ToolError(str(exc))",
        "raise errors.ToolError(str(exc))",
        "raise Refusal(str(exc))",
        'raise SomeFailure(f"{exc}")',
        'raise failures.Refusal(message=f"{exc}")',
        'lines.append(f"failed: {exc}")',
        'return f"failed: {exc}"',
        'return {"error": str(exc)}',
        'lines.extend([f"failed: {exc}"])',
        'message += f"failed: {exc}"',
        "return exc",
    ],
)
@pytest.mark.parametrize("name", ["exc", "error"])
def test_guard_rejects_raw_exception_responses(statement: str, name: str) -> None:
    statement = statement.replace("exc", name)
    source = f"def tool():\n    try:\n        run()\n    except Exception as {name}:\n        {statement}\n"
    assert len(raw_exception_sites(source)) == 1


@pytest.mark.parametrize(
    "statement",
    [
        'raise ToolError("safe refusal") from exc',
        'raise Refusal("safe refusal") from exc',
        "raise",
    ],
)
def test_guard_accepts_constant_errors_and_server_logs(statement: str) -> None:
    source = f"""try:
    run()
except Exception as exc:
    logger.error("failed", exc_info=exc)
    {statement}
"""
    assert raw_exception_sites(source) == []


def test_no_raw_exception_text_in_tool_errors() -> None:
    root = Path(__file__).resolve().parents[3] / "src" / "brain_v42"
    paths = sorted((root / "mcp").rglob("*.py"))
    paths.append(root / "services" / "proposal_service.py")
    violations: list[str] = []
    matched: set[tuple[str, str, str, str]] = set()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        for line, function, handler, expression in raw_exception_sites(path.read_text()):
            site = (relative, function, handler, expression)
            if site not in CALLER_SAFE_SITES:
                violations.append(f"{relative}:{line}: {handler}: {expression}")
            else:
                matched.add(site)
    assert not violations, "Raw exception text may reach callers:\n" + "\n".join(violations)
    stale = CALLER_SAFE_SITES.keys() - matched
    assert not stale, f"Stale caller-safe allowlist entries: {sorted(stale)}"
