"""Recorder session lifecycle (Phase 2 UI, D-004/D-016/D-020).

`recorder/recorder.py` is the capture mechanism (inject JS, coalesce events
into a Flow) but has no notion of a browser session or an HTTP-reachable
lifecycle — this module supplies both, for the Recorder page's start/stop
controls.

D-004: recording requires a scripting-enabled system and is an interactive,
human-driven activity — Giri watches the WebGUI window and clicks through
the flow himself. So unlike the bulk-run engine (headless, D-010 stateless,
run_id+sid keyed), a recording session:
  - launches a HEADED browser (headless=False) — a real window opens on
    whatever machine runs `uvicorn main:app`, matching the README's "run
    locally" model. This cannot work against a remote/headless deployment
    without VNC or similar; that's a known limitation, not a bug.
  - is in-process, keyed by a random recording_id, not persisted to
    RunStore/SQLite. If the backend restarts mid-recording, the recording is
    lost — acceptable because a half-finished interactive recording isn't a
    result worth persisting; the human just starts over.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import Playwright, async_playwright

from .login.base import SystemTarget
from .login.basic import BasicLoginPlugin
from .recorder.recorder import Recorder, save_flow
from .registry import Registry, RegistryCredentialProvider


@dataclass
class RecordingSession:
    recording_id: str
    sid: str
    pw: Playwright
    browser: Any
    context: Any
    page: Any
    recorder: Recorder
    status: str = "recording"   # recording | stopped | cancelled | error
    error: str | None = None


_sessions: dict[str, RecordingSession] = {}


async def start_recording(sid: str, registry_path: str | Path = "systems.json") -> RecordingSession:
    reg = Registry.load(registry_path)
    sys_entry = reg.get(sid)                    # raises KeyError if unknown sid
    creds = RegistryCredentialProvider(reg)     # D-025: registry sap_credential, env vars fallback

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context()
    page = await context.new_page()

    try:
        login = BasicLoginPlugin(  # TODO: plugin lookup by sys_entry.auth, same gap as main.py
            SystemTarget(sid, sys_entry.webgui_url, sys_entry.client), creds)
        await login.login(page)
    except Exception:
        await _close(pw, browser, context)
        raise

    recorder = Recorder(page)
    await recorder.start()

    recording_id = uuid.uuid4().hex[:12]
    session = RecordingSession(recording_id=recording_id, sid=sid, pw=pw,
                                browser=browser, context=context, page=page,
                                recorder=recorder)
    _sessions[recording_id] = session
    return session


def get_session(recording_id: str) -> RecordingSession:
    if recording_id not in _sessions:
        raise KeyError(recording_id)
    return _sessions[recording_id]


def status(recording_id: str) -> dict:
    s = get_session(recording_id)
    events = s.recorder.events
    return {
        "recording_id": s.recording_id, "sid": s.sid, "status": s.status,
        "error": s.error, "event_count": len(events),
        "last_events": events[-5:],
    }


def list_active() -> list[dict]:
    return [status(rid) for rid in list(_sessions.keys())]


async def stop_recording(recording_id: str, flow_id: str, description: str = "",
                          flows_dir: str | Path = "flows") -> dict:
    s = get_session(recording_id)
    flow = await s.recorder.stop(flow_id=flow_id, description=description)
    path = save_flow(flow, flows_dir)
    await _close(s.pw, s.browser, s.context)
    s.status = "stopped"
    _sessions.pop(recording_id, None)
    return {"flow_id": flow.flow_id, "step_count": len(flow.steps), "path": str(path)}


async def cancel_recording(recording_id: str) -> None:
    s = get_session(recording_id)
    await _close(s.pw, s.browser, s.context)
    s.status = "cancelled"
    _sessions.pop(recording_id, None)


async def _close(pw: Playwright, browser: Any, context: Any) -> None:
    for closeable in (context, browser):
        try:
            await closeable.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        pass
