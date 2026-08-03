"""D-040: selecting N recordings must produce N documents.

Two checks, at two different layers:

1. sections.build_reports() in isolation — two FlowRunResults with
   DIFFERENT flow_ids must produce two separate flow documents, and if one
   flow's document generation itself raises, the other must still come out
   (before this fix, one bad doc took the whole build_reports() call down,
   so NEITHER artifact reached the caller).

2. The real main.py run path (_execute_run_inner, unpatched) with two
   selected flows where the SECOND is a corrupt flow file (malformed JSON —
   the kind of thing an older/hand-edited recording could produce). Before
   this fix, that raised out of the flow loop entirely: the first flow's
   report was never generated either, because report generation happens
   AFTER the loop over all flows completes. Now each flow is isolated, so
   flow 1 gets its real report and flow 2 gets an error-flagged one — two
   documents for two selected recordings either way.
"""
import asyncio, os, sys, tempfile, json
from pathlib import Path
from cryptography.fernet import Fernet

ok = True
def check(n, c, d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ") + n + ("" if c else f"  << {d}"))

# ── Layer 1: sections.build_reports() resilience ────────────────────────
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work1 = tempfile.mkdtemp(); os.chdir(work1)
sys.path.insert(0, BACKEND)
from engine.sections import SectionResult, build_reports
from engine.schema import FlowRunResult, StepResult, Act

fr_a = FlowRunResult(run_id="r1", sid="F4H", flow_id="flow_A", mode="dom", ok=True)
fr_a.steps = [StepResult(index=0, act=Act.START_TCODE, ok=True, detail="tcode SE16")]
fr_b = FlowRunResult(run_id="r1", sid="F4H", flow_id="flow_B", mode="dom", ok=True)
fr_b.steps = [StepResult(index=0, act=Act.START_TCODE, ok=True, detail="tcode SM37")]
sr = SectionResult("functional_sanity", "Functional Sanity", ok=True, flow_results=[fr_a, fr_b])

arts = build_reports([sr], output_dir="reports")
labels = sorted(a["label"] for a in arts)
check("1. Two distinct flow_ids -> two documents", labels == ["flow_A", "flow_B"], str(labels))
check("2. Both documents actually written to disk",
      all(a.get("docx") and Path(a["docx"]).exists() for a in arts), str(arts))

# now make flow_B's generation explode and confirm flow_A survives
import flow_report
_orig = flow_report.FlowReportGenerator.generate_word
def _boom(self):
    if self.flow_id == "flow_B":
        raise RuntimeError("simulated doc-gen crash")
    return _orig(self)
flow_report.FlowReportGenerator.generate_word = _boom
arts2 = build_reports([sr], output_dir="reports_boom")
flow_report.FlowReportGenerator.generate_word = _orig
by_label = {a["label"]: a for a in arts2}
check("3. flow_A's document survives flow_B's crash",
      "flow_A" in by_label and by_label["flow_A"].get("docx")
      and Path(by_label["flow_A"]["docx"]).exists(), str(arts2))
check("4. flow_B still gets AN ENTRY (not silently dropped), flagged with its error",
      "flow_B" in by_label and by_label["flow_B"].get("docx") is None
      and by_label["flow_B"].get("error"), str(arts2))

# ── Layer 2: the real main.py flow loop, with a corrupt second flow ─────
work2 = tempfile.mkdtemp(); os.chdir(work2)
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["SYSTEMS_PATH"] = str(Path(work2) / "systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work2) / "runs.db")
os.environ["FLOWS_DIR"] = str(Path(work2) / "flows")
Path(os.environ["FLOWS_DIR"]).mkdir(parents=True, exist_ok=True)

from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto
Registry(systems=[SystemEntry(sid="CBX", client="100", hostname="127.0.0.1",
    webgui_url="http://127.0.0.1:8099/webgui",
    sap_credential=SapCredential(username="M", password_enc=crypto.encrypt("p")))]
).save(os.environ["SYSTEMS_PATH"])

# flow 1: trivial, valid, always succeeds (just screenshots the logged-in page)
Path(os.environ["FLOWS_DIR"], "good_flow.json").write_text(json.dumps({
    "flow_id": "good_flow", "steps": [{"act": "capture", "mode": ["screenshot"]}],
}))
# flow 2: malformed JSON -- simulates a corrupt/hand-edited recording file
Path(os.environ["FLOWS_DIR"], "broken_flow.json").write_text("{not valid json,,,")

import importlib
import main as m
importlib.reload(m)  # picks up the env vars set just above

async def go():
    run_id = "multiflow1"
    m._event_queues[run_id] = asyncio.Queue()
    from main import RunRequest
    req = RunRequest(sids=["CBX"], flows=["good_flow", "broken_flow"])
    await m._execute_run_inner(run_id, req)

    reports = m.store.recent_reports(sid="CBX", limit=20)
    print("      reports:", json.dumps(reports, default=str)[:400])
    check("5. BOTH selected flows produced a report row (not just the first)",
          len(reports) == 2, f"got {len(reports)}: {[r['label'] for r in reports]}")
    labels2 = sorted(r["label"] for r in reports)
    check("6. Labels are exactly the two selected recordings",
          labels2 == ["broken_flow", "good_flow"], str(labels2))
    good = next(r for r in reports if r["label"] == "good_flow")
    check("7. The GOOD flow's document was generated normally",
          bool(good.get("docx_path")) and Path(good["docx_path"]).exists(), str(good))

    runs = m.store.recent(sid="CBX", limit=20)
    broken_run = next((r for r in runs if r["flow_id"] == "broken_flow"), None)
    check("8. The broken flow is recorded as a failed run, not silently skipped",
          broken_run is not None and broken_run["ok"] is False and broken_run.get("error"),
          str(broken_run))

asyncio.run(go())

print("D-040 MULTI-FLOW REPORTS: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
sys.exit(0 if ok else 1)
