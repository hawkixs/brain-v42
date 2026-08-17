"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.benchmark`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.benchmark`` -- existing operator
invocations (``python scripts/benchmark.py ...``) and any code that imports or
patches ``scripts.benchmark`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import asyncio
import sys

from brain_v42.scripts import benchmark as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    asyncio.run(_impl.main())
