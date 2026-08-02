"""D-038: scrolling is recorded and replayed, so captures differ."""
import asyncio, os, sys, json, hashlib, tempfile
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"]="/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"]=Fernet.generate_key().decode()
BACKEND="/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work=tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"]=str(Path(work)/"systems.json")
os.environ["RUNS_DB_PATH"]=str(Path(work)/"runs.db")
sys.path.insert(0,BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="SCR", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8096/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])
from engine import recorder_service as rs
ok=True
def check(n,c,d=""):
    global ok; ok=ok and bool(c)
    print(("PASS " if c else "FAIL ")+n+("" if c else f"  << {d}"))
def sha(p): return hashlib.sha1(Path(p).read_bytes()).hexdigest()[:12]

async def main():
    s = await rs.start_recording("SCR", registry_path=os.environ["SYSTEMS_PATH"])
    await asyncio.sleep(0.8)
    # capture at top
    rs.add_capture_step(s.recording_id)
    # scroll the inner grid via remote wheel, then capture
    box = await s.page.locator("#grid").bounding_box()
    cx, cy = box["x"]+box["width"]/2, box["y"]+box["height"]/2
    for _ in range(6):
        await rs.dispatch_input(s.recording_id, {"kind":"wheel","x":cx,"y":cy,"dx":0,"dy":400})
        await asyncio.sleep(0.12)
    await asyncio.sleep(0.6)                      # let the debounce fire
    pos1 = await s.page.locator("#pos").inner_text()
    stop1 = await s.page.locator("#grid").evaluate("el => el.scrollTop")
    check("1. Custom-scrollbar grid moved (scrollTop stays 0 — the D-037 trap)",
          pos1 != "offset 0" and stop1 == 0, f"{pos1}, scrollTop={stop1}")
    rs.add_capture_step(s.recording_id)
    # scroll further, capture again
    for _ in range(6):
        await rs.dispatch_input(s.recording_id, {"kind":"wheel","x":cx,"y":cy,"dx":0,"dy":400})
        await asyncio.sleep(0.12)
    await asyncio.sleep(0.6)
    pos2 = await s.page.locator("#pos").inner_text()
    check("2. Second scroll goes further", pos2 != pos1, f"{pos1} -> {pos2}")
    rs.add_capture_step(s.recording_id)

    steps = rs.status(s.recording_id)["steps"]
    scrolls = [x for x in steps if x["act"]=="scroll"]
    check("3. Scroll GESTURES were recorded as steps", len(scrolls) >= 2,
          json.dumps(steps))
    print("      steps:", json.dumps([f"{x['act']}:{x['target']}" for x in steps]))
    res = await rs.stop_recording(s.recording_id, flow_id="alv_scroll", flows_dir="flows")
    data = json.loads(Path(res["path"]).read_text())
    acts = [x["act"] for x in data["steps"]]
    check("4. Saved flow interleaves scroll and capture",
          acts.count("scroll") >= 2 and acts.count("capture") == 3, str(acts))

    # ---- REPLAY and compare the produced screenshots ----
    from playwright.async_api import async_playwright
    from engine.engine import ReplayEngine
    from engine.login.basic import BasicLoginPlugin
    from engine.login.base import SystemTarget
    from engine.registry import RegistryCredentialProvider
    from engine.probe import probe
    from engine.schema import Flow
    reg=Registry.load(os.environ["SYSTEMS_PATH"]); e=reg.get("SCR")
    async with async_playwright() as pw:
        b=await pw.chromium.launch(headless=True,args=["--no-sandbox"])
        page=await b.new_page(viewport={"width":1280,"height":800})
        await BasicLoginPlugin(SystemTarget("SCR", e.webgui_url,"100"),
                                RegistryCredentialProvider(reg)).login(page)
        pr=await probe(page)
        eng=ReplayEngine(page,pr.mode,"rep","SCR",Path("art"),
                          webgui_url=e.webgui_url, client=e.client)
        r=await eng.run(Flow(**data))
        for st in r.steps: print(f"        {st.index} {st.act} ok={st.ok} {st.detail}"[:100])
        shots=[st.screenshot_path for st in r.steps if st.screenshot_path]
        check("5. Three screenshots produced", len(shots)==3, str(shots))
        hashes=[sha(p) for p in shots]
        print("      screenshot hashes:", hashes)
        check("6. THE FIX: screenshots are DIFFERENT (scroll replayed)",
              len(set(hashes))==3, f"{hashes} — identical means scroll wasn't restored")
        await b.close()
asyncio.run(main())
print("D-038: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
