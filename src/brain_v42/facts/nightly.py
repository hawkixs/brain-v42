"""The nightly claim-verification step (ADR 27 lot C): issuer and key formats.

Both formats are module constants so the orchestrator, the `claim_verify` CLI
and the `claims_verification_last_night` probe (PR 3) agree on the exact
literal a verdict's `issuer_identity`/`idempotency_key` carries -- the fact's
exact-match query (spec §7.2) depends on this being the ONE place either
format is built.
"""

from __future__ import annotations

from datetime import date
from typing import Final

#: No product path outside this step mints this prefix (spec §2): every other
#: MCP issuer is `mcp:<actor>` (`mcp/tools/claim_tools.py`).
ISSUER_PREFIX: Final = "dream:verify:"

#: Constant per run date: every verdict the step writes for one run carries
#: exactly this key, derived from that run's OWN `run_date` (spec §5.2).
KEY_PREFIX: Final = "dream-verify:v1:"


def issuer_for(run_id: int) -> str:
    """The declared issuer for verdicts written by the nightly run `run_id`."""
    return f"{ISSUER_PREFIX}{run_id}"


def key_for(run_date: date) -> str:
    """The idempotency key shared by every verdict one run writes."""
    return f"{KEY_PREFIX}{run_date.isoformat()}"
