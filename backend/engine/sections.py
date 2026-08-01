"""Sections (Phase 5, D-017): pluggable monitoring sections.

Two kinds:
  FlowSection      — runs recorded/hand-written flows via the ReplayEngine
  CoreBasisSection — wraps v1's orchestrator (../sap_monitoring) unchanged

Both return SectionResult; the report bridge maps everything into the
inherited report composer with the severity model (D-014).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .datasets import load as load_dataset
from .engine import ReplayEngine
from .schema import Flow, FlowRunResult, Severity


@dataclass
class SectionResult:
    section_id: str
    title: str
    ok: bool
    flow_results: list[FlowRunResult] = field(default_factory=list)
    v1_results: dict[str, Any] | None = None    # raw v1 results dict, untouched
    notes: list[str] = field(default_factory=list)


class Section(ABC):
    section_id: str
    title: str

    @abstractmethod
    async def run(self, engine: ReplayEngine, run_id: str, sid: str) -> SectionResult: ...


class FlowSection(Section):
    """A named group of flows + datasets (e.g. 'Functional sanity')."""

    def __init__(self, section_id: str, title: str,
                 flow_paths: list[str | Path],
                 dataset_paths: dict[str, str | Path] | None = None):
        self.section_id = section_id
        self.title = title
        self.flow_paths = [Path(p) for p in flow_paths]
        self.dataset_paths = dataset_paths or {}

    async def run(self, engine: ReplayEngine, run_id: str, sid: str) -> SectionResult:
        import json
        result = SectionResult(self.section_id, self.title, ok=True)
        for fp in self.flow_paths:
            flow = Flow(**json.loads(fp.read_text()))
            iterations = [{}]
            if flow.flow_id in self.dataset_paths:
                iterations = load_dataset(self.dataset_paths[flow.flow_id])
            for params in iterations:
                fr = await engine.run(flow, params)
                result.flow_results.append(fr)
                result.ok = result.ok and fr.ok
        return result


class CoreBasisSection(Section):
    """D-017: v1's 23-T-code BASIS sweep as a section provider.

    CONTRACT (for Claude Code, working with ../sap_monitoring in context):
      1. Import v1's orchestrator run entry point WITHOUT modifying
         sap_automator.py. Make ../sap_monitoring/backend importable
         (sys.path or editable install) — do not copy files.
      2. Call the existing run for one system; collect its results dict
         exactly as report_generator.py consumes it today.
      3. Return it in SectionResult.v1_results, ok = (no anomalies found).
      4. v1's own login is reused for now (its proven basic-auth path);
         browser session sharing with the v2 engine is a later optimization.
      5. RFC: per D-013, if v1's RFC login fails, log a note and continue —
         patch AROUND the abort (wrap, don't edit the v1 loop if avoidable).
    """

    section_id = "core_basis"
    title = "Core BASIS"

    def __init__(self, v1_path: str | Path = "../sap_monitoring"):
        self.v1_path = Path(v1_path)

    async def run(self, engine: ReplayEngine, run_id: str, sid: str) -> SectionResult:
        raise NotImplementedError(
            "Wrap v1 orchestrator per the contract in this class's docstring "
            "(needs ../sap_monitoring in context — Claude Code task).")


# ── Report bridge (D-014) ────────────────────────────────────────────────

SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
                  Severity.LOW, Severity.INFO]


def flow_results_to_report_rows(results: list[FlowRunResult]) -> list[dict]:
    """Map FlowRunResults into rows for the inherited report composer.

    Target shape mirrors what report_generator.py builds per T-code today
    (title, screenshots, anomaly list) PLUS severity — the field the v1
    template lacks. Claude Code: extend report_generator.py to render
    `severity` as chips/colors per D-016, keeping A3-landscape layout.
    """
    rows = []
    for fr in results:
        anomalies = [
            {"severity": c.severity.value, "message": c.message,
             "actual": c.actual, "expected": c.expected}
            for c in fr.checks if not c.ok
        ]
        worst = next((s.value for s in SEVERITY_ORDER
                      if any(a["severity"] == s.value for a in anomalies)), None)
        rows.append({
            "flow_id": fr.flow_id, "sid": fr.sid, "mode": fr.mode,
            "ok": fr.ok, "worst_severity": worst,
            "anomalies": anomalies,
            "screenshots": [s.screenshot_path for s in fr.steps
                            if s.screenshot_path],
            "low_confidence_steps": [s.index for s in fr.steps
                                     if s.confidence is not None
                                     and s.confidence < 0.75],
            "error": fr.error,
        })
    return rows
