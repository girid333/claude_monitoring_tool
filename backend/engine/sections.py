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

    def tcode_list(self) -> list[str]:
        """D-031: the T-codes this sweep will visit, in order — so the UI can
        render the full checklist up-front (all pending) and light each one up
        as v1 reports progress, instead of showing an opaque spinner."""
        try:
            from .core_basis_wrap import load_v1_orchestrator
            mod = load_v1_orchestrator(self._resolve_v1_path())
            return [t["tcode"] for t in mod.MONITORED_TCODES]
        except Exception:  # noqa: BLE001
            return []

    def __init__(self, v1_path: str | Path | None = None,
                 registry_path: str | Path = "systems.json"):
        # D-028: v1's code is found via a resolution order, because the
        # deployment layouts genuinely differ:
        #   1. V1_PATH env var — explicit override (e.g. volume-mounted v1)
        #   2. ../../sap_monitoring relative to CWD — the dev layout
        #      (sap_monitoring is a sibling of claude_monitoring_tool;
        #      uvicorn runs from backend/, so two levels up) — preferred
        #      when present so a locally-updated v1 wins over the snapshot
        #   3. backend/v1_core — verbatim vendored copy shipped in the
        #      Docker image (the container has no sap_monitoring at all;
        #      without this, Core BASIS could never run containerized)
        self.v1_path = Path(v1_path) if v1_path else None
        self.registry_path = registry_path

    def _resolve_v1_path(self) -> Path:
        import os
        candidates = []
        if self.v1_path:
            candidates.append(self.v1_path)
        if os.environ.get("V1_PATH"):
            candidates.append(Path(os.environ["V1_PATH"]))
        candidates.append(Path("../../sap_monitoring"))
        candidates.append(Path(__file__).resolve().parent.parent / "v1_core")
        for c in candidates:
            if (Path(c) / "backend" / "orchestrator.py").is_file():
                return Path(c)
        raise FileNotFoundError(
            "v1 engine not found — looked for backend/orchestrator.py under: "
            + ", ".join(str(c) for c in candidates))

    async def run(self, engine: ReplayEngine, run_id: str, sid: str,
                   on_status=None) -> SectionResult:
        """D-031: `on_status` is an optional SYNC callable receiving v1's own
        progress strings ("Capturing screenshot for SM50...", warnings, etc.).

        It must be sync: v1's `sap_automator.take_screenshots()` invokes the
        callback directly WITHOUT awaiting it (orchestrator.update_status
        handles coroutines, but take_screenshots does not), so an async
        callback would produce an un-awaited coroutine and silently drop the
        event. main.py passes a sync shim that schedules the SSE emit.
        """
        result = SectionResult(self.section_id, self.title, ok=True)

        from .core_basis_wrap import ResilientRfcClient, build_v1_config, load_v1_orchestrator
        from .registry import Registry, RegistryCredentialProvider

        try:
            reg = Registry.load(self.registry_path)
            sys_entry = reg.get(sid)
            creds = RegistryCredentialProvider(reg).get(sid)  # D-025
        except Exception as exc:
            result.ok = False
            result.notes.append(f"Core BASIS skipped: {exc}")
            return result

        try:
            orchestrator_mod = load_v1_orchestrator(self._resolve_v1_path())
        except Exception as exc:
            result.ok = False
            result.notes.append(
                f"Core BASIS skipped: could not import v1 orchestrator ({exc})")
            return result

        config = build_v1_config(sys_entry, creds)
        ResilientRfcClient.reset_log()
        orch = orchestrator_mod.ReportOrchestrator(config, status_callback=on_status)

        try:
            v1_results = await orch.generate_report()
        except Exception as exc:
            result.ok = False
            result.notes.append(f"Core BASIS run failed: {exc}")
            return result

        if not ResilientRfcClient.any_available():
            result.notes.append(
                "RFC enrichment unavailable this run (D-013): WebGUI-only "
                "monitoring completed; RFC-only T-codes report 'RFC Error' "
                "in their anomaly list instead of blocking the run.")

        result.v1_results = v1_results
        result.ok = len(v1_results.get("anomalies", [])) == 0
        return result


# ── Report bridge (D-014) ────────────────────────────────────────────────

SEVERITY_ORDER = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
                  Severity.LOW, Severity.INFO]


def flow_results_to_report_rows(results: list[FlowRunResult]) -> list[dict]:
    """Map FlowRunResults into rows for the inherited report composer.

    Target shape mirrors what report_generator.py builds per T-code today
    (title, screenshots, anomaly list) PLUS severity — the field the v1
    template lacks.
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


def build_reports(section_results: list[SectionResult], output_dir: str = "reports",
                   cover_image_path: str | None = None) -> list[dict]:
    """D-035: one document PER KIND, instead of one combined file.

    Core BASIS keeps v1's inherited four-section template. Recorded flows get
    `flow_report.FlowReportGenerator` — named after the recording and
    containing only sections that have content. Previously both shared the
    BASIS template, so a flows-only run produced a file called "SAP
    Monitoring Report v2.0" whose first three sections were empty pages.

    Returns [{"kind","label","docx","pdf"}, ...].
    """
    out: list[dict] = []

    v1_results = next((sr.v1_results for sr in section_results
                        if sr.v1_results is not None), None)
    if v1_results:
        from report_composer import ReportGenerator
        gen = ReportGenerator(v1_results, output_dir=output_dir,
                               cover_image_path=cover_image_path,
                               extra_sections=[])
        out.append({"kind": "core_basis", "label": "Core BASIS",
                    "docx": gen.generate_word(), "pdf": gen.generate_pdf()})

    # One report per recorded flow, named by the flow (D-035).
    from flow_report import FlowReportGenerator
    rows_by_flow: dict[str, list[dict]] = {}
    for sr in section_results:
        for row in flow_results_to_report_rows(sr.flow_results):
            rows_by_flow.setdefault(row["flow_id"], []).append(row)
    for flow_id, rows in rows_by_flow.items():
        fgen = FlowReportGenerator(flow_id, rows, output_dir=output_dir)
        out.append({"kind": "flow", "label": flow_id,
                    "docx": fgen.generate_word(), "pdf": fgen.generate_pdf()})

    return out


def build_report(section_results: list[SectionResult], output_dir: str = "reports",
                  cover_image_path: str | None = None) -> dict[str, str]:
    """D-014 (done): assemble the actual report_composer.ReportGenerator call
    from a run's SectionResults, whatever mix of CoreBasisSection/FlowSection
    ran. Returns {"docx": path, "pdf": path}.

    At most one CoreBasisSection's v1_results feeds the Core BASIS section
    (v1's shape isn't designed for more than one system's data per report);
    every FlowSection's flow_results become one "extra section" each, titled
    by section title, so multiple functional-sanity groupings stay visually
    separated in the output.
    """
    # Absolute, not relative: report_composer.py lives at backend/, a sibling
    # of the engine/ package, not inside it (backend/ has no __init__.py).
    # Same convention main.py already uses for `from engine.registry import
    # ...` — backend/ is the sys.path root when the app runs (uvicorn from
    # backend/) or in tests that insert backend/ onto sys.path. Local import
    # here also keeps report deps (python-docx, reportlab) optional for
    # callers that never generate a report.
    from report_composer import ReportGenerator

    v1_results: dict = {}
    extra_sections: list[dict] = []
    for sr in section_results:
        if sr.v1_results is not None:
            v1_results = sr.v1_results
        if sr.flow_results:
            extra_sections.append({
                "title": sr.title,
                "rows": flow_results_to_report_rows(sr.flow_results),
            })

    gen = ReportGenerator(v1_results, output_dir=output_dir,
                           cover_image_path=cover_image_path,
                           extra_sections=extra_sections)
    return {"docx": gen.generate_word(), "pdf": gen.generate_pdf()}
