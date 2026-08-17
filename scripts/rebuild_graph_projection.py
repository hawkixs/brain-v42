"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.rebuild_graph_projection`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.rebuild_graph_projection`` -- existing operator
invocations (``python scripts/rebuild_graph_projection.py ...``) and any code that imports or
patches ``scripts.rebuild_graph_projection`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import sys

from brain_v42.scripts import rebuild_graph_projection as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
