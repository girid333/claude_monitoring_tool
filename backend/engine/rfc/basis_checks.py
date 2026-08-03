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
    suspicious: str = ""   # succeeded, but the result looks wrong (see RfcCheck.expects_rows)

    def as_dict(self) -> dict:
        return {
            "check_id": self.check_id, "title": self.title, "ok": self.ok,
            "rows": self.rows, "anomalies": self.anomalies, "summary": self.summary,
            "error": self.error, "error_kind": self.error_kind, "function": self.function,
            "suspicious": self.suspicious,
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
    # A healthy system ALWAYS has work processes, servers and jobs. If such a
    # check returns nothing, that is a parsing/permission problem masquerading
    # as success — the exact failure the first live F4H run produced. Flag it
    # instead of rendering a reassuring empty table.
    expects_rows: bool = True
    # TABLES parameters to declare (empty) in the request envelope.
    #
    # THE ROOT CAUSE of the second live F4H sweep, where every table-returning
    # check came back with zero rows while RFC_SYSTEM_INFO — the only one
    # returning an EXPORT structure rather than a TABLE — returned perfect
    # data. SAP's SOAP RFC handler only sends back a TABLES parameter that the
    # CALLER declared in the request. Omit it and you get a successful,
    # completely empty response. Most tellingly, RFC_READ_TABLE was being sent
    # FIELDS but never DATA — and DATA is the table that holds the rows.
    tables: list[str] = field(default_factory=list)
    # Alternative TABLES names to try when `tables` yields nothing. SAP renames
    # export tables between releases (TH_SERVER_LIST returns SERVER_LIST on some
    # systems and something else on others), and since an undeclared table is
    # simply absent from the reply, no amount of clever parsing can recover it —
    # the NAME has to be right in the REQUEST. Trying a short candidate list
    # makes the check self-heal across releases instead of costing a
    # diagnose-and-patch round trip for every system.
    table_candidates: list[list[str]] = field(default_factory=list)
    # Optional callable producing extra params at call time (e.g. a date window
    # that has to be "today", which a static dict cannot express).
    dynamic_params: Optional[Callable[[], dict]] = None

    def call_params(self, tables: Optional[list[str]] = None) -> dict:
        """Params plus an empty element for each declared TABLES parameter,
        without which SAP simply won't return that table (see `tables`)."""
        merged = dict(self.params)
        if self.dynamic_params:
            merged.update(self.dynamic_params())
        for name in (self.tables if tables is None else tables):
            merged.setdefault(name, [])
        return merged

    def run(self, client: RfcSoapClient) -> CheckResult:
        attempts = [self.tables] + list(self.table_candidates)
        reply: dict = {}
        last_error: Optional[RfcSoapError] = None
        tried: list[str] = []
        for table_set in attempts:
            try:
                reply = client.call(self.function, **self.call_params(table_set))
            except RfcSoapError as exc:
                last_error = exc
                tried.append("+".join(table_set) or "(none)")
                continue
            except Exception as exc:  # noqa: BLE001 - one bad check never kills the sweep
                return CheckResult(self.check_id, self.title, ok=False,
                                    error=f"unexpected error: {exc!r}", error_kind="error",
                                    function=self.function)
            last_error = None
            tried.append("+".join(table_set) or "(none)")
            # Good enough if ANY table came back with content.
            if any(isinstance(v, list) and v for v in reply.values()):
                break
        if last_error is not None and not reply:
            return CheckResult(self.check_id, self.title, ok=False, error=last_error.message,
                                error_kind=last_error.kind, function=self.function)
        self._tried_tables = tried
        try:
            rows, anomalies, summary = (self.interpret(reply) if self.interpret
                                         else (_generic_rows(reply), [], ""))
        except Exception as exc:  # noqa: BLE001
            return CheckResult(self.check_id, self.title, ok=False,
                                error=f"could not interpret the reply: {exc!r}",
                                error_kind="parse", function=self.function)
        suspicious = ""
        if self.expects_rows and not rows:
            attempted = ", ".join(getattr(self, "_tried_tables", []) or ["(none)"])
            suspicious = (
                f"{self.function} returned no rows. A live system should always report "
                f"something here, so this is most likely the TABLES parameter having a "
                f"different name on this release, or missing authorization — not a "
                f"genuinely empty result. Declared table names tried: {attempted}. "
                f"Use POST /api/rfc/raw with function={self.function} (and a 'tables' "
                f"list) to see exactly what SAP returns.")
        return CheckResult(self.check_id, self.title, ok=True, rows=rows,
                            anomalies=anomalies, summary=summary, function=self.function,
                            suspicious=suspicious)


def _dedupe_anomalies(items: list[str], limit: int = 25) -> list[str]:
    """Collapse repeats into counts.

    The first useful live sweep produced 75 anomaly lines, ~50 of them the
    identical "Job SRSM_AMA_SELFMON_HEARTBEAT is CANCELLED". That is one
    finding repeated, not fifty findings, and rendering it verbatim buries
    the genuinely distinct problems (SAP_LMDB_NOTIFY_DISPATCHER, the
    SELFMON_CONFIG/ERRORS jobs) in noise. Counting repeats keeps the signal
    and preserves the magnitude."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    out = [(f"{msg}  (×{n})" if n > 1 else msg) for msg, n in
           sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if len(out) > limit:
        hidden = len(out) - limit
        out = out[:limit] + [f"… and {hidden} more distinct anomal{'y' if hidden == 1 else 'ies'}"]
    return out


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


def _first_table(reply: dict, *preferred_names: str) -> list[dict]:
    """Find a check's data table by NAME first, then fall back to the
    largest table-shaped value anywhere in the reply.

    Rationale (learned from the first live run on F4H, release 758):
    export/table parameter names differ between releases, so hard-coding
    one name made TH_WPINFO and TH_SERVER_LIST report "succeeded, 0 rows"
    on a system that obviously has work processes and servers. Falling
    back to "whatever table actually came back" makes the check work
    across releases instead of silently returning nothing."""
    for name in preferred_names:
        if name in reply:
            rows = _as_rows(reply[name])
            if rows:
                return rows
    best: list[dict] = []
    for key, value in reply.items():
        if key.upper() in ("RETURN", "RFCSI_EXPORT"):   # status blocks, not data
            continue
        rows = _as_rows(value) if isinstance(value, list) else []
        if len(rows) > len(best):
            best = rows
    return best


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
    rows = _first_table(reply, "WPLIST", "WP_TABLE", "WPTAB")
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
    return out, _dedupe_anomalies(anomalies), f"{len(out)} work processes, {running} running"


def _interpret_server_list(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM51 — active application servers."""
    rows = _first_table(reply, "SERVER_LIST", "SERVERS", "SERVER_TAB", "LIST")
    out = [{
        "Server": (r.get("NAME") or r.get("SERVERNAME") or "").strip(),
        "Host": (r.get("HOST") or "").strip(),
        "Services": (r.get("SERVTYPE") or r.get("SERVICES") or "").strip(),
    } for r in rows]
    return out, [], f"{len(out)} application server(s) active"


def _interpret_xbp_jobs(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM37 — background jobs. Anomalies: cancelled (status A) jobs, which
    is exactly the check the SM37 WebGUI recording was built for."""
    rows = _first_table(reply, "SELECTED_JOBS", "JOB_HEAD", "JOBLIST")
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
    cancelled = len(anomalies)
    return out, _dedupe_anomalies(anomalies), f"{len(out)} job(s); {cancelled} cancelled"


JOB_WINDOW_DAYS = 2


def _job_window_params() -> dict:
    """Restrict TBTCO to a recent window.

    Without this the read returns whatever 200 rows the database hands back
    — on the live system that meant jobs from five days earlier, which is
    not "current state" and is useless for monitoring. OPTIONS is itself a
    TABLES parameter, so it must be declared like any other."""
    from datetime import date, timedelta
    since = (date.today() - timedelta(days=JOB_WINDOW_DAYS)).strftime("%Y%m%d")
    return {"OPTIONS": [{"TEXT": f"STRTDATE GE '{since}'"}]}


def _interpret_job_table(reply: dict) -> tuple[list[dict], list[str], str]:
    """SM37 straight from TBTCO (transparent), independent of XBP."""
    rows, _, _ = _interpret_read_table(reply)
    status_names = {"A": "Cancelled", "F": "Finished", "R": "Running",
                     "S": "Released", "P": "Scheduled", "Y": "Ready"}
    out, anomalies = [], []
    for r in rows:
        raw = (r.get("STATUS") or "").strip().upper()
        name = (r.get("JOBNAME") or "").strip()
        out.append({"Job": name, "Status": status_names.get(raw, raw or "?"),
                     "User": (r.get("SDLUNAME") or "").strip(),
                     "Start date": (r.get("STRTDATE") or "").strip(),
                     "Start time": (r.get("STRTTIME") or "").strip()})
        if raw == "A":
            anomalies.append(f"Job {name} is CANCELLED")
    # newest first — a monitoring view should lead with what just happened
    out.sort(key=lambda r: (r["Start date"], r["Start time"]), reverse=True)
    cancelled = len(anomalies)
    return (out, _dedupe_anomalies(anomalies),
            f"{len(out)} job(s) in the last {JOB_WINDOW_DAYS} day(s); {cancelled} cancelled")


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
        function="TH_WPINFO", params={"SRVNAME": ""}, tables=["WPLIST"],
        interpret=_interpret_wpinfo, confidence="unverified",
        note="TH_WPINFO is the standard WP-list FM. WPLIST must be declared in the "
             "request or SAP returns nothing at all.",
    ),
    RfcCheck(
        check_id="server_list", title="Active servers", tcode="SM51",
        function="TH_SERVER_LIST", tables=["SERVER_LIST"],
        # Confirmed empty on release 758 with SERVER_LIST, so the name differs
        # there; try the other documented spellings before giving up.
        table_candidates=[["SERVERS"], ["LIST"], ["SERVER_TAB"], ["TH_SERVER_LIST"]],
        interpret=_interpret_server_list, confidence="unverified",
        note="Returns the instance list. The export table name varies by release; "
             "several spellings are tried automatically. If all fail, /api/rfc/raw "
             "will show the real one.",
    ),
    # NOTE (live finding, F4H rel 758): the original ST22 check queried
    # SNAP and failed with TABLE_NOT_AVAILABLE. That is not a permissions
    # problem and not fixable by retrying — SNAP is a CLUSTER table, and
    # RFC_READ_TABLE can only read transparent (database-visible) tables.
    # Reading short dumps over RFC needs a different mechanism entirely
    # (see OQ-20); rather than ship a check that can never work, this slot
    # now holds a genuinely useful transparent-table check.
    RfcCheck(
        check_id="job_table", title=f"Background jobs (last {JOB_WINDOW_DAYS} days)",
        tcode="SM37",
        function="RFC_READ_TABLE",
        params={"QUERY_TABLE": "TBTCO", "DELIMITER": "",
                "FIELDS": [{"FIELDNAME": "JOBNAME"}, {"FIELDNAME": "SDLUNAME"},
                            {"FIELDNAME": "STATUS"}, {"FIELDNAME": "STRTDATE"},
                            {"FIELDNAME": "STRTTIME"}],
                "ROWCOUNT": 200},
        # DATA is the table holding the actual rows. Sending FIELDS without
        # DATA is exactly why this returned "0 job(s)" against the live system.
        tables=["DATA", "OPTIONS"], dynamic_params=_job_window_params,
        interpret=_interpret_job_table, confidence="verified",
        note="TBTCO is the transparent table behind SM37, so this works via "
             "RFC_READ_TABLE and doubles as a fallback if the XBP interface "
             "is unavailable. Restricted to recent jobs via an OPTIONS WHERE "
             "clause. Needs S_TABU_* authorization for TBTCO.",
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
                             MAX_SELECT=max_jobs,
                             # Declare the result table, or SAP returns nothing
                             # at all (see RfcCheck.tables).
                             JOB_HEAD=[], SELECTED_JOBS=[])
        rows, anomalies, summary = _interpret_xbp_jobs(reply)
        suspicious = ""
        if not rows:
            suspicious = (
                "XBP returned no jobs. Most likely the XMI session did not carry over "
                "to this call, or the selection parameters are too narrow for this "
                "release. The 'Background jobs (table read)' check reads TBTCO directly "
                "and does not depend on XBP — compare the two.")
        return CheckResult(check_id, title, ok=True, rows=rows, anomalies=anomalies,
                            summary=summary, function="BAPI_XBP_JOB_SELECT",
                            suspicious=suspicious)
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
