"""Recorder session lifecycle — remote-controlled headless browser (D-033).

THE PROBLEM THIS SOLVES
The original design (D-004/D-020b) launched a HEADED Chromium so a human
could drive WebGUI by hand. That cannot work in the deployment actually in
use: containers have no display, so `/api/recorder/start` was gated behind a
501 and the whole record-and-replay feature was unreachable.

THE APPROACH
Keep the browser headless and server-side, and give the human a remote
control instead:
  * CDP `Page.startScreencast` streams JPEG frames of the real page.
  * The Recorder UI renders those frames and forwards mouse/keyboard events.
  * `Input.dispatchMouseEvent` / `Input.dispatchKeyEvent` replay them into
    the real browser, so SAP sees genuine user input.
  * The injected capture script (recorder/recorder.py) records every
    interaction with ranked DOM metadata as it happens.

Net effect: recording works in the same container that runs monitoring — no
display, no VNC, no X server.

Sessions stay in-process (keyed by recording_id) and are NOT persisted: a
half-finished interactive recording isn't a result worth keeping across a
backend restart (unchanged from D-020b).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Playwright, async_playwright

from .login.base import SystemTarget
from .login.basic import BasicLoginPlugin
from .recorder.recorder import Recorder, save_flow
from .registry import Registry, RegistryCredentialProvider

# Viewport of the server-side browser. Fixed so the UI can map click
# coordinates 1:1 without scaling maths on either side.
VIEWPORT = {"width": 1280, "height": 800}


@dataclass
class RecordingSession:
    recording_id: str
    sid: str
    pw: Playwright
    browser: Any
    context: Any
    page: Any
    cdp: Any                      # CDPSession
    recorder: Recorder
    status: str = "recording"     # recording | stopped | cancelled | error
    error: str | None = None
    last_frame: str | None = None            # base64 JPEG
    frame_listeners: list[Callable[[str], None]] = field(default_factory=list)
    # D-039: where the user last scrolled — paginated capture aims its wheel
    # at the same container rather than guessing the viewport centre.
    last_wheel: tuple[float, float] | None = None


_sessions: dict[str, RecordingSession] = {}


async def start_recording(sid: str,
                           registry_path: str | Path = "systems.json",
                           tcode: str = "") -> RecordingSession:
    """D-034: `tcode` (optional) navigates the session to that transaction
    before capture begins AND records it as the flow's first step, so a
    replay starts from the same screen the human did."""
    reg = Registry.load(registry_path)
    sys_entry = reg.get(sid)                    # raises KeyError if unknown sid
    creds = RegistryCredentialProvider(reg)     # D-025

    pw = await async_playwright().start()
    # D-033: headless — the human drives it remotely via screencast.
    browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(viewport=VIEWPORT)
    page = await context.new_page()

    try:
        login = BasicLoginPlugin(  # TODO: plugin lookup by sys_entry.auth
            SystemTarget(sid, sys_entry.webgui_url, sys_entry.client), creds)
        await login.login(page)
    except Exception:
        await _close(pw, browser, context)
        raise

    recorder = Recorder(page)
    await recorder.start()

    # D-034: jump to the requested T-code and make it step 1 of the flow.
    if tcode:
        from .tcode_nav import goto_tcode
        try:
            # Pause capture: goto_tcode drives the OK-code field itself, and
            # those are the tool's actions, not the user's (D-034).
            recorder.pause()
            await goto_tcode(page, tcode, webgui_url=sys_entry.webgui_url,
                              client=sys_entry.client)
            await page.wait_for_timeout(300)   # let the roundtrip settle
            recorder.resume()
            recorder.add_start_tcode(tcode)
            await recorder.refresh()   # new frames after navigation
        except Exception as exc:  # noqa: BLE001
            await _close(pw, browser, context)
            raise RuntimeError(f"could not open T-code {tcode}: {exc}") from exc

    cdp = await context.new_cdp_session(page)
    recording_id = uuid.uuid4().hex[:12]
    session = RecordingSession(recording_id=recording_id, sid=sid, pw=pw,
                                browser=browser, context=context, page=page,
                                cdp=cdp, recorder=recorder)

    def _on_frame(params: dict) -> None:
        session.last_frame = params.get("data")
        for cb in list(session.frame_listeners):
            try:
                cb(params.get("data", ""))
            except Exception:  # noqa: BLE001,PERF203
                continue
        # Acking is what makes the next frame arrive.
        asyncio.create_task(_ack(cdp, params.get("sessionId")))

    cdp.on("Page.screencastFrame", _on_frame)
    await cdp.send("Page.startScreencast", {
        "format": "jpeg", "quality": 60,
        "maxWidth": VIEWPORT["width"], "maxHeight": VIEWPORT["height"],
        "everyNthFrame": 1,
    })

    # Re-arm capture in frames created by SAP roundtrips.
    page.on("framenavigated", lambda _f: asyncio.create_task(_safe_refresh(recorder)))

    _sessions[recording_id] = session
    return session


async def _ack(cdp: Any, session_id: Any) -> None:
    if session_id is None:
        return
    try:
        await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
    except Exception:  # noqa: BLE001 - session may be closing
        pass


async def _safe_refresh(recorder: Recorder) -> None:
    try:
        await recorder.refresh()
    except Exception:  # noqa: BLE001
        pass


def get_session(recording_id: str) -> RecordingSession:
    if recording_id not in _sessions:
        raise KeyError(recording_id)
    return _sessions[recording_id]


def status(recording_id: str) -> dict:
    s = get_session(recording_id)
    return {
        "recording_id": s.recording_id, "sid": s.sid, "status": s.status,
        "error": s.error, "event_count": len(s.recorder.events),
        "steps": s.recorder.steps_preview(),
        "viewport": VIEWPORT,
    }


def list_active() -> list[dict]:
    return [status(rid) for rid in list(_sessions.keys())]


# ── Remote input (D-033) ────────────────────────────────────────────────

_VK = {
    "Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46, "Escape": 27,
    "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    **{f"F{i}": 111 + i for i in range(1, 13)},   # F1=112 … F12=123
}


async def dispatch_input(recording_id: str, ev: dict) -> None:
    """Forward a UI event into the server-side browser via CDP.

    Coordinates arrive in the screencast's own pixel space, which equals the
    browser viewport (VIEWPORT), so no scaling is needed — the frontend
    renders the frame at its natural size.
    """
    s = get_session(recording_id)
    kind = ev.get("kind")

    if kind == "click":
        x, y = float(ev.get("x", 0)), float(ev.get("y", 0))
        button = ev.get("button", "left")
        await s.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y})
        for t in ("mousePressed", "mouseReleased"):
            await s.cdp.send("Input.dispatchMouseEvent", {
                "type": t, "x": x, "y": y, "button": button, "clickCount": 1,
            })
    elif kind == "move":
        await s.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": float(ev.get("x", 0)), "y": float(ev.get("y", 0)),
        })
    elif kind == "wheel":
        x, y = float(ev.get("x", 0)), float(ev.get("y", 0))
        dx, dy = float(ev.get("dx", 0)), float(ev.get("dy", 0))
        await s.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy,
        })
        # D-038: record the gesture itself. Captured here rather than from a
        # DOM 'scroll' listener because SAP's ALV scrollbar repaints rows
        # without changing scrollTop — no scroll event, nothing to record.
        s.recorder.add_wheel(x, y, dx, dy)
        s.last_wheel = (x, y)
    elif kind == "text":
        # Plain text entry — insertText is far more reliable than synthesising
        # per-character key events for auto-complete-heavy SAP fields.
        await s.cdp.send("Input.insertText", {"text": ev.get("text", "")})
    elif kind == "key":
        key = ev.get("key", "")
        code = ev.get("code", "")
        vk = _VK.get(key, 0)
        base = {"key": key, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk,
                "modifiers": int(ev.get("modifiers", 0))}
        await s.cdp.send("Input.dispatchKeyEvent", {"type": "keyDown", **base})
        if len(key) == 1:
            await s.cdp.send("Input.dispatchKeyEvent", {"type": "char", "text": key, **base})
        await s.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", **base})
    else:
        raise ValueError(f"unknown input kind: {kind!r}")


def add_capture_step(recording_id: str, note: str = "", pages: int = 0) -> dict:
    """D-034/D-039: insert an evidence step at this point in the recording.

    `pages > 0` makes it a paginated capture: at replay it scrolls the list
    one page at a time, shooting each page until the view stops changing.
    """
    s = get_session(recording_id)
    if pages:
        x, y = s.last_wheel or (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
        s.recorder.add_capture_pages(max_pages=pages, x=x, y=y)
    else:
        s.recorder.add_capture(note)
    return {"steps": s.recorder.steps_preview()}


async def screenshot(recording_id: str) -> str:
    """Base64 JPEG of the current page — used for the initial paint and as a
    fallback if the screencast hasn't produced a frame yet."""
    s = get_session(recording_id)
    if s.last_frame:
        return s.last_frame
    shot = await s.page.screenshot(type="jpeg", quality=60)
    return base64.b64encode(shot).decode()


# ── Lifecycle ───────────────────────────────────────────────────────────

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
        except Exception:  # noqa: BLE001,PERF203
            pass
    try:
        await pw.stop()
    except Exception:  # noqa: BLE001
        pass
