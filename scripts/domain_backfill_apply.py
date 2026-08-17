"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.domain_backfill_apply`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.domain_backfill_apply`` -- existing operator
invocations (``python scripts/domain_backfill_apply.py ...``) and any code that imports or
patches ``scripts.domain_backfill_apply`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import sys

from brain_v42.scripts import domain_backfill_apply as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    sys.exit(_impl.main())
