"""The error family the fact tools surface to a caller — stable codes, safe text.

Kept apart from the tools module so `business_errors` (the disclosure
allowlist) never imports a FastMCP registration module, the way the other
families come from `models/` and `services/`.
"""

from __future__ import annotations

from typing import Literal

type FactToolErrorCode = Literal["invalid_argument", "unknown_fact"]

#: A caller-supplied name is echoed at most this far: an error is a message,
#: not a mirror.
ECHO_MAX_CHARS = 64


class FactToolError(Exception):
    """Carry only safe, operator-actionable errors across the masked MCP boundary."""

    def __init__(self, code: FactToolErrorCode, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")
