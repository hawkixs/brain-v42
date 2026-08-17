"""Entry point for python -m brain_v42.

This module is invoked when running:
    python -m brain_v42
    uv run python -m brain_v42

It delegates to the server.py __main__ block by re-running the server module.
The canonical entry point is `python -m brain_v42.mcp.server`.
"""

if __name__ == "__main__":
    import runpy

    runpy.run_module("brain_v42.mcp.server", run_name="__main__", alter_sys=True)
