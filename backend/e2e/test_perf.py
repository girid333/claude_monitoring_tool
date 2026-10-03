"""D-052: Performance section (SAP + HANA) against fake_sap_ahc_pull.py.

The fake returns workload the way SWNC_COLLECTOR_GET_AGGREGATES does on
F4H — MILLISECONDS summed over steps, base64 RAW task types ("AQ==" = 01),
TIME slots 'HH--HH', recent days only per instance (no TOTAL component) and
older days as TOTAL — so a stray /1000 or a missed instance shows up at
once. It also has a slow day 3
days back, a dialog-queue peak at 10:00, two users on the same tcode, an
empty task-type line, a blank transaction name, HANA top SQL with quotes,
and SMON snapshots on two servers."""
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"

from ahc.hana import MAX_SQL, HanaViaSm69  # noqa: E402
from ahc.sap import READ_ONLY_FMS  # noqa: E402
from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from perf import export, service  # noqa: E402

PORT = 8091
BASE = f"http://127.0.0.1:{PORT}"
WEBGUI = f"{BASE}/sap/bc/gui/sap/its/webgui"


def fake(path):
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


@pytest.fixture(autouse=True)
def reset():
    fake("/reset")
    yield
    fake("/reset")


def client():
    return RfcSoapClient.from_system(WEBGUI, client="100", username="MONITOR", password="secret", timeout_s=20)


def run(fn, *a):
    c = client()
    try:
        return fn(c, *a)
    finally:
        c.close()


def kpi(view, kid):
    return next(k for k in view["kpis"] if k["id"] == kid)


# ── workload (ST03N) ─────────────────────────────────────────────────────
def test_live_dialog_kpis_in_ms_summed_over_instances():
    v = run(service.live, "100")
    assert kpi(v, "dia_resp")["value"] == 850.0 and kpi(v, "dia_resp")["rating"] == "good"
    assert kpi(v, "dia_db")["value"] == 300.0 and kpi(v, "dia_cpu")["value"] == 200.0
    assert kpi(v, "dia_wait")["value"] == 20.0
    assert "12,000 dialog steps" in kpi(v, "dia_resp")["detail"]
    names = [t["name"] for t in v["workload"]["tasks"]]
    assert "DIALOG" in names and "BACKGROUND" in names and not any(t["steps"] == 0 for t in v["workload"]["tasks"])
    bg = next(t for t in v["workload"]["tasks"] if t["name"] == "BACKGROUND")
    assert bg["avg_db"] == 26000.0                               # READDIR+READSEQ+CHNG when DBTI is absent


def test_last_24_hours_profile_shows_the_queue_peak():
    v = run(service.live, "100")
    hours = v["hourly"]
    assert len(hours) == 24 and all("label" in h for h in hours)
    ten = [h for h in hours if h["hour"] == 10]
    assert ten and ten[0]["wait"] == 400.0 and ten[0]["avg_resp"] == 1600.0
    assert ten[0]["other"] == round(1600 - 200 - 300 - 400, 1)


def test_top_transactions_aggregate_users_and_skip_blanks():
    top = run(service.live, "100")["workload"]["top"]
    va01 = next(t for t in top if t["name"] == "VA01")
    assert va01["steps"] == 4000 and va01["users"] == 2 and va01["avg_resp"] == 1400.0
    assert all(t["name"] for t in top)
    assert any(t["name"] == "ZREPORT_SLOW  BATCHJOB_A" for t in top)    # job name kept, spaces collapsed
    assert round(sum(t["share"] for t in top)) == 100


def test_day_analysis_from_sap_history():
    slow = dt.date.today() - dt.timedelta(days=3)
    v = run(service.day, "100", slow)
    assert v["mode"] == "day" and v["date"] == slow.isoformat()
    assert kpi(v, "dia_resp")["value"] == 1530.0 and kpi(v, "dia_resp")["rating"] == "serious"
    assert len(v["hourly"]) == 24 and kpi(v, "dia_resp")["spark"][10] > kpi(v, "dia_resp")["spark"][9]
    assert v["overall"]["rating"] in ("serious", "critical") and v["findings"][0]["recommendation"]
    assert v["smon"]["series"] and v["smon"]["series"][0]["time"] == "10:00"
    assert len(v["hana_hours"]) == 24 and kpi(v, "hana_cpu")["value"] == 92.0


def test_top_transactions_from_times_when_tcdet_is_empty():
    fake("/perf/tcdet/off")                                     # F4H: TCDET empty, TIMES carries ENTRY_ID
    top = run(service.live, "100")["workload"]["top"]
    names = [t["name"] for t in top]
    assert names[0] == "ZREPORT_SLOW" and "VA01" in names and "SE16" in names   # 'T'/'R' type flag stripped
    va01 = next(t for t in top if t["name"] == "VA01")
    assert va01["users"] is None and va01["task"] == "DIALOG" and 800 < va01["avg_resp"] < 1600  # incl. the 10:00 peak


def test_live_without_recent_workload_explains_why():
    fake("/perf/work/off")
    v = run(service.live, "100")
    assert v["workload"] is None and kpi(v, "dia_resp")["rating"] == "unknown"
    assert "collector" in kpi(v, "dia_resp")["detail"]
    newest = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    assert any("SAP_COLLECTOR_FOR_PERFMONITOR" in n and newest in n for n in v["notes"])


def test_task_type_codes_decoded_and_named():
    from perf.collect import tt_code, tt_name
    assert tt_code("AQ==") == "01" and tt_code("/g==") == "FE" and tt_code("0B") == "0B"
    assert tt_name("FE") == "RFC" and tt_name("99") == "TYPE 99"
    names = [t["name"] for t in run(service.live, "100")["workload"]["tasks"]]
    assert {"DIALOG", "BACKGROUND", "UPDATE"} <= set(names) and "RFC" in names and not any(n.startswith("TYPE") for n in names)


def test_old_day_read_from_total_component():
    v = run(service.day, "100", dt.date.today() - dt.timedelta(days=25))
    assert kpi(v, "dia_resp")["value"] == 850.0 and v["workload"]["steps"] == 12000 + 800 + 5000 + 900


def test_sxpg_quote_needs_a_blank_after_it():
    from ahc.hana import sxpg_quotes
    assert sxpg_quotes("REPLACE(S, CHAR(34), '') AS S") == "REPLACE(S, CHAR(34), '' ) AS S"
    assert sxpg_quotes("A = 'X' AND B = 'Y'") == "A = 'X' AND B = 'Y'"


def test_day_outside_sap_retention_is_empty_not_an_error():
    old = dt.date.today() - dt.timedelta(days=70)
    v = run(service.day, "100", old)
    assert v["workload"] is None and v["hourly"] == [] and v["hana_hours"] == []
    assert kpi(v, "dia_resp")["rating"] == "unknown"


def test_ranges_follow_what_sap_keeps():
    r = run(lambda c: service.ranges(c))
    assert len(r["workload_days"]) == 40          # daily rows of any component; weekly and other SIDs dropped
    assert r["workload_days"][0] == r["today"]
    assert r["hana_from"] == (dt.date.fromisoformat(r["today"]) - dt.timedelta(days=30)).isoformat()
    assert r["oldest"] == r["workload_days"][-1] and r["limit_days"] == 90


# ── live ABAP + SMON ─────────────────────────────────────────────────────
def test_live_abap_state():
    v = run(service.live, "100")
    s = v["sap"]
    assert s["wp"]["DIA"]["total"] == 3 and s["dia_util"] == 0.0
    assert s["cpu"]["util"] == 15.0 and s["memory"]["free_pct"] == 10.6
    assert s["buffers"]["worst_hit"] == 90.0 and kpi(v, "buffers")["rating"] == "critical"
    assert s["buffers"]["worst_name"] == "PXA" and kpi(v, "buffers")["detail"].startswith("PXA")  # CTLGA 50 % not rated
    assert s["locks"] == 2 and s["update_errors"] == 0


def test_update_errors_are_serious():
    fake("/perf/upd/on")
    v = run(service.live, "100")
    assert v["sap"]["updates_pending"] == 2 and v["sap"]["update_errors"] == 1
    assert kpi(v, "upd_err")["rating"] == "serious"


def test_smon_active_inactive_and_missing():
    v = run(service.live, "100")
    assert v["smon"]["active"] and v["smon"]["latest"]["servers"] == 2
    assert any(p["diaq"] > 0 for p in v["smon"]["series"])
    fake("/perf/today/off")
    v = run(service.live, "100")
    assert v["smon"]["installed"] and not v["smon"]["active"] and "not scheduled" in v["smon"]["why"]
    assert not any(k["id"] == "smon_queue" for k in v["kpis"])
    fake("/perf/smon/off")
    v = run(service.live, "100")
    assert v["smon"]["installed"] is False and "not installed" in v["smon"]["why"]


# ── HANA ─────────────────────────────────────────────────────────────────
def test_hana_live_kpis_and_sql_limits(monkeypatch):
    sent = []
    orig = HanaViaSm69._run
    monkeypatch.setattr(HanaViaSm69, "_run", lambda self, sql: (sent.append(sql), orig(self, sql))[1])
    v = run(service.live, "100")
    assert kpi(v, "hana_mem")["value"] == 81.2 and kpi(v, "hana_mem")["rating"] == "warning"
    assert kpi(v, "hana_alerts")["value"] == 1 and kpi(v, "hana_alerts")["rating"] == "serious"
    assert kpi(v, "hana_unloads")["value"] == 3
    assert kpi(v, "hana_savepoint")["value"] == 4.2
    sql = v["hana"]["top_sql"]
    assert sql[0]["share"] == 75.0 and '"' not in sql[0]["statement"] and sql[0]["statement"].startswith("SELECT * FROM VBAK")
    # text matched by hash although the text query returned the rows in another order; newline kept no row
    assert sql[1]["statement"] == "UPDATE NRIV SET NRLEVEL = ? WHERE CLIENT = ? AND OBJECT = ?"
    assert sent and all(len(s) <= MAX_SQL and not s.startswith('"') for s in sent)
    assert not any('"' in s for s in sent)                       # SXPG must never see a double quote
    assert not [n for n in v["notes"] if not n.startswith("Cloud placement")], v["notes"]   # ZAHC_CLOUDMETA is optional


def test_without_hana_the_sap_side_still_works():
    fake("/hana/off")
    v = run(service.live, "100")
    assert v["hana"] is None and kpi(v, "hana_mem")["rating"] == "unknown"
    assert "ZAHC_HDBSQL" in kpi(v, "hana_mem")["detail"]
    assert kpi(v, "dia_resp")["value"] == 850.0
    assert any("HANA" in n for n in v["notes"])


def test_live_and_day_only_read(monkeypatch):
    fake("/reset")
    run(service.live, "100")
    run(service.day, "100", dt.date.today() - dt.timedelta(days=1))
    calls = set(json.loads(fake("/calls")))
    allowed = {f.replace("/", "_-") for f in READ_ONLY_FMS}
    assert calls <= allowed, calls - allowed
    assert not any(c.startswith("BAPI_X") or c == "DEST_CHECK_CONNECTION" for c in calls)


# ── exports ──────────────────────────────────────────────────────────────
def test_exports_build_from_the_view():
    from openpyxl import load_workbook
    v = run(service.live, "100")
    x = export.to_xlsx(json.loads(json.dumps(v)))
    wb = load_workbook(io.BytesIO(x))
    assert wb.sheetnames[:2] == ["Overview", "Findings"]
    for s in ("Hourly profile", "Task types", "Top transactions", "Work processes", "Buffers", "HANA hourly",
              "HANA top SQL", "SMON"):
        assert s in wb.sheetnames, s
    assert wb["Hourly profile"]._charts and wb["HANA hourly"]._charts
    ov = wb["Overview"]
    vals = [c.value for row in ov.iter_rows() for c in row]
    assert "Dialog response time" in vals and "Critical" in vals
    with zipfile.ZipFile(io.BytesIO(x)) as z:
        assert not any(b"secret" in z.read(n) for n in z.namelist())
    pdf = export.to_pdf(v)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 4000
    d = run(service.day, "100", dt.date.today() - dt.timedelta(days=3))
    assert export.to_pdf(d)[:5] == b"%PDF-" and load_workbook(io.BytesIO(export.to_xlsx(d)))


def test_pdf_treats_sap_text_as_text():
    v = run(service.live, "100")
    v["workload"]["top"][0]["name"] = "<AD_MSBUF>"                 # F4H: a real ENTRY_ID
    v["hana"]["top_sql"][0]["statement"] = "SELECT * FROM T WHERE A < ? AND B > ?"
    v["notes"].append("x < y & z")
    assert export.to_pdf(v)[:5] == b"%PDF-"
    hh = v["hana"]["today"]                                       # F4H: HANA down most of the day → gaps
    v["hana"]["today"] = [dict(hh[0], hour=0), dict(hh[0], hour=22), dict(hh[0], hour=23)]
    assert export.to_pdf(v)[:5] == b"%PDF-"


# ── API ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMS_PATH", str(tmp_path / "systems.json"))
    monkeypatch.setenv("REGISTRY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from engine import crypto
    from engine.registry import Registry, SapCredential, SystemEntry
    Registry(systems=[SystemEntry(sid="QAS", client="100", hostname="127.0.0.1", webgui_url=WEBGUI,
                                  sap_credential=SapCredential(username="MONITOR", password_enc=crypto.encrypt("secret"))),
                      SystemEntry(sid="BAD", client="100", hostname="127.0.0.1", webgui_url=WEBGUI,
                                  sap_credential=SapCredential(username="MONITOR", password_enc=crypto.encrypt("x")))]
             ).save(os.environ["SYSTEMS_PATH"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from perf.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_endpoints(api):
    live = api.get("/api/perf/QAS/live")
    assert live.status_code == 200 and live.json()["sid"] == "QAS"
    day = (dt.date.today() - dt.timedelta(days=3)).isoformat()
    assert api.get(f"/api/perf/QAS/day?date={day}").json()["date"] == day
    assert api.get("/api/perf/QAS/day?date=yesterday").status_code == 400
    assert api.get(f"/api/perf/QAS/day?date={dt.date.today() + dt.timedelta(days=5)}").status_code == 400
    old = api.get(f"/api/perf/QAS/day?date={dt.date.today() - dt.timedelta(days=service.LIMIT_DAYS + 5)}")
    assert old.status_code == 400 and "window" in old.json()["detail"]
    assert len(api.get("/api/perf/QAS/ranges").json()["workload_days"]) == 40
    x = api.post("/api/perf/export?format=xlsx", json=live.json())
    assert x.status_code == 200 and x.content[:2] == b"PK" and x.headers["content-disposition"].endswith('.xlsx"')
    p = api.post("/api/perf/export?format=pdf", json=live.json())
    assert p.status_code == 200 and p.content[:5] == b"%PDF-"
    assert api.post("/api/perf/export?format=doc", json=live.json()).status_code == 400
    assert api.post("/api/perf/export?format=xlsx", json={"x": 1}).status_code == 400
    r = api.get("/api/perf/BAD/live")
    assert r.status_code == 502 and "BAD" in r.json()["detail"]
    assert api.get("/api/perf/NOPE/live").status_code == 404
    s = api.post("/api/perf/QAS/smon/schedule")                     # a write needs {"confirm": true}
    assert s.status_code == 400 and "confirm" in s.json()["detail"]
