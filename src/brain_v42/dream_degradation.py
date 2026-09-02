"""The prefix that tells a DEGRADED night from a merely talkative one.

A Dream phase can succeed while having been served by its FALLBACK model:
`dream_runs.status` reads `'done'`, and the degradation sentence travels in
`error_message` without touching the status. That is deliberate — a successful
fallback is not a failure, and confusing it with one has already cost a ticket
(`4480d3df`, a deferral mistaken for a timeout).

But `error_message` is NOT reserved for degradation. `extract` legitimately
writes "N ticket(s) deferred or timed out before run deadline" there on `'done'`
runs. A reader settling for "there is a message" would therefore read a perfectly
clean night as a degraded one. The contract is on the PREFIX, and on it alone.

DO NOT STRIP THE ACCENTS FROM THIS VALUE, nor rewrite it "identically" elsewhere.
The rows already in the database carry it accented and there is no backfill: an
ASCII variant would not merely orphan the past rows, it would make them mute
without a single test turning red — the reader would simply stop finding what
they are looking for. That is why the value lives here rather than in two
literals held in agreement by discipline.

This module lives at the package root and imports NOTHING, like
`dream_run_project_key` and for the same reason: the layering graph measures
`_root: []`, and a single outgoing edge from here closes eight cycles and makes
`scripts/check_module_layering.py` exit with `rc=2`, before pytest even runs.

And it lives under `src/`, not under `scripts/`, because the allowed direction is
`scripts → src`. The `Dockerfile` never copies `scripts/`: an `src → scripts`
import would be green locally, green in CI, and would break the production image
at import time.
"""

from __future__ import annotations

# Written by `scripts/roadmap_curate.py::_degradation_notice`, read by
# `brain_v42.services.dream_run_service`. Both sides must take it from this
# module — a literal copied into the reader OR into a test cancels the guard,
# exactly like the retyped Alembic revision of learning `8dc7e042`.
DEGRADED_PREFIX = "DÉGRADÉ"
