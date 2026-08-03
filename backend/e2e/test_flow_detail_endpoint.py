"""D-041: GET /api/flows/{flow_id} step viewer, and the blur-based redundant
capture safety net doesn't double-record a normal field edit."""
import asyncio, os, sys, json, tempfile
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work) / "runs.db")
os.environ["FLOWS_DIR"] = str(Path(work) / "flows")
sys.path.insert(0, BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="DET", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8098/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

from engine import recorder_service as rs

ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def main():
    s = await rs.start_recording("DET", registry_path=os.environ["SYSTEMS_PATH"])
    await asyncio.sleep(0.6)

    # type into #jobname via keyboard (real change+blur pair) then click
    # elsewhere to blur it -- this is the scenario the blur safety-net
    # backstops, and coalescing must still leave exactly ONE set step.
    box = await s.page.locator("#jobname").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": box["x"] + 5, "y": box["y"] + 5})
    await rs.dispatch_input(s.recording_id, {"kind": "text", "text": "01.08.2026"})
    bb = await s.page.locator("#btn_exec").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": bb["x"] + 5, "y": bb["y"] + 5})
    await asyncio.sleep(0.4)

    res = await rs.stop_recording(s.recording_id, flow_id="e2e_detail_flow",
                                   flows_dir=os.environ["FLOWS_DIR"])
    data = json.loads(Path(res["path"]).read_text())
    set_steps = [st for st in data["steps"] if st["act"] == "set"]
    check("1. Blur safety-net does not double-record the field edit",
          len(set_steps) == 1, json.dumps(set_steps))
    check("2. The recorded value is the typed text",
          set_steps[0]["value"] == "01.08.2026", set_steps[0]["value"])
    press_steps = [st for st in data["steps"] if st["act"] == "press"]
    check("3. The Execute button click was captured as a PRESS step",
          len(press_steps) == 1, json.dumps(press_steps))

    from fastapi.testclient import TestClient
    import main as m
    c = TestClient(m.app)
    r = c.get("/api/flows/e2e_detail_flow")
    check("4. GET /api/flows/{id} responds 200", r.status_code == 200, r.text[:200])
    body = r.json()
    check("5. Detail carries the full step count", body["step_count"] == len(data["steps"]),
          f"{body['step_count']} vs {len(data['steps'])}")
    check("6. Steps are human-readable (act/target/value/resolved_by)",
          all(set(st.keys()) == {"act", "target", "value", "resolved_by"} for st in body["steps"]),
          json.dumps(body["steps"][:2]))
    set_row = next(st for st in body["steps"] if st["act"] == "set")
    check("7. The typed value is visible in the readable view",
          set_row["value"] == "01.08.2026", set_row)
    press_row = next(st for st in body["steps"] if st["act"] == "press")
    check("8. The button target has a readable label, not just '—'",
          press_row["target"] not in (None, "—", ""), press_row)

    r404 = c.get("/api/flows/does_not_exist")
    check("9. Unknown flow_id -> 404", r404.status_code == 404, r404.status_code)
    # a raw ".." segment gets normalized away by routing before it ever
    # reaches our handler (Starlette's own protection, even safer than
    # ours) -- exercise OUR guard directly with a leading-dot flow_id,
    # which routes through fine but must still be rejected.
    r400 = c.get("/api/flows/.hidden")
    check("10. Path-traversal-style flow_id rejected by our own guard",
          r400.status_code == 400, r400.status_code)

asyncio.run(main())
print("D-041: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
