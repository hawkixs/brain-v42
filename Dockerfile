# brain_v42 - Multi-stage Dockerfile
# ============================================

# ===== BASE =====
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS base

ARG UV_VERSION=0.10.7

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast deps
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# ===== DEPS =====
# Copy only files that declare dependencies so this layer is cache-stable.
# A change to src/ does NOT invalidate the locked dependency sync.
FROM base AS deps

COPY pyproject.toml uv.lock README.md ./
# Install dependencies only (no --editable src yet — src/ not copied here).
# The placeholder src stub below satisfies the "package must exist" requirement
# of uv's editable project install without polluting the cache with real source files.
RUN mkdir -p src/brain_v42 && touch src/brain_v42/__init__.py
# Same trick for the wheel's force-include sources. hatchling is STRICT about them:
# absent, `uv sync` dies with "Forced include not found: /app/alembic" and the whole
# image refuses to build. Copying the real migrations HERE would make every new
# revision invalidate the locked dependency sync; the production stage copies them.
RUN mkdir -p alembic && touch alembic.ini
RUN uv sync --locked --no-dev --no-cache

# ===== DEV DEPS =====
FROM deps AS deps-dev

RUN uv sync --locked --no-dev --extra dev --no-cache

# ===== TEST =====
FROM deps-dev AS test

# Overwrite the stub with the real source tree, then add tests.
COPY src/ ./src/
COPY tests/ ./tests/

# Run tests
CMD ["pytest", "tests/unit", "-v", "--tb=short"]

# ===== PRODUCTION =====
FROM deps AS production

# Overwrite the stub with the real source tree.
COPY src/ ./src/

# The migrations, next to the tool that plays them. `/app/.venv/bin/alembic` was
# already here (pulled by the alembic>=1.13 dependency), but `ls /app/alembic`
# answered "No such file": the image could not migrate its own database.
# alembic.ini resolves script_location as %(here)s/alembic -- next to itself -- so
# this layout works from any working directory, not just WORKDIR /app.
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Normalize checkout-dependent modes while keeping code and dependencies root-owned.
RUN chmod -R u=rwX,go=rX /app

# Create non-root user.
RUN useradd -m -u 1000 appuser
USER appuser

CMD ["python", "-m", "brain_v42.mcp.server"]
