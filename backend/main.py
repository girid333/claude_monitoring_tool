"""FastAPI backend — the D-016 API surface the frontend needs.

Endpoints:
  GET    /api/systems                     registry + last-run summary + credential status
  POST   /api/systems                     create a system (D-021; SSH creds encrypted, D-023)
  PUT    /api/systems/{sid}               update a system
  DELETE /api/systems/{sid}               remove a system
  GET    /api/systems/{sid}/credentials   presence-only credential check (SAP env-var hint + SSH status)
  POST /api/run                           start a run (background) -> {run_id}
  GET  /api/events/{run_id}               SSE stream of per-step/per-flow/report events
  GET  /api/runs?sid=                     recent flow run history (RunStore)
  GET  /api/runs/core-basis?sid=          recent Core BASIS section run history (D-017)
  GET  /api/reports?sid=                  recent generated Word/PDF reports (D-014)
  GET  /api/reports/{run_id}/{sid}/download/{docx|pdf}   fetch a generated report file
  GET  /api/reports/{run_id}/download/zip  all reports for a run bundled (v1's "Download All Reports (ZIP)", D-027)
  DELETE /api/reports/{run_id}/{sid}      delete a report (row + files on disk, D-032)
  GET  /api/runs/core-basis/{run_id}/{sid}  per-T-code anomaly summary for one run (D-032)
  POST /api/recorder/start                {sid} -> headless browser + screencast recording (D-033)
  GET  /api/recorder/frame/{recording_id}   latest screencast frame (base64 JPEG, D-033)
  POST /api/recorder/input                 forward mouse/keyboard into that browser (D-033)
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
import re
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from engine import crypto
from engine.registry import (
    EnvCredentialProvider,
    Registry,
    RegistryCredentialProvider,
    SapCredential,
    ServerCredential,
    ServerEntry,
    SystemEntry,
    credential_status,
)
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


class ServerCredentialIn(BaseModel):
    """D-023: request-side shape for an SSH credential. `password` is
    plaintext, accepted ONLY here (never persisted as-is, never echoed
    back) — encrypted into `ServerCredential.password_enc` by
    `_in_to_credential()` before anything touches disk. Leaving `password`
    blank on an edit means "keep whatever's already stored", so the
    frontend never needs to know or re-send the real secret."""

    auth_method: str = "password"
    username: str = ""
    password: str = ""
    key_path: str = ""


class ServerEntryIn(BaseModel):
    hostname: str
    credential: ServerCredentialIn | None = None


class SapCredentialIn(BaseModel):
    """D-025: request-side shape for the SAP WebGUI login credential.
    Same blank-password-means-keep-existing convention as
    ServerCredentialIn. No auth_method/key_path — SAP logon is always
    username+password."""

    username: str = ""
    password: str = ""


class SystemIn(BaseModel):
    """D-021/D-023/D-025: request body for POST/PUT /api/systems. Same
    fields as SystemEntry except credentials arrive as plaintext-in
    (ServerCredentialIn/SapCredentialIn) rather than the encrypted-at-rest
    shape the registry actually stores."""

    sid: str
    system_name: str = ""   # D-027: v1-parity display name
    webgui_url: str
    client: str
    release_tag: str = "UNKNOWN"
    auth: str = "basic"
    tags: list[str] = []
    rfc_enrichment: bool = False
    hostname: str = ""
    instance_number: str = "00"
    url_checks: list[str] = []
    servers: list[ServerEntryIn] = []
    db_hosts: list[ServerEntryIn] = []
    ssh_auth_method: str = "password"
    ssh_credential: ServerCredentialIn = ServerCredentialIn()
    sap_credential: SapCredentialIn = SapCredentialIn()


def _in_to_credential(cred_in: ServerCredentialIn | None,
                       existing: ServerCredential | None) -> ServerCredential | None:
    """D-023: encrypt a fresh plaintext password, or keep the previously
    stored ciphertext when the incoming password is blank (edit-without-
    changing-the-password case)."""
    if cred_in is None:
        return None
    password_enc = (crypto.encrypt(cred_in.password) if cred_in.password
                     else (existing.password_enc if existing else ""))
    return ServerCredential(auth_method=cred_in.auth_method, username=cred_in.username,
                             password_enc=password_enc, key_path=cred_in.key_path)


def _existing_host_credential(existing: SystemEntry | None, pool: str, hostname: str) -> ServerCredential | None:
    if existing is None:
        return None
    for h in (existing.servers if pool == "servers" else existing.db_hosts):
        if h.hostname == hostname:
            return h.credential
    return None


def _in_to_sap_credential(cred_in: SapCredentialIn, existing: SapCredential | None) -> SapCredential:
    """D-025: same blank-password-means-keep-existing rule as
    _in_to_credential(), specialized for the no-auth_method SAP shape."""
    password_enc = (crypto.encrypt(cred_in.password) if cred_in.password
                     else (existing.password_enc if existing else ""))
    return SapCredential(username=cred_in.username, password_enc=password_enc)


def _to_stored(sys_in: SystemIn, existing: SystemEntry | None) -> SystemEntry:
    """D-021/D-023/D-025: SystemIn (plaintext credentials) -> SystemEntry
    (encrypted, the only shape Registry.save() ever writes)."""

    def build_pool(entries_in: list[ServerEntryIn], pool: str) -> list[ServerEntry]:
        out = []
        for e in entries_in:
            existing_cred = _existing_host_credential(existing, pool, e.hostname)
            out.append(ServerEntry(hostname=e.hostname,
                                    credential=_in_to_credential(e.credential, existing_cred)))
        return out

    ssh_credential = _in_to_credential(
        sys_in.ssh_credential, existing.ssh_credential if existing else None) or ServerCredential()
    sap_credential = _in_to_sap_credential(
        sys_in.sap_credential, existing.sap_credential if existing else None)

    return SystemEntry(
        sid=sys_in.sid, system_name=sys_in.system_name,
        webgui_url=sys_in.webgui_url, client=sys_in.client,
        release_tag=sys_in.release_tag, auth=sys_in.auth, tags=sys_in.tags,
        rfc_enrichment=sys_in.rfc_enrichment, hostname=sys_in.hostname,
        instance_number=sys_in.instance_number, url_checks=sys_in.url_checks,
        servers=build_pool(sys_in.servers, "servers"),
        db_hosts=build_pool(sys_in.db_hosts, "db_hosts"),
        ssh_auth_method=sys_in.ssh_auth_method, ssh_credential=ssh_credential,
        sap_credential=sap_credential,
    )


def _cred_out(cred: ServerCredential | None) -> dict | None:
    """D-023: never return password_enc — only whether a password is set."""
    if cred is None:
        return None
    return {"auth_method": cred.auth_method, "username": cred.username,
            "key_path": cred.key_path, "password_set": bool(cred.password_enc)}


def _sap_cred_out(cred: SapCredential) -> dict:
    """D-025: same redaction rule as _cred_out(), no-auth_method shape."""
    return {"username": cred.username, "password_set": bool(cred.password_enc)}


def _system_out(s: SystemEntry) -> dict:
    d = s.model_dump(exclude={"servers", "db_hosts", "ssh_credential", "sap_credential"})
    d["sap_credential"] = _sap_cred_out(s.sap_credential)
    d["servers"] = [{"hostname": e.hostname, "credential": _cred_out(e.credential)} for e in s.servers]
    d["db_hosts"] = [{"hostname": e.hostname, "credential": _cred_out(e.credential)} for e in s.db_hosts]
    d["ssh_credential"] = _cred_out(s.ssh_credential)
    return d


class RunRequest(BaseModel):
    sids: list[str]
    flows: list[str] = []            # paths under flows/
    dataset: str | None = None
    include_core_basis: bool = False  # D-017: also run v1's 23-T-code sweep


class RecorderStartRequest(BaseModel):
    sid: str
    tcode: str = ""     # D-034: open this transaction first, record it as step 1


class RecorderStopRequest(BaseModel):
    recording_id: str
    flow_id: str
    description: str = ""


class RecorderCancelRequest(BaseModel):
    recording_id: str


def _parse_tcode(msg: str) -> str | None:
    """D-031: pull the T-code out of v1's own progress strings (e.g.
    'Capturing screenshot for SM50...', 'Extracting data for ST22 - Dumps').
    Returns None for general messages like 'Logging into SAP WebGUI...'."""
    m = re.search(r"(?:for|Running|Extracting data for)\s+([A-Z][A-Z0-9]{1,7})\b", msg)
    return m.group(1) if m else None


async def emit(run_id: str, event: dict) -> None:
    q = _event_queues.get(run_id)
    if q:
        await q.put(event)


def emit_sync(run_id: str, event: dict) -> None:
    """D-032: ordered, synchronous enqueue for callbacks that cannot await.

    The queue is unbounded, so put_nowait never blocks or fails. Using this
    instead of `loop.create_task(emit(...))` is what guarantees ORDERING:
    scheduled tasks flush whenever the loop next yields, so v1's final
    progress messages could land AFTER core_basis_done and re-mark an
    already-finished T-code as 'running' — which is exactly why the last
    T-code stayed blue at 22/23 instead of turning green."""
    q = _event_queues.get(run_id)
    if q:
        q.put_nowait(event)


@app.get("/api/systems")
def systems() -> list[dict]:
    reg = Registry.load(SYSTEMS_PATH)
    out = []
    for s in reg.systems:
        recent = store.recent(sid=s.sid, limit=1)
        out.append({**_system_out(s),
                    "last_run": recent[0] if recent else None,
                    "credentials": credential_status(s)})
    return out


@app.post("/api/systems")
def create_system(entry: SystemIn) -> dict:
    """D-021/D-023: replaces hand-editing systems.json. SAP WebGUI login
    credentials are still never accepted here (D-018) — set
    SAP_<SID>_USER/PASSWORD as env vars. SSH credentials for app/dialog
    servers and DB hosts ARE accepted here (D-023, supersedes D-018 for
    this one credential class) and are encrypted (engine/crypto.py) before
    Registry.save() ever writes them to disk."""
    reg = Registry.load(SYSTEMS_PATH)
    try:
        reg.get(entry.sid)
        raise HTTPException(409, f"system {entry.sid!r} already exists — use PUT to update it")
    except KeyError:
        pass
    stored = _to_stored(entry, existing=None)
    reg.upsert(stored)
    reg.save(SYSTEMS_PATH)
    return _system_out(stored)


@app.put("/api/systems/{sid}")
def update_system(sid: str, entry: SystemIn) -> dict:
    if entry.sid != sid:
        raise HTTPException(400, "body sid must match the URL sid")
    reg = Registry.load(SYSTEMS_PATH)
    try:
        existing = reg.get(sid)
    except KeyError:
        raise HTTPException(404, f"unknown sid {sid!r}")
    stored = _to_stored(entry, existing=existing)
    reg.upsert(stored)
    reg.save(SYSTEMS_PATH)
    return _system_out(stored)


@app.delete("/api/systems/{sid}")
def delete_system(sid: str) -> dict:
    reg = Registry.load(SYSTEMS_PATH)
    if not reg.remove(sid):
        raise HTTPException(404, f"unknown sid {sid!r}")
    reg.save(SYSTEMS_PATH)
    return {"deleted": sid}


@app.get("/api/systems/{sid}/credentials")
def system_credentials(sid: str) -> dict:
    """Presence-only check (never returns secret values). SAP WebGUI login
    status is env-var-based (D-018); SSH status is registry-based (D-023) —
    see credential_status()."""
    reg = Registry.load(SYSTEMS_PATH)
    try:
        entry = reg.get(sid)
    except KeyError:
        raise HTTPException(404, f"unknown sid {sid!r}")
    status = credential_status(entry)
    return {
        "sid": sid,
        **status,
        "hint": {"sap": f"export SAP_{sid}_USER=... SAP_{sid}_PASSWORD=..."},
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
            # D-029: heartbeat every 15s — a Core BASIS sweep can go minutes
            # between real events, and idle SSE connections get dropped by
            # some proxies/browsers. SSE comments (lines starting ":") are
            # ignored by EventSource but keep the connection alive.
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
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


@app.get("/api/runs/core-basis/{run_id}/{sid}")
def core_basis_detail(run_id: str, sid: str) -> dict:
    """D-032: per-run anomaly summary for the expandable Runs row.

    v1's raw results dict is large (screenshots, full data payloads); this
    returns only what a human triaging a run needs: per-T-code anomaly
    lists plus the external (SSH/URL) check outcomes."""
    row = store.get_core_basis(run_id, sid)
    if row is None:
        raise HTTPException(404, "run not found")
    payload = row.get("payload") or {}
    tcodes = [
        {"tcode": t.get("tcode"), "name": t.get("name"),
         "anomalies": t.get("anomalies") or [],
         "summary": t.get("analysis_summary") or ""}
        for t in payload.get("tcodes", [])
    ]
    return {
        "run_id": row["run_id"], "sid": row["sid"], "ok": row["ok"],
        "started_at": row["started_at"], "notes": row["notes"],
        "anomaly_count": row["anomaly_count"],
        "tcodes_with_anomalies": [t for t in tcodes if t["anomalies"]],
        "tcodes_clean": [t["tcode"] for t in tcodes if not t["anomalies"]],
        "external_checks": payload.get("external_checks", []),
    }


@app.get("/api/reports")
def reports(sid: str | None = None, limit: int = 20) -> list[dict]:
    return store.recent_reports(sid=sid, limit=limit)


@app.delete("/api/reports/{run_id}/{sid}")
def delete_report(run_id: str, sid: str, label: str | None = None) -> dict:
    """D-032/D-035: delete a generated report (DB row + the docx/pdf on
    disk). `?label=` targets one document of a run; omitted deletes all of
    that run+sid's reports."""
    paths = store.delete_report(run_id, sid, label)
    if not paths:
        raise HTTPException(404, "report not found")
    removed = []
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
            removed.append(p)
        except OSError as exc:  # noqa: PERF203 - report per-file failures
            removed.append(f"{p} (could not delete: {exc})")
    return {"deleted": True, "files": removed}


@app.get("/api/reports/{run_id}/download/zip")
def download_reports_zip(run_id: str) -> FileResponse:
    """D-027: v1-parity 'Download All Reports (ZIP)' — every docx/pdf this
    run generated, across all its systems, in one archive. v1 bundled the
    whole batch's output the same way (`/api/download/zip`)."""
    import zipfile

    rows = [r for r in store.recent_reports(limit=500) if r["run_id"] == run_id]
    files = [Path(r[k]) for r in rows for k in ("docx_path", "pdf_path")
             if r.get(k) and Path(r[k]).exists()]
    if not files:
        raise HTTPException(404, "no report files found for this run")
    zip_path = Path("reports") / f"{run_id}_all_reports.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in files:
            zf.write(f, f.name)
    return FileResponse(zip_path, filename=f"SAP_Reports_{run_id}.zip")


@app.get("/api/reports/{run_id}/{sid}/download/{kind}")
def download_report(run_id: str, sid: str, kind: str, label: str | None = None) -> FileResponse:
    if kind not in ("docx", "pdf"):
        raise HTTPException(400, "kind must be docx or pdf")
    rows = [r for r in store.recent_reports(sid=sid, limit=200) if r["run_id"] == run_id]
    if label is not None:  # D-035: a run has several documents now
        rows = [r for r in rows if r.get("label") == label]
    if not rows or not rows[0].get(f"{kind}_path"):
        raise HTTPException(404, "report not found")
    path = rows[0][f"{kind}_path"]
    if not Path(path).exists():
        raise HTTPException(404, "report file missing on disk")
    return FileResponse(path, filename=Path(path).name)


# ── Recorder (Phase 2, D-033) ────────────────────────────────────────────
# The browser is HEADLESS and server-side; the human drives it remotely
# through streamed frames (CDP screencast) with input forwarded back. This
# works in containers — the old headed-browser design did not, which is why
# recording used to be gated behind a 501 here.

@app.post("/api/recorder/start")
async def recorder_start(req: RecorderStartRequest) -> dict:
    from engine.recorder_service import start_recording
    try:
        session = await start_recording(req.sid, registry_path=SYSTEMS_PATH,  # D-028
                                         tcode=req.tcode.strip())             # D-034
    except KeyError:
        raise HTTPException(404, f"unknown sid {req.sid!r} in systems.json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not start recording: {exc}")
    from engine.recorder_service import VIEWPORT
    return {"recording_id": session.recording_id, "sid": session.sid,
            "viewport": VIEWPORT}


@app.get("/api/recorder/frame/{recording_id}")
async def recorder_frame(recording_id: str) -> dict:
    """D-033: latest screencast frame as base64 JPEG. The UI polls this (a
    WebSocket would be marginally cheaper, but polling survives the proxies
    and reverse-proxies people put in front of this without extra config)."""
    from engine.recorder_service import screenshot
    try:
        return {"frame": await screenshot(recording_id)}
    except KeyError:
        raise HTTPException(404, "unknown recording_id")


class RecorderInputRequest(BaseModel):
    recording_id: str
    events: list[dict]      # batched: [{kind:"click",x,y} | {kind:"text",text} | ...]


@app.post("/api/recorder/input")
async def recorder_input(req: RecorderInputRequest) -> dict:
    """D-033: forward user input into the server-side browser."""
    from engine.recorder_service import dispatch_input
    try:
        for ev in req.events:
            await dispatch_input(req.recording_id, ev)
    except KeyError:
        raise HTTPException(404, "unknown recording_id")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"input failed: {exc}")
    return {"ok": True}


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


class RecorderCaptureRequest(BaseModel):
    recording_id: str
    note: str = ""
    pages: int = 0      # D-039: >0 = paginated capture (scroll page by page)


@app.post("/api/recorder/capture")
def recorder_capture(req: RecorderCaptureRequest) -> dict:
    """D-034/D-039: insert a screenshot-evidence step at this point."""
    from engine.recorder_service import add_capture_step
    try:
        return add_capture_step(req.recording_id, req.note, pages=req.pages)
    except KeyError:
        raise HTTPException(404, "unknown recording_id")


# ── Recorded flow library (D-034) ────────────────────────────────────────

FLOWS_DIR = os.environ.get("FLOWS_DIR", "flows")


@app.get("/api/flows")
def list_flows() -> list[dict]:
    """Saved recordings, for the 'which flow do I run?' picker on Landscape."""
    out = []
    d = Path(FLOWS_DIR)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:  # noqa: BLE001,PERF203 - skip unreadable/corrupt files
            continue
        steps = data.get("steps", [])
        out.append({
            "flow_id": data.get("flow_id", p.stem),
            "description": data.get("description", ""),
            "path": str(p),
            "step_count": len(steps),
            "tcodes": [s.get("value") for s in steps
                       if s.get("act") == "start_tcode" and s.get("value")],
            "has_capture": any(s.get("act") == "capture" for s in steps),
            "applies_to": data.get("applies_to", []),
        })
    return out


@app.delete("/api/flows/{flow_id}")
def delete_flow(flow_id: str) -> dict:
    # Guard against path traversal — flow_id comes straight off the URL.
    if "/" in flow_id or "\\" in flow_id or flow_id.startswith("."):
        raise HTTPException(400, "invalid flow_id")
    p = Path(FLOWS_DIR) / f"{flow_id}.json"
    if not p.exists():
        raise HTTPException(404, "flow not found")
    p.unlink()
    return {"deleted": True, "flow_id": flow_id}


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
    flow_done, system_done, run_done.

    D-028 robustness contract: `run_done` is ALWAYS emitted (finally),
    whatever fails — an unknown SID, a browser-launch failure, anything.
    Before this, a pre-loop failure (e.g. Playwright browsers not
    installed) or an unknown SID killed the background task silently and
    the frontend island spun forever with no error shown.
    """
    try:
        await _execute_run_inner(run_id, req)
    except Exception as exc:  # noqa: BLE001
        await emit(run_id, {"type": "run_error", "error": repr(exc)})
    finally:
        await emit(run_id, {"type": "run_done"})


async def _execute_run_inner(run_id: str, req: RunRequest) -> None:
    from playwright.async_api import async_playwright

    from engine.engine import ReplayEngine
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.probe import probe
    from engine.recovery import dismiss_known_popups
    from engine.schema import Flow, FlowRunResult
    from engine.sections import CoreBasisSection, SectionResult, build_reports

    reg = Registry.load(SYSTEMS_PATH)
    creds = RegistryCredentialProvider(reg)  # D-025: registry sap_credential first, env vars fallback

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for sid in req.sids:
            await emit(run_id, {"type": "system_start", "sid": sid})
            ctx = None
            try:
                sys_entry = reg.get(sid)   # D-028: unknown SID -> system_error, not a dead task
                ctx = await browser.new_context()
                page = await ctx.new_page()
                login = BasicLoginPlugin(  # TODO: plugin lookup by sys_entry.auth
                    SystemTarget(sid, sys_entry.webgui_url, sys_entry.client), creds)
                await login.login(page)
                pr = await probe(page)
                await emit(run_id, {"type": "probe", "sid": sid,
                                    "mode": pr.mode, "info": pr.session_info})
                engine = ReplayEngine(page, pr.mode, run_id, sid,
                                      Path("run_artifacts") / run_id,
                                      webgui_url=sys_entry.webgui_url,   # D-034
                                      client=sys_entry.client)
                flow_results = []
                for flow_path in req.flows:
                    # D-034: accept either a path ("flows/x.json") or a bare
                    # flow_id ("x") — the UI's picker sends flow_ids.
                    p = Path(flow_path)
                    if not p.exists():
                        p = Path(FLOWS_DIR) / f"{Path(flow_path).stem}.json"
                    if not p.exists():
                        await emit(run_id, {"type": "system_error", "sid": sid,
                                            "error": f"flow not found: {flow_path}"})
                        continue
                    # D-040: each selected flow gets its OWN try/except.
                    # Before this, a single flow that failed to even LOAD
                    # (malformed JSON, a schema mismatch from an older
                    # recording) raised out of this loop entirely — caught
                    # only by the per-SYSTEM try/except further down, which
                    # meant every flow after the broken one, AND the report
                    # generation step for flow(s) that already succeeded,
                    # never ran. Selecting N recordings must produce N
                    # documents (one per recording, error or not) — a single
                    # bad flow can no longer take its siblings down with it.
                    try:
                        flow = Flow(**json.loads(p.read_text()))
                        await emit(run_id, {"type": "flow_start", "sid": sid,
                                            "flow_id": flow.flow_id})
                        await dismiss_known_popups(page)
                        fr = await engine.run(flow)
                    except Exception as exc:  # noqa: BLE001
                        flow_id = Path(flow_path).stem
                        await emit(run_id, {"type": "system_error", "sid": sid,
                                            "error": f"flow {flow_id} failed to load/run: {exc!r}"})
                        fr = FlowRunResult(run_id=run_id, sid=sid, flow_id=flow_id,
                                           mode="dom", ok=False, error=repr(exc))
                    store.save(fr)
                    flow_results.append(fr)
                    await emit(run_id, {
                        "type": "flow_done", "sid": sid, "flow_id": fr.flow_id,
                        "ok": fr.ok,
                        "failed_checks": sum(1 for c in fr.checks if not c.ok)})

                section_results: list[SectionResult] = []
                if flow_results:
                    section_results.append(SectionResult(
                        section_id="functional_sanity", title="Functional Sanity",
                        ok=all(fr.ok for fr in flow_results), flow_results=flow_results))

                if req.include_core_basis:
                    section = CoreBasisSection(registry_path=SYSTEMS_PATH)  # D-028: honor env override
                    # D-031: send the full T-code checklist up-front so the UI
                    # can render every step as "pending" before work begins.
                    await emit(run_id, {"type": "core_basis_start", "sid": sid,
                                        "tcodes": section.tcode_list()})

                    def on_status(msg: str, _sid=sid) -> None:
                        # SYNC by contract — v1's take_screenshots() calls this
                        # without awaiting (see CoreBasisSection.run docstring).
                        # emit_sync (not create_task) keeps these ORDERED
                        # relative to core_basis_done — see D-032.
                        emit_sync(run_id, {
                            "type": "core_basis_progress", "sid": _sid,
                            "message": msg, "tcode": _parse_tcode(msg),
                            "level": "warn" if msg.lower().startswith("warning") else "info",
                        })

                    sr = await section.run(engine, run_id, sid, on_status=on_status)
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
                    # D-035: separate documents per kind (Core BASIS vs each
                    # recorded flow) instead of one combined file.
                    artifacts = build_reports(section_results, output_dir=report_dir)
                    for a in artifacts:
                        store.save_report(run_id, sid, a.get("docx"), a.get("pdf"),
                                           label=a.get("label", ""), kind=a.get("kind", ""))
                    await emit(run_id, {"type": "report_ready", "sid": sid,
                                        "reports": [{"label": a["label"], "kind": a["kind"],
                                                     "error": a.get("error")}
                                                     for a in artifacts]})
                    failed = [a["label"] for a in artifacts if a.get("error")]
                    if failed:
                        await emit(run_id, {"type": "system_error", "sid": sid,
                                            "error": f"report generation failed for: {', '.join(failed)}"})
            except Exception as exc:  # noqa: BLE001
                await emit(run_id, {"type": "system_error", "sid": sid,
                                    "error": repr(exc)})
            finally:
                if ctx is not None:
                    await ctx.close()
                await emit(run_id, {"type": "system_done", "sid": sid})
        await browser.close()
