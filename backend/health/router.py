"""HTTP API for the Health Check Dashboard. Mount with one line in main.py:

    from health.router import router as health_router
    app.include_router(health_router)
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from .analyze import analyze
from .models import HealthReport, ReportListItem
from .store import HealthStore
from .workbook import MAX_BYTES, WorkbookError, read_workbook

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
