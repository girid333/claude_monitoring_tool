"""D-032: event ordering (last T-code goes green), report delete, run detail."""
import asyncio, os, sys, tempfile, json
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work)/"systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work)/"runs.db")
sys.path.insert(0, BACKEND)
from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="ORD", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8099/sap/bc/gui/sap/its/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])
import main
from engine.core_basis_wrap import load_v1_orchestrator
_m = load_v1_orchestrator(Path(BACKEND)/"v1_core")
_m.MONITORED_TCODES[:] = _m.MONITORED_TCODES[:3]
LAST = _m.MONITORED_TCODES[-1]["tcode"]
ok = True
def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

async def go():
    rid = "ord1"
    q = asyncio.Queue(); main._event_queues[rid] = q
    await main._execute_run(rid, main.RunRequest(sids=["ORD"], flows=[], include_core_basis=True))
    evs = []
    while not q.empty(): evs.append(await q.get())

    # ORDERING: no core_basis_progress may appear AFTER core_basis_done
    types = [e["type"] for e in evs]
    i_done = types.index("core_basis_done")
    after = [t for t in types[i_done+1:] if t == "core_basis_progress"]
    check("1. No progress events land after core_basis_done (ordering fixed)",
          len(after) == 0, f"{len(after)} stragglers")

    # simulate the UI reducer to confirm the last T-code ends green
    status = {}
    for e in evs:
        if e["type"] == "core_basis_start":
            status = {t: "pending" for t in e["tcodes"]}
        elif e["type"] == "core_basis_progress" and e.get("tcode"):
            for k in status:
                if status[k] == "running": status[k] = "done"
            status[e["tcode"]] = "warn" if e["level"] == "warn" else "running"
        elif e["type"] in ("core_basis_done", "run_done"):
            for k in status:
                if status[k] == "running": status[k] = "done"
    check(f"2. Last T-code ({LAST}) ends green, not stuck running",
          status.get(LAST) in ("done", "warn"), f"{LAST}={status.get(LAST)}")
    check("3. ALL T-codes resolved (none left running/pending)",
          all(v in ("done", "warn") for v in status.values()), json.dumps(status))
    return evs

evs = asyncio.run(go())

# ---- report delete + run detail via the API ----
from fastapi.testclient import TestClient
c = TestClient(main.app)
reps = c.get("/api/reports").json()
check("4. A report was recorded for the run", len(reps) == 1, json.dumps(reps)[:120])
if reps:
    r = reps[0]
    files = [p for p in (r["docx_path"], r["pdf_path"]) if p]
    check("5. Report files exist on disk", all(Path(p).exists() for p in files), str(files))
    det = c.get(f"/api/runs/core-basis/{r['run_id']}/{r['sid']}")
    check("6. Run detail endpoint returns 200", det.status_code == 200, det.text[:120])
    if det.status_code == 200:
        j = det.json()
        check("7. Detail has per-T-code anomaly breakdown",
              "tcodes_with_anomalies" in j and "external_checks" in j, json.dumps(j)[:150])
        tot = sum(len(t["anomalies"]) for t in j["tcodes_with_anomalies"])
        print(f"      {len(j['tcodes_with_anomalies'])} T-codes with anomalies, {tot} total; "
              f"clean: {j['tcodes_clean']}")
    d = c.delete(f"/api/reports/{r['run_id']}/{r['sid']}")
    check("8. DELETE /api/reports returns 200", d.status_code == 200, d.text[:120])
    check("9. Report files actually removed from disk",
          all(not Path(p).exists() for p in files), str(files))
    check("10. Report row gone from the list", len(c.get("/api/reports").json()) == 0)
    check("11. Deleting again -> 404",
          c.delete(f"/api/reports/{r['run_id']}/{r['sid']}").status_code == 404)
print("D-032: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
