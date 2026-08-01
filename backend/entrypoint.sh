#!/bin/sh
# D-022: WORKDIR is /data (see Dockerfile) so every relative state path the
# app touches — systems.json, runs.db, reports/, run_artifacts/, and
# notably flows/ and datasets/ — resolves under the mounted volume, not
# under /app where the image's seed content (the one recorded example flow,
# flows/sm37_cancelled_jobs.json, and datasets/daily.json) actually lives.
# Without this, that seed content would be baked into the image but
# invisible to the running app on first boot. This copies it into /data
# once, only if /data doesn't already have its own (real, possibly
# recorder-created) copy — so it never clobbers a volume that's already in
# use.
set -e

mkdir -p /data/flows /data/datasets /data/reports /data/run_artifacts

if [ -z "$(ls -A /data/flows 2>/dev/null)" ] && [ -d /app/flows ]; then
    cp -a /app/flows/. /data/flows/
fi

if [ -z "$(ls -A /data/datasets 2>/dev/null)" ] && [ -d /app/datasets ]; then
    cp -a /app/datasets/. /data/datasets/
fi

# systems.json is deliberately NOT seeded here — D-021's Systems CRUD page
# is the intended way to add systems, and Registry.load() already returns
# an empty registry (not a crash) when the file doesn't exist yet.

exec uvicorn main:app --app-dir /app --host 0.0.0.0 --port "${PORT:-8000}"
