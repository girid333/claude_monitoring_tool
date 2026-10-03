"""SOAP-RFC access for the AHC collectors (D-051).

Thin layer over engine.rfc.soap_client.RfcSoapClient with the things every
collector needs and that bit us during the F4H probe:

  - RFC_READ_TABLE with a DELIMITER. Fixed-width unpacking by OFFSET
    misaligned columns on F4H (RZLLITAB, TSP01), so rows are split on "|".
  - WHERE clauses wrapped on token boundaries into 72-char OPTIONS lines.
  - Namespaced function modules (/SDF/...) encoded the way SAP's SOAP
    handler expects ("/" -> "_-"); the raw name is "Document Format Error".
  - Every call is read-only by construction: collectors only use the
    function modules listed in READ_ONLY_FMS — plus, only through ahc/xbp.py,
    the XBP job modules in JOB_FMS (run one allowlisted report, read its list,
    delete the job).
"""
from __future__ import annotations

from typing import Any, Optional

from engine.rfc.soap_client import RfcSoapClient, RfcSoapError

READ_ONLY_FMS = {
    "RFC_SYSTEM_INFO", "RFC_READ_TABLE", "TH_SERVER_LIST", "TH_WPINFO", "TH_USER_LIST",
    "TH_GET_PARAMETER", "ENQUEUE_READ", "RSLG_READ_SLG", "SAPTUNE_GET_SUMMARY_STATISTIC",
    "SAPTUNE_BUFFER_NAMES", "SAPTUNE_BUFFER_STATISTICS", "GET_CPU_ALL", "GET_MEM_ALL",
    "SWNC_COLLECTOR_GET_AGGREGATES", "GWY_READ_CONNECTIONS", "RZL_READ_DIR_LOCAL",
    "SSFR_GET_ALL_STRUST_IDENTITIES", "SSFR_GET_OWNCERTIFICATE", "SSFR_GET_CERTIFICATELIST",
    "/SDF/SLIC_READ_LICENSES",
    # Executes ONE pre-defined SM69 command (the hdbsql wrapper a BASIS admin
    # created). It cannot run anything that command does not allow.
    "SXPG_COMMAND_EXECUTE",
}


# SM59's connection test: opens a connection to the destination, changes
# nothing. Used by the RFC_Con check with a per-call deadline.
TEST_FMS = {"DEST_CHECK_CONNECTION"}

# Not read-only: they create, run and delete ONE background job for an
# allowlisted report (ahc/xbp.py ALLOWED_REPORTS). AHC_NO_JOBS=1 disables them.
JOB_FMS = {
    "BAPI_XMI_LOGON", "BAPI_XMI_LOGOFF", "BAPI_XBP_JOB_OPEN", "BAPI_XBP_JOB_ADD_ABAP_STEP",
    "BAPI_XBP_JOB_CLOSE", "BAPI_XBP_JOB_START_IMMEDIATELY", "BAPI_XBP_JOB_STATUS_GET",
    "BAPI_XBP_JOB_SPOOLLIST_READ", "BAPI_XBP_JOB_JOBLOG_READ", "BAPI_XBP_JOB_DELETE",
    "BAPI_XBP_JOB_HEADER_MODIFY",          # D-052c: periodic start of the SMON daily scheduler job
}


def as_rows(value: Any) -> list[dict]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v if isinstance(v, dict) else {"value": v} for v in value]
    return []


def option_lines(where: str, width: int = 72) -> list[str]:
    lines, cur = [], ""
    for tok in where.split():
        if cur and len(cur) + 1 + len(tok) > width:
            lines.append(cur)
            cur = tok
        else:
            cur = f"{cur} {tok}" if cur else tok
    return lines + ([cur] if cur else [])


class Sap:
    def __init__(self, client: RfcSoapClient):
        self.c = client
        self.calls: list[str] = []

    def call(self, fm: str, _timeout_s: Optional[float] = None, **params: Any) -> dict:
        if fm not in READ_ONLY_FMS and fm not in JOB_FMS and fm not in TEST_FMS:
            raise RfcSoapError(f"{fm} is not on the read-only list", kind="refused", function=fm)
        self.calls.append(fm)
        if _timeout_s is not None:
            return self.c.call(fm.replace("/", "_-"), _timeout_s=_timeout_s, **params)
        return self.c.call(fm.replace("/", "_-"), **params)

    def fields(self, table: str) -> list[str]:
        r = self.call("RFC_READ_TABLE", QUERY_TABLE=table, DELIMITER="|", NO_DATA="X",
                      ROWCOUNT=1, FIELDS=[], DATA=[], OPTIONS=[])
        return [f.get("FIELDNAME", "").strip() for f in as_rows(r.get("FIELDS"))]

    def read(self, table: str, fields: list[str], where: str = "",
             rowcount: int = 5000, keep_missing: bool = True) -> list[dict]:
        """Rows as dicts. Fields this release doesn't have are dropped from the
        request (and, with keep_missing, returned as "") rather than failing."""
        have = set(self.fields(table))
        want = [f for f in fields if f in have]
        r = self.call("RFC_READ_TABLE", QUERY_TABLE=table, DELIMITER="|", ROWCOUNT=rowcount,
                      FIELDS=[{"FIELDNAME": f} for f in want], DATA=[],
                      OPTIONS=[{"TEXT": t} for t in option_lines(where)])
        fdefs = as_rows(r.get("FIELDS"))
        names = [f.get("FIELDNAME", "").strip() for f in fdefs]
        # A "|" inside a value splits it: give the surplus pieces back to the widest text field (the one
        # free text lives in — MTEXT, JOBNAME, RQTITLE…). OFFSETs can't be used: F4H returns numeric
        # fields narrower than their LENGTH (TSP01-RQIDENT), so positions drift.
        lens = [int(f.get("LENGTH") or 0) if (f.get("TYPE") or "C").strip() in ("C", "") else -1 for f in fdefs]
        widest = max(range(len(names)), key=lambda i: lens[i]) if names else 0
        out = []
        for d in as_rows(r.get("DATA")):
            vals = (d.get("WA", "") if isinstance(d, dict) else str(d)).split("|")
            extra = len(vals) - len(names)
            if extra > 0 and names:
                vals = vals[:widest] + ["|".join(vals[widest:widest + extra + 1])] + vals[widest + extra + 1:]
            row = {n: (vals[i].strip() if i < len(vals) else "") for i, n in enumerate(names)}
            if keep_missing:
                for f in fields:
                    row.setdefault(f, "")
            out.append(row)
        return out

    def sap_now(self, info: Optional[dict] = None):
        """SAP's local time. RFCTZONE is SY-TZONE — the UTC offset WITHOUT daylight saving; RFCDAYST
        (SY-DAYST) = 'X' while DST is on, which adds one hour (F4H, IST: no DST)."""
        import datetime as dt
        info = info if info is not None else self.system_info()
        tz = int(str(info.get("RFCTZONE") or "0").strip() or 0)
        if str(info.get("RFCDAYST") or "").strip().upper() == "X":
            tz += 3600
        return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=tz)

    def system_info(self) -> dict:
        info = self.call("RFC_SYSTEM_INFO").get("RFCSI_EXPORT") or {}
        return info[0] if isinstance(info, list) and info else info


def in_list(values: list[str]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


def first(d: Optional[dict], *keys: str, default: str = "") -> str:
    for k in keys:
        if d and d.get(k) not in (None, ""):
            return str(d[k])
    return default
