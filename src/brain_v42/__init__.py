"""brain_v42 - Second Cerveau v42.

Importing the package installs a leak-free structlog default, on purpose.

structlog's stock configuration ends in ``ConsoleRenderer()``, which renders
``exc_info`` through rich (when installed) with every frame's local variables.
During a Postgres outage the asyncpg connect frame holds ``password=...``, which
then lands in clear in the systemd journal (ticket 1bffd79a, operator decision
Q115 option a). Dozens of entry points -- ``__main__`` modules, console scripts,
the root ``scripts/*.py`` -- never configure structlog themselves, so the only
place that covers all of them is the package they all import.

Only the ``processors`` key is set, and only when nothing configured structlog
yet: an explicit configuration made earlier (or later, as the MCP server and the
metrics sidecar do) keeps precedence, and a later partial ``structlog.configure``
(for example ``logger_factory=...``) inherits these processors.
"""

import structlog

from brain_v42.safe_logging import safe_console_renderer


def _install_safe_logging_default() -> None:
    if structlog.is_configured():
        return
    # Take structlog's own default chain rather than a copy of it, so a structlog
    # upgrade that changes the defaults is followed; only the renderer is swapped.
    structlog.configure(
        processors=[
            safe_console_renderer()
            if isinstance(processor, structlog.dev.ConsoleRenderer)
            else processor
            for processor in structlog.get_config()["processors"]
        ]
    )


_install_safe_logging_default()
