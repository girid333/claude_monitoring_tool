"""FastAPI backend — the D-016 API surface the frontend needs.

Endpoints:
  GET    /api/systems                     registry + last-run summary + credential status
  POST   /api/systems                     create a system (D-021, no credential fields)
  PUT    /api/systems/{sid}               update a system
  DELETE /api/systems/{sid}               remove a system
  GET    /api/systems/{sid}/credentials   presence-only credential check + env var hint
  POST /api/run                           start a run (background) -> {run_id}
  GET  /api/events/{run_id}               SSE stream of per-step/per-flow/report events
  GET  /api/runs?sid=                     recent flow run history (RunStore)
  GET  /api/runs/core-basis?sid=          recent Core BASIS section run history (D-017)
  GET  /api/reports?sid=                  recent generated Word/PDF reports (D-014)
  GET  /api/reports/{run_id}/{sid}/download/{docx|pdf}   fetch a generated report file
  POST /api/recorder/start                {sid} -> open a headed browser + start recording (D-004)
  GET  /api/recorder/status/{recording_id}  live event count for the Recorder page
  GET  /api/recorder/active               all in-progress recording sessions
  POST /api/recorder/stop                 {recording_id, flow_id} -> save flows/{flow_id}.json
  POST /api/recorder/cancel               {recording_id} -> discard, close the browser

Design notes:
  - SSE (not the v1 2s global poll): the Dynamic Island subscribes here.
  - Runs execute sequentially per system for now; the event bus and store
    are already keyed run_id+sid (D-010) so parallelism is a loop change.

Run:  uvicorn main:app --reload   (from backend/)
CLI:  python -m engine.run --sid Q4H --flow flows/sm37_cancelled_jobs.json
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from engine.registry import EnvCredentialProvider, Registry, SystemEntry, credential_status
from engine.store import RunStore

# D-022 (containerization): all three are plain relative paths by default,
# resolved against the process's CWD — exactly what makes the container's
# `WORKDIR /data` + `--app-dir /app` split work without touching a line of
# this logic (see backend/Dockerfile). The env vars are an explicit override
# on top of that, for anyone who'd rather not rely on the CWD trick.
SYSTEMS_PATH = os.environ.get("SYSTEMS_PATH", "systems.json")
RUNS_DB_PATH = os.environ.get("RUNS_DB_PATH", "runs.db")

# D-022: recording (Phase 2 UI) needs a real display (headless=False,
# D-020b) — impossible in a container. Rather than let it fail with a raw
# Playwright "no display" error, HEADLESS_DEPLOYMENT=1 (set in the
# Dockerfile) makes every /api/recorder/* call return a clear, actionable
# 501 instead.
HEADLESS_DEPLOYMENT = os.environ.get("HEADLESS_DEPLOYMENT") == "1"

# D-022: CORS origins configurable — the hardcoded localhost:3000 only
# works for local dev; a containerized frontend is reachable at whatever
# host ECS/the reverse proxy puts it behind.
_cors_origins = os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")

app = FastAPI(title="Claude Monitoring Tool")
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["*"], allow_headers=["*"])

store = RunStore(RUNS_DB_PATH)
_event_queues: dict[str, asyncio.Queue] = {}


def _require_display() -> None:
    if HEADLESS_DEPLOYMENT:
        raise HTTPException(
            501,
            "Recording requires a local machine with a real display "
            "(D-004/D-020b) and is disabled in this headless/container "
            "deployment (D-022). Run the backend locally "
            "(uvicorn main:app) to use the Recorder.")


class RunRequest(BaseModel):
    sids: list[str]
    flows: list[str] = []            # paths under flows/
    dataset: str | None = None
    include_core_basis: bool = False  # D-017: also run v1's 23-T-code sweep


class RecorderStartRequest(BaseModel):
    sid: str


class RecorderStopRequest(BaseModel):
    recording_id: str
    flow_id: str
    description: str = ""


class RecorderCancelRequest(BaseModel):
    recording_id: str


async def emit(run_id: str, event: dict) -> None:
    q = _event_queues.get(run_id)
    if q:
        await q.put(event)


@app.get("/api/systems")
def systems() -> list[dict]:
    reg = Registry.load(SYSTEMS_PATH)
    out = []
    for s in reg.systems:
        recent = store.recent(sid=s.sid, limit=1)
        out.append({**s.model_dump(),
                    "last_run": recent[0] if recent else None,
                    "credentials": credential_status(s.sid, s.ssh_auth_method)})
    return out


@app.post("/api/systems")
def create_system(entry: SystemEntry) -> SystemEntry:
    """D-021: replaces hand-editing systems.json. Registry only ever holds
    non-secret connection topology (D-006/D-018/D-021) — credentials are
    never accepted here; set SAP_<SID>_USER/PASSWORD (+ SSH equivalents)
    as environment variables on the machine running this backend."""
    reg = Registry.load(SYSTEMS_PATH)
    try:
        reg.get(entry.sid)
        raise HTTPException(409, f"system {entry.sid!r} already exists — use PUT to update it")
    except KeyError:
        pass
    reg.upsert(entry)
    reg.save(SYSTEMS_PATH)
    return entry


@app.put("/api/systems/{sid}")
def update_system(sid: str, entry: SystemEntry) -> SystemEntry:
    if entry.sid != sid:
        raise HTTPException(400, "body sid must match the URL sid")
    reg = Registry.load(SYSTEMS_PATH)
    try:
        reg.get(sid)
    except KeyError:
        raise HTTPException(404, f"unknown sid {sid!r}")
    reg.upsert(entry)
    reg.save(SYSTEMS_PATH)
    return entry


@app.delete("/api/systems/{sid}")
def delete_system(sid: str) -> dict:
    reg = Registry.load(SYSTEMS_PATH)
    if not reg.remove(sid):
        raise HTTPException(404, f"unknown sid {sid!r}")
    reg.save(SYSTEMS_PATH)
    return {"deleted": sid}


@app.get("/api/systems/{sid}/credentials")
def system_credentials(sid: str) -> dict:
    """Presence-only check (never returns values) so the Systems page can
    show a configured/missing badge and tell the user exactly which env
    vars to set, without this API ever being able to leak a secret."""
    reg = Registry.load(SYSTEMS_PATH)
    try:
        entry = reg.get(sid)
    except KeyError:
        raise HTTPException(404, f"unknown sid {sid!r}")
    status = credential_status(sid, entry.ssh_auth_method)
    return {
        "sid": sid,
        **status,
        "hint": {
            "sap": f"export SAP_{sid}_USER=... SAP_{sid}_PASSWORD=...",
            "ssh": (f"export SAP_{sid}_SSH_USER=... SAP_{sid}_SSH_KEY_PATH=..."
                    if entry.ssh_auth_method == "key"
                    else f"export SAP_{sid}_SSH_USER=... SAP_{sid}_SSH_PASSWORD=..."),
        },
    }


@app.post("/api/run")
async def start_run(req: RunRequest, background: BackgroundTasks) -> dict:
    run_id = uuid.uuid4().hex[:12]
    _event_queues[run_id] = asyncio.Queue()
    background.add_task(_execute_run, run_id, req)
    return {"run_id": run_id}


@app.get("/api/events/{run_id}")
async def events(run_id: str) -> StreamingResponse:
    if run_id not in _event_queues:
        raise HTTPException(404, "unknown run")

    async def stream():
        q = _event_queues[run_id]
        while True:
            ev = await q.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("type") == "run_done":
                break
        _event_queues.pop(run_id, None)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/runs")
def runs(sid: str | None = None, limit: int = 20) -> list[dict]:
    return store.recent(sid=sid, limit=limit)


@app.get("/api/runs/core-basis")
def core_basis_runs(sid: str | None = None, limit: int = 20) -> list[dict]:
    return store.recent_core_basis(sid=sid, limit=limit)


@app.get("/api/reports")
def reports(sid: str | None = None, limit: int = 20) -> list[dict]:
    return store.recent_reports(sid=sid, limit=limit)


@app.get("/api/reports/{run_id}/{sid}/download/{kind}")
def download_report(run_id: str, sid: str, kind: str) -> FileResponse:
    if kind not in ("docx", "pdf"):
        raise HTTPException(400, "kind must be docx or pdf")
    rows = [r for r in store.recent_reports(sid=sid, limit=200) if r["run_id"] == run_id]
    if not rows or not rows[0].get(f"{kind}_path"):
        raise HTTPException(404, "report not found")
    path = rows[0][f"{kind}_path"]
    if not Path(path).exists():
        raise HTTPException(404, "report file missing on disk")
    return FileResponse(path, filename=Path(path).name)


# ── Recorder (Phase 2, D-004) ────────────────────────────────────────────
# A recording session is a HEADED Playwright browser a human drives
# interactively; see engine/recorder_service.py for why this can't be a
# background task like /api/run. Only usable when the backend runs on a
# machine with a display (README's "run locally" model).

@app.post("/api/recorder/start")
async def recorder_start(req: RecorderStartRequest) -> dict:
    _require_display()
    from engine.recorder_service import start_recording
    try:
        session = await start_recording(req.sid)
    except KeyError:
        raise HTTPException(404, f"unknown sid {req.sid!r} in systems.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not start recording: {exc}")
    return {"recording_id": session.recording_id, "sid": session.sid}


@app.get("/api/recorder/status/{recording_id}")
def recorder_status(recording_id: str) -> dict:
    from engine.recorder_service import status
    try:
        return status(recording_id)
    except KeyError:
        raise HTTPException(404, "unknown recording_id")


@app.get("/api/recorder/active")
def recorder_active() -> list[dict]:
    from engine.recorder_service import list_active
    return list_active()


@app.post("/api/recorder/stop")
async def recorder_stop(req: RecorderStopRequest) -> dict:
    from engine.recorder_service import stop_recording
    try:
        return await stop_recording(req.recording_id, req.flow_id, req.description)
    except KeyError:
        raise HTTPException(404, "unknown recording_id")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not stop recording: {exc}")


@app.post("/api/recorder/cancel")
async def recorder_cancel(req: RecorderCancelRequest) -> dict:
    from engine.recorder_service import cancel_recording
    try:
        await cancel_recording(req.recording_id)
    except KeyError:
        raise HTTPException(404, "unknown recording_id")
    return {"cancelled": True}


async def _execute_run(run_id: str, req: RunRequest) -> None:
    """Sequential per system. Emits: system_start, flow_start, step,
    flow_done, system_done, run_done."""
    from playwright.async_api import async_playwright

    from engine.engine import ReplayEngine
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.probe import probe
    from engine.recovery import dismiss_known_popups
    from engine.schema import Flow
    from engine.sections import CoreBasisSection, SectionResult, build_report

    reg = Registry.load(SYSTEMS_PATH)
    creds = EnvCredentialProvider()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for sid in req.sids:
            sys_entry = reg.get(sid)
            await emit(run_id, {"type": "system_start", "sid": sid})
            ctx = await browser.new_context()
            page = await ctx.new_page()
            try:
                login = BasicLoginPlugin(  # TODO: plugin lookup by sys_entry.auth
                    SystemTarget(sid, sys_entry.webgui_url, sys_entry.client), creds)
                await login.login(page)
                pr = await probe(page)
                await emit(run_id, {"type": "probe", "sid": sid,
                                    "mode": pr.mode, "info": pr.session_info})
                engine = ReplayEngine(page, pr.mode, run_id, sid,
                                      Path("run_artifacts") / run_id)
                flow_results = []
                for flow_path in req.flows:
                    flow = Flow(**json.loads(Path(flow_path).read_text()))
                    await emit(run_id, {"type": "flow_start", "sid": sid,
                                        "flow_id": flow.flow_id})
                    await dismiss_known_popups(page)
                    fr = await engine.run(flow)
                    store.save(fr)
                    flow_results.append(fr)
                    await emit(run_id, {
                        "type": "flow_done", "sid": sid, "flow_id": flow.flow_id,
                        "ok": fr.ok,
                        "failed_checks": sum(1 for c in fr.checks if not c.ok)})

                section_results: list[SectionResult] = []
                if flow_results:
                    section_results.append(SectionResult(
                        section_id="functional_sanity", title="Functional Sanity",
                        ok=all(fr.ok for fr in flow_results), flow_results=flow_results))

                if req.include_core_basis:
                    await emit(run_id, {"type": "core_basis_start", "sid": sid})
                    section = CoreBasisSection()
                    sr = await section.run(engine, run_id, sid)
                    anomaly_count = len((sr.v1_results or {}).get("anomalies", []))
                    store.save_core_basis(run_id, sid, sr.ok, anomaly_count,
                                           sr.notes, sr.v1_results or {})
                    section_results.append(sr)
                    await emit(run_id, {
                        "type": "core_basis_done", "sid": sid, "ok": sr.ok,
                        "anomaly_count": anomaly_count, "notes": sr.notes})

                if section_results:
                    await emit(run_id, {"type": "report_start", "sid": sid})
                    report_dir = str(Path("reports") / run_id)
                    paths = build_report(section_results, output_dir=report_dir)
                    store.save_report(run_id, sid, paths.get("docx"), paths.get("pdf"))
                    await emit(run_id, {"type": "report_ready", "sid": sid, **paths})
            except Exception as exc:  # noqa: BLE001
                await emit(run_id, {"type": "system_error", "sid": sid,
                                    "error": repr(exc)})
            finally:
                await ctx.close()
                await emit(run_id, {"type": "system_done", "sid": sid})
        await browser.close()
    await emit(run_id, {"type": "run_done"})
