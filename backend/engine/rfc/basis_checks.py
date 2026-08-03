"""D-045: BASIS monitoring checks expressed as RFC calls.

Deliberately DECLARATIVE: each check is a small object saying which
function module to call, with which parameters, and how to turn the reply
into rows + anomalies. Adding a new check — including one of your own
custom Z_* remote-enabled FMs — is a few lines here, not new plumbing.

Confidence labelling is intentional and honest. `verified` marks calls
whose interface is documented/standard and that I'm confident about;
`unverified` marks ones that vary by release or are less universally
remote-enabled. Nothing here has been run against Giri's live system yet
— the per-check error reporting exists precisely so the first real run
tells us exactly which ones work rather than failing as one opaque blob.

Design rule (D-044 lesson): a check that fails must SAY so, loudly, with
the underlying SAP error. Never return an empty list that reads like a
clean bill of health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .soap_client import RfcSoapClient, RfcSoapError


@dataclass
class CheckResult:
    check_id: str
    title: str
    ok: bool
    rows: list[dict] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    error_kind: str = ""
    function: str = ""

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id, "title": self.title, "ok": self.ok,
            "rows": self.rows, "anomalies": self.anomalies, "summary": self.summary,
            "error": self.error, "error_kind": self.error_kind, "function": self.function,
        }


@dataclass
class RfcCheck:
    check_id: str
    title: str
    tcode: str                    # the transaction a human would use for the same thing
    function: str
    params: dict = field(default_factory=dict)
    interpret: Optional[Callable[[dict], tuple[list[dict], list[str], str]]] = None
    confidence: str = "verified"  # verified | unverified
    note: str = ""

    def run(self, client: RfcSoapClient) -> CheckResult:
        try:
            reply = client.call(self.function, **self.params)
        except RfcSoapError as exc:
            return CheckResult(self.check_id, self.title, ok=False, error=exc.message,
                                error_kind=exc.kind, function=self.function)
        except Exception as exc:  # noqa: BLE001 - one bad check never kills the sweep
            return CheckResult(self.check_id, self.title, ok=False,
                                error=f"unexpected error: {exc!r}", error_kind="error",
                                function=self.function)
        try:
            rows, anomalies, summary = (self.interpret(reply) if self.interpret
                                         else (_generic_rows(reply), [], ""))
        except Exception as exc:  # noqa: BLE001
            return CheckResult(self.check_id, self.title, ok=False,
                                error=f"could not interpret the reply: {exc!r}",
                                error_kind="parse", function=self.function)
        return CheckResult(self.check_id, self.title, ok=True, rows=rows,
                            anomalies=anomalies, summary=summary, function=self.function)


def _as_rows(value: Any) -> list[dict]:
    """SOAP gives a single-row table as one dict, not a list — normalize so
    interpreters never have to care."""
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v if isinstance(v, dict) else {"value": v} for v in value]
    return [{"value": value}]


def _generic_rows(reply: dict) -> list[dict]:
    """Fallback for checks with no interpreter (e.g. a custom Z FM someone
    just added): surface the biggest table in the reply, else the reply."""
    best: list[dict] = []
    for value in reply.values():
        rows = _as_rows(value) if isinstance(value, (list, dict)) else []
        if len(rows) > len(best):
            best = rows
    return best or [reply]


# ── Interpreters ─────────────────────────────────────────────────────────

def _interpret_system_info(reply: dict) -> tuple[list[dict], list[str], str]:
    info = reply.get("RFCSI_EXPORT") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    row = {
        "System": info.get("RFCSYSID", ""), "Host": info.get("RFCHOST", ""),
        "Database": info.get("RFCDBSYS", ""), "DB host": info.get("RFCDBHOST", ""),
        "Release": info.get("RFCSAPRL", ""), "Kernel": info.get("RFCKERNRL", ""),
        "OS": info.get("RFCOPSYS", ""), "Timezone": info.get("RFCTZONE", ""),
    }
    return [row], [], f"{row['System']} on {row['Host']} ({row['Database']}, rel {row['Release']})"


def _interpret_wpinfo(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM50 — work processes. Anomalies: stopped/ended processes, and any
    long-running or PRIV-mode process (classic memory-leak symptom)."""
    rows = _as_rows(reply.get("WPLIST"))
    out, anomalies = [], []
    for r in rows:
        status = (r.get("WP_STATUS") or "").strip()
        wp_type = (r.get("WP_TYP") or "").strip()
        reason = (r.get("WP_WAITING") or r.get("WP_REASON") or "").strip()
        elapsed = (r.get("WP_ELTIME") or "").strip()
        out.append({
            "No": (r.get("WP_INDEX") or "").strip(), "Type": wp_type,
            "PID": (r.get("WP_PID") or "").strip(), "Status": status,
            "Reason": reason, "Runtime": elapsed,
            "User": (r.get("WP_BNAME") or "").strip(),
            "Report": (r.get("WP_REPORT") or "").strip(),
            "Action": (r.get("WP_ACTION") or "").strip(),
        })
        if status.lower() in ("stopped", "ended", "shutdown"):
            anomalies.append(f"WP {r.get('WP_INDEX','?')} ({wp_type}) is {status}"
                              + (f" — reason: {reason}" if reason else ""))
        if (r.get("WP_IPRIV") or "").strip() in ("X", "yes", "Yes"):
            anomalies.append(f"WP {r.get('WP_INDEX','?')} ({wp_type}) is running in PRIV mode")
    running = sum(1 for r in out if r["Status"].lower() == "running")
    return out, anomalies, f"{len(out)} work processes, {running} running"


def _interpret_server_list(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM51 — active application servers."""
    rows = _as_rows(reply.get("SERVER_LIST") or reply.get("SERVERS"))
    out = [{
        "Server": (r.get("NAME") or r.get("SERVERNAME") or "").strip(),
        "Host": (r.get("HOST") or "").strip(),
        "Services": (r.get("SERVTYPE") or r.get("SERVICES") or "").strip(),
    } for r in rows]
    return out, [], f"{len(out)} application server(s) active"


def _interpret_xbp_jobs(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM37 — background jobs. Anomalies: cancelled (status A) jobs, which
    is exactly the check the SM37 WebGUI recording was built for."""
    rows = _as_rows(reply.get("JOB_HEAD") or reply.get("SELECTED_JOBS"))
    out, anomalies = [], []
    status_names = {"A": "Cancelled", "F": "Finished", "R": "Running",
                     "S": "Released", "P": "Scheduled", "Y": "Ready"}
    for r in rows:
        raw = (r.get("STATUS") or "").strip().upper()
        name = (r.get("JOBNAME") or "").strip()
        out.append({
            "Job": name, "Status": status_names.get(raw, raw or "?"),
            "User": (r.get("SDLUNAME") or r.get("AUTHCKNAM") or "").strip(),
            "Start date": (r.get("STRTDATE") or "").strip(),
            "Start time": (r.get("STRTTIME") or "").strip(),
        })
        if raw == "A":
            anomalies.append(f"Job {name} is CANCELLED")
    return out, anomalies, f"{len(out)} job(s); {len(anomalies)} cancelled"


def _interpret_read_table(reply: dict) -> tuple[list[dict], list[str], str]:
    """RFC_READ_TABLE returns fixed-width rows in DATA/WA plus a FIELDS
    description — split the rows back into named columns."""
    fields = _as_rows(reply.get("FIELDS"))
    data = _as_rows(reply.get("DATA"))
    cols = [(f.get("FIELDNAME", "").strip(), int(f.get("OFFSET", 0) or 0),
             int(f.get("LENGTH", 0) or 0)) for f in fields]
    out = []
    for d in data:
        line = d.get("WA", "") if isinstance(d, dict) else str(d)
        out.append({name: line[off:off + ln].strip() for name, off, ln in cols})
    return out, [], f"{len(out)} row(s)"


# ── The registry ─────────────────────────────────────────────────────────

CHECKS: list[RfcCheck] = [
    RfcCheck(
        check_id="system_info", title="System identity", tcode="—",
        function="RFC_SYSTEM_INFO", interpret=_interpret_system_info,
        confidence="verified",
        note="Present on every ABAP system, no parameters — also used as the connection test.",
    ),
    RfcCheck(
        check_id="work_processes", title="Work processes", tcode="SM50",
        function="TH_WPINFO", params={"SRVNAME": ""}, interpret=_interpret_wpinfo,
        confidence="unverified",
        note="TH_WPINFO is the standard WP-list FM; field names can vary slightly by release.",
    ),
    RfcCheck(
        check_id="server_list", title="Active servers", tcode="SM51",
        function="TH_SERVER_LIST", interpret=_interpret_server_list,
        confidence="unverified",
        note="Returns the instance list; table parameter name differs across releases "
             "(SERVER_LIST vs SERVERS) — both are handled.",
    ),
    RfcCheck(
        check_id="short_dumps", title="ABAP short dumps (today)", tcode="ST22",
        function="RFC_READ_TABLE",
        params={"QUERY_TABLE": "SNAP", "DELIMITER": "",
                "FIELDS": [{"FIELDNAME": "DATUM"}, {"FIELDNAME": "UZEIT"},
                            {"FIELDNAME": "UNAME"}, {"FIELDNAME": "AHOST"}],
                "ROWCOUNT": 200},
        interpret=_interpret_read_table, confidence="unverified",
        note="RFC_READ_TABLE is universally available but often restricted by S_TABU_* "
             "authorization; SNAP can also be large, hence ROWCOUNT.",
    ),
]

# SM37 needs an XMI session opened first, so it's handled as a sequence
# rather than a single call — see run_job_check().
XBP_INTERFACE = ("BAPI_XMI_LOGON", "BAPI_XBP_JOB_SELECT", "BAPI_XMI_LOGOFF")


def run_job_check(client: RfcSoapClient, max_jobs: int = 200) -> CheckResult:
    """SM37 via the XBP interface. XBP mandates an XMI logon/logoff around
    the actual call (documented in SAP's BC-XBP interface spec), which is
    why this can't be a plain declarative entry in CHECKS."""
    check_id, title = "background_jobs", "Background jobs"
    try:
        client.call("BAPI_XMI_LOGON", EXTCOMPANY="CLAUDEMON", EXTPRODUCT="SAPSENTINEL",
                     INTERFACE="XBP", VERSION="3.0")
    except RfcSoapError as exc:
        return CheckResult(check_id, title, ok=False,
                            error=f"XBP logon failed: {exc.message}",
                            error_kind=exc.kind, function="BAPI_XMI_LOGON")
    try:
        reply = client.call("BAPI_XBP_JOB_SELECT",
                             JOB_SELECT_PARAM={"JOBNAME": "*", "USERNAME": "*",
                                                "ABORTED": "X", "FINISHED": "X",
                                                "RUNNING": "X", "READY": "X",
                                                "SCHEDUL": "X", "RELEASED": "X"},
                             MAX_SELECT=max_jobs)
        rows, anomalies, summary = _interpret_xbp_jobs(reply)
        return CheckResult(check_id, title, ok=True, rows=rows, anomalies=anomalies,
                            summary=summary, function="BAPI_XBP_JOB_SELECT")
    except RfcSoapError as exc:
        return CheckResult(check_id, title, ok=False, error=exc.message,
                            error_kind=exc.kind, function="BAPI_XBP_JOB_SELECT")
    finally:
        try:
            client.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")
        except Exception:  # noqa: BLE001 - never mask the real result with a logoff failure
            pass


def run_all(client: RfcSoapClient, check_ids: Optional[list[str]] = None) -> list[CheckResult]:
    """Run every check (or a chosen subset). One check failing never stops
    the others — the D-040 per-flow isolation lesson, applied here."""
    results: list[CheckResult] = []
    for check in CHECKS:
        if check_ids and check.check_id not in check_ids:
            continue
        results.append(check.run(client))
    if not check_ids or "background_jobs" in check_ids:
        results.append(run_job_check(client))
    return results


def describe_checks() -> list[dict]:
    out = [{"check_id": c.check_id, "title": c.title, "tcode": c.tcode,
            "function": c.function, "confidence": c.confidence, "note": c.note}
           for c in CHECKS]
    out.append({"check_id": "background_jobs", "title": "Background jobs", "tcode": "SM37",
                "function": "BAPI_XBP_JOB_SELECT", "confidence": "verified",
                "note": "Uses the documented XBP external interface; needs an XMI "
                        "logon/logoff around the call."})
    return out
