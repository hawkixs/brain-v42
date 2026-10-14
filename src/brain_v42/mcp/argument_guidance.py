"""Safe, actionable guidance for tool argument validation failures."""

from __future__ import annotations

import difflib
import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError

from brain_v42.mcp.parameter_vocabulary import PARAMETER_ALIASES

_SAFE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_ALIAS_HINTS: dict[str, set[str]] = {}
for _aliases in PARAMETER_ALIASES.values():
    for _alias, _canonical in _aliases.items():
        _ALIAS_HINTS.setdefault(_alias, set()).add(_canonical)


def _safe_key(key: Any) -> str:
    rendered = str(key)
    return rendered if _SAFE_KEY.fullmatch(rendered) else "<key>"


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        resolved = root.get("$defs", {}).get(ref.rsplit("/", 1)[-1])
        if isinstance(resolved, dict):
            return resolved
    return schema


def _unwrap_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    schema = _resolve_schema(schema, root)
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list):
        non_null = [item for item in variants if item.get("type") != "null"]
        if non_null:
            return _unwrap_schema(non_null[0], root)
    return schema


def _render_type(schema: dict[str, Any], root: dict[str, Any]) -> str:
    schema = _unwrap_schema(schema, root)
    values = schema.get("enum")
    if isinstance(values, list):
        rendered = "|".join(f'"{value}"' for value in values[:8])
        if len(values) > 8:
            rendered += "|…"
        return rendered
    kind = schema.get("type")
    if not isinstance(kind, str):
        return "object"
    if kind == "null":
        return "object"
    if kind == "array":
        return "array"
    if kind == "object" or "properties" in schema:
        return "object"
    return {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}.get(
        kind, "object"
    )


def _input_type(arguments: dict[str, Any], location: tuple[Any, ...]) -> str:
    value: Any = arguments
    for part in location:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int) and part < len(value):
            value = value[part]
        else:
            return "unknown"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def _signature(name: str, parameters: dict[str, Any], cap: int) -> str:
    if cap < len(name) + 2:
        return ""
    root = parameters
    props = parameters.get("properties", {})
    if not props:
        return f"{name}()"
    required = parameters.get("required", [])
    names = [key for key in props if key in required] + [
        key for key in props if key not in required
    ]
    required_names = [key for key in names if key in required]
    optional_names = [key for key in names if key not in required]

    def parameter(key: str) -> str:
        return f"{key}{'' if key in required else '?'}: {_render_type(props[key], root)}"

    all_text = ", ".join(parameter(key) for key in names)
    signature = f"{name}({all_text})"
    if len(signature) <= cap:
        return signature
    required_parts: list[str] = []
    for key in required_names:
        candidate_parts = [*required_parts, parameter(key)]
        candidate = f"{name}({', '.join(candidate_parts)}{', …' if len(candidate_parts) < len(required_names) else ''})"
        if len(candidate) > cap:
            break
        required_parts.append(parameter(key))
    required_remaining = len(required_names) - len(required_parts)
    kept: list[str] = []
    for key in optional_names if not required_remaining else []:
        remaining = len(optional_names) - len(kept) - 1
        suffix = f", … (+{remaining} optional)" if remaining else ""
        candidate_parts = [*required_parts, *kept, parameter(key)]
        candidate = f"{name}({', '.join(candidate_parts)}{suffix})"
        if len(candidate) > cap:
            break
        kept.append(parameter(key))
    remaining = len(optional_names) - len(kept) + required_remaining
    parts = [*required_parts, *kept]
    if remaining:
        parts.append(f"… (+{remaining} more)")
    if not required_parts and not kept:
        return ""
    result = f"{name}({', '.join(parts)})"
    return result if len(result) <= cap else ""


def _schema_path(
    root: dict[str, Any], location: tuple[Any, ...]
) -> tuple[list[str | int], dict[str, Any] | None]:
    path: list[str | int] = []
    schema = root
    matched: list[str | int] = []
    for part in location:
        schema = _unwrap_schema(schema, root)
        if isinstance(part, int) and schema.get("type") == "array":
            path.append(part)
            matched.append(part)
            schema = schema.get("items", {})
        elif isinstance(part, str) and part in schema.get("properties", {}):
            path.append(part)
            matched.append(part)
            schema = schema["properties"][part]
        elif (
            isinstance(part, str)
            and schema.get("type") == "object"
            and isinstance(schema.get("additionalProperties"), dict)
        ):
            path.append(part)
            matched.append(part)
            schema = schema["additionalProperties"]
        else:
            return path, None
    return path, schema


def _limit_message(code: str, ctx: dict[str, Any]) -> str | None:
    for key, symbol in (("le", "<="), ("lt", "<"), ("ge", ">="), ("gt", ">")):
        value = ctx.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"must be {symbol} {value}"
    for key in ("min_length", "min_items", "max_length", "max_items", "max_digits"):
        value = ctx.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            noun = "digits"
            if key.endswith("length"):
                noun = "characters" if code.startswith("string_too_") else "items"
            elif key.endswith("items"):
                noun = "items"
            noun = noun[:-1] if value == 1 else noun
            return (
                f"at least {value} {noun}" if key.startswith("min_") else f"at most {value} {noun}"
            )
    if code == "string_pattern_mismatch":
        return "does not match the expected format"
    return None


def _fit_message(
    prefix: str,
    core: list[str],
    unexpected: list[str],
    signature_for: Any,
) -> str:
    """Fit guidance to the public message limit with bounded clause passes."""
    parts = list(core)
    body = "; ".join(parts) or "invalid value shape"
    if len(f"{prefix} {body}.") > 600:
        budget = 600 - len(prefix) - len(" ….")
        kept: list[str] = []
        for clause in core:
            candidate = "; ".join([*kept, clause])
            if len(f"{prefix} {candidate}….") > 600:
                break
            kept.append(clause)
        if kept:
            body = "; ".join(kept) + "…"
        else:
            first = core[0] if core else "invalid value shape"
            body = first[: max(0, budget)] + "…"
        message = f"{prefix} {body}."
        if len(message) > 600:
            message = message[:599] + "…"
        return message

    base_message = f"{prefix} {body}."
    signature_cap = 600 - len(base_message) - len(" Expected: ")
    if unexpected:
        signature_cap -= len("…")
    signature = signature_for(signature_cap)
    kept_unexpected: list[str] = []
    truncated = False
    for index, clause in enumerate(unexpected):
        candidate_parts = [*core, *kept_unexpected, clause]
        candidate_body = "; ".join(candidate_parts)
        candidate_message = f"{prefix} {candidate_body}."
        more_clauses = index + 1 < len(unexpected)
        limit = 600 - (len("…") if more_clauses else 0)
        if signature and len(candidate_message) + len(" Expected: ") + len(signature) > limit:
            truncated = True
            break
        if not signature and len(candidate_message) > limit:
            truncated = True
            break
        kept_unexpected.append(clause)

    parts = [*core, *kept_unexpected]
    if not parts:
        parts = ["invalid value shape"]
    if truncated:
        parts[-1] += "…"
    body = "; ".join(parts)
    message = f"{prefix} {body}."
    if signature:
        message += f" Expected: {signature}"

    while len(message) > 600 and kept_unexpected:
        kept_unexpected.pop()
        parts = [*core, *kept_unexpected]
        if not parts:
            parts = ["invalid value shape"]
        parts[-1] += "…"
        body = "; ".join(parts)
        message = f"{prefix} {body}."
        if signature:
            message += f" Expected: {signature}"

    while len(message) > 600 and len(core) > 1:
        core.pop()
        parts = [*core]
        if not parts:
            parts = ["invalid value shape"]
        parts[-1] += "…"
        message = f"{prefix} {'; '.join(parts)}."
        if signature:
            message += f" Expected: {signature}"

    if len(message) > 600 and signature:
        message = f"{prefix} {body}."
    if len(message) > 600:
        shortened = message[:597].rsplit("; ", 1)[0].rstrip(" ;")
        if len(shortened) < len(prefix) + 1:
            shortened = message[:597].rstrip(" ;")
        message = shortened + "…"
    return message


def _guidance(
    name: str,
    error: ValidationError,
    parameters: dict[str, Any] | None,
    arguments: dict[str, Any],
) -> str:
    props = {} if parameters is None else parameters.get("properties", {})
    error_items = error.errors(include_url=False, include_input=False)
    is_call_schema_error = any(
        item.get("type") in {"missing_argument", "unexpected_keyword_argument"}
        for item in error_items
    )
    unknown = [
        head
        for item in error_items
        if item.get("type") == "unexpected_keyword_argument" and item.get("loc")
        for head in item["loc"][:1]
        if isinstance(head, str)
    ]
    missing = [
        head
        for item in error_items
        if item.get("type") == "missing_argument" and item.get("loc")
        for head in item["loc"][:1]
        if isinstance(head, str)
    ]
    unexpected_clauses: list[str] = []
    core_clauses: list[str] = []
    for key in unknown:
        semantic = next(iter(sorted(_ALIAS_HINTS.get(key, set()) & props.keys())), None)
        close = difflib.get_close_matches(key, list(props), n=1, cutoff=0.5)
        hint = semantic or (close[0] if close else None)
        suffix = f" (did you mean '{hint}'?)" if hint else ""
        unexpected_clauses.append(f"unexpected '{_safe_key(key)}'{suffix}")
    if missing:
        core_clauses.append("missing " + ", ".join(f"'{item}'" for item in missing))
        if name == "brain_update_project_focus" and "expected_focus_revision" in missing:
            core_clauses.append(
                "Read the current revision from brain_session_resume current_focus_revision "
                "or session.started_focus_revision from brain_session_start, then pass it "
                "as expected_focus_revision"
            )
        if name == "brain_session_end" and "next_focus" in missing:
            core_clauses.append(
                "next_focus must be the WHOLE base focus text because it replaces it; "
                "a handover under 70% of the current base is refused as base_focus_shrink. "
                "Read the current focus with brain_session_resume current_focus"
            )
    for item in error_items:
        raw_loc = tuple(item.get("loc", ()))
        path, leaf_schema = _schema_path(parameters or {}, raw_loc)
        loc = ".".join(str(part) if isinstance(part, int) else _safe_key(part) for part in path)
        if not loc:
            loc = _safe_key(raw_loc[-1]) if raw_loc else "<key>"
        error_type = item.get("type")
        if error_type in {"missing_argument", "unexpected_keyword_argument"}:
            continue
        if error_type == "missing":
            if leaf_schema is None:
                core_clauses.append(f"'{loc}': invalid value")
                continue
            core_clauses.append(f"'{loc}': missing required value")
            continue
        if error_type == "extra_forbidden":
            core_clauses.append(f"'{loc}': unexpected field")
            continue
        if leaf_schema is None:
            core_clauses.append(f"'{loc}': invalid value")
            continue
        code = str(item.get("type", ""))
        if code in {"literal_error", "enum"} and leaf_schema:
            schema = _resolve_schema(leaf_schema, parameters or {})
            values = schema.get("enum")
            if not values:
                values = [
                    choice.get("const") for choice in schema.get("anyOf", []) if "const" in choice
                ]
            if values:
                allowed = "|".join(f'"{value}"' for value in values[:8]) + (
                    "|…" if len(values) > 8 else ""
                )
                core_clauses.append(f"'{loc}': must be one of {allowed}")
                continue
        limit = _limit_message(code, item.get("ctx", {}))
        if limit:
            core_clauses.append(f"'{loc}': {limit}")
            continue
        actual_type = _input_type(arguments, tuple(path)) if path else "unknown"
        if code.startswith("string_type") or code.startswith("string_"):
            expected = "string"
        elif code.startswith("int_"):
            expected = "integer"
        elif code.startswith("list_"):
            expected = "array"
        elif code.startswith("dict_") or code.startswith("model_"):
            expected = "object"
        elif code.startswith("bool_"):
            expected = "boolean"
        else:
            core_clauses.append(f"'{loc}': invalid value")
            continue
        if path:
            core_clauses.append(f"'{loc}': expected {expected}, got {actual_type}")
        else:
            core_clauses.append(f"'{loc}': expected {expected}")
    prefix = f"Invalid arguments for {name}:"
    core_clauses = list(dict.fromkeys(core_clauses))
    unexpected_clauses = list(dict.fromkeys(unexpected_clauses))
    if parameters is not None and is_call_schema_error:
        return _fit_message(
            prefix,
            core_clauses,
            unexpected_clauses,
            lambda cap: _signature(name, parameters, cap),
        )
    clauses = [*unexpected_clauses, *core_clauses]
    message = f"{prefix} {'; '.join(clauses) or 'invalid value shape'}."
    return message if len(message) <= 600 else _fit_message(prefix, clauses, [], lambda _cap: "")


class ArgumentGuidanceMiddleware(Middleware):
    """Replace raw Pydantic details with published schema guidance."""

    async def _published_parameters(self, context: Any, name: str) -> dict[str, Any] | None:
        """Look up the published schema without allowing lookup failures to mask guidance."""
        try:
            tool = await context.fastmcp_context.fastmcp.get_tool(name)
        except Exception:
            return None
        return None if tool is None else tool.parameters

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = context.message.name
        if name != "brain_call_tool":
            aliases = PARAMETER_ALIASES.get(name, {})
            arguments = dict(context.message.arguments or {})
            for alias, canonical in aliases.items():
                if alias not in arguments:
                    continue
                if canonical in arguments or any(
                    other_alias in arguments and other_canonical == canonical
                    for other_alias, other_canonical in aliases.items()
                    if other_alias != alias
                ):
                    conflicting = (
                        canonical
                        if canonical in arguments
                        else next(
                            other_alias
                            for other_alias, other_canonical in aliases.items()
                            if other_alias != alias
                            and other_canonical == canonical
                            and other_alias in arguments
                        )
                    )
                    raise ToolError(
                        f"Ambiguous arguments for {name}: '{alias}' and '{conflicting}' name the same parameter; pass only '{canonical}'."
                    )
                arguments[canonical] = arguments.pop(alias)
            if arguments != (context.message.arguments or {}):
                context = context.copy(
                    message=context.message.model_copy(update={"arguments": arguments})
                )
        try:
            return await call_next(context)
        except ValidationError as exc:
            parameters = await self._published_parameters(context, name)
            raise ToolError(
                _guidance(name, exc, parameters, context.message.arguments or {})
            ) from None
