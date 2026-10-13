"""Internal error details belong to server logs, referenced by a short opaque id."""

import re

import pytest
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ValidationError, field_validator
from structlog.testing import capture_logs

from brain_v42.mcp.tools import formatters
from brain_v42.models.claim_input import ClaimInput


def test_internal_error_builder_returns_the_logged_reference() -> None:
    helper = getattr(formatters, "build_internal_error", None)
    assert callable(helper), "The batch error builder is missing"
    exc = RuntimeError("secret")
    with capture_logs() as logs:
        message = helper("claim replacement", exc)
    assert len(logs) == 1
    assert message == (
        f"claim replacement failed: internal error RuntimeError (ref {logs[0]['correlation_id']})"
    )
    assert logs[0]["exc_info"] is exc
    assert "secret" not in message


def test_validation_errors_preserve_curated_claim_rules() -> None:
    with pytest.raises(ValidationError) as caught:
        ClaimInput(
            statement="input-secret" * 50,
            fact_name="graph_projection_lag",
            expected={"path": "/lag_seconds", "op": "lte", "value": 300},
        )
    message = formatters.format_validation_errors(caught.value)
    assert "statement: Value error, statement rule requires 1 to 500 characters" in message
    assert "input-secret" not in message


def test_validation_errors_keep_a_validator_value_error_message() -> None:
    """A plain ValueError raised by a model validator is a curated caller message."""

    class Payload(BaseModel):
        title: str

        @field_validator("title")
        @classmethod
        def _short(cls, value: str) -> str:
            raise ValueError("title rule requires at most 200 characters")

    with pytest.raises(ValidationError) as caught:
        Payload(title="input-secret")
    message = formatters.format_validation_errors(caught.value)
    assert message == "title: Value error, title rule requires at most 200 characters"
    assert "input-secret" not in message


def test_internal_error_exposes_only_class_and_log_reference() -> None:
    helper = getattr(formatters, "format_internal_error", None)
    assert callable(helper), "The internal error guard is missing"
    exc = RuntimeError("postgresql://user:secret@private/db SELECT secret")
    with capture_logs() as logs, pytest.raises(ToolError) as caught:
        helper("proposal apply", exc)

    message = str(caught.value)
    match = re.fullmatch(
        r"proposal apply failed: internal error RuntimeError \(ref ([0-9a-f]{12})\)", message
    )
    assert match is not None
    assert "secret" not in message and "SELECT" not in message
    assert len(logs) == 1
    assert logs[0] == {
        "event": "mcp.tool_internal_error",
        "operation": "proposal apply",
        "error_class": "RuntimeError",
        "correlation_id": match[1],
        "exc_info": exc,
        "log_level": "error",
    }


def test_validation_errors_drop_input_values() -> None:
    helper = getattr(formatters, "format_validation_errors", None)
    assert callable(helper), "The validation error guard is missing"

    class Payload(BaseModel):
        count: int

    with pytest.raises(ValidationError) as caught:
        Payload(count="postgresql://user:secret@private/db")
    message = helper(caught.value)
    assert "count" in message and "valid integer" in message
    assert "secret" not in message and "input" not in message and "ctx" not in message


def test_validation_errors_drop_context_values() -> None:
    helper = getattr(formatters, "format_validation_errors", None)
    assert callable(helper), "The validation error guard is missing"
    exc = ValidationError.from_exception_data(
        "Payload",
        [
            {
                "type": "assertion_error",
                "loc": ("count",),
                "input": "input-secret",
                "ctx": {"error": AssertionError("context-secret")},
            }
        ],
    )
    message = helper(exc)
    # Pydantic embeds ctx['error'] in msg for custom validators too.
    assert "count" in message
    assert "input-secret" not in message and "context-secret" not in message


async def test_uncaught_internal_failure_has_a_log_reference() -> None:
    from brain_v42.mcp.business_errors import _wrap

    async def failing() -> None:
        raise RuntimeError("SELECT secret")

    with capture_logs() as logs, pytest.raises(ToolError) as caught:
        await _wrap(failing)()
    message = str(caught.value)
    match = re.search(r"internal error RuntimeError \(ref ([0-9a-f]{12})\)", message)
    assert match is not None
    assert "SELECT" not in message and "secret" not in message
    assert logs[0]["correlation_id"] == match[1]


async def test_business_wrapper_preserves_explicit_tool_errors() -> None:
    from brain_v42.mcp.business_errors import _wrap

    error = ToolError("curated refusal")

    async def failing() -> None:
        raise error

    with capture_logs() as logs, pytest.raises(ToolError) as caught:
        await _wrap(failing)()
    assert caught.value is error
    assert logs == []


async def test_delivery_transport_keeps_credential_exceptions_out_of_logs() -> None:
    from brain_v42.mcp.business_errors import _wrap

    error = RuntimeError("credential-secret")

    async def failing() -> None:
        raise error

    with capture_logs() as logs, pytest.raises(RuntimeError) as caught:
        await _wrap(failing, report_internal_errors=False)()
    assert caught.value is error
    assert logs == []
