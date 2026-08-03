"""D-046 E2E: eCATT availability gate + object listing, over real HTTP.

The point of these checks is that the PREREQUISITE is reported honestly
BEFORE anything is executed. eCATT is deliberately disabled on most
production clients (it can change data), so "eCATT is switched off here"
must be a clear, actionable answer — not a confusing runtime failure from
ECATT_EXECUTE later.
"""
import subprocess
import sys
import time
import urllib.request

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
sys.path.insert(0, BACKEND)

from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from engine.rfc import ecatt  # noqa: E402

PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"
ok = True


def check(n, c, d=""):
    global ok
    ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))


srv = subprocess.Popen([sys.executable, f"{BACKEND}/e2e/fake_sap_soap.py"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(1.2)

try:
    client = RfcSoapClient.from_system(f"{BASE}/sap/bc/gui/sap/its/webgui",
                                        client="100", username="MONITOR", password="secret")

    # ---- eCATT PERMITTED ----
    urllib.request.urlopen(f"{BASE}/ecatt-flag/X").read()
    st = ecatt.ecatt_status(client)
    check("1. Status read from the real SCC4 flag in T000", st.ok, st.as_dict())
    check("2. eCATT correctly reported as ALLOWED when the flag permits it",
          st.allowed and st.state == "allowed", st.as_dict())
    check("3. Client name and raw flag surfaced for transparency",
          st.client_name == "Home S/4" and st.raw_flag == "X", st.as_dict())
    check("4. Existing eCATT objects counted", st.counts.get("ECSC") == 2, st.counts)

    # ---- object listing ----
    scripts = ecatt.list_objects(client, "ECSC")
    check("5. Test scripts (ECSC) listed with package and author",
          len(scripts) == 2 and scripts[0]["name"] == "Z_SM37_CHECK"
          and scripts[0]["package"] == "ZMON", scripts)
    check("6. Object kind carries a human label", scripts[0]["kind_label"] == "Test script",
          scripts[0])
    configs = ecatt.list_objects(client, "ECTC")
    check("7. Test configurations (ECTC) listed separately",
          len(configs) == 1 and configs[0]["name"] == "Z_SM37_CHECK_CFG", configs)
    try:
        ecatt.list_objects(client, "NOPE")
        check("8. Unknown object kind rejected", False, "no error raised")
    except ValueError as exc:
        check("8. Unknown object kind rejected", "NOPE" in str(exc), str(exc))

    # ---- eCATT BLOCKED (the realistic production default) ----
    urllib.request.urlopen(f"{BASE}/ecatt-flag/blank").read()
    st2 = ecatt.ecatt_status(client)
    check("9. Locked-down client reported as NOT allowed",
          st2.ok and not st2.allowed and st2.state == "not_allowed", st2.as_dict())
    check("10. The message names SCC4 and explains it's often intentional",
          "SCC4" in st2.message and "production" in st2.message, st2.message)
    check("11. Blocked state is still ok=True (a clear answer, not an error)",
          st2.ok and not st2.error, st2.as_dict())

    # ---- trusted-RFC-only variant ----
    urllib.request.urlopen(f"{BASE}/ecatt-flag/T").read()
    st3 = ecatt.ecatt_status(client)
    check("12. 'Trusted RFC only' recognised as a distinct state",
          st3.state == "trusted_only" and st3.allowed, st3.as_dict())
finally:
    srv.terminate()

print("D-046 eCATT: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
