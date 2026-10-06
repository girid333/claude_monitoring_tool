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
    exports: dict[str, Any] = field(default_factory=dict)   # D-053: values read during the run

    def as_dict(self) -> dict:
        return {
            "index": self.index, "inputs": self.inputs, "ok": self.ok,
            "error": self.error, "outcome": self.outcome,
            "screenshots": self.screenshots, "failed_steps": self.failed_steps,
            "duration_ms": self.duration_ms, "notes": self.notes,
            "exports": self.exports,
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

def suggest_bindings(steps: list[dict], include_toggles: bool = False) -> list[Binding]:
    """Propose one dataset column per editable recorded field.

    Column names are derived from the field's own label so the resulting
    spreadsheet reads like the transaction ("Vendor", "Material") rather
    than "step_7" — the person filling it in is a functional user who has
    never seen the flow JSON.

    `include_toggles` exists because the default is a JUDGEMENT, not a
    rule: checkboxes and radios are usually part of the navigation path
    (so varying them per row would change the scenario rather than the
    data), but for some transactions a flag genuinely IS the varying
    data — "delivery complete", "final invoice", a posting-block
    indicator. This is a suggestion the caller can override, not a
    restriction."""
    used: set[str] = set()
    out: list[Binding] = []
    for position, s in enumerate(steps):
        # describe_steps() does NOT emit an "index" key — it returns steps in
        # order — so the POSITION is the step index. Defaulting to 0 here
        # gave every binding step_index=0, so all overrides collapsed onto
        # step 0 (last one winning) and the intended fields were never
        # changed. It looked fine in unit tests only because the fixture had
        # index keys the real function doesn't produce.
        idx = int(s.get("index", position))
        if s.get("act") != "set":
            continue
        if isinstance(s.get("value"), bool) and not include_toggles:
            continue
        label = (s.get("target") or "").strip()
        column = _column_name(label, idx, used)
        used.add(column)
        out.append(Binding(step_index=idx, column=column,
                            label=label or f"step {idx}",
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


def apply_overrides(flow, overrides: dict[str, Any]) -> list[str]:
    """Apply {step_index: value} onto a loaded Flow's SET steps (D-043).

    Lives here rather than in main.py because the data-run executor needs
    it too, and `engine` importing from `main` would invert the dependency
    direction. main.py re-exports it as `_apply_overrides` so existing
    callers and tests are unaffected.

    Returns human-readable notes for anything skipped, so the run log
    shows WHY an override didn't take rather than it silently not applying.
    """
    from .schema import Act
    notes = []
    for key, value in (overrides or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            notes.append(f"override ignored: {key!r} is not a step index")
            continue
        if not (0 <= idx < len(flow.steps)):
            notes.append(f"override ignored: no step {idx}")
            continue
        step = flow.steps[idx]
        if step.act != Act.SET:
            notes.append(f"override ignored: step {idx} is {step.act.value}, not set")
            continue
        # Preserve the recorded value's TYPE where it matters: a checkbox
        # step's value is a bool, and the UI/spreadsheet send strings.
        if isinstance(step.value, bool) and isinstance(value, str):
            value = value.strip().lower() in ("true", "check", "checked",
                                               "select", "1", "yes", "x")
        step.value = value
    return notes


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

# Verbs SAP uses when something was successfully committed. Deliberately
# broad — every module phrases this differently, and a data run is
# meaningless to an auditor if the confirmation isn't captured.
# NOTE: "blocked" was originally in this list and was a genuine bug — it is
# a FAILURE word ("Vendor 200000 is blocked for purchasing"), and including
# it made a blocked-vendor error extract as a successful outcome, which in
# turn marked the row PASS in an auditor's document.
_SUCCESS_VERB = (r"(?:created|saved|posted|changed|generated|issued|released|"
                 r"scheduled|updated|added|completed|deleted|reversed|"
                 # Display/report transactions (SE16, SM37, ST22 …) confirm
                 # with these rather than a created-document phrase, and are
                 # just as much a valid outcome to evidence.
                 r"displayed|selected|found|retrieved|listed)")

# An SAP object key is NOT always numeric. Materials, customers with alpha
# prefixes, batch numbers, equipment, and anything from a custom Z
# transaction can be alphanumeric — a digits-only pattern silently found
# nothing for most transactions, which was the PO-shaped assumption.
_KEY = r"[A-Za-z0-9][A-Za-z0-9_\-/]{2,29}"

_OUTCOME_PATTERNS = [
    # "Standard PO created under the number 4500001234"
    re.compile(rf"\b{_SUCCESS_VERB}\b[^.\n]{{0,60}}?\b(?:number|no\.?|#)\s*({_KEY})", re.I),
    # "Material RAW-001 created" / "Customer 0000012345 created"
    re.compile(rf"\b({_KEY})\b[^.\n]{{0,40}}?\b{_SUCCESS_VERB}\b", re.I),
    # "Standard Order 12345 has been saved" / "Document 1900004567 was posted"
    re.compile(rf"\b{_SUCCESS_VERB}\b[^.\n]{{0,60}}?\b({_KEY})\b", re.I),
]


def extract_outcome(text: str, pattern: Optional[str] = None) -> str:
    """Summarise what the transaction confirmed, for the summary table.

    The auditor's document is far more useful with "PO 4500001234 created"
    per row than with a screenshot alone — but this is best-effort ON TOP
    of the screenshot, never a replacement, precisely because message
    wording varies by module, release and language.

    `pattern` lets a flow override the default entirely: a custom Z
    transaction, a non-English system, or an unusual message format can
    supply its own regex (first capturing group wins, else the whole
    match). That escape hatch matters more than making the default
    cleverer — no default can cover every transaction."""
    if not text:
        return ""
    flat = " ".join(text.split())

    if pattern:
        try:
            m = re.search(pattern, flat, re.I)
        except re.error:
            return ""      # a bad user regex must not break the run
        if not m:
            return ""
        return (m.group(1) if m.groups() else m.group(0)).strip()[:200]

    for rx in _OUTCOME_PATTERNS:
        m = rx.search(flat)
        if m:
            start = max(0, m.start() - 40)
            return flat[start:m.end() + 40].strip()

    # Nothing matched a key, but a success verb alone is still worth
    # recording — "Data saved" carries no number and is a valid outcome.
    for sentence in re.split(r"(?<=[.!])\s+", flat):
        if re.search(rf"\b{_SUCCESS_VERB}\b", sentence, re.I):
            return sentence.strip()[:160]
    return ""


_ERROR_WORDS = (r"(?:error|cannot|could not|not possible|is blocked|no authorization|"
                r"does not exist|not permitted|invalid|missing|failed|is locked|"
                r"please enter|must be|not allowed|incorrect|terminated)")


def extract_error(text: str) -> str:
    """The specific sentence that reads as a failure.

    An auditor's document needs the actual SAP message ("Vendor 200000 is
    blocked for purchasing"), not the whole screen dumped into a cell."""
    if not text:
        return ""
    flat = " ".join(text.split())
    for sentence in re.split(r"(?<=[.!])\s+|(?<=\.)\s+", flat):
        if re.search(_ERROR_WORDS, sentence, re.I):
            return sentence.strip()[:200]
    m = re.search(rf"[^.]{{0,90}}{_ERROR_WORDS}[^.]{{0,90}}", flat, re.I)
    return m.group(0).strip()[:200] if m else ""


def looks_like_error(text: str) -> bool:
    """Whether a status message reads as a failure.

    Kept separate from step-level success because SAP frequently returns
    HTTP/step success while the SCREEN reports a business error — an
    auditor's document that marked those green would be actively
    misleading."""
    if not text:
        return False
    # Shares _ERROR_WORDS with extract_error() deliberately — a second copy
    # of this list had already drifted out of step with the first.
    return bool(re.search(_ERROR_WORDS, " ".join(text.split()), re.I))


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
