"""Thin cron/systemd entry point.

Implementation lives at ``brain_v42.scripts.scrub_xml_tool_call_leak`` (installed with the
package). This wrapper is aliased into ``sys.modules`` so it stays the exact
same module object as ``brain_v42.scripts.scrub_xml_tool_call_leak`` -- existing operator
invocations (``python scripts/scrub_xml_tool_call_leak.py ...``) and any code that imports or
patches ``scripts.scrub_xml_tool_call_leak`` keep working unchanged, private helpers included.
"""

from __future__ import annotations

import asyncio
import sys

from brain_v42.scripts import scrub_xml_tool_call_leak as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    sys.exit(asyncio.run(_impl.main()))
