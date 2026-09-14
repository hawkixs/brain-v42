"""The Dream's adapters over the shared headless-agent runtime.

Lot 1 and 2 of the agent runtime extraction (Brain tickets c31bad72,
afd56820) moved the codex/agy/claude runners, the capability boundary and the
phase chain from ``scripts/dream/`` into this package. Brain ticket b2a2d1a5
then split it in two: everything generic -- the providers, the sandbox, the
child environment, the chain state machine, the envelope parsers -- lives in
the ``headless_agents`` workspace member (``packages/headless-agents/``),
installable on its own; what remains here is the Dream POLICY over it. This
package builds the ``(project, phase)`` capability profile from
``brain_v42.mcp.dream_capabilities`` and the registry, keeps the argv contracts
``dream.sh`` invokes (``python -m brain_v42.agents.{run_phase_chain,
providers.*}``), and owns the phase orchestration (``phase``, ``lines``,
``prompt``) the runtime must never learn about.

Consumers today: the nightly Dream orchestrator (``scripts/dream.sh`` and the
thin ``scripts/dream/*_runner.py`` shims) and the extract rescue link
(``src/brain_v42/scripts/agy_completion.py``, via ``sandbox``).

This package must not import anything from the top-level ``scripts/`` tree:
``scripts/`` depends on it, never the reverse. ``headless_agents`` must not
import ``brain_v42``: ``tests/unit/headless_agents/test_package_boundary.py``
guards it.
"""

from __future__ import annotations
