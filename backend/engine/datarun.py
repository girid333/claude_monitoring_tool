"""D-049: data-driven replay — record one transaction, run it for N data rows,
document every execution.

The use case, in Giri's words: record a sample purchase order creation,
supply a template of vendor details, replay once per row, and produce a
document evidencing all executions for auditors and testers.

Why this reuses rather than replaces:
  - the recorded flow already exists (D-033/D-034 recorder);
  - D-043's replay-time overrides are ALREADY per-run field substitution —
    a dataset is that same mechanism driven by rows, so binding is just
    {step_index: column_name} resolved per row;
  - datasets.py already parses Excel/JSON;
  - FlowReportGenerator already embeds screenshots in Word/PDF.

What's genuinely new here is the LOOP and its failure discipline. Screen
replay of a business transaction can fail mid-way and leave a lock, a
draft document, or a popup that would derail every subsequent row. So
each iteration is isolated: failures are recorded, the session is
recovered before the next row, and the run continues. A failed row 5 must
never cost rows 6-20 — the same principle as D-040's per-flow isolation,
applied per data row.

Screenshots are per-iteration and kept separate, because the auditor's
document needs to show which evidence belongs to which input row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class Binding:
    """One recorded field wired to a dataset column."""
    step_index: int
    column: str
    label: str = ""          # human label from the step viewer, for the UI/report
    recorded_value: Any = None   # the sample value, used as the template's example row

    def as_dict(self) -> dict:
        return {"step_index": self.step_index, "column": self.column,
                "label": self.label, "recorded_value": self.recorded_value}


@dataclass
class IterationResult:
    index: int                    # 1-based, matches the spreadsheet row for humans
    inputs: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    error: str = ""
    outcome: str = ""             # e.g. the SAP success message, if captured
    screenshots: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "index": self.index, "inputs": self.inputs, "ok": self.ok,
            "error": self.error, "outcome": self.outcome,
            "screenshots": self.screenshots, "failed_steps": self.failed_steps,
            "duration_ms": self.duration_ms, "notes": self.notes,
        }


@dataclass
class DataRunResult:
    flow_id: str
    sid: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    iterations: list[IterationResult] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "flow_id": self.flow_id, "sid": self.sid, "total": self.total,
            "passed": self.passed, "failed": self.failed, "error": self.error,
            "iterations": [i.as_dict() for i in self.iterations],
        }


# ── binding helpers ──────────────────────────────────────────────────────

def suggest_bindings(steps: list[dict]) -> list[Binding]:
    """Propose one dataset column per editable recorded field.

    Column names are derived from the field's own label so the resulting
    spreadsheet reads like the transaction ("Vendor", "Material") rather
    than "step_7" — the person filling it in is a functional user who has
    never seen the flow JSON."""
    used: set[str] = set()
    out: list[Binding] = []
    for s in steps:
        if s.get("act") != "set":
            continue
        if isinstance(s.get("value"), bool):
            continue   # checkboxes/radios are usually part of the path, not data
        label = (s.get("target") or "").strip()
        column = _column_name(label, s.get("index", 0), used)
        used.add(column)
        out.append(Binding(step_index=int(s.get("index", 0)), column=column,
                            label=label or f"step {s.get('index')}",
                            recorded_value=s.get("value")))
    return out


def _column_name(label: str, index: int, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", label or "").strip("_")
    base = re.sub(r"_+", "_", base) or f"Field_{index}"
    if base[0].isdigit():
        base = f"F_{base}"
    name = base[:40]
    n = 2
    while name in used:
        name = f"{base[:36]}_{n}"
        n += 1
    return name


def overrides_for_row(bindings: list[Binding], row: dict[str, Any]) -> tuple[dict, list[str]]:
    """Turn one spreadsheet row into D-043 overrides.

    Missing or blank cells are reported rather than silently sending an
    empty value into a business transaction — a blank vendor would either
    fail confusingly or, worse, create something wrong."""
    overrides: dict[str, Any] = {}
    notes: list[str] = []
    for b in bindings:
        if b.column not in row:
            notes.append(f"column {b.column!r} missing from this row — "
                          f"the recorded value was kept")
            continue
        value = row[b.column]
        if value is None or (isinstance(value, str) and not value.strip()):
            notes.append(f"column {b.column!r} is empty — the recorded value was kept")
            continue
        overrides[str(b.step_index)] = value
    return overrides, notes


# ── outcome capture ──────────────────────────────────────────────────────

# SAP's confirmation messages, in the forms that carry a document number.
_OUTCOME_PATTERNS = [
    re.compile(r"\b(?:created|saved|posted|changed)\b[^.\n]*?\b(\d{6,12})\b", re.I),
    re.compile(r"\b(\d{6,12})\b[^.\n]*?\b(?:created|saved|posted)\b", re.I),
]


def extract_outcome(text: str) -> str:
    """Pull the created document number out of a status message.

    The auditor's document is far more useful with 'PO 4500001234 created'
    per row than with a screenshot alone, so this is best-effort on top of
    the screenshot rather than a replacement for it."""
    if not text:
        return ""
    flat = " ".join(text.split())
    for pattern in _OUTCOME_PATTERNS:
        m = pattern.search(flat)
        if m:
            start = max(0, m.start() - 40)
            return flat[start:m.end() + 40].strip()
    for line in flat.split(". "):
        if re.search(r"\b(created|saved|posted)\b", line, re.I):
            return line.strip()[:160]
    return ""


# ── template generation ──────────────────────────────────────────────────

def write_template(bindings: list[Binding], path: str | Path,
                    example_rows: int = 3) -> str:
    """Generate the Excel template a functional user fills in.

    Pre-filling row 1 with the values actually recorded matters: it shows
    the expected format for each field (date style, leading zeros, units)
    without anyone having to explain it."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    headers = [b.column for b in bindings]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="4472C4")
    ws.append([("" if b.recorded_value is None else b.recorded_value) for b in bindings])
    for _ in range(max(0, example_rows - 1)):
        ws.append([""] * len(headers))
    for i, b in enumerate(bindings, start=1):
        width = max(len(b.column), len(str(b.recorded_value or "")), 12) + 4
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(width, 40)

    notes = wb.create_sheet("Notes")
    notes.append(["Field", "Recorded sample value", "Recorded step"])
    for cell in notes[1]:
        cell.font = Font(bold=True)
    for b in bindings:
        notes.append([b.label or b.column,
                       "" if b.recorded_value is None else str(b.recorded_value),
                       b.step_index])
    notes.column_dimensions["A"].width = 34
    notes.column_dimensions["B"].width = 30

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return str(path)


def read_dataset(path: str | Path) -> list[dict[str, Any]]:
    """Rows from the filled-in template (Excel or JSON), blanks dropped."""
    from . import datasets
    rows = datasets.load(path)
    return [r for r in rows if any(
        v is not None and str(v).strip() != "" for v in r.values())]
