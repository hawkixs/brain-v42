# Delivery observer: cut over from the fine-grained PAT to one read-only GitHub App

Decision `f0bdd928` (Brain, 2026-09-11). Follow-up tickets `c475c92d`, `7259bdb2`,
`ebfd8a8c`, `e31f9ad6`.

## Why

The observer reads seven GitHub endpoints, all under `/repos/`, all `GET`. Six of
them work with the fine-grained PAT that runs today. One does not on a private
repository: `GET /repos/{o}/{r}/commits/{sha}/check-runs`, which needs
`checks:read`. The fine-grained PAT permission reference has **no Checks
section** (verified 2026-09-11 on
<https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens>),
so no fine-grained PAT can ever hold that permission. Observable delivery
therefore works today only because `hawkixs/brain-v42` is the one public
repository of the 25 on the `hawkixs` user account.

The fix is **one** GitHub App, registered once, installed once on the account.
The operator's direction (2026-09-11) is that every project must be able to carry
observable delivery tickets, and that the Factory should eventually deliver to all
of them, so the installation covers **all repositories** and the registry is
generated for every project rather than written by hand (Brain decision
`8247fa5c`, superseding `f0bdd928`). It is not one App per repository: the observer holds a single process-wide credential
(`DeliverySettings.github_app_id`, `github_installation_id`,
`github_private_key_path` are three scalars in `src/brain_v42/delivery_config.py`;
`GitHubAuthProvider.authorization_headers()` takes no repository argument and
interpolates one installation id). Adding a repository later costs one
registry regeneration and two restarts, no GitHub gesture. No code changes
in this cutover: the App path is written, wired and unit-tested; enabling it is a
configuration change.

Options rejected, with the reason: "Only select repositories" (the earlier choice,
right when the goal was three repositories out of 25, wrong once the goal is all of
them: 23 checkboxes plus one per future repository, for no security gain since the
App never holds a write permission); classic PAT with `repo` scope (read **and write** on every repository,
which turns "the observer cannot fabricate the proof it reads" from a structural
property into a policy); dropping check-runs from the evidence model (proof
collapses to "PR merged"); a CI push model (the judged PR could rewrite its own
judge); the operator's `gh` OAuth token (a human credential, forbidden by the
design spec).

## Scope gate: what the App can and cannot observe today (measured 2026-09-11)

The App fixes the credential. It does not, by itself, make Factory deliveries on
the two private repositories observable, because nothing delivers there on GitHub.

- **red-lab's Factory delivers to GitLab only.** `open_merge_request` in
  `src/lab/apps/devops/merge_poller.py` calls `{gitlab_url}/api/v4/projects/{pid}/merge_requests`
  with a `PRIVATE-TOKEN` header; `TargetRepo` in `src/lab/apps/devops/dispatch.py` has
  `path, gitlab, branch, …` and no forge or GitHub field; `config/lab.yaml` and the
  deployed `/etc/red-lab/lab.yaml` list a single `voie_b_projects` entry,
  `auto-discord → hawkixs_project/auto_discord`. Four Factory merge requests exist on
  GitLab for auto_discord (last one 2026-07-22). GitHub holds one pull request ever
  for `hawkixs/auto-discord`, human, merged 2026-07-19, and three of the six
  `factory/*` branches as a partial mirror. No Factory pull request, ever.
- **red-arena is not a Factory target at all.** It is absent from `voie_b_projects`,
  so the intake rejects its tickets before execution
  (`src/lab/apps/devops/intake/contract.py`, "ticket source is outside the execution
  allowlist"). Its four GitHub pull requests are human work by `hawkixs`; GitLab has
  never seen a merge request for it.
- **The live registry is therefore miswired**, for two different reasons. A
  `red-lab-factory` contract on `hawkixs/auto-discord` can never be satisfied:
  `brain_ticket_transition(action='resolve')` would refuse forever with
  `delivery_requirements_unsatisfied`, and that reads like an observer outage when
  it is a wiring error. A contract on `hawkixs/red-arena` would attribute human pull
  requests to the Factory: a false observation, not a missing one.
- Neither repository enforces anything on `main`: auto-discord has no branch
  protection (`404 Branch not protected`, no rulesets); red-arena has a classic
  protection with no required checks and no required reviews. Check-run names
  diverge: `Ruff`, `Pytest`, `Docker build` (skipped on pull requests) on
  auto-discord; `lint`, `test` on red-arena. A single contract for the project key
  cannot cover both.
- Whether the Factory still runs is unverified: no `red-lab` user unit is listed and
  the last GitLab delivery is seven weeks old.

The operator's direction, stated the same day, is that every project should be able
to carry observable delivery tickets and that the Factory should eventually deliver
to all of them; a red-lab session owns the Factory side. The registry is therefore
generated for every project (block 0) and the App is installed on all repositories,
so brain-v42 is ready to observe any repository the day a GitHub pull request lands
there.

What the cutover still buys, and why it proceeds: the observer's only working
surface today is `hawkixs/brain-v42`, on a PAT that expires 2026-12-05 and that
carries the human's identity. The App removes the expiry cliff, names the machine,
and makes the two private repositories readable the day something delivers there
on GitHub. Listing a repository observes nothing and costs nothing;
only a contract bound to a pull request creates load. Brain FYI ticket `11199e00`
to red-lab records the forge question and its three answers.

## Measured state before the cutover (2026-09-11)

Re-measure, never copy forward.

| Item | Measured |
|---|---|
| Live unit | `brain-v42-delivery-observer.service`, active, `NRestarts=0`, release `079dc10b`, started 2026-09-10 23:15 CEST |
| Sandbox | `PrivateUsers=yes`, `ProtectHome=read-only`, `ProtectSystem=strict`, `NoNewPrivileges=yes` |
| Observer env file | 12 keys, mode 0600, owner `hawixs`; exactly one `BRAIN_DELIVERY_GITHUB_TOKEN` in `/proc/<MainPID>/environ` |
| Live registry (both env files, identical) | `brain-v42` → `1337360966 hawkixs/brain-v42`; `red-lab-factory` → `1305619025 hawkixs/auto-discord`, `1324957236 hawkixs/red-arena` |
| Generated registry (block 0) | 22 owned, non-archived repositories whose name equals a Brain project key, 1:1, no ambiguity; `red-lab-factory` → all 22; 23 keys, 44 entries, 1811 bytes; unmatched: `brain-v42-archive`, `data_analyse`, `data_app`, `test_unyc` |
| Registry consumers | observer `_deliverable`/`_address`; MCP `delivery_service.py:185` (contract_set) and `:298` (bind_pr), both keyed by the ticket's `to_project`; MCP reads its copy from `delivery-mcp.env` at start (`server.py:651`) |
| Both private repos | `private=true`, owner type `User`, default branch `main` |
| Live PAT on the private repos | `404` (repository not selected in the token), not `403` |
| `delivery_confirmations` | 287 rows: 277 `success`, 9 `provider_invalid_response`, 1 `provider_not_found`, **0 `provider_forbidden`**; last row 2026-09-08 18:15 UTC |
| Eligible bindings | 3 bindings, all on `1337360966`, all on closed tickets: nothing is being observed right now |
| Latest check-run names | auto-discord `Docker build`; red-arena `test`; brain-v42 `build-docker` (all from the `github-actions` App, id `15368`) |

Dry run of blocks 0 to 4b below, 2026-09-11, with a throwaway key and a bogus App
id: block 0 generated the 23-key registry and the settings model accepted it; block 2
produced a 14-key candidate carrying the 44-entry registry that the observer's loader
accepted; block 3 reached the token exchange on the observer's own transport and
failed there with `provider_not_found`, as it must without a real App; block 4b
produced the MCP candidate. Candidates and key were deleted; neither live file's
mtime moved.

Sandbox proof, run 2026-09-11 with `systemd-run --user` and the unit's exact
hardening properties, on the release interpreter, `PYTHONSAFEPATH=1`: a freshly
generated 0600 RSA key in `~/.config/brain-v42/` is read by `read_private_file`,
the uid inside the sandbox is the service uid (1001), `load_observer_settings`
loads the live env file, and `jwt.encode(..., algorithm="RS256")` signs with the
key. Exit 0. The probe key was deleted. Note that `load_observer_settings` already
reads the live env file through `read_private_file` at every start, so the
sandbox-versus-private-file interaction was never unexercised; only the key file is
new.

## Operator steps in the browser

These three steps cannot be scripted for a user account.

1. **Register the App**: <https://github.com/settings/apps/new>.
   - Name: `brain-v42-delivery-observer` (names are global; add a suffix if taken).
   - Homepage URL: `https://github.com/hawkixs/brain-v42`.
   - Webhook: **untick Active**. The observer polls; it receives nothing.
   - Repository permissions, all **Read-only**, all at once (widening later forces a
     re-approval of the installation): `Checks`, `Commit statuses`, `Contents`,
     `Metadata` (mandatory), `Pull requests`. Nothing else. No account permissions.
   - "Where can this GitHub App be installed?": **Only on this account**.
   - Create. Note the **App ID** on the General page.
2. **Generate one private key** (General page, "Private keys"). The browser
   downloads `<name>.<date>.private-key.pem`. Move it, never paste it into a shell:

   ```bash
   install -m 0600 ~/Downloads/brain-v42-delivery-observer.*.private-key.pem \
     ~/.config/brain-v42/delivery-observer-app.pem
   shred -u ~/Downloads/brain-v42-delivery-observer.*.private-key.pem
   test "$(stat -c '%u:%a' ~/.config/brain-v42/delivery-observer-app.pem)" = "$(id -u):600"
   ```

3. **Install the App** ("Install App" in the left menu → `hawkixs`):
   **All repositories**. Write down the exact wording of that radio button.
   GitHub's documentation does not promise that it covers repositories created
   later by a human, and the installation screen is the only place that question
   is answered; if the wording does not say "current and future", a new repository
   is one checkbox on the installation page.

Hand the App ID and the key path to the operator shell. The installation id is
discovered below; it also appears in the URL after step 3
(`https://github.com/settings/installations/<installation_id>`).

## Cutover from the operator shell

Shared variables for every block. `RELEASE` is the interpreter the live unit runs;
measure it, do not copy it.

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
OBSERVER_ENV=~/.config/brain-v42/delivery-observer.env
PEM_PATH=~/.config/brain-v42/delivery-observer-app.pem
CANDIDATE=~/.config/brain-v42/delivery-observer.env.candidate
RELEASE=$(grep -o '/home/hawixs/.local/share/brain-v42/releases/[0-9a-f]*' \
  ~/.config/systemd/user/brain-v42-delivery-observer.service.d/90-immutable-release.conf | head -1)
export APP_ID=<app id from step 1>
```

### 0. Generate the registry from GitHub and Brain

The rule is mechanical: an owned, non-archived repository whose name equals a Brain
project key maps to that project; the Factory executor project maps to all of them.
The registry is keyed by the ticket's `to_project`, so a Factory ticket targeting
repository X is accepted only if X is listed under `red-lab-factory`.

```bash
gh api 'user/repos?per_page=100&affiliation=owner' --paginate \
  --jq '.[] | select(.archived|not) | [.id, .full_name, .name] | @tsv' > /tmp/gh_repos.tsv
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select project_key from project_contexts order by 1;" > /tmp/brain_projects.txt
python3 - <<'PY'
import json
FACTORY = "red-lab-factory"
projects = {line.strip() for line in open("/tmp/brain_projects.txt") if line.strip()}
repos = [line.rstrip("\n").split("\t") for line in open("/tmp/gh_repos.tsv")]
matched = {name: (int(rid), full) for rid, full, name in repos if name in projects}
registry = {key: {str(rid): full} for key, (rid, full) in sorted(matched.items())}
registry[FACTORY] = {str(rid): full for _, (rid, full) in sorted(matched.items())}
json.dump(registry, open("/tmp/registry.json", "w"), separators=(",", ":"), sort_keys=True)
print("matched:", len(matched), "| unmatched repos:", sorted(n for _, _, n in repos if n not in projects))
PY
env -i HOME="$HOME" BRAIN_DELIVERY_REPOSITORY_REGISTRY="$(cat /tmp/registry.json)" "$RELEASE/venv/bin/python" -I - <<'PY'
from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import _REPOSITORY
s = DeliverySettings()
bad = [n for repos in s.repository_registry.values() for n in repos.values() if not _REPOSITORY.fullmatch(n)]
print("keys:", len(s.repository_registry), "| entries:", sum(map(len, s.repository_registry.values())), "| rejected names:", bad)
PY
```

### 1. Discover the installation with an App JWT (no installation token yet)

Prints the App's slug, permissions and events, then every installation. Expected:
permissions exactly `{checks, contents, metadata, pull_requests, statuses}` all
`read`, `events == []`, exactly one installation, account `hawkixs`,
`repository_selection == "selected"`.

```bash
env -i HOME="$HOME" APP_ID="$APP_ID" PEM_PATH="$PEM_PATH" "$RELEASE/venv/bin/python" -I - <<'PY'
import os, ssl, time
from pathlib import Path
import httpx, jwt
from brain_v42.delivery_observer.auth import read_private_file

pem = read_private_file(Path(os.environ["PEM_PATH"])).decode()
now = int(time.time())
bearer = jwt.encode({"iss": os.environ["APP_ID"], "iat": now - 60, "exp": now + 540}, pem, algorithm="RS256")
headers = {"Authorization": "Bearer " + bearer, "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "brain-v42-delivery-observer"}
with httpx.Client(verify=ssl.create_default_context(), trust_env=False, timeout=10) as http:
    app = http.get("https://api.github.com/app", headers=headers)
    body = app.json()
    print("app", app.status_code, body.get("slug"), "permissions", body.get("permissions"), "events", body.get("events"))
    installations = http.get("https://api.github.com/app/installations", headers=headers)
    print("installations", installations.status_code)
    if installations.status_code != 200:
        raise SystemExit(f"App JWT refused: {installations.json().get('message')}")
    for item in installations.json():
        print(" ", item["id"], item["account"]["login"], item["repository_selection"], item["permissions"])
PY
```

Then `export INSTALLATION_ID=<the single id printed>`.

### 2. Prepare a validated candidate env file

Writes `$CANDIDATE` (0600, same directory) = the live file minus
`BRAIN_DELIVERY_GITHUB_TOKEN`, plus the three App keys, with
`BRAIN_DELIVERY_REPOSITORY_REGISTRY` replaced by the generated registry (which must
contain every live entry). Everything else is carried over byte for byte. Validation uses the
observer's own loader, which also reads the PEM through `read_private_file`. The
live file is not touched.

```bash
env -i HOME="$HOME" OBSERVER_ENV="$OBSERVER_ENV" CANDIDATE="$CANDIDATE" APP_ID="$APP_ID" \
  INSTALLATION_ID="$INSTALLATION_ID" PEM_PATH="$PEM_PATH" REGISTRY_JSON=/tmp/registry.json \
  "$RELEASE/venv/bin/python" -I - <<'PY'
import json, os, stat, tempfile
from pathlib import Path
from brain_v42.delivery_observer.auth import load_private_environment
from brain_v42.delivery_observer.config import load_observer_settings

TOKEN = "BRAIN_DELIVERY_GITHUB_TOKEN"
APP = {"BRAIN_DELIVERY_GITHUB_APP_ID": os.environ["APP_ID"],
       "BRAIN_DELIVERY_GITHUB_INSTALLATION_ID": os.environ["INSTALLATION_ID"],
       "BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH": os.environ["PEM_PATH"]}
observer, candidate = Path(os.environ["OBSERVER_ENV"]), Path(os.environ["CANDIDATE"])
original = load_private_environment(observer)
assert TOKEN in original and not (APP.keys() & original.keys()), "live file is not in plain PAT mode"
registry = json.load(open(os.environ["REGISTRY_JSON"]))
desired = {k: v for k, v in original.items() if k != TOKEN} | APP
desired["BRAIN_DELIVERY_REPOSITORY_REGISTRY"] = json.dumps(registry, separators=(",", ":"), sort_keys=True)
assert len(desired) == len(original) + 2, (len(original), len(desired))
assert all(v and not any(c in v for c in "\r\n\0") for v in desired.values())
descriptor, name = tempfile.mkstemp(dir=candidate.parent, prefix=".delivery-observer.env.")
os.fchmod(descriptor, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    for key, value in desired.items():
        stream.write(f"{key}={value}\n")
    stream.flush(); os.fsync(stream.fileno())
os.replace(name, candidate)
assert load_private_environment(candidate) == desired
settings = load_observer_settings(candidate)  # validates the triplet and reads the PEM
live = load_observer_settings(observer)
for project, repos in live.repository_registry.items():
    assert repos.items() <= settings.repository_registry.get(project, {}).items(), f"live entry lost: {project}"
assert settings.github_app_id == int(APP["BRAIN_DELIVERY_GITHUB_APP_ID"])
assert settings.github_installation_id == int(APP["BRAIN_DELIVERY_GITHUB_INSTALLATION_ID"])
assert settings.github_token.get_secret_value() == ""
print("candidate ok:", len(original), "->", len(desired), "keys; registry keys:", len(settings.repository_registry),
      "entries:", sum(map(len, settings.repository_registry.values())))
PY
test "$(stat -c '%u:%a' "$CANDIDATE")" = "$(id -u):600"
```

### 3. Canary the App credential before any restart

Uses the observer's own `GitHubTransport` and `GitHubAuthProvider` on the candidate
file: JWT → installation token exchange → the exact requests the observer makes.
Nothing running is touched. Expected: `/installation/repositories` lists every registry
repository (all 26 owned repositories under "All repositories"; the 22 registered
ones must be among them); each `check-runs` call answers `200` and
`x-accepted-github-permissions` names `checks=read`.

```bash
env -i HOME="$HOME" CANDIDATE="$CANDIDATE" "$RELEASE/venv/bin/python" -I - <<'PY'
import asyncio, os, ssl
from pathlib import Path
import httpx
from brain_v42.delivery_observer.auth import GitHubAuthProvider
from brain_v42.delivery_observer.config import load_observer_settings
from brain_v42.delivery_observer.transport import GitHubTransport

async def main() -> None:
    settings = load_observer_settings(Path(os.environ["CANDIDATE"]))
    expected = {name for repos in settings.repository_registry.values() for name in repos.values()}
    async with httpx.AsyncClient(verify=ssl.create_default_context(), trust_env=False,
                                 timeout=settings.request_timeout_seconds,
                                 limits=httpx.Limits(max_connections=settings.max_concurrent_requests)) as http:
        transport = GitHubTransport(http, settings)
        auth = GitHubAuthProvider(settings, transport)
        headers = await auth.authorization_headers()  # POST /app/installations/{id}/access_tokens
        page = await transport.request_page("GET", "/installation/repositories?per_page=100", headers=headers)
        visible = {item["full_name"] for item in page.data["repositories"]}
        print("installation sees", len(visible), "repositories | registry needs", len(expected),
              "| missing:", sorted(expected - visible))
        raw = {**headers, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": settings.github_api_version,
               "User-Agent": "brain-v42-delivery-observer"}
        for name in sorted(expected):
            repo = (await transport.request_page("GET", f"/repos/{name}", headers=headers)).data
            path = f"/repos/{name}/commits/{repo['default_branch']}/check-runs?per_page=1&filter=all"
            checks = (await transport.request_page("GET", path, headers=headers)).data
            response = await http.get(settings.github_api_origin + path, headers=raw)
            print(f"{name}: id={repo['id']} private={repo['private']} check-runs total={checks['total_count']} "
                  f"raw={response.status_code} accepted={response.headers.get('x-accepted-github-permissions')}")
asyncio.run(main())
PY
```

`provider_not_found` at the first call means the installation id does not belong
to this App (dry-run measured 2026-09-11 with a bogus App: the token exchange answers
`404`). `provider_forbidden` means the JWT was refused, the App lacks a permission,
or the installation does not cover a repository. Fix it on GitHub and rerun; nothing
has changed on the host.

### 4. Switch, restart, verify

The live file is replaced atomically. Its previous content is kept as a 0600 backup
**only until the PAT is revoked**, because the backup is the rollback path and also a
live secret.

```bash
BACKUP=~/.config/brain-v42/delivery-observer.env.bak-$(date +%Y%m%d-%H%M%S)
install -m 0600 "$OBSERVER_ENV" "$BACKUP"
mv -f "$CANDIDATE" "$OBSERVER_ENV"
test "$(stat -c '%u:%a' "$OBSERVER_ENV")" = "$(id -u):600"
grep -c '^BRAIN_DELIVERY_GITHUB_TOKEN=' "$OBSERVER_ENV"   # expect 0
grep -c '^BRAIN_DELIVERY_' "$OBSERVER_ENV"                # expect 14 (12 - 1 + 3)

systemctl --user restart brain-v42-delivery-observer.service
sleep 5
systemctl --user show brain-v42-delivery-observer.service -p ActiveState,NRestarts,MainPID
PID=$(systemctl --user show brain-v42-delivery-observer.service -p MainPID --value)
tr '\0' '\n' < /proc/$PID/environ | grep -c '^BRAIN_DELIVERY_GITHUB_TOKEN='   # expect 0
tr '\0' '\n' < /proc/$PID/environ | grep -c '^BRAIN_DELIVERY_GITHUB_APP_ID='  # expect 1
journalctl --user -u brain-v42-delivery-observer.service --since '-2 min' --no-pager | tail -20
docker exec brain_v42_postgres psql -U brain -d brain -Atc \
  "select outcome, coalesce(error_code,'-'), count(*) from delivery_confirmations group by 1,2 order by 3 desc;"
```

`observer_configuration_invalid` with exit 2 at start means the loader refused the
file or the key; rollback is below. A false silence is expected: with no eligible
binding, the observer observes nothing, so the confirmations table will not move
until a live contract binds a PR on one of the three repositories. Any new
`provider_forbidden` row after this restart is attributable to the cutover.

### 4b. The second registry: the MCP process

`brain-mcp-http` reads its own copy of the registry from `delivery-mcp.env` at start
(drop-in `91-delivery-mode.conf`), and `contract_set`/`bind_pr` validate against
it. A registry that differs between the two files makes the MCP accept a contract
the observer will refuse, or the reverse. Restarting `brain-mcp-http` disconnects
every agent session on the host: pick the moment.

```bash
MCP_ENV=~/.config/brain-v42/delivery-mcp.env
test "$(stat -c '%u:%a' "$MCP_ENV")" = "$(id -u):600"
grep -c '^BRAIN_DELIVERY_' "$MCP_ENV"   # expect 2: ENABLED and REPOSITORY_REGISTRY
env -i HOME="$HOME" MCP_ENV="$MCP_ENV" REGISTRY_JSON=/tmp/registry.json "$RELEASE/venv/bin/python" -I - <<'PY'
import json, os, tempfile
from pathlib import Path
from brain_v42.delivery_observer.auth import load_private_environment
mcp = Path(os.environ["MCP_ENV"])
original = load_private_environment(mcp)
assert set(original) == {"BRAIN_DELIVERY_ENABLED", "BRAIN_DELIVERY_REPOSITORY_REGISTRY"}, sorted(original)
registry = json.load(open(os.environ["REGISTRY_JSON"]))
for project, repos in json.loads(original["BRAIN_DELIVERY_REPOSITORY_REGISTRY"]).items():
    assert repos.items() <= registry.get(project, {}).items(), f"live entry lost: {project}"
desired = dict(original)
desired["BRAIN_DELIVERY_REPOSITORY_REGISTRY"] = json.dumps(registry, separators=(",", ":"), sort_keys=True)
descriptor, name = tempfile.mkstemp(dir=mcp.parent, prefix=".delivery-mcp.env.")
os.fchmod(descriptor, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    for key, value in desired.items():
        stream.write(f"{key}={value}\n")
    stream.flush(); os.fsync(stream.fileno())
candidate = mcp.with_suffix(".env.candidate")
os.replace(name, candidate)
assert load_private_environment(candidate) == desired
print("mcp candidate ok:", candidate)
PY
install -m 0600 "$MCP_ENV" ~/.config/brain-v42/delivery-mcp.env.bak-$(date +%Y%m%d-%H%M%S)
mv -f ~/.config/brain-v42/delivery-mcp.env.candidate "$MCP_ENV"
systemctl --user restart brain-mcp-http.service && sleep 3
systemctl --user show brain-mcp-http.service -p ActiveState,NRestarts
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp   # expect 401: up, bearer enforced
PID=$(systemctl --user show brain-mcp-http.service -p MainPID --value)
tr '\0' '\n' < /proc/$PID/environ | grep '^BRAIN_DELIVERY_REPOSITORY_REGISTRY=' | tr ',' '\n' | grep -c 'hawkixs/'   # expect 44
```

The MCP backup holds no secret and may stay.

### 5. Revoke the PAT, delete the backup, schedule the key rotation

Deleting the env line is not a revocation. Revoke the fine-grained PAT at
<https://github.com/settings/personal-access-tokens>, then:

```bash
shred -u ~/.config/brain-v42/delivery-observer.env.bak-*
```

An App private key never expires, so the free forced rotation the PAT carried
(2026-12-05) disappears with it. Open a Brain self-ticket dated for the rotation. An
App holds up to 25 keys, so rotation is overlap-based: generate the new key, install
it at `$PEM_PATH`, restart (the key is cached for the life of the process), then
delete the old key on GitHub.

## Rollback

Before step 4 there is nothing to roll back: the live file and unit are untouched.
After step 4, while the backup exists and the PAT is not revoked:

```bash
mv -f "$BACKUP" "$OBSERVER_ENV" && systemctl --user restart brain-v42-delivery-observer.service
```

After revocation, rollback means a new credential, not a restore.

## What this cutover does not fix

The credential was never the weak link, and the App must not be credited for more
than it does. The "integration" half of the proof is independent (issued by the
observer from GitHub's record). The "fulfillment" half is not: `brain_delivery_accept`
requires only a non-placeholder `X-Brain-Agent` label, the three production tickets
are self-tickets, and the second anti-self-approval guard in the evaluator compares
a GitHub login to a Brain project key (ticket `e31f9ad6`). The PostgreSQL role
`brain` has `INSERT` on `delivery_confirmations`, so the observer and the agent it
judges are the same database principal. The trust boundary is the host. Those are
questions of `acceptance_mode`, real cross-project tickets, a non-empty
`allowed_reviewers` and PostgreSQL role separation, not of GitHub permissions.

Two operational limits survive unchanged: the 40 requests/minute budget is not
configurable upward and saturates around five simultaneously active bindings; and
a cached installation token is never invalidated on `401`/`403` (ticket `c475c92d`),
so uninstalling the App during an incident leaves the observer blind for up to an
hour.
