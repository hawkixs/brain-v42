"""Alias the packaged CLI so operator invocations and imported patches agree."""

from __future__ import annotations

import sys

from brain_v42.scripts import repair_merge_chains as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
