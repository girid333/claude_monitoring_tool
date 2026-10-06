"""Check evaluation (Phase 4 verification layer, D-009).

All checks run against the most recent capture. Structured data first;
screenshots are evidence, not check inputs.
"""

from __future__ import annotations

import re
from typing import Any

from .schema import CheckOp, CheckResult, Severity, Step


def compare(actual: Any, op: CheckOp, expected: Any) -> bool:
    try:
        if op == CheckOp.EQ: return actual == expected
        if op == CheckOp.NE: return actual != expected
        if op == CheckOp.LT: return actual < expected
        if op == CheckOp.GT: return actual > expected
        if op == CheckOp.CONTAINS: return str(expected) in str(actual or "")
        if op == CheckOp.NOT_CONTAINS: return str(expected) not in str(actual or "")
        if op == CheckOp.REGEX: return re.search(str(expected), str(actual or "")) is not None
    except TypeError:
        return False
    return False


def _num(v: Any) -> Any:
    try:
        return int(v) if isinstance(v, str) and v.strip().lstrip("-").isdigit() else v
    except ValueError:
        return v


def evaluate(step: Step, last_capture: dict[str, Any]) -> CheckResult:
    severity = step.severity or Severity.MEDIUM
    op = step.op or CheckOp.EQ
    actual: Any = None

    if step.type == "row_count":
        actual = len(last_capture.get("table", []))
        ok = compare(actual, op, _num(step.value))

    elif step.type == "cell_value":
        # Passes iff NO row's <column> violates op/value?  No — semantics:
        # check holds when EVERY row satisfies op(value). Failing rows are
        # reported in `actual` for the report.
        rows = last_capture.get("table", [])
        col = step.column or ""
        failing = [r for r in rows if not compare(r.get(col), op, step.value)]
        ok = len(failing) == 0
        actual = {"rows": len(rows), "failing": len(failing),
                  "sample": failing[:5]}

    elif step.type == "statusbar_type":
        actual = (last_capture.get("statusbar") or {}).get("type", "")
        ok = compare(actual, op, step.value)

    elif step.type == "statusbar_text":
        actual = (last_capture.get("statusbar") or {}).get("text", "")
        ok = compare(actual, op, step.value)

    elif step.type == "field_value":
        actual = (last_capture.get("field") or {}).get("value")
        ok = compare(actual, op, step.value)

    elif step.type == "text":
        actual = last_capture.get("page_text", "")
        ok = compare(actual, op, step.value)

    else:
        ok = False
        actual = f"unknown check type {step.type!r}"

    return CheckResult(ok=ok, severity=severity, message=step.message or "",
                       actual=actual, expected=step.value)
