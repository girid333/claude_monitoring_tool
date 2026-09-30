"""D-051 Phase 1: the AHC data-source probe, against an adversarial fake SAP
(fake_sap_ahc.py — SOAP RFC and WebGUI on one host, like a real ICM).

Proves the probe tells the truth about each source (works / reachable but
empty / fails and why), never calls anything off its allowlist, reads the
check list from SAP's own workbook, and falls back to the unmodified v1
engine for WebGUI evidence."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from cryptography.fernet import Fernet

os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
for k in ("NO_PROXY", "no_proxy"):
    os.environ[k] = "127.0.0.1,localhost"
BACKEND = str(Path(__file__).resolve().parents[1])
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work) / "runs.db")
for k in [k for k in os.environ if k.startswith("HANA_")]:
    del os.environ[k]
sys.path.insert(0, BACKEND)
sys.path.insert(0, str(Path(BACKEND) / "e2e" / "fixtures"))

from engine.registry import Registry, SystemEntry, SapCredential  # noqa: E402
from engine import crypto  # noqa: E402
from ahc import probe as P  # noqa: E402
from make_ahc_fixture import build  # noqa: E402

PORT = 8092
PASSWORD = "secret"
Registry(systems=[SystemEntry(
    sid="F4H", client="100", hostname="127.0.0.1",
    webgui_url=f"http://127.0.0.1:{PORT}/sap/bc/gui/sap/its/webgui",
    sap_credential=SapCredential(username="MONITOR", password_enc=crypto.encrypt(PASSWORD)),
)]).save(os.environ["SYSTEMS_PATH"])

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {str(d)[:400]}"))


srv = subprocess.Popen([sys.executable, f"{BACKEND}/e2e/fake_sap_ahc.py", str(PORT)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)
try:
    # ── 1. check list comes from SAP's own workbook ──────────────────────
    sample = Path(work) / "ahc_sample.xls"
    sample.write_text(build(), encoding="utf-8")
    specs = P.checks_from_sample(sample)
    keys = [s.key for s in specs]
    check("1. Every Monitors row of the sample becomes one probe check", len(specs) == 12, keys)
    check("2. Rows map the same way the D-050 parser maps them",
          keys[:8] == ["SM51", "SM21", "SM12", "SMLG", "CERT", "SLICENSE", "DB12", "DB02_ALERTS"]
          and "DB02_LOG" in keys, keys)
    check("3. A row with no catalogued source is kept, not dropped",
          any(k.startswith("UNMAPPED_") and s.tcode == "ZZ99" for k, s in zip(keys, specs)))
    check("4. Built-in catalogue covers 33 checks", len(P.CATALOGUE) == 33, len(P.CATALOGUE))

    # ── 2. full run through the CLI entry point ──────────────────────────
    out = Path(work) / "out"
    rc = P.main(["--sid", "F4H", "--out", str(out)])
    data = json.loads((out / "ahc_probe.json").read_text())
    md = (out / "ahc_probe.md").read_text()
    R = {r["key"]: r for r in data["results"]}
    att = lambda k, src: next((a for a in R[k]["attempts"] if a["source"].startswith(src)), {})  # noqa: E731
    check("5. CLI exits 0 and writes Markdown + JSON", rc == 0 and "| Chosen source |" in md)
    check("6. Header names the real system from RFC_SYSTEM_INFO", "F4H release 758" in data["header"],
          data["header"])
    check("7. SM51 confirmed via TH_SERVER_LIST with real rows",
          R["SM51"]["confirmed"] and R["SM51"]["chosen"] == "RFC TH_SERVER_LIST"
          and "nwrhel9_F4H_00" in R["SM51"]["sample"], R["SM51"])
    check("8. Reachable-but-empty is confirmed AND says so (VBHDR)",
          R["SM13"]["confirmed"] and "reachable, 0 rows" in R["SM13"]["sample"], R["SM13"]["sample"])
    check("9. Authorization failure is reported with SAP's reason (TSP01)",
          not R["SP01"]["confirmed"] and "NOT_AUTHORIZED" in att("SP01", "RFC_READ_TABLE TSP01")["detail"],
          att("SP01", "RFC_READ_TABLE TSP01"))
    check("10. Wide table read by fitting fields into 512 chars instead of failing (SOOS)",
          R["SOST"]["confirmed"] and "dropped to fit" in att("SOST", "RFC_READ_TABLE SOOS")["detail"],
          att("SOST", "RFC_READ_TABLE SOOS"))
    check("11. RFCDES read without RFCOPTIONS (500 chars) and the drop is reported",
          R["RSRFCCHK"]["confirmed"] and "dropped" in att("RSRFCCHK", "RFC_READ_TABLE RFCDES")["detail"])
    check("12. Missing field on this release is named (RZLLITAB.GROUPTYPE)",
          "GROUPTYPE" in att("SMLG", "RFC_READ_TABLE RZLLITAB")["detail"])
    check("13. WHERE on a missing field fails loudly, not as 'no data' (RZ12)",
          not R["RZ12"]["confirmed"] and "OPTION_NOT_VALID" in att("RZ12", "RFC_READ_TABLE")["detail"])
    check("14. Cluster/unknown table fails with SAP's reason (SNAP_BEG)",
          "TABLE_NOT_AVAILABLE" in att("ST22", "RFC_READ_TABLE SNAP_BEG")["detail"])
    check("15. Non-remote-enabled FM is skipped by TFDIR, next candidate chosen (ST06)",
          "not remote-enabled" in att("ST06", "RFC GET_CPU_ALL")["detail"]
          and R["ST06"]["chosen"] == "RFC GET_MEM_ALL")
    check("16. Single-row table (one <item>) still yields the row (ENQUEUE_READ)",
          R["SM12"]["confirmed"] and "VBAK" in R["SM12"]["sample"], R["SM12"])
    check("17. FM fault surfaces as failed with the reason (SWNC NO_DATA_FOUND)",
          "NO_DATA_FOUND" in att("ST03N", "RFC SWNC")["detail"])
    check("18. Exports-only FM counts as working (SAPTUNE)",
          R["ST02"]["confirmed"] and "SAPMEMORY_HITRATIO" in R["ST02"]["sample"], R["ST02"])
    check("19. Local-only caveat carried on shared sources (SM66 via TH_WPINFO)",
          "local instance only" in att("SM66", "RFC TH_WPINFO")["detail"]
          and "local instance only" not in att("SM50", "RFC TH_WPINFO")["detail"])
    check("20. HANA sources skipped with the env vars to set, not faked",
          not R["DB12"]["confirmed"] and "HANA_F4H_USER" in att("DB12", "HANA SQL")["detail"])
    check("21. WebGUI fallback offered but not run without --webgui",
          "--webgui" in att("SM21", "WebGUI SM21")["detail"])
    check("22. WebGUI not attempted where a structured source works",
          "not needed" in att("SM51", "WebGUI")["detail"])

    # ── 3. safety: only allowlisted modules are ever called ─────────────
    calls = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/calls", timeout=5).read())
    extra = set(calls) - P.ALLOWLIST - {"RFC_READ_TABLE", "RFC_SYSTEM_INFO"}
    check("23. No function module outside the allowlist was called", not extra, extra)
    check("24. Discovered modules are listed, never called",
          any("RSLG_READ_SYSLOG_REMOTE" in d for d in R["SM21"]["discovered"])
          and "RSLG_READ_SYSLOG_REMOTE" not in calls and "SLIC_GET_LICENCES" not in calls,
          R["SM21"]["discovered"])
    check("25. GET_CPU_ALL (not remote-enabled) was never invoked", "GET_CPU_ALL" not in calls)
    check("26. Shared sources are called once (TH_SERVER_LIST for SM51/RZ03/SMMS)",
          calls.count("TH_SERVER_LIST") == 1 and calls.count("TH_USER_LIST") == 1,
          {c: calls.count(c) for c in ("TH_SERVER_LIST", "TH_USER_LIST")})
    check("27. No credential in the output files (D-018)",
          PASSWORD not in md and PASSWORD not in (out / "ahc_probe.json").read_text())

    # ── 4. HANA prober ───────────────────────────────────────────────────
    h = P.HanaProber("F4H", env={"HANA_F4H_HOST": "h", "HANA_F4H_USER": "u", "HANA_F4H_PASSWORD": "p"})
    try:
        import hdbcli  # noqa: F401
        check("28. hdbcli present — prober tries to connect", "could not connect" in h.skip_reason or h.conn)
    except ImportError:
        check("28. Configured but no hdbcli -> says how to fix", "pip install hdbcli" in h.skip_reason,
              h.skip_reason)

    class Cur:
        def __init__(self, fail=False):
            self.fail, self.description = fail, [("STATE_NAME",), ("SYS_START_TIME",)]
        def execute(self, sql):
            if self.fail:
                raise RuntimeError("invalid table name: M_BACKUP_CATALOG")
        def fetchmany(self, n):
            return [("successful", "2026-09-29 22:00:01")]
        def close(self):
            pass

    class Conn:
        def __init__(self, fail=False):
            self.fail = fail
        def cursor(self):
            return Cur(self.fail)

    h.conn, h.skip_reason = Conn(), ""
    a = h.run(P.CATALOGUE["DB12"].sources[0])
    check("29. HANA row sampled when a connection exists", a.status == "ok" and "successful" in a.sample, a)
    h.conn = Conn(fail=True)
    a = h.run(P.CATALOGUE["DB12"].sources[0])
    check("30. HANA SQL error reported, not swallowed", a.status == "failed" and "invalid table" in a.detail, a)

    # ── 5. WebGUI fallback orchestration ────────────────────────────────
    asked = []

    def fake_webgui(tcodes):
        asked.append(tcodes)
        return {t: P.Attempt(f"WebGUI {t} (v1 engine)", "webgui", "ok", sample="screenshot 20 KB")
                for t in tcodes}

    client, sap_client = P._rfc_client_for("F4H", os.environ["SYSTEMS_PATH"])
    res = P.probe(specs, P.RfcProber(client, "F4H", sap_client), P.HanaProber("F4H"),
                  fake_webgui, discover=False)
    client.close()
    check("31. v1 engine asked ONCE, only for checks without a structured source",
          asked == [["DB02", "DB12", "SLICENSE", "SM21", "STRUST", "ZZ99"]], asked)
    byk = {r.key: r for r in res}
    check("32. Screenshot-only evidence is labelled as such",
          byk["SM21"].confirmed and byk["SM21"].chosen_kind == "webgui"
          and "yes (image only)" in P.to_markdown(res, "F4H"))

    # ── 6. the real v1 engine, unmodified, as the fallback ──────────────
    from engine.core_basis_wrap import load_v1_orchestrator
    from engine.sections import CoreBasisSection
    shots = asyncio.run(P.webgui_capture("F4H", ["SICK"], os.environ["SYSTEMS_PATH"]))
    check("33. v1 engine captured SICK through its generic path",
          shots["SICK"].status == "ok" and "generic screenshot" in shots["SICK"].detail, shots)
    orch = load_v1_orchestrator(CoreBasisSection()._resolve_v1_path())
    check("34. v1's T-code list restored after the probe (in-memory swap only)",
          len(orch.MONITORED_TCODES) == 23, len(orch.MONITORED_TCODES))
    import subprocess as sp
    diff = sp.run(["git", "-C", BACKEND, "status", "--porcelain", "v1_core"], capture_output=True, text=True)
    check("35. v1_core untouched on disk", diff.returncode == 0 and diff.stdout.strip() == "", diff.stdout)
finally:
    srv.terminate()

print("D-051 AHC PROBE: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
raise SystemExit(0 if ok else 1)
