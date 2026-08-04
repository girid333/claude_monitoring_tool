"""D-048: execute an API flow — the replay engine.

Runs each step in order against the live system, carrying values forward
between steps, then evaluates the flow's checks.

Failure policy, following the D-040 lesson: a step failing never silently
truncates the run into a green result. The step is recorded as failed with
SAP's own error message, the flow is marked failed, and remaining steps
are reported as skipped so it's obvious what did and didn't happen.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ..rfc.soap_client import RfcSoapClient, RfcSoapError
from .odata import read_entity_set, service_path
from .schema import PLACEHOLDER, ApiFlow, FlowCheck, FlowStep


@dataclass
class StepResult:
    id: str
    kind: str
    ok: bool
    description: str = ""
    detail: str = ""
    error: str = ""
    row_count: int = 0
    rows: list[dict] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "ok": self.ok,
            "description": self.description, "detail": self.detail,
            "error": self.error, "row_count": self.row_count,
            "rows": self.rows[:100], "extracted": self.extracted,
            "skipped": self.skipped, "duration_ms": self.duration_ms,
        }


@dataclass
class CheckResult:
    description: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict:
        return {"description": self.description, "ok": self.ok, "detail": self.detail}


@dataclass
class FlowResult:
    flow_id: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "flow_id": self.flow_id, "ok": self.ok, "error": self.error,
            "params": self.params, "duration_ms": self.duration_ms,
            "steps": [s.as_dict() for s in self.steps],
            "checks": [c.as_dict() for c in self.checks],
            "failed_steps": [s.id for s in self.steps if not s.ok and not s.skipped],
            "failed_checks": [c.description for c in self.checks if not c.ok],
        }


# ── value plumbing ───────────────────────────────────────────────────────

def extract_path(result: dict, path: str) -> Any:
    """Read a value out of a step result with a small, predictable syntax:
    `count`, `rows[0].AlertGuid`, `raw.SomeField`.

    Deliberately not full JSONPath — a tiny documented subset is easier to
    explain in a UI and has no surprising behaviour."""
    current: Any = result
    for token in [t for t in re.split(r"\.", path) if t]:
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(\[(\d+)\])?", token)
        if not m:
            return None
        key, _, idx = m.groups()
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if idx is not None:
            if not isinstance(current, list) or int(idx) >= len(current):
                return None
            current = current[int(idx)]
    return current


def substitute(value: Any, context: dict[str, dict[str, Any]],
                missing: Optional[list[str]] = None) -> Any:
    """Replace {{step.name}} / {{param.name}} anywhere in a value.

    A placeholder that is the ENTIRE string yields the raw typed value
    (so an int stays an int); embedded placeholders interpolate as text.

    `missing` collects any reference that resolves to nothing. That has to
    be tracked HERE rather than inferred from the output, because a
    dangling reference resolves to None and then simply disappears — which
    would mean sending a silent null to SAP and getting a baffling error
    back instead of "step 1 never produced that value"."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value.strip())
        if whole:
            scope, name = whole.group(1), whole.group(2)
            if scope not in context or context[scope].get(name) is None:
                if missing is not None:
                    missing.append(f"{{{{{scope}.{name}}}}}")
                return None
            return context[scope][name]

        def repl(m: re.Match) -> str:
            scope, name = m.group(1), m.group(2)
            got = context.get(scope, {}).get(name)
            if got is None:
                if missing is not None:
                    missing.append(f"{{{{{scope}.{name}}}}}")
                return ""
            return str(got)
        return PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: substitute(v, context, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, context, missing) for v in value]
    return value


# ── step execution ───────────────────────────────────────────────────────

def _run_step(client: RfcSoapClient, step: FlowStep,
               context: dict[str, dict[str, Any]]) -> StepResult:
    started = time.time()
    res = StepResult(id=step.id, kind=step.kind, ok=False,
                      description=step.description)

    missing: list[str] = []
    resolved_params = substitute(step.params, context, missing)
    substitute(step.filter, context, missing)
    substitute(step.entity_set, context, missing)
    if missing:
        res.error = (f"Unresolved reference(s): {', '.join(sorted(set(missing)))}. "
                     "An earlier step did not produce that value.")
        res.duration_ms = int((time.time() - started) * 1000)
        return res

    try:
        if step.kind == "odata_read":
            out = read_entity_set(
                client, str(substitute(step.service, context)),
                str(substitute(step.entity_set, context)),
                top=step.top, filter_expr=str(substitute(step.filter, context) or ""),
                select=step.select)
            if not out.get("ok"):
                res.error = out.get("error", "read failed")
                res.duration_ms = int((time.time() - started) * 1000)
                return res
            res.rows = out.get("rows", [])
            res.row_count = out.get("count", len(res.rows))
            res.detail = f"{res.row_count} row(s) from {step.entity_set}"
            payload = {"count": res.row_count, "rows": res.rows}

        elif step.kind == "odata_function":
            path = service_path(str(substitute(step.service, context))).rstrip("/") \
                + "/" + str(substitute(step.function_import, context)).lstrip("/")
            status, text = client.http_get(path, params={"$format": "json",
                                                          **(resolved_params or {})})
            if status >= 400:
                from .odata import classify_failure
                _, explanation = classify_failure(status, text)
                res.error = explanation
                res.duration_ms = int((time.time() - started) * 1000)
                return res
            import json as _json
            try:
                body = _json.loads(text)
            except _json.JSONDecodeError:
                body = {"raw": text[:2000]}
            d = body.get("d", body)
            rows = d.get("results") if isinstance(d, dict) else None
            if isinstance(rows, list):
                res.rows = [r for r in rows if isinstance(r, dict)]
                res.row_count = len(res.rows)
            elif isinstance(d, dict):
                res.rows = [d]
                res.row_count = 1
            res.detail = f"{step.function_import} returned {res.row_count} result(s)"
            payload = {"count": res.row_count, "rows": res.rows, "raw": d}

        elif step.kind == "rfc_call":
            call_params = dict(resolved_params or {})
            for t in step.tables:
                call_params.setdefault(t, [])
            reply = client.call(str(substitute(step.function, context)), **call_params)
            tables = {k: v for k, v in reply.items() if isinstance(v, list)}
            biggest = max(tables.values(), key=len) if tables else []
            res.rows = [r for r in biggest if isinstance(r, dict)]
            res.row_count = len(res.rows)
            res.detail = f"{step.function} returned " + (
                ", ".join(f"{k}={len(v)}" for k, v in tables.items()) or "no tables")
            payload = {"count": res.row_count, "rows": res.rows, "raw": reply}

        else:
            res.error = f"unknown step kind {step.kind!r}"
            res.duration_ms = int((time.time() - started) * 1000)
            return res

    except RfcSoapError as exc:
        res.error = exc.message
        res.duration_ms = int((time.time() - started) * 1000)
        return res
    except Exception as exc:  # noqa: BLE001 - one bad step never kills the run
        res.error = f"unexpected error: {exc!r}"
        res.duration_ms = int((time.time() - started) * 1000)
        return res

    for name, path in (step.extract or {}).items():
        res.extracted[name] = extract_path(payload, path)
    res.ok = True
    res.duration_ms = int((time.time() - started) * 1000)
    return res


# ── checks ───────────────────────────────────────────────────────────────

def _evaluate(check: FlowCheck, by_step: dict[str, StepResult]) -> CheckResult:
    label = check.description or f"{check.step}: {check.op} {check.value!r}"
    step = by_step.get(check.step)
    if step is None:
        return CheckResult(label, False, f"step {check.step!r} did not run")

    if check.op == "step_ok":
        return CheckResult(label, step.ok, step.error or "ok")
    if check.op == "row_count_min":
        return CheckResult(label, step.row_count >= int(check.value or 0),
                            f"got {step.row_count}, wanted >= {check.value}")
    if check.op == "row_count_max":
        return CheckResult(label, step.row_count <= int(check.value or 0),
                            f"got {step.row_count}, wanted <= {check.value}")
    if check.op == "not_empty":
        got = extract_path({"count": step.row_count, "rows": step.rows},
                            check.path or "rows[0]")
        return CheckResult(label, bool(got), f"value at {check.path!r} = {got!r}")
    if check.op in ("equals", "contains"):
        got = extract_path({"count": step.row_count, "rows": step.rows,
                             **step.extracted}, check.path or "count")
        if check.op == "equals":
            return CheckResult(label, str(got) == str(check.value),
                                f"got {got!r}, wanted {check.value!r}")
        return CheckResult(label, str(check.value) in str(got),
                            f"{check.value!r} in {str(got)[:120]!r}")
    return CheckResult(label, False, f"unknown check op {check.op!r}")


# ── the run ──────────────────────────────────────────────────────────────

def run_flow(client: RfcSoapClient, flow: ApiFlow,
             params: Optional[dict[str, Any]] = None) -> FlowResult:
    started = time.time()
    problems = flow.validate()
    if problems:
        return FlowResult(flow_id=flow.flow_id, ok=False,
                           error="Flow is not valid: " + "; ".join(problems))

    merged = {**(flow.params or {}), **(params or {})}
    context: dict[str, dict[str, Any]] = {"param": merged}
    result = FlowResult(flow_id=flow.flow_id, ok=True, params=merged)

    aborted = False
    for step in flow.steps:
        if aborted:
            skipped = StepResult(id=step.id, kind=step.kind, ok=False, skipped=True,
                                  description=step.description,
                                  detail="skipped — an earlier step failed")
            result.steps.append(skipped)
            continue
        sr = _run_step(client, step, context)
        result.steps.append(sr)
        context[step.id] = {**sr.extracted, "count": sr.row_count}
        if not sr.ok and not step.optional:
            result.ok = False
            aborted = True

    by_step = {s.id: s for s in result.steps}
    for check in flow.checks:
        cr = _evaluate(check, by_step)
        result.checks.append(cr)
        if not cr.ok:
            result.ok = False

    result.duration_ms = int((time.time() - started) * 1000)
    return result
