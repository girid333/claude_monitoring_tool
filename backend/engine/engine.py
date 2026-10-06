"""ReplayEngine — executes a Flow against one system (D-008, D-009, D-010).

Stateless by design: everything is keyed by run_id + sid, results are
returned, nothing global. Parallelism later = orchestration loop change.

Resolver selection comes from the capability probe; a per-step fallback
from scripting -> dom is deliberately NOT automatic (a scripting failure on
a scripting-enabled system is a signal, not something to paper over).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from .checks import evaluate as evaluate_check
from .resolvers.base import CONFIDENCE_FLAG_THRESHOLD, ResolveError, Resolver
from .resolvers.dom import DomResolver, UnexpectedDialog
from .resolvers.scripting import ScriptingResolver
from .schema import Act, Flow, FlowRunResult, Step, StepResult
from .sync import NetworkTracker, wait_ready, wait_roundtrip
from .webgui_dom import normalize_title, read_screen, read_statusbar

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


# Acts that send something to SAP and therefore end in a roundtrip whose
# status-bar message counts (eCATT checks the message after each of these).
_ROUNDTRIP = {Act.START_TCODE, Act.PRESS, Act.DBLCLICK, Act.VKEY}


class StepFailed(Exception):
    """A step failed for a reason worth stating in plain words."""


def _tracker_for(page: Page) -> NetworkTracker:
    # One tracker per page: data runs create a new engine for every row on
    # the same page, and stacking listeners would count requests N times.
    t = getattr(page, "_sentinel_tracker", None)
    if t is None:
        t = NetworkTracker(page)
        try:
            setattr(page, "_sentinel_tracker", t)
        except Exception:  # noqa: BLE001
            pass
    return t


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
        # D-053: DOM-only abilities (double-click, reading values, dialogs,
        # tables) are available whichever resolver drives the run.
        self.dom: DomResolver = (self.resolver if isinstance(self.resolver, DomResolver)
                                 else DomResolver(page))
        self.tracker = _tracker_for(page)

    async def run(self, flow: Flow, dataset: dict[str, Any] | None = None) -> FlowRunResult:
        params = {**flow.params, **(dataset or {})}
        result = FlowRunResult(run_id=self.run_id, sid=self.sid,
                               flow_id=flow.flow_id, mode=self.mode)  # type: ignore[arg-type]
        last_capture: dict[str, Any] = {}
        self._flow = flow

        for i, step in enumerate(flow.steps):
            try:
                sr = await self._exec_step(i, step, params, last_capture, result)
            except StepFailed as exc:
                sr = StepResult(index=i, act=step.act, ok=False, detail=str(exc))
            except Exception as exc:  # noqa: BLE001 — every step failure is data
                sr = StepResult(index=i, act=step.act, ok=False, detail=repr(exc))

            # D-053: eCATT message handling. An E/A message after a roundtrip
            # means SAP refused the input; carrying on types into a screen
            # that is not the recorded one. Expected errors are recorded as a
            # status-bar check right after the step — then the check decides.
            if (sr.ok and step.act in _ROUNDTRIP and flow.stop_on_error_message
                    and sr.message and sr.message.get("type") in ("E", "A")
                    and not self._next_check_expects(flow, i, sr.message)):
                sr.ok = False
                sr.detail = (f"SAP {'error' if sr.message['type'] == 'E' else 'abort'} "
                             f"message: {sr.message.get('text', '')}")

            if not sr.ok:
                await self._failure_evidence(i, step, sr)
                result.steps.append(sr)
                result.error = f"step {i} ({step.act.value}) failed: {sr.detail}"
                break
            result.steps.append(sr)

        result.ok = result.error is None and all(c.ok for c in result.checks)
        return result

    @staticmethod
    def _next_check_expects(flow: Flow, i: int, message: dict) -> bool:
        """True when the next real step is a status-bar check that ACCEPTS
        this message (an expected error recorded on purpose). A check that
        expects success must not wave an error through."""
        from .checks import compare
        from .schema import CheckOp
        for nxt in flow.steps[i + 1:]:
            if nxt.act == Act.WAIT_READY:
                continue
            if nxt.act != Act.CHECK or nxt.type not in ("statusbar_type", "statusbar_text"):
                return False
            actual = message.get("type", "") if nxt.type == "statusbar_type" else message.get("text", "")
            return compare(actual, nxt.op or CheckOp.EQ, nxt.value)
        return False

    async def _failure_evidence(self, i: int, step: Step, sr: StepResult) -> None:
        """D-053: a failed step always leaves a screenshot of the screen it
        failed on — the first thing anyone asks when a test run goes red."""
        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / f"{self.run_id}_{self.sid}_step{i}_FAILED.png"
            await self.page.screenshot(path=str(path), full_page=False)
            sr.screenshot_path = sr.screenshot_path or str(path)
        except Exception:  # noqa: BLE001 - evidence never masks the failure
            pass
        if step.target is not None and "Could not resolve" in sr.detail:
            try:
                from .healing import capture_repair_item
                await capture_repair_item(self.page, step.target, getattr(self, "_flow").flow_id,
                                          i, self.artifact_dir / "repair")
            except Exception:  # noqa: BLE001
                pass

    async def _screen_note(self, step: Step) -> str:
        if not step.screen:
            return ""
        try:
            scr = await read_screen(self.page)
        except Exception:  # noqa: BLE001
            return ""
        now = scr.get("popup_title") if scr.get("popup") else scr.get("title")
        if now and normalize_title(now) != normalize_title(step.screen):
            return f"expected screen '{step.screen}' but the session is on '{now}'"
        return ""

    async def _resolve(self, step: Step, resolver: Resolver | None = None):
        """Resolve with patience: right after a roundtrip WebGUI may still be
        painting the next screen. Optional steps give up quickly and are
        skipped. Returns None for a skipped optional step."""
        from .recovery import dismiss_known_popups
        resolver = resolver or self.resolver
        assert step.target is not None
        limit = 1.5 if step.optional else min(step.timeout_s, 12.0)
        deadline = time.monotonic() + limit
        tried_popups = False
        last: Exception | None = None
        while True:
            try:
                return await resolver.resolve(step.target)
            except UnexpectedDialog as exc:
                last = exc
                if not tried_popups:
                    tried_popups = True
                    if await dismiss_known_popups(self.page):
                        continue
            except ResolveError as exc:
                last = exc
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        if step.optional:
            return None
        if isinstance(last, UnexpectedDialog):
            raise StepFailed(f"an unexpected dialog is open ('{last.title or 'untitled'}') "
                             f"— the recorded step belongs to the screen behind it")
        note = await self._screen_note(step)
        raise StepFailed((f"{note} — " if note else "") + f"{last}")

    async def _after_roundtrip(self, mark: int, step: Step) -> dict[str, str]:
        saw = await wait_roundtrip(self.page, self.tracker, mark, step.timeout_s,
                                   scripting_mode=self.mode == "scripting")
        if not saw:
            # Nothing went to SAP — whatever the status bar shows is from an
            # earlier step and must not be judged again.
            return {}
        try:
            return await read_statusbar(self.page)
        except Exception:  # noqa: BLE001
            return {"type": "", "text": ""}

    async def _ensure_no_dialog_conflict(self, step: Step) -> None:
        """D-053: before a key press, the same dialog rule as for clicks: a
        key recorded on the main screen must not be pressed into a dialog
        that wasn't there at record time (Enter would CONFIRM it)."""
        from .recovery import dismiss_known_popups
        if step.target is None:
            return
        deadline = time.monotonic() + min(step.timeout_s, 5.0)
        dismissed = False
        while True:
            try:
                await self.dom._scope(step.target)
                return
            except UnexpectedDialog as exc:
                if not dismissed:
                    dismissed = True
                    if await dismiss_known_popups(self.page):
                        continue
                if time.monotonic() >= deadline:
                    raise StepFailed(f"an unexpected dialog is open ('{exc.title or 'untitled'}') "
                                     f"— the recorded key belongs to the screen behind it") from exc
            except ResolveError as exc:
                if time.monotonic() >= deadline:
                    note = await self._screen_note(step)
                    raise StepFailed((f"{note} — " if note else "")
                                     + "the key was recorded in a dialog, but no dialog is open") from exc
            await asyncio.sleep(0.25)

    async def _exec_step(self, i: int, step: Step, params: dict,
                         last_capture: dict, result: FlowRunResult) -> StepResult:
        value = render_params(step.value, params)

        if step.act == Act.START_TCODE:
            # D-034: use v1's proven navigation (all frames, three selectors,
            # ~transaction URL fallback).
            from .tcode_nav import goto_tcode
            mark = self.tracker.started
            method = await goto_tcode(self.page, str(value),
                                       webgui_url=getattr(self, "webgui_url", ""),
                                       client=getattr(self, "client", ""))
            msg = await self._after_roundtrip(mark, step)
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=self.resolver.name, message=msg or None,
                              detail=f"tcode {value} (via {method})")

        if step.act == Act.WAIT_READY:
            await wait_ready(self.page, step.timeout_s, self.mode == "scripting")
            return StepResult(index=i, act=step.act, ok=True)

        if step.act in (Act.SET, Act.PRESS, Act.SELECT, Act.DBLCLICK):
            assert step.target is not None
            resolver = self.dom if step.act == Act.DBLCLICK else self.resolver
            res = await self._resolve(step, resolver)
            if res is None:
                return StepResult(index=i, act=step.act, ok=True, skipped=True,
                                  detail="skipped — optional step, element not present")
            flagged = res.confidence < CONFIDENCE_FLAG_THRESHOLD
            note = await self._screen_note(step)
            detail = res.via + (" [LOW CONFIDENCE]" if flagged else "") + (f" [{note}]" if note else "")
            mark = self.tracker.started
            if step.act == Act.SET:
                await resolver.set_value(res, value)
                # Checkboxes, radios and dropdowns often trigger a roundtrip
                # of their own (function codes on selection screens).
                dom = step.target.dom
                if isinstance(value, bool) or (dom and (dom.tag == "select"
                                                        or dom.sid_type == "GuiComboBox")):
                    await wait_roundtrip(self.page, self.tracker, mark, step.timeout_s,
                                         grace_s=0.4, scripting_mode=self.mode == "scripting")
                return StepResult(index=i, act=step.act, ok=True, resolver=res.resolver,
                                  confidence=res.confidence, detail=detail)
            if step.act == Act.DBLCLICK:
                await self.dom.press(res, double=True)
            else:
                await resolver.press(res)
            msg = await self._after_roundtrip(mark, step)
            return StepResult(index=i, act=step.act, ok=True,
                              resolver=res.resolver, confidence=res.confidence,
                              detail=detail, message=msg or None)

        if step.act == Act.VKEY:
            # D-053: press the key IN the field it was pressed in — Enter
            # submits whatever field has focus, which after a click elsewhere
            # is not the one the user typed into.
            if step.optional and step.target is not None:
                try:
                    await self.dom._scope(step.target)
                except ResolveError:
                    return StepResult(index=i, act=step.act, ok=True, skipped=True,
                                      detail="skipped — optional key, its screen is not showing")
            await self._ensure_no_dialog_conflict(step)
            if step.target is not None and step.target.dom is not None:
                try:
                    probe_step = step.model_copy(update={"optional": True})
                    res = await self._resolve(probe_step, self.dom)
                    if res is not None:
                        await res.handle.focus()
                except Exception:  # noqa: BLE001 - focus is best effort
                    pass
            mark = self.tracker.started
            if isinstance(self.resolver, DomResolver):
                await self.resolver.send_vkey(int(value), key=step.key)
            else:
                wnd = step.target.sid.split("/")[0] if (step.target and step.target.sid) else "wnd[0]"
                await self.resolver.send_vkey(int(value), wnd)
            msg = await self._after_roundtrip(mark, step)
            return StepResult(index=i, act=step.act, ok=True, message=msg or None,
                              detail=step.key or f"vkey {value}")

        if step.act == Act.GET:
            return await self._get(i, step, params, result)
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
            return await self._check(i, step, params, last_capture, result)

        return StepResult(index=i, act=step.act, ok=False,
                          detail=f"unknown act {step.act}")

    async def _capture_pages(self, i: int, step: Step) -> StepResult:
        """D-039: capture a long list page by page.

        Scrolling to two arbitrary offsets produced a first page and a last
        page with everything between missing. This walks the content one
        viewport at a time instead, and stops as soon as a page looks
        identical to the one before it — which is the only implementation-
        agnostic way to detect "no more to scroll", since SAP's ALV widget
        never changes scrollTop (see D-038).
        """
        import hashlib

        max_pages = max(1, min(int(step.pages or 25), 25))
        pos = step.value if isinstance(step.value, dict) else {}
        x = float(pos.get("x", 0) or (self.page.viewport_size or {}).get("width", 1280) / 2)
        y = float(pos.get("y", 0) or (self.page.viewport_size or {}).get("height", 800) / 2)
        page_delta = float(pos.get("page_delta", 600))

        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        seen: set[str] = set()
        await self.page.mouse.move(x, y)

        for p in range(max_pages):
            path = self.artifact_dir / f"{self.run_id}_{self.sid}_step{i}_p{p + 1}.png"
            shot = await self.page.screenshot(path=str(path), full_page=False)
            digest = hashlib.sha1(shot).hexdigest()
            if digest in seen:
                # Nothing new appeared — we're at the end of the list.
                Path(path).unlink(missing_ok=True)
                break
            seen.add(digest)
            paths.append(str(path))
            # Scroll one page. Chunked, because SAP coalesces a single large
            # delta into one small scroll (same reason as D-038).
            remaining = page_delta
            while remaining > 1:
                chunk = min(300.0, remaining)
                await self.page.mouse.wheel(0, chunk)
                remaining -= chunk
                await self.page.wait_for_timeout(50)
            await self.page.wait_for_timeout(350)   # let the grid repaint

        return StepResult(index=i, act=step.act, ok=True,
                          resolver=self.resolver.name,
                          screenshot_path=paths[0] if paths else None,
                          screenshot_paths=paths,
                          detail=f"{len(paths)} page(s) captured"
                                  + (" (hit max)" if len(paths) == max_pages else ""))

    async def _capture(self, i: int, step: Step, last_capture: dict) -> StepResult:
        modes = step.mode or ["screenshot"]
        if step.pages and "screenshot" in modes:
            return await self._capture_pages(i, step)
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

    async def _check(self, i: int, step: Step, params: dict, last_capture: dict,
                     result: FlowRunResult) -> StepResult:
        """Assertion (eCATT CHEGUI / message check). D-053: checks read the
        screen NOW when they name a target or the status bar, instead of
        relying on a capture step that nothing ever filled. The expected
        value may use variables ({{po_number}})."""
        if step.type in ("statusbar_type", "statusbar_text"):
            last_capture["statusbar"] = await read_statusbar(self.page)
        elif step.target is not None and step.type in ("field_value", "row_count", "cell_value"):
            res = await self._resolve(step, self.dom)
            if res is None:
                return StepResult(index=i, act=step.act, ok=True, skipped=True,
                                  detail="skipped — optional check, element not present")
            if step.type == "field_value":
                last_capture["field"] = {"value": await self.dom.read_value(res)}
            else:
                last_capture["table"] = await self.dom.read_table(res)
        elif step.type == "text":
            try:
                last_capture["page_text"] = await self.page.inner_text("body")
            except Exception:  # noqa: BLE001
                last_capture["page_text"] = ""
        expected = render_params(step.value, params)
        cr = evaluate_check(step.model_copy(update={"value": expected}), last_capture)
        result.checks.append(cr)
        return StepResult(index=i, act=step.act, ok=True,
                          detail=f"check {'passed' if cr.ok else 'FAILED'}: "
                                 f"{cr.actual} {step.op.value if step.op else '=='} {expected}")

    async def _get(self, i: int, step: Step, params: dict, result: FlowRunResult) -> StepResult:
        """D-053: eCATT export parameter. Read a field (or the status-bar
        message) into a variable that later steps use as {{name}} — e.g. the
        PO number from "Standard PO created under the number 4500001234"
        feeding ME23N in the next step."""
        if not step.var:
            raise StepFailed("get step has no variable name")
        if step.source == "statusbar" or step.target is None:
            raw = (await read_statusbar(self.page)).get("text", "")
            where = "status bar"
        else:
            res = await self._resolve(step, self.dom)
            if res is None:
                return StepResult(index=i, act=step.act, ok=True, skipped=True,
                                  detail=f"skipped — optional, {step.var} not set")
            raw = await self.dom.read_value(res)
            where = res.via
        val = "" if raw is None else str(raw).strip()
        if step.pattern:
            try:
                m = re.search(step.pattern, val)
            except re.error as exc:
                raise StepFailed(f"invalid pattern {step.pattern!r}: {exc}") from exc
            if m is None:
                val = ""
            else:
                val = (next((g for g in m.groups() if g), None) or m.group(0) or "").strip()
        if not val:
            if step.optional:
                return StepResult(index=i, act=step.act, ok=True, skipped=True,
                                  detail=f"skipped — {step.var} not found in {where}")
            raise StepFailed(f"could not read {step.var} from {where}"
                             + (f" with pattern {step.pattern!r}" if step.pattern else "")
                             + (f" (text was: {str(raw)[:120]!r})" if raw else " (empty)"))
        params[step.var] = val
        result.variables[step.var] = val
        return StepResult(index=i, act=step.act, ok=True, extracted={step.var: val},
                          detail=f"{step.var} = {val} (from {where})")


def _okcode_target():
    from .schema import DomCandidates, Target
    return Target(sid="wnd[0]/tbar[0]/okcd",
                  dom=DomCandidates(attr={"id": "okcd"},
                                    label="Command Field"))
