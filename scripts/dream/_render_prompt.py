"""Shim re-exporting ``brain_v42.agents.prompt`` (lot 2, Brain ticket afd56820).

The implementation moved to :mod:`brain_v42.agents.prompt`. This file stays
for two callers that still name it directly:
``scripts/dream/_promote_smoke.sh`` (``python3 -m scripts.dream._render_prompt``)
and ``tests/unit/test_render_prompt.py`` (``from scripts.dream._render_prompt
import render``).
"""

from __future__ import annotations

import sys

from brain_v42.agents.prompt import main as main
from brain_v42.agents.prompt import render as render

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
