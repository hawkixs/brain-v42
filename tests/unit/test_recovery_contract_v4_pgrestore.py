"""The v4 pg_restore asset retains the v3 pg_restore canonicalisations."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"


def _cte_names(sql: str) -> set[str]:
    return set(re.findall(r"(?m)^([a-z][a-z0-9_]*) AS \($", sql))


def test_v4_pgrestore_preserves_v3_specific_cast_canonicalisation() -> None:
    live = (RECOVERY / "brain-v42-v4.sql").read_text(encoding="utf-8")
    pgrestore = (RECOVERY / "brain-v42-v4-pgrestore.sql").read_text(encoding="utf-8")
    v3_pgrestore = (RECOVERY / "brain-v42-v3-pgrestore.sql").read_text(encoding="utf-8")

    assert pgrestore != live
    assert "observed_session_constraints" in pgrestore
    assert "observed_artifact_constraints" in pgrestore
    assert "'::character varying::text', '::character varying'" in pgrestore
    assert "']::text[]', ']'" in pgrestore
    assert "project_context_updated_at_039" in pgrestore
    assert "observed_session_constraints" not in live
    assert "observed_artifact_constraints" not in live
    assert "'::character varying::text', '::character varying'" not in live
    assert "']::text[]', ']'" not in live
    assert _cte_names(pgrestore) - _cte_names(live) == {
        "observed_artifact_constraints",
        "observed_session_constraints",
    }
    assert not (_cte_names(live) - _cte_names(pgrestore))
    for token in (
        "'::character varying::text', '::character varying'",
        "']::text[]', ']'",
        "public.brain_sessions",
        "public.brain_session_artifacts",
        "canonical_definition",
    ):
        assert token in v3_pgrestore
        assert token in pgrestore
