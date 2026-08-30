---
title: "Verifiable disaster recovery — B3 operational evidence"
status: active
summary: "Historical DR-v1 cycles authenticated and PostgreSQL head 037 restore acquired; roles/ACL, dedicated Neo4j rebuild, encrypted off-host copy, alerting, and DR-v5 activation remain open."
tags:
  - disaster-recovery
  - red-backup
  - systemd
  - operational-evidence
  - sol-ultra
---

# Verifiable disaster recovery — B3 operational evidence

> **Safety amendment — July 24, 2026.** Brain decision
> `3d3d72e4-acb7-49fe-aabb-1618e648e627` replaces the exact Neo4j restore proof with
> option A. The proof obtained at head 035 has been historical since production moved to
> 037: it no longer closed the current gate. The DR-v5 run `20260724_150315` now provides the
> isolated PostgreSQL proof at the exact deployed head. Rebuilding a dedicated, empty Neo4j
> projection with the graph protocol introduced in 035 remains a separate gate. Never
> downgrade to follow this checkpoint.

This checkpoint completes the
[DR plan](2026-07-11-disaster-recovery-verified-implementation-plan.md) without declaring DR1
deployed. It distinguishes manual runs from timer-triggered ones.

## Evidence acquired on July 12 and 13, 2026

The `red-backup.timer` timer is `enabled`, `active` and `waiting`. Its live unit enforces
`OnCalendar=*-*-* 03:00:00`, `Persistent=true` and `RandomizedDelaySec=300`. Systemd logs
two consecutive automatic triggers: July 12 at `03:02:22 CEST`, then
July 13 at `03:00:09 CEST`.

The first trigger produced the run `20260712_010222` under `/data/backups`. The log
reports `7/7 targets` in `30.9s`. The following command authenticates it without mutation:

```text
red-backup verify-run 20260712_010222
[OK] completeness=complete; 7 targets; 42 artifacts
```

The canonical receipt carries the SHA-256
`97efd0e0b33fec4bb16aba51ecdaa9cde1ced77466280f3908092256b0b51e53`. The `.complete`
marker contains exactly this SHA. The directory is mode `0700`; the receipt and the
marker are `0600`.

The second trigger produced the run `20260713_010009`. The log reports `7/7 targets`
in `32.4s`, and its independent verification yields the same inventory:

```text
red-backup verify-run 20260713_010009
[OK] completeness=complete; 7 targets; 42 artifacts
```

Its canonical receipt carries the SHA-256
`edc12c2fe42f1c9100380e176dd52e318100ba41e5654b51c7567b2ae6debd1f`. The `.complete`
marker matches this SHA. The directory is mode `0700`; the receipt and marker are
`0600`.

## State checked on July 14, 2026

The timer's automatic trigger produced the run `20260714_010021`. Its receipt ties this
run to the `red-backup-dr-v1` policy; independent verification confirms `7 targets` and
`42 artifacts`. It thus extends the DR-v1 automatic proof, but does not constitute
proof of an automatic trigger under DR-v2.

The run `20260714_072607`, launched manually under the `red-backup-dr-v2` policy, is
complete and verified with `8 targets` and `44 artifacts`. It includes the target
`red-writer-media`, absent from DR-v1. It proves the DR-v2 path and its recovery
authority, not its scheduled execution.

`red-backup`'s B3 batch is merged and pushed to `main` at commit `6b85657` from
feature commit `342d8d1`. The full suite rerun on `main` yields
`1272 passed, 4 skipped`. A live, read-only run of the watchdog, with a maximum
threshold of `25h30`, returns `fresh` for the DR-v2 run `20260714_072607`. This proof
validates the freshness calculation and the re-verification of present artifacts; it is
not a systemd trigger of the watchdog.

The secure credential-loading code and the
`red-backup-watchdog.service` / `red-backup-watchdog.timer` units are implemented and
versioned. The timer targets `04:45`, with a startup grace of `100min` and
`Persistent=false` so as not to replay a missed calendar check after the fact. The main
service declares `OnSuccess` and `OnFailure` toward the watchdog so as to trigger a check
after every backup outcome. The DR-v3 units have been installed since July 22 and the
event-driven watchdog is green; the daily watchdog timer remains disabled. No webhook is
provisioned, and so no Discord reception is proven either.

## PostgreSQL head 037 proof acquired on July 24, 2026

The `red-backup-dr-v5` authority, SHA-256
`6cb6b5e7a8805151301ab76ce94fe885cfc476bc370252848b7294767ab549e0`, keeps the Brain
contract v3 and uses a distinct attestation that only neutralizes `pg_restore`'s textual
redecomposition of array casts. The explicit run `20260724_150315` is complete: eight
targets, 47 artifacts and a green `verify-run`.

The PostgreSQL 16 drill restores Brain at head 037 and passes the 24 checks. The
independent SQL attestation, SHA-256 `d46bcdbbc1e560bb7859ddfff9883572fd4f6462cc38732520dd880d3155fd6a`,
matches exactly. The private report
`/data/backups/.drills/20260724_150315/brain-v42/5f0dc90b347b2de2b9ec4b210dafa004.json`
also attests the complete cleanup of the container and disposable volumes.

The MinIO round-trip passes on 33 objects and 52,832,376 bytes, with the inventory
`0cce7e6da277aac74190eff4dcc78f38f57ba3cb3758563eeaf5749168bbeeab`; no container, network
or temporary workspace remains. The explicit watchdog returns `fresh` on the exact v5 tuple.

The DR-v1 user cron, which had still produced the run `20260724_030001`, has been removed.
The live DR-v3 timer subsequently returned to `enabled`, `active` and `waiting`, next
occurrence `2026-07-25 03:00:03 CEST`. The units intentionally remain on DR-v3: a scheduled
cycle and activation under DR-v5 constitute a later delivery.

## Limit of the evidence

The service also succeeded at `01:52:14 CEST`, but the timer was only started at
`01:54:12 CEST`. This run was manual and does not count as an automatic cycle. The proof of
two consecutive automatic cycles rests exclusively on the runs `20260712_010222` and
`20260713_010009`; it is now acquired.

DR1 remains `building`. The isolated PostgreSQL restore at head 037 is now acquired. Still
open: post-restore reinstatement of roles, owners and ACLs, a dedicated empty Neo4j rebuild,
the encrypted off-host copy, and delivery of a Discord alert.

## Next verifications

The remaining proofs are independent of the two historical systemd cycles:

1. activate DR-v5 in a separate delivery and authenticate an automatic trigger;
2. prove the isolated reinstatement of roles, owners and ACLs, then a full rebuild in a
   dedicated, empty Neo4j database;
3. authenticate an encrypted off-host copy;
4. install then activate the daily watchdog, and trigger then receive a failed-backup
   Discord alert.
