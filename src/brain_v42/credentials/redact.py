"""Safe display helpers for credential-adjacent identifiers."""

import re


def sanitize_label(value: str | None) -> str | None:
    """Bound untrusted labels before escaping them for structured events."""
    return None if value is None else re.sub(r"[^a-z0-9.:_*-]", "_", value[:64])


def short_id(value: str | None) -> str:
    """Return a bounded identifier representation suitable for logs."""
    if not value:
        return "-"
    return value if len(value) <= 8 else f"{value[:8]}…"
