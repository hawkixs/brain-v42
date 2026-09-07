# Contributing to brain-v42

Thanks for considering a contribution. This project is maintained on a small
scale, so the bar for merging is the same bar the maintainer holds
themselves to: tests first, toolchain pinned, and nothing merges on a red
gate.

## Before you start

- Check open issues and pull requests first — avoid duplicate work.
- For anything non-trivial (a new tool, a schema change, a behavior
  change), open an issue to discuss the approach before writing code. Small
  fixes (typos, docs, an obvious bug) can go straight to a pull request.

## Development setup

```bash
git clone https://github.com/hawkixs/brain-v42 && cd brain-v42
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The dev toolchain is pinned **exactly** in `pyproject.toml`
(`[project.optional-dependencies].dev`) — pytest, ruff, mypy and the
security scanners all resolve to the same versions CI runs. Installing via
`pip install -e ".[dev]"` is what keeps local results predictable; a
floating version is how a change looks green locally and red in CI.

## TDD is mandatory

This project follows strict red-green-refactor:

1. Write the test for the behavior you want. Run it — it must fail, and for
   the right reason (not a typo, not a missing import).
2. Write the minimum implementation to make it pass.
3. Refactor with the test green as your safety net.
4. Never edit a test to make failing code pass. If the test was wrong,
   that's a decision to document in the commit message, not a silent edit.

A pull request that adds behavior without a test that would have failed
without it will be asked to add one before review continues.

## Running the tests

```bash
# Unit tests — no PostgreSQL, no Neo4j, no embedding service required
pytest tests/unit -v

# With coverage (CI blocks under 60%)
pytest --cov=brain_v42 --cov-report=term-missing

# Integration tests — need real services, brought up via docker compose
docker compose up -d
# The ONLY variable the integration suite reads. `POSTGRES_URL` is deliberately
# ignored here, so a shell configured for a live database cannot redirect the
# suite. Point it at an isolated test database — never at `brain`.
export BRAIN_V42_TEST_DB_URL="postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain_test"
pytest tests/integration -v
```

The suite migrates that database itself, under an advisory lock; you do not
run `alembic` by hand for it. A URL whose database name is `brain` is
refused outright.

**Without `BRAIN_V42_TEST_DB_URL` the whole suite skips and exits 0** —
measured on 2026-09-02: `423 skipped in 1.04s`. That is green, and it proves
nothing. Since ticket `634203e0` the run ends with a summary line naming the
variable and the number of tests that measured nothing, so the result cannot
be misread as a pass; CI sets the variable per job and keeps its authority.

Unit tests that would otherwise touch a real database skip themselves
loudly unless `BRAIN_V42_TEST_DB_URL` points at an isolated test database —
this is intentional, so a bare `pytest tests/unit` run can never silently
write into whatever `POSTGRES_URL` happens to be exported in your shell.

## Linting and types

```bash
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
python scripts/check_module_layering.py --package src/brain_v42
```

All four are blocking gates in CI. `check_module_layering.py` is not
cosmetic: it proves the top-level module graph under `src/brain_v42/` stays
acyclic, which is what keeps every subpackage extractable into its own
service later without dragging a cycle along.

## Security scanning

```bash
bandit -ll -r src/
```

Run `bandit` without a pipe after it — piping into `tail` or similar
swallows its exit code and a failing scan reads as green.

CI scans with `gitleaks dir . --no-banner --redact --exit-code 1` (`gitleaks
8.30` dropped the older `detect` subcommand). A raw local checkout scans far
more than the tracked tree — build artifacts, `.venv`, caches — and the
result won't match what CI sees. Scan a clean export instead:

```bash
git archive HEAD -o /tmp/brain-v42-clean.tar && \
  mkdir -p /tmp/brain-v42-clean && tar -xf /tmp/brain-v42-clean.tar -C /tmp/brain-v42-clean && \
  gitleaks dir /tmp/brain-v42-clean --no-banner --redact --exit-code 1
```

## Language

**Everything published to this repository is written in English.** That covers
commit messages (subject and body), branch names, pull request titles and
descriptions, issues, review comments, release notes and tag messages, and any
new file added to the tree — docs, code comments, test names, CI workflows.

The maintainer's working language is not English, and part of the history and
of `docs/` predates this rule; those files are left coherent in their original
language until a deliberate translation pass, rather than drifting into a
mix of both. New content does not get that grandfather clause.

**Exception — byte-exact apart from a declared anonymisation pass
(decided 2026-09-07).** A test fixture that captures a live artifact —
operator configuration, a drop-in, a captured log excerpt — is evidence,
not prose, and the four post-rule byte-exact captures named here are
exempt from the English-only rule for new files, each on one of two
grounds. Ground one: a test compares it byte-for-byte with its live
source, so retranslating it would break the identity the test exists to
check — `models.conf.2026-09-03-live-dream-drop-in` is the only fixture
on this ground, checked against the live systemd drop-in by
`tests/unit/test_dream_roadmap_configured_primary_is_guarded.py:139`; a
drift alarm on the machine that holds the source, and a skip everywhere
else, including CI's hosted runner, where the live file cannot exist.
Ground two: a test replays it because the process itself produced it,
and a hand-invented fixture would only prove the parser agrees with
itself rather than a real night happening (learning 187f107c, stated at
`tests/unit/test_reorg_report.py:336`) — the other three fixtures are on
this ground: `2026-09-03_brain-v42_reorg.anonymised.log`, replayed by
`tests/unit/test_reorg_report.py` and by
`tests/unit/test_dream_post_run_alert_reorg_line.py`, and
`2026-09-04_roadmap.excerpt.log` and `2026-09-05_roadmap.excerpt.log`,
replayed by `tests/unit/test_dream_post_run_alert_roadmap_shrink_line.py`.
Their parsers key on markers (`[n/n]`, `· shrunk`, the JSON trailer), not
on the French words around them, so retranslating one would leave every
assertion green while turning evidence of a real night into a
hand-written stand-in — the loss is silent, not a test failure.

Golden-output fixtures are a different case, outside this exception:
`tests/fixtures/briefing_full.md` is the session-briefing renderer's own
output, in the product's language, compared as text against freshly
rendered output — it is not a captured external artifact compared
byte-for-byte, nor a replay of a process run as evidence, so neither
ground above applies to it and this exception says nothing about it.

The exemption is narrow in a different way than "byte-exact" alone
would suggest. A capture that would carry a secret is not eligible for
it at all — it must be synthetic instead. Anonymising identifiers or
private corpus content is a separate move from redacting a secret, and
this exception allows it, provided the pass is declared both in the
filename and in a header comment: `2026-09-03_brain-v42_reorg.anonymised.log`
does exactly that, its header recording that the 28 entity UUIDs were
swapped for deterministic synthetic ones (same real id maps to the same
fake id, so duplicates and cross-references survive) and the quoted
corpus topics were elided, while structure, markers, counts and the
rest of the French prose stay verbatim. That declared pass is what
keeps the file inside the exception, not outside it — call this
category byte-exact apart from a declared anonymisation pass, not
byte-exact without qualification.

## Commit conventions

Conventional Commits, in English: `feat(scope): ...`, `fix(scope): ...`,
`refactor(scope): ...`, `test(scope): ...`, `docs(scope): ...`,
`chore(scope): ...`. Keep commits atomic — a security fix and a docs
change are two commits, not one.

## Pull request checklist

- [ ] Tests written first, and they failed for the right reason before the
      implementation existed.
- [ ] `pytest tests/unit`, `ruff check`, `ruff format --check`, `mypy src/`
      and `check_module_layering.py` all green.
- [ ] No secret, personal path, or private hostname introduced (see
      [SECURITY.md](SECURITY.md) for the trust model this repository
      assumes).
- [ ] Commit messages follow Conventional Commits and explain the *why*,
      not just the *what*.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0 license](LICENSE). Do not contribute code you don't
have the rights to license this way.
