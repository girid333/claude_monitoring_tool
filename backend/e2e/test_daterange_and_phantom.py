"""D-042: two bugs surfaced by Giri's real SM37 recording, reproduced
against a fake screen with the same shape.

1. Clicking into a prefilled field WITHOUT editing it must not record a
   step (the D-041 blur safety-net was recording every blur unconditionally
   -- confirmed via the step viewer showing phantom `set Job Name = "*"`).
2. Two date inputs sharing the exact same generic accessible label ("Input
   Field", no distinguishing name/title) must each replay to their OWN
   recorded value, not both collapse onto whichever the resolver's
   `.first` happened to match -- confirmed by Giri's report replaying both
   date boxes back to today's SAP default instead of the two different
   dates he actually typed.
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
sys.path.insert(0, BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="DTR", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8097/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

from engine import recorder_service as rs

ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def main():
    s = await rs.start_recording("DTR", registry_path=os.environ["SYSTEMS_PATH"])
    await asyncio.sleep(0.6)

    # 1. Click into the prefilled Job Name field, then click elsewhere
    #    WITHOUT typing anything -- must not record a step.
    jb = await s.page.locator("#jobname").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": jb["x"] + 5, "y": jb["y"] + 5})
    fb = await s.page.locator("#date_from").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": fb["x"] + 5, "y": fb["y"] + 5})
    await rs.dispatch_input(s.recording_id, {"kind": "text", "text": "01.08.2026"})

    tb = await s.page.locator("#date_to").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": tb["x"] + 5, "y": tb["y"] + 5})
    await rs.dispatch_input(s.recording_id, {"kind": "text", "text": "02.08.2026"})

    bb = await s.page.locator("#btn_exec").bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "click", "x": bb["x"] + 5, "y": bb["y"] + 5})
    await asyncio.sleep(0.4)

    res = await rs.stop_recording(s.recording_id, flow_id="e2e_daterange", flows_dir="flows")
    data = json.loads(Path(res["path"]).read_text())
    set_steps = [st for st in data["steps"] if st["act"] == "set"]
    print("      set steps:", json.dumps(set_steps)[:400])

    check("1. Clicking a prefilled field without editing records NOTHING",
          not any(st["target"].get("dom", {}).get("pos_id") == "jobname" for st in set_steps),
          json.dumps(set_steps))
    check("2. Exactly two SET steps recorded (the two dates, nothing phantom)",
          len(set_steps) == 2, json.dumps(set_steps))
    values = sorted(st["value"] for st in set_steps)
    check("3. Both distinct typed values recorded",
          values == ["01.08.2026", "02.08.2026"], values)
    nths = sorted(st["target"]["dom"].get("nth") for st in set_steps)
    check("4. Each date field carries a DIFFERENT disambiguation index",
          nths == [0, 1], nths)

    # ---- REPLAY against a fresh page ----
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
        await BasicLoginPlugin(SystemTarget("DTR", reg.get("DTR").webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr = await probe(page)
        eng = ReplayEngine(page, pr.mode, "replay_dtr", "DTR", Path("run_artifacts/replay_dtr"))
        flow = Flow(**data)
        result = await eng.run(flow)
        print(f"      replay ok={result.ok} err={result.error}")
        for st in result.steps:
            print(f"        step{st.index} {st.act} ok={st.ok} conf={st.confidence} {st.detail}"[:140])

        from_val = await page.locator("#date_from").input_value()
        to_val = await page.locator("#date_to").input_value()
        check("5. #date_from got its OWN recorded value",
              from_val == "01.08.2026", f"got {from_val!r}")
        check("6. #date_to got its OWN, DIFFERENT recorded value "
              "(THE BUG: both used to land on the same field)",
              to_val == "02.08.2026", f"got {to_val!r}")
        jobname_val = await page.locator("#jobname").input_value()
        check("7. Untouched Job Name still shows SAP's own default, unmodified",
              jobname_val == "*", f"got {jobname_val!r}")
        check("8. Overall replay result ok", result.ok, result.error or "")
        await b.close()

asyncio.run(main())
print("D-042 DATE-RANGE + PHANTOM-BLUR: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
