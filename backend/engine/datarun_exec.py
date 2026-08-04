"""D-049c: run a recorded flow once per data row, against the live system.

This is the loop that turns "a recorded transaction" into "20 executed
transactions with evidence". Everything it needs already exists — the
ReplayEngine, the recorded flow, D-043's overrides, screenshot capture —
so the value here is entirely in the DISCIPLINE around the loop:

  - **Per-iteration isolation of evidence.** Each row gets its own
    artifact directory, so the auditor's document can prove which
    screenshots belong to which input row. A shared directory would make
    that impossible after the fact.

  - **Per-iteration recovery.** A business transaction that fails halfway
    leaves the session somewhere unpredictable — a half-filled screen, a
    modal asking to save, a lock. Without an explicit reset, row 5's
    failure silently corrupts rows 6-20 and the whole run is worthless.
    After EVERY row (pass or fail) the session is returned to the flow's
    starting T-code and popups are dismissed. `/n<tcode>` is used because
    it cancels whatever is in progress, which is exactly what's wanted and
    is transaction-agnostic.

  - **Failure isolation.** One row failing never aborts the run — the
    D-040 per-flow lesson applied per data row. With 10-20 rows and a
    human waiting, losing rows 6-20 because row 5 hit a blocked vendor
    would be the worst possible behaviour.

  - **Business errors are not step failures.** SAP routinely completes
    every step successfully while the screen says "Vendor is blocked".
    The screen text is read after each iteration and checked, because an
    evidence document that marked those PASS would mislead an auditor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from .datarun import (Binding, DataRunResult, IterationResult, extract_error,
                       extract_outcome, looks_like_error, overrides_for_row)


async def _page_text(page) -> str:
    """Visible text of the current screen, for outcome/error detection."""
    try:
        return await page.inner_text("body")
    except Exception:  # noqa: BLE001 - never let evidence-gathering break the run
        return ""


def _start_tcode(flow) -> str:
    from .schema import Act
    for step in flow.steps:
        if step.act == Act.START_TCODE and step.value:
            return str(step.value)
    return ""


# Dialog buttons that are safe to press purely to CLEAR a blocking modal.
# recovery.py deliberately refuses to guess-click unknown dialogs, because
# a wrong click could confirm something destructive (D-004). That principle
# is kept: this allowlist contains only acknowledge/abandon actions, never
# "Yes", "Save", "Delete" or "OK" — any of which could commit something.
_SAFE_DIALOG_BUTTONS = ("Cancel", "Continue", "Close", "Exit", "No")

# Anything that covers the screen and swallows clicks.
_BLOCKING = ("[role=dialog]", ".lsBlockLayer:visible", "#popup",
             ".urPWContent", ".lsMessageBox")


async def _blocked(page) -> bool:
    for sel in _BLOCKING:
        try:
            if await page.locator(sel).first.is_visible(timeout=150):
                return True
        except Exception:  # noqa: BLE001,PERF203
            continue
    return False


async def _recover(page, tcode: str, webgui_url: str, client: str) -> str:
    """Return the session to a usable state before the next row, and SAY SO
    if it couldn't.

    This is the difference between a run that works for 3 rows and one that
    works unattended for 20. A failed transaction leaves a modal that
    swallows clicks, so without this every subsequent row fails for the
    wrong reason — and worse, fails in a way that looks like a data problem
    rather than a tooling one.

    Escalates gently: known popups, then Escape (cancels, never confirms),
    then a safe-labelled dialog button, then /n<tcode> which is SAP's own
    "abandon and go elsewhere". Finally VERIFIES the screen is usable,
    because a recovery that silently didn't work is worse than none."""
    from .recovery import dismiss_known_popups
    from .tcode_nav import goto_tcode
    actions: list[str] = []
    try:
        await dismiss_known_popups(page)

        if await _blocked(page):
            await page.keyboard.press("Escape")
            actions.append("pressed Escape")

        if await _blocked(page):
            for label in _SAFE_DIALOG_BUTTONS:
                try:
                    btn = page.get_by_role("button", name=label, exact=False).first
                    if await btn.is_visible(timeout=200):
                        await btn.click(timeout=2000)
                        actions.append(f"dismissed dialog via {label!r}")
                        break
                except Exception:  # noqa: BLE001,PERF203
                    continue

        if tcode:
            await goto_tcode(page, tcode, webgui_url=webgui_url, client=client)
            await dismiss_known_popups(page)

        if await _blocked(page):
            return ("session could NOT be returned to a clean state before the "
                    "next row — a dialog is still blocking the screen, so "
                    "later rows may fail for unrelated reasons"
                    + (f" (tried: {', '.join(actions)})" if actions else ""))
        return f"session recovered ({', '.join(actions)})" if actions else ""
    except Exception as exc:  # noqa: BLE001
        return f"session recovery failed before the next row: {exc!r}"


async def execute_data_run(
    *,
    page,
    mode: str,
    run_id: str,
    sid: str,
    flow_dict: dict,
    bindings: list[Binding],
    rows: list[dict[str, Any]],
    webgui_url: str = "",
    client: str = "",
    artifact_root: str = "run_artifacts",
    outcome_pattern: Optional[str] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> DataRunResult:
    """Replay `flow_dict` once per row in `rows`.

    `flow_dict` (not a Flow object) is taken deliberately: each iteration
    needs a FRESH copy, because overrides mutate step values in place and
    row 2 must not inherit row 1's substitutions.
    """
    from .engine import ReplayEngine
    from .schema import Flow

    result = DataRunResult(flow_id=flow_dict.get("flow_id", "flow"), sid=sid,
                            total=len(rows))
    tcode = _start_tcode(Flow(**json.loads(json.dumps(flow_dict))))

    def progress(payload: dict) -> None:
        if on_progress:
            try:
                on_progress(payload)
            except Exception:  # noqa: BLE001 - telemetry never breaks the run
                pass

    for i, row in enumerate(rows, start=1):
        iteration = IterationResult(index=i, inputs=dict(row))
        progress({"type": "iteration_start", "index": i, "total": len(rows),
                  "inputs": dict(row)})

        # Fresh flow per row — overrides mutate step values in place.
        flow = Flow(**json.loads(json.dumps(flow_dict)))
        overrides, notes = overrides_for_row(bindings, row)
        iteration.notes.extend(notes)

        from .datarun import apply_overrides
        iteration.notes.extend(apply_overrides(flow, overrides))

        iter_dir = Path(artifact_root) / run_id / f"iter_{i:03d}"
        engine = ReplayEngine(page, mode, f"{run_id}_i{i}", sid, iter_dir,
                               webgui_url=webgui_url, client=client)
        try:
            fr = await engine.run(flow)
            iteration.ok = bool(fr.ok)
            iteration.failed_steps = [f"step {s.index} ({s.act})"
                                       for s in fr.steps if not s.ok]
            if fr.error:
                iteration.error = str(fr.error)
        except Exception as exc:  # noqa: BLE001 - one row never kills the run
            iteration.ok = False
            iteration.error = f"iteration failed: {exc!r}"

        # Evidence + business-level verdict from the screen itself.
        #
        # ERROR TAKES PRECEDENCE over a matched outcome, deliberately. SAP
        # screens routinely contain both a success-shaped phrase and the
        # actual error, and an earlier version marked a blocked vendor PASS
        # because an outcome pattern happened to match first. In an
        # auditor's document a false PASS is far more damaging than a false
        # FAIL, so ambiguity resolves to failure.
        text = await _page_text(page)
        screen_error = extract_error(text)
        if screen_error:
            iteration.ok = False
            iteration.error = iteration.error or screen_error
            iteration.outcome = ""
        else:
            iteration.outcome = extract_outcome(text, outcome_pattern)

        if iter_dir.is_dir():
            iteration.screenshots = [str(p) for p in sorted(iter_dir.glob("*.png"))]

        recovery_note = await _recover(page, tcode, webgui_url, client)
        if recovery_note:
            iteration.notes.append(recovery_note)

        result.iterations.append(iteration)
        if iteration.ok:
            result.passed += 1
        else:
            result.failed += 1
        progress({"type": "iteration_done", "index": i, "total": len(rows),
                  "ok": iteration.ok, "outcome": iteration.outcome,
                  "error": iteration.error})

    return result
