"""D-052 HTTP API: SAP + HANA performance.

    GET  /api/perf/{sid}/live              current status (+ last 24 h profile)
    GET  /api/perf/{sid}/day?date=YYYY-MM-DD   one day from SAP's own history
    GET  /api/perf/{sid}/ranges            which dates SAP still holds, per source
    POST /api/perf/export?format=xlsx|pdf  file built from the posted view
    GET  /api/perf/{sid}/smon/schedule     daily SMON/MON scheduling state (read)
    POST /api/perf/{sid}/smon/schedule     schedule daily monitoring (WRITE, needs confirm)
    POST /api/perf/{sid}/smon/unschedule   delete the future scheduler jobs (WRITE, needs confirm)

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

from ahc.sap import Sap
from ahc.xbp import JobError

from . import export, service, smon_sched

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
    if not isinstance(view.get("kpis"), list) or not all(isinstance(k, dict) for k in view["kpis"]):
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


def _user(client) -> str:
    return (getattr(getattr(client, "target", None), "username", "") or "").upper()


def _sched(sid: str, fn):
    client, _ = _client(sid)
    try:
        sap = Sap(client)
        _, now = service.system_now(sap)
        return fn(sap, now, client)
    except JobError as exc:
        raise HTTPException(409 if exc.kind == "refused" else 502, exc.message) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RfcSoapError as exc:
        raise HTTPException(502, f"{sid}: {exc.message}") from exc
    finally:
        client.close()


@router.get("/{sid}/smon/schedule")
def smon_schedule_status(sid: str) -> dict:
    """What daily snapshot monitoring is scheduled (read-only)."""
    return _sched(sid, lambda sap, now, c: smon_sched.status(sap, now))


@router.post("/{sid}/smon/schedule")
def schedule_smon(sid: str, body: dict[str, Any] = Body(default={})) -> dict:
    """WRITES to SAP: the daily /SDF/SMON (or /SDF/MON) scheduler jobs, as /SDF/SMON's own button does.
    The browser must send {"confirm": true} after the user agreed."""
    if body.get("confirm") is not True:
        raise HTTPException(400, 'Scheduling writes background jobs to SAP — send {"confirm": true}.')
    try:
        interval, keep = int(body.get("interval_s", 10)), int(body.get("keep_days", 14))
    except (TypeError, ValueError):
        raise HTTPException(400, "interval_s and keep_days must be whole numbers") from None
    return _sched(sid, lambda sap, now, c: smon_sched.schedule(sap, now, _user(c), interval, keep, lock_key=sid))


@router.post("/{sid}/smon/unschedule")
def unschedule_smon(sid: str, body: dict[str, Any] = Body(default={})) -> dict:
    """WRITES to SAP: deletes the future daily scheduler jobs."""
    if body.get("confirm") is not True:
        raise HTTPException(400, 'Stopping deletes SAP background jobs — send {"confirm": true}.')
    return _sched(sid, lambda sap, now, c: smon_sched.unschedule(sap, now))
