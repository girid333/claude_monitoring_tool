"""D-052b: Performance view across a landscape — several application servers,
HANA scale-out with a standby host, HANA system replication, cloud placement
and the app server closest to the database.

The fake (fake_sap_ahc_pull.py, /perf/multi/on) has three instances:
qas_QAS_00 on qas (this instance), qas_QAS_01 on qasapp1, qas_QAS_02 on
qasapp2 (busy). SMON NETRTT 450 / 210 / 900 µs; the HANA client network trace
(/perf/nettrace/on) 520 / 180 / 1250 µs; ST03N DB time per call lowest on 01.
ZAHC_CLOUDMETA (/perf/cloud/on): DB hdb, qas and qasapp1 in eu-central-1a and
placement group sap-qas-pg, qasapp2 in eu-central-1b. HANA: hdb master, hdb2
worker (fullest memory), hdb3 standby; HSR PRIM → SEC SYNC (/perf/hsr_bad/on:
ERROR, 420 s backlog); one takeover three days ago."""
import datetime as dt
import io
import os
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"

from ahc.hana import sxpg_safe  # noqa: E402
from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from perf import export, landscape, service  # noqa: E402

PORT = 8093
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
    fake("/perf/multi/on")
    landscape._CLOUD_CACHE.clear()
    yield
    fake("/reset")


def run(fn, *a):
    c = RfcSoapClient.from_system(WEBGUI, client="100", username="MONITOR", password="secret", timeout_s=20)
    try:
        return fn(c, *a)
    finally:
        c.close()


def kpi(view, kid):
    return next((k for k in view["kpis"] if k["id"] == kid), None)


def app(view, name):
    return next(s for s in view["landscape"]["app"]["servers"] if s["name"] == name)


# ── application servers ──────────────────────────────────────────────────
def test_every_instance_is_listed_with_its_own_figures():
    v = run(service.live, "100")
    a = v["landscape"]["app"]
    assert [s["name"] for s in a["servers"]] == ["qas_QAS_00", "qas_QAS_01", "qas_QAS_02"]
    assert a["count"] == 3 and a["active"] == 3
    s0, s2 = app(v, "qas_QAS_00"), app(v, "qas_QAS_02")
    assert s0["local"] and not s2["local"]
    assert s2["busy_dia"] == 3 and s2["total_dia"] == 4                 # TH_WPINFO per server
    assert s2["cpu"] == 80.0 and s2["os_source"] == "SMON"              # other instances' CPU only from SMON
    assert all(s["dia_steps"] == 4000 and s["dia_resp_ms"] == 850.0 for s in a["servers"])   # ST03N per instance
    assert kpi(v, "app_servers")["value"] == 3 and kpi(v, "app_servers")["rating"] == "good"
    cpu = kpi(v, "host_cpu")
    assert cpu["value"] == 80.0 and "qas_QAS_02" in cpu["detail"]          # busiest instance, not the local one
    assert kpi(v, "users")["detail"].startswith("qas_QAS_00 only")         # SM04 is per instance


def test_nearest_server_from_smon_netrtt():
    v = run(service.live, "100")
    a = v["landscape"]["app"]
    assert a["source"] == "smon" and a["measured"] and a["nearest"] == "qas_QAS_01"
    assert [app(v, n)["latency_rank"] for n in ("qas_QAS_01", "qas_QAS_00", "qas_QAS_02")] == [1, 2, 3]
    assert app(v, "qas_QAS_01")["latency_rating"] == "good" and app(v, "qas_QAS_02")["latency_rating"] == "serious"
    k = kpi(v, "app_db_rtt")
    assert k["rating"] == "serious" and "nearest qas_QAS_01" in k["detail"] and "slowest qas_QAS_02" in k["detail"]


def test_hana_client_trace_ranks_when_smon_is_off():
    fake("/perf/smon/off")
    fake("/perf/nettrace/on")
    v = run(service.live, "100")
    a = v["landscape"]["app"]
    assert a["source"] == "hana_trace" and a["nearest"] == "qas_QAS_01"
    assert app(v, "qas_QAS_02")["rtt_hana_us"] == 1250 and app(v, "qas_QAS_02")["latency_rating"] == "serious"
    assert any("SMON" in h for h in a["how_to_measure"])


def test_st03n_only_ranks_but_does_not_rate():
    fake("/perf/smon/off")
    v = run(service.live, "100")
    a = v["landscape"]["app"]
    assert a["source"] == "st03n" and not a["measured"] and a["nearest"] == "qas_QAS_01"
    assert all(s["db_us_per_call"] for s in a["servers"])
    assert app(v, "qas_QAS_01")["db_us_per_call"] < app(v, "qas_QAS_00")["db_us_per_call"] < \
        app(v, "qas_QAS_02")["db_us_per_call"]
    assert kpi(v, "app_db_rtt") is None                                   # not a network figure → no rating
    assert len(a["how_to_measure"]) == 2


def test_st03n_per_instance_uses_microseconds():
    v = run(service.live, "100")
    s = app(v, "qas_QAS_00")
    # fake: dialog 300 ms DB per step; all task types / PHYCALLS (1000 per step) → hundreds of µs
    assert 100 < s["db_us_per_call"] < 50_000 and s["dia_db_ms"] == 300.0


# ── cloud placement ──────────────────────────────────────────────────────
def test_cloud_placement_and_zone_match():
    fake("/perf/cloud/on")
    v = run(service.live, "100")
    s0, s1, s2 = app(v, "qas_QAS_00"), app(v, "qas_QAS_01"), app(v, "qas_QAS_02")
    assert s1["cloud"]["zone"] == "eu-central-1a" and s1["same_zone_as_db"] and s1["same_pg_as_db"]
    assert s2["same_zone_as_db"] is False and s2["same_pg_as_db"] is False
    assert s0["cloud"]["vm_size"] == "m6i.2xlarge"
    assert v["landscape"]["cloud"]["hdb"]["placement_group"] == "sap-qas-pg"
    assert not any(n.startswith("Cloud placement") for n in v["notes"])


def test_cloud_command_missing_is_one_note():
    v = run(service.live, "100")
    assert [n for n in v["notes"] if n.startswith("Cloud placement")] == [
        "Cloud placement: SM69 command ZAHC_CLOUDMETA does not exist — see deploy/sap/zahc_cloudmeta.sh"]
    assert all(not s["cloud"] for s in v["landscape"]["app"]["servers"])
    assert "same_zone_as_db" not in app(v, "qas_QAS_01")


def test_cloudmeta_runs_on_each_host_once_per_hour():
    fake("/perf/cloud/on")
    run(service.live, "100")
    first = json_calls().count("SXPG_COMMAND_EXECUTE")
    run(service.live, "100")
    second = json_calls().count("SXPG_COMMAND_EXECUTE") - first
    assert first - second == 5          # qas, qasapp1, qasapp2, hdb, hdb2 — the refresh used the cache


def json_calls():
    import json
    return [c.get("fm") if isinstance(c, dict) else c for c in json.loads(fake("/calls"))]


def test_platform_guess():
    g = landscape.platform_guess
    assert g("Amazon EC2", "r6i.8xlarge") == "AWS" and g("Microsoft Corporation", "Virtual Machine") == "Azure or Hyper-V"
    assert g("Google", "Google Compute Engine") == "Google Cloud" and g("VMware, Inc.", "VMware7,1") == "VMware"


# ── HANA scale-out + system replication ──────────────────────────────────
def test_hana_scale_out_hosts_and_worst_host_memory():
    v = run(service.live, "100")
    hosts = v["landscape"]["hana"]["hosts"]
    assert [(h["host"], h["role"], h["active"]) for h in hosts] == [
        ("hdb", "master", True), ("hdb2", "slave", True), ("hdb3", "standby", False)]
    assert kpi(v, "hana_hosts")["rating"] == "good"                       # an inactive standby is normal
    m = kpi(v, "hana_mem")
    assert m["value"] == 90.6 and "hdb2" in m["detail"]                  # fullest host, not the sum
    assert kpi(v, "hana_cpu")["value"] == 64.0


def test_hsr_active():
    v = run(service.live, "100")
    sr = v["landscape"]["hana"]["hsr"]
    assert sr["primary_site"] == "PRIM" and sr["sites"][0]["site"] == "SEC" and sr["sites"][0]["mode"] == "SYNC"
    assert sr["sites"][0]["hosts"] == ["hdbs", "hdbs2"]
    assert kpi(v, "hsr_status")["value"] == "Active" and kpi(v, "hsr_status")["rating"] == "good"
    assert kpi(v, "hsr_lag")["value"] == 2.0 and kpi(v, "hsr_lag")["rating"] == "good"


def test_hsr_error_is_critical_with_details():
    fake("/perf/hsr_bad/on")
    v = run(service.live, "100")
    assert kpi(v, "hsr_status")["rating"] == "critical" and kpi(v, "hsr_lag")["value"] == 420.0
    svc = next(s for s in v["landscape"]["hana"]["hsr"]["services"] if s["host"] == "hdb2")
    assert svc["status"] == "ERROR" and "refused" in svc["details"] and svc["secondary_active"] == "NO"
    assert any(f["kpi"] == "hsr_status" for f in v["findings"])


def test_single_system_has_no_landscape_kpis():
    fake("/perf/multi/off")
    v = run(service.live, "100")
    assert v["landscape"]["hana"]["hsr"] is None
    assert kpi(v, "app_servers") is None and kpi(v, "hana_hosts") is None and kpi(v, "hsr_status") is None


# ── day analysis ─────────────────────────────────────────────────────────
def test_day_per_host_hana_and_takeover():
    day = dt.date.today() - dt.timedelta(days=3)
    v = run(service.day, "100", day)
    assert set(v["hana_host_hours"]) == {"hdb", "hdb2"}
    ten = next(h for h in v["hana_hours"] if h["hour"] == 10)
    assert ten["mem_host"] == "hdb2" and ten["mem_max_gb"] == 30.0          # worst host per hour
    assert kpi(v, "hsr_takeover")["value"] == 1 and "SEC → PRIM" in kpi(v, "hsr_takeover")["detail"]
    assert kpi(v, "hsr_status") is None and kpi(v, "app_servers") is None   # current state isn't that day's
    other = run(service.day, "100", day - dt.timedelta(days=1))
    assert kpi(other, "hsr_takeover") is None


def test_day_ranks_instances_from_that_days_data():
    v = run(service.day, "100", dt.date.today() - dt.timedelta(days=3))
    a = v["landscape"]["app"]
    # that day SMON only ran on qas_QAS_00 → ST03N covers every instance and ranks them
    assert a["source"] == "st03n" and a["nearest"] == "qas_QAS_01"


# ── exports ──────────────────────────────────────────────────────────────
def test_exports_carry_the_landscape():
    fake("/perf/cloud/on")
    v = run(service.live, "100")
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(export.to_xlsx(v)))
    assert {"App servers", "HANA hosts", "System replication"} <= set(wb.sheetnames)
    ws = wb["App servers"]
    assert "qas_QAS_01" in ws["A1"].value and "Closest" in ws["A1"].value
    vals = [c.value for row in ws.iter_rows() for c in row if c.value]
    assert "★ qas_QAS_01" in vals and any("eu-central-1b" in str(x) for x in vals)
    pdf = export.to_pdf(v)
    assert pdf[:5] == b"%PDF-" and len(pdf) > 5000
    assert zipfile.is_zipfile(io.BytesIO(export.to_xlsx(v)))


# ── safety ───────────────────────────────────────────────────────────────
def test_sxpg_percent_words_are_refused():
    with pytest.raises(ValueError):
        sxpg_safe("SELECT 1 FROM DUMMY WHERE KEY LIKE '%cloud%'")
    assert sxpg_safe("SELECT 1 FROM DUMMY WHERE KEY LIKE 'net%'")


def test_landscape_reads_are_read_only():
    fake("/perf/cloud/on")
    run(service.live, "100")
    from ahc.sap import READ_ONLY_FMS
    assert set(json_calls()) <= READ_ONLY_FMS
