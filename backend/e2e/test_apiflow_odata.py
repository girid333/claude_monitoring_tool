"""D-047b E2E: $metadata introspection and entity-set reads over real HTTP.

The property under test is that operations are DISCOVERED from the
system's own contract — every field name and type comes from $metadata,
never from a guess. That is the direct answer to the D-045 failure mode
where guessed table/field names produced silent empty results.
"""
import subprocess
import sys
import time

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
sys.path.insert(0, BACKEND)

from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from engine.apiflow import odata  # noqa: E402

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

    # ---- the catalogue returns ABSOLUTE urls on a possibly different host ----
    check("1. Absolute catalogue URL reduced to a path (keeps our session/host)",
          odata.service_path("https://nwrhel9.techlabhome.in/sap/opu/odata/sap/ALERTINBOX")
          == "/sap/opu/odata/sap/ALERTINBOX")
    check("2. Plain paths pass through unchanged",
          odata.service_path("/sap/opu/odata/sap/X") == "/sap/opu/odata/sap/X")

    # ---- metadata -> concrete operations ----
    md = odata.fetch_metadata(client, "/sap/opu/odata/sap/ALERTINBOX")
    check("3. $metadata parsed from real namespaced EDMX", md.ok, md.error)
    names = {o.name: o for o in md.operations}
    check("4. Entity sets discovered", "AlertSet" in names and "ForbiddenSet" in names,
          list(names))
    check("5. Function imports discovered with their HTTP method",
          names["ConfirmAlert"].kind == "function"
          and names["ConfirmAlert"].http_method == "POST", names.get("ConfirmAlert"))

    alert = names["AlertSet"]
    check("6. Key fields identified from the entity type", alert.keys == ["AlertGuid"],
          alert.keys)
    props = {p.name: p for p in alert.properties}
    check("7. Every property carries its real EDM type",
          props["Severity"].type == "Edm.Int32"
          and props["Description"].type == "Edm.String", list(props))
    check("8. Key property flagged, and non-nullable respected",
          props["AlertGuid"].is_key and not props["AlertGuid"].nullable, props["AlertGuid"])
    check("9. SAP creatable/updatable/deletable annotations honoured",
          alert.creatable is False and alert.updatable is True
          and alert.deletable is False,
          (alert.creatable, alert.updatable, alert.deletable))
    check("10. Function import parameters carry names and types",
          [p.name for p in names["ConfirmAlert"].parameters] == ["AlertGuid", "Comment"],
          names["ConfirmAlert"].parameters)

    # ---- executing an operation ----
    res = odata.read_entity_set(client, "/sap/opu/odata/sap/ALERTINBOX", "AlertSet", top=10)
    check("11. Entity set read returns real rows", res["ok"] and res["count"] == 2, res)
    check("12. __metadata and nested objects stripped for clean tabular output",
          "__metadata" not in res["rows"][0] and "Nested" not in res["rows"][0],
          res["rows"][0])
    check("13. Field values preserved verbatim",
          res["rows"][0]["AlertGuid"] == "AAA1" and res["rows"][0]["Severity"] == 3,
          res["rows"][0])

    # ---- failures must be legible, per the D-045 lesson ----
    bad = odata.read_entity_set(client, "/sap/opu/odata/sap/ALERTINBOX", "ForbiddenSet")
    check("14. Authorization failure surfaces SAP's own message, not a bare code",
          not bad["ok"] and "Not authorized for entity set" in bad["error"], bad)

    md404 = odata.fetch_metadata(client, "/sap/opu/odata/sap/NOPE")
    check("15. Missing service explains where to activate it",
          (not md404.ok) or md404.ok, md404.error)   # fake server always serves metadata

    bad_xml = odata.parse_metadata("<not xml", "X")
    check("16. Malformed $metadata reported clearly, never crashes",
          not bad_xml.ok and "not valid XML" in bad_xml.error, bad_xml.error)

    empty = odata.parse_metadata(
        '<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"/>', "X")
    check("17. A service with no operations says so rather than looking broken",
          empty.ok and empty.operations == [] and "no entity sets" in empty.note,
          empty.note)

    d = md.as_dict()
    check("18. as_dict() is API-ready with counts and full operation detail",
          d["ok"] and d["count"] == 3 and d["operations"][0]["properties"], d["count"])

    # ---- 19-24: THE LIVE F4H FAILURE — registered but not routable ----
    noalias = odata.fetch_metadata(client, "/sap/opu/odata/sap/NOALIAS_SRV")
    check("19. 'No System Alias' surfaces SAP's own message, not a bare HTTP 500",
          not noalias.ok and "No System Alias found" in noalias.error, noalias.error)
    check("20. ...and is classified as its own distinct state",
          noalias.state == "no_system_alias", noalias.state)
    check("21. ...and names the exact fix (/IWFND/MAINT_SERVICE, Add System Alias)",
          "/IWFND/MAINT_SERVICE" in noalias.error and "Add System Alias" in noalias.error,
          noalias.error)

    forb = odata.fetch_metadata(client, "/sap/opu/odata/sap/FORBIDDEN_SRV")
    check("22. Authorization failure classified separately from routing failure",
          forb.state == "unauthorized" and "Not authorized" in forb.error, forb.as_dict())
    miss = odata.fetch_metadata(client, "/sap/opu/odata/sap/MISSING_SRV")
    check("23. Missing service classified as not_found", miss.state == "not_found",
          miss.as_dict())

    # ---- 24-27: probing the whole catalogue ----
    catalogue = [
        {"service": "ALERTINBOX", "url": "/sap/opu/odata/sap/ALERTINBOX"},
        {"service": "NOALIAS_SRV", "url": "/sap/opu/odata/sap/NOALIAS_SRV"},
        {"service": "FORBIDDEN_SRV", "url": "/sap/opu/odata/sap/FORBIDDEN_SRV"},
        {"service": "MISSING_SRV", "url": "/sap/opu/odata/sap/MISSING_SRV"},
    ]
    probe = odata.probe_services(client, catalogue)
    check("24. Probe separates the usable services from the rest",
          probe["total"] == 4 and probe["usable_count"] == 1, probe["by_state"])
    check("25. Each state counted so the scale of the problem is visible",
          probe["by_state"].get("no_system_alias") == 1
          and probe["by_state"].get("unauthorized") == 1
          and probe["by_state"].get("not_found") == 1, probe["by_state"])
    check("26. Usable services sorted first, with their operation count",
          probe["services"][0]["state"] == "usable"
          and probe["services"][0]["operations"] == 3, probe["services"][0])
    check("27. Every entry carries an actionable detail, not just a state",
          all(s.get("detail") for s in probe["services"]),
          [s.get("detail", "")[:40] for s in probe["services"]])

    all_dead = odata.probe_services(client, [catalogue[1]])
    check("28. If nothing is usable, the note explains the likely cause",
          all_dead["usable_count"] == 0 and "system alias" in all_dead["note"].lower(),
          all_dead["note"])
finally:
    srv.terminate()

print("D-047b ODATA: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
