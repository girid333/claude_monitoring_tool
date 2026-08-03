"""D-040: checkbox record -> save -> REPLAY, proving ABSOLUTE end-state
(not blind toggle), headless, no display.

fake_sap_checkboxes.py serves six unlabeled `<span role="checkbox"
aria-checked="...">` elements — the same ARIA-widget shape v1 found in
SM37's job-overview screen (not a native <input type="checkbox">) — with
DIFFERENT starting states for the "record" and "replay" phases. Boxes
0, 1, 5 already sit at their recorded target state when replay starts;
a blind click-toggle would flip them to the WRONG state. Boxes 2 is never
interacted with at all (already correct at record time) and must stay
untouched. Boxes 3, 4 need an actual click at replay time too.
"""
import asyncio, os, sys, json, tempfile, urllib.request
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
Registry(systems=[SystemEntry(sid="CBX", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8099/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

from engine import recorder_service as rs

ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

# record-time defaults: [True, False, True, False, True, False]
# desired end-state (what the human clicks toward):
#   box0 True->False (click)   box1 False->True (click)
#   box2 stays True             (no click)
#   box3 False->True (click)   box4 True->False (click)
#   box5 False->True (click)
DESIRED = [False, True, True, True, False, True]
TO_CLICK = [0, 1, 3, 4, 5]

async def main():
    urllib.request.urlopen("http://127.0.0.1:8099/phase/record").read()

    s = await rs.start_recording("CBX", registry_path=os.environ["SYSTEMS_PATH"])
    check("1. Recording starts headless", s.status == "recording")
    await asyncio.sleep(0.8)

    boxes = s.page.locator('[role="checkbox"]')
    check("2. Six ARIA checkboxes present at record time", await boxes.count() == 6,
          f"count={await boxes.count()}")

    for i in TO_CLICK:
        bb = await boxes.nth(i).bounding_box()
        await rs.dispatch_input(s.recording_id, {"kind": "click", "x": bb["x"] + 5, "y": bb["y"] + 5})
        await asyncio.sleep(0.2)

    # sanity: the fake page's own click handler actually reached DESIRED
    for i in range(6):
        aria = await boxes.nth(i).get_attribute("aria-checked")
        check(f"3.{i}. Record-time page reached desired state (box{i})",
              (aria == "true") == DESIRED[i], f"aria-checked={aria}")

    res = await rs.stop_recording(s.recording_id, flow_id="e2e_checkbox_flow", flows_dir="flows")
    flow_json = json.loads(Path(res["path"]).read_text())
    set_steps = [st for st in flow_json["steps"] if st.get("act") == "set"]
    check("4. Recorded as SET steps with boolean values, not blind PRESS/click",
          len(set_steps) == len(TO_CLICK) and all(isinstance(st.get("value"), bool) for st in set_steps),
          json.dumps(set_steps)[:300])
    indices = sorted(st["target"]["dom"]["checkbox_index"] for st in set_steps)
    check("5. Each SET step carries a distinct checkbox_index",
          indices == sorted(TO_CLICK), f"{indices} vs {sorted(TO_CLICK)}")

    # ---- REPLAY against a page with DIFFERENT starting states ----
    urllib.request.urlopen("http://127.0.0.1:8099/phase/replay").read()

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
        await BasicLoginPlugin(SystemTarget("CBX", reg.get("CBX").webgui_url, "100"),
                                RegistryCredentialProvider(reg)).login(page)

        replay_boxes = page.locator('[role="checkbox"]')
        starting = [(await replay_boxes.nth(i).get_attribute("aria-checked")) == "true" for i in range(6)]
        check("6. Replay page genuinely starts DIFFERENT from record time",
              starting != [True, False, True, False, True, False], f"{starting}")

        pr = await probe(page)
        eng = ReplayEngine(page, pr.mode, "replay_cbx", "CBX", Path("run_artifacts/replay_cbx"))
        flow = Flow(**flow_json)
        result = await eng.run(flow)
        print(f"      replay ok={result.ok} err={result.error}")
        for st in result.steps:
            print(f"        step{st.index} {st.act} ok={st.ok} conf={st.confidence} {st.detail}"[:140])

        for i in range(6):
            aria = await replay_boxes.nth(i).get_attribute("aria-checked")
            got = (aria == "true")
            if i in TO_CLICK:
                check(f"7.{i}. Replay lands box{i} on ABSOLUTE recorded end-state "
                      f"(started {starting[i]}, want {DESIRED[i]})",
                      got == DESIRED[i], f"got {got}")
            else:
                check(f"7.{i}. Untouched box{i} left alone by replay (never a recorded step)",
                      got == starting[i], f"got {got}, started {starting[i]}")

        check("8. Overall replay result ok", result.ok, result.error or "")
        await b.close()

asyncio.run(main())
print("D-040 CHECKBOX RECORD+REPLAY: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
