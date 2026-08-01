# D-022: containerized backend. No nwrfcsdk/pyrfc here on purpose — D-013
# already made RFC enrichment optional (ResilientRfcClient degrades to
# honest "RFC unavailable" instead of aborting), so the image stays plain
# python:slim + Playwright's own Chromium install rather than depending on
# the SAP-proprietary RFC SDK. If you need RFC enrichment in a container,
# see DEPLOY.md for how to extend this image the same way v1's Dockerfile
# does (COPY a local nwrfcsdk/, install pyrfc, set SAPNWRFC_HOME).

FROM python:3.12-slim

# Playwright's own apt-deps installer needs these to resolve Debian package
# names for whichever python:3.12-slim base ships at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# App code lives at /app; the process's CWD (WORKDIR, set below at the very
# end) is /data. Every relative path this app touches (systems.json,
# runs.db, reports/, flows/, run_artifacts/, datasets/) resolves against
# CWD, not against the code's location — so pointing CWD at /data makes
# /data the single volume-mount target without editing any of those paths.
# SYSTEMS_PATH / RUNS_DB_PATH env vars (main.py) are an explicit override on
# top of this if you'd rather not rely on the CWD trick.
WORKDIR /app

# Fixed, home-independent browsers path (default is ~/.cache/ms-playwright,
# which would be root's home at install time but app's home at run time —
# this env var makes both users look in the same place).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .
RUN chmod +x entrypoint.sh

# Non-root. Chromium's browsers dir and /data both need to be writable.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home-dir /home/app --create-home app \
    && mkdir -p /data \
    && chown -R app:app /app /data /home/app /ms-playwright

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS_DEPLOYMENT=1

USER app

EXPOSE 8000

VOLUME ["/data"]
WORKDIR /data

# entrypoint.sh seeds /data with the image's example flow/dataset on first
# boot, then execs uvicorn with --app-dir /app so main:app stays importable
# while CWD (and therefore every relative data path) is /data. Respects
# $PORT for ECS/Fargate, Cloud Run, etc., which inject their own port.
CMD ["/app/entrypoint.sh"]
