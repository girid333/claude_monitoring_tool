"""D-049d: HTTP surface for data-driven runs.

The workflow this supports, end to end:

  1. Record a transaction once (existing Recorder).
  2. GET  /api/datarun/bindings/{flow_id}   — which recorded fields can be
     data-driven, with the recorded sample values.
  3. POST /api/datarun/template             — download an Excel template
     with those columns, pre-filled with the recorded row.
  4. POST /api/datarun/start                — upload the filled sheet; the
     run executes in the background, one iteration per row.
  5. GET  /api/datarun/events/{run_id}      — live per-row progress.
  6. GET  /api/datarun/result/{run_id}      — outcome + the Word/PDF
     evidence document for auditors and testers.

Runs happen in the background because 10-20 screen replays take minutes —
far too long for one HTTP request, and the operator wants to watch rows
tick past rather than stare at a spinner.

Mounted additively (its own router, own storage) — nothing in the
existing run pipeline, recorder or Core BASIS changes because of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .datarun import Binding, read_dataset, suggest_bindings, write_template

router = APIRouter(prefix="/api/datarun", tags=["datarun"])

# run_id -> {"queue": Queue, "result": dict|None, "reports": {...}, "error": str}
_runs: dict[str, dict[str, Any]] = {}


def _flows_dir() -> str:
    return os.environ.get("FLOWS_DIR", "flows")


def _systems_path() -> str:
    return os.environ.get("SYSTEMS_PATH", "systems.json")


def _safe_id(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value \
        and not value.startswith(".")


def _load_flow_dict(flow_id: str) -> dict:
    if not _safe_id(flow_id):
        raise HTTPException(400, "invalid flow_id")
    path = Path(_flows_dir()) / f"{flow_id}.json"
    if not path.exists():
        raise HTTPException(404, f"flow not found: {flow_id}")
    return json.loads(path.read_text())


def _described(flow_dict: dict) -> list[dict]:
    from .recorder.recorder import describe_steps
    from .schema import Flow
    return describe_steps(Flow(**flow_dict).steps)


# ── 2. what can be data-driven ───────────────────────────────────────────

@router.get("/bindings/{flow_id}")
def get_bindings(flow_id: str, include_toggles: bool = False) -> dict:
    """Suggested dataset columns for a recorded flow, plus the full step
    list so the UI can show what will and won't vary."""
    flow_dict = _load_flow_dict(flow_id)
    steps = _described(flow_dict)
    bindings = suggest_bindings(steps, include_toggles=include_toggles)
    return {
        "flow_id": flow_id,
        "description": flow_dict.get("description", ""),
        "step_count": len(steps),
        "steps": [{"index": i, **s} for i, s in enumerate(steps)],
        "bindings": [b.as_dict() for b in bindings],
    }


# ── 3. the template a business user fills in ─────────────────────────────

class TemplateRequest(BaseModel):
    flow_id: str
    bindings: Optional[list[dict]] = None   # omit to use the suggestions


@router.post("/template")
def download_template(req: TemplateRequest) -> FileResponse:
    flow_dict = _load_flow_dict(req.flow_id)
    bindings = _bindings_from(req.bindings, flow_dict)
    if not bindings:
        raise HTTPException(400, "this flow has no editable fields to vary")
    out = Path(tempfile.gettempdir()) / f"{req.flow_id}_template.xlsx"
    write_template(bindings, out)
    return FileResponse(out, filename=f"{req.flow_id}_data_template.xlsx",
                         media_type="application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet")


def _bindings_from(raw: Optional[list[dict]], flow_dict: dict) -> list[Binding]:
    if raw:
        return [Binding(step_index=int(b["step_index"]), column=str(b["column"]),
                         label=str(b.get("label", "")),
                         recorded_value=b.get("recorded_value"))
                for b in raw if b.get("column")]
    return suggest_bindings(_described(flow_dict))


# ── 4. run it ────────────────────────────────────────────────────────────

@router.post("/start")
async def start_data_run(background: BackgroundTasks,
                          sid: str = Form(...),
                          flow_id: str = Form(...),
                          bindings: str = Form(""),
                          outcome_pattern: str = Form(""),
                          dataset: UploadFile = File(...)) -> dict:
    """Upload the filled template and start the run."""
    flow_dict = _load_flow_dict(flow_id)
    parsed = json.loads(bindings) if bindings.strip() else None
    binding_objs = _bindings_from(parsed, flow_dict)
    if not binding_objs:
        raise HTTPException(400, "no fields bound to dataset columns")

    suffix = Path(dataset.filename or "data.xlsx").suffix or ".xlsx"
    tmp = Path(tempfile.gettempdir()) / f"datarun_{uuid.uuid4().hex[:8]}{suffix}"
    tmp.write_bytes(await dataset.read())
    try:
        rows = read_dataset(tmp)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not read the data file: {exc}")
    if not rows:
        raise HTTPException(400, "the data file has no rows")

    # Catch a mismatched sheet BEFORE touching SAP: running 20 transactions
    # with the recorded values because the columns were renamed would be
    # both useless and, on a write transaction, harmful.
    expected = {b.column for b in binding_objs}
    present = set(rows[0].keys())
    if not (expected & present):
        raise HTTPException(400, (
            f"none of the expected columns were found in the sheet. "
            f"Expected any of: {', '.join(sorted(expected))}. "
            f"Found: {', '.join(sorted(present)) or '(none)'}. "
            "Download the template again if the flow was re-recorded."))

    # A multi-row run needs a way back to the transaction's first screen
    # between iterations. Without a recorded start_tcode there is nothing to
    # navigate back to, so row 2 lands on whatever screen row 1 finished on
    # and fails with a baffling "could not resolve target" — as do all the
    # rows after it. Fail fast with the actual cause rather than burning 20
    # rows to discover it.
    from .schema import Act, Flow
    has_tcode = any(s.act == Act.START_TCODE and s.value
                    for s in Flow(**flow_dict).steps)
    if len(rows) > 1 and not has_tcode:
        raise HTTPException(400, (
            f"'{flow_id}' was recorded without a starting T-code, so the session "
            "cannot be returned to the first screen between rows — only the first "
            "row would succeed. Re-record it using the Recorder's 'Start at T-code' "
            "field (e.g. SE16), then run again. A single-row dataset would still work."))

    run_id = uuid.uuid4().hex[:12]
    _runs[run_id] = {"queue": asyncio.Queue(), "result": None,
                     "reports": {}, "error": "", "done": False,
                     "flow_id": flow_id, "sid": sid, "total": len(rows)}
    background.add_task(_execute, run_id, sid, flow_dict, binding_objs, rows,
                        outcome_pattern.strip() or None)
    return {"run_id": run_id, "rows": len(rows),
            "missing_columns": sorted(expected - present)}


async def _execute(run_id: str, sid: str, flow_dict: dict,
                    bindings: list[Binding], rows: list[dict],
                    outcome_pattern: Optional[str]) -> None:
    state = _runs[run_id]
    queue: asyncio.Queue = state["queue"]
    loop = asyncio.get_running_loop()

    def emit(payload: dict) -> None:
        # execute_data_run's callback is sync by contract; put_nowait keeps
        # events ORDERED relative to run_done (the D-032 lesson).
        queue.put_nowait(payload)

    try:
        from playwright.async_api import async_playwright

        from .datarun_exec import execute_data_run
        from .login.basic import BasicLoginPlugin
        from .login.base import SystemTarget
        from .probe import probe
        from .registry import Registry, RegistryCredentialProvider

        reg = Registry.load(_systems_path())
        entry = reg.get(sid)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            try:
                await BasicLoginPlugin(
                    SystemTarget(sid, entry.webgui_url, entry.client),
                    RegistryCredentialProvider(reg)).login(page)
                pr = await probe(page)
                emit({"type": "run_start", "total": len(rows), "mode": pr.mode})
                result = await execute_data_run(
                    page=page, mode=pr.mode, run_id=run_id, sid=sid,
                    flow_dict=flow_dict, bindings=bindings, rows=rows,
                    webgui_url=entry.webgui_url, client=entry.client,
                    artifact_root="run_artifacts",
                    outcome_pattern=outcome_pattern, on_progress=emit)
            finally:
                await ctx.close()
                await browser.close()

        state["result"] = result.as_dict()

        # The deliverable.
        from data_run_report import DataRunReportGenerator
        gen = DataRunReportGenerator(
            result.as_dict(), [b.as_dict() for b in bindings], sid=sid,
            output_dir=str(Path("reports") / f"datarun_{run_id}"),
            description=flow_dict.get("description", ""),
            executed_by=(entry.sap_credential.username or ""))
        docx = gen.generate_word()
        try:
            pdf = gen.generate_pdf()
        except Exception as exc:  # noqa: BLE001 - a PDF failure must not lose the docx
            pdf = ""
            emit({"type": "note", "message": f"PDF generation failed: {exc!r}"})
        state["reports"] = {"docx": docx, "pdf": pdf}
        emit({"type": "report_ready", "docx": docx, "pdf": pdf})
    except Exception as exc:  # noqa: BLE001
        state["error"] = repr(exc)
        emit({"type": "run_error", "error": repr(exc)})
    finally:
        state["done"] = True
        emit({"type": "run_done"})


# ── 5. live progress ─────────────────────────────────────────────────────

@router.get("/events/{run_id}")
async def data_run_events(run_id: str) -> StreamingResponse:
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(404, "unknown run")

    async def stream():
        queue: asyncio.Queue = state["queue"]
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"        # D-029: proxies drop idle SSE
                continue
            yield f"data: {json.dumps(event, default=str)}\n\n"
            if event.get("type") == "run_done":
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── 6. outcome + the evidence document ───────────────────────────────────

@router.get("/result/{run_id}")
def data_run_result(run_id: str) -> dict:
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(404, "unknown run")
    return {
        "run_id": run_id, "flow_id": state["flow_id"], "sid": state["sid"],
        "done": state["done"], "error": state["error"],
        "result": state["result"],
        "reports": {k: bool(v) for k, v in (state["reports"] or {}).items()},
    }


@router.get("/report/{run_id}/{kind}")
def download_report(run_id: str, kind: str) -> FileResponse:
    if kind not in ("docx", "pdf"):
        raise HTTPException(400, "kind must be docx or pdf")
    state = _runs.get(run_id)
    if state is None:
        raise HTTPException(404, "unknown run")
    path = (state.get("reports") or {}).get(kind)
    if not path or not Path(path).exists():
        raise HTTPException(404, "report not available yet")
    return FileResponse(path, filename=Path(path).name)
