# Observable delivery workflows — production receipt

**Status: FINAL — the production canary completed end to end.** This receipt
records measured deployment facts through 2026-09-08. Publication through the
normal protected receipt pull-request path remains separate; no receipt PR
number is asserted here.

Brain stores delivery contracts and independently observes GitHub pull-request
and check evidence. External orchestrators launch execution agents; Brain never
launches execution agents. The [design specification](../superpowers/specs/2026-09-07-observable-delivery-workflows-design.md)
and [operational runbook](../runbooks/2026-09-07-observable-delivery-workflows.md)
define that boundary and the evidence sequence.

## Release record

The first production release was
`d4fd5b880975f1f7dcc6be206fbb44203e7e9e29`, in window
`20260908T062345Z`. It advanced the schema from 052 to 053. Its dormant and
active preflights both returned `status: ok`, `source_sha: d4fd…e29`, and
`schema_revision: 053` (SHA-256
`16472aa50287faf22c421627b57a7cca70c50647ad8bed8027dee50d2b10ecd2` and
`2e2043d13885b4cbad02e796cae13cf03ddac2308106e170e8a8aa59813b140b`).

The isolated backup restoration proof passed. Its summary SHA-256 is
`e081cd06bd01d0e6148704a2f79e74915f8c75670a124511f72a957581e83a2e`;
the summary records schema revision 053, the source SHA above, and the backup,
globals, restore-image, pg_restore, and ACL receipt hashes.

The second release is
`40985fdcf61f38d11d7ea32e52661df70dfc0a44`, deployed without a second schema
migration. Its active canary preflight returned `status: ok`,
`schema_revision: 053`, and timestamp `1788856290` (SHA-256
`4dfad873222c69164211138693982e2408f48246315e9b3d3cf578400d5eda9c`). All
eight writers were re-pinned and the prior trigger state was restored.

The third release is `159cd2dd98b6ef8af3be60a27719fe3c9ac750a8`, the merge of
PR 126 at `2026-09-08T08:58:59Z`. Its nine trusted App `15368` checks succeeded.
It was deployed in window `20260908T085947Z`; its active canary preflight
returned `status: ok`, schema 053, and timestamp `1788858045` (SHA-256
`486553c9f076b7d73be2420d296ea7f59d69ccc68419e3b0904dea028dcd9d78`).
Its manifest records 296 package-payload entries with the same wheel, lock, and
interpreter hashes as the second release; no migration was run. Its source
archive hash is `1cb4a511847d9fa544dd7fc677e0e811a65229268bd771e6b8bd13e66a02827d`
and manifest SHA-256 is
`a1bf86862155d03f9425d82dd96d9cdf40654189b1cceffce0e2a65fc8035c0d`.

The fourth release is `198a7ebd0488e516ed202318bc0a8d8e1cde27dc`, built from
the reviewed PR 127 merge at `2026-09-08T09:42:20Z` (head
`d8d990878545a4ca399027e882dc91783b08ea00`), whose nine trusted App `15368`
checks passed. It was deployed in window `20260908T094310Z`; active canary
preflight returned `status: ok`, schema 053, and timestamp `1788860660`
(`2026-09-08T09:44:20Z`; SHA-256
`e687a9519a77947cffdaf0cf4155cfcfd4dfc3c84c82426d78b65a91f50afe0c`).
All eight writers were re-pinned, prior trigger state was restored, and the
observer was active and enabled. No migration ran. The bounded forward delta
changed only `brain_v42/delivery_config.py`; the other 295 package files,
dependency lock, interpreter, and migration payload were unchanged (SHA-256
`dfd5116caffa2d81546301927660c957b107d0577e8aadfb6f146f0872f32092`). The
fourth manifest SHA-256 is
`09b4d47788cbfdb97fe89cea7aaf61736addd966969dfcbc930a7acd1727d8aa`.

The first and second release manifests record the same guarded minimum revision
`fcc9328ff6e7f061879af2540c69717fc3061434`, interpreter hash
`a38cb57fabaa73c832a153d57601842818eefb2f03e122d992d09f004acff662`, wheel
hash `58a5e0ea6d5fc0db4724acd0ee504e62e47beb23d05998f3fa7a2ca97a071ff8`,
lock hash `58998fb6a49c2dc6a9b290f1b7a4518e85ee460fff07b16ddc9328de9404c9b1`,
and 296 package-payload entries. The source archives differ between releases as
expected: `6469c77ee7769496f226ed5f0e9bbfe4772ecf0dd0e2f4d7c71adde42b7dfc62`
and `4b1cbccbb6b18cbc04c1d837b360cddaf355ca6155be21747d7e698b7fa3f639`.
The manifest SHA-256 values are
`76fe89cd3dbac89b26427d44c5f5e7e068424de7a3a3f5dc38b334600dd7f51a` and
`309fdc890afc6dbd42e5ef49d373b1111fe465afcea48686a2c4b8f8ccaf148b`.

## Corrective verifier release

The initial verifier defect ran a blocking preflight inside the CLI event loop.
The preflight's synchronous schema probe invoked `asyncio.run()`, failed
closed as `running_artifact_unverified`, and produced an unawaited-coroutine
warning. The second release moves that strict preflight to a worker thread; it
does not bypass the real wrapper and it remains before MCP, GitHub, and clock
operations.

Root's accepted PR 125 verification covers
`scripts/verify_delivery_canary.py` and `tests/unit/test_delivery_canary.py`:
113 tests with zero failures, errors, or skips in
`/home/hawixs/.local/state/brain-v42-delivery/198a7ebd0488e516ed202318bc0a8d8e1cde27dc/20260908T094310Z/verification/task12-root-network.junit.xml` (SHA-256
`29ce44a81ee2e21de21a92344487dce7df0534713016d9bf01761798e23ccef6`).
The task 14 freshness correction passed 119 tests in
`/home/hawixs/.local/state/brain-v42-delivery/198a7ebd0488e516ed202318bc0a8d8e1cde27dc/20260908T094310Z/verification/task14-root-full.junit.xml` (SHA-256
`3c08e9bd4fd2b1ca84e80309dba7d597b9ed3fe8db97d21974eb9a4cf3bcec50`). The
task 15 API compatibility correction passed 280 tests with two optional private
documentation-fixture skips in
`/home/hawixs/.local/state/brain-v42-delivery/198a7ebd0488e516ed202318bc0a8d8e1cde27dc/20260908T094310Z/verification/task15-root.junit.xml`
(SHA-256 `7b84541a6d64440af93fab9c6b00d3fd76c8ebe355b6b35e39f732be02b4565c`).
An author's timed-out canary log was withdrawn and is not evidence for this
receipt.

## Canary state

Ticket `426eaf70-a050-4ae7-b4c7-61bdd10ebfdb` has contract revision 1, attempt
1, and deliverable `canary-docs` for repository `1337360966`. The deployed
second-release verifier completed `missing-proof` at
`2026-09-08T08:32:46.551798+00:00`: `status: ok`,
`outcome: expected_missing_proof`, source `40985f…0a44`, schema 053, fresh
assessment `82d554bb5d3e8aa2195c447f9b740e2e56b18a352a600bf95c00a2e378d3fcd0`
at version 102. The proof JSON SHA-256 is
`e66d6d7e2e76bc02f2db0cc9bbde2df558fcb875902144423915dffcc74998ec`.

At `2026-09-08T08:34:35Z`, the ticket was bound to PR 124, H1
`1faca155c4da62cb7c056e259d843d518635e7f3`, revision 1, attempt 1. The
deployed verifier then completed H1 `observed` at
`2026-09-08T08:36:13.148071+00:00`. It recorded nine check records, an open PR,
delivery digest
`d333b88b7717d137b4bd53adaa8fc446f967e24ad5d14ecef436027d39c1a099`,
and `requirements_satisfied: false`. Its JSON SHA-256 is
`2297f3c466a4f2093daed9b5e2f1a7c3f2e8ed26acd2fae5f5f0b01be191753a`.

The deployed H1 `verified` invocation then refused at `2026-09-08T08:37:00`.
The defect was a stale deduplicated snapshot `collected_at` being compared with
a newer, valid confirmation start. Its candidate H1 `verified` result at
`2026-09-08T08:44:12.597390+00:00` has nine required checks, but qualifies the
candidate only; it is not deployed verification evidence. The candidate JSON
SHA-256 is
`a6aa54edfd07da224fd3f9a6b9d807739fbaaac07b44862b15a7f939bfbf7695`.

H2 commit `90682da4fc6522d30202fa6007242431fccd86d6` was created at
`2026-09-08T08:45:23Z` and pushed successfully at `08:45Z`. The first read-only
H2 view at `2026-09-08T08:48:09Z` recorded a fresh health state, H2, delivery
digest `229aaa3bfd673854107f4e9ba775b068a1ff76406059ea0a3e171e9d4ab3c241`,
and pending CI. Its nine H2 checks later succeeded. The actual pre-merge legacy
guard refused at `2026-09-08T08:57:05.101927Z` with
`delivery_requirements_unsatisfied`, while the assessment was `verified` and
the only blocker was `pr_not_merged`; the ticket read at
`2026-09-08T08:57:05.639033Z` remained open and extraction-skipped. The guard
and ticket-recheck receipt SHA-256 values are
`d5d1e103e2fdf9744c4e3f11743efba76e7891f20a10efd88e56cf5e8bbe56b5` and
`cf6fcbaa8a6a522b86f7b33e24c67c6a86f2b01044e2edf038f92b254b91f6f0`.

The deployed third-release verifier completed H2 `observed` at
`2026-09-08T09:02:22.513489+00:00` and H2 `verified` at
`2026-09-08T09:02:28.773027+00:00`. Both retained source
`159cd2dd98b6ef8af3be60a27719fe3c9ac750a8`, base
`d4fd5b880975f1f7dcc6be206fbb44203e7e9e29`, head
`90682da4fc6522d30202fa6007242431fccd86d6`, the H2 digest above, assessment
`9fe9a31760c69038a6eb65cf997a8251e9fba14fa78ee5b9811e3a49e15b0ea2` version
157, binding `616deea5-520b-4f92-9a46-d52b1ed6a2cf` version 27, confirmation
`15aece39-acc9-4759-a9d8-76267f592ff2`, and snapshot
`3fc04206-731e-4d9c-9b68-fb9f1f92a4d6`. Collection ran from
`2026-09-08T09:01:43.975141+00:00` through
`2026-09-08T09:01:45.663044+00:00`. The observed and verified JSON SHA-256
values are `287775bce66e50e4ea46f4c67f93a3f86637d9e3b10729c8b0efe4de19cd3f15`
and `8310ecd424e8311e239c32d1c62f4fdadcb2ae4a7babae5f3c2321b368f202de`.

The verified receipt retains these nine trusted App `15368` selectors:

| Selector | Check ID | URL |
| --- | ---: | --- |
| lint-ruff | 101996267457 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267457 |
| lint-mypy | 101996267294 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267294 |
| test-unit | 101996267251 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267251 |
| test-integration | 101996267373 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267373 |
| test-coverage | 101996267247 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267247 |
| security-bandit | 101996267061 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267061 |
| security-gitleaks | 101996267344 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267344 |
| security-pip-audit | 101996267374 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267374 |
| security-pip-audit-embedding-supervisor | 101996267272 | https://api.github.com/repos/hawkixs/brain-v42/check-runs/101996267272 |

PR 124 was merged normally through the protected path at
`2026-09-08T09:03:26Z`, retaining H2 and integration SHA
`d6078f9a4239a4aea112f6da9ee0805d85a30fe1`. The merge evidence was obtained
from `gh pr view` after a successful match-head merge; its JSON SHA-256 is
`39794bd69158caebf23bcfbda5a190d37246eb8b314a18e3b56c79ba92993365`.

The deployed third-release `integrated` verifier failed closed at
`2026-09-08T09:06:03.786122+00:00` with `current_generation_unverified`
(JSON SHA-256
`ac3298ef67c6dad86344b8cb9e6453d0bcf53a6e77f02bcdd4f4f9b40df651ec`). The
provider response was incompatible with the `2026-03-10` GitHub REST API
version because that version removes the pull-request `merge_commit_sha` field.
GitHub publishes breaking changes by API version and supports the prior version
for at least 24 months after a new version is released; see [GitHub's breaking
changes documentation](https://docs.github.com/en/rest/about-the-rest-api/breaking-changes)
and [API version documentation](https://docs.github.com/en/rest/about-the-rest-api/api-versions).

A read-only compatibility probe using API version `2022-11-28` returned merged
H2, integration SHA `d6078f9a4239a4aea112f6da9ee0805d85a30fe1`, and nine
checks at `2026-09-08T09:18:28.678581+00:00` without a Brain mutation. It is
diagnostic evidence, not an autonomous observer proof (SHA-256
`daa47924675c4add74d70a190b946d18fd2cd0c490f5f47e89ab9df0e1215863`). The
operational API pin was recorded at `2026-09-08T09:22:08.867890+00:00`: only
`BRAIN_DELIVERY_GITHUB_API_VERSION` changed, from `2026-03-10` to `2022-11-28`,
with no credential change (SHA-256
`930920d1840c91af211f5c0d696901355ce723522c428885b0e3359556e424eb`).

After the API pin, the deployed third-release `integrated` verifier returned
`status: ok` at `2026-09-08T09:28:55.333705+00:00`. It retained integrated
delivery digest
`168e46b0f32e2797c3ced97a297bae4907dfcd75bbf43ed0961cf41f7a89e80e`,
assessment `8a4adf531f18bd30e85cbe361c31786161384aba96054e7903e18e0b2459e1bc`
version 196, current confirmation `bbe190fc-6b6f-4a3e-b258-88bc1a6eb656`,
and snapshot `adf207cc-369b-4d42-9983-b539fb936ea8`. The autonomous integration
receipt is `71bccdf5-484e-4c3f-aaa1-3b4bd775e033`, decided at
`2026-09-08T09:24:13.421092+00:00` from confirmation
`500db4f1-b2a7-4217-aa70-87108dd0bba4` and the same snapshot. The integrated
verifier JSON SHA-256 is
`9de4d9f178d8f91fe813db66e766c2a7e4ac7fa68166d00083477a08fcde273f`.

Explicit acceptance was submitted through the public MCP surface at
`2026-09-08T09:30:18.957515+00:00` (metadata JSON SHA-256
`8f5f2bdef4218ca554b229704113f8fcbe7119b73d77e2ac07ba7cb4776d5432`). The
deployed `accepted` verifier returned `status: ok` at
`2026-09-08T09:30:24.165606+00:00`, with fulfillment receipt
`c2aea0ac-e3fd-4522-867a-81d75f290248`, decision time
`2026-09-08T09:30:18.888296+00:00`, confirmation
`0926ce3e-8ae4-4404-a0ab-4dfadb95304e`, and the same snapshot. The accepted
verifier JSON SHA-256 is
`316158fba59a4d3f21354c5b69373c108f263caf0c9b46ef9996d0c39bd23ace`.

The installed third-release shared briefing loader passed at
`2026-09-08T09:34:18.606250+00:00` in a read-only transaction. It matched the
ticket, delivery digest, integration receipt, and fulfillment receipt recorded
above; it did not write session lifecycle or focus state. The briefing proof
SHA-256 is `9826e47a752a500039743351c189f5f60fe82ab9b9ff00a55bef095e7bea6d91`.
An earlier operator probe failed before connection because it used unsupported
structlog level 51; its empty output is not evidence. The corrected level 50
probe was rerun from the fourth-release source. Its final accepted verifier
passed at `2026-09-08T09:45:54.350232+00:00`, retaining source
`198a7ebd0488e516ed202318bc0a8d8e1cde27dc`, the two immutable receipt IDs
above, and confirmation `b590c32a-fc98-4903-9710-e4d2248bc127` (SHA-256
`710c4be32e6d627c54ddb4ff840438bc7a10371c842eb03571987814bed44be5`).

The fourth-release shared read-only briefing passed at
`2026-09-08T09:47:08.286973+00:00`, matching the ticket, digest, and both
immutable receipt IDs without a session lifecycle or focus write (SHA-256
`be284d387c80577a85b6dc3d459a77c2b9ce4544fab3381d1818c1093c6b7cd7`). A
later autonomous confirmation `a8ab032f-8799-47b5-9f0b-c9faf1144843` was
collected from `2026-09-08T09:47:22.473155+00:00` through
`2026-09-08T09:47:23.723713+00:00`. The accepted verifier and receipt-stability
proof passed at `2026-09-08T09:48:20.406746+00:00` and
`2026-09-08T09:48:20.520830+00:00`; both public receipts remained unchanged
(SHA-256 `51477f33306f643eb1110035820c4d2b6f349a73334f268535ec993c92cc741d`
and `4da880f1878e3d47a6c4c73c4c06114a6e1f3866f90ac8cbc1961e239a748003`).

The authorized legacy closure was submitted through public MCP at
`2026-09-08T09:49:40.874558+00:00`. The subsequent ticket read at
`2026-09-08T09:49:41.396124+00:00` recorded `closed` and extraction `skipped`.
The delivery read at `2026-09-08T09:49:42.153631+00:00` retained accepted state,
the same digest and receipt IDs, `contract_fulfilled: true`, and
`completion_eligible_now: false` because the ticket was closed. The persisted
receipt proof passed at `2026-09-08T09:50:44.518122+00:00` (SHA-256
`e8b1637bd7b5257e0d44d023d4f1547696d03e4b3f0b2f0acbab249d09264e26`).

The earlier candidate verifier result at `2026-09-08T07:09:02.508155+00:00`
only qualified the corrected candidate against the first release. It is not
evidence that the corrected verifier was deployed. Its JSON SHA-256 is
`d7047b50d25ba426724d01da7286d4dd9123e15cb11587eef140a8546f90de53`.

## Limits

Delivery persistence uses `READ COMMITTED`; these observations and retained
receipts do not prove the absence of unmeasured writers or create an exclusivity
claim. Caller identity is established at the MCP `X-Brain-Agent` boundary, not
by a caller-supplied request field. This receipt records the measured canary
acceptance and closure; it does not assert creation of a durable Brain feature
record, which follows the public receipt publication.
