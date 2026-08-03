"""D-045 E2E: the /api/rfc/* endpoints against a real fake SAP ICM, driving
the REAL registry (encrypted credential storage included) — proving an
operator can register a system once and get BASIS data with no extra
configuration, no agent, and no SDK.

Also proves the isolation contract: mounting this router changes nothing
about the existing /api/flows, /api/run or /api/sapgui surfaces.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from cryptography.fernet import Fernet

os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work) / "runs.db")
os.environ["FLOWS_DIR"] = str(Path(work) / "flows")
os.environ["SAPGUI_FLOWS_DIR"] = str(Path(work) / "flows_sapgui")
sys.path.insert(0, BACKEND)

PORT = 8095
from engine.registry import Registry, SystemEntry, SapCredential  # noqa: E402
from engine import crypto  # noqa: E402

Registry(systems=[
    SystemEntry(sid="F4H", client="100", hostname="127.0.0.1",
                webgui_url=f"http://127.0.0.1:{PORT}/sap/bc/gui/sap/its/webgui",
                sap_credential=SapCredential(username="MONITOR",
                                              password_enc=crypto.encrypt("secret"))),
    # a system with NO stored credential -> must produce a clear, actionable 400
    SystemEntry(sid="NOC", client="100", hostname="127.0.0.1",
                webgui_url=f"http://127.0.0.1:{PORT}/sap/bc/gui/sap/its/webgui"),
]).save(os.environ["SYSTEMS_PATH"])

import main as m  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


srv = subprocess.Popen([sys.executable, f"{BACKEND}/e2e/fake_sap_soap.py"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
c = TestClient(m.app)

try:
    r = c.get("/api/rfc/checks")
    check("1. GET /api/rfc/checks lists the available checks",
          r.status_code == 200 and len(r.json()) >= 5, r.text[:200])
    check("2. Each check names its T-code and function module",
          all(x["tcode"] and x["function"] for x in r.json()), r.json())

    r = c.post("/api/rfc/test-connection", json={"sid": "F4H"})
    body = r.json()
    check("3. Connection test succeeds using ONLY registered config",
          r.status_code == 200 and body.get("ok") and body.get("system") == "F4H", body)
    check("4. It derived the SOAP endpoint from the existing webgui_url",
          body.get("endpoint", "").endswith("/sap/bc/soap/rfc"), body.get("endpoint"))
    check("5. It reports real system identity (release/db/kernel)",
          body.get("release") == "757" and body.get("database") == "HDB", body)

    r = c.post("/api/rfc/test-connection", json={"sid": "NOC"})
    check("6. System without stored credentials -> actionable 400",
          r.status_code == 400 and "Systems page" in r.text, r.text[:200])

    r = c.post("/api/rfc/test-connection", json={"sid": "NOPE"})
    check("7. Unknown SID -> 404", r.status_code == 404, r.text[:120])

    r = c.post("/api/rfc/run", json={"sid": "F4H"})
    body = r.json()
    check("8. POST /api/rfc/run executes the full sweep",
          r.status_code == 200 and body.get("ok"), body.get("failed_checks"))
    by_id = {x["check_id"]: x for x in body["checks"]}
    check("9. SM50 work processes returned", len(by_id["work_processes"]["rows"]) == 3,
          by_id["work_processes"])
    check("10. SM37 jobs returned via the TBTCO table read (XBP not in default sweep)",
          "background_jobs" not in by_id and len(by_id["job_table"]["rows"]) == 2,
          list(by_id))
    check("11. Cancelled job surfaced as an anomaly",
          body["anomaly_count"] >= 1 and
          any("CANCELLED" in a for a in by_id["job_table"]["anomalies"]),
          body["anomaly_count"])
    r = c.post("/api/rfc/run", json={"sid": "F4H", "check_ids": ["background_jobs"]})
    check("11b. XBP still available on explicit request",
          r.json()["checks"][0]["check_id"] == "background_jobs", r.text[:200])

    r = c.post("/api/rfc/run", json={"sid": "F4H", "check_ids": ["system_info"]})
    check("12. check_ids filters the sweep to just that check",
          len(r.json()["checks"]) == 1 and r.json()["checks"][0]["check_id"] == "system_info",
          r.json()["checks"])

    # ---- raw diagnostic: the endpoint that ends field-name guesswork ----
    r = c.post("/api/rfc/raw", json={"sid": "F4H", "function": "TH_WPINFO",
                                      "tables": ["WPLIST"]})
    body = r.json()
    check("12b. POST /api/rfc/raw returns the ACTUAL response shape",
          r.status_code == 200 and body["ok"] and "WPLIST" in body["top_level_keys"],
          body if r.status_code != 200 else body.get("top_level_keys"))
    check("12c. raw reports table sizes and raw XML for diagnosis",
          body["table_sizes"].get("WPLIST") == 3 and "WPLIST" in (body.get("raw_xml") or ""),
          body.get("table_sizes"))
    r = c.post("/api/rfc/raw", json={"sid": "F4H", "function": "Z_NO_SUCH_FM"})
    check("12d. raw surfaces SAP's fault instead of throwing",
          r.status_code == 200 and not r.json()["ok"] and r.json()["error"], r.text[:200])

    # ---- isolation: existing surfaces unaffected by mounting this router ----
    check("13. ISOLATION: existing /api/flows still works",
          c.get("/api/flows").status_code == 200)
    check("14. ISOLATION: existing /api/systems still works",
          c.get("/api/systems").status_code == 200)
    check("15. ISOLATION: D-044 sandbox /api/sapgui/agents still works",
          c.get("/api/sapgui/agents").status_code == 200)
finally:
    srv.terminate()

print("D-045 RFC API: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
