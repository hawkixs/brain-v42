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
   row, and only that row, by an exact named exception (decision a00bdf68,
   Q136): its knowledge_id, session_id, type and capture instant, while the
   knowledge is absent from every knowledge table and ``brain_entities``
   tombstones it as ``deleted``. Three general rules were tried and each was
   fail-open on some history the tombstone does not keep; the server now
   refuses deleting captured knowledge, so the list is closed.

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

#: 2a. `artifact_source_matches` also carries the artifact's session, declared
#: type and capture instant: the exception below matches on all four. The
#: `knowledge_id` is the ledger's primary key, so grouping by them adds no row.
MATCHES_SELECT = (
    "artifact_source_matches AS (\n SELECT\n     artifact_record.knowledge_id,\n",
    "artifact_source_matches AS (\n"
    " SELECT\n"
    "     artifact_record.session_id,\n"
    "     artifact_record.knowledge_id,\n"
    "     artifact_record.knowledge_type,\n"
    "     artifact_record.captured_at,\n",
)
MATCHES_GROUP = (
    " GROUP BY artifact_record.session_id, artifact_record.knowledge_id\n),\n",
    " GROUP BY\n"
    "     artifact_record.session_id,\n"
    "     artifact_record.knowledge_id,\n"
    "     artifact_record.knowledge_type,\n"
    "     artifact_record.captured_at\n"
    "),\n",
)

#: 2b. The exception list (decision a00bdf68, Q136): every tolerated artifact is
#: named by its exact ledger row, and is tolerated only while its knowledge is
#: absent from every knowledge table and `brain_entities` tombstones it as
#: `deleted` with the same type. A general rule cannot prove that the knowledge
#: existed at the capture (the tombstone keeps only its last state); the
#: server refuses deleting captured knowledge since Q132, so the list is closed.
#: Adding an entry is a new contract generation.
MISMATCH_FILTER = (
    "artifact_source_mismatches AS (\n"
    " SELECT count(*) AS value\n"
    " FROM artifact_source_matches\n"
    " WHERE source_matches <> 1 OR typed_matches <> 1\n"
    "),\n",
    "tolerated_orphan_artifacts(knowledge_id, session_id, knowledge_type, captured_at) AS (\n"
    " VALUES\n"
    "     (\n"
    "         'a301034b-079a-4961-b613-5256a017a519'::uuid,\n"
    "         '2923d3c2-ef29-417d-ad52-a31d9c08bfe8'::uuid,\n"
    "         'decision'::text,\n"
    "         '2026-09-26 20:21:39.223113+00'::timestamptz\n"
    "     )\n"
    "),\n"
    "artifact_source_mismatches AS (\n"
    " SELECT count(*) AS value\n"
    " FROM artifact_source_matches AS match_record\n"
    " WHERE (match_record.source_matches <> 1 OR match_record.typed_matches <> 1)\n"
    "   AND NOT EXISTS (\n"
    "       SELECT 1\n"
    "       FROM tolerated_orphan_artifacts AS tolerated\n"
    "       WHERE tolerated.knowledge_id = match_record.knowledge_id\n"
    "         AND tolerated.session_id = match_record.session_id\n"
    "         AND tolerated.knowledge_type = match_record.knowledge_type\n"
    "         AND tolerated.captured_at = match_record.captured_at\n"
    "         AND NOT EXISTS (\n"
    "             SELECT 1\n"
    "             FROM knowledge_sources AS any_source\n"
    "             WHERE any_source.knowledge_id = match_record.knowledge_id\n"
    "         )\n"
    "         AND EXISTS (\n"
    "             SELECT 1\n"
    "             FROM public.brain_entities AS entity_record\n"
    "             WHERE entity_record.source_uuid = match_record.knowledge_id\n"
    "               AND entity_record.entity_type = tolerated.knowledge_type\n"
    "               AND entity_record.lifecycle = 'deleted'\n"
    "         )\n"
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
