"""Builds the Performance views: live status and one-day analysis (D-052)."""
from __future__ import annotations

import datetime as dt
import time
from typing import Optional

from engine.rfc.soap_client import RfcSoapClient, RfcSoapError

from ahc.collect import Ctx
from ahc.hana import HanaViaSm69
from ahc.sap import Sap

from . import collect as C
from . import landscape as L
from .kpi import Kpi, dialog_kpis, overall, rate_high, rate_low

LIMIT_DAYS = 90          # analysis window (decision 2026-10-02); SAP may keep less

RECOMMEND = {
    "dia_resp": "Find the transactions behind the slow dialog steps in the Top transactions table; "
                "check the hours with the worst response in the hourly profile.",
    "dia_db": "Database time dominates: look at the top SQL statements in HANA and the transactions "
              "with the highest DB time per step (indexes, selective WHERE clauses, table buffering).",
    "dia_cpu": "ABAP CPU time is high: check the transactions with the highest CPU per step (custom "
               "code loops, large internal tables) and the host CPU load.",
    "dia_wait": "Requests wait in the dispatcher queue: dialog work processes are exhausted at peak — "
                "add dialog work processes/instances or move load (background, logon groups).",
    "dia_roll": "High roll wait means synchronous RFC or GUI controls round trips — check RFC "
                "destinations and front-end network latency.",
    "dia_gui": "High GUI time points to front-end/network latency (WAN, VPN) rather than the server.",
    "dia_util": "Most dialog work processes are busy: users may soon wait in the queue.",
    "host_cpu": "Host CPU is under pressure: check SM50/SM66 for long-running processes and the HANA CPU.",
    "host_mem": "Little free OS memory left on the application server: check extended memory and "
                "heap usage in ST02 and the OS paging rate.",
    "buffers": "Table/program buffer quality is low or buffers swap: enlarge the affected buffers "
               "(ST02) — swaps force reloads from the database.",
    "upd_err": "Update requests terminated: process or analyse them in SM13 — business data may be missing.",
    "upd_pend": "Updates are queuing: check update work processes and SM13.",
    "hana_mem": "HANA memory is close to its allocation limit: risk of column-store unloads and OOM "
                "dumps — check the largest tables/heap allocators and the global_allocation_limit.",
    "hana_cpu": "HANA CPU is high: check the top SQL statements and active threads.",
    "hana_blocked": "Blocked transactions in HANA: find the lock holder (M_BLOCKED_TRANSACTIONS) and "
                    "the application lock it waits for.",
    "hana_alerts": "Open high-priority HANA alerts: review them in DB02 / HANA cockpit.",
    "hana_unloads": "Column-store tables were unloaded for lack of memory — performance drops when "
                    "they reload; increase memory or reduce footprint.",
    "hana_savepoint": "Long savepoints block writes briefly: check the data volume I/O (M_VOLUME_IO_TOTAL_STATISTICS).",
    "smon_queue": "SMON saw requests waiting in the dialog queue.",
    "app_db_rtt": "At least one application server is far from the database: route dialog users and heavy batch "
                  "to the nearest instance (logon groups SMLG, job server groups SM61) and check the placement "
                  "— same availability zone / proximity placement group as HANA (SAP Note 1100926: ≤ 0.7 ms).",
    "app_servers": "An application server instance is not running: check SM51 and the instance's dev_disp/dev_w0.",
    "hana_hosts": "A HANA worker host is not active: check the host and its services (sapcontrol GetProcessList, "
                  "M_SERVICES); a standby takes over only if the failover is configured.",
    "hsr_status": "HANA system replication is not in sync: check M_SERVICE_REPLICATION.REPLICATION_STATUS_DETAILS, "
                  "the network to the secondary site and the secondary's trace files — a takeover now would lose data "
                  "or fail.",
    "hsr_lag": "Log shipping to the secondary is behind: check the network throughput between the sites and the "
               "async log buffer (SAP Note 1999880).",
    "hsr_takeover": "A system replication takeover happened that day: check the cause in the nameserver trace and "
                    "that the former primary was re-registered as secondary.",
}


def system_now(sap: Sap) -> tuple[dict, dt.datetime]:
    info = sap.system_info()
    tz = int((info.get("RFCTZONE") or "0").strip() or 0)
    return info, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=tz)


def _hana(sap: Sap, notes: list[str]) -> Optional[HanaViaSm69]:
    h = HanaViaSm69(sap)
    if h.available():
        return h
    notes.append(f"HANA: {h.unavailable_reason()}")
    return None


def _last24(sap: Sap, sid: str, now: dt.datetime, notes: list[str]) -> tuple[Optional[dict], list[dict]]:
    """Today's workload plus the last 24 hours of the hourly profile (today + yesterday)."""
    today = C.workload_day(sap, sid, now.date(), notes)
    yest = C.workload_day(sap, sid, now.date() - dt.timedelta(days=1), notes)
    hours = []
    for src, day in ((yest, now.date() - dt.timedelta(days=1)), (today, now.date())):
        for hrow in (src or {}).get("hourly", []):
            stamp = dt.datetime.combine(day, dt.time(hrow["hour"]))
            if now - dt.timedelta(hours=24) < stamp + dt.timedelta(hours=1) and stamp <= now:
                hours.append({**hrow, "label": stamp.strftime("%d.%m %H:00")})
    return today or yest, hours[-24:]


def live_kpis(sapd: dict, work: Optional[dict], hana: Optional[dict], smon: dict, spark: dict) -> list[Kpi]:
    ks = dialog_kpis((work or {}).get("dialog") or {})
    for k in ks:
        if k.id == "dia_resp":
            k.spark = spark.get("dia_resp", [])
    u = sapd.get("dia_util")
    ks.append(Kpi("dia_util", "Dialog WPs busy", u, "%", rate_high(u, 70, 85, 95),
                  f"{sapd['wp'].get('DIA', {}).get('running', 0)} of {sapd['wp'].get('DIA', {}).get('total', 0)} running",
                  "< 70 %"))
    cpu = sapd["cpu"]["util"]
    ks.append(Kpi("host_cpu", "App server CPU", cpu, "%", rate_high(cpu, 70, 85, 95),
                  f"{sapd['cpu']['cpus'] or '?'} CPUs · load {sapd['cpu']['load1']}", "< 70 %"))
    fm = sapd["memory"]["free_pct"]
    ks.append(Kpi("host_mem", "App server free memory", fm, "%", rate_low(fm, 10, 5, 2),
                  f"{sapd['memory']['free_mb'] or 0:,} of {sapd['memory']['phys_mb'] or 0:,} MB free", "> 10 %"))
    b = sapd.get("buffers") or {}
    hit = b.get("worst_hit")
    br = rate_low(hit, 98, 95, 90) if hit is not None else "unknown"
    if b.get("swaps"):
        br = max(br, "warning", key=lambda r: {"unknown": 0, "good": 0, "warning": 1, "serious": 2, "critical": 3}[r])
    ks.append(Kpi("buffers", "Buffer quality (worst)", hit, "%", br,
                  (f"{b['worst_name']} · " if b.get("worst_name") else "") + f"{b.get('swaps', 0):,} swaps",
                  "≥ 98 %, 0 swaps"))
    ue = sapd.get("update_errors")
    ks.append(Kpi("upd_err", "Update errors", ue, "", "serious" if ue else ("good" if ue == 0 else "unknown"),
                  f"{sapd.get('updates_pending') or 0} update records pending", "0"))
    ks.append(Kpi("users", "Logged-on users", sapd.get("users"), "", "good" if sapd.get("users") is not None else "unknown",
                  f"{sapd.get('sessions') or 0} sessions · {sapd.get('locks') or 0} locks · "
                  f"{sapd.get('jobs_running') or 0} jobs running", ""))
    if smon.get("active") and smon.get("latest"):
        q = smon["latest"].get("diaq", 0)
        ks.append(Kpi("smon_queue", "Dialog queue (SMON)", q, "", rate_high(q, 1, 10, 50),
                      f"snapshot {smon['latest'].get('time')}", "0"))
    ks += hana_kpis(hana, spark)
    return ks


def hana_kpis(hana: Optional[dict], spark: dict) -> list[Kpi]:
    if not hana:
        return [Kpi("hana_mem", "HANA memory used", None, "%", group="HANA",
                    detail="HANA not reachable — SM69 command ZAHC_HDBSQL needed")]
    out = []
    hosts = hana.get("hosts") or []
    # scale-out: every host has its own allocation limit, so the host closest to it decides
    worst = max((h for h in hosts if h.get("mem_limit_gb")), key=lambda h: (h.get("mem_used_gb") or 0) / h["mem_limit_gb"],
                default=None)
    pct = round((worst.get("mem_used_gb") or 0) / worst["mem_limit_gb"] * 100, 1) if worst else None
    detail = (f"{worst.get('mem_used_gb') or 0:,.1f} of {worst['mem_limit_gb']:,.1f} GB allocation limit"
              + (f" · {worst['host']} (fullest of {len(hosts)} hosts)" if len(hosts) > 1 else "")) if worst else ""
    out.append(Kpi("hana_mem", "HANA memory used", pct, "%", rate_high(pct, 80, 90, 95), detail, "< 80 %", "HANA",
                   spark.get("hana_mem", [])))
    cpus = [h["cpu"] for h in hosts if h.get("cpu") is not None]
    cpu = max(cpus) if cpus else None
    out.append(Kpi("hana_cpu", "HANA CPU", cpu, "%", rate_high(cpu, 70, 85, 95), "busiest host", "< 70 %", "HANA",
                   spark.get("hana_cpu", [])))
    bl = hana.get("blocked")
    out.append(Kpi("hana_blocked", "Blocked transactions", bl, "", rate_high(bl, 1, 5, 20),
                   f"{hana.get('connections') or 0} connections · {hana.get('running_statements') or 0} running",
                   "0", "HANA"))
    al = hana.get("alerts") or {}
    high = sum(n for r, n in al.items() if r in ("4", "5"))
    out.append(Kpi("hana_alerts", "High/error alerts", high, "", "serious" if high else "good",
                   f"{sum(al.values())} open alerts in total", "0", "HANA"))
    un = hana.get("unloads_24h")
    out.append(Kpi("hana_unloads", "Low-memory unloads (24 h)", un, "", rate_high(un, 1, 10, 100),
                   "column-store tables unloaded for memory", "0", "HANA"))
    sp = hana.get("savepoint_max_s")
    out.append(Kpi("hana_savepoint", "Longest savepoint (24 h)", sp, "s", rate_high(sp, 30, 120, 300),
                   f"average {hana.get('savepoint_avg_s') or 0} s", "< 30 s", "HANA"))
    return out


def landscape_kpis(land: dict, live: bool = True) -> list[Kpi]:
    """Instances, app→DB latency, HANA hosts and system replication. A day view only rates what
    belongs to that day (latency from that day's SMON/ST03N, takeovers); instance and HSR state are
    'now' and stay in the landscape panel."""
    ks: list[Kpi] = []
    app = land.get("app") or {}
    if live and app.get("count", 0) > 1:
        down = [s["name"] for s in app["servers"] if not s["active"] and not s.get("historic")]
        ks.append(Kpi("app_servers", "Application servers active", app["active"], f"/ {app['count']}",
                      "serious" if down else "good", ("not active: " + ", ".join(down)) if down else "all instances up",
                      "all"))
    if app.get("source") and app.get("measured"):
        rows = [s for s in app["servers"] if s.get("latency_us") is not None]
        worst = max(rows, key=lambda s: s["latency_us"])
        near = next(s for s in rows if s.get("nearest"))
        ks.append(Kpi("app_db_rtt", "App server → DB round trip", round(worst["latency_us"] / 1000, 2), "ms",
                      L.rate_rtt(worst["latency_us"]),
                      (f"slowest {worst['name']} · nearest {near['name']} {near['latency_us'] / 1000:.2f} ms"
                       if len(rows) > 1 else f"{worst['name']}"), "≤ 0.3 ms (Note 1100926)"))
    hana = land.get("hana") or {}
    hosts = hana.get("hosts") or []
    if live and len(hosts) > 1:
        bad = [h["host"] for h in hosts if not h["active"] and h["role"] != "standby"]
        ks.append(Kpi("hana_hosts", "HANA hosts active", sum(1 for h in hosts if h["active"]), f"/ {len(hosts)}",
                      "critical" if bad else "good",
                      ("inactive worker: " + ", ".join(bad)) if bad else
                      " · ".join(f"{h['host']} {h['role'] or '?'}" for h in hosts)[:120], "all workers", "HANA"))
    sr = hana.get("hsr")
    if live and sr:
        sites = ", ".join(f"{x['site']} ({x['mode']})" for x in sr["sites"]) or "secondary"
        ks.append(Kpi("hsr_status", "System replication", sr["status"].title(), "",
                      L.HSR_RANK.get(sr["status"], "serious"), f"{sr['primary_site']} → {sites}",
                      "Active", "HANA"))
        lag = sr.get("max_lag_s")
        ks.append(Kpi("hsr_lag", "Replication log backlog", lag, "s", rate_high(lag, 30, 300, 900),
                      "log shipping backlog, worst service", "< 30 s", "HANA"))
    if hana.get("takeovers"):
        t = hana["takeovers"][-1]
        ks.append(Kpi("hsr_takeover", "HSR takeovers that day", len(hana["takeovers"]), "", "serious",
                      f"last {t['start'][11:16]} {t['from_site']} → {t['to_site']}", "0", "HANA"))
    return ks


def landscape(sap: Sap, sid: str, info: dict, h: Optional[HanaViaSm69], day: dt.date, servers: list[dict],
              local_os: dict, smon_installed: bool, live_since: Optional[dt.datetime], notes: list[str]) -> dict:
    """App tier per instance + HANA hosts/HSR + cloud placement + nearest app server to the DB."""
    wl = C._safe(lambda: L.instance_workload(sap, sid, day, notes), notes, "ST03N per instance", [])
    if not wl and live_since is not None:          # live, nothing aggregated yet today → yesterday
        wl = C._safe(lambda: L.instance_workload(sap, sid, day - dt.timedelta(days=1), notes), notes,
                     "ST03N per instance", [])
    smon = C._safe(lambda: L.smon_servers(sap, day, live_since), notes, "SMON per server", {}) if smon_installed else {}
    hana: dict = {}
    client_rtt: dict = {}
    if h:
        hana = C._safe(lambda: L.hana_landscape(h, notes), notes, "HANA landscape", {}) or {}
        if live_since is not None:
            client_rtt = C._safe(lambda: L.hana_client_rtt(h), notes, "HANA client network I/O", {})
        else:
            hana["takeovers"] = C._safe(lambda: L.takeovers(h, day), notes, "HSR takeover history", [])
    db_hosts = [x["host"] for x in hana.get("hosts", []) if x.get("active")] or \
        [(info.get("RFCDBHOST") or "").strip()]
    app_hosts = list(dict.fromkeys(s.get("host", "") for s in servers if s.get("host")))
    cloud = L.placement(sap, sid, app_hosts, [d for d in db_hosts if d], notes)
    app = L.app_servers(servers, (info.get("RFCDEST") or "").strip(), wl, smon, client_rtt, cloud,
                        db_hosts, local_os, live=live_since is not None)
    return {"app": app, "hana": hana, "cloud": cloud, "db_hosts": db_hosts}


def findings(kpis: list[Kpi]) -> list[dict]:
    order = {"critical": 0, "serious": 1, "warning": 2}
    bad = sorted([k for k in kpis if k.rating in order], key=lambda k: order[k.rating])
    return [{"kpi": k.id, "rating": k.rating, "title": f"{k.label}: {k.value if k.value is not None else '–'}"
             f"{(' ' + k.unit) if k.unit and k.value is not None else ''}", "detail": k.detail,
             "recommendation": RECOMMEND.get(k.id, "")} for k in bad]


def live(client: RfcSoapClient, sap_client: str) -> dict:
    t0 = time.time()
    sap = Sap(client)
    notes: list[str] = []
    info, now = system_now(sap)
    sid = (info.get("RFCSYSID") or "").strip() or "?"
    x = Ctx(sap=sap, sid=sid, client=sap_client, now=now)
    sapd = C.sap_live(x, notes)
    work, hours = _last24(sap, sid, now, notes)
    if work is None:
        days = C._safe(lambda: C.workload_dates(sap, sid), notes, "SWNCMONIINDEX", [])
        notes.append("ST03N: no workload aggregated for today or yesterday"
                     + (f" (newest day in SAP: {days[0]})" if days else "")
                     + " — check that job SAP_COLLECTOR_FOR_PERFMONITOR runs hourly (SM37).")
    smon = C._safe(lambda: C.smon_status(sap, now), notes, "SMON", {"installed": False, "active": False})
    if smon.get("active"):
        series = C._safe(lambda: C.smon_day(sap, now.date()), notes, "SMON series", [])
        smon["series"] = series[-96:]                       # last 8 h at 5-min buckets
        smon["latest"] = series[-1] if series else None
    h = _hana(sap, notes)
    hana = C._safe(lambda: C.hana_live(h, notes), notes, "HANA live", None) if h else None
    spark = {"dia_resp": [p["avg_resp"] for p in hours]}
    if h:
        today = C._safe(lambda: C.hana_day(h, now.date()), notes, "HANA today", [])
        if hana is not None:
            hana["today"] = today
        spark["hana_cpu"] = [p["cpu_max"] for p in today]
        spark["hana_mem"] = [round(p["mem_max_gb"] / p["limit_gb"] * 100, 1) if p["limit_gb"] else 0 for p in today]
    local_os = {"cpu": sapd["cpu"]["util"], "free_mem": sapd["memory"]["free_pct"], "users": sapd.get("users")}
    land = landscape(sap, sid, info, h, now.date(), sapd["servers"], local_os, bool(smon.get("installed")),
                     now - dt.timedelta(hours=1), notes)
    kpis = live_kpis(sapd, work, hana, smon, spark) + landscape_kpis(land)
    tier = land["app"]["servers"]
    if land["app"]["count"] > 1:     # ST06/SM04 figures are this instance only — use every instance SMON saw
        cpus = [s for s in tier if s.get("cpu") is not None]
        for k in kpis:
            if k.id == "host_cpu" and len(cpus) > 1:
                busiest = max(cpus, key=lambda s: s["cpu"])
                k.value, k.rating = busiest["cpu"], rate_high(busiest["cpu"], 70, 85, 95)
                k.label, k.detail = "App server CPU (busiest)", f"{busiest['name']} · {len(cpus)} instances (SMON)"
            elif k.id in ("host_cpu", "host_mem", "users"):
                k.detail = f"{(info.get('RFCDEST') or '').strip()} only · " + k.detail
    if work is None:
        for k in kpis:
            if k.id == "dia_resp":
                k.detail = "No ST03N workload for today or yesterday — workload collector not running?"
    return {"mode": "live", "sid": sid, "client": sap_client, "release": info.get("RFCSAPRL", ""),
            "db": info.get("RFCDBSYS", ""), "host": info.get("RFCHOST", ""), "as_of": now.isoformat(sep=" "),
            "overall": overall(kpis), "kpis": [k.dict() for k in kpis], "findings": findings(kpis),
            "sap": sapd, "workload": work, "hourly": hours, "smon": smon, "hana": hana, "landscape": land,
            "notes": notes, "took_s": round(time.time() - t0, 1), "calls": len(sap.calls)}


def day(client: RfcSoapClient, sap_client: str, date: dt.date) -> dict:
    t0 = time.time()
    sap = Sap(client)
    notes: list[str] = []
    info, now = system_now(sap)
    sid = (info.get("RFCSYSID") or "").strip() or "?"
    work = C.workload_day(sap, sid, date, notes)
    smon_series: list = []
    smon = C._safe(lambda: C.smon_status(sap, now), notes, "SMON", {"installed": False})
    if smon.get("installed"):
        smon_series = C._safe(lambda: C.smon_day(sap, date, 15), notes, "SMON series", [])
    h = _hana(sap, notes)
    hana_hours: list = []
    hana_host_hours: dict = {}
    if h:
        hana_host_hours = C._safe(lambda: C.hana_day_hosts(h, date), notes, "HANA history", {})
        hana_hours = C.worst_per_hour(hana_host_hours)
    x = Ctx(sap=sap, sid=sid, client=sap_client, now=now)
    C._safe(lambda: C.load_servers(x), notes, "TH_SERVER_LIST")
    land = landscape(sap, sid, info, h, date, x.servers, {}, bool(smon.get("installed")), None, notes)
    kpis = dialog_kpis((work or {}).get("dialog") or {})
    for k in kpis:
        if k.id == "dia_resp":
            k.spark = [p["avg_resp"] for p in (work or {}).get("hourly", [])]
    if hana_hours:
        peak_cpu = max(p["cpu_max"] for p in hana_hours)
        peak_mem = max((p["mem_max_gb"] / p["limit_gb"] * 100) for p in hana_hours if p["limit_gb"]) \
            if any(p["limit_gb"] for p in hana_hours) else None
        kpis.append(Kpi("hana_cpu", "HANA CPU peak", peak_cpu, "%", rate_high(peak_cpu, 80, 90, 98), "highest hourly max",
                        "< 80 %", "HANA", [p["cpu_max"] for p in hana_hours]))
        kpis.append(Kpi("hana_mem", "HANA memory peak", round(peak_mem, 1) if peak_mem else None, "%",
                        rate_high(peak_mem, 80, 90, 95), "vs allocation limit", "< 80 %", "HANA",
                        [round(p["mem_max_gb"] / p["limit_gb"] * 100, 1) if p["limit_gb"] else 0 for p in hana_hours]))
    kpis += landscape_kpis(land, live=False)
    if smon_series:
        q = max(p["diaq"] for p in smon_series)
        kpis.append(Kpi("smon_queue", "Peak dialog queue (SMON)", q, "", rate_high(q, 1, 10, 50), "15-min average", "0"))
    return {"mode": "day", "date": date.isoformat(), "sid": sid, "client": sap_client,
            "release": info.get("RFCSAPRL", ""), "db": info.get("RFCDBSYS", ""), "host": info.get("RFCHOST", ""),
            "as_of": now.isoformat(sep=" "), "overall": overall(kpis), "kpis": [k.dict() for k in kpis],
            "findings": findings(kpis), "workload": work, "hourly": (work or {}).get("hourly", []),
            "smon": {**smon, "series": smon_series}, "hana_hours": hana_hours, "hana_host_hours": hana_host_hours,
            "landscape": land,
            "notes": notes, "took_s": round(time.time() - t0, 1), "calls": len(sap.calls)}


def ranges(client: RfcSoapClient) -> dict:
    sap = Sap(client)
    notes: list[str] = []
    info, now = system_now(sap)
    sid = (info.get("RFCSYSID") or "").strip() or "?"
    wdays = C._safe(lambda: C.workload_dates(sap, sid), notes, "SWNCMONIINDEX", [])
    h = _hana(sap, notes)
    hana_from = C._safe(lambda: C.hana_history_range(h), notes, "HANA history range", None) if h else None
    smon = C._safe(lambda: C.smon_status(sap, now), notes, "SMON", {"installed": False, "active": False})
    oldest = min([d for d in [wdays[-1] if wdays else None, hana_from] if d], default=None)
    return {"sid": sid, "today": now.date().isoformat(), "workload_days": wdays, "hana_from": hana_from,
            "oldest": oldest, "limit_days": LIMIT_DAYS, "smon": smon, "notes": notes}
