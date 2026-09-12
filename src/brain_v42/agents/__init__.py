"""Shared headless-agent runtime: protocol, sandbox, capability boundary.

Lot 1 of the agent runtime extraction (Brain ticket c31bad72). Consumers today:
the nightly Dream orchestrator (``scripts/dream/{codex,agy,claude}_runner.py``,
now thin shims over ``brain_v42.agents.providers``) and the extract rescue link
(``src/brain_v42/scripts/agy_completion.py``, via
:mod:`brain_v42.agents.sandbox`). A PR reviewer service is the next planned
consumer.

This package must not import anything from the top-level ``scripts/`` tree:
``scripts/`` depends on it, never the reverse.
"""

from __future__ import annotations
