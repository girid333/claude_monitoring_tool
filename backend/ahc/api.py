"""D-051 HTTP API: pull a health check straight from SAP (SOAP RFC).

    POST /api/health/pull {"sid": "F4H"}  ->  HealthReport (stored like an import)

The pull writes SAP's AHC workbook (ahc/writer.py), then imports it through
the D-050 parser — the same path an uploaded SAP export takes — and keeps the
workbook so GET /api/health/reports/{id}/workbook exports it unchanged.
Credentials come from the system registry (D-023, encrypted); none are
accepted in the request or written anywhere else (D-018).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from engine import crypto
from engine.registry import Registry
from engine.rfc.soap_client import RfcSoapClient, RfcSoapError
from health.analyze import analyze
from health.models import HealthReport
from health.router import store
from health.workbook import read_workbook

from .run import pull

router = APIRouter(prefix="/api/health", tags=["health"])


class PullRequest(BaseModel):
    sid: str


def _client(sid: str) -> tuple[RfcSoapClient, str]:
    reg = Registry.load(os.environ.get("SYSTEMS_PATH", "systems.json"))
    try:
        entry = reg.get(sid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"Unknown system {sid}. Add it on the Systems page first.") from exc
    cred = entry.sap_credential
    if not cred.is_set():
        raise HTTPException(400, f"System {sid} has no SAP user/password stored. Add them on the "
                                 "Systems page — the pull uses the same credential as WebGUI.")
    try:
        password = crypto.decrypt(cred.password_enc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Could not decrypt the stored SAP password for {sid}: {exc}") from exc
    try:
        return RfcSoapClient.from_system(entry.webgui_url, client=entry.client, username=cred.username,
                                         password=password, timeout_s=60), entry.client
    except RfcSoapError as exc:
        raise HTTPException(400, exc.message) from exc


@router.post("/pull", response_model=HealthReport, status_code=201)
def pull_from_sap(req: PullRequest) -> HealthReport:
    client, sap_client = _client(req.sid)
    try:
        data, meta = pull(client, sap_client)
    except RfcSoapError as exc:
        # The very first calls (system info, server list) failing means no
        # connection at all — say so plainly instead of a half-empty report.
        raise HTTPException(502, f"Could not pull from {req.sid}: {exc.message}") from exc
    finally:
        client.close()
    rep = analyze(read_workbook(data, "pull.XLS"),
                  f"{meta['sid']}_{meta['client']}_AHC_direct.XLS")
    rep.source, rep.has_workbook = "sap_pull", True
    store().save_workbook(rep.id, data)
    store().save(rep)
    return rep
