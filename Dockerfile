# PowerSOC — single-container image (web + in-app daily scheduler).
# Base pinned by DIGEST (multi-arch index of python:3.14-slim as merged
# 2026-10-21 via Dependabot: Python 3.14.8, OpenSSL 3.5.7) so rebuilds are
# reproducible and a re-tagged upstream image can't slip in. Refresh
# deliberately (Dependabot proposes it): update the digest, rebuild, re-test,
# then refresh constraints.txt.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POWERSOC_DATA_DIR=/data \
    POWERSOC_SCHEDULER=1 \
    HOME=/data \
    TZ=Europe/London
# (The container port is fixed at 8073 by the gunicorn bind below — remap with
# the compose `ports:` mapping, not POWERSOC_PORT, which only affects the
# dev-server path.)

WORKDIR /app

# Install deps first for layer caching. constraints.txt locks the TRANSITIVE
# tree (captured from a verified build) so a rebuild months later resolves the
# same versions instead of floating.
COPY requirements.txt constraints.txt ./
# pip itself is pinned too: the base image ships pip 25.0.1 (12 OSV
# advisories). Keep in step with PIP_VERSION in .github/workflows/ci.yml.
RUN pip install "pip==26.2.1" \
    && pip install -r requirements.txt -c constraints.txt

# App source (see .dockerignore for exclusions).
COPY . .

# Run as a non-root user. /data is the volume the app writes (SQLite DB,
# persisted instance id, Sunsynk token cache) so it must be owned by that
# user. /app stays ROOT-owned read-only — a compromised app process must not
# be able to rewrite its own code (PYTHONDONTWRITEBYTECODE covers the .pyc
# write the old chown existed for).
RUN groupadd --system --gid 10001 powersoc \
    && useradd --system --uid 10001 --gid 10001 powersoc \
    && mkdir -p /data \
    && chown powersoc:powersoc /data
USER powersoc

# SQLite DB + persisted instance id live here; mount a volume to keep them.
VOLUME ["/data"]

EXPOSE 8073
# Liveness: a wedged worker (all threads stuck on a hung upstream socket) used
# to sit "Up" forever — unhealthy status lets Docker/monitors see and act on it.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8073/api/health', timeout=4).status==200 else 1)"]
# Single worker (+ threads) so the in-app scheduler and first-run backfill run
# exactly once; create_app() is the WSGI factory.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "120", \
     "--bind", "0.0.0.0:8073", "--access-logfile", "-", "app:create_app()"]
