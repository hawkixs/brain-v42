FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# docker-py (via `docker` Python package) drives the daemon through the
# mounted /var/run/docker.sock — no native docker CLI needed in this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY main.py .

EXPOSE 8003

# Healthcheck targets the supervisor itself (not the backed container),
# so the supervisor is "healthy" as soon as its HTTP server is up.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8003/healthz',timeout=3).status==200 else 1)" || exit 1

CMD ["python", "main.py"]
