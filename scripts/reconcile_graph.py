"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.reconcile_graph`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.reconcile_graph`` -- existing operator
invocations (``python scripts/reconcile_graph.py ...``) and any code that imports or
patches ``scripts.reconcile_graph`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import asyncio
import sys

from brain_v42.scripts import reconcile_graph as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(asyncio.run(_impl.main()))
