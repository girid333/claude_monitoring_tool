"""Multi-instance SAP + scale-out / HSR HANA landscape for the Performance view (D-052b).

Everything is READ-only except one allowlisted SM69 command per host:

  app servers  TH_SERVER_LIST + TH_WPINFO per server; ST03N per instance
               component (SWNC FACTOR=1 → microseconds); /SDF/SMON_HEADER
               per SERVER (CPU, free memory, dialog queue, NETRTT).
  HANA         M_LANDSCAPE_HOST_CONFIGURATION (roles), M_HOST_INFORMATION
               (hw vendor/model), M_SYSTEM_REPLICATION + M_SERVICE_REPLICATION
               (HSR mode, status, log shipping backlog),
               M_SYSTEM_REPLICATION_TAKEOVER_HISTORY, M_SQL_CLIENT_NETWORK_IO.
  cloud        SM69 ZAHC_CLOUDMETA run on each host with SXPG TARGETSYSTEM
               (script in deploy/zahc_cloudmeta.sh; reads the cloud
               metadata service: provider, region, zone, VM size, AWS
               placement group).

App server → DB latency, best source first (decision 2026-10-03: read-only):
  1. SMON NETRTT — "Network RTT (E. Acc.DB) in usec", measured by SMON on
     every instance each snapshot (verified field text on F4H);
  2. HANA M_SQL_CLIENT_NETWORK_IO — client minus server duration per
     CLIENT_HOST (empty until the SQL client network I/O trace is switched
     on — SAP Note 2081065; F4H: 0 rows);
  3. ST03N DB time per physical DB call per instance (µs) — includes HANA
     processing, so it ranks servers but is not a network figure.
SAP Note 1100926: app↔DB round trip ≤ 0.3 ms good, 0.3–0.7 ms moderate,
> 0.7 ms poor.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections import defaultdict
from typing import Optional

from engine.rfc.soap_client import RfcSoapError

from ahc.collect import to_float, to_int
from ahc.hana import HanaViaSm69
from ahc.sap import Sap, as_rows, first

from .collect import _components, _db_ms, _f, _index, _safe, _smon_dt, _ymd, tt_code

RTT_GOOD_US, RTT_WARN_US, RTT_SERIOUS_US = 300, 700, 1500        # SAP Note 1100926


def rate_rtt(us: Optional[float]) -> str:
    if us is None:
        return "unknown"
    return "good" if us <= RTT_GOOD_US else "warning" if us <= RTT_WARN_US else \
        "serious" if us <= RTT_SERIOUS_US else "critical"


def short_host(h: str) -> str:
    return (h or "").strip().split(".")[0].lower()


# ── ST03N per instance ────────────────────────────────────────────────────

def instance_workload(sap: Sap, sid: str, day: dt.date, notes: list[str]) -> list[dict]:
    """Dialog figures and DB time per DB call for every instance component of `day`.
    FACTOR=1 returns microseconds — needed because a HANA DB call takes well under a ms."""
    comps = [c for c in _components(sap, sid, day) if c != "TOTAL"]
    if not comps:          # TOTAL-only day: instances are whatever the index knows
        comps = sorted({r["COMPONENT"].strip() for r in _index(sap, sid)} - {"TOTAL"})
    out = []
    for comp in comps:
        try:
            r = sap.call("SWNC_COLLECTOR_GET_AGGREGATES", COMPONENT=comp, ASSIGNDSYS=sid, PERIODTYPE="D",
                         PERIODSTRT=day.isoformat(), FACTOR="1", TASKTYPE=[])
        except RfcSoapError as exc:
            if "NO_DATA_FOUND" not in exc.message:
                notes.append(f"ST03N {comp} {day}: {exc.message[:140]}")
            continue
        rows = as_rows(r.get("TASKTYPE"))
        if not rows:
            continue
        db_us = sum(_db_ms(t) for t in rows)                 # FACTOR=1 → these are µs
        calls = sum(to_float(t.get("PHYCALLS")) for t in rows)
        dia = [t for t in rows if tt_code(t.get("TASKTYPE")) == "01"]
        n = sum(to_float(first(t, "COUNT", "DCOUNT")) for t in dia)
        resp = sum(to_float(t.get("RESPTI")) for t in dia)
        ddb = sum(_db_ms(t) for t in dia)
        out.append({"component": comp, "date": day.isoformat(),
                    "steps": int(sum(to_float(first(t, "COUNT", "DCOUNT")) for t in rows)),
                    "dia_steps": int(n), "dia_resp_ms": round(resp / n / 1000, 1) if n else None,
                    "dia_db_ms": round(ddb / n / 1000, 1) if n else None,
                    "db_calls": int(calls), "db_us_per_call": round(db_us / calls) if calls else None})
    return out


# ── SMON per server ───────────────────────────────────────────────────────

SMON_SRV_FIELDS = ["DATUM", "TIME", "SERVER", "ACT_DIA", "IDLE_TOTAL", "FREE_MEM_PERC", "DIAQ", "USERS", "NETRTT"]


def smon_servers(sap: Sap, day: dt.date, since: Optional[dt.datetime] = None) -> dict[str, dict]:
    """Per SMON server: CPU, free memory, dialog queue, users and NETRTT (µs) — the newest snapshot
    plus averages over the window (`since` → live last hour, None → whole day)."""
    from .collect import smon_table
    tab, _ = smon_table(sap)
    if not tab:
        return {}
    from .collect import smon_read
    rows = smon_read(sap, tab, SMON_SRV_FIELDS, day, since)          # live hour may start yesterday
    have = set(sap.fields(tab))                  # /SDF/MON: no NETRTT, no FREE_MEM_PERC
    by: dict[str, list[tuple[dt.datetime, dict]]] = defaultdict(list)
    for r in rows:
        t = _smon_dt(r.get("DATUM", ""), r.get("TIME", ""))
        if t and (since is None or t >= since):
            by[(r.get("SERVER") or "").strip()].append((t, r))
    out = {}
    for srv, rs in by.items():
        rs.sort(key=lambda x: x[0])
        last = rs[-1][1]
        rtts = [to_float(r.get("NETRTT")) for _, r in rs if to_float(r.get("NETRTT")) > 0] if "NETRTT" in have else []
        cpus = [100 - to_float(r.get("IDLE_TOTAL")) for _, r in rs]
        out[srv] = {"last": rs[-1][0].strftime("%H:%M"), "samples": len(rs),
                    "cpu": round(100 - to_float(last.get("IDLE_TOTAL")), 1), "cpu_avg": round(sum(cpus) / len(cpus), 1),
                    "cpu_max": round(max(cpus), 1),
                    "free_mem": to_float(last.get("FREE_MEM_PERC")) if "FREE_MEM_PERC" in have else None,
                    "diaq": to_int(last.get("DIAQ")), "diaq_max": max(to_int(r.get("DIAQ")) for _, r in rs),
                    "users": to_int(last.get("USERS")), "act_dia": to_int(last.get("ACT_DIA")),
                    "rtt_us": round(sum(rtts) / len(rtts)) if rtts else None,
                    "rtt_max_us": round(max(rtts)) if rtts else None}
    return out


# ── HANA landscape + system replication ───────────────────────────────────

def _ts(v: str) -> Optional[dt.datetime]:
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d):(\d\d)", v or "")
    return dt.datetime(*map(int, m.groups())) if m else None


def hana_landscape(h: HanaViaSm69, notes: list[str]) -> dict:
    hosts = _safe(lambda: h.table(["HOST", "HOST_ACTIVE", "HOST_STATUS", "INDEXSERVER_ACTUAL_ROLE",
                                   "NAMESERVER_ACTUAL_ROLE"], "FROM M_LANDSCAPE_HOST_CONFIGURATION ORDER BY HOST"),
                  notes, "HANA landscape", [])
    hw = _safe(lambda: h.query("SELECT HOST, KEY, VALUE FROM M_HOST_INFORMATION "
                               "WHERE KEY IN ('hw_manufacturer', 'hw_model') ORDER BY HOST, KEY"),
               notes, "HANA host information", [])
    info: dict[str, dict] = defaultdict(dict)
    for r in hw:
        info[r.get("HOST", "")][r.get("KEY", "")] = (r.get("VALUE") or "").strip()
    out_hosts = []
    for r in hosts:
        i = info.get(r["HOST"], {})
        out_hosts.append({"host": r["HOST"], "active": (r.get("HOST_ACTIVE") or "").upper() == "YES",
                          "status": r.get("HOST_STATUS", ""), "role": (r.get("INDEXSERVER_ACTUAL_ROLE") or "").lower(),
                          "nameserver": (r.get("NAMESERVER_ACTUAL_ROLE") or "").lower(),
                          "vendor": i.get("hw_manufacturer", ""), "model": i.get("hw_model", ""),
                          "platform": platform_guess(i.get("hw_manufacturer", ""), i.get("hw_model", ""))})
    return {"hosts": out_hosts, "hsr": _safe(lambda: hsr(h), notes, "HANA system replication", None)}


def platform_guess(vendor: str, model: str) -> str:
    v, m = (vendor or "").lower(), (model or "").lower()
    if "amazon" in v or "amazon" in m or re.match(r"[a-z]\d[a-z0-9]*\.\d*x?(large|metal)", m):
        return "AWS"
    if "google" in v:
        return "Google Cloud"
    if "microsoft" in v and "virtual machine" in m:
        return "Azure or Hyper-V"           # an Azure VM and on-prem Hyper-V report the same
    if "xen" in v or "xen" in m:
        return "AWS (Xen) or Xen"
    if "vmware" in v or "vmware" in m:
        return "VMware"
    return "Physical / other" if v else ""


HSR_RANK = {"ACTIVE": "good", "SYNCING": "warning", "INITIALIZING": "warning", "UNKNOWN": "serious",
            "ERROR": "critical"}


def hsr(h: HanaViaSm69) -> Optional[dict]:
    """None when no system replication is configured (no M_SERVICE_REPLICATION rows)."""
    sites = h.table(["SITE_NAME", "SECONDARY_SITE_NAME", "REPLICATION_MODE", "REPLICATION_STATUS",
                     "OPERATION_MODE", "TIER"], "FROM M_SYSTEM_REPLICATION ORDER BY TIER, SECONDARY_SITE_NAME")
    svc = h.table(["HOST", "PORT", "SECONDARY_HOST", "SECONDARY_SITE_NAME", "REPLICATION_MODE",
                   "REPLICATION_STATUS", "SECONDARY_ACTIVE_STATUS", "LAST_LOG_POSITION_TIME",
                   "SHIPPED_LOG_POSITION_TIME", "BACKLOG_SIZE", "BACKLOG_TIME", "SECONDARY_RECONNECT_COUNT",
                   "SECONDARY_FAILOVER_COUNT"], "FROM M_SERVICE_REPLICATION ORDER BY HOST, PORT, SECONDARY_SITE_NAME")
    if not svc and not sites:
        return None
    detail = _safe(lambda: h.table(["HOST", "PORT", "SECONDARY_SITE_NAME", "REPLICATION_STATUS_DETAILS"],
                                   "FROM M_SERVICE_REPLICATION ORDER BY HOST, PORT, SECONDARY_SITE_NAME"), [], "", [])
    services = []
    for i, r in enumerate(svc):
        last, shipped = _ts(r.get("LAST_LOG_POSITION_TIME")), _ts(r.get("SHIPPED_LOG_POSITION_TIME"))
        delay = max(0.0, (last - shipped).total_seconds()) if last and shipped else None
        bt = _f(r.get("BACKLOG_TIME"))
        services.append({"host": r["HOST"], "port": r.get("PORT", ""), "secondary_host": r.get("SECONDARY_HOST", ""),
                         "secondary_site": r.get("SECONDARY_SITE_NAME", ""), "mode": r.get("REPLICATION_MODE", ""),
                         "status": (r.get("REPLICATION_STATUS") or "").upper(),
                         "secondary_active": (r.get("SECONDARY_ACTIVE_STATUS") or "").upper(),
                         "shipping_delay_s": delay,
                         "backlog_mb": round((_f(r.get("BACKLOG_SIZE")) or 0) / 1024 ** 2, 1),
                         "backlog_s": round(bt / 1e6, 1) if bt is not None else None,
                         "reconnects": to_int(r.get("SECONDARY_RECONNECT_COUNT")),
                         "failovers": to_int(r.get("SECONDARY_FAILOVER_COUNT")),
                         "details": (detail[i].get("REPLICATION_STATUS_DETAILS") if i < len(detail) else "") or ""})
    worst = max((s["status"] for s in services), key=lambda s: ["ACTIVE", "SYNCING", "INITIALIZING",
                                                               "UNKNOWN", "ERROR"].index(s)
                if s in ("ACTIVE", "SYNCING", "INITIALIZING", "UNKNOWN", "ERROR") else 3, default="UNKNOWN")
    lag = [x for s in services for x in (s["backlog_s"], s["shipping_delay_s"]) if x is not None]
    primary = sites[0].get("SITE_NAME", "") if sites else ""
    return {"primary_site": primary,
            "sites": [{"site": r.get("SECONDARY_SITE_NAME", ""), "source": r.get("SITE_NAME", ""),
                       "mode": r.get("REPLICATION_MODE", ""), "status": (r.get("REPLICATION_STATUS") or "").upper(),
                       "operation_mode": r.get("OPERATION_MODE", ""), "tier": to_int(r.get("TIER"), 2),
                       "hosts": sorted({s["secondary_host"] for s in services
                                        if s["secondary_site"] == r.get("SECONDARY_SITE_NAME", "")})}
                      for r in sites],
            "services": services, "status": worst, "max_lag_s": round(max(lag), 1) if lag else None}


def takeovers(h: HanaViaSm69, day: dt.date) -> list[dict]:
    nxt = day + dt.timedelta(days=1)
    rows = h.table(["TAKEOVER_START_TIME", "TAKEOVER_END_TIME", "SITE_NAME", "SOURCE_SITE_NAME", "TAKEOVER_TYPE",
                    "REPLICATION_MODE"],
                   f"FROM M_SYSTEM_REPLICATION_TAKEOVER_HISTORY WHERE TAKEOVER_START_TIME >= '{day}' "
                   f"AND TAKEOVER_START_TIME < '{nxt}' ORDER BY TAKEOVER_START_TIME")
    return [{"start": r["TAKEOVER_START_TIME"][:19], "end": (r.get("TAKEOVER_END_TIME") or "")[:19],
             "to_site": r.get("SITE_NAME", ""), "from_site": r.get("SOURCE_SITE_NAME", ""),
             "type": r.get("TAKEOVER_TYPE", ""), "mode": r.get("REPLICATION_MODE", "")} for r in rows]


def hana_client_rtt(h: HanaViaSm69) -> dict[str, dict]:
    """Network part of each SQL round trip per client host (µs), from the client network I/O trace."""
    rows = h.query("SELECT CLIENT_HOST AS H, COUNT(*) AS N, AVG(CLIENT_DURATION - SERVER_DURATION) AS D "
                   "FROM M_SQL_CLIENT_NETWORK_IO WHERE CLIENT_DURATION >= SERVER_DURATION GROUP BY CLIENT_HOST")
    return {short_host(r.get("H", "")): {"rtt_us": round(_f(r.get("D")) or 0), "samples": to_int(r.get("N"))}
            for r in rows if r.get("H")}


# ── cloud placement (SM69 ZAHC_CLOUDMETA per host) ─────────────────────────

CLOUD_COMMAND = "ZAHC_CLOUDMETA"
CLOUD_KEYS = ("provider", "region", "zone", "vm_size", "placement_group", "instance_id", "error")
_CLOUD_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
CLOUD_TTL_S = 3600                               # placement changes only with a redeploy


def cloud_meta(sap: Sap, sid: str, host: str) -> dict:
    """Run ZAHC_CLOUDMETA on `host` (SXPG TARGETSYSTEM). Returns {key: value} or {"error": why}."""
    cmd = os.environ.get("AHC_CLOUDMETA_COMMAND", CLOUD_COMMAND)
    key = (sid, host)
    hit = _CLOUD_CACHE.get(key)
    if hit and time.time() - hit[0] < (CLOUD_TTL_S if not hit[1].get("error") else 60):
        return hit[1]
    try:
        r = sap.call("SXPG_COMMAND_EXECUTE", COMMANDNAME=cmd, ADDITIONAL_PARAMETERS="", TARGETSYSTEM=host,
                     EXEC_PROTOCOL=[], _timeout_s=30)
        lines = [(p.get("MESSAGE") or "").strip() for p in as_rows(r.get("EXEC_PROTOCOL"))]
        out = {}
        for line in lines:
            k, _, v = line.partition("=")
            if k.strip() in CLOUD_KEYS and v.strip():
                out[k.strip()] = v.strip()[:80]
        if not out:
            out = {"error": "no metadata returned (" + " ".join(lines)[:80] + ")"}
    except RfcSoapError as exc:
        msg = exc.message
        out = {"error": f"SM69 command {cmd} does not exist — see deploy/sap/zahc_cloudmeta.sh"
               if "COMMAND_NOT_FOUND" in msg else
               f"cannot run {cmd} on {host}: {msg[:100]}"}
    _CLOUD_CACHE[key] = (time.time(), out)
    return out


def placement(sap: Sap, sid: str, app_hosts: list[str], db_hosts: list[str], notes: list[str]) -> dict:
    hosts = list(dict.fromkeys([*app_hosts, *db_hosts]))
    meta = {hh: cloud_meta(sap, sid, hh) for hh in hosts if hh}
    errs = {m["error"] for m in meta.values() if m.get("error")}
    if errs and all(m.get("error") for m in meta.values()):
        notes.append("Cloud placement: " + sorted(errs)[0])
    return meta


def _host_of(instance: str) -> str:
    """'appsrv2_PRD_01' → 'appsrv2' (SAP instance names are <host>_<SID>_<nr>)."""
    m = re.match(r"(.+)_[A-Z0-9]{3}_\d{2}$", instance or "")
    return m.group(1) if m else ""


# ── putting the app tier together ─────────────────────────────────────────

def app_servers(servers: list[dict], local: str, wl: list[dict], smon: dict[str, dict], client_rtt: dict[str, dict],
                cloud: dict[str, dict], db_hosts: list[str], local_os: dict, live: bool = True) -> dict:
    """One row per application server + the latency ranking and the nearest server to the database."""
    by_comp = {w["component"]: w for w in wl}
    db_short = {short_host(x) for x in db_hosts if x}
    db_meta = next((cloud[h] for h in db_hosts if h in cloud and not cloud[h].get("error")), {})
    rows = []
    known = {s.get("name", "") for s in servers}
    # instances that recorded workload but are not in today's server list (removed / stopped since)
    servers = list(servers) + [{"name": w["component"], "host": _host_of(w["component"]), "active": False,
                                "historic": True} for w in wl if w["component"] not in known]
    for s in servers:
        name, host = s.get("name", ""), s.get("host", "")
        w, sm = by_comp.get(name, {}), smon.get(name, {})
        cm = cloud.get(host, {})
        is_local = name == local
        row = {"name": name, "host": host, "active": s.get("active", True), "local": is_local,
               "historic": s.get("historic", False),
               "services": s.get("services", ""),
               "wp": s.get("wp", {}), "busy_dia": s.get("busy_dia"), "total_dia": s.get("total_dia"),
               "cpu": sm.get("cpu") if sm else (local_os.get("cpu") if is_local else None),
               "free_mem": sm.get("free_mem") if sm else (local_os.get("free_mem") if is_local else None),
               "os_source": "SMON" if sm else ("ST06 (this instance)" if is_local else ""),
               "diaq": sm.get("diaq"), "users": sm.get("users") if sm else (local_os.get("users") if is_local else None),
               "dia_steps": w.get("dia_steps"), "dia_resp_ms": w.get("dia_resp_ms"), "dia_db_ms": w.get("dia_db_ms"),
               "db_us_per_call": w.get("db_us_per_call"), "workload_date": w.get("date"),
               "rtt_smon_us": sm.get("rtt_us"), "rtt_hana_us": client_rtt.get(short_host(host), {}).get("rtt_us"),
               "same_host_as_db": short_host(host) in db_short,
               "cloud": {k: cm.get(k) for k in CLOUD_KEYS if cm.get(k) and k != "error"}}
        if db_meta and cm and not cm.get("error"):
            row["same_zone_as_db"] = bool(cm.get("zone")) and cm.get("zone") == db_meta.get("zone")
            row["same_pg_as_db"] = (bool(cm.get("placement_group")) and
                                    cm.get("placement_group") == db_meta.get("placement_group"))
        rows.append(row)
    # pick the best latency source that covers every active server
    # live: only running instances can be "nearest"; a day view ranks every instance that worked that day
    act = [r for r in rows if r["active"] and not r["historic"]] if live else \
        [r for r in rows if r["historic"] or r["dia_steps"] is not None or r["rtt_smon_us"] is not None or r["active"]]
    act = act or rows
    source, field, measured = None, None, False
    for src, fld, meas in (("smon", "rtt_smon_us", True), ("hana_trace", "rtt_hana_us", True),
                           ("st03n", "db_us_per_call", False)):
        if act and all(r[fld] is not None for r in act):
            source, field, measured = src, fld, meas
            break
    if source is None:      # partial coverage: rank what we have with the best partial source
        for src, fld, meas in (("smon", "rtt_smon_us", True), ("hana_trace", "rtt_hana_us", True),
                               ("st03n", "db_us_per_call", False)):
            if any(r[fld] is not None for r in act):
                source, field, measured = src, fld, meas
                break
    nearest = None
    if field:
        ranked = sorted([r for r in act if r[field] is not None], key=lambda r: r[field])
        for i, r in enumerate(ranked, 1):
            r["latency_rank"] = i
            r["latency_us"] = r[field]
            r["latency_rating"] = rate_rtt(r[field]) if measured else "unknown"
        nearest = ranked[0]["name"] if ranked else None
        for r in rows:
            r["nearest"] = r["name"] == nearest
    if nearest is None and len(act) == 1:          # one instance: it is the closest by definition
        nearest = act[0]["name"]
        for r in rows:
            r["nearest"] = r["name"] == nearest
    labels = {"smon": "Network round trip to the database measured by SMON (NETRTT)",
              "hana_trace": "Network part of SQL round trips, HANA client network I/O trace",
              "st03n": "DB time per DB call from ST03N — includes HANA processing; ranks servers, "
                       "not a pure network figure"}
    how = []
    if source != "smon":
        how.append("Schedule /SDF/SMON (it measures NETRTT from every instance to the database).")
    if source not in ("smon", "hana_trace"):
        how.append("Or switch on HANA's SQL client network I/O trace for a while (SAP Note 2081065) — "
                   "M_SQL_CLIENT_NETWORK_IO then shows the network share of every round trip per app server.")
    return {"servers": rows, "source": source, "source_label": labels.get(source, "No latency source available"),
            "measured": measured, "nearest": nearest, "how_to_measure": how,
            "count": sum(1 for r in rows if not r["historic"]),
            "active": sum(1 for r in rows if r["active"] and not r["historic"])}
