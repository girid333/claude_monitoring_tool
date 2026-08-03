"""D-044 Phase A — SAP GUI bridge-agent SANDBOX.

Per Giri's explicit instruction, this whole feature is built as an
**isolated, additive section**, not merged into the existing WebGUI/Core
BASIS pipeline:

  - Nothing in `engine/sections.py`, `engine/resolvers/dom.py`,
    `engine/recorder/recorder.py`, `engine/engine.py`, or `main.py`'s
    existing `_execute_run_inner` run path is imported BY this module or
    modified because of it.
  - This module owns its own state (agents, pairing codes, jobs, saved
    SAPGUI flows) and exposes its own `APIRouter`. `main.py` mounts it
    with a single `app.include_router(sapgui_router)` line — everything
    else needed for the sandbox lives here.
  - SAPGUI flows are saved under a SEPARATE directory (`SAPGUI_FLOWS_DIR`,
    default `flows_sapgui/`) so they never appear in the existing
    Landscape flow picker (`/api/flows`) and can never accidentally get
    selected into a normal WebGUI run.
  - If this sandbox doesn't pan out, deleting this file + its one mount
    line + the frontend's `/sapgui` page removes the feature cleanly.

Architecture recap (see design/sapgui-agent-plan.md for the full writeup):
  - A Windows "bridge agent" (agent/sapgui_bridge_agent.py) connects
    OUTBOUND to this server over WebSocket (`WS /api/sapgui/agent/ws`) —
    the container never needs to reach the workstation.
  - Pairing: the UI requests a one-time code; the agent is launched once
    with that code and exchanges it for a session on the socket.
  - Record/replay both work by the server dispatching a small JSON job to
    an agent and receiving `step` / `screenshot` / `flow_saved` /
    `step_result` / `replay_done` messages back, fanned out to whichever
    HTTP client is watching via `GET /api/sapgui/events/{agent_id}/{job_id}`
    (plain SSE, same pattern as the existing `/api/events/{run_id}`).
  - The COM/SAP-GUI-scripting layer only exists on the Windows agent; this
    module and its E2E tests never touch it directly, which is what makes
    the whole protocol testable on Linux with a fake agent script.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

SAPGUI_FLOWS_DIR = "flows_sapgui"
PAIRING_TTL_S = 300


def _flows_dir() -> Path:
    import os
    return Path(os.environ.get("SAPGUI_FLOWS_DIR", SAPGUI_FLOWS_DIR))


def _safe_flow_id(flow_id: str) -> None:
    if "/" in flow_id or "\\" in flow_id or flow_id.startswith("."):
        raise HTTPException(400, "invalid flow_id")


# ── Agent registry / pairing ─────────────────────────────────────────────

@dataclass
class PendingPairing:
    code: str
    created_at: float


@dataclass
class Agent:
    agent_id: str
    ws: WebSocket
    hostname: str = ""
    username: str = ""
    sap_gui_version: str = ""
    scripting_enabled: Optional[bool] = None
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    job_queues: dict[str, asyncio.Queue] = field(default_factory=dict)


class AgentHub:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._pending: dict[str, PendingPairing] = {}

    def new_pairing_code(self) -> str:
        code = secrets.token_hex(4)
        self._pending[code] = PendingPairing(code, time.time())
        return code

    def consume_pairing(self, code: str) -> bool:
        p = self._pending.pop(code, None)
        if not p:
            return False
        return (time.time() - p.created_at) <= PAIRING_TTL_S

    def register(self, ws: WebSocket, info: dict) -> Agent:
        agent_id = secrets.token_hex(6)
        agent = Agent(agent_id=agent_id, ws=ws,
                       hostname=info.get("hostname", ""),
                       username=info.get("username", ""),
                       sap_gui_version=info.get("sap_gui_version", ""),
                       scripting_enabled=info.get("scripting_enabled"))
        self._agents[agent_id] = agent
        return agent

    def unregister(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def list_agents(self) -> list[dict]:
        now = time.time()
        return [{
            "agent_id": a.agent_id, "hostname": a.hostname, "username": a.username,
            "sap_gui_version": a.sap_gui_version, "scripting_enabled": a.scripting_enabled,
            "connected_seconds": round(now - a.connected_at),
            "last_seen_seconds": round(now - a.last_seen),
        } for a in self._agents.values()]

    def get(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    async def send_job(self, agent_id: str, job: dict) -> str:
        agent = self._agents.get(agent_id)
        if not agent:
            raise KeyError(agent_id)
        job_id = job.setdefault("job_id", secrets.token_hex(6))
        agent.job_queues[job_id] = asyncio.Queue()
        await agent.ws.send_text(json.dumps(job))
        return job_id

    def queue_for(self, agent_id: str, job_id: str) -> asyncio.Queue | None:
        agent = self._agents.get(agent_id)
        if not agent:
            return None
        return agent.job_queues.get(job_id)

    async def on_agent_message(self, agent: Agent, msg: dict) -> None:
        agent.last_seen = time.time()
        job_id = msg.get("job_id")
        if job_id and job_id in agent.job_queues:
            await agent.job_queues[job_id].put(msg)


hub = AgentHub()

router = APIRouter(prefix="/api/sapgui", tags=["sapgui-sandbox"])


# ── Pairing + agent connection ───────────────────────────────────────────

@router.post("/pair/start")
def pair_start() -> dict:
    """UI calls this to get a one-time code; give it to the bridge agent
    (`sapgui_bridge_agent.py --server ... --code XXXX`) within 5 minutes."""
    return {"code": hub.new_pairing_code(), "ttl_seconds": PAIRING_TTL_S}


@router.get("/agents")
def list_agents() -> list[dict]:
    return hub.list_agents()


@router.websocket("/agent/ws")
async def agent_ws(ws: WebSocket) -> None:
    await ws.accept()
    agent: Agent | None = None
    try:
        hello_raw = await ws.receive_text()
        hello = json.loads(hello_raw)
        if hello.get("kind") != "hello" or not hub.consume_pairing(hello.get("code", "")):
            await ws.send_text(json.dumps({"kind": "error", "message": "invalid or expired pairing code"}))
            await ws.close()
            return
        agent = hub.register(ws, hello.get("info", {}))
        await ws.send_text(json.dumps({"kind": "welcome", "agent_id": agent.agent_id}))
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("kind") == "heartbeat":
                agent.last_seen = time.time()
                continue
            await hub.on_agent_message(agent, msg)
    except WebSocketDisconnect:
        pass
    finally:
        if agent is not None:
            hub.unregister(agent.agent_id)


@router.get("/events/{agent_id}/{job_id}")
async def job_events(agent_id: str, job_id: str) -> StreamingResponse:
    """SSE stream of one job's live messages (steps while recording,
    step_result/screenshot while replaying) — same pattern as the
    existing `/api/events/{run_id}`, kept as its own function so nothing
    about that endpoint changes."""
    q = hub.queue_for(agent_id, job_id)
    if q is None:
        raise HTTPException(404, "unknown agent_id/job_id")

    async def stream():
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("kind") in ("record_stopped", "replay_done", "error"):
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Record ────────────────────────────────────────────────────────────────

class RecordStartRequest(BaseModel):
    agent_id: str
    tcode: str = ""
    sid: str = ""


@router.post("/record/start")
async def record_start(req: RecordStartRequest) -> dict:
    try:
        job_id = await hub.send_job(req.agent_id, {
            "kind": "record_start", "tcode": req.tcode.strip(), "sid": req.sid.strip(),
        })
    except KeyError:
        raise HTTPException(404, "agent not connected")
    return {"agent_id": req.agent_id, "job_id": job_id}


class RecordStopRequest(BaseModel):
    agent_id: str
    job_id: str
    flow_id: str
    description: str = ""


@router.post("/record/stop")
async def record_stop(req: RecordStopRequest) -> dict:
    """Tells the agent to stop; the agent's `flow_saved` message (carrying
    the recorded steps) arrives on the job's event queue and gets saved to
    the SANDBOX flow dir here — never into the existing FLOWS_DIR."""
    _safe_flow_id(req.flow_id)
    try:
        await hub.send_job(req.agent_id, {
            "kind": "record_stop", "job_id": req.job_id,
        })
    except KeyError:
        raise HTTPException(404, "agent not connected")

    q = hub.queue_for(req.agent_id, req.job_id)
    if q is None:
        raise HTTPException(404, "unknown job")
    try:
        msg = await asyncio.wait_for(q.get(), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(504, "agent did not respond with recorded steps in time")
    if msg.get("kind") != "flow_saved":
        raise HTTPException(500, f"unexpected agent response: {msg.get('kind')}")

    steps = msg.get("steps", [])
    d = _flows_dir()
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "flow_id": req.flow_id,
        "description": req.description,
        "applies_to": [msg.get("sid")] if msg.get("sid") else [],
        "engine": "sapgui",
        "recorded_by_agent": req.agent_id,
        "steps": steps,
    }
    (d / f"{req.flow_id}.json").write_text(json.dumps(data, indent=2))
    return {"flow_id": req.flow_id, "step_count": len(steps), "path": str(d / f"{req.flow_id}.json")}


# ── Flow library (sandbox-only, separate directory) ─────────────────────

@router.get("/flows")
def list_flows() -> list[dict]:
    out = []
    d = _flows_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:  # noqa: BLE001,PERF203
            continue
        steps = data.get("steps", [])
        out.append({
            "flow_id": data.get("flow_id", p.stem),
            "description": data.get("description", ""),
            "step_count": len(steps),
            "applies_to": data.get("applies_to", []),
            "recorded_by_agent": data.get("recorded_by_agent"),
        })
    return out


def _describe_sapgui_steps(steps: list[dict]) -> list[dict]:
    """Human-readable render for sandbox steps. Deliberately separate from
    (and structurally similar to) `engine.recorder.recorder.describe_steps`
    rather than importing it, since sapgui steps are plain dicts keyed by
    `sid` (SAP GUI scripting id) rather than the WebGUI `Target`/`Step`
    Pydantic models — reusing that function would mean either changing its
    signature (touching the existing recorder module) or coercing sandbox
    data into the WebGUI schema. Keeping this tiny and separate is the
    safer, sandbox-appropriate choice."""
    out = []
    for i, s in enumerate(steps):
        act = s.get("act", "?")
        sid = s.get("sid", "")
        label = s.get("label") or sid or "—"
        value = s.get("value")
        desc = f"{act} — {label}"
        if value is not None:
            desc += f" = {value!r}"
        out.append({"index": i, "act": act, "sid": sid, "label": label,
                     "value": value, "description": desc})
    return out


@router.get("/flows/{flow_id}")
def flow_detail(flow_id: str) -> dict:
    _safe_flow_id(flow_id)
    p = _flows_dir() / f"{flow_id}.json"
    if not p.exists():
        raise HTTPException(404, "flow not found")
    data = json.loads(p.read_text())
    return {
        "flow_id": data.get("flow_id", flow_id),
        "description": data.get("description", ""),
        "applies_to": data.get("applies_to", []),
        "step_count": len(data.get("steps", [])),
        "steps": _describe_sapgui_steps(data.get("steps", [])),
    }


@router.delete("/flows/{flow_id}")
def delete_flow(flow_id: str) -> dict:
    _safe_flow_id(flow_id)
    p = _flows_dir() / f"{flow_id}.json"
    if not p.exists():
        raise HTTPException(404, "flow not found")
    p.unlink()
    return {"deleted": True, "flow_id": flow_id}


# ── Replay ───────────────────────────────────────────────────────────────

class ReplayRequest(BaseModel):
    agent_id: str
    flow_id: str
    overrides: dict[str, Any] = {}


@router.post("/replay")
async def replay(req: ReplayRequest) -> dict:
    _safe_flow_id(req.flow_id)
    p = _flows_dir() / f"{req.flow_id}.json"
    if not p.exists():
        raise HTTPException(404, "flow not found")
    data = json.loads(p.read_text())
    try:
        job_id = await hub.send_job(req.agent_id, {
            "kind": "replay", "flow": data, "overrides": req.overrides,
        })
    except KeyError:
        raise HTTPException(404, "agent not connected")
    return {"agent_id": req.agent_id, "job_id": job_id}
