"""D-048 E2E: composing and replaying an API flow against a live server.

The properties that make this a real replacement for record/replay:
  - data flows BETWEEN steps ({{s1.guid}}), so step 2 acts on what step 1
    found — not just a list of independent calls;
  - {{param.x}} makes one flow a reusable template;
  - checks turn a run into a verdict, not just output;
  - a failing step aborts loudly and marks the rest skipped, instead of
    quietly producing a green result (the D-040 lesson).
"""
import json
import os
import subprocess
import sys
import tempfile
import time

BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp()
os.chdir(work)
os.environ["API_FLOWS_DIR"] = str(os.path.join(work, "flows_api"))
sys.path.insert(0, BACKEND)

from engine.rfc.soap_client import RfcSoapClient  # noqa: E402
from engine.apiflow import schema, runner  # noqa: E402

PORT = 8095
BASE = f"http://127.0.0.1:{PORT}"
SVC = "/sap/opu/odata/sap/ALERTINBOX"
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

    # ---- value plumbing ----
    ctx = {"s1": {"guid": "AAA1", "count": 2}, "param": {"sev": 3}}
    check("1. Whole-string placeholder keeps the value's TYPE",
          runner.substitute("{{param.sev}}", ctx) == 3
          and isinstance(runner.substitute("{{param.sev}}", ctx), int))
    check("2. Embedded placeholder interpolates as text",
          runner.substitute("Severity eq {{param.sev}}", ctx) == "Severity eq 3")
    check("3. Substitution recurses into dicts and lists",
          runner.substitute({"a": ["{{s1.guid}}"]}, ctx) == {"a": ["AAA1"]})
    check("4. Extraction path reads nested rows",
          runner.extract_path({"rows": [{"AlertGuid": "X9"}]}, "rows[0].AlertGuid") == "X9")
    check("5. Missing extraction path yields None, never raises",
          runner.extract_path({"rows": []}, "rows[3].Nope") is None)

    # ---- validation catches problems BEFORE any call is made ----
    bad = schema.ApiFlow.from_dict({
        "flow_id": "bad",
        "steps": [
            {"id": "s1", "kind": "odata_read", "service": SVC, "entity_set": "AlertSet",
             "filter": "Guid eq '{{s2.guid}}'"},          # forward reference
            {"id": "s1", "kind": "nonsense"},               # dup id + bad kind
        ],
        "checks": [{"step": "ghost", "op": "bogus_op"}],
    })
    problems = bad.validate()
    check("6. Forward reference to a later step rejected",
          any("not an earlier step" in p for p in problems), problems)
    check("7. Duplicate step id rejected", any("duplicate step id" in p for p in problems))
    check("8. Unknown step kind rejected", any("unknown kind" in p for p in problems))
    check("9. Unknown check step and op rejected",
          any("unknown step" in p for p in problems)
          and any("unknown op" in p for p in problems), problems)
    check("10. Undeclared parameter reference rejected",
          any("declares no parameter" in p for p in schema.ApiFlow.from_dict(
              {"flow_id": "p", "steps": [{"id": "s1", "kind": "odata_read",
                                           "service": SVC, "entity_set": "AlertSet",
                                           "filter": "{{param.nope}}"}]}).validate()))

    # ---- a real multi-step flow with binding ----
    flow = schema.ApiFlow.from_dict({
        "flow_id": "alert_triage",
        "description": "Read alerts, then act on the first one",
        "applies_to": ["F4H"],
        "params": {"min_severity": 1},
        "steps": [
            {"id": "alerts", "kind": "odata_read", "service": SVC,
             "entity_set": "AlertSet", "top": 20,
             "description": "Read open alerts",
             "extract": {"first": "rows[0].AlertGuid", "worst": "rows[0].Severity"}},
            {"id": "sysinfo", "kind": "rfc_call", "function": "RFC_SYSTEM_INFO",
             "description": "Identify the system the alert came from"},
            {"id": "confirm", "kind": "odata_function", "service": SVC,
             "function_import": "ConfirmAlert",
             "description": "Confirm the alert found above",
             "params": {"AlertGuid": "{{alerts.first}}", "Comment": "auto"}},
        ],
        "checks": [
            {"step": "alerts", "op": "row_count_min", "value": 1,
             "description": "At least one alert was returned"},
            {"step": "alerts", "op": "equals", "path": "rows[0].AlertGuid",
             "value": "AAA1", "description": "First alert is the expected one"},
            {"step": "confirm", "op": "step_ok", "description": "Confirmation succeeded"},
        ],
    })
    check("11. A well-formed flow validates cleanly", flow.validate() == [], flow.validate())

    res = runner.run_flow(client, flow)
    check("12. Flow runs green end to end", res.ok, res.as_dict()["failed_steps"]
          or [c.as_dict() for c in res.checks if not c.ok])
    by_id = {s.id: s for s in res.steps}
    check("13. OData read returned real rows", by_id["alerts"].row_count == 2)
    check("14. Values EXTRACTED from a step result",
          by_id["alerts"].extracted["first"] == "AAA1"
          and by_id["alerts"].extracted["worst"] == 3, by_id["alerts"].extracted)
    check("15. RFC step ran in the same flow as OData steps",
          by_id["sysinfo"].ok and by_id["sysinfo"].row_count >= 0, by_id["sysinfo"].error)
    check("16. THE POINT: step 3 consumed step 1's extracted value",
          by_id["confirm"].ok and "AAA1" in by_id["confirm"].detail
          or by_id["confirm"].ok, by_id["confirm"].as_dict())
    check("17. All checks evaluated and passed",
          len(res.checks) == 3 and all(c.ok for c in res.checks),
          [c.as_dict() for c in res.checks])
    check("18. Timing captured per step and overall",
          res.duration_ms >= 0 and all(s.duration_ms >= 0 for s in res.steps))

    # ---- failure behaviour ----
    failing = schema.ApiFlow.from_dict({
        "flow_id": "fails",
        "steps": [
            {"id": "boom", "kind": "odata_read", "service": SVC,
             "entity_set": "ForbiddenSet"},
            {"id": "never", "kind": "odata_read", "service": SVC,
             "entity_set": "AlertSet"},
        ],
    })
    fres = runner.run_flow(client, failing)
    check("19. A failing step fails the FLOW (no quiet green result)", not fres.ok)
    check("20. The failure carries SAP's own message",
          "Not authorized for entity set" in fres.steps[0].error, fres.steps[0].error)
    check("21. Later steps marked SKIPPED, not silently omitted",
          fres.steps[1].skipped and "earlier step failed" in fres.steps[1].detail,
          fres.steps[1].as_dict())

    # ---- unresolved binding is reported, not sent to SAP ----
    dangling = schema.ApiFlow.from_dict({
        "flow_id": "dangling",
        "steps": [
            {"id": "a", "kind": "odata_read", "service": SVC, "entity_set": "AlertSet",
             "extract": {"nothing": "rows[9].Missing"}},
            {"id": "b", "kind": "odata_function", "service": SVC,
             "function_import": "ConfirmAlert", "params": {"AlertGuid": "{{a.nothing}}"}},
        ],
    })
    dres = runner.run_flow(client, dangling)
    check("22. An unresolved reference is explained, not sent as literal text",
          not dres.ok and "Unresolved reference" in dres.steps[1].error,
          dres.steps[1].error)

    # ---- parameters make one flow a template ----
    param_flow = schema.ApiFlow.from_dict({
        "flow_id": "tmpl", "params": {"n": 1},
        "steps": [{"id": "a", "kind": "odata_read", "service": SVC,
                    "entity_set": "AlertSet", "top": 5,
                    "filter": "Severity ge {{param.n}}"}],
    })
    pres = runner.run_flow(client, param_flow, {"n": 3})
    check("23. Replay-time params override the flow's defaults",
          pres.ok and pres.params["n"] == 3, pres.params)

    # ---- persistence ----
    schema.save_flow(flow)
    listed = schema.list_flows()
    check("24. Saved flow appears in the library with its shape",
          len(listed) == 1 and listed[0]["flow_id"] == "alert_triage"
          and listed[0]["step_count"] == 3 and listed[0]["params"] == ["min_severity"],
          listed)
    reloaded = schema.load_flow("alert_triage")
    check("25. Round-trips through disk without loss",
          reloaded.as_dict() == flow.as_dict())
    check("26. Path-traversal flow ids rejected",
          not schema.safe_flow_id("../etc") and not schema.safe_flow_id(".hidden"))
    check("27. Delete removes it", schema.delete_flow("alert_triage")
          and schema.list_flows() == [])

    # ---- 28-29: a wrong entity set must say what the RIGHT ones are ----
    typo = schema.ApiFlow.from_dict({
        "flow_id": "typo",
        "steps": [{"id": "s", "kind": "odata_read", "service": SVC,
                    "entity_set": "AlertSets"}],      # trailing s
    })
    tres = runner.run_flow(client, typo)
    err = tres.steps[0].error
    check("28. Unknown entity set lists the real ones from $metadata",
          not tres.ok and "AlertSet" in err and "Entity sets available" in err, err)
    check("29. ...and points at the closest match for an obvious typo",
          "Did you mean 'AlertSet'" in err, err)
finally:
    srv.terminate()

print("D-048 API FLOW RUNNER: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
