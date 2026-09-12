"""Isolated ``codex exec`` adapter for one Dream phase -- CLI entry point.

Moved to :mod:`brain_v42.agents.providers.codex` (lot 1 of the agent runtime
extraction, Brain ticket c31bad72). The ``argparse`` CLI and ``main()`` moved
there too in lot 2 (Brain ticket afd56820): ``run_phase`` no longer shells
out to this module by name, it invokes ``brain_v42.agents.providers.codex``
directly. This file stays a thin shim re-exporting every name
``tests/unit/test_dream_codex_runner.py`` and
``tests/unit/test_dream_provider_chain.py`` import from
``scripts.dream.codex_runner`` by name -- including ``subprocess`` itself,
which those tests monkeypatch (``runner.subprocess.Popen``); patching that
shared module object also patches the ``subprocess.Popen`` the package's
``run_codex`` calls.
"""

from __future__ import annotations

import subprocess  # noqa: F401  (re-exported: tests monkeypatch runner.subprocess.Popen)

from brain_v42.agents.providers.codex import (
    _DISABLED_FEATURES as _DISABLED_FEATURES,
)
from brain_v42.agents.providers.codex import (
    PHASE_TOOL_ALLOWLISTS as PHASE_TOOL_ALLOWLISTS,
)
from brain_v42.agents.providers.codex import (
    brain_tool_call_completed as brain_tool_call_completed,
)
from brain_v42.agents.providers.codex import (
    build_codex_command as build_codex_command,
)
from brain_v42.agents.providers.codex import (
    main as main,
)
from brain_v42.agents.providers.codex import (
    run_codex as run_codex,
)

if __name__ == "__main__":
    raise SystemExit(main())
