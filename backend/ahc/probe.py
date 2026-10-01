"""D-051 Phase 1 — which data source actually works for each health check?

Before building a single collector, find out — on the REAL system — where
every Automated Health Check (AHC) check can get its data from:

  1. RFC over SOAP (D-045): RFC_READ_TABLE on the tables the check needs,
     and a fixed allowlist of known read-only function modules.
  2. HANA SQL — only when a HANA monitoring user is configured
     (env HANA_<SID>_HOST / _PORT / _USER / _PASSWORD, D-018: never a file).
  3. WebGUI capture through the unmodified v1 Core BASIS engine (D-017), as
     the fallback for checks with no structured source (--webgui).

Output: check -> chosen source -> sample data -> confirmed yes/no, as a
Markdown table (stdout + <out>/ahc_probe.md) and JSON (<out>/ahc_probe.json).

Run it where F4H is reachable, e.g. inside the backend container:
    docker compose exec -w /data backend \
        python /app/ahc/probe.py --sid F4H --sample /data/ahc_sample.XLS --webgui
`--sample` reads the check list from SAP's own workbook (its Monitors tab),
so the probe covers exactly the checks that workbook has; without it a
built-in catalogue reconstructed from backend/health/ is used.

Safety (same principle as D-004 / D-049 recovery):
  - Only function modules on the ALLOWLIST below are ever called, and only
    after TFDIR confirms they are remote-enabled. Everything the D-047
    discovery search turns up is LISTED for review, never called — a
    guessed call to an unknown module could change something.
  - RFC_READ_TABLE is capped at a few rows; field lists are chosen to fit
    its 512-character line limit so a wide table never aborts the read.
  - HANA statements are fixed SELECTs with a row limit.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):          # executed as a script: python ahc/probe.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.rfc.soap_client import RfcSoapClient, RfcSoapError  # noqa: E402

READ_TABLE_WIDTH = 512   # RFC_READ_TABLE's WA line length
SAMPLE_ROWS = 3
SAMPLE_CHARS = 160


# ── Catalogue ─────────────────────────────────────────────────────────────

@dataclass
class Source:
    kind: str                 # rfc_table | rfc_fm | hana_sql | webgui
    target: str               # table / function module / SQL / T-code
    params: dict = field(default_factory=dict)   # rfc_fm: imports + TABLES to declare
    fields: list = field(default_factory=list)   # rfc_table: fields the check needs
    where: str = ""
    note: str = ""

    def label(self) -> str:
        return {
            "rfc_table": f"RFC_READ_TABLE {self.target}",
            "rfc_fm": f"RFC {self.target}",
            "hana_sql": "HANA SQL " + self.target.split(" FROM ")[-1].split()[0]
                        if " FROM " in self.target else f"HANA SQL {self.target[:40]}",
            "webgui": f"WebGUI {self.target} (v1 engine)",
        }[self.kind]


@dataclass
class CheckSpec:
    key: str
    task: str
    tcode: str
    sources: list
    discover: list = field(default_factory=list)  # TFDIR patterns — listed, never called
    note: str = ""


def T(table, fields=(), where="", note=""):
    return Source("rfc_table", table, fields=list(fields), where=where, note=note)


def F(fm, note="", **params):
    return Source("rfc_fm", fm, params=params, note=note)


def H(sql, note=""):
    return Source("hana_sql", sql, note=note)


def W(tcode, note=""):
    return Source("webgui", tcode, note=note)


# Function modules the probe may CALL. Read-only by nature; nothing else is
# ever invoked. TABLES parameters are declared as [] (D-045: undeclared
# tables come back absent).
ALLOWLIST = {
    "TH_SERVER_LIST", "TH_WPINFO", "TH_USER_LIST", "ENQUEUE_READ",
    "SWNC_COLLECTOR_GET_AGGREGATES", "GET_CPU_ALL", "GET_MEM_ALL",
    "SAPTUNE_GET_SUMMARY_STATISTIC", "GWY_READ_CONNECTIONS",
}

# key -> spec. Keys are matched from a sample workbook's Monitors rows by
# match_key(); the task/tcode here are only used without --sample.
CATALOGUE: dict[str, CheckSpec] = {s.key: s for s in [
    # Availability
    CheckSpec("SM51", "Application Servers", "SM51", [
        F("TH_SERVER_LIST", LIST=[]), W("SM51")]),
    CheckSpec("RZ03", "Server status and alerts", "RZ03", [
        F("TH_SERVER_LIST", LIST=[], note="server states only; RZ03 alerts are CCMS"),
        W("RZ03")], discover=["%ALERT%READ%"]),
    CheckSpec("RZ04", "Operation modes", "RZ04", [
        T("TPFBA"), T("TPFID"), W("RZ04")]),
    CheckSpec("SMLG", "Logon groups and load distribution", "SMLG", [
        T("RZLLITAB", ["CLASSNAME", "APPLSERVER", "GROUPTYPE"]), W("SMLG")]),
    CheckSpec("RZ12", "RFC server groups", "RZ12", [
        T("RZLLITAB", ["CLASSNAME", "APPLSERVER", "GROUPTYPE"], where="GROUPTYPE EQ 'S'"),
        W("RZ12")]),
    CheckSpec("SMMS", "Message server", "SMMS", [
        F("TH_SERVER_LIST", LIST=[], note="the message server's own server list"),
        W("SMMS")], discover=["MS_%"]),
    CheckSpec("SMGW", "Gateway monitor", "SMGW", [
        F("GWY_READ_CONNECTIONS"), W("SMGW")], discover=["GWY_%"]),
    CheckSpec("SICK", "Installation check", "SICK", [W("SICK")], discover=["%SICK%"]),
    CheckSpec("SCC4", "Client settings", "SCC4", [
        T("T000", ["MANDT", "MTEXT", "CCCATEGORY", "CCCORACTIV", "CCNOCLIIND"]), W("SCC4")]),
    # Performance
    CheckSpec("ST02", "Buffers (tune summary)", "ST02", [
        F("SAPTUNE_GET_SUMMARY_STATISTIC"), W("ST02")], discover=["SAPTUNE%"]),
    CheckSpec("ST06", "OS monitor", "ST06", [
        F("GET_CPU_ALL", TF_CPU_ALL=[]), F("GET_MEM_ALL", TF_MEM_ALL=[]), W("ST06")],
        discover=["GET_%_ALL"]),
    CheckSpec("ST03N", "Workload analysis", "ST03N", [
        F("SWNC_COLLECTOR_GET_AGGREGATES", COMPONENT="TOTAL", ASSIGNDSYS="{SID}",
          PERIODTYPE="D", PERIODSTRT="{YESTERDAY}", TASKTYPE=[], TIMES=[]),
        W("ST03N")], discover=["SWNC%GET%"]),
    CheckSpec("SM50", "Work processes", "SM50", [F("TH_WPINFO", WPLIST=[]), W("SM50")]),
    CheckSpec("SM66", "Global work processes", "SM66", [
        F("TH_WPINFO", WPLIST=[], note="local instance only; SM66 is system-wide"),
        W("SM66")], discover=["TH_%WP%"]),
    CheckSpec("DB01", "Blocked transactions", "DB01", [
        H("SELECT * FROM M_BLOCKED_TRANSACTIONS LIMIT 5"), W("DB01")]),
    # Jobs and output
    CheckSpec("SM37", "Background jobs", "SM37", [
        T("TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS", "SDLUNAME", "STRTDATE", "STRTTIME",
                    "ENDDATE", "ENDTIME"]), W("SM37")]),
    CheckSpec("SM13", "Update requests", "SM13", [
        T("VBHDR", ["VBKEY", "VBUSR", "VBDATE", "VBRC", "VBTCODE"]), W("SM13")]),
    CheckSpec("SP01", "Spool requests", "SP01", [
        T("TSP01", ["RQIDENT", "RQOWNER", "RQCRETIME", "RQDEST", "RQ2NAME"]), W("SP01")]),
    CheckSpec("SOST", "SAPconnect send requests", "SOST", [T("SOOS"), W("SOST")]),
    CheckSpec("SRT_MONI", "Web service messages", "SRT_MONI", [
        T("SRT_MMASTER"), W("SRT_MONI")], discover=["SRT_%MONI%"]),
    # Logs and dumps
    CheckSpec("SM21", "SAP System Log", "SM21", [W("SM21")],
              discover=["RSLG%", "%SYSLOG%"],
              note="the syslog is a file on each instance, not a table"),
    CheckSpec("ST22", "ABAP dumps", "ST22", [
        T("SNAP_BEG", ["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "FLIST"]), W("ST22")],
        discover=["%ST22%", "RS_ST22%"], note="SNAP itself is a cluster table (D-045)"),
    CheckSpec("ST11", "Developer traces", "ST11", [W("ST11")]),
    # Database (HANA)
    CheckSpec("DB12", "Backup catalog", "DB12", [
        H("SELECT ENTRY_TYPE_NAME, STATE_NAME, SYS_START_TIME, SYS_END_TIME "
          "FROM M_BACKUP_CATALOG ORDER BY SYS_START_TIME DESC LIMIT 5"), W("DB12")]),
    CheckSpec("DB02_ALERTS", "Current Alerts", "DB02", [
        H("SELECT ALERT_ID, ALERT_RATING, ALERT_DETAILS, ALERT_TIMESTAMP "
          "FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS LIMIT 5"), W("DB02")]),
    CheckSpec("DB02_LOG", "System Availability Log", "DB02", [
        H("SELECT * FROM _SYS_STATISTICS.GLOBAL_SYSTEM_AVAILABILITY LIMIT 5"),
        H("SELECT * FROM M_SYSTEM_AVAILABILITY LIMIT 5"), W("DB02")]),
    CheckSpec("DB02_MEMORY", "Memory and disk usage", "DB02", [
        H("SELECT HOST, USED_PHYSICAL_MEMORY, FREE_PHYSICAL_MEMORY, "
          "ALLOCATION_LIMIT FROM M_HOST_RESOURCE_UTILIZATION LIMIT 5"),
        H("SELECT HOST, USAGE_TYPE, TOTAL_SIZE, USED_SIZE FROM M_DISKS LIMIT 5"),
        W("DB02")], note="SAP labels this row DB12 but its tab is DB02_Memory (D-050)"),
    # Security
    CheckSpec("CERT", "Certificate validity", "Certificate Info", [
        W("STRUST", note="v1 has a bespoke STRUST handler (expiry dates)")],
        discover=["SSFR%", "%CERTIFICATE%"]),
    CheckSpec("SLICENSE", "SAP license", "SLICENSE", [W("SLICENSE")], discover=["SLIC%"]),
    CheckSpec("RSRFCCHK", "RFC connections", "RSRFCCHK", [
        T("RFCDES", ["RFCDEST", "RFCTYPE", "RFCOPTIONS"],
          note="configuration only — testing each destination is a separate step"),
        W("SM59")]),
    # Users and sessions
    CheckSpec("AL08", "Users logged on (all instances)", "AL08", [
        F("TH_USER_LIST", USRLIST=[], note="local instance only"), W("AL08")]),
    CheckSpec("SM04", "User sessions", "SM04", [F("TH_USER_LIST", USRLIST=[]), W("SM04")]),
    CheckSpec("SM12", "Lock entries", "SM12", [
        F("ENQUEUE_READ", GCLIENT="{CLIENT}", GNAME="", GUNAME="", ENQ=[]), W("SM12")]),
]}


def match_key(task: str, tcode: str) -> Optional[str]:
    """Map a Monitors row to a catalogue key, mirroring health/analyze.py's
    resolve_sheet() so probe and parser agree on which check is which."""
    t, k = (tcode or "").upper().strip(), (task or "").lower()
    if "certificate" in t.lower() or "certificate" in k:
        return "CERT"
    if "RSRFCCHK" in t or "rfc connection" in k:
        return "RSRFCCHK"
    if t.startswith(("DB02", "DB12")):
        if "alert" in k:
            return "DB02_ALERTS"
        if "availability" in k:
            return "DB02_LOG"
        if "memory" in k or "disk" in k:
            return "DB02_MEMORY"
    if t in CATALOGUE:
        return t
    for key in CATALOGUE:              # "SM37 " / "ST03N (Workload)" etc.
        if t.startswith(key):
            return key
    return None


def checks_from_sample(path: Path) -> list[CheckSpec]:
    """The checks exactly as SAP's own workbook lists them (Monitors tab)."""
    from health.analyze import cell, find_monitors
    from health.workbook import read_workbook
    sheets = read_workbook(path.read_bytes(), path.name)
    mon, hi = find_monitors(sheets)
    if not mon:
        raise SystemExit(f"{path}: no Monitors tab found (need a row with "
                         "'Transaction' and 'OK' headers).")
    hdr = [x.lower() for x in mon.rows[hi]]
    it = hdr.index("monitoring task") if "monitoring task" in hdr else None
    ix = hdr.index("transaction")
    out = []
    for r in mon.rows[hi + 1:]:
        task, tcode = cell(r, it), cell(r, ix)
        if not (task or tcode):
            continue
        key = match_key(task, tcode)
        base = CATALOGUE.get(key) if key else None
        if base:
            spec = CheckSpec(base.key, task or base.task, tcode, base.sources,
                             base.discover, base.note)
        else:
            spec = CheckSpec(f"UNMAPPED_{tcode or task}", task, tcode,
                             [W(tcode.split()[0])] if tcode else [],
                             note="no candidate source catalogued for this row")
        out.append(spec)
    return out


# ── Results ───────────────────────────────────────────────────────────────

@dataclass
class Attempt:
    source: str
    kind: str
    status: str            # ok | empty | failed | skipped
    rows: int = 0
    sample: str = ""
    detail: str = ""       # error text, missing fields, notes


@dataclass
class ProbeResult:
    key: str
    task: str
    tcode: str
    attempts: list = field(default_factory=list)
    chosen: str = ""
    chosen_kind: str = ""
    confirmed: bool = False
    sample: str = ""
    discovered: list = field(default_factory=list)
    note: str = ""


def _clip(s: str, n: int = SAMPLE_CHARS) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_rows(rows: list[dict]) -> str:
    parts = []
    for r in rows[:SAMPLE_ROWS]:
        parts.append("; ".join(f"{k}={v}" for k, v in r.items() if str(v).strip())[:90])
    return _clip(" | ".join(parts))


def _as_rows(value: Any) -> list[dict]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v if isinstance(v, dict) else {"value": v} for v in value]
    return []


def _option_lines(where: str, width: int = 72) -> list[str]:
    """RFC_READ_TABLE's OPTIONS lines are 72 chars and SAP builds the dynamic
    WHERE from them, so a line must never end mid-token: wrap on spaces."""
    lines, cur = [], ""
    for tok in where.split():
        if cur and len(cur) + 1 + len(tok) > width:
            lines.append(cur)
            cur = tok
        else:
            cur = f"{cur} {tok}" if cur else tok
    return lines + ([cur] if cur else [])


# ── RFC ───────────────────────────────────────────────────────────────────

class RfcProber:
    def __init__(self, client: RfcSoapClient, sid: str, sap_client: str):
        self.c = client
        self.subst = {"{SID}": sid, "{CLIENT}": sap_client,
                      "{YESTERDAY}": (dt.date.today() - dt.timedelta(days=1)).strftime("%Y%m%d")}
        self._remote: Optional[set] = None
        self._tfdir_error = ""
        self._cache: dict[str, Attempt] = {}
        self.called: list[str] = []            # every FM actually invoked (tests assert on it)

    def _call(self, fm: str, **params) -> dict:
        self.called.append(fm)
        return self.c.call(fm, **params)

    def _remote_enabled(self, fm: str) -> Optional[bool]:
        """TFDIR-FMODE='R' check for the allowlist, read once. None = unknown
        (TFDIR unreadable) — then the call itself is the test."""
        if self._remote is None:
            names = ", ".join(f"'{n}'" for n in sorted(ALLOWLIST))
            try:
                rows = self._read(("TFDIR"), ["FUNCNAME", "FMODE"],
                                  f"FUNCNAME IN ({names})", rowcount=len(ALLOWLIST) + 5)
                self._remote = {r["FUNCNAME"] for r in rows if r.get("FMODE") == "R"}
            except RfcSoapError as exc:
                self._remote, self._tfdir_error = set(), exc.message
        if self._tfdir_error:
            return None
        return fm in self._remote

    def _read(self, table: str, fields: list[str], where: str = "",
              rowcount: int = SAMPLE_ROWS, no_data: bool = False) -> Any:
        params: dict[str, Any] = {
            "QUERY_TABLE": table, "DELIMITER": "", "ROWCOUNT": rowcount,
            "FIELDS": [{"FIELDNAME": f} for f in fields], "DATA": [],
            "OPTIONS": [{"TEXT": line} for line in _option_lines(where)],
        }
        if no_data:
            params["NO_DATA"] = "X"
        reply = self._call("RFC_READ_TABLE", **params)
        if no_data:
            return reply
        cols = [(f.get("FIELDNAME", "").strip(), int(f.get("OFFSET", 0) or 0),
                 int(f.get("LENGTH", 0) or 0)) for f in _as_rows(reply.get("FIELDS"))]
        out = []
        for d in _as_rows(reply.get("DATA")):
            line = d.get("WA", "") if isinstance(d, dict) else str(d)
            out.append({n: line[o:o + ln].strip() for n, o, ln in cols})
        return out

    def table(self, src: Source) -> Attempt:
        label = src.label() + (f" WHERE {src.where}" if src.where else "")
        # 1) structure first: which fields exist, how wide are they?
        try:
            meta = self._read(src.target, [], no_data=True)
        except RfcSoapError as exc:
            return Attempt(label, src.kind, "failed", detail=_clip(exc.message, 220))
        avail = [(f.get("FIELDNAME", "").strip(), int(f.get("LENGTH", 0) or 0))
                 for f in _as_rows(meta.get("FIELDS"))]
        names = [n for n, _ in avail]
        missing = [f for f in src.fields if f not in names]
        wanted = [f for f in src.fields if f in names] or names
        chosen, width = [], 0
        for n, ln in avail:
            if n in wanted and width + ln <= READ_TABLE_WIDTH:
                chosen.append(n)
                width += ln
        notes = []
        if missing:
            notes.append("fields not in this release: " + ", ".join(missing))
        if len(chosen) < len(wanted):
            notes.append(f"{len(wanted) - len(chosen)} field(s) dropped to fit the "
                         f"{READ_TABLE_WIDTH}-char RFC_READ_TABLE line")
        # 2) a few real rows
        try:
            rows = self._read(src.target, chosen, src.where)
        except RfcSoapError as exc:
            return Attempt(label, src.kind, "failed",
                           detail=_clip("; ".join([exc.message, *notes]), 220))
        return Attempt(label, src.kind, "ok" if rows else "empty", rows=len(rows),
                       sample=_fmt_rows(rows) if rows else f"0 rows ({len(avail)} fields readable)",
                       detail="; ".join(notes))

    def fm(self, src: Source) -> Attempt:
        label = src.label()
        if src.target not in ALLOWLIST:          # belt and braces
            return Attempt(label, src.kind, "skipped", detail="not on the read-only allowlist")
        enabled = self._remote_enabled(src.target)
        if enabled is False:
            return Attempt(label, src.kind, "failed",
                           detail="not remote-enabled on this system (TFDIR); not called")
        params = {k: (self.subst.get(v, v) if isinstance(v, str) else v)
                  for k, v in src.params.items()}
        try:
            reply = self._call(src.target, **params)
        except RfcSoapError as exc:
            return Attempt(label, src.kind, "failed", detail=_clip(exc.message, 220))
        tables = {k: _as_rows(v) for k, v in reply.items() if isinstance(v, list)}
        declared = [k for k, v in src.params.items() if isinstance(v, list)]
        rows = max(tables.values(), key=len) if tables else []
        shape = ", ".join(f"{k}:{len(v)}" for k, v in tables.items()) or "no tables"
        scalars = [k for k, v in reply.items() if not isinstance(v, list)]
        notes = [f"reply tables {shape}"]
        if scalars:
            notes.append("exports " + ", ".join(scalars[:8]))
        if declared and not any(tables.get(d) for d in declared):
            notes.append("declared table(s) " + ", ".join(declared) + " came back empty")
        if enabled is None:
            notes.append(f"TFDIR unreadable ({_clip(self._tfdir_error, 60)}) — called directly")
        status = "ok" if rows else ("ok" if scalars and not declared else "empty")
        sample = _fmt_rows(rows) if rows else _clip(json.dumps(
            {k: reply[k] for k in scalars[:6]}, default=str))
        return Attempt(label, src.kind, status, rows=len(rows), sample=sample,
                       detail="; ".join(notes))

    def run(self, src: Source) -> Attempt:
        # Same source used by several checks (TH_SERVER_LIST, TH_USER_LIST):
        # call it once, report it everywhere.
        k = json.dumps([src.kind, src.target, src.where, src.params], sort_keys=True, default=str)
        if k not in self._cache:
            self._cache[k] = self.table(src) if src.kind == "rfc_table" else self.fm(src)
        a = self._cache[k]
        detail = a.detail
        if src.note and src.note not in detail:        # per-check caveat on a shared call
            detail = "; ".join(x for x in (detail, src.note) if x)
        return Attempt(**{**asdict(a), "detail": detail})

    def discover(self, patterns: list[str]) -> list[str]:
        from engine.apiflow.discovery import discover_rfc_functions
        found: list[str] = []
        for p in patterns:
            ok, fns, _ = discover_rfc_functions(self.c, p, limit=15)
            for f in fns:
                if f["name"] not in found:
                    found.append(f["name"] + (f" ({f['description']})" if f.get("description") else ""))
        return found


# ── HANA ──────────────────────────────────────────────────────────────────

class HanaProber:
    """Runs only when HANA_<SID>_HOST/_USER/_PASSWORD are in the environment
    (and _PORT, default 30015) and hdbcli is installed. Credentials are never
    read from or written to a file (D-018)."""

    def __init__(self, sid: str, env=os.environ):
        p = f"HANA_{sid.upper()}_"
        self.host, self.user = env.get(p + "HOST", ""), env.get(p + "USER", "")
        self.password, self.port = env.get(p + "PASSWORD", ""), int(env.get(p + "PORT", "30015") or 30015)
        self.skip_reason = ""
        self.conn = None
        if not (self.host and self.user and self.password):
            self.skip_reason = (f"no HANA monitoring user configured "
                                f"(set {p}HOST, {p}USER, {p}PASSWORD, optional {p}PORT)")
            return
        try:
            from hdbcli import dbapi  # type: ignore
        except ImportError:
            self.skip_reason = "hdbcli not installed (pip install hdbcli)"
            return
        try:
            self.conn = dbapi.connect(address=self.host, port=self.port,
                                      user=self.user, password=self.password)
        except Exception as exc:  # noqa: BLE001
            self.skip_reason = f"could not connect to {self.host}:{self.port}: {_clip(exc, 160)}"

    def run(self, src: Source) -> Attempt:
        if not self.conn:
            return Attempt(src.label(), src.kind, "skipped", detail=self.skip_reason)
        try:
            cur = self.conn.cursor()
            cur.execute(src.target)
            cols = [d[0] for d in cur.description or []]
            rows = [dict(zip(cols, r)) for r in cur.fetchmany(SAMPLE_ROWS)]
            cur.close()
        except Exception as exc:  # noqa: BLE001
            return Attempt(src.label(), src.kind, "failed", detail=_clip(exc, 220))
        return Attempt(src.label(), src.kind, "ok" if rows else "empty", rows=len(rows),
                       sample=_fmt_rows(rows) if rows else f"0 rows ({len(cols)} columns)",
                       detail=src.note)


# ── WebGUI (v1 engine, unmodified) ────────────────────────────────────────

async def webgui_capture(sid: str, tcodes: list[str], registry_path: str) -> dict[str, Attempt]:
    """Run the v1 Core BASIS engine for just these T-codes. v1 is not edited:
    its MONITORED_TCODES list is swapped IN MEMORY for this run (the same
    technique the e2e suites use to trim it) and restored afterwards. T-codes
    v1 has no bespoke handler for go through its generic screenshot path."""
    from engine.core_basis_wrap import load_v1_orchestrator
    from engine.sections import CoreBasisSection
    section = CoreBasisSection(registry_path=registry_path)
    orch = load_v1_orchestrator(section._resolve_v1_path())
    original = orch.MONITORED_TCODES
    known = {t["tcode"]: t for t in original}
    orch.MONITORED_TCODES = [known.get(t, {"tcode": t, "name": t, "check": "screenshot"})
                             for t in tcodes]
    try:
        sr = await section.run(engine=None, run_id="ahc_probe", sid=sid)
    finally:
        orch.MONITORED_TCODES = original
    out: dict[str, Attempt] = {}
    got = {t["tcode"]: t for t in (sr.v1_results or {}).get("tcodes", [])}
    for t in tcodes:
        label = f"WebGUI {t} (v1 engine)"
        r = got.get(t)
        if not r:
            out[t] = Attempt(label, "webgui", "failed",
                             detail=_clip("; ".join(sr.notes) or "v1 returned no result", 220))
            continue
        shot = Path(r.get("screenshot") or "")
        size = shot.stat().st_size if shot.is_file() else 0
        bespoke = "bespoke v1 handler" if t in known else "generic screenshot only"
        status = "ok" if size > 2000 else "failed"
        out[t] = Attempt(label, "webgui", status, rows=0,
                         sample=_clip(f"screenshot {size // 1024} KB; {r.get('analysis_summary', '')}"),
                         detail=f"{bespoke}; image evidence, no table rows"
                                + ("" if status == "ok" else "; no usable screenshot captured"))
    return out


# ── HANA SQL through the ABAP system (feasibility only) ───────────────────

# Ways an ABAP system can run SQL against its own HANA DB on our behalf.
# NOTHING here is executed: the probe only checks whether each path exists.
HANA_VIA_RFC_FMS = {
    "SXPG_COMMAND_EXECUTE": "runs an SM69 external command (e.g. an hdbsql wrapper) — v1 already uses it for df -h",
    "SXPG_COMMAND_LIST_GET": "lists SM69 external commands",
    "RFC_ABAP_INSTALL_AND_RUN": "generic ABAP execution — never acceptable for monitoring; listed only so its presence is known",
}
HANA_VIA_RFC_PATTERNS = ["/SDF/HDB%", "%HDB%SQL%", "%SQL%EXEC%", "%EXEC%SQL%", "DB6%SQL%",
                         "%DBACOCKPIT%", "SHDB%", "/SDF/%HANA%"]
HANA_VIEWS = ["M_BACKUP_CATALOG", "M_HOST_RESOURCE_UTILIZATION", "M_DISKS", "M_BLOCKED_TRANSACTIONS",
              "M_SYSTEM_AVAILABILITY", "STATISTICS_CURRENT_ALERTS"]


def hana_via_rfc(rfc: "RfcProber") -> list[dict]:
    """Can the ABAP system run HANA SQL for us over SOAP RFC, the way DBACOCKPIT's
    SQL editor does locally? Checks (read-only): remote-enabled modules that
    could, SM69 external commands wrapping hdbsql, and DDIC objects that would
    let RFC_READ_TABLE read a HANA monitoring view directly."""
    out: list[dict] = []

    def add(path, status, detail):
        out.append({"path": path, "status": status, "detail": _clip(detail, 260)})

    names = ", ".join(f"'{n}'" for n in HANA_VIA_RFC_FMS)
    try:
        rows = rfc._read("TFDIR", ["FUNCNAME", "FMODE"], f"FUNCNAME IN ({names})", rowcount=10)
        found = {r["FUNCNAME"]: r.get("FMODE") for r in rows}
        for fm, why in HANA_VIA_RFC_FMS.items():
            st = "remote-enabled" if found.get(fm) == "R" else ("exists, not remote" if fm in found else "absent")
            add(f"FM {fm}", st, why)
    except RfcSoapError as exc:
        add("TFDIR", "failed", exc.message)

    try:
        cmds = rfc._read("SXPGCOSTAB", ["NAME", "OPSYSTEM", "OPCOMMAND", "PARAMETERS"], rowcount=500)
        hdb = [c for c in cmds if "hdbsql" in (c.get("OPCOMMAND", "") + c.get("PARAMETERS", "")).lower()]
        add("SM69 commands (SXPGCOSTAB)", "ok" if hdb else "none matching",
            (f"{len(hdb)} hdbsql command(s): " + "; ".join(
                f"{c['NAME']} = {c['OPCOMMAND']} {c.get('PARAMETERS', '')}" for c in hdb[:5]))
            if hdb else f"{len(cmds)} customer commands read, none call hdbsql")
    except RfcSoapError as exc:
        add("SM69 commands (SXPGCOSTAB)", "failed", exc.message)

    try:
        tabs = ", ".join(f"'{v}'" for v in HANA_VIEWS)
        rows = rfc._read("DD02L", ["TABNAME", "TABCLASS"], f"TABNAME IN ({tabs})", rowcount=20)
        add("DDIC views over HANA monitoring views (DD02L)", "ok" if rows else "none",
            ", ".join(f"{r['TABNAME']} ({r['TABCLASS']})" for r in rows)
            or "none of " + ", ".join(HANA_VIEWS) + " is in the DDIC, so RFC_READ_TABLE cannot read them")
    except RfcSoapError as exc:
        add("DDIC (DD02L)", "failed", exc.message)

    try:
        found = rfc.discover(HANA_VIA_RFC_PATTERNS)
        add("Discovered remote-enabled modules (listed, NOT called)", "ok" if found else "none",
            ", ".join(found[:25]) or "no module matched " + ", ".join(HANA_VIA_RFC_PATTERNS))
    except RfcSoapError as exc:
        add("Discovery", "failed", exc.message)
    return out


# ── Orchestration ─────────────────────────────────────────────────────────

STRUCTURED = ("rfc_table", "rfc_fm", "hana_sql")


def probe(specs: list[CheckSpec], rfc: Optional[RfcProber], hana: Optional[HanaProber],
          webgui: Optional[Callable[[list[str]], dict[str, Attempt]]] = None,
          discover: bool = True) -> list[ProbeResult]:
    results = []
    for s in specs:
        pr = ProbeResult(s.key, s.task, s.tcode, note=s.note)
        for src in s.sources:
            if src.kind in ("rfc_table", "rfc_fm"):
                pr.attempts.append(rfc.run(src) if rfc else
                                   Attempt(src.label(), src.kind, "skipped", detail="no RFC client"))
            elif src.kind == "hana_sql":
                pr.attempts.append(hana.run(src) if hana else
                                   Attempt(src.label(), src.kind, "skipped", detail="HANA not probed"))
        if discover and rfc and s.discover:
            try:
                pr.discovered = rfc.discover(s.discover)
            except RfcSoapError as exc:
                pr.discovered = [f"(discovery failed: {_clip(exc.message, 80)})"]
        results.append(pr)

    # WebGUI only for checks with no working structured source.
    need = {}
    for s, pr in zip(specs, results):
        if not any(a.status in ("ok", "empty") for a in pr.attempts):
            w = next((x for x in s.sources if x.kind == "webgui"), None)
            if w:
                need[s.key] = w
    shots = webgui(sorted({w.target for w in need.values()})) if (webgui and need) else {}
    for s, pr in zip(specs, results):
        w = next((x for x in s.sources if x.kind == "webgui"), None)
        if not w:
            continue
        if s.key in need:
            pr.attempts.append(shots.get(w.target) or Attempt(
                w.label(), "webgui", "skipped",
                detail="not run — pass --webgui to try the v1 engine" if not webgui else "no result"))
        else:
            pr.attempts.append(Attempt(w.label(), "webgui", "skipped",
                                       detail="not needed — a structured source works"))

    for pr in results:
        best = next((a for a in pr.attempts if a.status == "ok"), None) \
            or next((a for a in pr.attempts if a.status == "empty"), None)
        if best:
            pr.chosen, pr.chosen_kind, pr.confirmed, pr.sample = best.source, best.kind, True, best.sample
            if best.status == "empty":
                pr.sample = "reachable, " + best.sample
        else:
            pr.chosen = "—"
            groups: dict[str, list[str]] = {}          # same reason -> one entry
            for a in pr.attempts:
                groups.setdefault(a.detail or a.status, []).append(a.source)
            reasons = [f"{', '.join(srcs)}: {why}" for why, srcs in groups.items()]
            pr.sample = _clip(" / ".join(reasons), 240) if reasons else "no candidate source"
    return results


def to_markdown(results: list[ProbeResult], sid: str, header: str = "",
                hana_rfc: Optional[list[dict]] = None) -> str:
    ok = sum(r.confirmed for r in results)
    img = sum(r.confirmed and r.chosen_kind == "webgui" for r in results)
    lines = [f"# AHC data-source probe — {sid}", "",
             header or f"Generated {dt.datetime.now().isoformat(timespec='seconds')}", "",
             f"**{ok} of {len(results)} checks have a confirmed source"
             + (f" ({img} of them WebGUI screenshots only — no rows for a workbook tab)" if img else "")
             + ".**", "",
             "| # | Check | T-code | Chosen source | Sample data | Confirmed |",
             "|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        esc = lambda s: str(s).replace("|", "\\|")  # noqa: E731
        lines.append(f"| {i} | {esc(r.task)} | {esc(r.tcode)} | {esc(r.chosen)} | "
                     f"{esc(r.sample)} | {('yes (image only)' if r.chosen_kind == 'webgui' else 'yes') if r.confirmed else 'no'} |")
    if hana_rfc:
        lines += ["", "## HANA SQL through the ABAP system (feasibility, nothing executed)", "",
                  "| Path | Status | Detail |", "|---|---|---|"]
        for h in hana_rfc:
            lines.append(f"| {h['path']} | {h['status']} | {str(h['detail']).replace('|', '/')} |")
    lines += ["", "## Every attempt", ""]
    for r in results:
        lines.append(f"### {r.task} ({r.tcode})" + (f" — {r.note}" if r.note else ""))
        for a in r.attempts:
            lines.append(f"- **{a.status}** {a.source}" + (f" — rows {a.rows}" if a.rows else "")
                         + (f" — {a.detail}" if a.detail else "")
                         + (f"  \n  sample: `{a.sample}`" if a.sample and a.status in ("ok", "empty") else ""))
        if r.discovered:
            lines.append("- discovered (listed, NOT called): " + ", ".join(r.discovered[:20]))
        lines.append("")
    return "\n".join(lines)


def _rfc_client_for(sid: str, registry_path: str) -> tuple[RfcSoapClient, str]:
    from engine import crypto
    from engine.registry import Registry
    entry = Registry.load(registry_path).get(sid)
    cred = entry.sap_credential
    if not cred.is_set():
        raise SystemExit(f"{sid} has no SAP credential stored (Systems page).")
    return (RfcSoapClient.from_system(entry.webgui_url, client=entry.client,
                                      username=cred.username,
                                      password=crypto.decrypt(cred.password_enc)),
            entry.client)


def _rfc_client_direct(url: str, sap_client: str) -> tuple[RfcSoapClient, str]:
    """No registry: WebGUI/ICM URL from the command line, credentials ONLY from
    the environment (SAP_PROBE_USER / SAP_PROBE_PASSWORD, D-018)."""
    user, pwd = os.environ.get("SAP_PROBE_USER", ""), os.environ.get("SAP_PROBE_PASSWORD", "")
    if not (user and pwd):
        raise SystemExit("--url needs SAP_PROBE_USER and SAP_PROBE_PASSWORD in the environment.")
    client = RfcSoapClient.from_system(url, client=sap_client, username=user, password=pwd)
    if os.environ.get("AHC_PROBE_ENV_PROXY"):
        # The app's client ignores proxy env vars on purpose (D-045). A probe run
        # from a workstation that can only reach SAP through a proxy opts in here.
        import httpx
        client._http.close()
        client._http = httpx.Client(auth=(user, pwd), timeout=client.timeout_s,
                                    follow_redirects=True, trust_env=True)
    return client, sap_client


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sid", required=True)
    ap.add_argument("--sample", type=Path, help="SAP AHC workbook whose Monitors tab lists the checks")
    ap.add_argument("--webgui", action="store_true",
                    help="run the v1 engine for checks with no structured source (slow)")
    ap.add_argument("--no-discover", action="store_true", help="skip the TFDIR discovery search")
    ap.add_argument("--registry", default=os.environ.get("SYSTEMS_PATH", "systems.json"))
    ap.add_argument("--out", type=Path, default=Path("ahc_probe"))
    ap.add_argument("--url", help="WebGUI URL; bypasses the registry (creds from SAP_PROBE_USER/_PASSWORD)")
    ap.add_argument("--client", default="100", help="SAP client for --url")
    a = ap.parse_args(argv)

    specs = checks_from_sample(a.sample) if a.sample else list(CATALOGUE.values())
    client, sap_client = (_rfc_client_direct(a.url, a.client) if a.url
                          else _rfc_client_for(a.sid, a.registry))
    rfc = RfcProber(client, a.sid, sap_client)
    try:
        info = client.ping().get("RFCSI_EXPORT") or {}
        head = (f"System {info.get('RFCSYSID', '?')} release {info.get('RFCSAPRL', '?')} "
                f"kernel {info.get('RFCKERNRL', '?')} DB {info.get('RFCDBSYS', '?')} — "
                f"{len(specs)} checks from " + (a.sample.name if a.sample else "the built-in catalogue"))
    except RfcSoapError as exc:
        head = f"RFC_SYSTEM_INFO failed: {exc.message}"
    hana = HanaProber(a.sid)
    webgui = (lambda tcodes: asyncio.run(webgui_capture(a.sid, tcodes, a.registry))) \
        if a.webgui else None
    results = probe(specs, rfc, hana, webgui, discover=not a.no_discover)
    hana_rfc = hana_via_rfc(rfc)
    client.close()

    md = to_markdown(results, a.sid, head, hana_rfc)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "ahc_probe.md").write_text(md, encoding="utf-8")
    (a.out / "ahc_probe.json").write_text(json.dumps(
        {"sid": a.sid, "header": head, "results": [asdict(r) for r in results],
         "hana_via_rfc": hana_rfc},
        indent=2, default=str), encoding="utf-8")
    print(md)
    print(f"\nWritten: {a.out / 'ahc_probe.md'} and ahc_probe.json", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
