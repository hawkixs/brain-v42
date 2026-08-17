"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.canary_roadmap_model`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.canary_roadmap_model`` -- existing operator
invocations (``python scripts/canary_roadmap_model.py ...``) and any code that imports or
patches ``scripts.canary_roadmap_model`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import asyncio
import sys

from brain_v42.scripts import canary_roadmap_model as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(asyncio.run(_impl.main()))
