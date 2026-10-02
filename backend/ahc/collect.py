"""D-051 collectors: pull every Automated Health Check tab straight from SAP
over SOAP RFC and decide OK / Not OK with the same rules SAP's AHC applies.

Sources were confirmed on F4H by ahc/probe.py (see SKILL.md D-051). Rules:
  - Never a false OK. When a source is unavailable or a rule cannot be
    evaluated (no data, no field), the Monitors row ticks NEITHER box and the
    evidence says why — the dashboard then shows "No evidence".
  - Every tab is emitted with the sample's exact headers (ahc/layout.json),
    even when it has no rows, so the workbook structure never drifts.
  - A collector failure never fails the run: it becomes that tab's evidence.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

from engine.rfc.soap_client import RfcSoapError

from .hana import HanaViaSm69
from .sap import Sap, as_rows, first, in_list
from .sapctl import SapControlViaSm69, parse_server_port
from .writer import BODY, CYAN, GREEN, LAYOUT, RED, Block, TextTab, Verdict
from .xbp import jobs_enabled, run_report

DASH = "-" * 100
WEEK_NONE = "Not collected"


def cols(sheet: str, i: int = 0) -> list[str]:
    s = next(x for x in LAYOUT["sheets"] if x["name"] == sheet)
    return list(s["tables"][i]) if i < len(s["tables"]) else []


@dataclass
class Ctx:
    sap: Sap
    sid: str
    client: str
    now: dt.datetime
    servers: list[dict] = field(default_factory=list)   # {name, host, active, services}
    hana: Optional[HanaViaSm69] = None
    notes: list[str] = field(default_factory=list)

    @property
    def since(self) -> dt.datetime:
        return self.now - dt.timedelta(hours=24)

    @property
    def central(self) -> str:
        return self.servers[0]["name"] if self.servers else ""

    def sc(self) -> str:
        return f"{self.sid} {self.client}"


# ── formatting helpers ────────────────────────────────────────────────────

def d8(s: str) -> Optional[dt.date]:
    s = re.sub(r"\D", "", s or "")[:8]
    try:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8])) if len(s) == 8 and s != "00000000" else None
    except ValueError:
        return None


def ts(date8: str, time6: str = "") -> Optional[dt.datetime]:
    d = d8(date8)
    if not d:
        return None
    t = re.sub(r"\D", "", time6 or "")[:6].ljust(6, "0")
    try:
        return dt.datetime(d.year, d.month, d.day, int(t[:2]), int(t[2:4]), int(t[4:6]))
    except ValueError:
        return dt.datetime(d.year, d.month, d.day)


def stamp(s: str) -> Optional[dt.datetime]:
    """YYYYMMDDhhmmss... (RQCRETIME, VBDATE, SLGDATTIM, timestamps)."""
    s = re.sub(r"\D", "", s or "")
    return ts(s[:8], s[8:14]) if len(s) >= 8 else None


def dot(d: Optional[dt.datetime], time: bool = False) -> str:
    return "" if not d else d.strftime("%d.%m.%Y %H:%M:%S" if time else "%d.%m.%Y")


def slash(d: Optional[dt.datetime]) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def hms(d: Optional[dt.datetime]) -> str:
    return d.strftime("%H:%M:%S") if d else ""


def eu(v: float, decimals: int = 2) -> str:
    """European number format, as SAP's ST06 export ("0,16", "20.479")."""
    s = f"{v:,.{decimals}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def to_int(s, default: int = 0) -> int:
    try:
        return int(str(s).strip() or default)
    except ValueError:
        return default


def to_float(s, default: float = 0.0) -> float:
    try:
        return float(str(s).strip() or default)
    except ValueError:
        return default


def none_tab(sheet: str, title: str, subtitle: str, why: str, merge: int = 3) -> list[Block]:
    blocks = [Block(columns=cols(sheet, i), title=title if i == 0 else None,
                    subtitle=subtitle if i == 0 else None, subtitle_merge=merge)
              for i in range(max(1, len(next(x for x in LAYOUT["sheets"] if x["name"] == sheet)["tables"])))]
    return blocks


Result = tuple[object, Verdict]

# ── servers (shared by SM51, RZ03, SMLG, SM66, ...) ───────────────────────

MSGTYPES = [(1, "Dialog"), (8, "Batch"), (2, "Update"), (32, "Upd2"), (16, "Spool"), (128, "ICM"),
            (4, "Enqueue"), (64, "ATP")]


def load_servers(x: Ctx) -> None:
    r = x.sap.call("TH_SERVER_LIST", LIST=[])
    out = []
    for s in as_rows(r.get("LIST")):
        try:
            mask = base64.b64decode(s.get("MSGTYPES") or "")[0]
        except Exception:  # noqa: BLE001
            mask = 0
        try:
            state = base64.b64decode(s.get("STATE") or "")[0]
        except Exception:  # noqa: BLE001
            state = 1
        out.append({"name": s.get("NAME", ""), "host": s.get("HOST", ""),
                    "services": " ".join(n for b, n in MSGTYPES if mask & b),
                    "active": state == 1})
    x.servers = out


def sysnr(inst: str) -> str:
    m = re.search(r"_(\d{2})$", inst or "")
    return m.group(1) if m else "00"


# ── collectors ────────────────────────────────────────────────────────────

def c_sm51(x: Ctx) -> Result:
    rows = [[s["name"], s["host"], s["services"], "Active" if s["active"] else "Not active"] for s in x.servers]
    down = [s["name"] for s in x.servers if not s["active"]]
    v = Verdict(not down, "All Servers are active" if not down else
                f"{len(down)} Server(s) not active: {', '.join(down)}")
    return [Block(cols("SM51"), rows, "SM51", f"{len(rows)} AS Instance of SAP System {x.sc()}")], v


_PRIO = {"RA": ("Very High Priority", "01"), "RE": ("Very High Priority", "01"),
         "GR": ("Warning", "04"), "WH": ("Information", "08")}
_TASK = {"DI": "DIA", "BT": "BTC", "UP": "UPD", "U2": "UP2", "V2": "UP2", "SP": "SPO", "DP": "DP",
         "GW": "GW", "MS": "MS", "IC": "ICM", "EN": "ENQ", "IG": "IGS"}


def _fill(template: str, data: str) -> str:
    if "&" in data:
        params = {m.group(1).lower(): m.group(2).strip()
                  for m in re.finditer(r"&([a-zA-Z])(.*?)(?=&[a-zA-Z]|$)", data)}
    else:
        toks = [t for t in re.split(r"\s{2,}", data.strip()) if t]
        params = {chr(97 + i): t for i, t in enumerate(toks)}
    out = re.sub(r"&([a-zA-Z])", lambda m: params.get(m.group(1).lower(), ""), template)
    return re.sub(r"\s+", " ", out).strip()


def c_sm21(x: Ctx) -> Result:
    entries = []
    for s in x.servers or [{"name": x.central}]:
        r = x.sap.call("RSLG_READ_SLG", IM_POS=0, IM_WRAPCOUNT=0,
                       IM_FILTER_DATETIME_FROM=x.since.strftime("%Y%m%d%H%M%S"),
                       IM_FILTER_DATETIME_TO=x.now.strftime("%Y%m%d%H%M%S"),
                       IM_INSTANCENAME=s["name"], T_SYSLOG_ENTRIES=[])
        entries += [(s["name"], e) for e in as_rows(r.get("T_SYSLOG_ENTRIES"))]
    ids = sorted({e["SLGTYPE"][1:4] for _, e in entries if len(e.get("SLGTYPE", "")) >= 4})
    areas = sorted({i[:2] for i in ids})
    texts, prio = {}, {}
    for k in range(0, len(areas), 40):
        chunk = in_list(areas[k:k + 40])
        for t in x.sap.read("TSL1T", ["AREA", "SUBID", "TXT"], f"SPRAS EQ 'E' AND AREA IN {chunk}"):
            texts[t["AREA"] + t["SUBID"]] = t["TXT"]
        for t in x.sap.read("TSL1D", ["AREA", "SUBID", "MONBEW"], f"AREA IN {chunk}"):
            prio[t["AREA"] + t["SUBID"]] = t["MONBEW"]
    rows, high, parent = [], 0, None
    entries.sort(key=lambda ie: ie[1].get("SLGDATTIM", ""))
    for inst, e in entries:
        kind, mid = e.get("SLGTYPE", " ")[0], e.get("SLGTYPE", "    ")[1:4]
        when = stamp(e.get("SLGDATTIM", ""))
        proc = e.get("SLGPROC", "")
        wp = proc[5:10] if proc[5:10].isdigit() else ""
        if kind == "p" and parent is not None:          # parameter / continuation line
            p_mid, p_pr, p_sev = parent
            text = "> " + re.sub(r"&[a-zA-Z]", " ", e.get("SLGDATA", "")).strip()
            rows.append([slash(when), hms(when), inst, _TASK.get(proc[-2:], proc[-2:]),
                         f"{to_int(wp):03d}" if wp else "", e.get("SLGMAND", ""), e.get("SLGUSER", ""),
                         p_pr, p_sev, p_mid, text])
            continue
        pr, sev = _PRIO.get(prio.get(mid, "WH"), ("Information", "08"))
        parent = (mid, pr, sev)
        if sev == "01":
            high += 1
        rows.append([slash(when), hms(when), inst, _TASK.get(proc[-2:], proc[-2:]),
                     f"{to_int(wp):03d}" if wp else "", e.get("SLGMAND", ""), e.get("SLGUSER", ""),
                     pr, sev, mid, _fill(texts.get(mid, mid), e.get("SLGDATA", ""))])
    v = Verdict(high == 0, f"{high}  High Priority Syslog message(s) are found in last 24 hours period"
                if high else "No High Priority Syslog message found in last 24 hours period")
    return [Block(cols("SM21"), rows, "SM21", f"Syslog Messages in {x.sc()}")], v


def _flist(s: str) -> dict:
    out, i = {}, 0
    while i + 5 <= len(s):
        key, ln = s[i:i + 2], s[i + 2:i + 5]
        if not ln.isdigit():
            break
        n = int(ln)
        out.setdefault(key, s[i + 5:i + 5 + n])
        i += 5 + n
    return out


def c_st22(x: Ctx) -> Result:
    since = x.since
    raw = x.sap.read("SNAP_BEG", ["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "MODNO", "SEQNO", "XHOLD", "FLIST"],
                     f"DATUM GE '{since:%Y%m%d}' AND SEQNO EQ '000'")
    rows = []
    for r in raw:
        when = ts(r["DATUM"], r["UZEIT"])
        if not when or when < since:
            continue
        f = _flist(r["FLIST"])
        rows.append([dot(when), hms(when), r["AHOST"], r["UNAME"], r["MANDT"], r["XHOLD"],
                     f.get("FC", ""), f.get("EX", ""), f.get("AP", ""), r["MODNO"].lstrip("0") or "0", f.get("TD", "")])
    v = Verdict(not rows, "No ABAP Runtime Errors were found" if not rows
                else f"{len(rows)} ABAP Runtime Error(s) found in last 24 hours")
    return [Block(cols("ST22"), rows, "ST22", f"List of Selected Runtime Errors in {x.sc()}")], v


def c_sm13(x: Ctx) -> Result:
    raw = x.sap.read("VBHDR", ["VBMANDT", "VBUSR", "VBDATE", "VBTCODE", "VBRC", "VBSTATE", "VBNAME"])
    rows = []
    for r in raw:
        when = stamp(r["VBDATE"])
        err = r["VBRC"].strip() not in ("", "0", "0000000000") or r["VBSTATE"].strip() not in ("", "0", "000")
        if err and when and when >= x.since:
            rows.append([r["VBMANDT"], r["VBUSR"], dot(when), hms(when), r["VBTCODE"],
                         "Error" if r["VBRC"].strip() not in ("", "0") else f"State {r['VBSTATE']}", r["VBNAME"]])
    v = Verdict(not rows, f"{len(rows)} Update record found")
    return [Block(cols("SM13"), rows, "SM13", f"Update Requests in {x.sc()}")], v


def c_sm12(x: Ctx, limit_h: int = 5) -> Result:
    r = x.sap.call("ENQUEUE_READ", GCLIENT="", GNAME="", GUNAME="", ENQ=[])
    rows, old = [], 0
    for e in as_rows(r.get("ENQ")):
        when = ts(e.get("GTDATE", ""), e.get("GTTIME", ""))
        if when and (x.now - when).total_seconds() >= limit_h * 3600:
            old += 1
        rows.append([e.get("GCLIENT", ""), e.get("GUNAME", ""),
                     f"{slash(when)} --{hms(when)}" if when else "", e.get("GMODE", ""), e.get("GNAME", ""),
                     e.get("GARG", ""), "" if e.get("GUSE", "0") in ("0", "") else e.get("GUSE", ""),
                     e.get("GUSEVB", "")])
    v = Verdict(old == 0, f"{old}  entries are locked for more than {limit_h} hours" if old
                else f"No entries are locked for more than {limit_h} hours")
    return [Block(cols("SM12"), rows, "SM12", f"Lock Entry List in {x.sc()}")], v


def c_sp01(x: Ctx) -> Result:
    raw = x.sap.read("TSP01", ["RQIDENT", "RQCLIENT", "RQOWNER", "RQCRETIME", "RQERROR", "RQPJSERR",
                               "RQPJHERR", "RQTITLE"], f"RQCRETIME GE '{x.since:%Y%m%d%H%M%S}'")
    rows = []
    for r in raw:
        if r["RQERROR"].strip() or to_int(r["RQPJSERR"]) or to_int(r["RQPJHERR"]):
            when = stamp(r["RQCRETIME"])
            rows.append([r["RQIDENT"], r["RQCLIENT"], r["RQOWNER"], dot(when), hms(when), "Error", r["RQTITLE"]])
    v = Verdict(not rows, "OK" if not rows else f"{len(rows)} spool request(s) with errors in last 24 hours")
    return [Block(cols("SP01"), rows, "SP01", f"Output Controller: List of Spool Requests in {x.sc()}")], v


_JOBSTAT = {"A": "Canceled", "F": "Finished", "R": "Active", "S": "Released", "Y": "Ready",
            "P": "Scheduled", "Z": "Released/Susp."}


def c_sm37(x: Ctx, delay_s: int = 300) -> Result:
    y = x.since
    raw = x.sap.read("TBTCO", ["JOBNAME", "SDLUNAME", "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME",
                               "SDLSTRTDT", "SDLSTRTTM", "STATUS"],
                     f"( STRTDATE GE '{y:%Y%m%d}' OR ENDDATE GE '{y:%Y%m%d}' )")
    delayed, cancelled = [], []
    for r in raw:
        start, end = ts(r["STRTDATE"], r["STRTTIME"]), ts(r["ENDDATE"], r["ENDTIME"])
        plan = ts(r["SDLSTRTDT"], r["SDLSTRTTM"])
        status = _JOBSTAT.get(r["STATUS"], r["STATUS"])
        if r["STATUS"] == "A" and (end or start) and (end or start) >= y:
            dur = int((end - start).total_seconds()) if end and start else 0
            cancelled.append([r["JOBNAME"], r["SDLUNAME"], dot(start), hms(start), str(dur), status])
        elif start and plan and start >= y and (start - plan).total_seconds() > delay_s:
            delayed.append([r["JOBNAME"], r["SDLUNAME"], dot(start), hms(start),
                            str(int((start - plan).total_seconds())), status])
    v = Verdict(not cancelled, "OK" if not cancelled else
                f"{len(cancelled)} job(s) cancelled in last 24 hours. Check SM37 tab")
    return [Block(cols("SM37", 0), delayed, "SM37", f"Valid (expires soon) {x.sc()}", lead_blank=True),
            Block(cols("SM37", 1), cancelled, "SM37", f"Cancelled Jobs in {x.sc()}", lead_blank=True)], v


def c_srt_moni(x: Ctx) -> Result:
    raw = x.sap.read("SRT_MMASTER", ["MESSAGE_STATE", "CREATED_AT", "OB_IF_NAME", "IB_IF_NAME",
                                     "LOCATION", "ADAPTER_TYPE", "CREATED_BY", "ERROR_COUNTER"],
                     "ERROR_COUNTER NE '000'")
    rows = []
    for r in raw:
        when = stamp(r["CREATED_AT"])
        if when and when >= x.since:
            rows.append([r["MESSAGE_STATE"], dot(when), hms(when), "", r["OB_IF_NAME"], "", r["IB_IF_NAME"],
                         "", f"{to_int(r['ERROR_COUNTER'])} error(s)", r["LOCATION"], r["ADAPTER_TYPE"],
                         r["CREATED_BY"], "", "", "", ""])
    v = Verdict(not rows, "OK" if not rows else f"{len(rows)} erroneous web service message(s) in last 24 hours")
    return [Block(cols("SRT_MONI"), rows, "SRT_MONI",
                  f"Erroneously Processed Web Service Message Monitor Log(s) in {x.sc()}")], v


def _usr_type(u: dict) -> str:
    if (u.get("RFC_TYPE") or "").strip():
        return "RFC"
    return {"2": "System", "4": "GUI", "32": "RFC", "16": "HTTP", "128": "Plugin"}.get(u.get("TYPE", ""), u.get("TYPE", ""))


def _users(x: Ctx) -> list[dict]:
    return as_rows(x.sap.call("TH_USER_LIST", USRLIST=[]).get("USRLIST"))


def c_al08(x: Ctx, users: list[dict]) -> Result:
    inst = x.central
    rows = [[inst, u.get("MANDT", ""), u.get("BNAME", ""), u.get("TERM", ""), u.get("TCODE", ""),
             f"{x.now:%d.%m.%Y} {u.get('ZEIT', '')[:2]}:{u.get('ZEIT', '')[2:4]}:{u.get('ZEIT', '')[4:6]}",
             u.get("EXTMODI", ""), "", _usr_type(u), "", "", ""] for u in users]
    abap = sum(to_int(u.get("EXTMODI")) for u in users)
    note = "" if len(x.servers) <= 1 else f" (instance {inst} only; TH_USER_LIST is per instance)"
    v = Verdict(True, f"{len(rows)} user sessions with {abap} ABAP sessions{note}")
    return [Block(cols("AL08"), rows, "AL08", f"System-Wide List of User Session(s) in SAP System {x.sc()}")], v


def c_sm04(x: Ctx, users: list[dict]) -> Result:
    rows = [[u.get("MANDT", ""), u.get("BNAME", ""), u.get("TERM", ""), u.get("TCODE", ""),
             f"{x.now:%d.%m.%Y} {u.get('ZEIT', '')[:2]}:{u.get('ZEIT', '')[2:4]}:{u.get('ZEIT', '')[4:6]}",
             _usr_type(u), u.get("EXTMODI", ""), "", "", "", ""] for u in users]
    v = Verdict(None, f"{len(rows)} user session(s) listed; session priority is not exposed by "
                      "TH_USER_LIST, so high-priority sessions cannot be checked over SOAP RFC")
    return [Block(cols("SM04"), rows, "SM04", f"User Sessions for AS Instance {x.central}")], v


def _wps(x: Ctx, server: str) -> list[dict]:
    return as_rows(x.sap.call("TH_WPINFO", SRVNAME=server, WPLIST=[]).get("WPLIST"))


def _wp_row(w: dict) -> list[str]:
    return [w.get("WP_INDEX", ""), w.get("WP_TYP", ""), w.get("WP_PID", ""), w.get("WP_STATUS", ""),
            w.get("WP_WAITING", ""), w.get("WP_DUMPS", ""), w.get("WP_SEM", ""), "", w.get("WP_CPU", ""),
            w.get("WP_ELTIME", ""), "", "", w.get("WP_REPORT", ""), w.get("WP_MANDT", ""),
            w.get("WP_BNAME", ""), w.get("WP_ACTION", "")]


def _wp_verdict(all_wps: list[dict], tab: str) -> Verdict:
    bad = [w for w in all_wps if w.get("WP_STATUS", "") in ("Stopped", "Ended")
           or "PRIV" in (w.get("WP_WAITING", "") or "").upper()]
    ev = f"Total Number of Active Work Processes {len(all_wps)}  . Check {tab} Tab"
    return Verdict(not bad, ev if not bad else ev + f" ({len(bad)} stopped/ended or in PRIV mode)")


def c_sm50(x: Ctx) -> Result:
    wps = _wps(x, x.central)
    return [Block(cols("SM50"), [_wp_row(w) for w in wps], "SM50",
                  f"Work Processes of {x.sid} Instance {x.central}")], _wp_verdict(wps, "SM50")


def c_sm66(x: Ctx) -> Result:
    rows, allw = [], []
    for s in x.servers:
        wps = _wps(x, s["name"])
        allw += wps
        rows += [[s["name"]] + _wp_row(w)[:15] for w in wps]
    return [Block(cols("SM66"), rows, "SM66",
                  f"Work Processes of All Active AS Instances of System {x.sid}")], _wp_verdict(allw, "SM66")


def c_sost(x: Ctx) -> Result:
    raw = x.sap.read("SOES", ["SNDDAT", "SNDTIM", "SNDART", "SCDATE", "SCTIME", "DELIVER", "NOT_DELI",
                              "STATUS", "MSGID", "MSGTY", "MSGV1"], f"SNDDAT GE '{x.since:%Y%m%d}'")
    waiting = sent = error = 0
    rows = []
    for r in raw:
        when = ts(r["SNDDAT"], r["SNDTIM"])
        if not when or when < x.since:
            continue
        if r["MSGTY"] in ("E", "A") or r["NOT_DELI"].strip():
            error += 1
            state = "Error"
        elif r["DELIVER"].strip() or r["STATUS"].strip() in ("73", "70"):
            sent += 1
            continue
        else:
            waiting += 1
            state = "Waiting"
        sc = ts(r["SCDATE"], r["SCTIME"])
        rows.append([r["MSGV1"], "", dot(sc), hms(sc), state, f"{r['MSGID']} {r['MSGTY']}".strip()])
    v = Verdict(error == 0, f"Email delivery status: {waiting}  Waiting {sent}  Sent and {error}  Error")
    return [Block(cols("SOST"), rows, "SOST", f"Expires soon {x.sc()}")], v


_BUFFER_LABEL = {"PXA": "program", "CUA": "CUA", "PRES": "Screen", "CALE": "Calendar", "OTR": "OTR",
                 "TABL": "Buffer", "TABLP": "Buffer", "EIBUF": "Export/import", "ESM": "Exp./ Imp. SHM",
                 "CTLGA": "Nametab (NTAB)", "IRBD": "Nametab (NTAB) initial", "FTAB": "Nametab field descr.",
                 "SNTAB": "Nametab short NTAB", "TTAB": "Nametab table definition"}


def c_st02(x: Ctx) -> Result:
    s = x.sap.call("SAPTUNE_GET_SUMMARY_STATISTIC")
    bufrows, low, swaps = [], [], 0
    try:
        names = [first(b, "NAME", "BUFFERNAME", "value") for b in
                 as_rows(x.sap.call("SAPTUNE_BUFFER_NAMES", BUFFERNAMES=[]).get("BUFFERNAMES"))]
    except RfcSoapError:
        names = []
    seen = set()
    for n in [n for n in names if n]:
        try:
            b = x.sap.call("SAPTUNE_BUFFER_STATISTICS", BUFFERNAME=n).get("BUFFER_STATISTIC") or {}
        except RfcSoapError:
            continue
        label = _BUFFER_LABEL.get(n, n)
        if label in seen:
            continue
        seen.add(label)
        req, hit_n = to_float(b.get("REQUEST")), to_float(b.get("HIT"))
        alloc, avail = to_float(b.get("ALLOC_SIZE")), to_float(b.get("AVAIL_SIZE"))
        dmax, dact = to_float(b.get("MAX_OBJCTS")), to_float(b.get("ACT_OBJCTS"))
        hit = hit_n / req * 100 if req else None
        sw = to_int(b.get("SWAP"))
        swaps += sw
        if hit is not None and hit < 97 and req > 1000:
            low.append(label)
        bufrows.append([label, f"{hit:.2f}" if hit is not None else "", f"{alloc:.0f}" if alloc else "",
                        f"{avail:.0f}" if alloc else "", f"{avail / alloc * 100:.2f}" if alloc else "",
                        f"{dmax:.0f}" if dmax else "", f"{dmax - dact:.0f}" if dmax else "",
                        f"{(dmax - dact) / dmax * 100:.2f}" if dmax else "", str(sw), b.get("DB_ACCESS", "")])

    def pct(u, t):
        return f"{to_float(u) / to_float(t) * 100:.2f}" if to_float(t) else ""
    eg, eu_, hp = s.get("EX_GLOB_MEM_USAGE") or {}, s.get("EXTENDED_MEMORY_USAGE") or {}, s.get("HEAP_MEMORY_USAGE") or {}
    pa, so = s.get("PAGING_AREA") or {}, s.get("SHR_OBJ_MEM_USAGE") or {}
    cc = s.get("SAP_CURSOR_CACHE_TOTAL") or {}
    mem = [["Ext. global memory", pct(eg.get("USED"), eg.get("TOTAL")), eg.get("USED", ""), eg.get("PEAK", ""),
            eg.get("TOTAL", ""), "0", "", "0.00"],
           ["Shared objects", pct(so.get("ALLOC"), to_float(so.get("ALLOC")) + to_float(so.get("FREE"))),
            so.get("ALLOC", ""), "0", str(to_int(so.get("ALLOC")) + to_int(so.get("FREE"))), "0", "", "0.00"],
           ["Extended memory", pct(eu_.get("USED"), eu_.get("TOTAL")), eu_.get("USED", ""), "",
            eu_.get("TOTAL", ""), "0", "IDs", cc.get("ID_HITR", "")],
           ["Heap memory", "", hp.get("USED", ""), hp.get("TOTAL", ""), "0", "0", "", "0.00"],
           ["Page area", "", pa.get("CURR_USED", ""), pa.get("MAX_USED", ""), pa.get("AREA_SIZE", ""),
            pa.get("FILE_SIZE", ""), "Statement", cc.get("STMT_HITR", "")]]
    ev = "" if not (low or swaps) else f"{len(low)} buffer(s) below 97% hit ratio, {swaps} swaps"
    v = Verdict(not (low or swaps), ev)
    if not bufrows:
        v = Verdict(True if not swaps else False, "Per-buffer statistics not available via SAPTUNE_BUFFER_STATISTICS; "
                    "SAP memory figures listed")
    title = f"Tune Summary ({x.central})"
    return [Block(cols("ST02", 0), bufrows, "ST02", title),
            Block(cols("ST02", 1), mem)], v


def c_smlg(x: Ctx, group_type: str = "") -> tuple[list[list[str]], list[list[str]], list[list[str]], list[str]]:
    where = f"GROUPTYPE EQ '{group_type}'" if group_type else "GROUPTYPE EQ ' '"
    raw = x.sap.read("RZLLITAB", ["CLASSNAME", "APPLSERVER", "GROUPTYPE", "IP_ADDRESS", "RESP_TIME", "USERS"], where)
    active = {s["name"] for s in x.servers if s["active"]}
    g1, bad = [], []
    for r in raw:
        ok = r["APPLSERVER"] in active
        g1.append([r["CLASSNAME"] or "SPACE", r["APPLSERVER"], "Green" if ok else "NOT Green"])
        if not ok:
            bad.append(r["APPLSERVER"])
    g2 = [[s["name"], "Green" if s["active"] else "NOT Green",
           next((r["RESP_TIME"].strip() for r in raw if r["APPLSERVER"] == s["name"]), ""), "",
           next((r["USERS"].strip() for r in raw if r["APPLSERVER"] == s["name"]), ""), "",
           f"{x.now:%H:%M:%S}", "", ""] for s in x.servers]
    g3 = []
    for grp in dict.fromkeys(r[0] for r in g1):
        cur = next((r for r in raw if (r["CLASSNAME"] or "SPACE") == grp and r["APPLSERVER"] in active), None)
        g3.append([grp, cur["APPLSERVER"] if cur else "", cur["IP_ADDRESS"] if cur else ""])
    return g1, g2, g3, bad


def _status_styles(rows, col):
    return {(i, col): (GREEN if r[col] == "Green" else RED) for i, r in enumerate(rows)}


def c_smlg_tab(x: Ctx) -> Result:
    g1, g2, g3, bad = c_smlg(x)
    v = Verdict(not bad, "All Instances are in Green status" if not bad else
                f"Instance {bad[0]} is not in Green status. Check SMLG tab.")
    return [Block(cols("SMLG", 0), g1, "SMLG", "CCMS Logon Groups and Load Distribution", styles=_status_styles(g1, 2)),
            Block(cols("SMLG", 1), g2, styles=_status_styles(g2, 1)),
            Block(cols("SMLG", 2), g3)], v


def c_rz12(x: Ctx) -> Result:
    g1, _, _, bad = c_smlg(x, "S")
    v = Verdict(not bad, "Instance(s) in Green status" if not bad else
                f"Instance {bad[0]} is not in Green status. Check RZ12 tab.")
    return [Block(cols("RZ12"), g1, "RZ12", "CCMS: RFC Server Group Maintenance", styles=_status_styles(g1, 2))], v


def c_st06(x: Ctx) -> Result:
    rows, worst = [], None
    for s in x.servers[:1] or [{"name": x.central}]:
        cpu = (as_rows(x.sap.call("GET_CPU_ALL", TF_CPU_ALL=[]).get("TF_CPU_ALL")) or [{}])[0]
        mem = (as_rows(x.sap.call("GET_MEM_ALL", TF_MEM_ALL=[]).get("TF_MEM_ALL")) or [{}])[0]
        cpu_rows = [("Average processes waiting (  1 min)", eu(to_float(cpu.get("LOAD1_AVG")) / 100), ""),
                    ("Average processes waiting (  5 min)", eu(to_float(cpu.get("LOAD5_AVG")) / 100), ""),
                    ("Average processes waiting ( 15 min)", eu(to_float(cpu.get("LOAD15_AVG")) / 100), ""),
                    ("Number of CPUs", str(to_int(cpu.get("NBR_CPU"))), ""),
                    ("User", str(to_int(cpu.get("USR_TOTAL"))), "%"),
                    ("System", str(to_int(cpu.get("SYS_TOTAL"))), "%"),
                    ("Idle", str(to_int(cpu.get("IDLE_TOTAL"))), "%"),
                    ("I/O wait", str(to_int(cpu.get("WAIT_TRUE"))), "%"),
                    ("Interrupts", eu(to_float(cpu.get("INT_SEC")), 0), "/s"),
                    ("Context switches", eu(to_float(cpu.get("CS_SEC")), 0), "/s")]
        kb = lambda k: eu(to_float(mem.get(k)) / 1024, 0)  # noqa: E731
        mem_rows = [("Physical memory", kb("PHYS_MEM"), "MB"), ("Free memory", kb("FREE_MEM"), "MB"),
                    ("Configured swap", kb("SWAP_CONF"), "MB"), ("Actual swap size", kb("SWAP_SIZE"), "MB"),
                    ("Free swap", kb("SWAP_FREE"), "MB"), ("Pages in", str(to_int(mem.get("IN_PG_SEC"))), "/s"),
                    ("Pages out", str(to_int(mem.get("OUT_PG_SEC"))), "/s")]
        for cat, items in (("CPU", cpu_rows), ("Memory", mem_rows)):
            for i, (d, val, unit) in enumerate(items):
                rows.append([s["name"], cat if i == 0 else "", d, val, unit])
        idle = to_int(cpu.get("IDLE_TOTAL"), 100)
        worst = min(worst, idle) if worst is not None else idle
    v = Verdict(worst is None or worst >= 20, "OK" if worst is None or worst >= 20
                else f"CPU utilization at {100 - worst}%. Check ST06 tab")
    return [Block(cols("ST06"), rows, "ST06", f"{x.sid} CPU and Memory Utilization")], v


def _cert_info(der_b64: str) -> Optional[tuple[str, dt.datetime]]:
    from cryptography import x509
    try:
        der = base64.b64decode(der_b64)
        c = x509.load_der_x509_certificate(der)
    except Exception:  # noqa: BLE001
        return None
    exp = getattr(c, "not_valid_after_utc", None) or c.not_valid_after
    return c.subject.rfc4514_string().replace(",", ", "), exp.replace(tzinfo=None)


def c_cert(x: Ctx, horizon: int = 42) -> Result:
    ids = as_rows(x.sap.call("SSFR_GET_ALL_STRUST_IDENTITIES", ET_STRUST_IDENTITIES=[], ET_BAPIRET2=[])
                  .get("ET_STRUST_IDENTITIES"))
    lines: list[tuple[str, Optional[str]]] = [
        (f"{x.now:%d.%m.%Y}      Overview of Installed Certificates", None), (DASH, None)]
    soon = []
    for i in ids:
        ident = {"PSE_CONTEXT": i.get("PSE_CONTEXT", ""), "PSE_APPLIC": i.get("PSE_APPLIC", "")}
        label = i.get("PSE_DESCRIPT") or ident["PSE_APPLIC"]
        lines.append((f"{label:<56}{ident['PSE_CONTEXT']} {ident['PSE_APPLIC']}", CYAN))
        try:
            own = x.sap.call("SSFR_GET_OWNCERTIFICATE", IS_STRUST_IDENTITY=ident, ET_BAPIRET2=[]).get("EV_CERTIFICATE")
        except RfcSoapError as exc:
            lines += [(f"SSF-Exist: {exc.message[:90]}", None), ("", None)]
            continue
        info = _cert_info(own) if own else None
        if not info:
            lines += [("SSF-Exist: The PSE does not exist", None), ("", None)]
            continue
        lines += [("", None), ("Own Certificate", None), (f"{info[0][:80]:<82}{info[1]:%d.%m.%Y}", None)]
        if (info[1] - x.now).days <= horizon:
            soon.append(info)
        try:
            lst = as_rows(x.sap.call("SSFR_GET_CERTIFICATELIST", IS_STRUST_IDENTITY=ident, ET_BAPIRET2=[])
                          .get("ET_CERTIFICATELIST"))
        except RfcSoapError:
            lst = []
        certs = [c for c in (_cert_info(first(r, "value", "LINE", "CERT")) for r in lst) if c]
        if certs:
            lines += [("", None), ("Certificate List", None)]
            for n, c in enumerate(certs, 1):
                lines.append((f"{n} = {c[0][:76]:<78}{c[1]:%d.%m.%Y}", None))
                if (c[1] - x.now).days <= horizon:
                    soon.append(c)
        lines += [("", None), (DASH, None), ("", None)]
    v = Verdict(not soon, f"Certificate will be expired in next {horizon} days. Please check Cert_Info Tab"
                if soon else f"No certificate expires in next {horizon} days")
    return TextTab("Cert_Info", f"Certificate Validity Info of {x.sc()}", "Certificate Info", lines), v


def _lv(s: str) -> list[str]:
    """SAPLIKEY encoding: repeated 4-digit length + value."""
    out, i = [], 0
    while i + 4 <= len(s) and s[i:i + 4].isdigit():
        n = int(s[i:i + 4])
        out.append(s[i + 4:i + 4 + n])
        i += 4 + n
    return out


_LIC_TYPE = {"P": "Perm", "T": "Temp"}


def _licenses_from_saplikey(x: Ctx) -> Optional[list[dict]]:
    """Installed licenses straight from table SAPLIKEY (what SLICENSE shows).

    LIKEY  = LK | SID | hardware key | product          (LC rows: last check, skipped)
    VALUE  = limit | type | signature | installation no | start | end | system no
    VALUE is an NCLOB, which RFC_READ_TABLE cannot return, so it is read with
    hdbsql (SM69); the HANA user needs SELECT on <schema>.SAPLIKEY. Verified
    field-by-field against SLICENSE on F4H (2026-10-01)."""
    if not (x.hana and x.hana.available()):
        return None
    sch = x.hana.query("SELECT TOP 1 SCHEMA_NAME FROM SYS.TABLES WHERE TABLE_NAME = 'SAPLIKEY' "
                       "ORDER BY SCHEMA_NAME")
    schema = sch[0]["SCHEMA_NAME"] if sch else ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", schema):
        return None
    rows = x.hana.table(["LIKEY", "VALUE"], f"FROM {schema}.SAPLIKEY "
                        "WHERE LIKEY LIKE '0002LK%' ORDER BY LIKEY", lobs=["VALUE"])
    out = []
    for r in rows:
        k, v = _lv(r.get("LIKEY", "")), _lv(r.get("VALUE", ""))
        if len(k) < 4 or len(v) < 7:
            continue
        out.append({"sid": k[1], "hwkey": k[2], "product": k[3], "limit": v[0], "type": v[1],
                    "instno": v[3], "start": d8(v[4]), "end": d8(v[5]), "sysno": v[6]})
    return out


def c_slicense(x: Ctx) -> Result:
    active_hw, fm_rows = "", []
    try:
        r = x.sap.call("/SDF/SLIC_READ_LICENSES", LICENSES=[])
        active_hw = (r.get("CUSTKEY") or "").strip()
        fm_rows = as_rows(r.get("LICENSES"))
    except RfcSoapError:
        pass
    lic, why = None, ""
    try:
        lic = _licenses_from_saplikey(x)
    except (RfcSoapError, ValueError) as exc:
        why = f"SAPLIKEY via hdbsql failed ({str(exc)[:120]})"
    if lic is None and fm_rows:
        lic = [{"sid": first(l, "SYSTEMID", "SYSID", "SID"), "hwkey": first(l, "HWKEY", "HARDWARE_KEY"),
                "product": first(l, "SWPRODUCT", "SW_PRODUCT", "PRODUCT"), "limit": first(l, "SWPRODLIMIT", "LIMIT"),
                "type": first(l, "TYPE"), "instno": first(l, "INSTNR", "INSTALLATION_NUMBER"),
                "start": d8(first(l, "BEGIN_DATE", "START_DATE")), "end": d8(first(l, "END_DATE", "ENDDATE", "VALID_TO")),
                "sysno": first(l, "SYSTEMNR", "SYSTEM_NO")} for l in fm_rows]
    title = f"SAP License Administration {x.sc()}"
    if not lic:
        why = why or ("no licenses in SAPLIKEY or from /SDF/SLIC_READ_LICENSES" if x.hana and x.hana.available()
                      else "license dates are in SAPLIKEY.VALUE (NCLOB), readable only via the SM69 hdbsql "
                           "command (" + (x.hana.unavailable_reason() if x.hana else "not configured") + ")")
        return [Block(cols("SLICENSE"), [], "SLICENSE", title)], Verdict(None, f"Not collected: {why}")
    today = x.now.date()
    rows = []
    for l in lic:
        if l["end"] and l["end"] < today:
            status = "Expired"
        elif active_hw and l["hwkey"] != active_hw:
            status = "Valid (inactive Hardware Key)"
        else:
            status = "Valid"
        rows.append((l["hwkey"] != active_hw if active_hw else False, l["product"],
                     ["", l["sid"], l["hwkey"], l["product"], l["limit"],
                      l["start"].strftime("%d/%m/%Y") if l["start"] else "",
                      l["end"].strftime("%d/%m/%Y") if l["end"] else "", _LIC_TYPE.get(l["type"], l["type"]),
                      l["instno"], l["sysno"], status]))
    rows = [r for *_, r in sorted(rows, key=lambda t: (t[0], t[1]))]   # active key first, as SAP lists them
    bad = [r for r in rows if not r[10].startswith("Valid")]
    v = Verdict(not bad, "At Least One System ID Status is Invalid. Check SLICENSE Tab" if bad else "OK")
    return [Block(cols("SLICENSE"), rows, "SLICENSE", title)], v


def c_scc4(x: Ctx) -> Result:
    role = {"P": "Production", "T": "Test", "C": "Customizing", "D": "Demo", "E": "Training/Education",
            "S": "SAP reference"}
    raw = x.sap.read("T000", ["MANDT", "MTEXT", "ORT01", "MWAER", "CCCATEGORY", "CHANGEDATE"])
    rows = [[r["MANDT"], role.get(r["CCCATEGORY"], r["CCCATEGORY"]), r["MWAER"], r["MTEXT"], r["ORT01"],
             dot(ts(r["CHANGEDATE"]))] for r in raw]
    return [Block(cols("SCC4"), rows, "SCC4", 'Display Vies "Client": Overview')], Verdict(True, "OK")


def _check_text(header_ok: bool, message: str, ok_style: str = GREEN) -> list[tuple[str, Optional[str]]]:
    box = "-" * 74
    return [(box, BODY), ("|SAP System Check" + " " * 56 + "|", CYAN), (box, BODY),
            (f"|{message:<72}|", ok_style if header_ok else RED), (box, BODY)]


def c_rz03(x: Ctx) -> Result:
    down = [s["name"] for s in x.servers if not s["active"]]
    msg = "no errors reported" if not down else f"instance(s) not active: {', '.join(down)}"
    return (TextTab("RZ03", "CCMS Control Panel: Display Server States and Alerts",
                    "Presentation, Control SAP Instances", _check_text(not down, msg, CYAN)),
            Verdict(not down, "All Servers are Active" if not down else f"{len(down)} Server(s) not active"))


def c_rz04(x: Ctx) -> Result:
    ops = x.sap.read("TPFBA", ["BANAME", "SYSNAME", "TYPE", "TXT"])
    dummy = [o for o in ops if o["BANAME"].upper() == "DUMMY"]
    msg = "no errors reported" if not dummy else \
        "dummy operation mode DUMMY in use (" + (dummy[0]["TXT"] or "self-configured") + ")"
    return (TextTab("RZ04", "CCMS: Edit Operation Modes and Instances", "Maintain SAP Instances",
                    _check_text(not dummy, msg)),
            Verdict(not dummy, "No Dummy Operation Mode found" if not dummy else
                    "Dummy Operation Mode found. Maintain operation modes in RZ04"))


def c_st11(x: Ctx) -> Result:
    home = x.sap.call("TH_GET_PARAMETER", PARAMETER_NAME="DIR_HOME").get("PARAMETER_VALUE", "")
    files = as_rows(x.sap.call("RZL_READ_DIR_LOCAL", NAME=home, FILE_TBL=[]).get("FILE_TBL"))
    rows = [["X" if to_int(f.get("SIZE")) else "", "", "", f.get("NAME", ""), (f.get("SIZE", "") or "").zfill(15),
             "", "", ""] for f in files if f.get("NAME") not in (".", "..")]
    stderr = [r for r in rows if r[3].startswith("stderr") and r[0] == "X"]
    v = Verdict(True, "No file was found with name stderr*") if not stderr else Verdict(
        None, f"{len(stderr)} non-empty stderr* file(s) in {home}; change times are not exposed by "
              "RZL_READ_DIR_LOCAL, so the last-24-hours rule cannot be evaluated")
    return [Block(cols("ST11"), rows, "ST11", "Display Developer Traces - Error Log Files")], v


def c_smgw(x: Ctx) -> Result:
    host = x.servers[0]["host"] if x.servers else ""
    r = x.sap.call("GWY_READ_CONNECTIONS", GWHOST=host, GWSERV=f"sapgw{sysnr(x.central)}", CONNECTIONS=[])
    conns = as_rows(r.get("CONNECTIONS"))
    rows = [[first(c, "CONV_INDEX", "NUMBER"), first(c, "LU_LOCAL", "LLU"), first(c, "TP_LOCAL", "LTP"),
             first(c, "LU_REMOTE", "RLU"), first(c, "TP_REMOTE", "RTP"), first(c, "USER"),
             first(c, "STATE", "STATUS"), first(c, "SYMDEST"), first(c, "CONV_ID"), first(c, "PROTOCOL"),
             first(c, "REQTIME", "LAST_REQUEST"), first(c, "SAPRC", "SAP_RC")] for c in conns]
    bad = [x_ for x_ in rows if x_[6] and x_[6].lower() not in ("connected", "1")]
    v = Verdict(not bad, "Connection status is fine" if not bad else f"{len(bad)} connection(s) not connected")
    return [Block(cols("SMGW"), rows, "SMGW", f"Gateway Monitor for {x.central} /Active Connections")], v


_TASKTYPE = {"01": "DIALOG", "02": "UPDATE", "03": "SPOOL", "04": "BACKGROUND", "05": "ENQUEUE",
             "06": "BUF.SYNC", "07": "AUTOABAP", "08": "UPDATE2", "0B": "AUTOTH", "0C": "RPCTH",
             "12": "AUTOCCMS", "21": "RFC", "22": "CPIC", "23": "ALE", "24": "HTTP", "25": "HTTPS",
             "27": "SMTP", "65": "WEB DYNPRO"}


def c_st03n(x: Ctx) -> Result:
    day = (x.now - dt.timedelta(days=1)).date()
    # SWNC_COLLECTOR_GET_AGGREGATES returns ms; TOTAL may be missing (instances summed) — D-052 live findings.
    from perf.collect import _aggregates, task_line
    r = _aggregates(x.sap, x.sid, day, [])
    tasks = r["TASKTYPE"] if r else []
    rows = []
    for t in tasks:
        n = to_float(first(t, "COUNT", "DCOUNT"))
        if not n:
            continue
        tl = task_line(t)
        avg = lambda *k: f"{sum(to_float(t.get(x)) for x in k) / n:.1f}"  # noqa: E731
        rows.append([tl["name"], str(int(n)), f"{tl['avg_resp']:.1f}", f"{tl['avg_proc']:.1f}", f"{tl['avg_cpu']:.1f}",
                     f"{tl['avg_db']:.1f}", avg("DBP_TIME"), f"{tl['avg_wait']:.1f}", avg("ROLLINTI"),
                     f"{tl['avg_rollwait']:.1f}", f"{tl['avg_load']:.1f}", f"{tl['avg_lock']:.1f}", avg("CPICTI"),
                     avg("GUINETTIME"), f"{tl['avg_gui']:.1f}", first(t, "GUICNT")])
    subtitle = f"Date: {day:%d.%m.%Y} - Workload Overview: Average time per step in ms"
    if not rows:
        v = Verdict(None, f"No workload recorded for {day:%d.%m.%Y} (SWNC_COLLECTOR_GET_AGGREGATES is empty) — "
                          "normal on an idle or test system; on a used system check the workload collector "
                          "job SAP_COLLECTOR_FOR_PERFMONITOR")
    else:
        dia = next((float(rw[2]) for rw in rows if rw[0] == "DIALOG"), None)
        v = Verdict(dia is None or dia <= 1000, "OK" if dia is None or dia <= 1000
                    else f"Dialog average response time {dia:.0f} ms")
    return [Block(cols("ST03N"), rows, f"Workload in System {x.sid}", subtitle, subtitle_merge=8)], v


_RFC_TYPE = {"3": "ABAP Connection", "T": "TCP/IP Connection", "G": "HTTP Connection to External Server",
             "H": "HTTP Connection to ABAP System", "I": "Internal Connection", "L": "Logical Connection",
             "W": "WebSocket RFC Connection", "X": "Connection via ABAP Driver", "2": "R/2 Connection"}


def _rfc_opts(s: str) -> dict:
    """RFCDES.RFCOPTIONS: comma-separated key=value (H host, S system no.,
    I port, M client, U user, L language, v=%_PWD password stored)."""
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out.setdefault(k, v.strip())
    return out


def _rfc_test(x: Ctx, dest: str, timeout_s: float) -> tuple[str, str, str]:
    """(state ok|failed|untestable, connection text, logon text)."""
    try:
        r = x.sap.call("DEST_CHECK_CONNECTION", _timeout_s=timeout_s, NAME=dest)
    except RfcSoapError as exc:
        if exc.kind == "timeout":
            return "failed", f"Failed: no response within {timeout_s:g} s — test aborted (hanging connection)", ""
        if "CHECK_NOT_POSSIBLE" in exc.message:
            return "untestable", "Test not possible for this connection type", ""
        return "failed", f"Failed: {exc.message[:150]}", ""
    res = (r.get("CONNECTION_TEST_RESULT") or "").strip()
    err = " ".join((r.get("CONNECTION_ERROR_TEXT") or "").split())
    auth, aerr = (r.get("AUTHORIZATION_TEST_RESULT") or "").strip(), " ".join((r.get("AUTHORIZATION_ERROR_TEXT") or "").split())
    if res == "E":
        return "failed", f"Failed: {err or 'connection error'}", ""
    if res == "N":
        return "untestable", "Test not possible for this connection type", ""
    logon = {"": "Logon successful", "N": "Logon test not possible"}.get(auth, f"Logon failed: {aerr or auth}")
    return ("failed" if auth == "E" else "ok"), "Connection OK", logon


def c_rfc_con(x: Ctx) -> Result:
    """SM59 connection test for every destination (DEST_CHECK_CONNECTION is
    SM59's own test). Each test gets AHC_RFC_TIMEOUT seconds (default 30);
    a hanging ("zombie") destination is abandoned then and counted as failed.
    Tests run AHC_RFC_PARALLEL at a time (default 3) so zombies don't add
    up; each one occupies a dialog work process in SAP until SAP's own RFC
    timeout ends it. AHC_RFC_SKIP: comma-separated name patterns to leave out."""
    import fnmatch
    from concurrent.futures import ThreadPoolExecutor
    timeout_s = float(os.environ.get("AHC_RFC_TIMEOUT", "30") or 30)
    workers = max(1, min(8, int(os.environ.get("AHC_RFC_PARALLEL", "3") or 3)))
    skip = [p.strip() for p in os.environ.get("AHC_RFC_SKIP", "").split(",") if p.strip()]
    title = "RFC_Con"
    sub = f"Overview of RFC Connections with Logon Data and Connection Test in {x.sc()}"
    dests = [d for d in x.sap.read("RFCDES", ["RFCDEST", "RFCTYPE", "RFCOPTIONS"])
             if not any(fnmatch.fnmatchcase(d["RFCDEST"], p) for p in skip)]
    docs = {d["RFCDEST"]: d.get("RFCDOC1", "") for d in x.sap.read("RFCDOC", ["RFCDEST", "RFCDOC1"], "RFCLANG EQ 'E'")}
    attrib = {d["RFCDEST"]: d for d in x.sap.read("RFCATTRIB", ["RFCDEST", "CUNAME", "CUDATE", "MUNAME", "MUDATE"])}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {d["RFCDEST"]: pool.submit(_rfc_test, x, d["RFCDEST"], timeout_s) for d in dests}
        results = {}
        for name, f in futs.items():
            try:
                results[name] = f.result(timeout=timeout_s + 15)
            except Exception as exc:  # noqa: BLE001  (belt and braces: never let one destination stall the pull)
                results[name] = ("failed", f"Failed: {type(exc).__name__}", "")
    rows, styles, failed, ok, untestable = [], {}, [], 0, 0
    order = {"failed": 0, "ok": 1, "untestable": 2}
    for d in sorted(dests, key=lambda d: (order[results[d["RFCDEST"]][0]], d["RFCTYPE"], d["RFCDEST"])):
        name, o = d["RFCDEST"], _rfc_opts(d.get("RFCOPTIONS", ""))
        state, conn, logon = results[name]
        at = attrib.get(name, {})
        who, when = (at.get("MUNAME") or at.get("CUNAME") or ""), d8(at.get("MUDATE") or at.get("CUDATE") or "")
        i = len(rows)
        rows.append([_RFC_TYPE.get(d["RFCTYPE"], d["RFCTYPE"]), name, docs.get(name, ""), who,
                     when.strftime("%d.%m.%Y") if when else "", o.get("H", ""), o.get("S") or o.get("I", ""),
                     o.get("M", ""), "", o.get("U", ""), o.get("L", ""),
                     "Saved" if o.get("v") == "%_PWD" else "", "", "", "", "", conn, logon])
        if state == "failed":
            failed.append(name)
            styles[(i, 16)] = RED
        elif state == "ok":
            ok += 1
            styles[(i, 16)] = GREEN
        else:
            untestable += 1
    summary = f"{ok} OK, {len(failed)} failed, {untestable} not testable"
    v = Verdict(not failed, f"All {ok} testable RFC connection(s) OK ({untestable} not testable)" if not failed else
                f"{len(failed)} RFC connection(s) failed: " + ", ".join(failed[:8])
                + (" …" if len(failed) > 8 else "") + f". {summary}. Check RFC_Con tab")
    return [Block(cols("RFC_Con"), rows, title, f"{sub} — {summary}", subtitle_merge=8, styles=styles)], v


BOX = "-" * 74          # width of SAP's system-check box (RSICC000 list)


def _box_lines(lines: list[str], ok_style: str) -> list[tuple[str, Optional[str]]]:
    """Style a system-check box the way the AHC export does: borders plain,
    the "SAP System Check" title cyan, the result line green (or red)."""
    out = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.fullmatch(r"\|?-+\|?", s):
            out.append((BOX, BODY))
        elif "sap system check" in s.lower():
            out.append((s, CYAN))
        else:
            out.append((s, ok_style if "no errors reported" in s.lower() else RED))
    return out


def c_sick(x: Ctx) -> Result:
    title, sub, head = "SICK", f"Installation Check of System {x.sc()}", "SAP Initial Consistency Check"
    if not jobs_enabled():
        why = "SICK runs report RSICC000, which needs a background job; jobs are switched off (AHC_NO_JOBS)"
        return TextTab(title, sub, head, [(BOX, BODY), (f"|Not collected{' ' * 59}|", None), (BOX, BODY)]), \
            Verdict(None, f"Not collected: {why}")
    lines = run_report(x.sap, "RSICC000", "SICK")
    body = [line.strip(" |") for line in lines if line.strip() and not re.fullmatch(r"\|?-+\|?", line.strip())
            and "sap system check" not in line.lower()]
    errors = [b for b in body if b and "no errors reported" not in b.lower()]
    ok = bool(body) and not errors
    v = Verdict(ok, "No Errors Found" if ok else
                (f"{len(errors)} error(s) reported: {errors[0][:120]}. Check SICK tab" if errors
                 else "RSICC000 printed no result. Run SICK manually"))
    return TextTab("SICK", sub, head, _box_lines(lines, GREEN)), v


def c_smms(x: Ctx) -> Result:
    """Message server services (ms/server_port_N) via sapcontrol (SM69)."""
    title, sub = "SMMS", "Message Monitor - Display Services"
    sc = SapControlViaSm69(x.sap)
    try:
        inst = sc.table(None, "GetSystemInstanceList")
    except RfcSoapError as exc:
        why = sc.unavailable_reason() or f"sapcontrol via SM69 failed: {exc.message[:150]}"
        return [Block(cols("SMMS"), [], title, sub)], Verdict(None, f"Not collected: {why}")
    ms = next((i for i in inst if "MESSAGESERVER" in i.get("features", "").upper()), None)
    if not ms:
        return [Block(cols("SMMS"), [], title, sub)], \
            Verdict(None, "Not collected: sapcontrol GetSystemInstanceList shows no message server instance")
    nr = f"{to_int(ms.get('instanceNr')):02d}"
    active = {}
    for ap in sc.table(nr, "GetAccessPointList"):
        if ap.get("processname") == "msg_server":
            active[ap.get("port", "")] = active.get(ap.get("port", ""), False) or ap.get("active") == "Yes"
    rows = []
    for n in range(10):
        val = sc.parameter(nr, f"ms/server_port_{n}")
        if not val:
            break
        p = parse_server_port(val)
        port = p.get("PORT", "")
        rows.append([str(n), p.get("PROT", ""), port, p.get("HOST", ""), p.get("EXTBIND", "0"),
                     p.get("VCLIENT", ""), "Opened" if active.get(port) else "Not opened",
                     p.get("TIMEOUT", "20"), p.get("PROCTIMEOUT", "60"),      # message server defaults
                     p.get("ACLFILE", ""), "", "", f"ms/server_port_{n}", "", ""])
    closed = [r for r in rows if r[6] != "Opened"]
    v = Verdict(not closed, "OK" if not closed else
                f"{len(closed)} message server port(s) not opened: " + ", ".join(f"{r[1]} {r[2]}" for r in closed))
    if not rows:
        v = Verdict(None, f"No ms/server_port_N parameter set in the message server instance {nr}")
    return [Block(cols("SMMS"), rows, title, sub)], v


# ── HANA tabs (through the SM69 hdbsql command) ───────────────────────────

def _hana_or_none(x: Ctx, sheet: str, title: str, subtitle: str) -> Optional[Result]:
    if x.hana and x.hana.available():
        return None
    why = x.hana.unavailable_reason() if x.hana else "HANA collection not configured"
    return ([Block(cols(sheet, i), [], title if i == 0 else None, subtitle if i == 0 else None, subtitle_merge=8)
             for i in range(len(next(s for s in LAYOUT["sheets"] if s["name"] == sheet)["tables"]))],
            Verdict(None, f"Not collected: {why}"))


def c_db12(x: Ctx) -> Result:
    title, sub = f"Data Backup Catalog {x.sid}", f"Date: {x.now:%d.%m.%Y} - Data Backup Catalog"
    skip = _hana_or_none(x, "DB12", title, sub)
    if skip:
        return skip
    # One row per backup file, newest data backup first — as SAP's DB12 export.
    rs = x.hana.table(["C.SYS_START_TIME", "C.SYS_END_TIME", "C.STATE_NAME", "C.MESSAGE", "F.SOURCE_ID",
                       "F.SOURCE_TYPE_NAME", "F.HOST", "F.SERVICE_TYPE_NAME", "F.DESTINATION_PATH", "F.BACKUP_SIZE"],
                      "FROM M_BACKUP_CATALOG C JOIN M_BACKUP_CATALOG_FILES F ON C.ENTRY_ID=F.ENTRY_ID "
                      "WHERE C.ENTRY_TYPE_NAME='complete data backup' ORDER BY C.ENTRY_ID DESC, F.SOURCE_ID",
                      top=10)
    rows = []
    for r in rs:
        s, e = stamp(r.get("SYS_START_TIME", "")), stamp(r.get("SYS_END_TIME", ""))
        secs = int((e - s).total_seconds()) if s and e else None
        dur = f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs is not None else ""
        rows.append([dot(s, True), dot(e, True), dur, r.get("STATE_NAME", ""), r.get("MESSAGE", ""),
                     r.get("SOURCE_ID", ""), r.get("SOURCE_TYPE_NAME", ""), r.get("HOST", ""),
                     r.get("SERVICE_TYPE_NAME", ""), r.get("DESTINATION_PATH", ""), r.get("BACKUP_SIZE", "")])
    ok = [stamp(r.get("SYS_END_TIME", "")) for r in rs if "success" in r.get("STATE_NAME", "").lower()]
    ok = [o for o in ok if o]
    fresh = bool(ok) and (x.now - max(ok)).total_seconds() <= 26 * 3600
    return ([Block(cols("DB12"), rows, title, sub, subtitle_merge=8)],
            Verdict(fresh, "OK" if fresh else "No successful data backup in the last 26 hours"))


_RATING = {"5": "Error", "4": "High", "3": "Medium", "2": "Low", "1": "Information"}


def c_db02_alerts(x: Ctx) -> Result:
    title = f"Current Alerts in system {x.sid}"
    skip = _hana_or_none(x, "DB02_Alerts", title, "Current Alerts")
    if skip:
        return skip
    rs = x.hana.table(["ALERT_TIMESTAMP", "ALERT_ID", "ALERT_DETAILS", "ALERT_USERACTION", "ALERT_RATING"],
                      "FROM _SYS_STATISTICS.STATISTICS_CURRENT_ALERTS ORDER BY ALERT_RATING DESC, "
                      "ALERT_TIMESTAMP DESC, ALERT_ID, INDEX, ALERT_HOST, ALERT_PORT")
    rows = [[dot(stamp(r.get("ALERT_TIMESTAMP", "")), True), r.get("ALERT_ID", ""), r.get("ALERT_DETAILS", ""),
             r.get("ALERT_USERACTION", ""), _RATING.get(r.get("ALERT_RATING", ""), r.get("ALERT_RATING", ""))]
            for r in rs]
    cnt = {k: sum(1 for r in rows if r[4] == k) for k in ("Error", "High", "Medium", "Low")}
    sub = f"Current Alerts: {cnt['Error']}  Errors, {cnt['High']}  High, {cnt['Medium']}  Medium, {cnt['Low']}  Low"
    bad = cnt["Error"] + cnt["High"] + cnt["Medium"]
    return ([Block(cols("DB02_Alerts"), rows, title, sub, subtitle_merge=8)],
            Verdict(not bad, "OK" if not bad else
                    "Error, High or Medium Priority message is there. Check DB02_Alerts TAB"))


def c_db02_log(x: Ctx) -> Result:
    title, sub = f"System Availability Log in system {x.sid}", f"Date: {x.now:%d.%m.%Y} - System Availability Log"
    skip = _hana_or_none(x, "DB02_Log", title, sub)
    if skip:
        return skip
    c = cols("DB02_Log")
    keys = ["EVENT_TIME", "GUID", "IS_ORIGIN", "TRACE_HOST", "EVENT_NAME", "EVENT_DETAIL", "ERROR_MESSAGE",
            "SYSTEM_ACTIVE", "SYSTEM_STATUS", "HOST", "HOST_ACTIVE", "HOST_STATUS", "DATABASE_NAME",
            "DATABASE_ACTIVE", "SERVICE_NAME", "PORT", "VOLUME_ID", "SERVICE_ACTIVE", "HOST_CONFIG_ROLES",
            "HOST_ACTUAL_ROLES", "STORAGE_CONFIG_PARTITION", "STORAGE_ACTUAL_PARTITION", "TARGET_HOST",
            "TARGET_HOST_CONFIG_ROLES", "TARGET_HOST_ACTUAL_ROLES", "TARGET_STORAGE_CONFIG_PARTITION",
            "TARGET_STORAGE_ACTUAL_PARTITION", "SITE_ID"]
    rs = x.hana.table(keys, "FROM SYS.M_SYSTEM_AVAILABILITY ORDER BY EVENT_TIME DESC, GUID", top=10)
    rs = list(reversed(rs))          # SAP lists the events oldest first, newest (highlighted) last
    rows = [[dot(stamp(r.get(k, "")), True) if k == "EVENT_TIME" else r.get(k, "") for k in keys][:len(c)] for r in rs]
    last = max((stamp(r.get("EVENT_TIME", "")) for r in rs if stamp(r.get("EVENT_TIME", ""))), default=None)
    stale = last is not None and (x.now - last).total_seconds() > 86400
    return ([Block(c, rows, title, sub, subtitle_merge=8)],
            Verdict(not stale, "OK" if not stale else "Last Event Time is Greater than 24 Hrs. Check DB02_Log TAB"))


def c_db02_memory(x: Ctx) -> Result:
    d = x.now.strftime("%d.%m.%Y")
    title, sub = f"Database Disk Usage in system {x.sid}", f"Database Disk Usage info on {d}"
    skip = _hana_or_none(x, "DB02_Memory", title, sub)
    if skip:
        return skip
    gb = 1073741824
    disks = x.hana.query("SELECT USAGE_TYPE, SUM(USED_SIZE) AS USED, SUM(TOTAL_SIZE) AS TOTAL FROM M_DISKS "
                         "GROUP BY USAGE_TYPE ORDER BY USAGE_TYPE")
    label = {"DATA": "Data Volume Size / Disk Size", "LOG": "Log Volume Size / Disk Size",
             "TRACE": "Trace Files / Disk Size", "DATA_BACKUP": "Bckup Disk Size"}
    rows1, worst = [], 0.0
    for r in disks:
        u, t = to_float(r.get("USED")) / gb, to_float(r.get("TOTAL")) / gb
        rows1.append([label.get(r.get("USAGE_TYPE", ""), r.get("USAGE_TYPE", "")), f"{u:.2f} GB / {t:.2f} GB"])
        if t:
            worst = max(worst, u / t * 100)
    hosts = x.hana.query("SELECT HOST, USED_PHYSICAL_MEMORY, FREE_PHYSICAL_MEMORY FROM M_HOST_RESOURCE_UTILIZATION")
    blocks = [Block(cols("DB02_Memory", 0), rows1, title, sub, subtitle_merge=8)]
    for h in hosts:
        u, f = to_float(h.get("USED_PHYSICAL_MEMORY")) / gb, to_float(h.get("FREE_PHYSICAL_MEMORY")) / gb
        if u + f:
            worst = max(worst, u / (u + f) * 100)
        blocks.append(Block(cols("DB02_Memory", 1), [["Physical Memory Used / Available", f"{u:.2f} GB / {u + f:.2f} GB"]],
                            caption=f"Host {h.get('HOST', '')}"))
    return blocks, Verdict(worst < 90, "OK" if worst < 90 else f"Usage at {worst:.0f}%. Check DB02_Memory tab")


def c_db01(x: Ctx) -> Result:
    skip = _hana_or_none(x, "DB01", "DB01", "Blocked Transactions")
    if skip:
        b, v = skip
        b[0].subtitle_merge = 3
        return b, v
    keys = ["HOST", "PORT", "BLOCKED_TRANSACTION_ID", "BLOCKED_UPDATE_TRANSACTION_ID", "BLOCKED_CONNECTION_ID",
            "LOCK_OWNER_TRANSACTION_ID", "LOCK_OWNER_UPDATE_TRANSACTION_ID", "LOCK_OWNER_CONNECTION_ID",
            "BLOCKED_TIME", "WAITING_RECORD_ID", "WAITING_SCHEMA_NAME", "WAITING_TABLE_NAME",
            "WAITING_OBJECT_NAME", "WAITING_OBJECT_TYPE", "WAITING_PART_ID", "LOCK_TYPE", "LOCK_MODE"]
    rs = x.hana.table(keys, "FROM M_BLOCKED_TRANSACTIONS ORDER BY BLOCKED_TRANSACTION_ID, "
                            "LOCK_OWNER_TRANSACTION_ID, WAITING_RECORD_ID")
    rows = [[r.get(k, "") for k in keys] for r in rs]
    return ([Block(cols("DB01"), rows, "DB01", "Blocked Transactions")],
            Verdict(not rows, "No Blocked Transactions found" if not rows else f"{len(rows)} Blocked Transaction(s) found"))


# ── run everything ────────────────────────────────────────────────────────

SHEET_OF_MONITOR = ["SM51", "SM21", "ST22", "SM13", "SM12", "SP01", "SM37", "SRT_MONI", "AL08", "SM50", "SM66",
                    "SOST", "ST02", "SMLG", "ST06", "Cert_Info", "SLICENSE", "DB12", "DB02_Alerts", "DB02_Log",
                    "DB02_Memory", "RFC_Con", "ST03N", "SICK", "RZ03", "RZ04", "SMGW", "SM04", "SMMS", "SCC4",
                    "RZ12", "ST11", "DB01"]


def collect(x: Ctx, progress: Optional[Callable[[str], None]] = None) -> tuple[dict, list[Verdict]]:
    load_servers(x)
    users: list[dict] = []
    try:
        users = _users(x)
    except RfcSoapError as exc:
        x.notes.append(f"TH_USER_LIST: {exc.message}")
    collectors: dict[str, Callable[[], Result]] = {
        "SM51": lambda: c_sm51(x), "SM21": lambda: c_sm21(x), "ST22": lambda: c_st22(x),
        "SM13": lambda: c_sm13(x), "SM12": lambda: c_sm12(x), "SP01": lambda: c_sp01(x),
        "SM37": lambda: c_sm37(x), "SRT_MONI": lambda: c_srt_moni(x), "AL08": lambda: c_al08(x, users),
        "SM50": lambda: c_sm50(x), "SM66": lambda: c_sm66(x), "SOST": lambda: c_sost(x),
        "ST02": lambda: c_st02(x), "SMLG": lambda: c_smlg_tab(x), "ST06": lambda: c_st06(x),
        "Cert_Info": lambda: c_cert(x), "SLICENSE": lambda: c_slicense(x), "DB12": lambda: c_db12(x),
        "DB02_Alerts": lambda: c_db02_alerts(x), "DB02_Log": lambda: c_db02_log(x),
        "DB02_Memory": lambda: c_db02_memory(x), "RFC_Con": lambda: c_rfc_con(x), "ST03N": lambda: c_st03n(x),
        "SICK": lambda: c_sick(x), "RZ03": lambda: c_rz03(x), "RZ04": lambda: c_rz04(x),
        "SMGW": lambda: c_smgw(x), "SM04": lambda: c_sm04(x, users), "SMMS": lambda: c_smms(x),
        "SCC4": lambda: c_scc4(x), "RZ12": lambda: c_rz12(x), "ST11": lambda: c_st11(x), "DB01": lambda: c_db01(x),
    }
    tabs: dict[str, object] = {}
    verdicts: dict[str, Verdict] = {}
    for sheet in SHEET_OF_MONITOR:
        if progress:
            progress(sheet)
        try:
            tabs[sheet], verdicts[sheet] = collectors[sheet]()
        except Exception as exc:  # noqa: BLE001 — one tab never fails the run
            msg = exc.message if isinstance(exc, RfcSoapError) else f"{type(exc).__name__}: {exc}"
            x.notes.append(f"{sheet}: {msg}\n{traceback.format_exc(limit=2)}")
            verdicts[sheet] = Verdict(None, f"Not collected: {msg[:200]}")
    return tabs, [verdicts[s] for s in SHEET_OF_MONITOR]
