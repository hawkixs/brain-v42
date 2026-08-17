"""Scrub stranded XML tool-call fragments from entity text columns.

Historical corruption: some brain_learn / brain_log_decision invocations
serialized their own Claude XML tool-call parameters into the stored
content field. The signature is a trailing sequence of the form
``<closing-tag>\\n<parameter name="...">...`` — a real newline followed
by a real XML tag — appearing at the end of otherwise-legit content.

Meta-references in OTHER entities describe this pattern using escaped
backslashes (literal ``\\n`` as two characters). The scrub uses a real
newline in the pattern, so meta-references are preserved.
"""

from __future__ import annotations

import re

# Real newline + either another leaked param or an upstream invoke/function_calls
# close tag. Literal \n meta-refs don't match because the regex requires a real
# newline char. The closing left-side tag name can be any brain_* parameter name
# (insight, topic, context, reasoning, decision_made, consequences, alternatives,
# tags, current_focus, …) or the generic <parameter> wrapper.
_LEAK_PATTERN = re.compile(
    r"</[a-z_]+>\n(?:<parameter name=|</invoke>|</function_calls>|<function_calls>)",
    flags=re.DOTALL,
)


def scrub_xml_tool_call_leak(text: str) -> tuple[str, bool]:
    """Strip a trailing stranded XML tool-call leak from ``text``.

    Returns ``(cleaned, was_modified)``. Idempotent — calling twice yields
    the same string and ``was_modified=False`` on the second call. Returns
    the input unchanged when no leak is detected.
    """
    match = _LEAK_PATTERN.search(text)
    if not match:
        return text, False
    return text[: match.start()], True
