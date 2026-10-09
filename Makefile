# Local entry points for the CI checks that need no database and no Docker
# (.github/workflows/continuous-integration.yml). `make ci` is the rail's task runner
# contract: lint, format, types, module layering, image pins, bandit, the dependency
# audit and the unit tests, against the venv CI builds (`make sync`). DB-backed unit
# tests skip without BRAIN_V42_TEST_DB_URL, and nothing here runs `alembic upgrade`:
# a local POSTGRES_URL may name a shared or production database. Integration,
# coverage, gitleaks (Docker) and the image build run only in the workflow, which
# stays the merge gate.

VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: ci sync lint format-check typecheck layering pins bandit audit test-unit

ci: lint format-check typecheck layering pins bandit audit test-unit

sync:
	uv sync --locked --no-dev --extra dev --python 3.12

lint:
	$(BIN)/ruff check .

format-check:
	$(BIN)/ruff format --check .

typecheck:
	$(BIN)/mypy src/

layering:
	$(BIN)/python scripts/check_module_layering.py --package src/brain_v42

pins:
	$(BIN)/python scripts/check_container_image_pins.py

bandit:
	$(BIN)/bandit -q -ll -r src/

# The export goes to a temporary file, never into the working tree.
audit:
	@tmp=$$(mktemp) && trap 'rm -f "$$tmp"' EXIT && \
	uv export --locked --no-dev --extra dev --format requirements-txt \
		--no-emit-workspace --no-emit-package headless-agents -o "$$tmp" >/dev/null && \
	$(BIN)/pip-audit -r "$$tmp" --require-hashes --disable-pip \
		--vulnerability-service pypi --timeout 15 --strict --progress-spinner=off

test-unit:
	$(BIN)/pytest tests/unit/ -q
