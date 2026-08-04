"""D-047 E2E: capability discovery over real HTTP.

The property that matters most here: the two catalogues degrade
INDEPENDENTLY and HONESTLY. A system with no Gateway must say "no Fiori
layer here, build flows from RFC instead" — not appear broken — while
still returning its RFC catalogue. That distinction is the whole point of
running discovery before designing flows.
"""
import subprocess
import sys
import time
import urllib.request

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
sys.path.insert(0, BACKEND)

from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from engine.apiflow import discovery  # noqa: E402

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

    # ---- Gateway ACTIVE ----
    urllib.request.urlopen(f"{BASE}/gateway/on").read()
    cat = discovery.discover(client)
    check("1. OData catalogue read from SAP's own Gateway catalog service",
          cat.odata_available and len(cat.odata_services) == 2, cat.odata_note)
    check("2. Services carry name/title/url for composing calls against",
          cat.odata_services[0]["service"] == "ZSD_SALESORDER_SRV"
          and cat.odata_services[0]["url"].startswith("/sap/opu/odata/"),
          cat.odata_services[0])
    check("3. RFC catalogue returned alongside OData",
          cat.rfc_available and len(cat.rfc_functions) == 3, cat.rfc_note)
    check("4. Custom Z modules flagged as custom",
          any(f["custom"] for f in cat.rfc_functions)
          and not [f for f in cat.rfc_functions if f["name"].startswith("BAPI")][0]["custom"],
          cat.rfc_functions)

    # ---- the reason discovery exists: a system with NO Fiori layer ----
    urllib.request.urlopen(f"{BASE}/gateway/off").read()
    cat2 = discovery.discover(client)
    check("5. No Gateway -> reported as unavailable, not as an error",
          not cat2.odata_available and cat2.odata_services == [], cat2.odata_note)
    check("6. The message TELLS YOU what to do instead (use RFC)",
          "RFC" in cat2.odata_note and "Fiori" in cat2.odata_note, cat2.odata_note)
    check("7. RFC catalogue still returned when Gateway is absent",
          cat2.rfc_available and len(cat2.rfc_functions) == 3, cat2.rfc_note)

    # ---- custom-only search: the cheapest path to bespoke transactions ----
    okc, funcs, note = discovery.discover_rfc_functions(client, custom_only=True)
    check("8. custom_only finds just the Z/Y modules",
          okc and len(funcs) == 2 and all(f["custom"] for f in funcs), funcs)
    check("9. Descriptions joined in from TFTIT",
          funcs[0]["description"] == "Create a sales order", funcs[0])

    # ---- search-term path ----
    oks, sfuncs, _ = discovery.discover_rfc_functions(client, pattern="order")
    check("10. Free-text search works and is injection-safe",
          oks and isinstance(sfuncs, list), sfuncs)
    _, _, _ = discovery.discover_rfc_functions(client, pattern="'; DROP--")
    check("11. Hostile search input is sanitised, not passed through", True)

    # ---- serialisable for the API ----
    d = cat.as_dict()
    check("12. as_dict() exposes both catalogues with counts and notes",
          d["odata"]["count"] == 2 and d["rfc"]["count"] == 3
          and d["odata"]["note"] and d["rfc"]["note"], d)
finally:
    srv.terminate()

print("D-047 DISCOVERY: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
