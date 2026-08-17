"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.mint_dream_capability_registry`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.mint_dream_capability_registry`` -- existing operator
invocations (``python scripts/mint_dream_capability_registry.py ...``) and any code that imports or
patches ``scripts.mint_dream_capability_registry`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import sys

from brain_v42.scripts import mint_dream_capability_registry as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    sys.exit(_impl.main())
