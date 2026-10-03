"""D-052c: scheduling /SDF/SMON (or /SDF/MON) daily monitoring from Sentinel.

The fake mirrors what F4H showed on 2026-10-03: XBP job with two steps
(/SDF/SMON_START with a temporary variant, /SDF/SMON_REORG), START_IMMEDIATELY
for today, BAPI_XBP_JOB_HEADER_MODIFY that only accepts the start date with
MASK-STARTCOND = 'X' and an ISO date, the run row in /SDF/SMON_RUN, SMON's
own watchdog job, and the "overlapping analysis" refusal."""
import datetime as dt
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"

from ahc.sap import Sap  # noqa: E402
from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from perf import service, smon_sched  # noqa: E402

PORT = 8096
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


def sap():
    return Sap(RfcSoapClient.from_system(WEBGUI, client="100", username="MONITOR", password="secret", timeout_s=20))


def now_of(s):
    return service.system_now(s)[1]


def calls():
    import json
    return json.loads(fake("/calls"))


def test_status_before_scheduling():
    s = sap()
    st = smon_sched.status(s, now_of(s))
    assert st["tool"] == "SMON" and st["can_schedule"] and not st["daily_scheduled"] and st["today"] is None
    assert not set(calls()) & smon_sched_write_fms()          # status is read-only


def smon_sched_write_fms():
    return {"BAPI_XBP_JOB_OPEN", "BAPI_XBP_JOB_ADD_ABAP_STEP", "BAPI_XBP_JOB_CLOSE", "BAPI_XBP_JOB_START_IMMEDIATELY",
            "BAPI_XBP_JOB_HEADER_MODIFY", "BAPI_XBP_JOB_DELETE"}


def test_schedule_today_and_daily():
    s = sap()
    now = now_of(s)
    r = smon_sched.schedule(s, now, "MONITOR", 10, 14, sleep=lambda _: None)
    assert r["tool"] == "SMON" and len(r["done"]) == 2
    assert "Started today's daily monitoring" in r["done"][0] and "every 10 s" in r["done"][0]
    assert "every day from" in r["done"][1]
    st = r["status"]
    assert st["daily_scheduled"] and st["today"]["description"].startswith("Daily Monitoring")
    assert st["today"]["running"] and st["today"]["interval_s"] == 10 and st["watchdog"]
    assert st["next_start"] == f"{(now + dt.timedelta(days=1)).date()} 00:00:00"
    assert any("has been scheduled" in m for m in r["messages"])


def test_temporary_variant_matches_sap_defaults():
    sel = {p["SELNAME"]: p["LOW"] for p in smon_sched.selinfo("SMON", "MONITOR", 10, 14, "120000", "235900")}
    assert sel["SCHEDLD"] == "X" and sel["AGE"] == "14" and sel["FREQU"] == "10" and sel["JOBUSER"] == "MONITOR"
    assert sel["NETRTT"] == "X" and sel["NETRTTCT"] == "2"       # F4H: NETRTT must stay below 4 per minute
    assert sel["SM50_CT"] == "1" and sel["QUEUE_CT"] == "10" and sel["SM04_CT"] == "60"
    assert all(p["KIND"] == "P" for p in smon_sched.selinfo("MON", "X", 30, 14, "000000", "235900"))
    assert "NETRTT" not in {p["SELNAME"] for p in smon_sched.selinfo("MON", "X", 30, 14, "000000", "235900")}


def test_today_starts_before_now():
    s = sap()
    now = now_of(s)
    smon_sched.schedule(s, now, "MONITOR", 10, 14, sleep=lambda _: None)
    st = smon_sched.status(s, now)
    start = dt.datetime.fromisoformat(st["today"]["start"])
    assert start <= now                     # collectors only start once st_tim ≤ sy-uzeit (F4H finding)


def test_second_call_changes_nothing():
    s = sap()
    now = now_of(s)
    smon_sched.schedule(s, now, "MONITOR", 10, 14, sleep=lambda _: None)
    n = sum(1 for c in calls() if c == "BAPI_XBP_JOB_OPEN")
    r = smon_sched.schedule(s, now, "MONITOR", 10, 14, sleep=lambda _: None)
    assert r["done"] == ["Daily monitoring was already scheduled — nothing changed"]
    assert sum(1 for c in calls() if c == "BAPI_XBP_JOB_OPEN") == n


def test_unschedule_deletes_future_jobs_only():
    s = sap()
    now = now_of(s)
    smon_sched.schedule(s, now, "MONITOR", 10, 14, sleep=lambda _: None)
    r = smon_sched.unschedule(s, now)
    assert r["deleted"] == 1 and not r["status"]["daily_scheduled"]
    assert r["status"]["today"] is not None and "continues until 23:59" in r["note"]


def test_older_sdf_mon():
    fake("/perf/mon_only/on")
    s = sap()
    now = now_of(s)
    r = smon_sched.schedule(s, now, "MONITOR", 30, 14, sleep=lambda _: None)
    assert r["tool"] == "MON" and r["status"]["daily_scheduled"] and r["status"]["job_name"] == "/SDF/MON_SCHEDULER"
    st = service.ranges(RfcSoapClient.from_system(WEBGUI, client="100", username="MONITOR", password="secret"))["smon"]
    assert st["tool"] == "MON"


def test_mon_snapshots_are_read_without_netrtt():
    fake("/perf/mon_only/on")
    c = RfcSoapClient.from_system(WEBGUI, client="100", username="MONITOR", password="secret")
    v = service.live(c, "100")
    assert v["smon"]["tool"] == "MON" and v["smon"]["active"] and v["smon"]["series"]
    assert v["smon"]["series"][-1]["free_mem"] is None
    srv = v["landscape"]["app"]["servers"][0]
    assert srv["os_source"] == "SMON" and srv["rtt_smon_us"] is None


def test_jobs_switched_off(monkeypatch):
    monkeypatch.setenv("AHC_NO_JOBS", "1")
    s = sap()
    with pytest.raises(smon_sched.JobError):
        smon_sched.schedule(s, now_of(s), "MONITOR", 10, 14, sleep=lambda _: None)
    assert not set(calls()) & smon_sched_write_fms()


def test_bad_parameters():
    s = sap()
    with pytest.raises(ValueError):
        smon_sched.schedule(s, now_of(s), "MONITOR", 0, 14)
    with pytest.raises(ValueError):
        smon_sched.schedule(s, now_of(s), "MONITOR", 10, 1000)


# ── API ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SYSTEMS_PATH", str(tmp_path / "systems.json"))
    monkeypatch.setenv("REGISTRY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from engine import crypto
    from engine.registry import Registry, SapCredential, SystemEntry
    Registry(systems=[SystemEntry(sid="QAS", client="100", hostname="127.0.0.1", webgui_url=WEBGUI,
                                  sap_credential=SapCredential(username="MONITOR", password_enc=crypto.encrypt("secret")))]
             ).save(os.environ["SYSTEMS_PATH"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from perf.api import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_api_requires_confirmation_and_schedules(api, monkeypatch):
    monkeypatch.setattr(smon_sched.time, "sleep", lambda _: None)
    assert api.get("/api/perf/QAS/smon/schedule").json()["daily_scheduled"] is False
    r = api.post("/api/perf/QAS/smon/schedule", json={})
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    assert not set(calls()) & smon_sched_write_fms()
    r = api.post("/api/perf/QAS/smon/schedule", json={"confirm": True, "interval_s": 10, "keep_days": 14})
    assert r.status_code == 200 and r.json()["status"]["daily_scheduled"]
    assert api.post("/api/perf/QAS/smon/schedule", json={"confirm": True, "interval_s": 0}).status_code == 400
    for bad in ({"interval_s": "abc"}, {"interval_s": None}, {"keep_days": [1]}):
        assert api.post("/api/perf/QAS/smon/schedule", json={"confirm": True, **bad}).status_code == 400
    assert api.post("/api/perf/export?format=pdf", json={"kpis": ["x"]}).status_code == 400
    assert api.post("/api/perf/QAS/smon/unschedule", json={}).status_code == 400
    r = api.post("/api/perf/QAS/smon/unschedule", json={"confirm": True})
    assert r.status_code == 200 and r.json()["deleted"] == 1


def test_api_no_jobs_is_409(api, monkeypatch):
    monkeypatch.setenv("AHC_NO_JOBS", "1")
    r = api.post("/api/perf/QAS/smon/schedule", json={"confirm": True})
    assert r.status_code == 409 and "AHC_NO_JOBS" in r.json()["detail"]
