"""D-035: separate, named flow reports with no empty pages."""
import os, sys, tempfile, json
from pathlib import Path
BACKEND="/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work=tempfile.mkdtemp(); os.chdir(work); sys.path.insert(0, BACKEND)
ok=True
def check(n,c,d=""):
    global ok; ok = ok and bool(c)
    print(("PASS " if c else "FAIL ")+n+("" if c else f"  << {d}"))

from engine.sections import SectionResult, build_reports
from engine.schema import FlowRunResult, StepResult, Act, CheckResult, Severity

# a screenshot to embed
from PIL import Image as _PI
_PI.new("RGB", (900, 500), (240, 244, 250)).save("shot.png")
Path("broken.png").write_bytes(b"not really a png")

fr = FlowRunResult(run_id="r1", sid="F4H", flow_id="se16_check", mode="dom", ok=True)
fr.steps=[StepResult(index=0, act=Act.START_TCODE, ok=True, detail="tcode SE16"),
          StepResult(index=1, act=Act.CAPTURE, ok=True, screenshot_path="shot.png"),
          StepResult(index=2, act=Act.CAPTURE, ok=True, screenshot_path="broken.png")]
fr.checks=[CheckResult(ok=False, severity=Severity.MEDIUM, message="500 rows returned",
                        actual=500, expected=0)]
flows_only=[SectionResult("functional_sanity","Functional Sanity",ok=False,flow_results=[fr])]

arts = build_reports(flows_only, output_dir="reports")
check("1. Flows-only run produces exactly ONE report (no BASIS doc)", len(arts)==1, str(arts))
a=arts[0]
check("2. Report kind is 'flow'", a["kind"]=="flow", a["kind"])
check("3. Label is the recording name", a["label"]=="se16_check", a["label"])
check("4. FILENAME carries the recording name, not 'SAP Monitoring Report'",
      "se16_check" in Path(a["docx"]).name and "Monitoring_Report" not in Path(a["docx"]).name,
      Path(a["docx"]).name)
for k in ("docx","pdf"):
    check(f"5. {k} written and non-trivial", Path(a[k]).exists() and Path(a[k]).stat().st_size>4000,
          f"{a[k]} {Path(a[k]).stat().st_size if Path(a[k]).exists() else 0}b")

# no Core BASIS section headings => no empty pages from that template
from docx import Document
d=Document(a["docx"])
texts=[p.text for p in d.paragraphs]
joined=" | ".join(texts)
check("6. Title names the flow", any("se16_check" in t for t in texts), joined[:120])
for bad in ["Infrastructure & Server Health","Detailed Status","Executive Summary",
            "SAP Monitoring Report v2.0"]:
    check(f"7. No Core BASIS section '{bad}'", bad not in joined, joined[:160])
check("8. Findings rendered", "500 rows returned" in joined, joined[:200])
check("9. Evidence section present", "Captured evidence" in joined, joined[:200])
# empty-paragraph ratio: template must not pad with blanks
blanks=sum(1 for t in texts if not t.strip())
check("10. Not padded with empty paragraphs", blanks <= 3, f"{blanks} blank paras of {len(texts)}")

# combined run -> two separate docs
v1={"tcodes":[{"tcode":"SM50","name":"WP","screenshot":None,"anomalies":[],"analysis_summary":""}],
    "anomalies":[],"external_checks":[],"ssh_filesystem_data":[],"system_info":{}}
both=[SectionResult("core_basis","Core BASIS",ok=True,v1_results=v1),
      SectionResult("functional_sanity","Functional Sanity",ok=False,flow_results=[fr])]
arts2=build_reports(both, output_dir="reports2")
kinds=sorted(x["kind"] for x in arts2)
check("11. Mixed run yields TWO separate documents", kinds==["core_basis","flow"], str(kinds))
names=[Path(x["docx"]).name for x in arts2]
check("12. The two documents have different names", len(set(names))==2, str(names))
check("13. A corrupt screenshot does not kill the report",
      Path(a["pdf"]).exists() and Path(a["pdf"]).stat().st_size>4000, "pdf missing")
print("D-035: " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
