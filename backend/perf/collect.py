"""SAP + HANA performance data over SOAP RFC (D-052).

Everything here is a READ through the same allowlisted `ahc.sap.Sap`
wrapper the health check uses; HANA goes through the SM69 hdbsql command
(`ahc.hana.HanaViaSm69`, ZAHC_HDBSQL). Sources:

  live    TH_WPINFO (SM50/SM66), GET_CPU_ALL/GET_MEM_ALL (ST06),
          TH_USER_LIST (AL08), SAPTUNE_* (ST02), ENQUEUE_READ (SM12),
          VBHDR (SM13), TBTCO (SM37), SWNC aggregates for today (ST03N),
          /SDF/SMON_HEADER newest snapshot (SMON), HANA M_* views.
  history SWNCMONIINDEX + SWNC_COLLECTOR_GET_AGGREGATES (ST03N, per day:
          task types, hourly profile, top transactions), /SDF/SMON_HEADER
          for that date, HANA M_LOAD_HISTORY_HOST (hourly avg/max).

SWNC_COLLECTOR_GET_AGGREGATES returns MILLISECONDS: its FACTOR parameter
(default 1000) already divides the stored microseconds — verified live on
F4H (dialog 2946 ms / 4 steps = 736 ms per step). TASKTYPE comes back as a
base64-encoded RAW(1) ("AQ==" = 01). Systems whose collector never built a
TOTAL component only hold per-instance components (nwrhel9_F4H_00), so a
day is read per instance and summed. TCDET can be empty while TIMES carries
the per-transaction ENTRY_ID (name padded + trailing T/R type flag).
The history window is whatever SAP itself keeps (decision 2026-10-02):
each source reports its own oldest available date.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
from collections import defaultdict
from typing import Callable, Optional

from engine.rfc.soap_client import RfcSoapError

from ahc.collect import _TASKTYPE, Ctx, load_servers, to_float, to_int
from ahc.hana import HanaViaSm69, Truncated
from ahc.sap import Sap, as_rows, first

SMON_FIELDS = ["DATUM", "TIME", "SERVER", "ACT_WPS", "ACT_DIA", "USR_TOTAL", "SYS_TOTAL", "IDLE_TOTAL",
               "FREE_MEM_PERC", "DIAQ", "UPDQ", "ENQQ", "USERS", "SESSIONS", "SM12_CNT"]


def _safe(fn: Callable, notes: list[str], label: str, default=None):
    try:
        return fn()
    except (RfcSoapError, Truncated, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        notes.append(f"{label}: {getattr(exc, 'message', str(exc))[:180]}")
        return default


# ── workload (ST03N) ──────────────────────────────────────────────────────

def _ms(rec: dict, steps: float, *keys: str) -> float:
    return sum(to_float(rec.get(k)) for k in keys) / steps if steps else 0.0


def _db_ms(rec: dict) -> float:
    dbti = to_float(rec.get("DBTI"))
    return dbti if dbti else sum(to_float(rec.get(k)) for k in ("READDIRTI", "READSEQTI", "CHNGTI"))


# Raw kernel codes. ahc's older map guessed 21–27/65 as RFC/CPIC/ALE/HTTP…; F4H shows RFC under FE,
# so those guesses are dropped and an unknown code is shown as "TYPE xx" rather than mislabelled.
TASK_NAMES = {**{k: v for k, v in _TASKTYPE.items() if k not in ("21", "22", "23", "24", "25", "27", "65")}, "0A": "EXT.PLUGIN", "0D": "RFCVMC", "0E": "DDLOG CLEANUP", "0F": "DEL. THCALL",
              "10": "AUTOJAVA", "11": "LICENCESRV", "FE": "RFC", "FD": "CPIC", "FC": "ALE", "FB": "HTTP",
              "FA": "HTTPS", "F9": "NNTP", "F8": "SMTP", "F7": "FTP"}


def tt_code(v) -> str:
    """SWNC task type → 2-char hex code. SOAP delivers the RAW(1) base64-encoded ("AQ==" → "01");
    older kernels/fakes may already send the hex text."""
    v = (v or "").strip()
    if re.fullmatch(r"[0-9A-Fa-f]{2}", v):
        return v.upper()
    try:
        raw = base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError):
        return v.upper()
    return raw.hex().upper() if len(raw) == 1 else v.upper()


def tt_name(code: str) -> str:
    return TASK_NAMES.get(code, f"TYPE {code}" if code else "?")


def task_line(rec: dict) -> dict:
    steps = to_float(first(rec, "COUNT", "DCOUNT"))
    code = tt_code(rec.get("TASKTYPE"))
    db = _db_ms(rec)
    return {
        "code": code, "name": tt_name(code), "steps": int(steps),
        "avg_resp": _ms(rec, steps, "RESPTI"), "avg_cpu": _ms(rec, steps, "CPUTI"),
        "avg_db": db / steps if steps else 0.0, "avg_wait": _ms(rec, steps, "QUEUETI"),
        "avg_rollwait": _ms(rec, steps, "ROLLWAITTI"), "avg_gui": _ms(rec, steps, "GUITIME"),
        "avg_load": _ms(rec, steps, "LOADGENTI"), "avg_proc": _ms(rec, steps, "PROCTI"),
        "avg_lock": _ms(rec, steps, "LOCKTI"),
        "total_resp_s": to_float(rec.get("RESPTI")) / 1e3, "total_cpu_s": to_float(rec.get("CPUTI")) / 1e3,
        "total_db_s": db / 1e3,
    }


def _ymd(d: str) -> Optional[str]:
    s = re.sub(r"\D", "", d or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else None


def _index(sap: Sap, sid: str) -> list[dict]:
    rows = sap.read("SWNCMONIINDEX", ["COMPONENT", "ASSIGNDSYS", "PERIODTYPE", "PERIODSTRT"],
                    "PERIODTYPE EQ 'D'", rowcount=5000)
    return [r for r in rows if r.get("ASSIGNDSYS", "").strip() in (sid, "")]


def workload_dates(sap: Sap, sid: str) -> list[str]:
    """Days for which SAP still holds daily workload aggregates (newest first), any component."""
    return sorted({d for d in (_ymd(r.get("PERIODSTRT")) for r in _index(sap, sid)) if d}, reverse=True)


def _components(sap: Sap, sid: str, day: dt.date) -> list[str]:
    """TOTAL if the collector built it, else every instance component of that day (or of any day,
    for today, which is only indexed once the day has been aggregated)."""
    idx = _index(sap, sid)
    that = {r["COMPONENT"].strip() for r in idx if _ymd(r.get("PERIODSTRT")) == day.isoformat()}
    if "TOTAL" in that:
        return ["TOTAL"]
    if that:
        return sorted(that)
    every = sorted({r["COMPONENT"].strip() for r in idx} - {"TOTAL"})
    return ["TOTAL", *every]


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _merge(rows: list[dict], key: Callable[[dict], tuple]) -> list[dict]:
    """Sum numeric fields of rows sharing `key` (instances → system total)."""
    out: dict[tuple, dict] = {}
    for r in rows:
        k = key(r)
        if k not in out:
            out[k] = dict(r)
            continue
        m = out[k]
        for f, v in r.items():
            a, b = _num(m.get(f)), _num(v)
            if a is not None and b is not None and f not in ("TASKTYPE", "TIME", "ENTRY_ID"):
                m[f] = str(a + b)
    return list(out.values())


def _aggregates(sap: Sap, sid: str, day: dt.date, notes: list[str]) -> Optional[dict]:
    tasks, times, tcdet, got = [], [], [], False
    for comp in _components(sap, sid, day):
        base = dict(COMPONENT=comp, ASSIGNDSYS=sid, PERIODTYPE="D", PERIODSTRT=day.isoformat())
        r = None
        for tables in (dict(TASKTYPE=[], TIMES=[], TCDET=[]), dict(TASKTYPE=[], TIMES=[])):
            try:
                r = sap.call("SWNC_COLLECTOR_GET_AGGREGATES", **base, **tables)
                break
            except RfcSoapError as exc:
                if "NO_DATA_FOUND" in exc.message:
                    break
                if "TCDET" not in tables:
                    notes.append(f"ST03N {day} {comp}: {exc.message[:160]}")
        if r is None:
            continue
        t = as_rows(r.get("TASKTYPE"))
        if not t:
            continue
        got = True
        tasks += t
        times += as_rows(r.get("TIMES"))
        tcdet += as_rows(r.get("TCDET"))
        if comp == "TOTAL":
            break
    if not got:
        return None
    return {"TASKTYPE": _merge(tasks, lambda r: (tt_code(r.get("TASKTYPE")),)), "TIMES": times, "TCDET": tcdet}


def _hour(v: str) -> int:
    """TIMES.TIME is '07--08' (hour slot) on 7.5x; older releases used HHMMSS / HH."""
    m = re.match(r"\s*(\d{1,2})", v or "")
    return int(m.group(1)) if m else -1


def _entry(name: str) -> str:
    """'DB02      …      T' → 'DB02' (trailing single-char T/R/… entry-type flag dropped)."""
    name = re.sub(r"\s+[A-Z]$", "", (name or "").rstrip())
    return re.sub(r"\s{2,}", "  ", name.strip())


def workload_day(sap: Sap, sid: str, day: dt.date, notes: list[str]) -> Optional[dict]:
    """ST03N for one day: task-type lines, hourly profile, top transactions."""
    r = _aggregates(sap, sid, day, notes)
    if r is None:
        return None
    tasks = [task_line(t) for t in r["TASKTYPE"] if to_float(first(t, "COUNT", "DCOUNT"))]
    if not tasks:
        return None
    tasks.sort(key=lambda t: -t["total_resp_s"])
    hours: dict[int, dict] = {h: {"hour": h, "steps": 0, "dia_steps": 0, "resp": 0.0, "cpu": 0.0, "db": 0.0,
                                   "wait": 0.0, "other": 0.0} for h in range(24)}
    for t in r["TIMES"]:
        h = _hour(first(t, "TIME", "HOUR", "TIMEID"))
        if not 0 <= h <= 23:
            continue
        n = to_float(first(t, "COUNT", "DCOUNT"))
        hours[h]["steps"] += int(n)
        if tt_code(t.get("TASKTYPE")) == "01" and n:
            hb = hours[h]
            hb["dia_steps"] += int(n)
            hb["resp"] += to_float(t.get("RESPTI"))
            hb["cpu"] += to_float(t.get("CPUTI"))
            hb["db"] += _db_ms(t)
            hb["wait"] += to_float(t.get("QUEUETI"))
    profile = []
    for h in range(24):
        hb = hours[h]
        n = hb["dia_steps"]
        avg = lambda v: round(v / n, 1) if n else 0.0  # noqa: E731
        cpu, db, wait, resp = avg(hb["cpu"]), avg(hb["db"]), avg(hb["wait"]), avg(hb["resp"])
        profile.append({"hour": h, "steps": hb["steps"], "dia_steps": n, "avg_resp": resp, "cpu": cpu, "db": db,
                        "wait": wait, "other": round(max(0.0, resp - cpu - db - wait), 1)})
    # TCDET (per transaction + user) when the collector keeps it, else TIMES (per transaction + hour).
    src, with_users = (r["TCDET"], True) if r["TCDET"] else (r["TIMES"], False)
    top = defaultdict(lambda: {"steps": 0, "resp": 0.0, "cpu": 0.0, "db": 0.0, "users": set(), "task": ""})
    for t in src:
        name = _entry(t.get("ENTRY_ID") or "")
        if not name:
            continue
        e = top[name]
        e["steps"] += int(to_float(first(t, "COUNT", "DCOUNT")))
        e["resp"] += to_float(t.get("RESPTI"))
        e["cpu"] += to_float(t.get("CPUTI"))
        e["db"] += _db_ms(t)
        e["task"] = e["task"] or (tt_name(tt_code(t.get("TASKTYPE"))) if t.get("TASKTYPE") else "")
        if t.get("ACCOUNT"):
            e["users"].add(t["ACCOUNT"].strip())
    total_resp = sum(e["resp"] for e in top.values()) or 1
    tops = sorted(top.items(), key=lambda kv: -kv[1]["resp"])[:15]
    top_tx = [{"name": k, "task": v["task"], "steps": v["steps"], "total_s": round(v["resp"] / 1e3, 1),
               "share": round(v["resp"] / total_resp * 100, 1),
               "avg_resp": round(v["resp"] / v["steps"], 1) if v["steps"] else 0.0,
               "avg_cpu": round(v["cpu"] / v["steps"], 1) if v["steps"] else 0.0,
               "avg_db": round(v["db"] / v["steps"], 1) if v["steps"] else 0.0,
               "users": len(v["users"]) if with_users else None} for k, v in tops]
    return {"date": day.isoformat(), "tasks": tasks, "hourly": profile, "top": top_tx,
            "steps": sum(t["steps"] for t in tasks),
            "dialog": next((t for t in tasks if t["code"] == "01"), None)}


# ── SMON (/SDF/SMON) ──────────────────────────────────────────────────────

def smon_status(sap: Sap, now: dt.datetime) -> dict:
    exists = bool(sap.read("DD02L", ["TABNAME"], "TABNAME EQ '/SDF/SMON_HEADER' AND AS4LOCAL EQ 'A'", rowcount=1))
    if not exists:
        return {"installed": False, "active": False, "last": None,
                "why": "ST-PI component /SDF/SMON is not installed in this system"}
    rows = sap.read("/SDF/SMON_HEADER", ["DATUM", "TIME"], f"DATUM EQ '{now:%Y%m%d}'", rowcount=5000)
    last = max((r["DATUM"] + r["TIME"] for r in rows), default="")
    last_dt = _smon_dt(last[:8], last[8:]) if last else None
    active = bool(last_dt and (now - last_dt).total_seconds() < 20 * 60)
    return {"installed": True, "active": active, "last": last_dt.isoformat(sep=" ") if last_dt else None,
            "why": "" if active else ("No SMON snapshot today — daily monitoring is not scheduled" if not last_dt
                                      else f"Last SMON snapshot {last_dt:%H:%M} — collection stopped")}


def _smon_dt(d: str, t: str) -> Optional[dt.datetime]:
    d, t = re.sub(r"\D", "", d), re.sub(r"\D", "", t).ljust(6, "0")
    try:
        return dt.datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), int(t[:2]), int(t[2:4]), int(t[4:6]))
    except (ValueError, IndexError):
        return None


def smon_day(sap: Sap, day: dt.date, bucket_min: int = 5) -> list[dict]:
    """SMON snapshots for one day, all servers, averaged into `bucket_min` buckets."""
    rows = sap.read("/SDF/SMON_HEADER", SMON_FIELDS, f"DATUM EQ '{day:%Y%m%d}'", rowcount=40000)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        t = _smon_dt(r.get("DATUM", ""), r.get("TIME", ""))
        if not t:
            continue
        key = t.replace(minute=t.minute - t.minute % bucket_min, second=0).strftime("%H:%M")
        buckets[key].append(r)
    out = []
    for key in sorted(buckets):
        rs = buckets[key]
        per_srv: dict[str, list[dict]] = defaultdict(list)
        for r in rs:
            per_srv[r.get("SERVER", "")].append(r)
        mean = lambda f, src: sum(to_float(x.get(f)) for x in src) / len(src)  # noqa: E731
        # sums across servers of each server's bucket mean; CPU as an average over servers
        tot = lambda f: round(sum(mean(f, v) for v in per_srv.values()), 1)  # noqa: E731
        avg = lambda f: round(sum(mean(f, v) for v in per_srv.values()) / len(per_srv), 1)  # noqa: E731
        out.append({"time": key, "act_wps": tot("ACT_WPS"), "act_dia": tot("ACT_DIA"),
                    "cpu": round(100 - avg("IDLE_TOTAL"), 1), "free_mem": avg("FREE_MEM_PERC"),
                    "diaq": tot("DIAQ"), "updq": tot("UPDQ"), "enqq": tot("ENQQ"), "users": tot("USERS"),
                    "sessions": tot("SESSIONS"), "locks": tot("SM12_CNT"), "servers": len(per_srv)})
    return out


# ── live ABAP state ───────────────────────────────────────────────────────

def _wp_summary(wps: list[dict]) -> dict:
    by = defaultdict(lambda: {"total": 0, "running": 0, "waiting": 0, "hold": 0, "stopped": 0})
    for w in wps:
        typ = (w.get("WP_TYP") or w.get("WP_TYPE") or "?").strip().upper()[:3]
        st = (w.get("WP_STATUS") or "").strip().lower()
        b = by[typ]
        b["total"] += 1
        if st.startswith("run"):
            b["running"] += 1
        elif st.startswith("wait"):
            b["waiting"] += 1
        elif "hold" in st:
            b["hold"] += 1
        elif st in ("stopped", "ended", "complete"):
            b["stopped"] += 1
    return dict(by)


def sap_live(x: Ctx, notes: list[str]) -> dict:
    sap = x.sap
    _safe(lambda: load_servers(x), notes, "TH_SERVER_LIST")
    servers = []
    all_wp: dict[str, dict] = defaultdict(lambda: {"total": 0, "running": 0, "waiting": 0, "hold": 0, "stopped": 0})
    for s in x.servers or [{"name": "", "host": "", "active": True}]:
        wps = _safe(lambda: as_rows(sap.call("TH_WPINFO", SRVNAME=s["name"], WPLIST=[]).get("WPLIST")),
                    notes, f"TH_WPINFO {s['name']}", [])
        summ = _wp_summary(wps)
        for typ, b in summ.items():
            for k, v in b.items():
                all_wp[typ][k] += v
        servers.append({"name": s["name"], "host": s.get("host", ""), "active": s.get("active", True),
                        "wp": summ, "busy_dia": summ.get("DIA", {}).get("running", 0),
                        "total_dia": summ.get("DIA", {}).get("total", 0)})
    cpu = _safe(lambda: (as_rows(sap.call("GET_CPU_ALL", TF_CPU_ALL=[]).get("TF_CPU_ALL")) or [{}])[0], notes,
                "GET_CPU_ALL", {})
    mem = _safe(lambda: (as_rows(sap.call("GET_MEM_ALL", TF_MEM_ALL=[]).get("TF_MEM_ALL")) or [{}])[0], notes,
                "GET_MEM_ALL", {})
    users = _safe(lambda: as_rows(sap.call("TH_USER_LIST", USRLIST=[]).get("USRLIST")), notes, "TH_USER_LIST", [])
    locks = _safe(lambda: to_int(sap.call("ENQUEUE_READ", GCLIENT="", GNAME="", GUNAME="", ENQ=[]).get("NUMBER")),
                  notes, "ENQUEUE_READ", None)
    vb = _safe(lambda: sap.read("VBHDR", ["VBKEY", "VBRC"], "", rowcount=2000), notes, "VBHDR", None)
    upd = len(vb) if vb is not None else None
    upd_err = sum(1 for r in vb if (r.get("VBRC") or "0").strip() not in ("", "0")) if vb is not None else None
    jobs = _safe(lambda: len(sap.read("TBTCO", ["JOBNAME"], "STATUS EQ 'R'", rowcount=2000)), notes, "TBTCO", None)
    buffers = _safe(lambda: buffer_quality(sap), notes, "ST02", None)
    idle = to_float(cpu.get("IDLE_TOTAL"), -1)
    phys, free = to_float(mem.get("PHYS_MEM")), to_float(mem.get("FREE_MEM"))
    dia_total = all_wp.get("DIA", {}).get("total", 0)
    dia_busy = all_wp.get("DIA", {}).get("running", 0)
    return {
        "servers": servers, "wp": dict(all_wp),
        "dia_util": round(dia_busy / dia_total * 100, 1) if dia_total else None,
        "cpu": {"util": round(100 - idle, 1) if idle >= 0 else None, "cpus": to_int(cpu.get("NBR_CPU")) or None,
                "load1": round(to_float(cpu.get("LOAD1_AVG")) / 100, 2), "load15": round(to_float(cpu.get("LOAD15_AVG")) / 100, 2),
                "user": to_int(cpu.get("USR_TOTAL")), "sys": to_int(cpu.get("SYS_TOTAL")), "wait": to_int(cpu.get("WAIT_TRUE"))},
        "memory": {"phys_mb": round(phys / 1024) if phys else None, "free_mb": round(free / 1024) if phys else None,
                   "free_pct": round(free / phys * 100, 1) if phys else None,
                   "swap_used_pct": round((1 - to_float(mem.get("SWAP_FREE")) / to_float(mem.get("SWAP_SIZE"))) * 100, 1)
                   if to_float(mem.get("SWAP_SIZE")) else None},
        "users": len({(u.get("MANDT"), u.get("BNAME")) for u in users}) if users is not None else None,
        "sessions": sum(to_int(u.get("EXTMODI"), 1) for u in users) if users else (0 if users == [] else None),
        "locks": locks, "updates_pending": upd, "update_errors": upd_err, "jobs_running": jobs, "buffers": buffers,
    }


# Buffers EWA rates by hit ratio. Catalog (CTLGA), export/import (EIBUF/ESM/OTR) and calendar buffers
# are listed in ST02 but naturally run low hit ratios on a quiet system — F4H showed CTLGA 78 %.
RATED_BUFFERS = {"PXA", "TABL", "TABLP", "CUA", "PRES", "TTAB", "FTAB", "SNTAB", "IRBD", "IRDB"}


def buffer_quality(sap: Sap) -> dict:
    names = [first(b, "NAME", "BUFFERNAME", "value") for b in
             as_rows(sap.call("SAPTUNE_BUFFER_NAMES", BUFFERNAMES=[]).get("BUFFERNAMES"))]
    out, swaps, worst, worst_name = [], 0, None, None
    for n in [n for n in names if n][:20]:
        try:
            b = sap.call("SAPTUNE_BUFFER_STATISTICS", BUFFERNAME=n).get("BUFFER_STATISTIC") or {}
        except RfcSoapError:
            continue
        req, hit = to_float(b.get("REQUEST")), to_float(b.get("HIT"))
        ratio = hit / req * 100 if req else None
        sw = to_int(b.get("SWAP"))
        swaps += sw
        alloc, avail = to_float(b.get("ALLOC_SIZE")), to_float(b.get("AVAIL_SIZE"))
        out.append({"name": n, "hit": round(ratio, 2) if ratio is not None else None, "swaps": sw,
                    "free_pct": round(avail / alloc * 100, 1) if alloc else None, "requests": int(req)})
        out[-1]["rated"] = n.strip().upper() in RATED_BUFFERS
        if ratio is not None and req > 1000 and out[-1]["rated"] and (worst is None or ratio < worst):
            worst, worst_name = ratio, n
    return {"buffers": out, "swaps": swaps, "worst_hit": round(worst, 2) if worst is not None else None,
            "worst_name": worst_name}


# ── HANA ──────────────────────────────────────────────────────────────────

def _cols(h: HanaViaSm69, schema: str, view: str) -> set[str]:
    rows = h.query(f"SELECT COLUMN_NAME FROM SYS.VIEW_COLUMNS WHERE SCHEMA_NAME = '{schema}' AND VIEW_NAME = '{view}'")
    return {r.get("COLUMN_NAME", "") for r in rows}


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


GB = 1024 ** 3


def hana_live(h: HanaViaSm69, notes: list[str]) -> dict:
    out: dict = {}
    lh = _cols(h, "SYS", "M_LOAD_HISTORY_HOST")
    want = [c for c in ("HOST", "TIME", "CPU", "MEMORY_USED", "MEMORY_ALLOCATION_LIMIT", "MEMORY_SIZE",
                        "DISK_USED", "DISK_SIZE", "NETWORK_IN", "NETWORK_OUT") if c in lh]
    if {"HOST", "TIME", "CPU"} <= set(want):
        hosts = h.table(want, "FROM M_LOAD_HISTORY_HOST WHERE TIME > ADD_SECONDS(CURRENT_TIMESTAMP, -120) "
                              "ORDER BY TIME DESC, HOST", top=48)
        seen, latest = set(), []
        for r in hosts:
            if r["HOST"] not in seen:
                seen.add(r["HOST"])
                latest.append(r)
        out["hosts"] = [{"host": r["HOST"], "cpu": _f(r.get("CPU")),
                         "mem_used_gb": round(_f(r.get("MEMORY_USED")) / GB, 1) if _f(r.get("MEMORY_USED")) else None,
                         "mem_limit_gb": round(_f(r.get("MEMORY_ALLOCATION_LIMIT")) / GB, 1)
                         if _f(r.get("MEMORY_ALLOCATION_LIMIT")) else None,
                         "disk_used_gb": round(_f(r.get("DISK_USED")) / GB, 1) if _f(r.get("DISK_USED")) else None,
                         "disk_size_gb": round(_f(r.get("DISK_SIZE")) / GB, 1) if _f(r.get("DISK_SIZE")) else None}
                        for r in latest]
    if not out.get("hosts"):
        res = h.query("SELECT HOST, INSTANCE_TOTAL_MEMORY_USED_SIZE AS U, ALLOCATION_LIMIT AS L "
                      "FROM M_HOST_RESOURCE_UTILIZATION ORDER BY HOST")
        out["hosts"] = [{"host": r["HOST"], "cpu": None,
                         "mem_used_gb": round((_f(r.get("U")) or 0) / GB, 1),
                         "mem_limit_gb": round((_f(r.get("L")) or 0) / GB, 1) or None} for r in res]
    q = lambda sql, label: _safe(lambda: h.query(sql), notes, f"HANA {label}", [])  # noqa: E731
    c = q("SELECT COUNT(*) AS N, SUM(CASE WHEN CONNECTION_STATUS = 'RUNNING' THEN 1 ELSE 0 END) AS R "
          "FROM M_CONNECTIONS WHERE CONNECTION_ID > 0", "connections")
    out["connections"] = to_int(c[0].get("N")) if c else None
    out["running_statements"] = to_int(c[0].get("R")) if c else None
    b = q("SELECT COUNT(*) AS N FROM M_BLOCKED_TRANSACTIONS", "blocked")
    out["blocked"] = to_int(b[0].get("N")) if b else None
    a = q("SELECT ALERT_RATING AS R, COUNT(*) AS N FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS "
          "GROUP BY ALERT_RATING", "alerts")
    out["alerts"] = {str(r.get("R")): to_int(r.get("N")) for r in a}
    u = q("SELECT COUNT(*) AS N FROM M_CS_UNLOADS WHERE UNLOAD_TIME > ADD_DAYS(CURRENT_TIMESTAMP, -1) "
          "AND REASON = 'LOW MEMORY'", "unloads")
    out["unloads_24h"] = to_int(u[0].get("N")) if u else None
    s = q("SELECT AVG(DURATION) AS A, MAX(DURATION) AS M FROM M_SAVEPOINTS "
          "WHERE START_TIME > ADD_DAYS(CURRENT_TIMESTAMP, -1)", "savepoints")
    out["savepoint_avg_s"] = round((_f(s[0].get("A")) or 0) / 1e6, 2) if s and _f(s[0].get("A")) is not None else None
    out["savepoint_max_s"] = round((_f(s[0].get("M")) or 0) / 1e6, 2) if s and _f(s[0].get("M")) is not None else None
    out["top_sql"] = _safe(lambda: top_sql(h), notes, "HANA top SQL", [])
    return out


def top_sql(h: HanaViaSm69, n: int = 10) -> list[dict]:
    order = "ORDER BY TOTAL_EXECUTION_TIME DESC, STATEMENT_HASH"
    nums = h.table(["STATEMENT_HASH", "EXECUTION_COUNT", "TOTAL_EXECUTION_TIME", "AVG_EXECUTION_TIME"],
                   f"FROM M_SQL_PLAN_CACHE {order}", top=n)
    # quotes stripped server-side: hdbsql escapes them (\"), which could push a line past SXPG's 128 chars
    text = h.query(f"SELECT TOP {n} SUBSTR(REPLACE(TO_NVARCHAR(STATEMENT_STRING), CHAR(34), ''), 1, 90) AS S "
                   f"FROM M_SQL_PLAN_CACHE {order}")
    tot = sum((_f(r.get("TOTAL_EXECUTION_TIME")) or 0) for r in nums) or 1
    out = []
    for i, r in enumerate(nums):
        t = _f(r.get("TOTAL_EXECUTION_TIME")) or 0
        out.append({"hash": r.get("STATEMENT_HASH", "")[:12], "executions": to_int(r.get("EXECUTION_COUNT")),
                    "total_s": round(t / 1e6, 1), "avg_ms": round((_f(r.get("AVG_EXECUTION_TIME")) or 0) / 1000, 2),
                    "share": round(t / tot * 100, 1),
                    "statement": " ".join((text[i].get("S", "") if i < len(text) else "").split())})
    return out


def hana_history_range(h: HanaViaSm69) -> Optional[str]:
    r = h.query("SELECT TO_DATE(MIN(TIME)) AS D FROM M_LOAD_HISTORY_HOST")
    return ((r[0].get("D") or "")[:10] or None) if r else None


def hana_day_hosts(h: HanaViaSm69, day: dt.date) -> dict[str, list[dict]]:
    """Hourly CPU and memory per HANA host (scale-out: every worker host) from the load history."""
    nxt = day + dt.timedelta(days=1)
    where = (f"FROM M_LOAD_HISTORY_HOST WHERE TIME >= '{day}' AND TIME < '{nxt}' "
             "GROUP BY HOST, HOUR(TIME) ORDER BY 1, 2")
    cpu = {(r.get("O"), to_int(r.get("H"))): r for r in
           h.query(f"SELECT HOST AS O, HOUR(TIME) AS H, AVG(CPU) AS C, MAX(CPU) AS CM {where}")}
    mem = {(r.get("O"), to_int(r.get("H"))): r for r in h.query(
        f"SELECT HOST O,HOUR(TIME) H,AVG(MEMORY_USED) M,MAX(MEMORY_USED) MM,MAX(MEMORY_ALLOCATION_LIMIT) L {where}")}
    out: dict[str, list[dict]] = defaultdict(list)
    for key in sorted(set(cpu) | set(mem)):
        c, m = cpu.get(key, {}), mem.get(key, {})
        out[key[0]].append({"hour": key[1], "cpu_avg": round(_f(c.get("C")) or 0, 1),
                            "cpu_max": round(_f(c.get("CM")) or 0, 1),
                            "mem_avg_gb": round((_f(m.get("M")) or 0) / GB, 1),
                            "mem_max_gb": round((_f(m.get("MM")) or 0) / GB, 1),
                            "limit_gb": round((_f(m.get("L")) or 0) / GB, 1)})
    return dict(out)


def worst_per_hour(per_host: dict[str, list[dict]]) -> list[dict]:
    """One row per hour: highest CPU of any host, memory of the host closest to its limit."""
    hours: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    for host, rows in per_host.items():
        for r in rows:
            hours[r["hour"]].append((host, r))
    out = []
    for hr in sorted(hours):
        rs = hours[hr]
        mh, m = max(rs, key=lambda x: (x[1]["mem_max_gb"] / x[1]["limit_gb"]) if x[1]["limit_gb"] else 0)
        ch, c = max(rs, key=lambda x: x[1]["cpu_max"])
        out.append({"hour": hr, "cpu_avg": round(sum(x[1]["cpu_avg"] for x in rs) / len(rs), 1),
                    "cpu_max": c["cpu_max"], "cpu_host": ch, "mem_avg_gb": m["mem_avg_gb"],
                    "mem_max_gb": m["mem_max_gb"], "limit_gb": m["limit_gb"], "mem_host": mh, "hosts": len(rs)})
    return out


def hana_day(h: HanaViaSm69, day: dt.date) -> list[dict]:
    """Hourly CPU and memory for one day; with several hosts, the worst host per hour."""
    return worst_per_hour(hana_day_hosts(h, day))
