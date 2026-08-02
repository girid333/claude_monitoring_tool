"""D-034: T-code start, manual capture steps, flow library, robust tcode nav."""
import asyncio, os, sys, json, tempfile
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work)/"systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work)/"runs.db")
os.environ["FLOWS_DIR"] = str(Path(work)/"flows")
sys.path.insert(0, BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="TCD", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8098/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])
from engine import recorder_service as rs
ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def main():
    # ---- record starting AT a T-code ----
    s = await rs.start_recording("TCD", registry_path=os.environ["SYSTEMS_PATH"], tcode="SM37")
    check("1. Recording starts at a given T-code", s.status == "recording")
    st = rs.status(s.recording_id)["steps"]
    check("2. T-code becomes step 1 of the flow",
          st and st[0]["act"] == "start_tcode" and "SM37" in str(st[0]["target"]),
          json.dumps(st[:2]))

    # interact
    box = await s.page.locator("#jobname").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind":"click","x":box["x"]+5,"y":box["y"]+5})
    await rs.dispatch_input(s.recording_id, {"kind":"text","text":"NIGHT_JOB"})
    await rs.dispatch_input(s.recording_id, {"kind":"key","key":"Tab","code":"Tab"})
    await asyncio.sleep(0.4)

    # ---- manual screenshot step mid-recording ----
    rs.add_capture_step(s.recording_id)
    st = rs.status(s.recording_id)["steps"]
    check("3. 'Capture screenshot' inserts a step where the user asked",
          any(x["act"] == "capture" for x in st), json.dumps(st))
    caps = [i for i, x in enumerate(st) if x["act"] == "capture"]
    sets = [i for i, x in enumerate(st) if x["act"] == "set"]
    check("4. Capture lands AFTER the interaction that preceded it",
          caps and sets and caps[0] > sets[0], f"caps={caps} sets={sets}")

    res = await rs.stop_recording(s.recording_id, flow_id="sm37_night_check",
                                   flows_dir=os.environ["FLOWS_DIR"])
    check("5. Named flow saved", Path(res["path"]).exists(), res["path"])
    data = json.loads(Path(res["path"]).read_text())
    check("6. Saved flow's first act is start_tcode",
          data["steps"][0]["act"] == "start_tcode" and data["steps"][0]["value"] == "SM37")
    check("7. Saved flow contains the capture step",
          any(x["act"] == "capture" for x in data["steps"]))

    # ---- flow library endpoint ----
    from fastapi.testclient import TestClient
    import main as m
    c = TestClient(m.app)
    flows = c.get("/api/flows").json()
    check("8. GET /api/flows lists the recording", len(flows) == 1, json.dumps(flows)[:150])
    if flows:
        f = flows[0]
        check("9. Listing carries name, T-codes and screenshot marker",
              f["flow_id"] == "sm37_night_check" and f["tcodes"] == ["SM37"] and f["has_capture"],
              json.dumps(f))

    # ---- replay it (proves tcode nav + capture step execute) ----
    from playwright.async_api import async_playwright
    from engine.engine import ReplayEngine
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.registry import RegistryCredentialProvider
    from engine.probe import probe
    from engine.schema import Flow
    reg = Registry.load(os.environ["SYSTEMS_PATH"])
    e = reg.get("TCD")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await b.new_page()
        await BasicLoginPlugin(SystemTarget("TCD", e.webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr = await probe(page)
        eng = ReplayEngine(page, pr.mode, "r1", "TCD", Path("run_artifacts/r1"),
                            webgui_url=e.webgui_url, client=e.client)
        r = await eng.run(Flow(**data))
        print(f"      replay ok={r.ok} steps={len(r.steps)} err={r.error}")
        for x in r.steps: print(f"        {x.index} {x.act} ok={x.ok} {x.detail}"[:110])
        check("10. START_TCODE step replays (D-034 nav fix)",
              r.steps and r.steps[0].ok, r.steps[0].detail if r.steps else "no steps")
        shots = [x for x in r.steps if x.act.value == "capture" and x.screenshot_path]
        check("11. Capture step produced a screenshot file",
              shots and Path(shots[0].screenshot_path).exists(),
              str([x.screenshot_path for x in r.steps if x.act.value=="capture"]))
        check("12. Whole recorded flow replays green", r.ok, r.error or "")
        await b.close()

asyncio.run(main())
print("D-034: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
