"""The runbook's prose and `ops/recovery/current.json` name the SAME contract.

`test_recovery_current_binding.py` makes `ops/recovery/current.json` the
machine-readable declaration; `test_runbook_normative_values_have_one_source.py`
makes `dr-current` the ONE place `docs/PLAN_INDEX_REPAIR_RUNBOOK.md` may write a
head or an asset name. Neither test compares the two documents to each other,
so nothing stopped them from drifting apart again exactly the way `dr-current`
drifted from production for the six days ticket `3b3d64a1` was opened over. This
module is that missing comparison: it reads the binding and asserts the
declaration names the same head, the same live asset and the same restored
asset — never a copy pinned in this file, which would go stale exactly like the
document it is meant to guard.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_JSON = REPO_ROOT / "ops" / "recovery" / "current.json"
RUNBOOK = REPO_ROOT / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"
DECLARATION_REGION = "dr-current"


def _declaration_block(document: str) -> str:
    start = f"<!-- {DECLARATION_REGION}:start -->"
    end = f"<!-- {DECLARATION_REGION}:end -->"
    assert start in document and end in document, "la région `dr-current` a disparu du runbook"
    return document.split(start, 1)[1].split(end, 1)[0]


def test_the_declaration_names_the_head_the_binding_declares() -> None:
    binding = json.loads(CURRENT_JSON.read_text(encoding="utf-8"))
    block = _declaration_block(RUNBOOK.read_text(encoding="utf-8"))

    head = binding["schema_head"]
    assert f"`{head}`" in block, (
        f"`dr-current` ne cite pas la tête `{head}` déclarée par ops/recovery/current.json"
    )


def test_the_declaration_names_the_live_and_restored_assets_the_binding_declares() -> None:
    binding = json.loads(CURRENT_JSON.read_text(encoding="utf-8"))
    block = _declaration_block(RUNBOOK.read_text(encoding="utf-8"))

    live_asset = Path(binding["attestation_sql"]["path"]).name
    restored_asset = Path(binding["restored_attestation_sql"]["path"]).name

    assert live_asset in block, (
        f"`dr-current` ne cite pas l'actif live `{live_asset}` déclaré par "
        "ops/recovery/current.json"
    )
    assert restored_asset in block, (
        f"`dr-current` ne cite pas l'actif restauré `{restored_asset}` déclaré par "
        "ops/recovery/current.json"
    )
