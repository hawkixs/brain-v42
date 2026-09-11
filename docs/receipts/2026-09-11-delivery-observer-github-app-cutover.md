# Receipt: delivery observer cut over to a read-only GitHub App (2026-09-11)

Runbook: `docs/runbooks/2026-09-11-delivery-observer-github-app-cutover.md`.
Brain decision `8247fa5c` (supersedes `f0bdd928`); learning `5abe9cda`; roadmap
feature `69ee0126`. Every value below was measured on the day; re-measure rather
than copy.

## Outcome

The observer unit `brain-v42-delivery-observer.service` runs on GitHub App
`4907416` (`brain-v42-delivery-observer`), installation `160825374` on the
`hawkixs` account with repository selection `all`, and on the generated
23-key / 44-entry registry. The fine-grained PAT left the env file and the process
environment. Nothing was observed before or after the restart: no eligible binding
exists, so the confirmations table is unchanged by construction.

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

## Pending at the time of writing

- PAT revocation on GitHub by the operator; the 0600 backup is then shredded.
- The MCP copy of the registry (`delivery-mcp.env`, block 4b) and the restart of
  `brain-mcp-http`, scheduled for a moment when cutting agent sessions is acceptable.
- An end-to-end canary with a live contract bound to a pull request; until then the
  App path is proven by the pre-restart canary on the observer's own transport and
  by the successful start (the loader reads the key inside the sandbox).
- Private-key rotation: an App key never expires; Brain self-ticket dated
  2026-12-05, the day the old PAT would have expired.

## What did not change

The Factory in red-lab still delivers to GitLab only, and red-arena is not a
Factory target, so no `red-lab-factory` contract can be satisfied on the private
repositories until the Factory opens GitHub pull requests (red-lab FYI `11199e00`).
The observer's budget is unchanged: about five simultaneously active bindings
saturate it (ticket `bd1879f6`).
