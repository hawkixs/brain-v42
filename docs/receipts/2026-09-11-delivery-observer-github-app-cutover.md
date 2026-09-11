# Receipt: delivery observer cut over to a read-only GitHub App (2026-09-11)

Runbook: `docs/runbooks/2026-09-11-delivery-observer-github-app-cutover.md`.
Brain decision `8247fa5c` (supersedes `f0bdd928`); learning `5abe9cda`; roadmap
feature `69ee0126`. Every value below was measured on the day; re-measure rather
than copy.

## Outcome

The observer unit `brain-v42-delivery-observer.service` runs on GitHub App
`4907416` (`brain-v42-delivery-observer`), installation `160825374` on the
`hawkixs` account with repository selection `all`, and on the generated
23-key / 44-entry registry. The fine-grained PAT left the env file, the process environment and GitHub. A
same-day canary on a private repository was observed by the live process and
received an integration receipt (see Completion).

| Step | Measured |
|---|---|
| App record (JWT, `GET /app`) | `200`, slug `brain-v42-delivery-observer`, permissions exactly `checks/contents/metadata/pull_requests/statuses = read`, `events = []` |
| Installations (`GET /app/installations`) | one: `160825374`, account `hawkixs`, `repository_selection = all`, same five permissions |
| Private key | `~/.config/brain-v42/delivery-observer-app.pem`, `hawixs 0600`, 1679 bytes, `openssl rsa -check` ok, accepted by `read_private_file`, RS256 signing ok; public-key fingerprint `SHA256:cRfyEKZ8Zk/Qvic3m+SKgrcQzHkyCE7cyfaALjXM/Xg=`; downloaded on the dev PC, pulled over SSH, source copy deleted |
| Registry (block 0) | 22 owned non-archived repositories matched 1:1 to a Brain project key; `red-lab-factory` → all 22; 23 keys, 44 entries; unmatched `brain-v42-archive`, `data_analyse`, `data_app`, `test_unyc` |
| Candidate env (block 2) | 12 → 14 keys, 0 `BRAIN_DELIVERY_GITHUB_TOKEN`, registry 44 entries, accepted by `load_observer_settings`, every live registry entry preserved |
| Canary before restart (block 3) | installation token obtained through `GitHubAuthProvider`; `/installation/repositories` lists 26, registry needs 22, missing `[]`; `GET /repos/{r}/commits/main/check-runs` answers `200` with `x-accepted-github-permissions: checks=read` on all 22 repositories, 21 of them private |
| Switch (block 4) | backup `delivery-observer.env.bak-20260911-103304` (0600); env file `hawixs 0600`, 14 keys; restart at 10:33:05 CEST; `ActiveState=active`, `SubState=running`, `NRestarts=0`, `MainPID=2080231` |
| Process environment | `BRAIN_DELIVERY_GITHUB_TOKEN` 0; `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_PRIVATE_KEY_PATH` 1 each; registry 44 entries |
| Journal | stop `{"exit_code": 0, "stopped": true}`, then `Started`; no error line |
| `delivery_confirmations` | 277 `success`, 9 `provider_invalid_response`, 1 `provider_not_found`, 0 `provider_forbidden`, before and after |

## Completion (same day)

| Step | Measured |
|---|---|
| PAT revocation | done by the operator on GitHub; the 0600 env backup holding it was shredded |
| MCP registry (block 4b) | `delivery-mcp.env` rewritten with the 44-entry registry, `brain-mcp-http` restarted at 10:39:45 CEST, `ActiveState=active`, `NRestarts=0`, unauthenticated `POST /mcp` → `401`, process environment carries 44 registry entries and `BRAIN_DELIVERY_ENABLED=true`; journal lines matching "error" were two ASGI stream closures at shutdown, one embedding retry and the probe's own `invalid_token` |
| End-to-end canary, private repository | self-ticket `fb7f7a38` (red-arena → red-arena, extraction skipped); contract revision 1 on `hawkixs/red-arena` (`1324957236`), target `main`, required check-runs `lint` and `test` from `github-actions`, explicit acceptance; binding `9b792f2c` to PR 4 (merged 2026-09-10) |
| Observation by the live process | confirmation `c35e900a`, `outcome=success`, collected 08:42:05.74 → 08:42:07.53 UTC, 66 s after binding; evidence: state `merged`, head `9915e5ed…`, base `22142dc6…`, integration `d2e6a887…`, author `hawkixs`, two check-runs `success` (`103087930980` lint, `103087931202` test, provider `15368`), `complete=true` |
| Assessment | `delivery_stage=integrated`, `observation_health=fresh`, `requirements_satisfied=true`, `blockers=[]`, delivery digest `805bc45a…`; integration receipt `fb37c460` issued by `brain-v42-delivery-observer` (`issuer_kind=observer`) |
| `delivery_confirmations` after | 278 `success` (was 277), 9 `provider_invalid_response`, 1 `provider_not_found`, 0 `provider_forbidden` |
| Closure | explicit acceptance by the requester → fulfillment receipt `0cb9106a` (`acceptance_basis=explicit`, issuer kind `requester`); `brain_ticket_transition(action=resolve)` accepted and the self-ticket went straight to `closed` |

This is the first observation of a private repository's check-runs by this
observer. The MCP side accepted a contract for a repository other than brain-v42
for the first time as well, which is the proof that its registry copy is live.

## Still open

- Private-key rotation: an App key never expires; Brain self-ticket `4bf3eabd` dated
  2026-12-05, the day the old PAT would have expired.
- Observer capacity (ticket `bd1879f6`): about five simultaneously active bindings
  saturate the fixed 40 requests/minute budget.

## What did not change

The Factory in red-lab still delivers to GitLab only, and red-arena is not a
Factory target, so no `red-lab-factory` contract can be satisfied on the private
repositories until the Factory opens GitHub pull requests (red-lab FYI `11199e00`).
The observer's budget is unchanged: about five simultaneously active bindings
saturate it (ticket `bd1879f6`).
