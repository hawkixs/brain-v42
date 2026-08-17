"""The repair's required Alembic head must track the repository head.

``plan_index_repair_store`` refuses to run unless the deployed schema equals
``_REQUIRED_ALEMBIC_HEAD``. The gate is right — the repair mutates
``indexed_plans`` rows and must not run against a schema it was not reviewed
against. What is wrong is pinning it as a constant nobody is forced to revisit:
migration 041 landed in 59e5ed1b without bumping it, and the desync stayed
invisible until the branch was finally pushed and CI ran, five integration
tests failing at once with ``alembic_head_mismatch``.

That was the fourth hardcoded Alembic head found on 2026-08-06 (twice in the
Codex gateway at 037, once in the recovery contract at 039, here at 040). This
test makes the constant self-checking: a new migration fails it immediately, in
the unit suite, instead of surfacing as an opaque runtime refusal much later.

Bumping the constant stays a deliberate act. The failure message says what to
review before doing it.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from brain_v42.maintenance.plan_index_repair_store import _REQUIRED_ALEMBIC_HEAD

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _repository_head() -> str:
    """Return the single head revision declared under ``alembic/versions``."""
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"expected exactly one Alembic head, got {heads}"
    return heads[0]


def test_required_head_matches_the_repository_head() -> None:
    """A migration that lands without revisiting the repair gate must fail here."""
    head = _repository_head()

    assert _REQUIRED_ALEMBIC_HEAD == head, (
        f"plan_index_repair_store._REQUIRED_ALEMBIC_HEAD is {_REQUIRED_ALEMBIC_HEAD!r} "
        f"but the repository head is {head!r}.\n"
        f"Do not bump it blindly: review what {head} changes on the tables the repair "
        f"writes (indexed_plans, indexed_plan_chunks, project_contexts) — new triggers, "
        f"new constraints or new NOT NULL columns without a default would each change "
        f"the repair's behaviour. Bump only once that review is done."
    )
