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
export POSTGRES_URL="postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain"
BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade head
pytest tests/integration -v
```

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
