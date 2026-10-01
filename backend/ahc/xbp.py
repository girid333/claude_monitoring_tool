"""Run an SAP report as a background job and read its list (D-051).

Some AHC checks have no remote-enabled read interface; their transaction is
just a report (SICK = RSICC000). SAP's standard external job interface XBP
(BAPI_XBP_*, remote-enabled) lets us run that report in batch and read the
spool list it prints, which is what the transaction would have shown.

This is the ONE thing the pull does that is not a pure read: it creates a
background job named SENTINEL_AHC_<check>, runs it, reads the list, then
deletes the job (and with it the spool). Set AHC_NO_JOBS=1 to switch it off;
the affected checks are then "Not collected".

Verified on F4H (2026-10-01): RSICC000 finishes in ~1 s and its spool is
exactly SICK's "SAP System Check" box. RSMONMS_ALV (SMMS) also runs, but it
has no selection screen and prints only the instance list in batch, not the
Services view the AHC tab shows — so it is not used.

Authorizations for the RFC user: S_XMI_PROD (XBP), S_BTCH_JOB (release own
jobs), S_PROGRAM / authorization group of the report.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from engine.rfc.soap_client import RfcSoapError

from .sap import JOB_FMS, Sap, as_rows  # noqa: F401  (JOB_FMS: the modules used here)

EXT_USER = "SENTINEL"
# Reports this module may run. Anything else is refused.
ALLOWED_REPORTS = {"RSICC000"}


class JobError(RfcSoapError):
    pass


def jobs_enabled() -> bool:
    return os.environ.get("AHC_NO_JOBS", "").strip() not in ("1", "true", "yes")


def _check(fm: str, r: dict) -> dict:
    ret = r.get("RETURN") or {}
    if isinstance(ret, dict) and ret.get("TYPE") in ("E", "A"):
        raise JobError(f"{fm}: {ret.get('MESSAGE', '')}".strip(), kind="fault", function=fm)
    return r


def run_report(sap: Sap, program: str, tag: str, timeout_s: float = 60.0,
               sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Run `program` as job SENTINEL_AHC_<tag>; return its spool list lines."""
    if program not in ALLOWED_REPORTS:
        raise ValueError(f"report {program} is not on the AHC job allowlist")
    job = f"SENTINEL_AHC_{tag}"[:32]
    call = lambda fm, **kw: _check(fm, sap.call(fm, **kw))  # noqa: E731
    key = dict(JOBNAME=job, EXTERNAL_USER_NAME=EXT_USER)
    call("BAPI_XMI_LOGON", EXTCOMPANY="SENTINEL", EXTPRODUCT="AHC", INTERFACE="XBP", VERSION="3.0")
    count: Optional[str] = None
    try:
        count = (call("BAPI_XBP_JOB_OPEN", **key).get("JOBCOUNT") or "").strip()
        k = dict(key, JOBCOUNT=count)
        step = call("BAPI_XBP_JOB_ADD_ABAP_STEP", ABAP_PROGRAM_NAME=program, **k).get("STEP_NUMBER") or 1
        call("BAPI_XBP_JOB_CLOSE", **k)
        call("BAPI_XBP_JOB_START_IMMEDIATELY", **k)
        status, waited = "", 0.0
        while waited < timeout_s:
            sleep(1.0)
            waited += 1.0
            status = (call("BAPI_XBP_JOB_STATUS_GET", **k).get("STATUS") or "").strip()
            if status in ("F", "A"):
                break
        if status != "F":
            log = ""
            if status == "A":
                try:
                    log = " ".join((m.get("TEXT") or "").strip() for m in
                                   as_rows(sap.call("BAPI_XBP_JOB_JOBLOG_READ", JOB_PROTOCOL=[], **k)
                                           .get("JOB_PROTOCOL")))[:200]
                except RfcSoapError:
                    pass
            raise JobError(f"job {job} {'was cancelled' if status == 'A' else f'did not finish in {timeout_s:.0f} s'}"
                           + (f": {log}" if log else ""), kind="fault", function="BAPI_XBP_JOB_STATUS_GET")
        r = call("BAPI_XBP_JOB_SPOOLLIST_READ", STEP_NUMBER=int(step), SPOOL_LIST=[], **k)
        return [(row.get("LINE") or "").rstrip() for row in as_rows(r.get("SPOOL_LIST"))]
    finally:
        if count:
            try:
                sap.call("BAPI_XBP_JOB_DELETE", JOBNAME=job, JOBCOUNT=count, EXTERNAL_USER_NAME=EXT_USER)
            except RfcSoapError:
                pass        # a job that is still running cannot be deleted; SAP's reorg job removes it
        try:
            sap.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")
        except RfcSoapError:
            pass
