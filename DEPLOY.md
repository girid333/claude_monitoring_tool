# Deploying claude_monitoring_tool (D-022)

Two containers: `backend` (FastAPI + Playwright, port 8000) and `frontend`
(Next.js standalone, port 3000). No dependency on the SAP-proprietary RFC
SDK — RFC enrichment is optional (D-013) and simply degrades to "unavailable"
in a container unless you extend the backend image yourself (see below).

## Quick start — Docker or Podman

```bash
cd claude_monitoring_tool

# Generate the registry encryption key ONCE (D-023) — needed before you can
# store any SSH credentials for app/dialog servers or DB hosts via the
# Systems page. Keep it safe: losing it makes every stored SSH password
# unrecoverable (you'd re-enter them, nothing else breaks).
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Optional: SAP WebGUI login credentials (D-018, still env-var-only —
# unaffected by D-023) — never baked into the image.
cat > .env <<'EOF'
REGISTRY_ENCRYPTION_KEY=<paste the key generated above>
SAP_Q4H_USER=...
SAP_Q4H_PASSWORD=...
EOF

docker compose up --build
# or: podman compose up --build   /  podman-compose up --build
```

**REGISTRY_ENCRYPTION_KEY (D-023)** is only required once you add SSH
credentials through the Systems page (app/dialog server or DB host `df -h`
checks). Everything else — adding systems, SAP WebGUI login, running flows —
works without it. If it's unset and you try to save an SSH password, the API
returns a clear error telling you to generate and set it; it's never
auto-generated (an auto-generated key that changes on every restart would
silently orphan previously-encrypted passwords).

Frontend: http://localhost:3000 — Systems, Runs, Recorder, Reports.
Backend: http://localhost:8000/docs

Data (systems.json, runs.db, reports/, flows/, datasets/, run_artifacts/)
persists in the `backend_data` named volume across restarts. First boot
seeds it with the one example flow (`sm37_cancelled_jobs.json`) and dataset
shipped in the image.

### First lockfile generation (do this once)

No `package-lock.json` is committed yet, so `frontend/Dockerfile` currently
runs `npm install` instead of `npm ci`. For reproducible builds, run once
locally and commit the result:

```bash
cd frontend && npm install   # generates package-lock.json
```

Then in `frontend/Dockerfile`, swap the `deps` stage's `npm install` for
`npm ci` (also `COPY package.json package-lock.json ./`).

## Plain Docker (no compose)

```bash
docker build -t sap-backend ./backend
docker run -d -p 8000:8000 -v sap_data:/data \
  -e SAP_Q4H_USER=... -e SAP_Q4H_PASSWORD=... sap-backend

docker build -t sap-frontend --build-arg NEXT_PUBLIC_API=http://<backend-host>:8000 ./frontend
docker run -d -p 3000:3000 sap-frontend
```

`NEXT_PUBLIC_API` is a **build arg**, not a runtime env var — Next.js
inlines `NEXT_PUBLIC_*` values into the client bundle at build time. If the
backend's reachable address changes, rebuild the frontend image with a new
`--build-arg`.

## AWS ECS / Fargate

- Push both images to ECR; two task definitions or two containers in one
  task (with `frontend` → `NEXT_PUBLIC_API` pointed at the backend's
  service-discovery DNS name or ALB path).
- Backend container: mount an **EFS access point** at `/data` for
  persistence across task restarts/redeployments (Fargate has no local
  persistent disk). Without it, systems/runs/reports vanish on every
  redeploy.
- Backend already respects `$PORT` (falls back to 8000) — set it via the
  task definition if your target group expects something else.
- Put `SAP_<SID>_USER` / `SAP_<SID>_PASSWORD` (D-018) and
  `REGISTRY_ENCRYPTION_KEY` (D-023) in **Secrets Manager** and reference
  them as `secrets` (not `environment`) in the task definition — never as
  plaintext task-def env vars. SSH credentials themselves (D-023) live
  encrypted inside `systems.json` on the `/data` EFS volume, not as
  per-system env vars — only the one master key needs a secret entry.
- Security group: backend only needs outbound HTTPS to the SAP WebGUI host
  (+ SSH/DB ports if using Core BASIS filesystem checks) and inbound 8000
  from the frontend/ALB. No inbound needed on the frontend beyond the ALB.

## What does NOT work in a container: the Recorder

`/api/recorder/start` launches a **headed** (`headless=False`) Chromium — a
human watches and clicks through a real browser window to record a flow
(D-004/D-020b). That's structurally impossible in a headless container.

Rather than let this fail with a raw Playwright "no display" error, the
image sets `HEADLESS_DEPLOYMENT=1`, which makes `/api/recorder/start`
return a clear `501` instead. `status`/`active`/`stop`/`cancel` still work
(they only touch existing in-process sessions, none of which will exist in
a container). **To record new flows, run the backend locally**
(`uvicorn main:app --reload`, per the main README) where a display exists,
then commit the resulting `flows/*.json` and rebuild/redeploy the
container — recording is a one-time authoring step, not a runtime need.

## Extending with RFC enrichment (optional, off by default)

D-013 made RFC enrichment optional by design — the backend image
deliberately ships without `pyrfc`/`nwrfcsdk` so it stays a plain
`python:3.12-slim` + Playwright image. If you want the ~4 RFC-backed
T-codes' real data in Core BASIS runs, extend `backend/Dockerfile` the way
`sap_monitoring/backend/Dockerfile` (v1) does: `COPY` a local `nwrfcsdk/`
folder, `pip install pyrfc`, and set `SAPNWRFC_HOME` /
`LD_LIBRARY_PATH`. Everything else (the 19 non-RFC T-codes, WebGUI
monitoring, Functional Sanity flows) is unaffected either way —
`ResilientRfcClient` (D-013) degrades cleanly without it.

## Config reference

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `SYSTEMS_PATH` | backend | `systems.json` (under `/data`) | override registry file location |
| `RUNS_DB_PATH` | backend | `runs.db` (under `/data`) | override SQLite db location |
| `CORS_ORIGINS` | backend | `http://localhost:3000` | comma-separated allowed origins |
| `HEADLESS_DEPLOYMENT` | backend | `1` (set in image) | disables `/api/recorder/start` with a clear 501 |
| `PORT` | backend | `8000` | uvicorn bind port (ECS/Fargate/Cloud Run inject this) |
| `SAP_<SID>_USER` / `_PASSWORD` | backend | — | SAP WebGUI login credentials (D-018, env-only, never stored) |
| `REGISTRY_ENCRYPTION_KEY` | backend | — | Fernet key encrypting SSH credentials stored in `systems.json` (D-023) — required only once you add an SSH credential via the Systems page |
| `NEXT_PUBLIC_API` | frontend | `http://localhost:8000` | **build arg**, backend URL baked into the client bundle |

Note: `SAP_<SID>_SSH_USER`/`_SSH_PASSWORD`/`_SSH_KEY_PATH` env vars from
earlier versions of this doc are **obsolete** — D-023 moved SSH credentials
into the registry (Systems page → per-host, encrypted). If you're upgrading
from before D-023, re-enter SSH credentials through the UI; the old env
vars are no longer read.
