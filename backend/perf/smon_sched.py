"""Schedule /SDF/SMON (or the older /SDF/MON) daily monitoring from Sentinel (D-052c).

This WRITES to SAP — only after the user confirms in the UI, and only the
jobs SAP's own "Schedule Daily Monitoring" button creates. Read from the
ST-PI sources on F4H (RPY_PROGRAM_READ, 2026-10-03):

  /SDF/SMON → "Schedule Daily Monitoring" = report /SDF/SMON_START_CM, a
  dialog (CALL SCREEN 1001) that cannot run in batch. On confirm it creates
    1. job /SDF/SMON_SCHEDULER for today (start now, unless a daily run
       exists), and
    2. job /SDF/SMON_SCHEDULER periodic every day from tomorrow at st_tim,
  each with two steps: SUBMIT /SDF/SMON_START WITH schedld = 'X', age,
  st_tim, endtim, frequ, the content switches … and /SDF/SMON_REORG. Report
  /SDF/SMON_START (in its AT SELECTION-SCREEN 'ONLI' branch, which also runs
  in batch) inserts the day's "Daily Monitoring dd.mm.yyyy" row into
  /SDF/SMON_RUN, registers one collector per server, starts them and
  schedules its own /SDF/SMON_WATCHDOG job.
  /SDF/MON → the same with /SDF/MON_START, /SDF/MON_REORG, job
  /SDF/MON_SCHEDULER and table /SDF/ANALYSES.

Sentinel does exactly that through XBP: BAPI_XBP_JOB_OPEN, two
BAPI_XBP_JOB_ADD_ABAP_STEP with SELINFO (temporary variant — no variant is
created), BAPI_XBP_JOB_CLOSE, then BAPI_XBP_JOB_START_IMMEDIATELY (today) or
BAPI_XBP_JOB_HEADER_MODIFY (tomorrow 00:00, PERIODIC + PRDDAYS 1, MASK-STARTCOND
= 'X' — verified on F4H 2026-10-03: job released, periodic, PRDDAYS 001).
Parameter names come from BAPI_XBP_READ_SELSCREEN on F4H; every parameter is
passed explicitly with SAP's own default, because a temporary variant does
not fall back to the report's DEFAULT values for missing ones.

Stopping = deleting the future /SDF/SMON_SCHEDULER jobs; today's run ends at
its end time (or "Stop Analysis" in /SDF/SMON).
Authorizations: S_XMI_PROD, S_BTCH_JOB (JOBACTION RELE), S_PROGRAM for the
/SDF/ reports — the same as starting it in /SDF/SMON. AHC_NO_JOBS=1 switches
it off.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from collections import defaultdict
from typing import Callable, Optional

from engine.rfc.soap_client import RfcSoapError

from ahc.sap import Sap, as_rows
from ahc.xbp import EXT_USER, JobError, _check, jobs_enabled

from .collect import _smon_dt

TOOLS = {
    "SMON": {"start": "/SDF/SMON_START", "reorg": "/SDF/SMON_REORG", "job": "/SDF/SMON_SCHEDULER",
             "runs": "/SDF/SMON_RUN", "watchdog": "/SDF/SMON_WATCHDOG"},
    "MON": {"start": "/SDF/MON_START", "reorg": "/SDF/MON_REORG", "job": "/SDF/MON_SCHEDULER",
            "runs": "/SDF/ANALYSES", "watchdog": "/SDF/MON_WATCHDOG"},
}
SCHED_REPORTS = {t[k] for t in TOOLS.values() for k in ("start", "reorg")}
FUTURE = ("P", "S", "Y", "Z")          # scheduled, released, ready, released/suspended
DAY_START, DAY_END = "000000", "235900"


def tool_of(sap: Sap) -> Optional[str]:
    """SMON when its start report exists, else MON, else None (ST-PI not installed)."""
    for tool in ("SMON", "MON"):
        if sap.read("TRDIR", ["NAME"], f"NAME EQ '{TOOLS[tool]['start']}'", rowcount=1):
            return tool
    return None


def _p(name: str, value) -> dict:
    return {"SELNAME": name, "KIND": "P", "SIGN": "I", "OPTION": "EQ", "LOW": str(value), "HIGH": ""}


def selinfo(tool: str, user: str, frequ: int, keep_days: int, st_tim: str, endtim: str) -> list[dict]:
    """The SUBMIT … WITH list of /SDF/S?MON_START_CM's schedule_job, SAP defaults for the content switches."""
    common = [_p("SCHEDLD", "X"), _p("AGE", keep_days), _p("ST_TIM", st_tim), _p("ENDTIM", endtim),
              _p("FREQU", frequ), _p("MAXSIZE", 0), _p("SIZE", ""), _p("MAXRECS", 0),
              _p("SM50", "X"), _p("SM50_CT", 1), _p("QUEUE", "X"), _p("QUEUE_CT", 10),
              _p("ST02", "X"), _p("ST02_CT", 10), _p("USERS", "X"), _p("USERS_CT", 10),
              _p("SM04", ""), _p("SM04_CT", 60), _p("ST06", "X"), _p("TOPCPU", "X"),
              _p("SM12", ""), _p("SM12_CT", 60), _p("SM12STAT", ""), _p("SM12STCT", 60),
              _p("QIN", ""), _p("QIN_CT", 60), _p("QOUT", ""), _p("QOUT_CT", 60)]
    if tool == "SMON":
        return common + [_p("JOBUSER", user), _p("DO_REORG", "X"), _p("FREQU_DB", 10), _p("STACK", "X"),
                         _p("STACK_CT", 10), _p("TRANSP", "X"), _p("NETRTT", "X"),
                         # NETRTT at most 3×/min: F4H job log "NetRTT frequency too high (limit <4 times/minute)"
                         _p("NETRTTCT", 15 // frequ + 1),
                         _p("LOCKWP", "")]
    return common + [_p("WPDETAIL", "X"), _p("CPU", ""), _p("SQL_ST", ""), _p("ABAP", ""), _p("TRFC", "X"),
                     _p("TRFC_CT", 1), _p("STAD", "")]


def status(sap: Sap, now: dt.datetime) -> dict:
    """What is scheduled now: the tool, future scheduler jobs, today's daily run, the watchdog."""
    tool = tool_of(sap)
    if not tool:
        return {"tool": None, "can_schedule": False,
                "why": "ST-PI snapshot monitoring (/SDF/SMON or /SDF/MON) is not installed"}
    t = TOOLS[tool]
    jobs = sap.read("TBTCO", ["JOBNAME", "JOBCOUNT", "STATUS", "SDLSTRTDT", "SDLSTRTTM", "PERIODIC", "PRDDAYS",
                              "SDLUNAME"],
                    f"JOBNAME EQ '{t['job']}' AND SDLSTRTDT GE '{now:%Y%m%d}'", rowcount=200)
    future = [j for j in jobs if (j.get("STATUS") or "").strip() in FUTURE]
    daily = [j for j in future if (j.get("PERIODIC") or "").strip() == "X"]
    running_today = [j for j in jobs if (j.get("STATUS") or "").strip() in ("R", "F")
                     and j.get("SDLSTRTDT") == f"{now:%Y%m%d}"]
    runs = sap.read(t["runs"], ["DESCRIPTION", "ST_DAT", "ST_TIM", "ENDDAT", "ENDTIM", "FREQU"],
                    f"ENDDAT GE '{now:%Y%m%d}'", rowcount=200)
    today = [r for r in runs if r.get("ST_DAT") == f"{now:%Y%m%d}" or r.get("ENDDAT") == f"{now:%Y%m%d}"]
    daily_run = next((r for r in today if "Daily Monitoring" in (r.get("DESCRIPTION") or "")), None)
    watchdog = sap.read("TBTCO", ["JOBNAME", "STATUS"], f"JOBNAME EQ '{t['watchdog']}' AND "
                        f"SDLSTRTDT GE '{now:%Y%m%d}'", rowcount=50)
    nxt = min((_smon_dt(j["SDLSTRTDT"], j["SDLSTRTTM"]) for j in daily), default=None,
              key=lambda x: x or dt.datetime.max)

    def run_view(r):
        if not r:
            return None
        s, e = _smon_dt(r["ST_DAT"], r["ST_TIM"]), _smon_dt(r["ENDDAT"], r["ENDTIM"])
        return {"description": r.get("DESCRIPTION", ""), "start": s.isoformat(sep=" ") if s else "",
                "end": e.isoformat(sep=" ") if e else "", "interval_s": int(float(r.get("FREQU") or 0) or 0),
                "running": bool(s and e and s <= now <= e)}
    return {"tool": tool, "can_schedule": jobs_enabled(), "why": "" if jobs_enabled() else
            "Job scheduling is switched off in Sentinel (AHC_NO_JOBS=1)",
            "daily_scheduled": bool(daily), "next_start": nxt.isoformat(sep=" ") if nxt else None,
            "daily_jobs": [{"jobcount": j["JOBCOUNT"], "status": j["STATUS"], "user": j.get("SDLUNAME", "")}
                           for j in daily],
            "today": run_view(daily_run), "other_runs_today": [run_view(r) for r in today if r is not daily_run],
            "today_job": bool(running_today), "watchdog": any((w.get("STATUS") or "").strip() in FUTURE + ("R",)
                                                             for w in watchdog),
            "job_name": t["job"]}


def _delete(sap: Sap, job: str, count: str) -> None:
    """Best effort: a half-built job must not stay behind (an unreleased job has no start date, so
    status() would not see it and every retry would add one more)."""
    try:
        sap.call("BAPI_XBP_JOB_DELETE", JOBNAME=job, JOBCOUNT=count, EXTERNAL_USER_NAME=EXT_USER)
    except RfcSoapError:
        pass


def _job(sap: Sap, call, tool: str, steps: list[tuple[str, list[dict]]]) -> str:
    t = TOOLS[tool]
    for program, _ in steps:
        if program not in SCHED_REPORTS:
            raise ValueError(f"report {program} is not on the scheduling allowlist")
    key = dict(JOBNAME=t["job"], EXTERNAL_USER_NAME=EXT_USER)
    count = (call("BAPI_XBP_JOB_OPEN", **key).get("JOBCOUNT") or "").strip()
    k = dict(key, JOBCOUNT=count)
    try:
        for program, sel in steps:
            call("BAPI_XBP_JOB_ADD_ABAP_STEP", ABAP_PROGRAM_NAME=program, SELINFO=sel, **k)
        call("BAPI_XBP_JOB_CLOSE", **k)
    except (RfcSoapError, ValueError):
        _delete(sap, t["job"], count)
        raise
    return count


_LOCKS: dict[str, threading.Lock] = defaultdict(threading.Lock)


def schedule(sap: Sap, now: dt.datetime, user: str, interval_s: int = 10, keep_days: int = 14,
             sleep: Callable[[float], None] = time.sleep, wait_s: float = 60.0, lock_key: str = "") -> dict:
    """Start daily monitoring today (unless it already runs) and every day from tomorrow 00:00.
    One scheduling per system at a time: a second click while the first runs is refused, otherwise both
    would see "not scheduled" and create the jobs twice."""
    lock = _LOCKS[lock_key]
    if not lock.acquire(blocking=False):
        raise JobError("Scheduling is already in progress for this system", kind="refused",
                       function="BAPI_XBP_JOB_OPEN")
    try:
        return _schedule(sap, now, user, interval_s, keep_days, sleep, wait_s)
    finally:
        lock.release()


def _schedule(sap: Sap, now: dt.datetime, user: str, interval_s: int, keep_days: int,
              sleep: Callable[[float], None], wait_s: float) -> dict:
    if not jobs_enabled():
        raise JobError("Job scheduling is switched off in Sentinel (AHC_NO_JOBS=1)", kind="refused",
                       function="BAPI_XBP_JOB_OPEN")
    if not 1 <= interval_s <= 600 or not 1 <= keep_days <= 366:
        raise ValueError("interval must be 1–600 s and retention 1–366 days")
    before = status(sap, now)
    tool = before["tool"]
    if not tool:
        raise ValueError(before["why"])
    t = TOOLS[tool]
    call = lambda fm, **kw: _check(fm, sap.call(fm, **kw))  # noqa: E731
    done: list[str] = []
    messages: list[str] = []
    call("BAPI_XMI_LOGON", EXTCOMPANY="SENTINEL", EXTPRODUCT="SENTINEL", INTERFACE="XBP", VERSION="3.0")
    try:
        if not before["today"] and not before["today_job"]:
            # /SDF/SMON_COLLECTOR_START only starts collectors whose run has begun (st_tim ≤ sy-uzeit): a
            # start time in the future left F4H's first run idle until the watchdog's first round an hour later
            st = (now - dt.timedelta(minutes=1)).strftime("%H%M%S") if now.hour or now.minute else "000000"
            if st < DAY_END:
                count = _job(sap, call, tool, [(t["start"], selinfo(tool, user, interval_s, keep_days, st, DAY_END)),
                                               (t["reorg"], [])])
                try:
                    call("BAPI_XBP_JOB_START_IMMEDIATELY", JOBNAME=t["job"], JOBCOUNT=count,
                         EXTERNAL_USER_NAME=EXT_USER)
                except RfcSoapError:            # e.g. no S_BTCH_JOB RELE: the closed job would stay unreleased
                    _delete(sap, t["job"], count)
                    raise
                st_job, waited = "", 0.0
                while waited < wait_s:
                    sleep(1.0)
                    waited += 1.0
                    st_job = (call("BAPI_XBP_JOB_STATUS_GET", JOBNAME=t["job"], JOBCOUNT=count,
                                   EXTERNAL_USER_NAME=EXT_USER).get("STATUS") or "").strip()
                    if st_job in ("F", "A"):
                        break
                log = _joblog(sap, t["job"], count)
                if st_job == "A":
                    raise JobError(f"today's {t['job']} job was cancelled: {log or 'see SM37'}", kind="fault",
                                   function="BAPI_XBP_JOB_STATUS_GET")
                done.append(f"Started today's daily monitoring ({st[:2]}:{st[2:4]}–23:59, every {interval_s} s)"
                            + ("" if st_job == "F" else f" — job still {st_job or 'starting'}, check SM37"))
                messages += [m for m in log.split(" | ") if m] if log else []
        if not before["daily_scheduled"]:
            count = _job(sap, call, tool, [(t["start"], selinfo(tool, user, interval_s, keep_days, DAY_START,
                                                                 DAY_END)), (t["reorg"], [])])
            tomorrow = (now + dt.timedelta(days=1)).date()
            try:
                # F4H: without MASK-STARTCOND = 'X' the start fields are ignored ("Invalid start date");
                # SOAP RFC takes ISO dates/times
                call("BAPI_XBP_JOB_HEADER_MODIFY", JOBNAME=t["job"], JOBCOUNT=count, EXTERNAL_USER_NAME=EXT_USER,
                     JOB_HEADER={"SDLSTRTDT": tomorrow.isoformat(), "SDLSTRTTM": "00:00:00", "PERIODIC": "X",
                                 "PRDDAYS": "1"}, MASK={"STARTCOND": "X"}, DONT_RELEASE="")
            except RfcSoapError:
                _delete(sap, t["job"], count)        # never leave a half-built, unscheduled job behind
                raise
            done.append(f"Scheduled {t['job']} every day from {tomorrow:%d.%m.%Y} at 00:00")
    finally:
        try:
            sap.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")
        except RfcSoapError:
            pass
    after = status(sap, now)
    if not done:
        done.append("Daily monitoring was already scheduled — nothing changed")
    if not after["daily_scheduled"]:
        messages.append(f"{t['job']} is not visible as a released periodic job — check SM37")
    return {"tool": tool, "done": done, "messages": messages, "status": after}


def unschedule(sap: Sap, now: dt.datetime) -> dict:
    """Delete the future daily scheduler jobs. A run already collecting today ends at its end time."""
    st = status(sap, now)
    if not st["tool"]:
        raise ValueError(st["why"])
    t = TOOLS[st["tool"]]
    call = lambda fm, **kw: _check(fm, sap.call(fm, **kw))  # noqa: E731
    deleted = 0
    call("BAPI_XMI_LOGON", EXTCOMPANY="SENTINEL", EXTPRODUCT="SENTINEL", INTERFACE="XBP", VERSION="3.0")
    try:
        for j in st["daily_jobs"]:
            call("BAPI_XBP_JOB_DELETE", JOBNAME=t["job"], JOBCOUNT=j["jobcount"], EXTERNAL_USER_NAME=EXT_USER)
            deleted += 1
    finally:
        try:
            sap.call("BAPI_XMI_LOGOFF", INTERFACE="XBP")
        except RfcSoapError:
            pass
    note = (f"Today's run continues until {st['today']['end'][11:16]} — stop it in /SDF/{st['tool']} "
            "(Stop Analysis) if needed." if st["today"] and st["today"]["running"] else "")
    return {"tool": st["tool"], "deleted": deleted, "note": note, "status": status(sap, now)}


def _joblog(sap: Sap, job: str, count: str) -> str:
    try:
        rows = as_rows(sap.call("BAPI_XBP_JOB_JOBLOG_READ", JOBNAME=job, JOBCOUNT=count, EXTERNAL_USER_NAME=EXT_USER,
                                JOB_PROTOCOL=[]).get("JOB_PROTOCOL"))
    except RfcSoapError:
        return ""
    keep = [(r.get("TEXT") or "").strip() for r in rows]
    keep = [k for k in keep if k and not k.startswith(("Job ", "Step 00"))]
    return " | ".join(keep)[:400]
