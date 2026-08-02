"""D-033: full record -> save -> REPLAY round trip, headless, no display."""
import asyncio, os, sys, json, tempfile
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work)/"systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work)/"runs.db")
sys.path.insert(0, BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="REC", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8098/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

from engine import recorder_service as rs
ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def main():
    # ---- RECORD (headless, remote-controlled) ----
    s = await rs.start_recording("REC", registry_path=os.environ["SYSTEMS_PATH"])
    check("1. Recording starts HEADLESS (no display needed)", s.status == "recording")
    await asyncio.sleep(1.2)
    fr = await rs.screenshot(s.recording_id)
    check("2. Screencast produces a frame", bool(fr) and len(fr) > 500, f"len={len(fr or '')}")

    # locate the field, then drive it purely through remote input
    box = await s.page.locator("#jobname").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind":"click","x":box["x"]+5,"y":box["y"]+5})
    await rs.dispatch_input(s.recording_id, {"kind":"text","text":"NIGHTLY_BATCH"})
    await rs.dispatch_input(s.recording_id, {"kind":"key","key":"Tab","code":"Tab"})
    await asyncio.sleep(0.4)
    val = await s.page.locator("#jobname").input_value()
    check("3. Remote typing reaches the real page", val == "NIGHTLY_BATCH", f"got {val!r}")

    cb = await s.page.locator("#aborted").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind":"click","x":cb["x"]+5,"y":cb["y"]+5})
    await asyncio.sleep(0.3)
    check("4. Remote click toggles a checkbox", await s.page.locator("#aborted").is_checked())

    bb = await s.page.locator("#btn_exec").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind":"click","x":bb["x"]+5,"y":bb["y"]+5})
    await asyncio.sleep(0.5)

    prev = rs.status(s.recording_id)["steps"]
    print("      captured:", json.dumps(prev)[:220])
    check("5. Steps captured with DOM identification", len(prev) >= 2,
          f"{len(prev)} steps")
    check("6. Steps resolved via DOM (no scripting API present)",
          any(p["resolved_by"] == "dom" for p in prev))

    res = await rs.stop_recording(s.recording_id, flow_id="e2e_recorded", flows_dir="flows")
    check("7. Flow saved to disk", Path(res["path"]).exists(), res["path"])
    flow_json = json.loads(Path(res["path"]).read_text())
    has_dom = any(st.get("target", {}).get("dom") for st in flow_json["steps"] if st.get("target"))
    check("8. Saved flow carries DOM candidates (replayable elsewhere)", has_dom)
    no_sid_required = all(st.get("target", {}).get("sid") is None
                          for st in flow_json["steps"] if st.get("target"))
    check("9. Works with NO scripting IDs (Target.sid optional, D-033)", no_sid_required)

    # ---- REPLAY the recorded flow in a fresh browser ----
    from playwright.async_api import async_playwright
    from engine.engine import ReplayEngine
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.registry import RegistryCredentialProvider
    from engine.probe import probe
    from engine.schema import Flow
    reg = Registry.load(os.environ["SYSTEMS_PATH"])
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await b.new_page()
        await BasicLoginPlugin(SystemTarget("REC", reg.get("REC").webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr = await probe(page)
        eng = ReplayEngine(page, pr.mode, "replay1", "REC", Path("run_artifacts/replay1"))
        flow = Flow(**flow_json)
        result = await eng.run(flow)
        print(f"      replay mode={result.mode} ok={result.ok} steps={len(result.steps)} err={result.error}")
        for st in result.steps:
            print(f"        step{st.index} {st.act} ok={st.ok} conf={st.confidence} {st.detail}"[:130])
        replayed_val = await page.locator("#jobname").input_value()
        check("10. REPLAY re-entered the recorded value", replayed_val == "NIGHTLY_BATCH",
              f"got {replayed_val!r}")
        check("11. Replay resolved steps without scripting API",
              any(st.ok for st in result.steps), result.error or "")
        await b.close()

asyncio.run(main())
print("D-033 RECORD+REPLAY: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
