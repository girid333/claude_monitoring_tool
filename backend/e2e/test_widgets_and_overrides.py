"""D-043: radio buttons (ARIA spans) + native <select> replay accurately,
and replay-time input overrides change a SET value without touching the
saved recording.
"""
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
Registry(systems=[SystemEntry(sid="WDG", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8096/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

from engine import recorder_service as rs

ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def main():
    s = await rs.start_recording("WDG", registry_path=os.environ["SYSTEMS_PATH"])
    await asyncio.sleep(0.6)

    # type a material number
    mb = await s.page.locator("#matnr").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": mb["x"] + 5, "y": mb["y"] + 5})
    await rs.dispatch_input(s.recording_id, {"kind": "text", "text": "MAT-100"})

    # pick the SECOND radio ("Detail view") — starts unselected
    radios = s.page.locator('[role="radio"]')
    rb = await radios.nth(1).bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": rb["x"] + 5, "y": rb["y"] + 5})
    await asyncio.sleep(0.2)
    check("1. Radio click reached the page (Detail view selected)",
          await radios.nth(1).get_attribute("aria-checked") == "true")

    # choose a dropdown value via the page's own select mechanics
    await s.page.locator("#evt").select_option("SAP_END_OF_DAY")
    await asyncio.sleep(0.3)

    res = await rs.stop_recording(s.recording_id, flow_id="e2e_widgets",
                                   flows_dir=os.environ["FLOWS_DIR"])
    data = json.loads(Path(res["path"]).read_text())
    set_steps = [st for st in data["steps"] if st["act"] == "set"]
    print("      set steps:", json.dumps(set_steps)[:500])

    radio_steps = [st for st in set_steps
                   if st.get("target", {}).get("dom", {}).get("role") == "radio"]
    check("2. Radio recorded as SET (absolute select), not a blind press",
          len(radio_steps) == 1 and radio_steps[0]["value"] is True, json.dumps(radio_steps))
    check("3. Radio carries its positional index (1)",
          radio_steps and radio_steps[0]["target"]["dom"].get("checkbox_index") == 1,
          json.dumps(radio_steps))
    select_steps = [st for st in set_steps if st.get("value") == "SAP_END_OF_DAY"]
    check("4. Dropdown change recorded with its chosen value", len(select_steps) == 1,
          json.dumps(set_steps))
    matnr_steps = [st for st in set_steps if st.get("value") == "MAT-100"]
    check("5. Text field recorded", len(matnr_steps) == 1, json.dumps(set_steps))

    # index of the material SET step, for the override
    matnr_idx = next(i for i, st in enumerate(data["steps"])
                     if st["act"] == "set" and st.get("value") == "MAT-100")

    # ---- REPLAY with an override changing the material number ----
    from main import _apply_overrides
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
        await BasicLoginPlugin(SystemTarget("WDG", reg.get("WDG").webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr = await probe(page)
        eng = ReplayEngine(page, pr.mode, "replay_wdg", "WDG", Path("run_artifacts/replay_wdg"))
        flow = Flow(**data)
        notes = _apply_overrides(flow, {str(matnr_idx): "MAT-999",
                                         "999": "nope", "abc": "nope"})
        check("6. Override applied cleanly; bad indices reported not crashed",
              len(notes) == 2, str(notes))
        result = await eng.run(flow)
        print(f"      replay ok={result.ok} err={result.error}")
        for st in result.steps:
            print(f"        step{st.index} {st.act} ok={st.ok} conf={st.confidence} {st.detail}"[:140])

        check("7. OVERRIDE: replay used the changed value, not the recorded one",
              await page.locator("#matnr").input_value() == "MAT-999",
              await page.locator("#matnr").input_value())
        radios2 = page.locator('[role="radio"]')
        check("8. Radio replayed: Detail view selected on the fresh page",
              await radios2.nth(1).get_attribute("aria-checked") == "true")
        check("9. Other radios stay deselected (group semantics intact)",
              await radios2.nth(0).get_attribute("aria-checked") == "false"
              and await radios2.nth(2).get_attribute("aria-checked") == "false")
        check("10. Native select replayed to the recorded option",
              await page.locator("#evt").input_value() == "SAP_END_OF_DAY",
              await page.locator("#evt").input_value())
        check("11. Overall replay ok", result.ok, result.error or "")
        await b.close()

    # the saved recording on disk must be untouched by the override
    data2 = json.loads((Path(os.environ["FLOWS_DIR"]) / "e2e_widgets.json").read_text())
    check("12. Saved recording on disk still has the ORIGINAL value",
          data2["steps"][matnr_idx]["value"] == "MAT-100",
          data2["steps"][matnr_idx]["value"])

    # ---- the whole run path honors flow_overrides from the API body ----
    from fastapi.testclient import TestClient
    import main as m
    c = TestClient(m.app)
    body = {"sids": ["WDG"], "flows": ["e2e_widgets"],
            "flow_overrides": {"e2e_widgets": {str(matnr_idx): "MAT-777"}}}
    r = c.post("/api/run", json=body)
    check("13. /api/run accepts flow_overrides", r.status_code == 200, r.text[:200])

asyncio.run(main())
print("D-043 WIDGETS + OVERRIDES: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
