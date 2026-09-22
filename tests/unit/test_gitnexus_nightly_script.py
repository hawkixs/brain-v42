"""The nightly GitNexus reindex refreshes the index and writes nothing else.

Ticket 07b9e892. Without an injection-free flag, `gitnexus analyze` also copies
six standard skills into `.claude/skills/` and `.agents/skills/`, and rewrites the
generated section of `CLAUDE.md` and `AGENTS.md`. The cron at 04:30 did both
every night: it reinstalled the skills pruned on 2026-09-22 (four task skills
plus duplicates of `cli` and `guide`), which a manual deletion could not outlast
past the morning, and it rewrote two files that are now tracked by git.

`--index-only` is GitNexus' own "pure index mode": no AGENTS.md, no CLAUDE.md,
no skills. The index still advances, which the script verifies on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gitnexus-nightly.sh"


#: The binary followed by the subcommand -- not a mere mention of the word, which
#: the script's own progress lines (`echo "... analyze exit status"`) carry.
_INVOCATION = re.compile(r"""(?:\$\{?GITNEXUS_BIN\}?"?|\bgitnexus)\s+analyze\b""")


def _analyze_invocations() -> list[str]:
    """Every command line of the script that runs `analyze`, comments excluded."""
    return [
        line.strip()
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and _INVOCATION.search(line)
    ]


def test_the_nightly_script_runs_exactly_one_analyze() -> None:
    assert len(_analyze_invocations()) == 1, _analyze_invocations()


def test_the_nightly_analyze_injects_nothing_into_the_checkout() -> None:
    (invocation,) = _analyze_invocations()

    assert "--index-only" in invocation.split()
    # `--skills` generates community skills; `--index-only` would no-op it, but a
    # line asking for it states an intent this repository refused.
    assert "--skills" not in invocation.split()


def test_the_nightly_analyze_keeps_embeddings_and_its_wal_threshold() -> None:
    """Going injection-free must not cost what the index is run for."""
    (invocation,) = _analyze_invocations()

    assert "--embeddings" in invocation.split()
    assert re.search(r"--wal-checkpoint-threshold \d+", invocation)
