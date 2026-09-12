"""Isolated ``claude -p`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.claude` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). The ``argparse`` CLI and ``main()`` moved
there too in lot 2 (Brain ticket afd56820): ``run_phase`` no longer shells
out to this module by name, it invokes ``brain_v42.agents.providers.claude``
directly. This file stays a thin shim re-exporting every name
``tests/unit/test_dream_claude_runner.py`` and
``tests/unit/test_dream_provider_chain.py`` import from
``scripts.dream.claude_runner`` by name.
"""

from __future__ import annotations

from brain_v42.agents.providers.claude import (
    PHASE_TOOL_ALLOWLISTS as PHASE_TOOL_ALLOWLISTS,
)
from brain_v42.agents.providers.claude import (
    brain_tool_call_completed as brain_tool_call_completed,
)
from brain_v42.agents.providers.claude import (
    build_claude_command as build_claude_command,
)
from brain_v42.agents.providers.claude import (
    build_claude_mcp_config as build_claude_mcp_config,
)
from brain_v42.agents.providers.claude import (
    claude_child_environment as claude_child_environment,
)
from brain_v42.agents.providers.claude import (
    main as main,
)
from brain_v42.agents.providers.claude import (
    run_claude as run_claude,
)

if __name__ == "__main__":
    raise SystemExit(main())
