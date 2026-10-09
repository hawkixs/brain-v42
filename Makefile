# Local entry points for the checks CI runs (.github/workflows/continuous-integration.yml).
# `make ci` is the rail's task runner contract: it replays the blocking lint, type,
# layering, image-pin and unit-test steps against the venv `uv sync` builds.

VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: ci sync lint format-check typecheck layering pins test-unit

ci: lint format-check typecheck layering pins test-unit

sync:
	uv sync --locked --extra dev --python 3.12

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

test-unit:
	$(BIN)/pytest tests/unit/ -q
