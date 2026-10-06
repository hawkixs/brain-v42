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
# uv installs headless-agents from its pinned Git tag in uv.lock.
# Install dependencies only (no --editable src yet — src/ not copied here).
# The placeholder src stub below satisfies the "package must exist" requirement
# of uv's editable project install without polluting the cache with real source files.
RUN mkdir -p src/brain_v42 && touch src/brain_v42/__init__.py
# Same trick for the wheel's force-include sources. hatchling is STRICT about them:
# absent, `uv sync` dies with "Forced include not found: /app/alembic" and the whole
# image refuses to build. Copying the real migrations HERE would make every new
# revision invalidate the locked dependency sync; the production stage copies them.
RUN mkdir -p alembic && touch alembic.ini
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && uv sync --locked --no-dev --no-cache \
    && apt-get purge -y --auto-remove git \
    && rm -rf /var/lib/apt/lists/*

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

# PGDG's archive retains exact versions; the client major matches Compose's PG16.
ARG POSTGRES_CLIENT_VERSION=16.14-1.pgdg13+1
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates gnupg \
    && curl --fail --silent --show-error --location \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /tmp/pgdg.asc \
    && test "$(gpg --show-keys --with-colons /tmp/pgdg.asc | awk -F: '$1 == "fpr" {print $10; exit}')" \
        = B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8 \
    && gpg --dearmor --output /usr/share/keyrings/postgresql-pgdg.gpg /tmp/pgdg.asc \
    && chmod 644 /usr/share/keyrings/postgresql-pgdg.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/postgresql-pgdg.gpg] https://apt-archive.postgresql.org/pub/repos/apt trixie-pgdg-archive main' \
        > /etc/apt/sources.list.d/pgdg.list \
    && printf "Package: *\nPin: origin apt-archive.postgresql.org\nPin-Priority: 100\n\nPackage: postgresql-client-16\nPin: version ${POSTGRES_CLIENT_VERSION}\nPin-Priority: 1001\n" \
        > /etc/apt/preferences.d/pgdg \
    && apt-get update && apt-get install -y --no-install-recommends \
        "postgresql-client-16=${POSTGRES_CLIENT_VERSION}" \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/* /tmp/pgdg.asc

# Overwrite the stub with the real source tree.
COPY src/ ./src/

# The migrations, next to the tool that plays them. `/app/.venv/bin/alembic` was
# already here (pulled by the alembic>=1.13 dependency), but `ls /app/alembic`
# answered "No such file": the image could not migrate its own database.
# alembic.ini resolves script_location as %(here)s/alembic -- next to itself -- so
# this layout works from any working directory, not just WORKDIR /app.
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY ops/recovery/ ./ops/recovery/

ARG VERSION=
ARG GIT_SHA=
ENV BRAIN_RELEASE_VERSION=$VERSION BRAIN_RELEASE_SHA=$GIT_SHA
RUN python -m brain_v42.release_recovery publish-image /app /app/recovery --release-sha "$GIT_SHA"
LABEL org.opencontainers.image.revision=$GIT_SHA

# Normalize checkout-dependent modes while keeping code and dependencies root-owned.
RUN chmod -R u=rwX,go=rX /app

# Create non-root user.
RUN useradd -m -u 1000 appuser
USER appuser

CMD ["python", "-m", "brain_v42.mcp.server"]
