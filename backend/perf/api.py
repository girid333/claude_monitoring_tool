"""D-052 HTTP API: SAP + HANA performance.

    GET  /api/perf/{sid}/live              current status (+ last 24 h profile)
    GET  /api/perf/{sid}/day?date=YYYY-MM-DD   one day from SAP's own history
    GET  /api/perf/{sid}/ranges            which dates SAP still holds, per source
    POST /api/perf/export?format=xlsx|pdf  file built from the posted view

Credentials come from the system registry, exactly like the health pull.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from ahc.api import _client
from engine.rfc.soap_client import RfcSoapError
from health.xlsx_export import XLSX_MIME

from . import export, service

router = APIRouter(prefix="/api/perf", tags=["performance"])


def _run(sid: str, fn):
    client, sap_client = _client(sid)
    try:
        return fn(client, sap_client)
    except RfcSoapError as exc:
        raise HTTPException(502, f"Could not read performance data from {sid}: {exc.message}") from exc
    finally:
        client.close()


@router.get("/{sid}/live")
def live(sid: str) -> dict:
    return _run(sid, service.live)


@router.get("/{sid}/day")
def day(sid: str, date: str) -> dict:
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    if d > dt.date.today() + dt.timedelta(days=1):
        raise HTTPException(400, "date is in the future")
    if d < dt.date.today() - dt.timedelta(days=service.LIMIT_DAYS + 1):
        raise HTTPException(400, f"date is older than the {service.LIMIT_DAYS}-day analysis window")
    return _run(sid, lambda c, sc: service.day(c, sc, d))


@router.get("/{sid}/ranges")
def ranges(sid: str) -> dict:
    return _run(sid, lambda c, sc: service.ranges(c))


@router.post("/export")
def export_view(format: str = "xlsx", view: dict[str, Any] = Body(...)) -> Response:
    if not isinstance(view.get("kpis"), list):
        raise HTTPException(400, "Post the performance view returned by /live or /day.")
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", f"Performance_{view.get('sid', 'SAP')}_"
                  f"{view.get('date') or str(view.get('as_of', ''))[:16].replace(' ', '_').replace(':', '')}")
    if format == "xlsx":
        return Response(export.to_xlsx(view), media_type=XLSX_MIME,
                        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'})
    if format == "pdf":
        return Response(export.to_pdf(view), media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.pdf"'})
    raise HTTPException(400, "format must be xlsx or pdf")


@router.post("/{sid}/smon/schedule")
def schedule_smon(sid: str) -> dict:
    # The mechanism (which report/job /SDF/SMON's "Schedule" creates) is
    # verified against a live system before this writes anything to SAP.
    raise HTTPException(501, "Scheduling SMON from Sentinel isn't enabled yet: start daily monitoring in "
                             "transaction /SDF/SMON (Schedule new monitoring) for now.")
