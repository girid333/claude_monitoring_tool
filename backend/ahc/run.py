"""Run a direct health check pull and produce SAP's AHC workbook (D-051).

    workbook_bytes, notes = pull(client, client_no)

The workbook is then imported through backend/health/ exactly like an
uploaded SAP export — the D-050 parser is the acceptance test.
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Optional

from engine.rfc.soap_client import RfcSoapClient

from .collect import Ctx, collect
from .hana import HanaViaSm69
from .sap import Sap
from .writer import write_workbook


def pull(client: RfcSoapClient, sap_client: str, now: Optional[dt.datetime] = None,
         hdbsql_command: Optional[str] = None,
         progress: Optional[Callable[[str], None]] = None) -> tuple[bytes, dict]:
    sap = Sap(client)
    info = sap.system_info()
    sid = (info.get("RFCSYSID") or "").strip() or "???"
    if now is None:
        # SAP's clock, not the container's: windows like "last 24 hours" must
        # line up with the system's own timestamps (F4H runs UTC+5:30 while the
        # container runs UTC — a 5.5 h skew silently emptied SM21).
        now = sap.sap_now(info)
    x = Ctx(sap=sap, sid=sid, client=sap_client, now=now)
    x.hana = HanaViaSm69(sap, hdbsql_command)
    tabs, verdicts = collect(x, progress)
    title = f"Technical Daily Health Check Report - {x.now:%d/%m/%Y}"
    data = write_workbook(title, verdicts, tabs)
    meta = {"sid": sid, "client": sap_client, "release": info.get("RFCSAPRL", ""),
            "notes": x.notes, "calls": len(sap.calls),
            "not_collected": sum(1 for v in verdicts if v.ok is None)}
    return data, meta
