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

from .checks import evaluate as evaluate_check
from .resolvers.base import CONFIDENCE_FLAG_THRESHOLD, Resolver
from .resolvers.dom import DomResolver
from .resolvers.scripting import ScriptingResolver
from .schema import Act, Flow, FlowRunResult, Step, StepResult
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
                 artifact_dir: Path, webgui_url: str = "", client: str = ""):
        self.page = page
        self.mode = mode
        self.run_id = run_id
        self.sid = sid
        self.artifact_dir = artifact_dir
        # D-034: needed for the ~transaction URL fallback in START_TCODE.
        self.webgui_url = webgui_url
        self.client = client
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
            # D-034: use v1's proven navigation (all frames, three selectors,
            # ~transaction URL fallback). The previous single-selector,
            # main-frame-only lookup failed on real systems.
            from .tcode_nav import goto_tcode
            method = await goto_tcode(self.page, str(value),
                                       webgui_url=getattr(self, "webgui_url", ""),
                                       client=getattr(self, "client", ""))
            await wait_ready(self.page, step.timeout_s, self.mode == "scripting")
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=self.resolver.name,
                              detail=f"tcode {value} (via {method})")

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

        if step.act == Act.SCROLL:
            pos = step.value if isinstance(step.value, dict) else {}

            # D-038: gesture replay — the recorded wheel, at the recorded
            # coordinates. Works whether the target scrolls natively or via
            # SAP's own scrollbar widget (which never touches scrollTop, so
            # D-037's position-restoring approach silently did nothing on a
            # real ALV grid). Deltas are applied in chunks because SAP
            # coalesces large single deltas into one small scroll.
            if "dy" in pos or "dx" in pos:
                x, y = float(pos.get("x", 640)), float(pos.get("y", 400))
                dx, dy = float(pos.get("dx", 0)), float(pos.get("dy", 0))
                await self.page.mouse.move(x, y)
                remaining_y, remaining_x = dy, dx
                step_y = 400 if dy >= 0 else -400
                step_x = 400 if dx >= 0 else -400
                guard = 0
                while (abs(remaining_y) > 1 or abs(remaining_x) > 1) and guard < 60:
                    cy = step_y if abs(remaining_y) > abs(step_y) else remaining_y
                    cx = step_x if abs(remaining_x) > abs(step_x) else remaining_x
                    await self.page.mouse.wheel(cx, cy)
                    remaining_y -= cy
                    remaining_x -= cx
                    guard += 1
                    await self.page.wait_for_timeout(60)
                await self.page.wait_for_timeout(300)   # let the grid repaint
                return StepResult(index=i, act=step.act, ok=True,
                                  resolver=self.resolver.name,
                                  detail=f"wheel {int(dx)},{int(dy)} at {int(x)},{int(y)}")

            # Legacy (D-037) position-restoring shape.
            top, left = int(pos.get("top", 0)), int(pos.get("left", 0))
            if step.target is not None and step.target.dom is not None:
                try:
                    res = await self.resolver.resolve(step.target)
                    await res.handle.evaluate(
                        "(el, p) => { el.scrollTop = p.top; el.scrollLeft = p.left; }",
                        {"top": top, "left": left})
                    detail = f"container -> {top},{left}"
                except Exception:  # noqa: BLE001 — fall back to the document
                    await self.page.evaluate("p => window.scrollTo(p.left, p.top)",
                                              {"top": top, "left": left})
                    detail = f"document (container unresolved) -> {top},{left}"
            else:
                await self.page.evaluate("p => window.scrollTo(p.left, p.top)",
                                          {"top": top, "left": left})
                detail = f"document -> {top},{left}"
            await self.page.wait_for_timeout(250)   # let the grid repaint
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=self.resolver.name, detail=detail)

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
            # D-037: viewport, NOT full_page. SAP WebGUI is a fixed-height app
            # whose grids scroll an inner container, so a full-page shot both
            # ignores the scroll position and renders the same image every
            # time — the user asked for "this view", so capture this view.
            await self.page.screenshot(path=str(path), full_page=False)
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
        cr = evaluate_check(step, last_capture)
        result.checks.append(cr)
        return StepResult(index=i, act=step.act, ok=True,
                          detail=f"check {'passed' if cr.ok else 'FAILED'}: "
                                 f"{cr.actual} {step.op} {step.value}")


def _okcode_target():
    from .schema import DomCandidates, Target
    return Target(sid="wnd[0]/tbar[0]/okcd",
                  dom=DomCandidates(attr={"id": "okcd"},
                                    label="Command Field"))
