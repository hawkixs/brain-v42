"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.roadmap_curate`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.roadmap_curate`` -- existing operator
invocations (``python scripts/roadmap_curate.py ...``) and any code that imports or
patches ``scripts.roadmap_curate`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import sys

from brain_v42.scripts import roadmap_curate as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    sys.exit(_impl.main())
