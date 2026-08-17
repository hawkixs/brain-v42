"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.migrate_neo4j_to_pg`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.migrate_neo4j_to_pg`` -- existing operator
invocations (``python scripts/migrate_neo4j_to_pg.py ...``) and any code that imports or
patches ``scripts.migrate_neo4j_to_pg`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import asyncio
import sys

from brain_v42.scripts import migrate_neo4j_to_pg as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    sys.exit(asyncio.run(_impl.main()))
