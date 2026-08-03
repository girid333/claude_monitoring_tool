"""D-045 E2E: RFC-over-SOAP against a real HTTP server speaking SAP's
/sap/bc/soap/rfc dialect — real sockets, real Basic auth, real namespaced
XML. Proves the whole path works with NO pyrfc and NO NW RFC SDK present
(neither is installed here, which is the point).

Explicitly covers the failure modes that burned us in D-044, where things
failed silently: wrong credentials, inactive SICF service, SOAP faults,
and single-row tables. Every one must produce a LOUD, specific error
rather than empty-looking success.
"""
import os
import sys
import subprocess
import time
from pathlib import Path

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
sys.path.insert(0, BACKEND)

from engine.rfc.soap_client import (  # noqa: E402
    RfcSoapClient, RfcSoapError, derive_soap_url, build_envelope, parse_response)
from engine.rfc import basis_checks  # noqa: E402

PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"

ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


# start the fake SAP ICM in THIS bash invocation (D-030: per-call PID namespace)
srv = subprocess.Popen([sys.executable, f"{BACKEND}/e2e/fake_sap_soap.py"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)

try:
    # ---- 0. no SDK present (this is the whole reason for this approach) ----
    try:
        import pyrfc  # noqa: F401
        have_pyrfc = True
    except ImportError:
        have_pyrfc = False
    check("0. Works with NO pyrfc/NW RFC SDK installed", not have_pyrfc,
          "pyrfc present — the test is no longer proving the SDK-free path")

    # ---- 1. URL derivation from the EXISTING webgui_url (no new config) ----
    got = derive_soap_url("http://sapf4h01:8000/sap/bc/gui/sap/its/webgui?sap-client=100")
    check("1. SOAP endpoint derived from the existing webgui_url",
          got == "http://sapf4h01:8000/sap/bc/soap/rfc", got)
    try:
        derive_soap_url("not-a-url")
        check("2. Malformed webgui_url rejected clearly", False, "no error raised")
    except RfcSoapError as exc:
        check("2. Malformed webgui_url rejected clearly", "webgui_url" in exc.message, exc.message)

    # ---- 3. envelope uses SAP's exact required namespace ----
    env = build_envelope("RFC_SYSTEM_INFO", {}).decode()
    check("3. Envelope carries the exact SAP RFC namespace",
          "urn:sap-com:document:sap:rfc:functions" in env, env[:200])

    client = RfcSoapClient.from_system(f"{BASE}/sap/bc/gui/sap/its/webgui",
                                        client="100", username="MONITOR", password="secret")

    # ---- 4. real call over real HTTP ----
    info = client.ping()
    sysid = (info.get("RFCSI_EXPORT") or {}).get("RFCSYSID")
    check("4. RFC_SYSTEM_INFO round-trips over real HTTP", sysid == "F4H", info)

    # ---- 5. bad credentials must fail LOUDLY, not return empty ----
    bad = RfcSoapClient.from_system(f"{BASE}/sap/bc/gui/sap/its/webgui",
                                     client="100", username="MONITOR", password="wrong")
    try:
        bad.ping()
        check("5. Wrong password raises (never silent empty success)", False, "no error raised")
    except RfcSoapError as exc:
        check("5. Wrong password raises (never silent empty success)",
              exc.kind == "auth" and "authorization" in exc.message.lower(), exc.message)

    # ---- 6. inactive SICF service -> actionable message naming SICF ----
    off = RfcSoapClient(type(client.target)(soap_url=f"{BASE}/inactive", username="MONITOR",
                                             password="secret", client="100"))
    try:
        off.ping()
        check("6. Inactive SICF service raises with a fix hint", False, "no error raised")
    except RfcSoapError as exc:
        check("6. Inactive SICF service raises with a fix hint",
              exc.kind == "not_enabled" and "SICF" in exc.message, exc.message)

    # ---- 7. SOAP Fault surfaces the SAP message ----
    try:
        client.call("Z_DOES_NOT_EXIST")
        check("7. SOAP Fault surfaces SAP's own message", False, "no error raised")
    except RfcSoapError as exc:
        check("7. SOAP Fault surfaces SAP's own message",
              exc.kind == "fault" and "No RFC authorization" in exc.message, exc.message)

    # ---- 8-10. work processes: rows + real anomalies ----
    wp = next(c for c in basis_checks.CHECKS if c.check_id == "work_processes").run(client)
    check("8. SM50 check ok with all three work processes",
          wp.ok and len(wp.rows) == 3, f"ok={wp.ok} rows={len(wp.rows)} err={wp.error}")
    check("9. STOPPED work process flagged as an anomaly",
          any("stopped" in a.lower() for a in wp.anomalies), wp.anomalies)
    check("10. PRIV-mode work process flagged as an anomaly",
          any("PRIV" in a for a in wp.anomalies), wp.anomalies)

    # ---- 11. single-row table must NOT be lost ----
    sl = next(c for c in basis_checks.CHECKS if c.check_id == "server_list").run(client)
    check("11. SINGLE-row table parsed as one row (not dropped)",
          sl.ok and len(sl.rows) == 1 and sl.rows[0]["Host"] == "sapf4h01",
          f"ok={sl.ok} rows={sl.rows} err={sl.error}")

    # ---- 12-13. SM37 via the XBP interface (logon/select/logoff) ----
    jobs = basis_checks.run_job_check(client)
    check("12. SM37 via XBP returns both jobs", jobs.ok and len(jobs.rows) == 2,
          f"ok={jobs.ok} rows={jobs.rows} err={jobs.error}")
    check("13. CANCELLED job flagged as an anomaly",
          any("Z_FAILED_LOAD" in a and "CANCELLED" in a for a in jobs.anomalies), jobs.anomalies)

    # ---- 14. RFC_READ_TABLE fixed-width split into named columns ----
    jt = next(c for c in basis_checks.CHECKS if c.check_id == "job_table").run(client)
    check("14. RFC_READ_TABLE fixed-width rows split into named columns",
          jt.ok and len(jt.rows) == 2 and jt.rows[0]["Job"] == "NIGHTLY_BATCH"
          and jt.rows[0]["User"] == "GIRID",
          f"ok={jt.ok} rows={jt.rows} err={jt.error}")
    check("14b. TBTCO job-table check flags the CANCELLED job too (XBP-independent)",
          any("Z_FAILED_LOAD" in a and "CANCELLED" in a for a in jt.anomalies), jt.anomalies)

    # ---- 15-16. run_all: isolation + no silent success ----
    results = basis_checks.run_all(client)
    check("15. run_all returns every registered check", len(results) == len(basis_checks.CHECKS) + 1,
          [r.check_id for r in results])
    failed = [r for r in results if not r.ok]
    check("16. All checks pass against the fake system",
          not failed, [(r.check_id, r.error) for r in failed])

    # ---- 17. a broken system reports per-check errors, never silent empties ----
    broken = RfcSoapClient.from_system("http://127.0.0.1:9/sap/bc/gui/sap/its/webgui",
                                        client="100", username="X", password="Y")
    broken_results = basis_checks.run_all(broken)
    check("17. Unreachable system: EVERY check reports an explicit error",
          all((not r.ok) and r.error for r in broken_results),
          [(r.check_id, r.ok, r.error[:60]) for r in broken_results])

    # ---- 18. the checks are self-describing for the UI ----
    described = basis_checks.describe_checks()
    check("18. describe_checks() exposes tcode/function/confidence for each",
          all(d.get("tcode") and d.get("function") and d.get("confidence") for d in described),
          described)

    # ---- 19-21. THE LIVE-F4H BUGS, reproduced and fixed ----
    # 19: table arriving under a different name (release variation) must
    #     still be found, instead of "succeeded, 0 rows".
    alt = basis_checks._first_table(client.call("TH_WPINFO_ALT"), "WPLIST")
    check("19. Table under an UNEXPECTED name is still found (no silent 0 rows)",
          len(alt) == 2, alt)

    # 20: SNAP is a cluster table -> RFC_READ_TABLE genuinely can't read it.
    #     Must surface as a real failure, and no shipped check may use it.
    try:
        client.call("RFC_READ_TABLE", QUERY_TABLE="SNAP")
        check("20. SNAP cluster-table read fails loudly", False, "no error raised")
    except RfcSoapError as exc:
        check("20. SNAP cluster-table read fails loudly",
              "TABLE_NOT_AVAILABLE" in exc.message, exc.message)
    check("21. No shipped check queries the unreadable SNAP table",
          all(c.params.get("QUERY_TABLE") != "SNAP" for c in basis_checks.CHECKS),
          [c.params.get("QUERY_TABLE") for c in basis_checks.CHECKS])

    # ---- 22. zero rows where rows are expected is FLAGGED, not silent ----
    class _Empty:
        def call(self, fn, **kw):
            return {"WPLIST": []}
    empty = basis_checks.RfcCheck(
        check_id="t", title="t", tcode="SM50", function="TH_WPINFO",
        interpret=basis_checks._interpret_wpinfo).run(_Empty())
    check("22. Succeeded-but-empty is flagged as suspicious, not clean",
          empty.ok and empty.suspicious and "no rows" in empty.suspicious, empty.as_dict())

    # ---- 23. session/cookies persist across calls (XBP needs this) ----
    check("23. Client reuses ONE HTTP session (cookie jar for XBP/XMI state)",
          hasattr(client, "_http") and client._http is not None)

    # ---- 24. raw diagnostic returns the actual shape ----
    parsed, rawxml = client.call_raw("TH_WPINFO")
    check("24. call_raw() exposes parsed keys AND raw XML for diagnosis",
          "WPLIST" in parsed and b"WPLIST" in rawxml, sorted(parsed.keys()))

finally:
    srv.terminate()

print("D-045 RFC-over-SOAP: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
