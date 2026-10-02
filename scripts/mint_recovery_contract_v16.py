"""Mint the v16 DR contract as a surgical TEXT delta of v15, still for head 058.

v16 moves no fingerprint: the schema is v15's (head 058). It fixes two defects
red-backup's DR-v7 drill found in the attestation itself (operator decisions
7d2f7fe8 / Q131 = a and ee26407b / Q132 = c):

1. The final ``jsonb_agg(... ORDER BY id)`` carried no ``COLLATE "C"``. On a
   server whose default collation is a locale (production runs ``en_US.utf8``),
   the checks came back out of byte order, and red-backup, which pairs checks by
   position, rejected the attestation. Every other ordering of the asset already
   says ``COLLATE "C"``.

2. ``artifact_project_mismatches`` counted an append-only
   ``brain_session_artifacts`` row whose knowledge was deleted after capture
   (decision ``a301034b``, red-arena, deleted 2026-09-30). v16 tolerates that
   case, and only that case: the knowledge row is absent from EVERY knowledge
   table, and ``brain_entities`` carries its entity, of the matching knowledge
   type, in the artifact's session project, with ``lifecycle = 'deleted'`` and a
   ``deleted_at`` no earlier than the capture: knowledge deleted BEFORE it was
   captured was never there to capture. An absent row
   without that tombstone, a tombstone of another type or project, or a row that
   still exists elsewhere stays a mismatch.

Both variants (base and ``-pgrestore`` twin) receive the identical edits: the
regions touched carry no fingerprint, so no measurement is needed and none is
taken. The proof that the written assets describe a 058 database lives in
``tests/integration/db/test_recovery_contract_v16.py``, on disposable databases
only.

Never regenerates. Every replacement is anchored and must match exactly once,
and the unit test diffs v15 and v16 LINE BY LINE against an explicit list.

Usage:
    python scripts/mint_recovery_contract_v16.py [src.sql dst.sql]...
    python scripts/mint_recovery_contract_v16.py --manifest [src.json] [dst.json]

Without arguments the SQL mode mints both variants from the v15 assets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RECOVERY = Path("ops/recovery")

#: 1. The final aggregate's ordering, byte order whatever the server collation.
FINAL_ORDER = ("     ORDER BY id\n )", '     ORDER BY id COLLATE "C"\n )')

#: 2a. `artifact_source_matches` also carries the artifact's declared type and
#: its session's project: the tolerance below needs both. `knowledge_id` is the
#: ledger's primary key, so grouping by them adds no row.
MATCHES_SELECT = (
    "artifact_source_matches AS (\n SELECT\n     artifact_record.knowledge_id,\n",
    "artifact_source_matches AS (\n"
    " SELECT\n"
    "     artifact_record.knowledge_id,\n"
    "     artifact_record.knowledge_type,\n"
    "     artifact_record.captured_at,\n"
    "     session_record.project_key,\n",
)
MATCHES_GROUP = (
    " GROUP BY artifact_record.session_id, artifact_record.knowledge_id\n),\n",
    " GROUP BY\n"
    "     artifact_record.session_id,\n"
    "     artifact_record.knowledge_id,\n"
    "     artifact_record.knowledge_type,\n"
    "     artifact_record.captured_at,\n"
    "     session_record.project_key\n"
    "),\n",
)

#: 2b. The narrow tolerance. `legacy` artifacts predate typed capture: their
#: tombstone may be any knowledge entity type, never a project or a domain.
MISMATCH_FILTER = (
    "artifact_source_mismatches AS (\n"
    " SELECT count(*) AS value\n"
    " FROM artifact_source_matches\n"
    " WHERE source_matches <> 1 OR typed_matches <> 1\n"
    "),\n",
    "artifact_source_mismatches AS (\n"
    " SELECT count(*) AS value\n"
    " FROM artifact_source_matches AS match_record\n"
    " WHERE (match_record.source_matches <> 1 OR match_record.typed_matches <> 1)\n"
    "   AND NOT (\n"
    "       NOT EXISTS (\n"
    "           SELECT 1\n"
    "           FROM knowledge_sources AS any_source\n"
    "           WHERE any_source.knowledge_id = match_record.knowledge_id\n"
    "       )\n"
    "       AND EXISTS (\n"
    "           SELECT 1\n"
    "           FROM public.brain_entities AS entity_record\n"
    "           WHERE entity_record.source_uuid = match_record.knowledge_id\n"
    "             AND entity_record.lifecycle = 'deleted'\n"
    "             AND entity_record.deleted_at >= match_record.captured_at\n"
    "             AND entity_record.project_key = match_record.project_key\n"
    "             AND (\n"
    "                 (\n"
    "                     match_record.knowledge_type IN (\n"
    "                         'decision', 'learning', 'snippet', 'runbook', 'adr'\n"
    "                     )\n"
    "                     AND entity_record.entity_type = match_record.knowledge_type\n"
    "                 )\n"
    "                 OR (\n"
    "                     match_record.knowledge_type = 'indexed_plan'\n"
    "                     AND entity_record.entity_type = 'plan'\n"
    "                 )\n"
    "                 OR (\n"
    "                     match_record.knowledge_type = 'legacy'\n"
    "                     AND entity_record.entity_type IN (\n"
    "                         'decision', 'learning', 'snippet', 'runbook', 'adr', 'plan'\n"
    "                     )\n"
    "                 )\n"
    "             )\n"
    "       )\n"
    "   )\n"
    "),\n",
)

IDENTITY = [
    (
        " 'contract_id', 'brain-v42/postgresql-recovery/v15',",
        " 'contract_id', 'brain-v42/postgresql-recovery/v16',",
    ),
    (" 'schema_version', 15", " 'schema_version', 16"),
]

REPLACEMENTS = [FINAL_ORDER, MATCHES_SELECT, MATCHES_GROUP, MISMATCH_FILTER, *IDENTITY]


def mint_sql(text: str) -> str:
    """Apply every anchored replacement; refuse an anchor that is not unique."""
    for before, after in REPLACEMENTS:
        count = text.count(before)
        if count != 1:
            raise SystemExit(f"anchor not unique ({count}x): {before[:60]!r}")
        text = text.replace(before, after)
    return text


def mint_manifest(src: Path, dst: Path) -> None:
    """The JSON contract moves the two identity values, nothing else."""
    document = json.loads(src.read_text(encoding="utf-8"))
    if document["contract_id"] != "brain-v42/postgresql-recovery/v15":
        raise SystemExit(f"not the v15 manifest: {document['contract_id']}")
    document["contract_id"] = "brain-v42/postgresql-recovery/v16"
    document["schema_version"] = 16
    dst.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote : {dst}", flush=True)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--manifest":
        mint_manifest(
            Path(argv[1]) if len(argv) > 1 else RECOVERY / "brain-v42-v15.json",
            Path(argv[2]) if len(argv) > 2 else RECOVERY / "brain-v42-v16.json",
        )
        return 0
    pairs = (
        list(zip(argv[0::2], argv[1::2], strict=True))
        if argv
        else [
            (RECOVERY / "brain-v42-v15.sql", RECOVERY / "brain-v42-v16.sql"),
            (
                RECOVERY / "brain-v42-v15-pgrestore.sql",
                RECOVERY / "brain-v42-v16-pgrestore.sql",
            ),
        ]
    )
    for src, dst in pairs:
        Path(dst).write_text(mint_sql(Path(src).read_text(encoding="utf-8")), encoding="utf-8")
        print(f"wrote : {dst}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
