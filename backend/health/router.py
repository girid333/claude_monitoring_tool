"""HTTP API for the Health Check Dashboard. Mount with one line in main.py:

    from health.router import router as health_router
    app.include_router(health_router)
"""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from .analyze import analyze
from .models import HealthReport, ReportListItem
from .store import HealthStore
from .workbook import MAX_BYTES, WorkbookError, read_workbook
from .xlsx_export import XLSX_MIME, spreadsheetml_to_xlsx

router = APIRouter(prefix="/api/health", tags=["health"])
_store: HealthStore | None = None


def store() -> HealthStore:
    global _store
    if _store is None:
        _store = HealthStore()
    return _store


@router.post("/reports", response_model=HealthReport, status_code=201)
async def import_report(file: UploadFile = File(...)) -> HealthReport:
    data = await file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(400, "The file is empty.")
    try:
        sheets = read_workbook(data, file.filename or "")
        rep = analyze(sheets, (file.filename or "report")[:200])
    except WorkbookError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not rep.checks:
        raise HTTPException(422, "No health checks found. Import the workbook produced "
                                 "by SAP's Automated Health Check (it has a 'Monitors' tab).")
    if data[:2] != b"PK":                # SpreadsheetML: keep the original for export
        rep.has_workbook = True
        store().save_workbook(rep.id, data)
    store().save(rep)
    return rep


@router.get("/reports", response_model=list[ReportListItem])
def list_reports() -> list[ReportListItem]:
    return store().list()


@router.get("/reports/latest", response_model=Optional[HealthReport])
def latest_report() -> Optional[HealthReport]:
    """Newest by health-check date. `null` (not 404) when nothing is imported:
    an empty store is a normal state, and the landing page asks on every load."""
    items = store().list()
    return store().get(items[0].id) if items else None


@router.get("/reports/{rid}", response_model=HealthReport)
def get_report(rid: str) -> HealthReport:
    try:
        return store().get(rid)
    except KeyError:
        raise HTTPException(404, "Report not found.") from None


@router.delete("/reports/{rid}", status_code=204)
def delete_report(rid: str) -> None:
    try:
        store().delete(rid)
    except KeyError:
        raise HTTPException(404, "Report not found.") from None


@router.get("/reports/{rid}/workbook")
def download_workbook(rid: str, format: str = "xlsx") -> Response:
    """D-051: the report's AHC workbook. Default .xlsx (converted, opens in
    every Excel); format=xls returns the stored SpreadsheetML unchanged —
    the uploaded original or the workbook a direct pull generated."""
    try:
        rep, data = store().get(rid), store().workbook(rid)
    except KeyError:
        raise HTTPException(404, "No workbook stored for this report.") from None
    stem = re.sub(r"\.(xls|xml|xlsx)$", "", rep.filename, flags=re.I) or f"AHC_{rid}"
    stem = re.sub(r'[^A-Za-z0-9._ -]', "_", stem)
    if format.lower() == "xls":
        ext = "xlsx" if data[:2] == b"PK" else "XLS"
        mime = XLSX_MIME if ext == "xlsx" else "application/vnd.ms-excel"
        return Response(data, media_type=mime,
                        headers={"Content-Disposition": f'attachment; filename="{stem}.{ext}"'})
    if format.lower() != "xlsx":
        raise HTTPException(400, "format must be xlsx or xls")
    try:
        out = spreadsheetml_to_xlsx(data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not convert the workbook to .xlsx: {exc}") from exc
    return Response(out, media_type=XLSX_MIME,
                    headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'})