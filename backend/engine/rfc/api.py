"""D-045: HTTP surface for the RFC capability — its own APIRouter, mounted
with a single `include_router()` line in main.py, exactly like D-044's
sandbox. Nothing in the existing run pipeline, sections, or recorder is
touched by this file.

Endpoints:
  GET  /api/rfc/checks              what can be checked (id/tcode/FM/confidence)
  POST /api/rfc/test-connection     {sid} -> can we reach + authenticate + call an FM?
  POST /api/rfc/run                 {sid, check_ids?} -> run checks, return rows+anomalies
"""

from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import crypto
from ..registry import Registry
from .basis_checks import describe_checks, run_all
from .soap_client import RfcSoapClient, RfcSoapError, derive_soap_url

router = APIRouter(prefix="/api/rfc", tags=["rfc"])


def _systems_path() -> str:
    return os.environ.get("SYSTEMS_PATH", "systems.json")


def _client_for(sid: str) -> RfcSoapClient:
    """Build a client purely from what's ALREADY registered for this system
    — same host as WebGUI, same encrypted credential. No new setup step is
    the entire point of this approach."""
    reg = Registry.load(_systems_path())
    try:
        entry = reg.get(sid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, f"unknown system: {sid}") from exc

    cred = entry.sap_credential
    if not cred.is_set():
        raise HTTPException(400, (
            f"System {sid} has no SAP username/password stored. Add them on the Systems "
            "page — the RFC checks reuse the same credential as WebGUI login."))
    try:
        password = crypto.decrypt(cred.password_enc)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"could not decrypt the stored SAP password for {sid}: {exc}")
    try:
        return RfcSoapClient.from_system(entry.webgui_url, client=entry.client,
                                          username=cred.username, password=password)
    except RfcSoapError as exc:
        raise HTTPException(400, exc.message) from exc


class SidRequest(BaseModel):
    sid: str


class RunRfcRequest(BaseModel):
    sid: str
    check_ids: Optional[list[str]] = None


@router.get("/checks")
def list_checks() -> list[dict]:
    return describe_checks()


@router.post("/test-connection")
def test_connection(req: SidRequest) -> dict:
    """Deliberately returns 200 with ok:false (rather than an HTTP error)
    for SAP-side problems, so the UI can render the actionable message —
    'activate /sap/bc/soap/rfc in SICF', 'user lacks S_RFC' — instead of a
    bare red failure the operator can't act on."""
    client = _client_for(req.sid)
    try:
        info = client.ping()
    except RfcSoapError as exc:
        return {"ok": False, "sid": req.sid, "endpoint": client.target.soap_url,
                "error": exc.message, "error_kind": exc.kind}
    sysinfo = info.get("RFCSI_EXPORT") or {}
    if isinstance(sysinfo, list):
        sysinfo = sysinfo[0] if sysinfo else {}
    return {
        "ok": True, "sid": req.sid, "endpoint": client.target.soap_url,
        "system": sysinfo.get("RFCSYSID", ""), "host": sysinfo.get("RFCHOST", ""),
        "release": sysinfo.get("RFCSAPRL", ""), "database": sysinfo.get("RFCDBSYS", ""),
        "kernel": sysinfo.get("RFCKERNRL", ""), "os": sysinfo.get("RFCOPSYS", ""),
    }


@router.post("/run")
def run_checks(req: RunRfcRequest) -> dict:
    client = _client_for(req.sid)
    results = [r.as_dict() for r in run_all(client, req.check_ids)]
    return {
        "sid": req.sid,
        "endpoint": client.target.soap_url,
        "checks": results,
        "ok": all(r["ok"] for r in results),
        "anomaly_count": sum(len(r["anomalies"]) for r in results),
        "failed_checks": [r["check_id"] for r in results if not r["ok"]],
    }
