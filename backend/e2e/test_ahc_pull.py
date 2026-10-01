"""D-051 Phase 2: pull the Automated Health Check straight from SAP over
SOAP RFC, against fake_sap_ahc_pull.py (a small adversarial SAP system with
KNOWN findings).

Proves:
  - the generated workbook has exactly the sample's structure (sheet order,
    table headers, the 33 Monitors rows) and parses through backend/health/
    with no analysis errors;
  - the findings are the true ones — including entries a container-clock
    (UTC) pull would have missed on a UTC+5:30 system;
  - not-collected checks are no_data, never OK;
  - only allowlisted, read-only function modules are called;
  - POST /api/health/pull stores the report and the workbook export returns
    the exact bytes; no credential appears in any response or stored file.
Set HEALTH_REAL_AHC=/path/to/SAP_export.XLS to also compare the structure
against a real SAP export.
"""
import io
import json
import os
import re
import zipfile
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ["AHC_RFC_TIMEOUT"] = "1.5"        # the fake's zombie destination sleeps 8 s
os.environ["AHC_RFC_SKIP"] = "Z_SKIP*"
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"

from ahc.layout_check import excel_problems, sheet_signature  # noqa: E402
from health.xlsx_export import XLSX_MIME, spreadsheetml_to_xlsx  # noqa: E402
from ahc.run import pull  # noqa: E402
from ahc.sap import READ_ONLY_FMS  # noqa: E402
from engine.rfc.soap_client import RfcSoapClient, RfcSoapError  # noqa: E402
from health.analyze import analyze  # noqa: E402
from health.workbook import read_workbook  # noqa: E402

PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
WEBGUI = f"{BASE}/sap/bc/gui/sap/its/webgui"
USER, PASSWORD = "MONITOR", "secret"
LAYOUT = __import__("json").loads((BACKEND / "ahc" / "layout.json").read_text())
NS = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}


def xlsx_text(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return b"".join(z.read(n) for n in z.namelist())


def assert_valid_xlsx(data: bytes, sheets=None):
    """A real .xlsx: a zip whose parts are well-formed XML, that openpyxl
    loads, with every AHC tab in order and the Monitors styling intact."""
    from openpyxl import load_workbook
    assert data[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        for n in z.namelist():
            if n.endswith(".xml") or n.endswith(".rels"):
                ET.fromstring(z.read(n))
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == (sheets or [s["name"] for s in LAYOUT["sheets"]])
    return wb


def fake(path: str):
    return urllib.request.urlopen(BASE + path, timeout=5).read()


@pytest.fixture(scope="module", autouse=True)
def server():
    p = subprocess.Popen([sys.executable, str(BACKEND / "e2e" / "fake_sap_ahc_pull.py"), str(PORT)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            fake("/reset")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    yield
    p.kill()


def client(password=PASSWORD):
    return RfcSoapClient.from_system(WEBGUI, client="100", username=USER, password=password, timeout_s=20)


@pytest.fixture(scope="module")
def pulled(server):
    fake("/reset")
    c = client()
    try:
        data, meta = pull(c, "100")
    finally:
        c.close()
    calls = __import__("json").loads(fake("/calls"))
    rep = analyze(read_workbook(data, "pull.XLS"), "pull.XLS")
    return data, meta, rep, calls


def by_sheet(rep):
    return {c.sheet: c for c in rep.checks}


# ── structure ────────────────────────────────────────────────────────────
def test_sheet_order_and_headers_match_sample_layout(pulled):
    data = pulled[0]
    root = ET.fromstring(data)
    names = [w.get(f"{{{NS['ss']}}}Name") for w in root.findall("ss:Worksheet", NS)]
    assert names == [s["name"] for s in LAYOUT["sheets"]]
    sheets = read_workbook(data, "pull.XLS")
    for s in LAYOUT["sheets"]:
        if s["name"] == "Monitors" or not s["tables"]:
            continue
        got = [t.columns for t in sheets[s["name"]].tables]
        for want in s["tables"]:
            assert want in got, f"{s['name']}: header {want} missing; got {got}"


def test_monitors_rows_are_the_33_sample_checks(pulled):
    rep = pulled[2]
    want = [(m["task"], m["tcode"]) for m in LAYOUT["monitors"]]
    assert [(c.task, c.tcode) for c in rep.checks] == want
    assert rep.summary.total == 33


def test_every_sheet_keeps_sap_row_layout(pulled):
    """Title, subtitle, blank, header on row 5 — the signature SAP's own
    export has and the D-050 parser relies on."""
    for name, sig in sheet_signature(pulled[0]).items():
        if name == "Monitors":
            continue
        assert sig["header_row"] == (4 if name == "SM51" else 6 if name == "SM37" else 5), (name, sig)
        assert sig["title_style"] == "s73", (name, sig)


def test_parses_with_no_analysis_errors(pulled):
    rep = pulled[2]
    bad = [(c.sheet, c.analysis_note) for c in rep.checks
           if c.analysis_note and "could not be analysed" in c.analysis_note.lower()]
    assert not bad
    assert rep.system.sid == "QAS" and rep.system.client == "100"
    assert re.match(r"\d{4}-\d{2}-\d{2}", rep.report_date or "")


# ── findings ─────────────────────────────────────────────────────────────
def test_known_findings_are_reported(pulled):
    c = by_sheet(pulled[2])
    assert c["SM21"].sap_status == "not_ok" and c["SM21"].triage == "act"   # AB0 1 h ago (SAP time)
    assert c["ST22"].sap_status == "not_ok" and "1 ABAP" in c["ST22"].headline
    assert c["SM12"].sap_status == "not_ok"                                 # 30 h old VBAK lock
    assert c["SP01"].sap_status == "not_ok"
    assert c["SM37"].sap_status == "not_ok"
    assert c["ST02"].sap_status == "not_ok"                                 # PXA 90 % hit ratio
    assert c["SMLG"].sap_status == "not_ok"                                 # DEV instance in group
    assert c["Cert_Info"].sap_status == "not_ok" and "example.test" in c["Cert_Info"].headline
    assert c["RZ04"].sap_status == "not_ok"                                 # DUMMY op mode
    assert c["SLICENSE"].sap_status == "not_ok" and c["SLICENSE"].triage == "act"     # expired Maintenance_HDB
    for ok in ("SM51", "SM13", "SRT_MONI", "SOST", "ST06", "DB12", "DB02_Memory",
               "SCC4", "RZ12", "ST11", "DB01", "SMGW"):
        assert c[ok].sap_status == "ok", (ok, c[ok].headline)


def test_windows_use_sap_clock_not_container_clock(pulled):
    sheets = read_workbook(pulled[0], "pull.XLS")
    sm21 = " ".join(" ".join(r) for t in sheets["SM21"].tables for r in t.rows)
    assert "CONVT_NO_NUMBER" in sm21 and "OLD_DUMP" not in sm21
    st22 = " ".join(" ".join(r) for t in sheets["ST22"].tables for r in t.rows)
    assert "CONVT_NO_NUMBER" in st22 and "OLD_DUMP" not in st22


def test_not_collected_checks_are_no_data_never_ok(pulled):
    c = by_sheet(pulled[2])
    for s in ("SM04",):
        assert c[s].sap_status == "unknown" and c[s].triage == "no_data", (s, c[s].triage)
    assert pulled[1]["not_collected"] == 1


def test_only_read_only_modules_called(pulled):
    """Everything is a read, except the XBP modules that run SICK's report as
    one background job — and that job is deleted again."""
    from ahc.sap import JOB_FMS, TEST_FMS
    calls = pulled[3]
    allowed = {f.replace("/", "_-") for f in READ_ONLY_FMS} | TEST_FMS
    assert set(calls) - set(JOB_FMS) <= allowed, set(calls) - allowed - set(JOB_FMS)
    assert calls.count("BAPI_XBP_JOB_OPEN") == 1 and calls.count("BAPI_XBP_JOB_DELETE") == 1
    assert "_-SDF_-SLIC_READ_LICENSES" in calls                     # namespaced FM encoded
    assert pulled[1]["calls"] == len(calls)


def test_hana_tabs_without_sm69_command_say_why(server):
    fake("/reset")
    fake("/hana/off")
    c = client()
    try:
        data, meta = pull(c, "100")
    finally:
        c.close()
        fake("/reset")
    rep = by_sheet(analyze(read_workbook(data, "pull.XLS"), "pull.XLS"))
    for s in ("DB12", "DB02_Alerts", "DB02_Log", "DB02_Memory", "DB01"):
        assert rep[s].triage == "no_data" and rep[s].sap_status == "unknown", s
    assert any("ZAHC_HDBSQL" in n for n in meta["notes"]) or "ZAHC_HDBSQL" in data.decode("utf-8", "ignore")
    assert meta["not_collected"] == 6


def test_wrong_password_raises_not_half_report(server):
    c = client("wrong")
    with pytest.raises(RfcSoapError):
        try:
            pull(c, "100")
        finally:
            c.close()


# ── API ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_STORAGE_DIR", str(tmp_path / "health"))
    monkeypatch.setenv("SYSTEMS_PATH", str(tmp_path / "systems.json"))
    monkeypatch.setenv("REGISTRY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from engine import crypto
    from engine.registry import Registry, SapCredential, SystemEntry
    Registry(systems=[
        SystemEntry(sid="QAS", client="100", hostname="127.0.0.1", webgui_url=WEBGUI,
                    sap_credential=SapCredential(username=USER, password_enc=crypto.encrypt(PASSWORD))),
        SystemEntry(sid="BAD", client="100", hostname="127.0.0.1", webgui_url=WEBGUI,
                    sap_credential=SapCredential(username=USER, password_enc=crypto.encrypt("nope"))),
    ]).save(os.environ["SYSTEMS_PATH"])
    import health.router as R
    R._store = None
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ahc.api import router as ahc_router
    app = FastAPI()
    app.include_router(R.router)
    app.include_router(ahc_router)
    return TestClient(app), tmp_path


def test_api_pull_store_and_export(api):
    tc, tmp = api
    r = tc.post("/api/health/pull", json={"sid": "QAS"})
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["source"] == "sap_pull" and rep["has_workbook"] is True
    assert rep["summary"]["total"] == 33
    lst = tc.get("/api/health/reports").json()
    assert lst[0]["id"] == rep["id"] and lst[0]["source"] == "sap_pull"
    wb = tc.get(f"/api/health/reports/{rep['id']}/workbook")
    assert wb.status_code == 200
    assert re.search(r'attachment; filename="[^"]+\.xlsx"', wb.headers["content-disposition"])
    assert wb.headers["content-type"] == XLSX_MIME
    assert_valid_xlsx(wb.content)
    # the export re-imports to the same verdicts
    again = analyze(read_workbook(wb.content, "x.xlsx"), "x.xlsx")
    assert [c.sap_status for c in again.checks] == [c["sap_status"] for c in rep["checks"]]
    assert [c.triage for c in again.checks] == [c["triage"] for c in rep["checks"]]
    # the original SpreadsheetML is still available, and loads in Excel
    raw = tc.get(f"/api/health/reports/{rep['id']}/workbook?format=xls")
    assert raw.headers["content-disposition"].endswith('.XLS"') and raw.content[:5] == b"<?xml"
    assert excel_problems(raw.content) == []
    assert tc.get(f"/api/health/reports/{rep['id']}/workbook?format=pdf").status_code == 400
    # no credential anywhere
    blobs = [r.content, xlsx_text(wb.content), raw.content] + [p.read_bytes() for p in (tmp / "health").iterdir()]
    for b in blobs:
        assert PASSWORD.encode() not in b and b"Authorization" not in b
    assert tc.delete(f"/api/health/reports/{rep['id']}").status_code == 204
    assert not list((tmp / "health").glob(f"{rep['id']}.*"))


def test_api_errors_are_actionable(api):
    tc, _ = api
    r = tc.post("/api/health/pull", json={"sid": "NOPE"})
    assert r.status_code == 404 and "Systems page" in r.json()["detail"]
    r = tc.post("/api/health/pull", json={"sid": "BAD"})
    assert r.status_code == 502 and "BAD" in r.json()["detail"]
    assert "nope" not in r.text
    assert tc.get("/api/health/reports").json() == []


def test_imported_file_is_exportable_too(api):
    tc, _ = api
    sys.path.insert(0, str(BACKEND / "e2e" / "fixtures"))
    from make_ahc_fixture import build
    raw = build().encode()
    rep = tc.post("/api/health/reports", files={"file": ("ahc.xls", raw, "application/vnd.ms-excel")}).json()
    assert rep["source"] == "import" and rep["has_workbook"] is True
    x = tc.get(f"/api/health/reports/{rep['id']}/workbook")
    assert x.headers["content-type"] == XLSX_MIME and x.headers["content-disposition"].endswith('ahc.xlsx"')
    assert_valid_xlsx(x.content, sheets=list(read_workbook(raw, "ahc.xls")))
    assert tc.get(f"/api/health/reports/{rep['id']}/workbook?format=xls").content == raw


# ── optional: against a real SAP export ──────────────────────────────────
@pytest.mark.skipif(not os.environ.get("HEALTH_REAL_AHC"), reason="real export not provided")
def test_structure_matches_real_sap_export(pulled):
    real = Path(os.environ["HEALTH_REAL_AHC"]).read_bytes()
    a, b = sheet_signature(real), sheet_signature(pulled[0])
    assert list(a) == list(b)
    for name in a:
        assert a[name]["header_row"] == b[name]["header_row"], name
        assert a[name]["title_style"] == b[name]["title_style"], name
    ra, rb = read_workbook(real, "r.XLS"), read_workbook(pulled[0], "p.XLS")
    for name in ra:
        assert [t.columns for t in ra[name].tables][:1] == [t.columns for t in rb[name].tables][:1] \
            or name in ("Monitors",), name


# ── HANA through SM69 hdbsql: behaviour seen on F4H ──────────────────────
def test_hdbsql_line_parser():
    from ahc.hana import _split
    assert _split('|"2026-10-01 08:10"|33|"say ""hi"" | there"|?|') == ["2026-10-01 08:10", "33", 'say "hi" | there', ""]
    assert _split("|PING|") == ["PING"]
    assert _split('|"mode \\"normal\\". C:\\\\x"|') == ['mode "normal". C:\\x']      # hdbsql's escaping


def test_hana_tabs_complete_despite_128_char_protocol_lines(pulled):
    """SXPG cuts every output line at 128 chars (and drops trailing blanks);
    long alert texts and the 28-column availability log must still arrive whole."""
    sheets = read_workbook(pulled[0], "pull.XLS")
    alerts = sheets["DB02_Alerts"].tables[0].rows
    first = alerts[0]
    assert first[2].endswith("so it must be read in chunks.") and len(first[2]) > 128
    assert first[3].startswith('Set the log mode to "normal".') and first[3].endswith("before next run.")
    assert first[4] == "High"
    assert alerts[1][3] == "Change the password" and alerts[1][4] == "Medium"
    log = sheets["DB02_Log"].tables[0].rows
    assert len(log) == 2 and all(len(r) == 28 for r in log)
    assert log[-1][4] == "SERVICE_STARTED" and log[-1][-1] == "0"          # newest last, last column read
    db12 = sheets["DB12"].tables[0].rows
    assert [r[6] for r in db12] == ["topology", "volume"] and db12[1][10] == "279810408448"
    assert db12[0][2] == "00:05:00"
    c = by_sheet(pulled[2])
    assert c["DB02_Alerts"].sap_status == "not_ok" and c["DB02_Alerts"].triage == "act"


def test_hana_sql_never_quoted_or_too_long(server):
    """F4H: quoting the SQL is a syntax error (SXPG passes quotes verbatim);
    more than ~230 chars is TOO_MANY_PARAMETERS. The reader must avoid both."""
    from ahc.hana import MAX_SQL, HanaViaSm69
    from ahc.sap import Sap
    c = client()
    try:
        h = HanaViaSm69(Sap(c))
        sent = []
        orig = h._run
        h._run = lambda s: (sent.append(s), orig(s))[1]
        assert h.available()
        h.table(["C.SYS_START_TIME", "C.MESSAGE", "F.DESTINATION_PATH", "F.BACKUP_SIZE"],
                "FROM M_BACKUP_CATALOG C JOIN M_BACKUP_CATALOG_FILES F ON C.ENTRY_ID=F.ENTRY_ID "
                "WHERE C.ENTRY_TYPE_NAME='complete data backup' ORDER BY C.ENTRY_ID DESC, F.SOURCE_ID", top=10)
        assert sent and all(not s.startswith('"') and len(s) <= MAX_SQL for s in sent)
        with pytest.raises(ValueError):
            h.query("SELECT 1 FROM DUMMY WHERE 'x' = '" + "x" * 300 + "'")
    finally:
        c.close()


def test_slicense_from_saplikey_matches_slicense_screen(pulled):
    """SAPLIKEY: LIKEY = LK|SID|hw key|product, VALUE (NCLOB, printed cut to 32
    chars by hdbsql) = limit|type|signature|inst no|start|end|system no."""
    rows = read_workbook(pulled[0], "pull.XLS")["SLICENSE"].tables[0].rows
    assert [(r[2], r[3], r[6], r[10]) for r in rows] == [
        ("J1", "Maintenance_HDB", "12/01/2026", "Expired"),
        ("J1", "NetWeaver_HDB", "31/12/9999", "Valid"),
        ("H2", "NetWeaver_HDB", "31/12/9999", "Valid (inactive Hardware Key)")]
    r = rows[1]
    assert r[1] == "QAS" and r[4] == "2147483647" and r[5] == "11/10/2025" and r[7] == "Perm"
    assert r[8] == "0021277862" and r[9] == "000000000801035261"
    c = by_sheet(pulled[2])["SLICENSE"]
    assert "Maintenance_HDB" in " ".join(c.findings)


def test_slicense_falls_back_to_sap_module_without_hana(server):
    fake("/reset")
    fake("/hana/off")
    c = client()
    try:
        data, _ = pull(c, "100")
    finally:
        c.close()
        fake("/reset")
    rows = read_workbook(data, "pull.XLS")["SLICENSE"].tables[0].rows
    assert [(r[3], r[10]) for r in rows] == [("NetWeaver_HDB", "Valid")]


# ── SICK: report RSICC000 as a background job (XBP) ──────────────────────
def test_sick_from_rsicc000_job(pulled):
    sick = read_workbook(pulled[0], "pull.XLS")["SICK"]
    assert "|no errors reported" in sick.text and "|SAP System Check" in sick.text
    c = by_sheet(pulled[2])["SICK"]
    assert c.sap_status == "ok" and c.triage == "ok"
    assert json.loads(fake("/jobs")) == {}                    # job deleted after reading the list
    sig = sheet_signature(pulled[0])["SICK"]
    assert sig["header_row"] == 5


def test_sick_error_is_not_ok(server):
    fake("/reset")
    fake("/sick/error")
    c = client()
    try:
        data, _ = pull(c, "100")
    finally:
        c.close()
        fake("/reset")
    rep = by_sheet(analyze(read_workbook(data, "pull.XLS"), "pull.XLS"))
    assert rep["SICK"].sap_status == "not_ok"
    assert "not supported with kernel" in read_workbook(data, "pull.XLS")["SICK"].text
    assert json.loads(fake("/jobs")) == {}


def test_jobs_can_be_switched_off(server, monkeypatch):
    monkeypatch.setenv("AHC_NO_JOBS", "1")
    fake("/reset")
    c = client()
    try:
        data, meta = pull(c, "100")
    finally:
        c.close()
    calls = json.loads(fake("/calls"))
    assert not [f for f in calls if f.startswith("BAPI_X")]
    rep = by_sheet(analyze(read_workbook(data, "pull.XLS"), "pull.XLS"))
    assert rep["SICK"].triage == "no_data" and "AHC_NO_JOBS" in rep["SICK"].headline + rep["SICK"].sap_evidence


def test_only_allowlisted_reports_can_run(server):
    from ahc.sap import Sap
    from ahc.xbp import run_report
    c = client()
    try:
        with pytest.raises(ValueError):
            run_report(Sap(c), "RSBDCREO", "X")
    finally:
        c.close()


# ── SMMS: message server services via sapcontrol (SM69 ZAHC_SAPCONTROL) ──
def test_smms_from_sapcontrol(pulled):
    rows = read_workbook(pulled[0], "pull.XLS")["SMMS"].tables[0].rows
    assert rows == [
        ["0", "HTTP", "8101", "", "0", "", "Opened", "20", "60", "", "", "", "ms/server_port_0", "", ""],
        ["1", "HTTPS", "44401", "", "0", "", "Opened", "30", "120", "", "", "", "ms/server_port_1", "", ""]]
    assert by_sheet(pulled[2])["SMMS"].sap_status == "ok"


def test_smms_closed_port_is_not_ok_and_missing_command_is_no_data(server):
    for switch, check in (("/port/closed", "not_ok"), ("/sapctl/off", "unknown")):
        fake("/reset")
        fake(switch)
        c = client()
        try:
            data, _ = pull(c, "100")
        finally:
            c.close()
            fake("/reset")
        smms = by_sheet(analyze(read_workbook(data, "pull.XLS"), "pull.XLS"))["SMMS"]
        assert smms.sap_status == check, (switch, smms.sap_status)
        if check == "not_ok":
            assert "HTTPS 44401" in smms.sap_evidence
        else:
            assert smms.triage == "no_data" and "ZAHC_SAPCONTROL" in smms.headline + smms.sap_evidence


def test_sapcontrol_only_read_functions():
    from ahc.sap import Sap
    from ahc.sapctl import SapControlViaSm69
    sc = SapControlViaSm69(Sap(None))
    for fn, args in (("Stop", ()), ("RestartInstance", ()), ("ParameterValue", ("x; rm -rf /",)),
                     ("ParameterValue", ("a b",))):
        with pytest.raises(ValueError):
            sc.run("01", fn, *args)
    with pytest.raises(ValueError):
        sc.run("1 -function Stop", "GetAccessPointList")


# ── Export: Excel must open every workbook (the .XLS bug) ────────────────
def test_generated_spreadsheetml_obeys_excel_load_rules(pulled):
    """Excel refuses SpreadsheetML whose tabs use more columns than
    ExpandedColumnCount declares (was: SOST, DB02_Alerts, DB02_Memory)."""
    assert excel_problems(pulled[0]) == []


def test_writer_drops_xml_illegal_characters():
    from ahc.writer import Block, Verdict, write_workbook
    from ahc.collect import SHEET_OF_MONITOR
    data = write_workbook("T", [Verdict(True, "OK")] * 33,
                          {"SM21": [Block(LAYOUT["sheets"][2]["tables"][0], [["a\x00b\x1bc", "x" * 40000]])]})
    assert excel_problems(data) == []
    assert len(SHEET_OF_MONITOR) == 33


def test_xlsx_keeps_ahc_formatting(pulled):
    wb = assert_valid_xlsx(spreadsheetml_to_xlsx(pulled[0]))
    mon = wb["Monitors"]
    assert "A1:H1" in {str(r) for r in mon.merged_cells.ranges}
    assert mon["A1"].fill.fgColor.rgb == "FF000000" and mon["A1"].font.bold
    assert mon["A2"].value == "Monitoring Task" and mon["A2"].fill.fgColor.rgb not in (None, "00000000")
    oks = [mon.cell(r, 5) for r in range(3, 36) if mon.cell(r, 5).value == "X"]
    assert oks and all(c.fill.fgColor.rgb == oks[0].fill.fgColor.rgb for c in oks)
    assert mon.column_dimensions["C"].width > 10
    sl = wb["SLICENSE"]
    assert sl.cell(5, 1).value == "Status" and sl.cell(5, 1).border.top.style == "thin"
    assert sl.cell(7, 4).value == "Maintenance_HDB" and sl.cell(7, 11).value == "Expired"


def test_xlsx_round_trip_same_verdicts(pulled):
    x = spreadsheetml_to_xlsx(pulled[0])
    a, b = pulled[2], analyze(read_workbook(x, "x.xlsx"), "x.xlsx")
    assert [(c.sheet, c.sap_status, c.triage) for c in a.checks] == [(c.sheet, c.sap_status, c.triage) for c in b.checks]


def test_xlsx_input_is_returned_unchanged(pulled):
    x = spreadsheetml_to_xlsx(pulled[0])
    assert spreadsheetml_to_xlsx(x) == x


@pytest.mark.skipif(not os.environ.get("HEALTH_REAL_AHC"), reason="real export not provided")
def test_real_sap_export_converts_to_valid_xlsx():
    """SAP's own export contains XML-illegal control characters; Excel can
    refuse it as .XLS, the .xlsx conversion must still open."""
    real = Path(os.environ["HEALTH_REAL_AHC"]).read_bytes()
    x = spreadsheetml_to_xlsx(real)
    assert_valid_xlsx(x)
    a, b = analyze(read_workbook(real, "r.XLS"), "r"), analyze(read_workbook(x, "r.xlsx"), "r")
    assert [(c.sap_status, c.triage) for c in a.checks] == [(c.sap_status, c.triage) for c in b.checks]


# ── RFC_Con: SM59 connection test of every destination ───────────────────
def test_rfc_con_tests_every_destination_and_aborts_zombies(pulled):
    t = read_workbook(pulled[0], "pull.XLS")["RFC_Con"].tables[0]
    rows = {r[1]: r for r in t.rows}
    assert set(rows) == {"QASCLNT100", "PRDCLNT100", "DEVCLNT100", "ZOMBIE_TCP", "NONE"}   # Z_SKIPPED filtered
    q = rows["QASCLNT100"]
    assert q[0] == "ABAP Connection" and q[2] == "QAS client 100" and q[3] == "GIRI" and q[4] == "15.09.2026"
    assert (q[5], q[6], q[7], q[9], q[10], q[11]) == ("qas", "00", "100", "RFCUSER", "E", "Saved")
    assert (q[16], q[17]) == ("Connection OK", "Logon successful")
    assert rows["PRDCLNT100"][17].startswith("Logon failed: Name or password")
    assert "NIEHOST_UNKNOWN" in rows["DEVCLNT100"][16]
    assert "no response within 1.5 s" in rows["ZOMBIE_TCP"][16]
    assert rows["NONE"][16] == "Test not possible for this connection type"
    assert [r[1] for r in t.rows][:3] == sorted(["PRDCLNT100", "DEVCLNT100", "ZOMBIE_TCP"],
                                                key=lambda n: ("3" if n != "ZOMBIE_TCP" else "T", n))  # failures first
    c = by_sheet(pulled[2])["RFC_Con"]
    assert c.sap_status == "not_ok"
    for name in ("PRDCLNT100", "DEVCLNT100", "ZOMBIE_TCP"):
        assert name in c.sap_evidence
    assert "1 OK, 3 failed, 1 not testable" in c.sap_evidence


def test_rfc_con_zombie_does_not_stall_the_pull(server, monkeypatch):
    """Several hanging destinations are tested in parallel, each abandoned
    after the timeout: the check takes about one timeout, not one per zombie."""
    import datetime as dt
    import time as _t
    from ahc.collect import Ctx, c_rfc_con
    from ahc.sap import Sap
    monkeypatch.setenv("AHC_RFC_TIMEOUT", "1")
    monkeypatch.setenv("AHC_RFC_SKIP", "")
    c = client()
    try:
        x = Ctx(sap=Sap(c), sid="QAS", client="100", now=dt.datetime.now())
        t0 = _t.time()
        blocks, v = c_rfc_con(x)
        took = _t.time() - t0
    finally:
        c.close()
    assert took < 4, took
    assert v.ok is False and "ZOMBIE_TCP" in v.evidence
    assert len(blocks[0].rows) == 6


def test_soap_call_timeout_is_reported_as_timeout(server):
    c = client()
    try:
        with pytest.raises(RfcSoapError) as e:
            c.call("DEST_CHECK_CONNECTION", _timeout_s=0.5, NAME="ZOMBIE_TCP")
        assert e.value.kind == "timeout" and "0.5 s" in e.value.message
        assert c.call("DEST_CHECK_CONNECTION", NAME="QASCLNT100")["CONNECTION_TEST_RESULT"] == ""
    finally:
        c.close()
