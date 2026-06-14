# Cartograph quickstart — demo container.
# Installs the proven core (packages/core) and runs the dramatic, scripted demo.
# This image contains NO caching logic of its own; it only drives the engine.
FROM python:3.11-slim

# psycopg2-binary ships wheels, so no build toolchain is needed.
WORKDIR /app

# Install the core package first (its own layer) for cache-friendly rebuilds.
# Build context is the repo root (see docker-compose.yml `context: ..`).
COPY packages/core /app/core
RUN pip install --no-cache-dir -e /app/core

# The demo layer (changes most often → last layer).
COPY quickstart/demo.py /app/demo.py

# Unbuffered output so the dramatic pacing streams live to `docker compose up`.
ENV PYTHONUNBUFFERED=1

# demo.py is self-contained: it waits for the sample Postgres, calls
# Cartograph.bootstrap() to seed the schema + replication slot + DDL trigger,
# then runs the scripted never-stale demo. Invoking python directly (rather than
# a shell wrapper) avoids any CRLF-on-checkout pitfall from Windows clones.
ENTRYPOINT ["python", "/app/demo.py"]
