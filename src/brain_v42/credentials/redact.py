"""Safe display helpers for credential-adjacent identifiers."""


def short_id(value: str | None) -> str:
    """Return a bounded identifier representation suitable for logs."""
    if not value:
        return "-"
    return value if len(value) <= 8 else f"{value[:8]}…"
