"""Static authority checks for the head-039 recovery contract."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).parents[2]
RECOVERY = ROOT / "ops" / "recovery"
V3_JSON = RECOVERY / "brain-v42-v3.json"
V4_JSON = RECOVERY / "brain-v42-v4.json"
V4_SQL = RECOVERY / "brain-v42-v4.sql"
RUNBOOK = ROOT / "docs" / "PLAN_INDEX_REPAIR_RUNBOOK.md"
ARCHITECTURE = ROOT / "docs" / "ARCHITECTURE.md"
SCHEMA = ROOT / "docs" / "SCHEMA.md"

V3_SHA256 = {
    "brain-v42-v3.json": "dcd717edc885689937f1126668b56b7cdde310deb90d447d4f1f2abbf19ee27f",
    "brain-v42-v3.sql": "2160b75148ffeaa3af1a8f0115319c7b9d32e406093e08d5c2283ca0b43cb8f3",
    "brain-v42-v3-pgrestore.sql": (
        "d46bcdbbc1e560bb7859ddfff9883572fd4f6462cc38732520dd880d3155fd6a"
    ),
}

HISTORICAL_BINDINGS = {
    ("adrs", "trg_adrs_updated", "public.update_updated_at()"),
    ("decisions", "trg_decisions_updated", "public.update_updated_at()"),
    ("features", "set_features_updated_at", "public.update_updated_at()"),
    ("indexed_plans", "set_indexed_plans_updated_at", "public.update_updated_at()"),
    ("learnings", "trg_learnings_updated", "public.update_updated_at()"),
    ("runbooks", "trg_runbooks_updated", "public.update_updated_at()"),
    ("snippets", "trg_snippets_updated", "public.update_updated_at()"),
}


def _expected_v4() -> dict[str, Any]:
    document = cast(dict[str, Any], copy.deepcopy(json.loads(V3_JSON.read_text(encoding="utf-8"))))
    checks = document["checks"]
    assert isinstance(checks, list)
    by_id = {check["id"]: check for check in checks}
    by_id["alembic_head"]["revision"] = "039"
    by_id["catalog_counts"].update(foreign_keys=26, indexes=128)
    by_id["table_set"]["tables"] = sorted(
        [*by_id["table_set"]["tables"], "ticket_extraction_attempts"]
    )
    checks.append(
        {
            "id": "project_context_updated_at_039",
            "kind": "postgresql_catalog_invariant",
            "name": "project_context_updated_at_039",
            "revision": "039",
        }
    )
    document["checks"] = sorted(checks, key=lambda check: check["id"])
    document["contract_id"] = "brain-v42/postgresql-recovery/v4"
    document["schema_version"] = 4
    return document


def test_v4_json_is_the_exact_v3_delta() -> None:
    raw = V4_JSON.read_bytes()
    document = json.loads(raw)

    assert document == _expected_v4()
    assert (
        raw
        == (
            json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
    )
    assert len(document["checks"]) == 25


def test_v3_recovery_assets_remain_byte_identical() -> None:
    assert {
        name: hashlib.sha256((RECOVERY / name).read_bytes()).hexdigest() for name in V3_SHA256
    } == V3_SHA256


def test_v4_live_sql_tracks_dream_038_and_adds_only_project_context_039() -> None:
    sql = V4_SQL.read_text(encoding="utf-8")
    expected_ids = {check["id"] for check in _expected_v4()["checks"]}

    assert sql.startswith("WITH ") and sql.endswith(";\n") and sql.count(";") == 1
    assert "'brain-v42/postgresql-recovery/v4'" in sql
    assert "'schema_version', 4" in sql
    assert "'039'" in sql
    assert "'ticket_extraction_attempts'" in sql
    assert {check_id for check_id in expected_ids if f"'{check_id}'" in sql} == expected_ids
    check_rows = sql[sql.index("check_rows(id, expected, observed, passed) AS (") :]
    assert all(check_rows.count(f"'{check_id}'") == 1 for check_id in expected_ids)


def test_v4_catalog_check_is_derived_from_exact_head_039_observations() -> None:
    sql = V4_SQL.read_text(encoding="utf-8")

    for cte in (
        "historical_function",
        "dedicated_function",
        "function_acl",
        "project_context_trigger",
        "expected_updated_at_bindings",
        "historical_bindings",
        "recovery_039_observation",
    ):
        assert re.search(rf"(?m)^{cte}(?:\([^\n]*\))? AS \($", sql)

    assert "83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59" in sql
    assert "60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419" in sql
    assert sql.count("'prosrc_octets'") == 2
    assert ") = 96 AS passed" in sql
    assert ") = 391 AS passed" in sql
    for token in (
        "prokind = 'f'",
        "provolatile = 'v'",
        "proparallel = 'u'",
        "NOT function_record.prosecdef",
        "NOT function_record.proleakproof",
        "NOT function_record.proisstrict",
        "NOT function_record.proretset",
        "function_record.pronargs = 0",
        "function_record.pronargdefaults = 0",
        "function_record.proargtypes = ''::oidvector",
        "function_record.proconfig IS NULL",
        "brain_v42.allow_explicit_project_context_updated_at",
        "explicit_project_context_updated_at_null",
        "pg_catalog.aclexplode",
        "pg_catalog.acldefault('f'",
        "tgtype = 19",
        "tgattr = ''::int2vector",
        "tgqual IS NULL",
        "tgparentid = 0",
        "tgconstraint = 0",
        "tgconstrrelid = 0",
        "tgconstrindid = 0",
        "NOT trigger_record.tgdeferrable",
        "NOT trigger_record.tginitdeferred",
        "tgoldtable IS NULL",
        "tgnewtable IS NULL",
        "tgenabled = 'O'",
        "NOT trigger_record.tgisinternal",
        "tgnargs = 0",
        "tgargs = ''::bytea",
    ):
        assert token in sql

    for table_name, trigger_name, function_name in HISTORICAL_BINDINGS | {
        (
            "project_contexts",
            "trg_project_contexts_updated",
            "public.set_project_context_updated_at()",
        )
    }:
        assert f"('{table_name}', '{trigger_name}', '{function_name}')" in sql

    catalog_branch = sql[
        sql.index("'project_context_updated_at_039'") : sql.index("'project_contexts_nonempty'")
    ]
    assert "FROM recovery_039_observation" in catalog_branch
    assert "recovery_039_observation.passed" in catalog_branch


def _project_context_runbook_section() -> str:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    start = "<!-- project-context-cas-039:start -->"
    end = "<!-- project-context-cas-039:end -->"
    assert runbook.count(start) == 1
    assert runbook.count(end) == 1
    section = runbook.split(start, 1)[1].split(end, 1)[0]
    return re.sub(r"\s+", " ", section)


def _normal_runtime_runbook_section() -> str:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split("## 11. Publish the normal runtime last", 1)[1].split(
        "## Rollback before finalization", 1
    )[0]
    return re.sub(r"\s+", " ", section)


def _canonical_mcp_preflight_fence() -> str:
    runbook = (ROOT / "deploy" / "systemd" / "MCP_HTTP_RUNBOOK.md").read_text(encoding="utf-8")
    section_start = "## Preflight\n"
    section_end = "### Préflight de fichiers hors systemd"
    assert runbook.count(section_start) == 1
    assert runbook.count(section_end) == 1
    preflight = runbook.split(section_start, 1)[1].split(section_end, 1)[0]
    fences = re.findall(r"```bash\n(.*?)\n```", preflight, flags=re.DOTALL)
    assert len(fences) == 1
    return cast(str, fences[0])


def _mcp_unit_loops(fence: str) -> list[tuple[tuple[str, ...], str, str]]:
    loops = re.finditer(
        r"(?ms)^for unit in \\\n(?P<units>(?:  [^\n]+\n)+?)  (?P<last>[^\n;]+); do\n"
        r"(?P<body>.*?)^done$",
        fence,
    )
    return [
        (
            (
                *(line.strip().removesuffix("\\").rstrip() for line in match["units"].splitlines()),
                match["last"],
            ),
            match["body"],
            match[0],
        )
        for match in loops
    ]


def _assert_canonical_mcp_preflight_contract(fence: str) -> None:
    expected_units = (
        "brain-mcp-http.service",
        "brain-mcp-http-watchdog.service",
        "brain-mcp-http-watchdog.timer",
    )
    loops = _mcp_unit_loops(fence)
    assert [units for units, _, _ in loops] == [expected_units, expected_units]

    publication_body = loops[1][1]
    backup_condition = 'if [[ -e "$user_unit_dir/$unit" || -L "$user_unit_dir/$unit" ]]; then'
    backup = 'cp -a -- "$user_unit_dir/$unit" "$backup_dir/"'
    mktemp = 'new_unit="$(mktemp "$user_unit_dir/.$unit.new.XXXXXX")"'
    publication_sequence = re.compile(
        r".*?".join(
            re.escape(token)
            for token in (
                backup_condition,
                backup,
                "fi",
                mktemp,
                'install -m 0644 "$render_dir/$unit" "$new_unit"',
                'cmp -s "$render_dir/$unit" "$new_unit"',
                'mv -f -- "$new_unit" "$user_unit_dir/$unit"',
                'new_unit=""',
            )
        ),
        flags=re.DOTALL,
    )
    assert publication_sequence.search(publication_body)

    sequence = re.compile(
        r".*?".join(
            re.escape(token)
            for token in (
                'deploy/systemd/install.sh --render-dir "$render_dir"',
                "systemctl --user stop brain-mcp-http-watchdog.timer",
                "systemctl --user stop brain-mcp-http-watchdog.service",
                "systemctl --user disable --no-reload brain-mcp-http-watchdog.timer",
                loops[1][2],
                "systemctl --user daemon-reload",
                "systemd-analyze --user verify",
                "systemctl --user show brain-mcp-http.service",
                "systemctl --user show brain-mcp-http-watchdog.timer",
            )
        ),
        flags=re.DOTALL,
    )
    assert sequence.search(fence)
    assert re.findall(r"(?m)^systemctl --user show ([^\\\s]+)", fence) == [
        "brain-mcp-http.service",
        "brain-mcp-http-watchdog.timer",
    ]


def test_runbook_requires_distinct_v4_pgrestore_and_live_gates() -> None:
    section = _project_context_runbook_section()
    ordered_tokens = (
        "backup production 037",
        "pg_restore isolated",
        "upgrade isolated 038 then 039",
        "brain-v42-v4-pgrestore.sql",
        "**isolated v4 receipt: 25/25**",
        "writers off",
        "upgrade production 038 then 039",
        "brain-v42-v4.sql",
        "**live v4 receipt: 25/25**",
        "inventory",
        "apply-paths",
        "reindex one project at a time",
        "Run `verify`",
        "Run `finalize`",
        'render_parent="$(mktemp -d "$TMPDIR/systemd-render.XXXXXX")"',
        'render_dir="$render_parent/units"',
        'chmod 0700 "$render_parent"',
        "./deploy/systemd/install.sh --check-only",
        './deploy/systemd/install.sh --render-dir "$render_dir"',
        "brain-mcp-http-watchdog.timer; do",
        "canonical MCP publication preflight",
        "systemctl --user restart brain-mcp-http.service",
        "curl --max-time 10",
        "read-only MCP call",
        "systemctl --user enable --now brain-mcp-http-watchdog.timer",
    )
    positions = [section.index(token) for token in ordered_tokens]
    assert positions == sorted(positions)
    assert "./deploy/systemd/install.sh --dry-run" not in section
    assert section.count("systemctl --user daemon-reload") == 0


def test_runbook_operator_order_has_no_numbering_gap() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    operator_order = runbook.split("Use this single operator order:", 1)[1].split(
        "Before inventory and throughout repair", 1
    )[0]

    numbers = [int(number) for number in re.findall(r"(?m)^(\d+)\. ", operator_order)]
    assert numbers == list(range(1, 19))


def test_installer_watchdog_error_recommends_isolated_preflight() -> None:
    installer = (ROOT / "deploy" / "systemd" / "install.sh").read_text(encoding="utf-8")

    assert "use --check-only, then --render-dir in a private directory" in installer
    assert "use --dry-run and follow MCP_HTTP_RUNBOOK.md for a canary upgrade" not in installer


def test_runbook_requires_canonical_mcp_publication_before_live_restart() -> None:
    project_section = _project_context_runbook_section()
    normal_runtime_section = _normal_runtime_runbook_section()
    canonical_runbook = (ROOT / "deploy" / "systemd" / "MCP_HTTP_RUNBOOK.md").read_text(
        encoding="utf-8"
    )

    publication = (
        "[canonical MCP publication preflight](../deploy/systemd/MCP_HTTP_RUNBOOK.md#preflight)"
    )
    assert publication in project_section
    assert project_section.index('./deploy/systemd/install.sh --render-dir "$render_dir"') < (
        project_section.index(publication)
    )
    assert publication in normal_runtime_section
    assert (
        normal_runtime_section.index('./deploy/systemd/install.sh --render-dir "$render_dir"')
        < (normal_runtime_section.index(publication))
        < normal_runtime_section.index("systemctl --user restart brain-mcp-http.service")
    )
    assert "`--render-dir` only creates a private artifact; it never publishes live." in (
        normal_runtime_section
    )
    assert "Do not run `daemon-reload` or restart the service before that block succeeds." in (
        normal_runtime_section
    )
    publication_targets = re.findall(
        r"\[canonical MCP publication preflight\]\(([^)]+)\)",
        RUNBOOK.read_text(encoding="utf-8"),
    )
    assert set(publication_targets) == {"../deploy/systemd/MCP_HTTP_RUNBOOK.md#preflight"}
    assert canonical_runbook.count("## Preflight\n") == 1
    _assert_canonical_mcp_preflight_contract(_canonical_mcp_preflight_fence())


@pytest.mark.parametrize(
    "mutation",
    (
        "fourth live unit",
        "missing install from render directory",
        "missing second systemctl show",
        "systemctl shows before publication loop",
        "publication body outside publication loop",
        "backup outside publication loop",
    ),
)
def test_canonical_mcp_preflight_rejects_contract_mutations(mutation: str) -> None:
    fence = _canonical_mcp_preflight_fence()
    if mutation == "fourth live unit":
        mutated = fence.replace(
            "  brain-mcp-http-watchdog.timer; do\n",
            "  brain-mcp-http-watchdog.timer " + chr(92) + "\n  brain-v42-dream.service; do\n",
            1,
        )
    elif mutation == "missing install from render directory":
        mutated = fence.replace('install -m 0644 "$render_dir/$unit" "$new_unit"\n  ', "", 1)
    elif mutation == "systemctl shows before publication loop":
        show_start = fence.index("systemctl --user show brain-mcp-http.service")
        show_block = fence[show_start:]
        publication_loop = _mcp_unit_loops(fence)[1][2]
        mutated = fence[:show_start].replace(
            publication_loop, f"{show_block}\n{publication_loop}", 1
        )
    elif mutation == "publication body outside publication loop":
        _, publication_body, publication_loop = _mcp_unit_loops(fence)[1]
        empty_publication_loop = publication_loop.replace(publication_body, "  :\n", 1)
        mutated = fence.replace(
            publication_loop, f"{empty_publication_loop}\n{publication_body}", 1
        )
    elif mutation == "backup outside publication loop":
        _, _, publication_loop = _mcp_unit_loops(fence)[1]
        backup_block = (
            '  if [[ -e "$user_unit_dir/$unit" || -L "$user_unit_dir/$unit" ]]; then\n'
            '    cp -a -- "$user_unit_dir/$unit" "$backup_dir/"\n'
            "  fi\n"
        )
        assert backup_block in publication_loop
        mutated_publication_loop = publication_loop.replace(backup_block, "", 1)
        mutated = fence.replace(publication_loop, f"{mutated_publication_loop}\n{backup_block}", 1)
    else:
        mutated = fence.replace(
            "systemctl --user show brain-mcp-http-watchdog.timer " + chr(92) + "\n",
            "",
            1,
        )

    assert mutated != fence, mutation
    with pytest.raises(AssertionError):
        _assert_canonical_mcp_preflight_contract(mutated)


def test_full_runbook_has_one_039_operator_order_and_no_live_claim() -> None:
    """The 039 order is now the record of an executed cutover, not a plan.

    It ran on 2026-08-03 and production measured `039` on 2026-08-04, so the
    section may no longer say production "remains at 037" — but it must still
    hold exactly one operator order, and still refuse to assert a live head.
    """
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = _project_context_runbook_section()

    assert section.count("upgrade isolated 038 then 039") == 1
    assert section.count("upgrade production 038 then 039") == 1
    assert "Repository target: 039. Executed on 2026-08-03" in section
    assert "Production remains at 037" not in section
    assert "Production is already running schema 039" not in runbook
    assert "Production runs schema 039" not in runbook
    architecture = re.sub(r"\s+", " ", ARCHITECTURE.read_text(encoding="utf-8"))
    assert "Repository target: 040." in architecture
    assert "Production remains at 037 before an authorized cutover." not in architecture
    schema = re.sub(r"\s+", " ", SCHEMA.read_text(encoding="utf-8"))
    # Épingle de tête, portée par un test qui parle d'autre chose — c'est un
    # doublon de test_documentation_contract.py, et sa position la rend facile à
    # manquer quand on inventorie les gardes. Bumpée à 042 le 2026-08-08, puis à
    # 043 puis 044 le 2026-08-10, puis à 045 le 2026-08-16 — troisième bascule
    # consécutive où ce doublon coûte une passe de plus, après que la suite
    # entière soit déjà repassée verte. Il n'est toujours pas retiré ici parce
    # que retirer une garde en la traversant est exactement la manœuvre que ce
    # dépôt refuse ; le retrait mérite son propre commit.
    assert "La cible du dépôt est 046." in schema
    assert "La production reste à 037 avant une bascule autorisée." not in schema


def test_040_runbook_section_ships_with_the_code_and_claims_no_live_head() -> None:
    """040 must be applied in the same session that merges it.

    `_REQUIRED_ALEMBIC_HEAD` moves to `040` in the same commit, and that constant
    is checked fail-closed, so the plan-index repair tool refuses to run for as
    long as the repository and the database disagree.
    """
    runbook = RUNBOOK.read_text(encoding="utf-8")
    start = runbook.index("<!-- project-context-focus-updated-at-040:start -->")
    end = runbook.index("<!-- project-context-focus-updated-at-040:end -->")
    section = re.sub(r"\s+", " ", runbook[start:end])

    assert "Repository target: 040. This section claims no live head; measure it." in section
    assert section.count("upgrade production 039 then 040") == 1
    assert "_REQUIRED_ALEMBIC_HEAD" in section
    assert "24/25" in section, "the v4 attestation drop must be stated, not discovered"
    assert "must return `0`" in section, "the absent backfill must be verified, not assumed"
    assert "alembic downgrade 040:039" in section


def test_runbook_provenance_sidecars_fail_closed() -> None:
    section = _project_context_runbook_section()

    for token in (
        "brain-v42-v4-pgrestore-result.json",
        "brain-v42-v4-pgrestore-provenance.json",
        "brain-v42-v4-live-result.json",
        "brain-v42-v4-live-provenance.json",
        "mode 0600",
        "exactly 25 unique checks",
        "all statuses are pass",
        "MutationProof and the backup receipt remain unchanged",
    ):
        assert token in section


def test_runbook_mcp_reindex_cleanup_and_rollback_branches() -> None:
    section = _project_context_runbook_section()

    for token in (
        "brain-mcp-http-watchdog.timer is inactive and disabled",
        "brain-mcp-http-watchdog.service is inactive",
        "brain-mcp-http.service is inactive and MainPID=0",
        "A partial reindex is restore-only",
        "rollback-before-finalize",
        "rolled_back",
        "already_rolled_back",
        "After finalize, restore the complete tested backup",
    ):
        assert token in section
