"""Manual smoke test for the D-014 report bridge — run directly:
`python _dryrun_report.py` from backend/. Builds a report from mock Core
BASIS + Functional Sanity data (no live system needed) and asserts real
docx/pdf files come out with plausible sizes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engine.schema import CheckResult, FlowRunResult, Severity, StepResult, Act  # noqa: E402
from engine.sections import SectionResult, build_report  # noqa: E402

mock_v1_results = {
    "anomalies": [
        "SM37: Failed background jobs found.",
        "DB02: Missing indexes detected.",
        "SM50: RFC unavailable this run (D-013: optional enrichment, WebGUI monitoring continues)",
    ],
    "external_checks": [
        {"type": "URL Check", "url": "https://example.com/health", "status": "HTTP 200", "details": "Reachable"},
    ],
    "ssh_filesystem_data": [],
    "tcodes": [
        {"tcode": "SM37", "name": "Background Jobs", "screenshot": "",
         "data": [], "anomalies": ["SM37: Failed background jobs found."],
         "analysis_summary": "Background job analysis completed. 1 canceled job found."},
        {"tcode": "DB02", "name": "Database Performance", "screenshot": "",
         "data": [], "anomalies": ["DB02: Missing indexes detected."],
         "analysis_summary": "Database diagnostic analysis completed. 1 database alert identified."},
        {"tcode": "SM51", "name": "Active Servers", "screenshot": "",
         "data": [], "anomalies": [], "analysis_summary": "All instances reporting Active."},
    ],
}
core_basis_sr = SectionResult(section_id="core_basis", title="Core BASIS", ok=False,
                               v1_results=mock_v1_results,
                               notes=["RFC enrichment unavailable this run (D-013)."])

flow_result_fail = FlowRunResult(
    run_id="dryrun2", sid="Q4H", flow_id="sm37_cancelled_jobs", mode="dom",
    steps=[StepResult(index=0, act=Act.CAPTURE, ok=True, resolver="dom", confidence=0.62)],
    checks=[CheckResult(ok=False, severity=Severity.HIGH,
                         message="Cancelled jobs found in last 24h", actual=3, expected=0)],
    ok=False,
)
flow_result_ok = FlowRunResult(
    run_id="dryrun2", sid="Q4H", flow_id="st22_dumps", mode="scripting",
    steps=[], checks=[CheckResult(ok=True, severity=Severity.INFO, message="no dumps")],
    ok=True,
)
functional_sr = SectionResult(section_id="functional_sanity", title="Functional Sanity",
                               ok=False, flow_results=[flow_result_fail, flow_result_ok])

paths = build_report([core_basis_sr, functional_sr], output_dir="reports/dryrun2")
for kind, path in paths.items():
    size = Path(path).stat().st_size
    assert size > 2000, f"{kind} suspiciously small: {size} bytes"
    print(f"[ok] {kind}: {path} ({size} bytes)")
print("ALL REPORT DRY-RUN CHECKS PASSED")
