# Apply migration 054 (delivery attestations) inside the merge window

Brain ticket `04bc1f4a`, resolution criterion (2): `brain_delivery_attest` and
`brain_delivery_attestation_list` on `main`, migrated in production, with the
idempotence canary passed — one `gate_passed` attestation emitted twice under
the same idempotency key lands as ONE row.

This runbook adds to the
[immutable delivery release runbook](2026-09-07-observable-delivery-workflows.md)
exactly what 054 changes. Every section it names by title is followed unchanged
unless a line below says otherwise. Read it whole before the window opens.

## Authority and stop conditions

- The apply is an operator gesture inside the merge window of the pull request
  that carries 054. The migration and its writers ship together: a release built
  from this merge refuses its own preflight until 054 is applied
  (`scripts/check_delivery_deployment.py` requires `required_schema_revision`
  `"054"` and reads `/health.alembic_head == "054"`), and the plan-index repair
  pins the same head. This is the 049 lesson made mechanical, not a fault.
- Never between 06:00 and 13:00, the Dream window. Remove any `zz-*` drop-in
  first. The pull request's nine checks must be green and merged; the shared
  `brain_test` was already upgraded to 054 on 2026-09-18.
- Production must read `053` immediately before Alembic and `054` immediately
  after. Do not downgrade: 054 is additive and empty until the canary, and its
  downgrade is fail-closed once a row exists.
- Every private value stays where it lives (`CANONICAL_ENV`, the observer env
  file, the MCP token file). Nothing below prints one.

## Inputs

- `SOURCE_SHA`: the merge commit of the pull request on `main`.
- `PREDECESSOR_SHA`: the live release, measured, never copied —
  `systemctl --user show brain-mcp-http.service -p ExecStart | grep -o 'releases/[0-9a-f]\{8\}'`
  (`e11e3660` on 2026-09-18).
- The release script of the 2026-09-15 handoff
  (`~/.local/state/brain-v42-delivery/handoff-2026-09-15/release-script.sh`,
  outside the repository). Before running it: set `PREDECESSOR_SHA` to the live
  release, change its smoke assertion `assert shipped_alembic_head() == "053"`
  and the print next to it to `"054"`, and insert the [schema step](#apply-054)
  between the quiesce and the start of the cutover.
- The deployment preflight configuration with `"required_schema_revision": "054"`
  (section "Generate the deployment preflight configuration").
- Recovery contract v11, the head-054 candidate, pinned by SHA-256:

```bash
RECOVERY_ASSETS="$RELEASE/brain-v42/ops/recovery"
test "$(sha256sum -- "$RECOVERY_ASSETS/brain-v42-v11-pgrestore.sql" | awk '{print $1}')" = f2c05ac82665aef5e103ed7224727867f449dfba9a063e301c979f16cc7dbb40
test "$(sha256sum -- "$RECOVERY_ASSETS/brain-v42-v11.json" | awk '{print $1}')" = b7022b4a981d1c9805bf6ea4259b4727ed284784320a77e8ae78c5657ad509c4
test "$(sha256sum -- "$RECOVERY_ASSETS/brain-v42-v11-acl-pgrestore.sql" | awk '{print $1}')" = 3e434e425080afa625dd6f163bc27f06d1a37602b0aa51b12df918df95e37de7
test "$(jq -r '.contract_id + ":" + (.schema_version|tostring) + ":" + ((.checks|length)|tostring)' "$RECOVERY_ASSETS/brain-v42-v11.json")" = 'brain-v42/postgresql-recovery/v11:11:30'
```

## Sequence

1. **Build the exact release** from `SOURCE_SHA`. The smoke must print
   `alembic head 054`; the wheel carries `054_delivery_attestations.py`.
2. **Private configuration**, **Render every immutable writer**, **Generate the
   deployment preflight configuration** with `required_schema_revision` `"054"`.
3. **Fresh backup and disposable recovery proof**, with one difference: the
   dump is named `pre054`, and the restored clone is proved TWICE. First replay
   v10 on the clone as restored (it is at 053: the backup is proved by the
   contract of the day). Then migrate the clone 053→054 with the release venv and
   replay v11; expect
   `assert_base_receipt "$V11_RESULT" "$V11_MANIFEST" brain-v42/postgresql-recovery/v11 11 054 44 48 160`
   and `assert_acl_receipt "$V11_ACL_RESULT" brain-v42/postgresql-recovery/v11-acl-pgrestore 11`.
   A clone that fails v11 after its migration stops the window: the migration
   would not reproduce the schema the repository attests.
4. **Quiesce all writers**.
5. **Apply 054** — the block below.
6. **Publish the reviewed systemd configuration**, **Start guarded writers in
   dormant mode** (the dormant preflight must now pass on 054), **Activate MCP
   delivery and the observer**.
7. **The idempotence canary** — below.
8. **Production receipt** — below.

## Apply 054

Identical to "Apply additive schema 053" with the revision moved forward. The
pre-check refuses a head that is not `053`; the Python block is the one of that
section with `command.upgrade(Config(os.environ["ALEMBIC_INI"]), "054")` and the
evidence files renamed `alembic-053-to-054.private.stdout` / `.stderr`, with one
correction measured on 2026-09-18: that block pins
`BRAIN_DELIVERY_REPOSITORY_REGISTRY` to the single-project literal of 2026-09-08,
and the [GitHub App cutover of 2026-09-11](2026-09-11-delivery-observer-github-app-cutover.md)
extended the registry to every ReD repository, identically in the observer env
and the MCP env. Drop the literal from `REQUIRED`, keep the key in the required
set, and require instead that the parsed registry carries
`"brain-v42": {"1337360966": "hawkixs/brain-v42"}` and equals the parsed
registry of `~/.config/brain-v42/delivery-mcp.env` (whose key set is exactly
`BRAIN_DELIVERY_ENABLED=true` and the registry). Run the attestation once WITHOUT
the upgrade before the window — the refusal is cheap there and expensive between
the quiesce and the start. The post-checks are:

```bash
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')" = 054
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM information_schema.tables \
    WHERE table_schema='public' AND table_name LIKE 'delivery_%';")" = 9
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_indexes \
    WHERE schemaname='public' AND tablename='delivery_attestations';")" = 4
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM delivery_attestations;')" = 0
```

Migration 054 is additive and starts with no attestation. If the migration or a
post-check fails, keep writers stopped and diagnose. Do not downgrade to 053.

## The idempotence canary

Subject: ticket `78fc643a-f3ca-45fe-8a0d-497283ac7d5d` (a `brain-v42` delivery
workflow with a contract), `actor_project` `brain-v42`. The client is the one
`scripts/verify_delivery_canary.py` uses: Streamable HTTP on the loopback MCP,
the bearer read raw from the private token file
`~/.config/brain-v42/delivery-canary-mcp-token-<SOURCE_SHA>` — prepared from
`MCP_HTTP_TOKEN` as the 2026-09-07 runbook's "canary verifier interface" section
prescribes, `0600`, never printed and never a command argument — and the two
headers `X-Brain-Tool-Profile: native` and `X-Brain-Agent`. There is no fixed
canary JSON: the configuration is a **per-invocation** private `0600` file with
the two keys `mcp_url` and `mcp_token_file`, written under the evidence directory
and retained beside the result. Run it from the release venv, after activation:

```bash
SOURCE_SHA=<merge commit>
CANARY_MCP_TOKEN=~/.config/brain-v42/delivery-canary-mcp-token-$SOURCE_SHA
test -f "$CANARY_MCP_TOKEN" && test ! -L "$CANARY_MCP_TOKEN"
test "$(stat -c '%u:%a' "$CANARY_MCP_TOKEN")" = "$(id -u):600"
CANARY_CONFIG="$EVIDENCE_DIR/attestation-canary.config.private.json"
test ! -e "$CANARY_CONFIG"
env CANARY_CONFIG="$CANARY_CONFIG" CANARY_MCP_TOKEN="$CANARY_MCP_TOKEN" \
  "$RELEASE/venv/bin/python" -I - <<'PY'
import json, os
from pathlib import Path

config = {"mcp_url": "http://127.0.0.1:8765/mcp", "mcp_token_file": os.environ["CANARY_MCP_TOKEN"]}
fd = os.open(Path(os.environ["CANARY_CONFIG"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(config, stream, sort_keys=True, indent=2)
PY
"$RELEASE/venv/bin/python" - "$CANARY_CONFIG" "$SOURCE_SHA" <<'PY'
import asyncio, json, sys
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport

from brain_v42.models.delivery_hashes import canonical_digest

config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
token = Path(config["mcp_token_file"]).read_text(encoding="ascii").strip()
sha = sys.argv[2]
headers = {"x-brain-tool-profile": "native", "x-brain-agent": "release-canary-054"}
payload = {"gate": "canary", "release": sha}
arguments = {
    "ticket_id": "78fc643a-f3ca-45fe-8a0d-497283ac7d5d",
    "actor_project": "brain-v42",
    "kind": "gate_passed",
    "payload": payload,
    "idempotency_key": f"canary-054:{sha}",
    "emitted_at": datetime.now(UTC).isoformat(),
}


async def main() -> None:
    transport = StreamableHttpTransport(config["mcp_url"], auth=BearerAuth(token), headers=headers)
    async with Client(transport) as client:
        first = await client.call_tool("brain_delivery_attest", arguments)
        second = await client.call_tool("brain_delivery_attest", arguments)
        assert first.structured_content["id"] == second.structured_content["id"], "two rows"
        assert first.structured_content["digest"] == canonical_digest(payload, domain="attestation")
        reused = await client.call_tool(
            "brain_delivery_attest", {**arguments, "payload": {"gate": "other"}}, raise_on_error=False
        )
        assert reused.is_error and "idempotency_key_reused" in str(reused.content)
        rejected = await client.call_tool(
            "brain_delivery_attest",
            {**arguments, "idempotency_key": f"canary-054-float:{sha}", "payload": {"ratio": 1.5}},
            raise_on_error=False,
        )
        assert rejected.is_error and "invalid_payload" in str(rejected.content)
        wide = await client.call_tool(
            "brain_delivery_attestation_list",
            {"actor_project": "brain-v42", "issuer_project": "brain-v42", "kind": "gate_passed"},
        )
        assert any(item["id"] == first.structured_content["id"] for item in wide.structured_content["items"])
        view = await client.call_tool(
            "brain_delivery_get", {"ticket_id": arguments["ticket_id"], "actor_project": "brain-v42"}
        )
        assert any(item["id"] == first.structured_content["id"] for item in view.structured_content["attestations"]["items"])
    print("canary ok", first.structured_content["id"], first.structured_content["digest"])


asyncio.run(main())
PY
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) || '|' || count(DISTINCT id) FROM delivery_attestations \
      WHERE idempotency_key = 'canary-054:$SOURCE_SHA';")" = '1|1'
```

PostgreSQL defines no `min`/`max` aggregate on `uuid`; `count(DISTINCT id)` carries the
same proof (measured on 2026-09-18, the first form errored after a green canary).

The canary proves, live, the four things the ticket and red-rail froze: a replay
lands on the same row, a reused key with other content is refused with its code,
a form violation is refused with its code and writes nothing, and the fact is
readable in both scopes with its digest. Its row stays: it is a true attestation
about this release.

## Production receipt

`docs/receipts/<date>-delivery-attestations-054.md`, published through the normal
pull-request path — the 2026-09-18 window's is
[`2026-09-18-delivery-attestations-054.md`](../receipts/2026-09-18-delivery-attestations-054.md) —
carrying: `SOURCE_SHA` and the tag; the writer and trigger
states before and after; the Alembic 053→054 timestamps; the `pre054` dump name
and SHA-256 and the two clone replays (v10 as restored, v11 after migration) with
their receipt hashes; the dormant and active preflight JSON hashes; the canary's
attestation id and digest; and the message sent to red-rail.

Then close the loop:

- `brain_ticket_transition(04bc1f4a, resolve)` citing the receipt.
- A cross-session message to red-rail with the three things it asked to pin at
  once: `SOURCE_SHA` and the tag, the contract path at that tag
  (`docs/contracts/delivery_attestations.json`), and how the reference client
  holds its bearer — a private file whose absolute path is the `mcp_token_file`
  key of its JSON configuration, read raw and trimmed, never an environment
  variable and never in a repository.

## Rollback

"Compatible forward rollback" of the 2026-09-07 runbook, keeping 054 in place:
the previous release runs unchanged on a database that merely carries one more
empty-or-canary table. Never `alembic downgrade`; once the canary row exists the
downgrade refuses without its named opt-in, and that refusal is the design.
