"""FastAPI backend — the D-016 API surface the frontend needs.

Endpoints:
  GET  /api/systems               registry + last-run summary per system
  POST /api/run                   start a run (background) -> {run_id}
  GET  /api/events/{run_id}       SSE stream of per-step/per-flow events
  GET  /api/runs?sid=             recent run history (RunStore)

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
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from engine.registry import EnvCredentialProvider, Registry
from engine.store import RunStore

app = FastAPI(title="Claude Monitoring Tool")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"],
                   allow_methods=["*"], allow_headers=["*"])

store = RunStore("runs.db")
_event_queues: dict[str, asyncio.Queue] = {}


class RunRequest(BaseModel):
    sids: list[str]
    flows: list[str]                 # paths under flows/
    dataset: str | None = None


async def emit(run_id: str, event: dict) -> None:
    q = _event_queues.get(run_id)
    if q:
        await q.put(event)


@app.get("/api/systems")
def systems() -> list[dict]:
    reg = Registry.load("systems.json")
    out = []
    for s in reg.systems:
        recent = store.recent(sid=s.sid, limit=1)
        out.append({**s.model_dump(),
                    "last_run": recent[0] if recent else None})
    return out


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

    reg = Registry.load("systems.json")
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
                for flow_path in req.flows:
                    flow = Flow(**json.loads(Path(flow_path).read_text()))
                    await emit(run_id, {"type": "flow_start", "sid": sid,
                                        "flow_id": flow.flow_id})
                    await dismiss_known_popups(page)
                    fr = await engine.run(flow)
                    store.save(fr)
                    await emit(run_id, {
                        "type": "flow_done", "sid": sid, "flow_id": flow.flow_id,
                        "ok": fr.ok,
                        "failed_checks": sum(1 for c in fr.checks if not c.ok)})
            except Exception as exc:  # noqa: BLE001
                await emit(run_id, {"type": "system_error", "sid": sid,
                                    "error": repr(exc)})
            finally:
                await ctx.close()
                await emit(run_id, {"type": "system_done", "sid": sid})
        await browser.close()
    await emit(run_id, {"type": "run_done"})
