"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.repair_plan_index`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.repair_plan_index`` -- existing operator
invocations (``python scripts/repair_plan_index.py ...``) and any code that imports or
patches ``scripts.repair_plan_index`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import sys

from brain_v42.scripts import repair_plan_index as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
