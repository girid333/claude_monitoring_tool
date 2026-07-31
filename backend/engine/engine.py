"""ReplayEngine — executes a Flow against one system (D-008, D-009, D-010).

Stateless by design: everything is keyed by run_id + sid, results are
returned, nothing global. Parallelism later = orchestration loop change.

Resolver selection comes from the capability probe; a per-step fallback
from scripting -> dom is deliberately NOT automatic (a scripting failure on
a scripting-enabled system is a signal, not something to paper over).
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from .resolvers.base import CONFIDENCE_FLAG_THRESHOLD, Resolver
from .resolvers.dom import DomResolver
from .resolvers.scripting import ScriptingResolver
from .schema import (Act, CheckOp, CheckResult, Flow, FlowRunResult, Severity,
                     Step, StepResult)
from .sync import wait_ready

_PARAM_RE = re.compile(r"\{\{(\w+)([+-]\d+)?\}\}")


def render_params(value: Any, params: dict[str, Any]) -> Any:
    """Resolve {{name}} and date expressions {{today}}, {{today-1}} (D-008)."""
    if not isinstance(value, str):
        return value

    def repl(m: re.Match) -> str:
        name, offset = m.group(1), m.group(2)
        if name == "today":
            d = dt.date.today() + dt.timedelta(days=int(offset or 0))
            return d.strftime("%d.%m.%Y")  # TODO: honor user date format from probe
        if name in params:
            return str(params[name])
        raise KeyError(f"unresolved parameter {{{{{name}}}}}")

    return _PARAM_RE.sub(repl, value)


class ReplayEngine:
    def __init__(self, page: Page, mode: str, run_id: str, sid: str,
                 artifact_dir: Path):
        self.page = page
        self.mode = mode
        self.run_id = run_id
        self.sid = sid
        self.artifact_dir = artifact_dir
        self.resolver: Resolver = (
            ScriptingResolver(page) if mode == "scripting" else DomResolver(page)
        )

    async def run(self, flow: Flow, dataset: dict[str, Any] | None = None) -> FlowRunResult:
        params = {**flow.params, **(dataset or {})}
        result = FlowRunResult(run_id=self.run_id, sid=self.sid,
                               flow_id=flow.flow_id, mode=self.mode)  # type: ignore[arg-type]
        last_capture: dict[str, Any] = {}

        for i, step in enumerate(flow.steps):
            try:
                sr = await self._exec_step(i, step, params, last_capture, result)
                result.steps.append(sr)
                if not sr.ok:
                    result.error = f"step {i} ({step.act}) failed: {sr.detail}"
                    break
            except Exception as exc:  # noqa: BLE001 — every step failure is data
                result.steps.append(StepResult(index=i, act=step.act, ok=False,
                                               detail=repr(exc)))
                result.error = f"step {i} ({step.act}) raised: {exc}"
                break

        result.ok = result.error is None and all(c.ok for c in result.checks)
        return result

    async def _exec_step(self, i: int, step: Step, params: dict,
                         last_capture: dict, result: FlowRunResult) -> StepResult:
        value = render_params(step.value, params)

        if step.act == Act.START_TCODE:
            # OK-code field first (v1-proven fast path), URL param fallback.
            res = await self.resolver.resolve(_okcode_target())
            await self.resolver.set_value(res, f"/n{value}")
            await self.resolver.send_vkey(0)
            await wait_ready(self.page, step.timeout_s, self.mode == "scripting")
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=self.resolver.name, detail=f"tcode {value}")

        if step.act == Act.WAIT_READY:
            await wait_ready(self.page, step.timeout_s, self.mode == "scripting")
            return StepResult(index=i, act=step.act, ok=True)

        if step.act in (Act.SET, Act.PRESS, Act.SELECT):
            assert step.target is not None
            res = await self.resolver.resolve(step.target)
            flagged = res.confidence < CONFIDENCE_FLAG_THRESHOLD
            if step.act == Act.SET:
                await self.resolver.set_value(res, value)
            else:
                await self.resolver.press(res)
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=res.resolver, confidence=res.confidence,
                              detail=res.via + (" [LOW CONFIDENCE]" if flagged else ""))

        if step.act == Act.VKEY:
            wnd = step.target.sid if step.target else "wnd[0]"
            await self.resolver.send_vkey(int(value), wnd)
            return StepResult(index=i, act=step.act, ok=True)

        if step.act == Act.CAPTURE:
            return await self._capture(i, step, last_capture)

        if step.act == Act.CHECK:
            return self._check(i, step, last_capture, result)

        return StepResult(index=i, act=step.act, ok=False,
                          detail=f"unknown act {step.act}")

    async def _capture(self, i: int, step: Step, last_capture: dict) -> StepResult:
        modes = step.mode or ["screenshot"]
        sr = StepResult(index=i, act=step.act, ok=True, resolver=self.resolver.name)
        if "screenshot" in modes:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / f"{self.run_id}_{self.sid}_step{i}.png"
            await self.page.screenshot(path=str(path), full_page=True)
            sr.screenshot_path = str(path)
        if "table" in modes and step.target is not None:
            res = await self.resolver.resolve(step.target)
            rows = await self.resolver.read_table(res)
            last_capture["table"] = rows
            sr.extracted = {"row_count": len(rows)}
        if "statusbar" in modes:
            last_capture["statusbar"] = await self.resolver.read_statusbar()
            sr.extracted = {**(sr.extracted or {}), "statusbar": last_capture["statusbar"]}
        return sr

    def _check(self, i: int, step: Step, last_capture: dict,
               result: FlowRunResult) -> StepResult:
        severity = step.severity or Severity.MEDIUM
        actual: Any = None
        if step.type == "row_count":
            actual = len(last_capture.get("table", []))
        elif step.type == "statusbar_type":
            actual = last_capture.get("statusbar", {}).get("type", "")
        # cell_value / field_value / text: Phase 4 (verification layer)

        ok = _compare(actual, step.op or CheckOp.EQ, step.value)
        result.checks.append(CheckResult(ok=ok, severity=severity,
                                         message=step.message or "",
                                         actual=actual, expected=step.value))
        return StepResult(index=i, act=step.act, ok=True,
                          detail=f"check {'passed' if ok else 'FAILED'}: "
                                 f"{actual} {step.op} {step.value}")


def _compare(actual: Any, op: CheckOp, expected: Any) -> bool:
    if op == CheckOp.EQ: return actual == expected
    if op == CheckOp.NE: return actual != expected
    if op == CheckOp.LT: return actual < expected
    if op == CheckOp.GT: return actual > expected
    if op == CheckOp.CONTAINS: return expected in (actual or "")
    if op == CheckOp.NOT_CONTAINS: return expected not in (actual or "")
    if op == CheckOp.REGEX: return re.search(str(expected), str(actual or "")) is not None
    return False


def _okcode_target():
    from .schema import DomCandidates, Target
    return Target(sid="wnd[0]/tbar[0]/okcd",
                  dom=DomCandidates(attr={"id": "okcd"},
                                    label="Command Field"))
