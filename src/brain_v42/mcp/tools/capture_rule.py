"""Shared routing guidance for knowledge capture tools.

The five capture tools publish an explicit description built from this rule, so
their docstrings are not what callers see (their Args sections still feed the
parameter descriptions).
"""

CAPTURE_RULE = (
    "Which capture tool: a choice between options -> brain_log_decision; a procedure -> "
    "brain_create_runbook; code -> brain_save_snippet; durable architecture -> "
    "brain_propose_adr; anything else -> brain_learn."
)
