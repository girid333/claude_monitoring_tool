"""D-053: record and replay a business transaction the way eCATT/CBTA would.

Drives the REAL recorder (headless Chromium, input over CDP — exactly what
the Recorder page does) against fake_webgui_txn.py, then replays the saved
flow with the ReplayEngine. The fake copies F4H's real WebGUI markup (lsdata
SIDs, message bar, title) and is adversarial on purpose — see its docstring.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("LD_LIBRARY_PATH", "/tmp/stublib")
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"
BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from engine import crypto  # noqa: E402
from engine import recorder_service as rs  # noqa: E402
from engine.registry import Registry, SapCredential, SystemEntry  # noqa: E402
from engine.schema import Act, Flow  # noqa: E402

PORT = 8089
BASE = f"http://127.0.0.1:{PORT}"
WORK = Path(tempfile.mkdtemp())
SYSTEMS = WORK / "systems.json"


def fake(path):
    return json.loads(urllib.request.urlopen(BASE + path, timeout=5).read() or b"{}")


@pytest.fixture(scope="module", autouse=True)
def server():
    p = subprocess.Popen([sys.executable, str(BACKEND / "e2e" / "fake_webgui_txn.py"), str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            fake("/reset")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    Registry(systems=[SystemEntry(sid="TXN", client="100", hostname="127.0.0.1",
                                  webgui_url=f"{BASE}/webgui",
                                  sap_credential=SapCredential(username="U", password_enc=crypto.encrypt("p")))]
             ).save(str(SYSTEMS))
    yield
    p.kill()


def sel(sid):
    return f"[lsdata*='\"SID\":\"{sid}\"']"


async def click(s, locator, double=False):
    box = await locator.bounding_box()
    await rs.dispatch_input(s.recording_id, {"kind": "dblclick" if double else "click",
                                             "x": box["x"] + 6, "y": box["y"] + box["height"] / 2})
    await asyncio.sleep(0.3)


async def type_text(s, text):
    await rs.dispatch_input(s.recording_id, {"kind": "text", "text": text})
    await asyncio.sleep(0.2)


async def key(s, k, modifiers=0, settle=1.3):
    await rs.dispatch_input(s.recording_id, {"kind": "key", "key": k, "code": k, "modifiers": modifiers})
    await asyncio.sleep(settle)


async def record() -> dict:
    fake("/reset")
    s = await rs.start_recording("TXN", registry_path=str(SYSTEMS), tcode="ZPO")
    p = s.page
    try:
        await asyncio.sleep(0.8)
        await click(s, p.locator(sel("wnd[0]/usr/ctxtEKKO-LIFNR")))
        await type_text(s, "100000")
        await key(s, "Enter")                       # Enter straight after typing — no blur
        assert "Vendor 100000" in await p.inner_text("body")
        await click(s, p.locator(sel("wnd[0]/usr/tblSAPMZPOTC_ITEMS/ctxtITEM-MATNR[0,1]")))
        await type_text(s, "MAT-1")
        await click(s, p.locator(sel("wnd[0]/usr/tblSAPMZPOTC_ITEMS/txtITEM-MENGE[1,1]")))
        await type_text(s, "150")
        await click(s, p.locator(sel("wnd[0]/usr/chkEKKO-URGENT")))
        await click(s, p.locator("td[data-dbl='10']").first, double=True)
        await asyncio.sleep(1.2)
        assert "Display Contract 10" in await p.inner_text("body")
        await key(s, "F3")
        await key(s, "s", modifiers=2)              # Ctrl+S
        assert "Large Quantity" in await p.inner_text("body")
        await click(s, p.locator(sel("wnd[1]/tbar[0]/btn[0]")))
        await asyncio.sleep(1.2)
        await click(s, p.locator(sel("wnd[1]/usr/btnSPOP-OPTION1")))
        await asyncio.sleep(1.2)
        r = await rs.message_step(s.recording_id, "get_message", var="po", pattern=r"(\d+)")
        po = r["message"]["text"].split()[-1]
        await rs.message_step(s.recording_id, "check_message")
        await key(s, "F5", modifiers=8)             # Shift+F5
        await click(s, p.locator(sel("wnd[0]/usr/ctxtPO-EBELN")))
        await type_text(s, po)
        await key(s, "Enter")
        vb = await p.locator(sel("wnd[0]/usr/txtPO-LIFNR")).bounding_box()
        await rs.pick_step(s.recording_id, "get", vb["x"] + 5, vb["y"] + 5, var="vendor_read")
        await rs.pick_step(s.recording_id, "check_field", vb["x"] + 5, vb["y"] + 5)
        gb = await p.locator("table[role=grid] td").first.bounding_box()
        await rs.pick_step(s.recording_id, "check_rows", gb["x"] + 5, gb["y"] + 5)
        assert fake("/state")["screen"] == "display"   # picking clicked nothing
        res = await rs.stop_recording(s.recording_id, "zpo_create", flows_dir=str(WORK / "flows"))
    except Exception:
        await rs.cancel_recording(s.recording_id)
        raise
    return {"path": res["path"], "po": po}


RECORDED: dict = {}


def recorded_flow() -> dict:
    if not RECORDED:
        RECORDED.update(asyncio.run(record()))
    return json.loads(Path(RECORDED["path"]).read_text())


async def _replay(flow_dict: dict, tag: str):
    from playwright.async_api import async_playwright

    from engine.engine import ReplayEngine
    from engine.login.base import SystemTarget
    from engine.login.basic import BasicLoginPlugin
    from engine.registry import RegistryCredentialProvider
    reg = Registry.load(str(SYSTEMS))
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await (await b.new_context(viewport={"width": 1280, "height": 800})).new_page()
        await BasicLoginPlugin(SystemTarget("TXN", f"{BASE}/webgui", "100"),
                               RegistryCredentialProvider(reg)).login(page)
        eng = ReplayEngine(page, "dom", f"r_{tag}", "TXN", WORK / "art" / tag,
                           webgui_url=f"{BASE}/webgui", client="100")
        fr = await eng.run(Flow(**flow_dict))
        await b.close()
        return fr


def replay(flow_dict, tag):
    return asyncio.run(_replay(flow_dict, tag))


def steps_of(flow, act):
    return [(i, s) for i, s in enumerate(flow["steps"]) if s["act"] == act]


# ── what the recorder captured ─────────────────────────────────────────
def test_value_is_recorded_before_the_enter_that_submits_it():
    f = recorded_flow()
    acts = [(s["act"], s.get("target", {}).get("sid"), s.get("key")) for s in f["steps"]]
    i_set = next(i for i, a in enumerate(acts) if a[1] == "wnd[0]/usr/ctxtEKKO-LIFNR")
    i_enter = next(i for i, a in enumerate(acts) if a[0] == "vkey" and a[2] == "Enter")
    assert i_set < i_enter, acts
    assert f["steps"][i_set]["value"] == "100000"


def test_sap_ids_are_recorded_from_lsdata():
    f = recorded_flow()
    sids = {s.get("target", {}).get("sid") for s in f["steps"]}
    for want in ("wnd[0]/usr/ctxtEKKO-LIFNR", "wnd[0]/usr/tblSAPMZPOTC_ITEMS/ctxtITEM-MATNR[0,1]",
                 "wnd[0]/usr/chkEKKO-URGENT", "wnd[1]/usr/btnSPOP-OPTION1"):
        assert want in sids, sids
    assert not any(s and s.startswith("wnd[0]/usr") and s.endswith("usr") for s in sids)


def test_keys_with_modifiers_become_sap_vkeys():
    f = recorded_flow()
    keys = [(s["key"], s["value"]) for _, s in steps_of(f, "vkey")]
    assert ("Control+S", 11) in keys and ("Shift+F5", 17) in keys and ("F3", 3) in keys, keys


def test_double_click_is_one_step_with_a_cell_position():
    f = recorded_flow()
    (i, d), = steps_of(f, "dblclick")
    assert f["steps"][i - 1]["act"] != "press", "the clicks of a double-click must not be steps"
    cell = d["target"]["dom"]["cell"]
    assert cell["row"] == 0 and cell["column"] == "Contract"


def test_dialog_steps_and_screens_are_recorded():
    f = recorded_flow()
    yes = next(s for s in f["steps"] if s.get("target", {}).get("sid") == "wnd[1]/usr/btnSPOP-OPTION1")
    assert yes["target"]["dom"]["window"] == "wnd[1]" and yes["screen"] == "Save Document"
    v = next(s for s in f["steps"] if s.get("target", {}).get("sid") == "wnd[0]/usr/ctxtEKKO-LIFNR")
    assert v["screen"] == "Create Purchase Order: Initial Screen"


def test_variables_and_checks_are_recorded():
    f = recorded_flow()
    gets = [s for _, s in steps_of(f, "get")]
    assert {g["var"] for g in gets} == {"po", "vendor_read"}
    checks = {s["type"] for _, s in steps_of(f, "check")}
    assert checks == {"statusbar_type", "field_value", "row_count"}


# ── replay ─────────────────────────────────────────────────────────────
def _with_po_variable(f):
    f = json.loads(json.dumps(f))
    po_set = next(s for s in f["steps"] if s.get("target", {}).get("sid") == "wnd[0]/usr/ctxtPO-EBELN")
    po_set["value"] = "{{po}}"
    return f


def test_replay_creates_the_document_and_reads_it_back():
    flow = _with_po_variable(recorded_flow())
    fake("/reset")
    fr = replay(flow, "ok")
    assert fr.ok, (fr.error, [(s.index, s.act, s.detail) for s in fr.steps])
    state = fake("/state")
    assert len(state["pos"]) == 1
    po = next(iter(state["pos"]))
    assert fr.variables == {"po": po, "vendor_read": "100000"}
    assert state["pos"][po]["items"] == [["MAT-1", "150"]] and state["pos"][po]["urgent"]
    assert all(c.ok for c in fr.checks) and len(fr.checks) == 3
    sets = [s for s in fr.steps if s.act == Act.SET]
    assert all(s.detail.startswith("sap_id:") for s in sets), [s.detail for s in sets]
    assert "Wrong 'Yes'" not in json.dumps(state)


def test_missing_dialog_fails_clearly_then_optional_skips_it():
    f = recorded_flow()
    f2 = _with_po_variable(f)
    qty = next(s for s in f2["steps"] if s.get("target", {}).get("sid", "").endswith("txtITEM-MENGE[1,1]"))
    qty["value"] = "5"                              # no "Large Quantity" dialog this time
    fake("/reset")
    fr = replay(f2, "nodialog")
    assert not fr.ok and "expected screen 'Large Quantity' but the session is on 'Save Document'" in fr.error, fr.error
    assert fr.steps[-1].screenshot_path and Path(fr.steps[-1].screenshot_path).exists()

    cont = next(s for s in f2["steps"] if s.get("target", {}).get("sid") == "wnd[1]/tbar[0]/btn[0]")
    cont["optional"] = True
    fake("/reset")
    fr = replay(f2, "optional")
    assert fr.ok, fr.error
    assert any(s.skipped for s in fr.steps)
    assert len(fake("/state")["pos"]) == 1


def test_sap_error_message_stops_the_run():
    f = json.loads(json.dumps(recorded_flow()))
    v = next(s for s in f["steps"] if s.get("target", {}).get("sid") == "wnd[0]/usr/ctxtEKKO-LIFNR")
    v["value"] = "200000"
    fake("/reset")
    fr = replay(f, "blocked")
    assert not fr.ok
    assert "SAP error message: Vendor 200000 is blocked" in fr.error, fr.error
    assert fr.steps[-1].act == Act.VKEY


def test_unexpected_dialog_is_reported_not_clicked_through():
    recorded_flow()
    fake("/reset")
    fake("/fault/popup")
    fr = replay(_with_po_variable(recorded_flow()), "surprise")
    assert not fr.ok and "unexpected dialog is open ('System Message')" in fr.error, fr.error
    assert fake("/state")["pos"] == {}


def test_failed_check_fails_the_run_but_keeps_evidence():
    f = _with_po_variable(recorded_flow())
    chk = next(s for s in f["steps"] if s["act"] == "check" and s["type"] == "field_value")
    chk["value"] = "999999"
    fake("/reset")
    fr = replay(f, "badcheck")
    assert fr.error is None and not fr.ok
    bad = [c for c in fr.checks if not c.ok]
    assert len(bad) == 1 and bad[0].actual == "100000"


# ── units ──────────────────────────────────────────────────────────────
def test_vkey_mapping_round_trips():
    from engine.resolvers.dom import key_to_vkey, vkey_to_key
    for k in ("Enter", "F2", "F8", "Shift+F5", "Control+F1", "Control+S", "Escape", "PageDown"):
        assert vkey_to_key(key_to_vkey(k)) == k
    assert key_to_vkey("Ctrl+S") == 11 and key_to_vkey("x") is None


def test_title_normalisation_ignores_document_numbers():
    from engine.webgui_dom import normalize_title
    assert normalize_title("Change PO 4500001234") == normalize_title("change  po 4500009999")
    assert normalize_title("Display Contract") != normalize_title("Change Contract")


def test_get_pattern_and_flow_defaults():
    f = Flow(flow_id="x", steps=[])
    assert f.stop_on_error_message is True


# ── data-driven run + API ──────────────────────────────────────────────
async def _data_run(flow_dict, rows):
    from playwright.async_api import async_playwright

    from engine.datarun import Binding
    from engine.datarun_exec import execute_data_run
    from engine.login.base import SystemTarget
    from engine.login.basic import BasicLoginPlugin
    from engine.registry import RegistryCredentialProvider
    reg = Registry.load(str(SYSTEMS))
    vendor_idx = next(i for i, s in enumerate(flow_dict["steps"])
                      if s.get("target", {}).get("sid") == "wnd[0]/usr/ctxtEKKO-LIFNR")
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await (await b.new_context(viewport={"width": 1280, "height": 800})).new_page()
        await BasicLoginPlugin(SystemTarget("TXN", f"{BASE}/webgui", "100"),
                               RegistryCredentialProvider(reg)).login(page)
        res = await execute_data_run(page=page, mode="dom", run_id="dr", sid="TXN", flow_dict=flow_dict,
                                     bindings=[Binding(step_index=vendor_idx, column="Vendor")],
                                     rows=rows, webgui_url=f"{BASE}/webgui", client="100",
                                     artifact_root=str(WORK / "dr"))
        await b.close()
        return res


def test_data_run_exports_each_rows_document_number():
    flow = _with_po_variable(recorded_flow())
    fake("/reset")
    res = asyncio.run(_data_run(flow,
                                [{"Vendor": "100000"}, {"Vendor": "200000"}, {"Vendor": "300000"}]))
    it = res.iterations
    # row 3 creates its PO but fails the recorded check "vendor is 100000"
    # (a check records the sample value; it is the tester's to parameterise)
    assert [i.ok for i in it] == [True, False, False], [(i.ok, i.error) for i in it]
    assert "check failed: field value is '100000' (found '300000'" in it[2].error
    pos = fake("/state")["pos"]
    assert sorted(pos) == sorted([it[0].exports["po"], it[2].exports["po"]])
    assert pos[it[2].exports["po"]]["vendor"] == "300000"
    assert "Vendor 200000 is blocked" in it[1].error
    assert any(p.endswith("_FAILED.png") for p in it[1].screenshots)


def test_optional_flag_api_and_pick_validation():
    from fastapi.testclient import TestClient
    os.environ["FLOWS_DIR"] = str(WORK / "flows")
    import importlib

    import main
    importlib.reload(main)
    c = TestClient(main.app)
    recorded_flow()
    r = c.patch("/api/flows/zpo_create/steps/2", json={"optional": True})
    assert r.status_code == 200 and r.json()["steps"][2]["optional"] is True
    assert json.loads((WORK / "flows" / "zpo_create.json").read_text())["steps"][2]["optional"] is True
    c.patch("/api/flows/zpo_create/steps/2", json={"optional": False})
    assert c.patch("/api/flows/zpo_create/steps/999", json={"optional": True}).status_code == 400
    assert c.patch("/api/flows/..%2Fx/steps/1", json={"optional": True}).status_code in (400, 404)
    assert c.post("/api/recorder/pick", json={"recording_id": "nope", "kind": "get", "x": 1, "y": 1,
                                              "var": "v"}).status_code == 404


# ── review regressions (2026-10-06) ────────────────────────────────────
def test_only_a_check_that_accepts_the_error_exempts_it():
    from engine.engine import ReplayEngine
    from engine.schema import CheckOp, Step
    f = Flow(flow_id="x", steps=[Step(act=Act.PRESS), Step(act=Act.WAIT_READY),
                                 Step(act=Act.CHECK, type="statusbar_type", op=CheckOp.EQ, value="S")])
    assert not ReplayEngine._next_check_expects(f, 0, {"type": "E", "text": "blocked"})
    f.steps[2].value = "E"
    assert ReplayEngine._next_check_expects(f, 0, {"type": "E", "text": "blocked"})


def test_more_key_combos_map_to_vkeys():
    from engine.resolvers.dom import key_to_vkey, vkey_to_key
    assert key_to_vkey("Control+PageDown") == 83 and key_to_vkey("Control+PageUp") == 80
    assert key_to_vkey("Control+Shift+F5") == 41 and vkey_to_key(41) == "Control+Shift+F5"


def test_patch_writes_the_file_it_read_and_flow_names_are_safe():
    from fastapi.testclient import TestClient
    import main
    recorded_flow()
    src = WORK / "flows" / "zpo_create.json"
    copy = WORK / "flows" / "zpo_copy.json"
    copy.write_text(src.read_text())          # flow_id inside still says zpo_create
    c = TestClient(main.app)
    os.environ["FLOWS_DIR"] = str(WORK / "flows")
    main.FLOWS_DIR = str(WORK / "flows")
    assert c.patch("/api/flows/zpo_copy/steps/2", json={"optional": True}).status_code == 200
    assert json.loads(copy.read_text())["steps"][2]["optional"] is True
    assert json.loads(src.read_text())["steps"][2].get("optional") is not True
    copy.unlink()
    r = c.post("/api/recorder/stop", json={"recording_id": "x", "flow_id": "../escape"})
    assert r.status_code == 400


def test_every_browser_snippet_parses():
    """A JS syntax error inside a snippet is swallowed at replay (the lookup
    just 'misses'), so parse them all here."""
    import engine.webgui_dom as w
    from engine.recorder.recorder import _INJECT_JS

    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            p = await b.new_page()
            bad = {}
            for name in ("SCREEN_JS", "STATUSBAR_JS", "MARK_POPUP_JS", "MARK_SID_JS", "MARK_CELL_JS",
                         "READ_TABLE_JS", "VALUE_JS"):
                err = await p.evaluate("src => { try { new Function('return ' + src); return null } "
                                       "catch (e) { return String(e) } }", getattr(w, name))
                if err:
                    bad[name] = err
            err = await p.evaluate("src => { try { new Function(src); return null } catch (e) { return String(e) } }",
                                   _INJECT_JS)
            if err:
                bad["_INJECT_JS"] = err
            await b.close()
            return bad
    assert asyncio.run(go()) == {}


def test_cell_is_found_by_position_after_its_text_changed():
    from engine.resolvers.dom import DomResolver
    from engine.schema import DomCandidates, Target

    async def go():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            p = await b.new_page()
            html = ('<table><tr><th>Material</th><th>Qty</th></tr><tr><td>MAT-A</td><td>1</td></tr></table>'
                    '<table><tr><th>Partner</th><th>Name</th></tr><tr><td>P1</td><td>{}</td></tr>'
                    '<tr><td>P2</td><td>Other</td></tr></table>')
            await p.set_content(html.format("ACME Corp"))
            cell = await p.evaluate("() => {" + __import__("engine.webgui_dom", fromlist=["x"]).HELPERS_JS
                                    + " return __cellOf(document.querySelectorAll('table')[1].rows[1].cells[1]); }")
            await p.set_content(html.format("Globex Ltd"))
            r = DomResolver(p)
            res = await r.resolve(Target(dom=DomCandidates(tag="td", text="ACME Corp", cell=cell, window="main")))
            txt = await res.handle.inner_text()
            await b.close()
            return res.via, txt
    via, txt = asyncio.run(go())
    assert via.startswith("cell:") and txt == "Globex Ltd"
