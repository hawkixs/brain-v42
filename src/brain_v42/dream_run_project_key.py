"""The project sentinel of the global Dream phases.

`dream_runs.project_key` (migration 042) distinguishes three states, and all
three carry meaning:

- `NULL` — a row written BEFORE 042. Forever: there was no backfill, and there
  will be none. Any retrospective per-project measurement over the earlier rows
  is impossible, definitively. (No count is pinned here: `dream_runs` gains six
  to nine rows every night at 06:00, and a figure written into a comment is
  wrong by the following morning.)
- `'*'` — a GLOBAL phase. `extract`, `roadmap` and `sweep` sit outside the loop
  and run once a night, for nobody in particular: they have no project to name.
  `RESONANCE` sets it too, although it is dead and unwired, so as not to be the
  only inconsistent writer the day someone reconnects it.
- a kebab-case key — a PER-PROJECT phase, as received by the orchestrator.

This module lives at the package root and imports NOTHING, deliberately. The
layering graph measures `_root: []` while eight sub-packages target the root: a
single outgoing edge from here would close eight cycles and make
`scripts/check_module_layering.py` exit with `rc=2`, before pytest even runs.

And it lives under `src/`, not under `scripts/`, because the allowed direction is
`scripts → src`. The `Dockerfile` never copies `scripts/`: an `src → scripts`
import would be green locally, green in CI, and would break the production image
at import time.
"""

from __future__ import annotations

# DO NOT route this value through `canonicalize_project_key`: its
# `^[a-z0-9]+([:-][a-z0-9]+)*$` pattern rejects it. On the three best-effort
# writers the exception would be swallowed by design, and the column would stay
# NULL silently, every night, on the global phases.
GLOBAL_PHASE_PROJECT_KEY = "*"
