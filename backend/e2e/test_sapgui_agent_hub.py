"""D-044 SANDBOX: SAP GUI bridge-agent hub protocol, end to end, with a FAKE
agent standing in for a real Windows+SAP GUI machine (which this Linux
sandbox can never have). This proves the whole wire protocol — pairing,
WebSocket registration, job dispatch, SSE fan-out, flow storage in the
SEPARATE flows_sapgui/ directory, replay-time overrides — genuinely works
against a REAL running server (a real uvicorn process, real TCP, real
`websockets` client — the same library agent/sapgui_bridge_agent.py uses),
not a mocked transport.

It also proves the isolation contract Giri asked for: the existing
`/api/flows` (WebGUI flow library) and `FLOWS_DIR` are completely
untouched by anything this test does.

What this test CANNOT prove (needs Giri's real Windows workstation, per
the design doc's Phase B/C): that `win32com.client.GetObject("SAPGUI")`
actually talks to a real SAP GUI session, or that the polling-diff
recorder produces usable steps against real screens.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from cryptography.fernet import Fernet

os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work) / "runs.db")
os.environ["FLOWS_DIR"] = str(Path(work) / "flows")               # existing webgui dir
os.environ["SAPGUI_FLOWS_DIR"] = str(Path(work) / "flows_sapgui")  # sandbox dir
sys.path.insert(0, BACKEND)

from engine.registry import Registry  # noqa: E402
Registry(systems=[]).save(os.environ["SYSTEMS_PATH"])

import uvicorn  # noqa: E402
import httpx  # noqa: E402
import websockets  # noqa: E402
import main as m  # noqa: E402

PORT = 8199
BASE = f"http://127.0.0.1:{PORT}"
WS_BASE = f"ws://127.0.0.1:{PORT}"

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


def run_server():
    config = uvicorn.Config(m.app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


srv_thread = threading.Thread(target=run_server, daemon=True)
srv_thread.start()
for _ in range(50):
    try:
        httpx.get(BASE + "/api/sapgui/agents", timeout=1, trust_env=False)
        break
    except Exception:  # noqa: BLE001
        time.sleep(0.2)


async def fake_agent(code_holder: dict, out: dict):
    async with websockets.connect(WS_BASE + "/api/sapgui/agent/ws") as ws:
        await ws.send(json.dumps({"kind": "hello", "code": code_holder["code"],
                                   "info": {"hostname": "TESTBOX", "username": "giri",
                                            "sap_gui_version": "FAKE 8.00",
                                            "scripting_enabled": True}}))
        welcome = json.loads(await ws.recv())
        out["welcome"] = welcome
        out["agent_id"] = welcome["agent_id"]
        out["ready"].set()

        job = json.loads(await ws.recv())  # record_start
        out["record_start_job"] = job
        job_id = job["job_id"]
        await ws.send(json.dumps({"kind": "step", "job_id": job_id,
                                   "step": {"act": "set", "sid": "wnd[0]/usr/matnr",
                                            "label": "Material", "value": "MAT-100"}}))
        await ws.send(json.dumps({"kind": "step", "job_id": job_id,
                                   "step": {"act": "set", "sid": "wnd[0]/usr/chk1",
                                            "label": "Active", "value": True}}))

        job2 = json.loads(await ws.recv())  # record_stop
        await ws.send(json.dumps({
            "kind": "flow_saved", "job_id": job2["job_id"], "sid": "F4H",
            "steps": [
                {"act": "start_tcode", "sid": "wnd[0]/tbar[0]/okcd", "label": "cmd", "value": "SM37"},
                {"act": "set", "sid": "wnd[0]/usr/matnr", "label": "Material", "value": "MAT-100"},
                {"act": "set", "sid": "wnd[0]/usr/chk1", "label": "Active", "value": True},
            ],
        }))

        job3 = json.loads(await ws.recv())  # replay
        out["replay_job"] = job3
        ov = job3.get("overrides", {})
        for i, step in enumerate(job3["flow"]["steps"]):
            value = ov.get(str(i), step.get("value"))
            await ws.send(json.dumps({"kind": "step_result", "job_id": job3["job_id"],
                                       "index": i, "ok": True, "detail": f"set -> {value!r}"}))
        await ws.send(json.dumps({"kind": "replay_done", "job_id": job3["job_id"],
                                   "ok": True, "step_count": len(job3["flow"]["steps"])}))
        await asyncio.sleep(0.3)  # let the last SSE reads drain before closing


async def sse_events(job_url: str, want: int, stop_kinds=()) -> list[dict]:
    events = []
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        async with client.stream("GET", job_url) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    events.append(ev)
                    if len(events) >= want or ev.get("kind") in stop_kinds:
                        break
    return events


async def main() -> None:
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        r = await client.get(BASE + "/api/sapgui/agents")
        check("0. GET /api/sapgui/agents works, starts empty", r.status_code == 200 and r.json() == [], r.text)

        r = await client.post(BASE + "/api/sapgui/pair/start")
        check("1. pairing code issued", r.status_code == 200 and "code" in r.json(), r.text)
        code_holder = {"code": r.json()["code"]}

        out = {"ready": asyncio.Event()}
        agent_task = asyncio.create_task(fake_agent(code_holder, out))
        await asyncio.wait_for(out["ready"].wait(), timeout=10)
        agent_id = out["agent_id"]

        agents = (await client.get(BASE + "/api/sapgui/agents")).json()
        check("2. agent shows up in /api/sapgui/agents after hello+welcome",
              len(agents) == 1 and agents[0]["hostname"] == "TESTBOX", agents)

        r = await client.post(BASE + "/api/sapgui/record/start",
                               json={"agent_id": agent_id, "tcode": "SM37", "sid": "F4H"})
        check("3. record/start dispatched", r.status_code == 200 and "job_id" in r.json(), r.text)
        job_id = r.json()["job_id"]

        events = await sse_events(f"{BASE}/api/sapgui/events/{agent_id}/{job_id}", want=2)
        check("4. two live 'step' events arrived over SSE",
              len(events) == 2 and all(e["kind"] == "step" for e in events), events)

        r = await client.post(BASE + "/api/sapgui/record/stop",
                               json={"agent_id": agent_id, "job_id": job_id,
                                     "flow_id": "e2e_sapgui_flow", "description": "SM37 sandbox test"})
        check("5. record/stop returns saved flow summary",
              r.status_code == 200 and r.json()["step_count"] == 3, r.text)

        sandbox_path = Path(os.environ["SAPGUI_FLOWS_DIR"]) / "e2e_sapgui_flow.json"
        check("6. flow file written under SAPGUI_FLOWS_DIR (not FLOWS_DIR)", sandbox_path.exists(), sandbox_path)
        webgui_path = Path(os.environ["FLOWS_DIR"]) / "e2e_sapgui_flow.json"
        check("7. ISOLATION: nothing written into the existing webgui FLOWS_DIR", not webgui_path.exists())

        r = await client.get(BASE + "/api/flows")
        check("8. ISOLATION: existing /api/flows (webgui picker) does not list the sandbox flow",
              all(f["flow_id"] != "e2e_sapgui_flow" for f in r.json()), r.json())

        r = await client.get(BASE + "/api/sapgui/flows")
        check("9. GET /api/sapgui/flows lists the sandbox flow",
              any(f["flow_id"] == "e2e_sapgui_flow" for f in r.json()), r.json())

        r = await client.get(BASE + "/api/sapgui/flows/e2e_sapgui_flow")
        check("10. GET /api/sapgui/flows/{id} returns human-readable steps",
              r.status_code == 200 and len(r.json()["steps"]) == 3 and
              r.json()["steps"][1]["description"].startswith("set —"), r.json())

        r2 = await client.post(BASE + "/api/sapgui/replay",
                                json={"agent_id": agent_id, "flow_id": "e2e_sapgui_flow",
                                      "overrides": {"1": "MAT-999"}})
        check("11. replay dispatched", r2.status_code == 200 and "job_id" in r2.json(), r2.text)
        replay_job_id = r2.json()["job_id"]

        events2 = await sse_events(f"{BASE}/api/sapgui/events/{agent_id}/{replay_job_id}",
                                    want=4, stop_kinds=("replay_done",))
        check("12. replay SSE stream ends with replay_done(ok=True)",
              events2 and events2[-1] == {"kind": "replay_done", "job_id": replay_job_id,
                                           "ok": True, "step_count": 3}, events2)
        override_result = next((e for e in events2 if e.get("index") == 1), None)
        check("13. the OVERRIDDEN value (MAT-999, not the recorded MAT-100) reached the agent",
              override_result is not None and "MAT-999" in override_result["detail"], events2)

        data_after = json.loads(sandbox_path.read_text())
        check("14. saved sandbox recording on disk unchanged by the override",
              data_after["steps"][1]["value"] == "MAT-100", data_after["steps"][1]["value"])

        r = await client.post(BASE + "/api/sapgui/record/start",
                               json={"agent_id": "nope", "tcode": "SM37"})
        check("15. record/start against unknown agent -> 404", r.status_code == 404, r.text)
        r = await client.get(BASE + "/api/sapgui/events/nope/nope")
        check("16. events for unknown agent/job -> 404", r.status_code == 404, r.text)
        # a raw ".." segment is normalized away by routing before it ever
        # reaches our handler (Starlette's own protection); exercise OUR
        # guard directly with a leading-dot flow_id (routes through fine,
        # must still be rejected) — same pattern as test_flow_detail_endpoint.py.
        r = await client.get(BASE + "/api/sapgui/flows/.hidden")
        check("17. leading-dot flow_id rejected by our own guard", r.status_code == 400, r.text)

        await agent_task


asyncio.run(main())
print("D-044 SAPGUI AGENT HUB: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
