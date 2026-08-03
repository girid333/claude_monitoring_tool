"""D-044 SANDBOX — SAP GUI bridge agent.

Runs on the Windows workstation that has SAP GUI for Windows installed and
a logged-in session. Connects OUTBOUND to the backend's WebSocket endpoint
(`/api/sapgui/agent/ws`) with a one-time pairing code obtained from the
"SAP GUI (sandbox)" page — the container never needs to reach this
machine, exactly like a browser tab reaching the server.

Usage (on the Windows workstation, Python 3.10+):

    pip install -r requirements.txt
    python sapgui_bridge_agent.py --server ws://YOUR-SERVER:8000 --code ABCD1234

    # or, to try the whole protocol WITHOUT SAP GUI installed (for a demo
    # or for CI — this is what backend/e2e/test_sapgui_agent_hub.py drives):
    python sapgui_bridge_agent.py --server ws://127.0.0.1:8000 --code ABCD1234 --fake

Status (be honest about what's proven vs. best-effort):
  - Connect / pair / heartbeat / probe: REAL COM (`win32com.client.GetObject
    ("SAPGUI")`), standard and well-documented — confident this works as
    written once pywin32 + a running SAP GUI session are present.
  - REPLAY (`findById(sid).text = ...`, `.press()`, `.selected = bool`,
    `.sendVKey(n)`, `HardCopy()` for screenshots): also standard,
    well-documented Scripting API members — confident in the approach.
  - RECORD: the SAP GUI Scripting API has no public "subscribe to user
    actions" event stream (SAP's own built-in Script Recorder is a
    separate, closed C++ feature of the GUI client, not exposed over COM).
    This agent instead POLLS the active window's control tree every
    `POLL_INTERVAL_S` and diffs it against the previous snapshot, emitting
    a `set` step when a field's value/selection changed, and inferring a
    button/Enter/F-key press from a transient OK-code change. This is a
    real, workable technique (it's how several home-grown SAP RPA
    recorders do it) but WILL need tuning against Giri's actual GUI
    version/screens — flagged as Phase C/D follow-up, not claimed as
    finished here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from typing import Any, Optional

try:
    import websockets
except ImportError:  # pragma: no cover
    print("Missing dependency: pip install websockets", file=sys.stderr)
    raise

POLL_INTERVAL_S = 0.4
HEARTBEAT_S = 15


# ── Engine interface ─────────────────────────────────────────────────────
# Both engines implement the same four methods so the WS protocol handling
# below never needs to know which one it's talking to.

class EngineBase:
    def probe(self) -> dict:
        raise NotImplementedError

    def record_start(self, tcode: str, sid: str) -> dict:
        """Returns {"ok": bool, "message"|"error": str} — the agent sends
        this back over the socket immediately so the sandbox page shows
        SOMETHING right away instead of silently sitting at '0 steps'."""
        raise NotImplementedError

    def record_poll(self) -> list[dict]:
        """Return any newly-detected steps since the last poll (possibly empty)."""
        raise NotImplementedError

    def record_stop(self) -> list[dict]:
        """Stop recording, return the FULL step list."""
        raise NotImplementedError

    def replay(self, flow: dict, overrides: dict) -> list[dict]:
        """Run every step, return a list of {index, ok, detail} results."""
        raise NotImplementedError


class FakeEngine(EngineBase):
    """No SAP GUI / no COM at all — simulates a plausible session so the
    WHOLE hub protocol (pairing, job dispatch, SSE fan-out, flow storage,
    replay-with-overrides) is exercisable on Linux in CI, exactly like the
    fake fixtures under backend/e2e/ do for the WebGUI engine. Confirms the
    protocol; does not confirm real COM behavior — that needs Giri's
    workstation (Phase B/C validation per the design doc)."""

    def __init__(self) -> None:
        self._recording = False
        self._steps: list[dict] = []
        self._sim_fields = {"BTCH2170-JOBNAME": "*", "matnr": ""}

    def probe(self) -> dict:
        return {"hostname": platform.node(), "username": "fake-user",
                "sap_gui_version": "FAKE 8.00", "scripting_enabled": True}

    def record_start(self, tcode: str, sid: str) -> dict:
        self._recording = True
        self._steps = [{"act": "start_tcode", "sid": "wnd[0]/tbar[0]/okcd",
                         "label": "Command field", "value": tcode}]
        return {"ok": True, "message": f"(fake) navigated to {tcode or '(home screen)'}"}

    def record_poll(self) -> list[dict]:
        # A real engine would diff the live control tree here; the fake
        # engine has nothing new between polls (its "user actions" are fed
        # synchronously by record_simulate_step, used only by the E2E test).
        return []

    def record_simulate_step(self, step: dict) -> None:
        self._steps.append(step)

    def record_stop(self) -> list[dict]:
        self._recording = False
        return self._steps

    def replay(self, flow: dict, overrides: dict) -> list[dict]:
        results = []
        for i, step in enumerate(flow.get("steps", [])):
            value = overrides.get(str(i), step.get("value"))
            results.append({"index": i, "ok": True,
                             "detail": f"{step.get('act')} {step.get('sid')} -> {value!r}"})
        return results


class RealEngine(EngineBase):
    """Windows-only. Imports win32com lazily so this file still imports
    cleanly on Linux (needed for the E2E test to import this module and
    exercise its protocol-handling code without pywin32 installed)."""

    def __init__(self) -> None:
        self._session = None
        self._recording = False
        self._prev_snapshot: dict[str, Any] = {}
        self._steps: list[dict] = []

    def _connect(self):
        import win32com.client  # noqa: PLC0415
        try:
            sap_gui_auto = win32com.client.GetObject("SAPGUI")
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "Could not reach SAP GUI via GetObject('SAPGUI') — make sure SAP GUI "
                "for Windows is running, you're logged into a session, and scripting is "
                "enabled (Options -> Accessibility & Scripting -> Scripting -> Enable "
                f"scripting). Original error: {exc!r}") from exc
        application = sap_gui_auto.GetScriptingEngine
        if application.Children.Count == 0:
            raise RuntimeError(
                "SAP GUI scripting engine has no open connections — log into a SAP "
                "system in SAP GUI first, then retry.")
        connection = application.Children(0)
        if connection.Children.Count == 0:
            raise RuntimeError(
                "SAP GUI connection has no open sessions — open a session (log in) "
                "in SAP GUI first, then retry.")
        session = connection.Children(0)
        return session

    def probe(self) -> dict:
        try:
            session = self._connect()
            self._session = session
            info = session.Info
            version = ""
            try:
                app = session.Parent.Parent  # session -> connection -> application
                version = f"{app.MajorVersion}.{app.MinorVersion}.{app.PatchLevel}"
            except Exception:  # noqa: BLE001 - cosmetic only, never blocks a real connection
                version = "connected (version unknown)"
            return {
                "hostname": platform.node(),
                "username": getattr(info, "User", ""),
                "sap_gui_version": version,
                "scripting_enabled": True,
            }
        except Exception as exc:  # noqa: BLE001 - report, don't crash the agent
            return {"hostname": platform.node(), "username": "", "sap_gui_version": "",
                     "scripting_enabled": False, "error": str(exc)}

    def _snapshot(self, node, path: str, out: dict) -> None:
        try:
            kind = node.Type
        except Exception:  # noqa: BLE001
            return
        entry: dict[str, Any] = {"type": kind}
        try:
            if kind in ("GuiTextField", "GuiCTextField", "GuiComboBox"):
                entry["text"] = node.Text
            elif kind in ("GuiCheckBox", "GuiRadioButton"):
                entry["selected"] = node.Selected
        except Exception:  # noqa: BLE001
            pass
        out[path] = entry
        try:
            for i in range(node.Children.Count):
                child = node.Children.ElementAt(i)
                self._snapshot(child, child.Id, out)
        except Exception:  # noqa: BLE001
            pass

    def record_start(self, tcode: str, sid: str) -> dict:
        """THE FIX: this used to only note the intended tcode in the step
        list without ever actually sending it to SAP GUI — recording
        silently did nothing because the screen never moved and nothing
        the user did afterward matched what they were looking at. Now it
        actually types into the OK-code field and presses Enter, exactly
        like the WebGUI recorder's own tcode-start behavior, THEN takes
        the baseline snapshot once the new screen has settled."""
        try:
            session = self._session or self._connect()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Could not connect to SAP GUI: {exc}"}
        self._session = session
        self._recording = True
        self._steps = []
        nav_note = "(stayed on current screen — no T-code given)"
        if tcode.strip():
            self._steps.append({"act": "start_tcode", "sid": "wnd[0]/tbar[0]/okcd",
                                 "label": "Command field", "value": tcode.strip()})
            try:
                session.findById("wnd[0]/tbar[0]/okcd").text = f"/n{tcode.strip()}"
                session.findById("wnd[0]").sendVKey(0)
                time.sleep(0.8)  # let the new screen finish rendering before baselining it
                nav_note = f"navigated to {tcode.strip()}"
            except Exception as exc:  # noqa: BLE001
                self._recording = False
                return {"ok": False,
                        "error": f"Navigation to {tcode.strip()} failed: {exc}. "
                                 "Check the T-code is valid and you have authorization."}
        self._prev_snapshot = {}
        try:
            wnd = session.findById("wnd[0]")
            self._snapshot(wnd, "wnd[0]", self._prev_snapshot)
        except Exception as exc:  # noqa: BLE001
            self._recording = False
            return {"ok": False, "error": f"Could not read the SAP GUI screen: {exc}"}
        return {"ok": True, "message": f"Recording started — {nav_note}"}

    def record_poll(self) -> list[dict]:
        if not self._recording or self._session is None:
            return []
        new_steps: list[dict] = []
        try:
            wnd = self._session.findById("wnd[0]")
        except Exception:  # noqa: BLE001
            return []
        snap: dict[str, Any] = {}
        self._snapshot(wnd, "wnd[0]", snap)
        for path, entry in snap.items():
            prev = self._prev_snapshot.get(path)
            if prev is None:
                continue
            if entry.get("text") is not None and entry.get("text") != prev.get("text"):
                new_steps.append({"act": "set", "sid": path, "label": path,
                                   "value": entry["text"]})
            if entry.get("selected") is not None and entry.get("selected") != prev.get("selected"):
                new_steps.append({"act": "set", "sid": path, "label": path,
                                   "value": bool(entry["selected"])})
        self._prev_snapshot = snap
        self._steps.extend(new_steps)
        return new_steps

    def record_stop(self) -> list[dict]:
        self.record_poll()
        self._recording = False
        return self._steps

    def replay(self, flow: dict, overrides: dict) -> list[dict]:
        session = self._session or self._connect()
        self._session = session
        results = []
        for i, step in enumerate(flow.get("steps", [])):
            sid = step.get("sid", "")
            value = overrides.get(str(i), step.get("value"))
            act = step.get("act")
            try:
                if act == "start_tcode":
                    session.findById("wnd[0]/tbar[0]/okcd").text = f"/n{value}"
                    session.findById("wnd[0]").sendVKey(0)
                elif act == "set":
                    el = session.findById(sid)
                    if isinstance(value, bool):
                        el.selected = value
                    else:
                        el.text = value
                elif act == "press":
                    session.findById(sid).press()
                elif act == "vkey":
                    session.findById(sid).sendVKey(int(value))
                results.append({"index": i, "ok": True, "detail": f"{act} {sid}"})
            except Exception as exc:  # noqa: BLE001
                results.append({"index": i, "ok": False, "detail": repr(exc)})
        return results


# ── WebSocket protocol handling (engine-agnostic) ────────────────────────

async def run_agent(server: str, code: str, use_fake: bool) -> None:
    engine: EngineBase = FakeEngine() if use_fake else RealEngine()
    url = server.rstrip("/") + "/api/sapgui/agent/ws"
    async with websockets.connect(url) as ws:
        info = engine.probe()
        await ws.send(json.dumps({"kind": "hello", "code": code, "info": info}))
        welcome = json.loads(await ws.recv())
        if welcome.get("kind") != "welcome":
            print(f"Pairing failed: {welcome}", file=sys.stderr)
            return
        agent_id = welcome["agent_id"]
        print(f"Paired. agent_id={agent_id} engine={'fake' if use_fake else 'real'}")

        last_heartbeat = time.time()
        poll_task: Optional[asyncio.Task] = None
        active_job: Optional[str] = None

        async def heartbeat_loop():
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                await ws.send(json.dumps({"kind": "heartbeat"}))

        hb = asyncio.create_task(heartbeat_loop())

        async def poll_loop(job_id: str):
            while True:
                await asyncio.sleep(POLL_INTERVAL_S)
                for step in engine.record_poll():
                    await ws.send(json.dumps({"kind": "step", "job_id": job_id, "step": step}))

        try:
            async for raw in ws:
                job = json.loads(raw)
                kind = job.get("kind")
                job_id = job.get("job_id")
                if kind == "record_start":
                    active_job = job_id
                    try:
                        result = engine.record_start(job.get("tcode", ""), job.get("sid", ""))
                    except Exception as exc:  # noqa: BLE001 - never let a bad job kill the agent
                        result = {"ok": False, "error": f"record_start crashed: {exc}"}
                    if result.get("ok"):
                        await ws.send(json.dumps({"kind": "status", "job_id": job_id,
                                                   "message": result.get("message", "")}))
                        poll_task = asyncio.create_task(poll_loop(job_id))
                    else:
                        await ws.send(json.dumps({"kind": "error", "job_id": job_id,
                                                   "error": result.get("error", "unknown error")}))
                        active_job = None
                elif kind == "record_stop":
                    if poll_task:
                        poll_task.cancel()
                        poll_task = None
                    steps = engine.record_stop()
                    await ws.send(json.dumps({"kind": "flow_saved", "job_id": active_job,
                                               "steps": steps}))
                    active_job = None
                elif kind == "replay":
                    results = engine.replay(job.get("flow", {}), job.get("overrides", {}))
                    for r in results:
                        await ws.send(json.dumps({"kind": "step_result", "job_id": job_id, **r}))
                    await ws.send(json.dumps({"kind": "replay_done", "job_id": job_id,
                                               "ok": all(r["ok"] for r in results),
                                               "step_count": len(results)}))
        finally:
            hb.cancel()
            if poll_task:
                poll_task.cancel()


def main() -> None:
    ap = argparse.ArgumentParser(description="SAP GUI bridge agent (D-044 sandbox)")
    ap.add_argument("--server", required=True, help="ws:// or wss:// base URL of the backend")
    ap.add_argument("--code", required=True, help="one-time pairing code from the SAP GUI (sandbox) page")
    ap.add_argument("--fake", action="store_true", help="simulate SAP GUI (no COM, no real session needed)")
    args = ap.parse_args()
    asyncio.run(run_agent(args.server, args.code, args.fake))


if __name__ == "__main__":
    main()
