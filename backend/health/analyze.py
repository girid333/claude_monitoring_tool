"""Turn an AHC workbook into a triaged HealthReport.

SAP's Automated Health Check says OK / Not OK per check. It does not say
WHICH certificate, WHICH lock, or whether a flag is by-design behaviour.
Each analyser reads the evidence tab and re-assesses the flag.

Invariant: an analyser failure never fails the import. The check falls back
to SAP's own verdict and records why it could not be analysed.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections import Counter, defaultdict
from typing import Callable, Optional

from . import kb
from .models import (BarItem, Chart, Check, HealthReport, Metric, Summary,
                     SystemInfo, TableOut)
from .workbook import Sheet, Table

MAX_TABLE_ROWS = 300
MAX_TEXT_CHARS = 60_000

# ── small helpers ─────────────────────────────────────────────────────────

_DT_PATTERNS = [
    (re.compile(r"(\d{2})[./](\d{2})[./](\d{4})\D{0,4}(\d{2}):(\d{2}):(\d{2})"), "dmyhms"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})\D{1,5}(\d{2}):(\d{2}):(\d{2})"), "ymdhms"),
    (re.compile(r"(\d{2})[./](\d{2})[./](\d{4})"), "dmy"),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd"),
]


def parse_dt(s: str) -> Optional[dt.datetime]:
    for rx, kind in _DT_PATTERNS:
        m = rx.search(s or "")
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        try:
            if kind == "dmyhms":
                return dt.datetime(g[2], g[1], g[0], g[3], g[4], g[5])
            if kind == "ymdhms":
                return dt.datetime(g[0], g[1], g[2], g[3], g[4], g[5])
            if kind == "dmy":
                return dt.datetime(g[2], g[1], g[0])
            return dt.datetime(g[0], g[1], g[2])
        except ValueError:
            continue
    return None


def num(s: str, european: bool = False) -> Optional[float]:
    s = (s or "").strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    if european:
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def col(table: Table, *needles: str) -> Optional[int]:
    """Index of the first column whose header contains any needle."""
    for i, c in enumerate(table.columns):
        cl = c.lower()
        if any(n.lower() in cl for n in needles):
            return i
    return None


def cell(row: list[str], i: Optional[int]) -> str:
    return row[i] if i is not None and i < len(row) else ""


def plural(n: int, word: str, many: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")[:60]


def pct_tone(p: float, warn: float = 70, bad: float = 85) -> str:
    return "bad" if p >= bad else "warn" if p >= warn else "good"


class Ctx:
    def __init__(self, report_dt: Optional[dt.datetime], sid: str):
        self.report_dt = report_dt
        self.snapshot_dt = report_dt
        self.sid = sid


# ── analysers ─────────────────────────────────────────────────────────────
# Each returns a dict of Check fields to override. Missing keys keep defaults.

def a_sm21(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ip, it, im = col(t, "priority"), col(t, "message id"), col(t, "message text")
    rows = [r for r in t.rows if cell(r, it) or cell(r, im)]
    prio = Counter(cell(r, ip) or "Unspecified" for r in rows)
    high_idx = [i for i, r in enumerate(rows) if "high" in cell(r, ip).lower()]
    heads = [i for i in high_idx if not cell(rows[i], im).startswith(">")]
    groups: dict[str, list[int]] = defaultdict(list)
    for i in heads:
        groups[cell(rows[i], it) or "?"].append(i)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    def continuation(i: int) -> list[str]:
        # SAP writes the detail of a message as following lines starting with ">"
        out = []
        for r in rows[i + 1: i + 6]:
            txt = cell(r, im)
            if not txt.startswith(">"):
                break
            if txt.strip("> ").strip():
                out.append(txt.lstrip("> ").strip())
        return out

    order = {"ok": 0, "expected": 1, "review": 2, "act": 3}
    findings, worst = [], ("expected" if heads else "ok")
    by_severity = sorted(ranked, key=lambda kv: (-order[kb.SYSLOG.get(kv[0], ("review",))[0]], -len(kv[1])))
    for mid, idx in by_severity[:8]:
        tri, meaning, _ = kb.SYSLOG.get(mid, ("review", "", ""))
        worst = max(worst, tri, key=order.get)
        detail = list(dict.fromkeys(d for i in idx[:3] for d in continuation(i)))[:3]
        sample = cell(rows[idx[0]], im)
        text = f"{mid} × {len(idx)}: {meaning or sample}"
        if detail:
            text += " — " + "; ".join(detail)
        elif meaning:
            text += f" — “{sample[:90]}”"
        findings.append(text)
    if len(ranked) > 8:
        findings.append(f"…and {plural(len(ranked) - 8, 'more message type')}.")

    if not high_idx:
        headline = f"No high-priority messages among {plural(len(rows), 'syslog entry', 'syslog entries')}."
    else:
        headline = (f"{plural(len(high_idx), 'high-priority message')} in "
                    f"{plural(len(groups), 'distinct type')}")
        act_groups = [(mid, idx) for mid, idx in ranked if kb.SYSLOG.get(mid, ("",))[0] == "act"]
        lead = act_groups[0] if act_groups else (ranked[0] if ranked else None)
        if lead:
            m = kb.SYSLOG.get(lead[0], (None, "", None))[1] or cell(rows[lead[1][0]], im)[:60]
            m = m[:1].lower() + m[1:] if m[1:2].islower() else m
            headline += (f"; {len(lead[1])} are {m} ({lead[0]})." if act_groups
                         else f"; the most frequent is {lead[0]} ({len(lead[1])}×, {m}).")
        else:
            headline += "."
    recs = [kb.SYSLOG[mid][2] for mid, _ in sorted(ranked[:4], key=lambda kv: -order[kb.SYSLOG.get(kv[0], ("review",))[0]])
            if mid in kb.SYSLOG and kb.SYSLOG[mid][0] in ("act", "review")]
    return dict(
        triage=worst, headline=headline, findings=findings,
        recommendation=" ".join(dict.fromkeys(recs)) or None,
        metrics=[Metric(label="Entries", value=str(len(rows))),
                 Metric(label="High priority", value=str(len(high_idx)),
                        tone="warn" if high_idx else "good"),
                 Metric(label="Message types", value=str(len(groups)))],
        charts=[Chart(title="High-priority messages by ID", items=[
            BarItem(label=mid, value=len(idx),
                    tone={"act": "bad", "review": "warn"}.get(kb.SYSLOG.get(mid, ("review",))[0], "neutral"),
                    note=kb.SYSLOG.get(mid, ("", cell(rows[idx[0]], im)[:50]))[1])
            for mid, idx in ranked[:10]])] if ranked else [],
    )


def a_sm12(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    iu, idt, itab = col(t, "user"), col(t, "date"), col(t, "table")
    m = re.search(r"more than (\d+) hour", base.get("sap_evidence", ""))
    limit_h = int(m.group(1)) if m else 5
    locks = []
    for r in t.rows:
        when = parse_dt(cell(r, idt))
        if when:
            locks.append((r, when))
    now = c.snapshot_dt or (max(w for _, w in locks) if locks else None)
    old, by_design = [], []
    for r, when in locks:
        age = (now - when).total_seconds() / 3600 if now else 0
        if age >= limit_h:
            (by_design if cell(r, itab).startswith(kb.PERSISTENT_LOCK_PREFIXES)
             else old).append((r, age))
    def summarise(items, suffix=""):
        agg = Counter((cell(r, itab), cell(r, iu), round(age)) for r, age in items)
        return [f"{tab} held by {user} for {age} h" + (f" ({n} entries)" if n > 1 else "") + suffix
                for (tab, user, age), n in sorted(agg.items(), key=lambda kv: -kv[0][2])[:10]]
    findings = summarise(old) + summarise(by_design, " — held permanently by design")
    if old:
        tri = "act" if any(a > 24 for _, a in old) else "review"
        head = (f"{plural(len(old), 'lock')} older than {limit_h} h not explained by "
                f"standard daemons; oldest {max(a for _, a in old):.0f} h.")
        rec = ("Identify the owning process in SM50/SM66 before deleting anything. "
               "Delete a lock only when its owner no longer exists.")
    elif by_design:
        tri, head = "expected", (f"All {plural(len(by_design), 'long-held lock')} belong to "
                                 "standard daemons (bgRFC registration, timer daemon, SMON) "
                                 "that hold them permanently.")
        rec = None
    else:
        tri, head, rec = "ok", f"{plural(len(locks), 'lock entry', 'lock entries')}; none older than {limit_h} h.", None
    return dict(triage=tri, headline=head, findings=findings, recommendation=rec,
                metrics=[Metric(label="Lock entries", value=str(len(t.rows))),
                         Metric(label=f"Older than {limit_h} h", value=str(len(old) + len(by_design))),
                         Metric(label="Unexplained", value=str(len(old)),
                                tone="bad" if old else "good")])


def _instance_sid(inst: str) -> str:
    m = re.search(r"_([A-Z0-9]{3})_\d{2}$", inst or "")
    return m.group(1) if m else ""


def a_smlg(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ig, ii, ist = col(t, "logon group"), col(t, "instance"), col(t, "status")
    bad = [r for r in t.rows if "not" in cell(r, ist).lower() or "red" in cell(r, ist).lower()]
    foreign = [r for r in bad if _instance_sid(cell(r, ii)) and c.sid
               and _instance_sid(cell(r, ii)) != c.sid]
    findings = []
    for r in bad:
        s = _instance_sid(cell(r, ii))
        extra = f" — an instance of system {s}, not {c.sid}" if r in foreign else ""
        findings.append(f"Group {cell(r, ig)}: {cell(r, ii)} is {cell(r, ist)}{extra}")
    groups_all = {cell(r, ig) for r in t.rows}
    groups_ok = {cell(r, ig) for r in t.rows if r not in bad}
    dead = sorted(groups_all - groups_ok)
    if dead:
        findings.append("No healthy instance left in group " + ", ".join(dead)
                        + ": logons through it cannot be balanced to a server.")
    if not bad:
        return dict(triage="ok", headline="All logon group instances are green.")
    if foreign:
        head = (f"{plural(len(foreign), 'instance')} from other systems "
                f"({', '.join(sorted({_instance_sid(cell(r, ii)) for r in foreign}))}) "
                "still assigned to logon groups here")
        head += "; group " + dead[0] + " has no usable instance." if dead else "."
        rec = ("Remove the foreign instances in SMLG — typically a leftover from a "
               "system copy — and make sure every group has at least one local instance.")
        return dict(triage="act", headline=head, findings=findings, recommendation=rec)
    return dict(triage="act" if dead else "review",
                headline=f"{plural(len(bad), 'instance')} not green in logon groups.",
                findings=findings,
                recommendation="Check the instance in SM51/SMLG; a stopped instance "
                               "in a logon group degrades load balancing.")


_PSE = re.compile(r"^(\S.*?)\s{3,}([A-Z]{4}) (\S+)\s*$")
_CERT = re.compile(r"^\s*(?:\d+\s*=\s*)?((?:CN|OU|O|EMAIL|C|L)=.*?)\s{2,}(\d{2}\.\d{2}\.\d{4})\s*$")


def a_cert(sh: Sheet, c: Ctx, base: dict) -> dict:
    m = re.search(r"next (\d+) days", base.get("description", "") + base.get("sap_evidence", ""))
    horizon = int(m.group(1)) if m else 30
    pse, kind = "", "own"
    certs: dict[tuple, dict] = {}
    for line in sh.text.split("\n"):
        pm = _PSE.match(line)
        if pm and "=" not in pm.group(1):
            pse = f"{pm.group(1).strip()} ({pm.group(2)} {pm.group(3)})"
            continue
        if line.strip() == "Own Certificate":
            kind = "own"
        elif line.strip() == "Certificate List":
            kind = "trusted"
        cm = _CERT.match(line)
        if cm:
            exp = parse_dt(cm.group(2))
            subj = cm.group(1).strip()
            key = (pse, subj, cm.group(2))
            if exp and key not in certs:
                certs[key] = dict(pse=pse, subject=subj, kind=kind, expires=exp)
    ref = c.report_dt or dt.datetime.now()
    rows = sorted(certs.values(), key=lambda x: x["expires"])
    for x in rows:
        x["days"] = (x["expires"] - ref).days
    expired = [x for x in rows if x["days"] < 0]
    soon = [x for x in rows if 0 <= x["days"] <= horizon]

    def cn(s: str) -> str:
        mm = re.search(r"CN=([^,]+)", s)
        return (mm.group(1) if mm else s)[:48]

    findings = [f"{cn(x['subject'])} in {x['pse'] or 'unknown PSE'} "
                f"{'expired' if x['days'] < 0 else 'expires'} {x['expires']:%d.%m.%Y}"
                f" ({abs(x['days'])} days{' ago' if x['days'] < 0 else ''})"
                + (" — own certificate" if x["kind"] == "own" else "")
                for x in (expired + soon)[:12]]
    if expired or soon:
        nxt = (expired + soon)[0]
        head = (f"{plural(len(expired), 'certificate')} expired, " if expired else "") + \
               f"{plural(len(soon), 'certificate')} expiring within {horizon} days; " \
               f"most urgent: {cn(nxt['subject'])} " \
               f"({'expired' if nxt['days'] < 0 else 'expires'} {nxt['expires']:%d.%m.%Y})."
        own_soon = [x for x in expired + soon if x["kind"] == "own"]
        rec = ("Renew the listed certificates in STRUST before expiry. "
               + ("An OWN certificate is affected — that breaks the server's TLS or "
                  "SSO identity, so treat it first. " if own_soon else
                  "All are trusted partner certificates: import the partner's renewed "
                  "certificate (or its new CA) into the same PSE. "))
        tri = "act"
    else:
        head, rec, tri = f"No certificate expires within {horizon} days.", None, "ok"
    chart_items = [BarItem(label=cn(x["subject"]), value=max(x["days"], 0),
                           tone="bad" if x["days"] <= 14 else "warn" if x["days"] <= horizon else "good",
                           note=x["pse"]) for x in rows[:8]]
    table = TableOut(title="Certificates by expiry", columns=["Subject", "PSE", "Type", "Expires", "Days left"],
                     rows=[[x["subject"], x["pse"], x["kind"], f"{x['expires']:%d.%m.%Y}", str(x["days"])]
                           for x in rows[:MAX_TABLE_ROWS]], total=len(rows))
    return dict(triage=tri, headline=head, findings=findings, recommendation=rec,
                metrics=[Metric(label="Certificates", value=str(len(rows))),
                         Metric(label=f"Expiring ≤ {horizon} d", value=str(len(soon)),
                                tone="warn" if soon else "good"),
                         Metric(label="Expired", value=str(len(expired)),
                                tone="bad" if expired else "good")],
                charts=[Chart(title="Days until expiry — soonest first", unit="days", items=chart_items)] if rows else [],
                _extra_tables=[table])


def a_slicense(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ist, ihw, iprod, iend = col(t, "status text"), col(t, "hardware"), col(t, "sw product"), col(t, "end date")
    rows = [r for r in t.rows if cell(r, ist) or cell(r, iprod)]
    ref = c.report_dt or dt.datetime.now()
    invalid, inactive, expiring = [], [], []
    for r in rows:
        s = cell(r, ist).lower()
        end = parse_dt(cell(r, iend))
        if not s.startswith("valid"):
            invalid.append(r)
        elif "inactive" in s:
            inactive.append(r)
        if end and 0 <= (end - ref).days <= 30:
            expiring.append(r)
    findings = [f"{cell(r, iprod)} on hardware key {cell(r, ihw)}: {cell(r, ist) or 'no status'}"
                for r in invalid + inactive]
    findings += [f"{cell(r, iprod)} ends {cell(r, iend)}" for r in expiring]
    if invalid or expiring:
        return dict(triage="act", headline=f"{plural(len(invalid), 'license')} invalid"
                    + (f", {len(expiring)} ending within 30 days" if expiring else "") + ".",
                    findings=findings,
                    recommendation="Request and install new license keys (SLICENSE) for the "
                                   "affected hardware key before the grace period ends.")
    if inactive:
        keys = sorted({cell(r, ihw) for r in inactive})
        return dict(triage="expected",
                    headline=f"All licenses are valid. {plural(len(inactive), 'entry', 'entries')} "
                             f"for hardware key {', '.join(keys)} are valid but inactive.",
                    findings=findings,
                    recommendation="Inactive-hardware-key licenses are normal for the second "
                                   "node of a high-availability pair. Confirm the key belongs "
                                   "to your failover host; if it belongs to a decommissioned "
                                   "host, delete the entry.")
    return dict(triage="ok", headline=f"All {plural(len(rows), 'license')} valid.")


def a_db02_alerts(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ip, idesc = col(t, "priority"), col(t, "description")
    prios = Counter(cell(r, ip) or "Unspecified" for r in t.rows)
    def kind(d: str) -> str:
        d = d.split(". ")[0]
        d = re.sub(r"\b(user|table|volume|service|host|file) \S+", r"\1 …", d)
        d = re.sub(r"\s+on \d{4}-\d{2}-\d{2}.*$", "", d)
        d = re.sub(r"\d[\d.:\-]*", "…", d)
        return d[:90]
    kinds = Counter(kind(cell(r, idesc)) for r in t.rows)
    ref = c.report_dt or dt.datetime.now()
    pw = []
    for r in t.rows:
        m = re.search(r"database user (\S+) will expire on (\d{4}-\d{2}-\d{2})", cell(r, idesc))
        if m:
            pw.append((m.group(1), dt.datetime.fromisoformat(m.group(2))))
    pw.sort(key=lambda x: x[1])
    severe = prios.get("Error", 0) + prios.get("High", 0)
    urgent = [(u, d) for u, d in pw if (d - ref).days <= 7]
    findings = [f"{n} × {k}" for k, n in kinds.most_common(5)
                if not (pw and "password" in k.lower())]
    if pw:
        findings.append(f"{plural(len(pw), 'database user password')} expiring; earliest "
                        f"{pw[0][1]:%d.%m.%Y}"
                        + (" — the day of the check." if (pw[0][1] - ref).days <= 0
                           else f" ({(pw[0][1] - ref).days} days after the check)."))
    if severe:
        tri, head = "act", f"{plural(severe, 'error or high-priority alert')} open."
    elif urgent:
        tri = "act"
        head = (f"{plural(len(urgent), 'database user password')} expire within 7 days "
                f"(first on {urgent[0][1]:%d.%m.%Y}); expired users get locked.")
    elif t.rows:
        tri, head = "review", f"{plural(len(t.rows), 'alert')}, none above medium priority."
    else:
        tri, head = "ok", "No current alerts."
    rec = ("Change the passwords before they expire or, for technical users, disable "
           "password lifetime per your security policy — a locked technical user "
           "silently stops the interface or job that uses it.") if pw else None
    return dict(triage=tri, headline=head, findings=findings, recommendation=rec,
                metrics=[Metric(label=p, value=str(n),
                                tone="bad" if p in ("Error", "High") else "warn" if p == "Medium" else "neutral")
                         for p, n in prios.most_common()],
                charts=[Chart(title="Alerts by type", items=[
                    BarItem(label=k[:40], value=n) for k, n in kinds.most_common(6)])] if kinds else [])


def a_db02_log(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    it, ie = col(t, "event time"), col(t, "event name")
    ev = sorted([(parse_dt(cell(r, it)), cell(r, ie)) for r in t.rows if parse_dt(cell(r, it))],
                key=lambda x: x[0])
    if not ev:
        return dict(triage="ok" if base["sap_status"] == "ok" else "review",
                    headline="No availability events recorded.")
    last_t, last_e = ev[-1]
    ref = c.snapshot_dt or c.report_dt or last_t
    age_h = (ref - last_t).total_seconds() / 3600
    recent_bad = [e for t_, e in ev if (ref - t_).total_seconds() < 86400
                  and any(k in e for k in ("STOP", "ERROR", "CRASH", "FAIL"))]
    stops = Counter(t_.date() for t_, e in ev if "STOPPED" in e)
    findings = [f"{d:%d.%m.%Y}: service restart" for d in sorted(stops)[-5:]]
    if recent_bad:
        return dict(triage="act", headline=f"{plural(len(recent_bad), 'stop or error event')} in the last 24 h.",
                    findings=findings, recommendation="Check the HANA trace files for the "
                    "affected service and the reason for the stop.")
    return dict(triage="expected" if base["sap_status"] == "not_ok" else "ok",
                headline=(f"No stop or error events in the last 24 hours. The last event was "
                          f"{last_e} on {last_t:%d.%m.%Y} ({age_h:.0f} h earlier)." if age_h < 24 else
                          f"No availability events in the last 24 hours — the database has been "
                          f"stable. The last event was {last_e} on {last_t:%d.%m.%Y} "
                          f"({age_h / 24:.0f} days earlier)."),
                findings=findings,
                recommendation=None)


_PAIR = re.compile(r"([\d.,]+)\s*GB\s*/\s*([\d.,]+)\s*GB")


def a_db02_memory(sh: Sheet, c: Ctx, base: dict) -> dict:
    items, findings = [], []
    for t in sh.tables:
        for r in t.rows:
            m = _PAIR.search(" ".join(r))
            if not m:
                continue
            used, total = num(m.group(1)), num(m.group(2))
            if not used or not total or total > 1e6:   # 8589934592 GB = "unbounded" placeholder
                findings.append(f"{r[0]}: {r[1]} (total not reported)")
                continue
            p = used / total * 100
            items.append(BarItem(label=r[0].split("/")[0].strip(), value=round(p, 1),
                                 tone=pct_tone(p), note=f"{used:,.0f} of {total:,.0f} GB"))
    worst = max(items, key=lambda x: x.value, default=None)
    if not items:
        return dict(triage="ok" if base["sap_status"] == "ok" else "review",
                    headline="No usage figures found.")
    tri = "act" if worst.value >= 90 else "review" if worst.value >= 80 else "ok"
    return dict(triage=tri, headline=f"Highest utilisation: {worst.label} at {worst.value:.0f}% "
                f"({worst.note}).", findings=findings,
                charts=[Chart(title="Utilisation", unit="%", items=items)],
                recommendation=("Plan capacity for the resource above 80%."
                                if tri != "ok" else None))


def a_sm04(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ip, it, iu = col(t, "priority"), col(t, "type of user session"), col(t, "user id")
    types = Counter(cell(r, it) or "Unspecified" for r in t.rows)
    hi = [r for r in t.rows if cell(r, ip).lower() == "high"]
    by_design = [r for r in hi if any(k in cell(r, it).upper() for k in kb.HIGH_PRIO_BY_DESIGN)]
    other = [r for r in hi if r not in by_design]
    findings = [f"{cell(r, iu) or '(no user)'} — {cell(r, it)} session, high priority"
                + (" (bgRFC schedulers run at high priority by design)" if r in by_design else "")
                for r in hi]
    if other:
        tri = "review"
        head = (f"{plural(len(hi), 'high-priority session')}: {len(by_design)} are bgRFC "
                f"schedulers (by design), {len(other)} {'needs' if len(other) == 1 else 'need'} a look.") if by_design else \
               f"{plural(len(other), 'high-priority session')} from regular users or RFC."
        rec = "Check why these sessions were raised to high priority (SM04 → session details)."
    elif hi:
        tri, head, rec = "expected", "All high-priority sessions are bgRFC schedulers, which run at high priority by design.", None
    else:
        tri, head, rec = "ok", f"{plural(len(t.rows), 'session')}; none at high priority.", None
    return dict(triage=tri, headline=head, findings=findings, recommendation=rec,
                metrics=[Metric(label="Sessions", value=str(len(t.rows))),
                         Metric(label="High priority", value=str(len(hi)))],
                charts=[Chart(title="Sessions by type", items=[BarItem(label=k, value=v) for k, v in types.most_common(6)])])


def a_st02(sh: Sheet, c: Ctx, base: dict) -> dict:
    low, swaps, mem = [], 0, []
    servers = []
    for t in sh.tables:
        if col(t, "hit ratio") is not None and col(t, "buffer") == 0:
            servers.append(t.title.replace("Tune Summary", "").strip(" ()"))
            ih, isw = col(t, "hit ratio"), col(t, "swaps")
            for r in t.rows:
                h, s = num(cell(r, ih)), num(cell(r, isw))
                if s:
                    swaps += int(s)
                if h is not None and h < 97 and r[0]:
                    low.append(f"{servers[-1]}: {r[0]} buffer hit ratio {h:.2f}%")
        elif col(t, "curr. use %") is not None:
            iu = col(t, "curr. use %")
            for r in t.rows:
                u = num(cell(r, iu))
                if u is not None:
                    mem.append(BarItem(label=f"{r[0]} · {servers[-1] if servers else ''}".strip(" ·"),
                                       value=round(u, 1), tone=pct_tone(u, 80, 95)))
    high_mem = [m for m in mem if m.value >= 95]
    tri = "review" if (low or swaps or high_mem) else "ok"
    head = ("Buffers healthy: no swaps and every hit ratio at or above 97%."
            if tri == "ok" else f"{plural(len(low), 'buffer')} below 97% hit ratio, {swaps} swaps.")
    return dict(triage=tri, headline=head, findings=low + [f"{m.label} at {m.value}%" for m in high_mem],
                metrics=[Metric(label="Servers", value=str(len(servers))),
                         Metric(label="Swaps", value=str(swaps), tone="warn" if swaps else "good")],
                charts=[Chart(title="SAP memory in use", unit="%", items=sorted(mem, key=lambda m: -m.value)[:8])] if mem else [],
                recommendation="Resize the swapping buffers (RZ10) after checking ST02 history." if tri != "ok" else None)


def a_st06(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    isv, idesc, ival = col(t, "server"), col(t, "description"), col(t, "value")
    server, idle, cpus = "", {}, {}
    for r in t.rows:
        server = cell(r, isv) or server
        d, v = cell(r, idesc).lower(), num(cell(r, ival), european=True)
        if v is None:
            continue
        if d == "idle":
            idle.setdefault(server, v)
        if "number of cpus" in d:
            cpus[server] = int(v)
    items = [BarItem(label=s, value=round(100 - i, 1), tone=pct_tone(100 - i, 70, 90),
                     note=f"{cpus.get(s, '?')} CPUs") for s, i in idle.items()]
    worst = max(items, key=lambda x: x.value, default=None)
    tri = "ok" if not worst or worst.value < 80 else "review"
    return dict(triage=tri, headline=(f"Peak CPU utilisation {worst.value:.0f}% on {worst.label}."
                                      if worst else base["headline"]),
                charts=[Chart(title="CPU utilisation", unit="%", items=items)] if items else [])


def a_st03n(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    iname, isteps, iresp = col(t, "task type"), col(t, "number of dialog steps"), col(t, "average response time")
    items, dialog = [], None
    for r in t.rows:
        n, steps, resp = cell(r, iname), num(cell(r, isteps)), num(cell(r, iresp))
        if not n or resp is None or not steps:
            continue
        if n.upper() == "DIALOG":
            dialog = resp
        items.append(BarItem(label=n, value=round(resp), note=f"{int(steps):,} steps",
                             tone="warn" if n.upper() == "DIALOG" and resp > 1000 else "neutral"))
    items.sort(key=lambda x: -x.value)
    tri = "review" if dialog and dialog > 1000 else "ok"
    head = (f"Dialog response averages {dialog:.0f} ms." if dialog is not None
            else "No dialog steps recorded for the period.")
    return dict(triage=tri if base["sap_status"] != "not_ok" else "review", headline=head,
                charts=[Chart(title="Average response time per task type", unit="ms", items=items[:10])] if items else [],
                recommendation="Dialog response above 1 s: analyse the top transactions in ST03N." if tri == "review" else None)


def a_wp(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ity, ist = col(t, "type"), col(t, "wp status")
    rows = [r for r in t.rows if cell(r, ity)]
    status = Counter(cell(r, ist) or "Unknown" for r in rows)
    busy = Counter(cell(r, ity) for r in rows if cell(r, ist).lower() not in ("waiting", ""))
    held = status.get("Stopped", 0) + status.get("Ended", 0)
    tri = "review" if held else "ok"
    return dict(triage=tri if base["sap_status"] != "not_ok" else "review",
                headline=f"{plural(len(rows), 'work process', 'work processes')}: "
                         + ", ".join(f"{n} {s.lower()}" for s, n in status.most_common(4)) + ".",
                metrics=[Metric(label=s, value=str(n), tone="warn" if s in ("Stopped", "Ended") else "neutral")
                         for s, n in status.most_common(4)],
                charts=[Chart(title="Busy work processes by type", items=[BarItem(label=k, value=v) for k, v in busy.most_common()])] if busy else [])


def a_db12(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ie, ist, isz = col(t, "end time"), col(t, "state"), col(t, "size")
    ok = sorted([parse_dt(cell(r, ie)) for r in t.rows if "success" in cell(r, ist).lower() and parse_dt(cell(r, ie))])
    failed = [r for r in t.rows if cell(r, ist) and "success" not in cell(r, ist).lower()]
    ref = c.snapshot_dt or c.report_dt
    sizes = [num(cell(r, isz)) or 0 for r in t.rows]
    if not ok:
        return dict(triage="act", headline="No successful data backup in the catalog extract.",
                    recommendation="Check the backup schedule and backint logs immediately.")
    age = (ref - ok[-1]).total_seconds() / 3600 if ref else 0
    tri = "act" if age > 26 or failed else "ok"
    return dict(triage=tri, headline=f"Last successful data backup finished {ok[-1]:%d.%m.%Y %H:%M}"
                + (f", {age:.0f} h before the check." if ref else ".")
                + (f" {plural(len(failed), 'backup')} failed." if failed else ""),
                metrics=[Metric(label="Successful", value=str(len(ok)), tone="good"),
                         Metric(label="Failed", value=str(len(failed)), tone="bad" if failed else "good"),
                         Metric(label="Largest", value=f"{max(sizes) / 1e9:,.0f} GB")],
                recommendation="Investigate the failed backup and confirm the next one succeeds." if tri == "act" else None)


def a_al08(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ity = col(t, "type of user session")
    types = Counter(cell(r, ity) or "Unspecified" for r in t.rows)
    return dict(triage="ok" if base["sap_status"] != "not_ok" else "review",
                headline=f"{plural(len(t.rows), 'session')} across the system.",
                charts=[Chart(title="Sessions by type", items=[BarItem(label=k, value=v) for k, v in types.most_common(6)])])


def a_sm51(sh: Sheet, c: Ctx, base: dict) -> dict:
    t = sh.tables[0]
    ii, ist = col(t, "instance"), col(t, "state")
    rows = [r for r in t.rows if cell(r, ii)]
    down = [r for r in rows if cell(r, ist).lower() != "active"]
    if down:
        return dict(triage="act", headline=f"{plural(len(down), 'instance')} not active.",
                    findings=[f"{cell(r, ii)}: {cell(r, ist)}" for r in down],
                    recommendation="Check the instance's dev_disp and dev_w0 traces.")
    return dict(triage="ok", headline=f"All {plural(len(rows), 'instance')} active.")


def a_textcheck(sh: Sheet, c: Ctx, base: dict) -> dict:
    txt = sh.text.lower()
    if "no errors reported" in txt:
        return dict(triage="ok", headline="No errors reported.")
    return {}


ANALYSERS: dict[str, Callable[[Sheet, Ctx, dict], dict]] = {
    "SM21": a_sm21, "SM12": a_sm12, "SMLG": a_smlg, "CERT_INFO": a_cert,
    "SLICENSE": a_slicense, "DB02_ALERTS": a_db02_alerts, "DB02_LOG": a_db02_log,
    "DB02_MEMORY": a_db02_memory, "SM04": a_sm04, "ST02": a_st02, "ST06": a_st06,
    "ST03N": a_st03n, "SM50": a_wp, "SM66": a_wp, "DB12": a_db12, "AL08": a_al08,
    "SM51": a_sm51, "SICK": a_textcheck, "RZ03": a_textcheck, "RZ04": a_textcheck,
}


# ── Monitors sheet + sheet resolution ─────────────────────────────────────

def find_monitors(sheets: dict[str, Sheet]) -> tuple[Optional[Sheet], int]:
    for name in ["Monitors", *sheets.keys()]:
        sh = sheets.get(name)
        if not sh:
            continue
        for i, r in enumerate(sh.rows[:10]):
            low = [x.lower() for x in r]
            if "transaction" in low and "ok" in low:
                return sh, i
    return None, -1


def resolve_sheet(task: str, tcode: str, sheets: dict[str, Sheet], used: set) -> Optional[str]:
    names = {n.upper(): n for n in sheets}
    t, k = tcode.upper(), task.lower()
    candidates = []
    if "certificate" in t.lower() or "certificate" in k:
        candidates.append("CERT_INFO")
    if "RSRFCCHK" in t or "rfc connection" in k:
        candidates.append("RFC_CON")
    if t.startswith("DB02") or t.startswith("DB12"):
        if "alert" in k:
            candidates.append("DB02_ALERTS")
        if "availability" in k:
            candidates.append("DB02_LOG")
        if "memory" in k or "disk" in k:
            candidates.append("DB02_MEMORY")
    candidates.append(t)
    for c in candidates:
        if c in names and names[c] not in used:
            return names[c]
    return None


def _category(tcode: str) -> str:
    t = tcode.upper()
    for key, cat in kb.CATEGORY.items():
        if key in t:
            return cat
    return "Other"


def _system_from(sheets: dict[str, Sheet]) -> SystemInfo:
    sid = client = ""
    for sh in sheets.values():
        for r in sh.rows[:3]:
            for x in r:
                m = re.search(r"\b(?:in|of|System)\s+([A-Z][A-Z0-9]{2})\s+(\d{3})\b", x)
                if m:
                    sid, client = m.group(1), m.group(2)
                    break
            if sid:
                break
        if sid:
            break
    instances = []
    if "SM51" in sheets and sheets["SM51"].tables:
        t = sheets["SM51"].tables[0]
        instances = [r[0] for r in t.rows if r and r[0]]
    return SystemInfo(sid=sid, client=client, instances=instances)


def _tables_out(sh: Sheet) -> list[TableOut]:
    out = []
    for t in sh.tables:
        if not t.columns:
            continue
        out.append(TableOut(title=t.title, columns=t.columns,
                            rows=t.rows[:MAX_TABLE_ROWS], total=len(t.rows)))
    return out


def analyze(sheets: dict[str, Sheet], filename: str) -> HealthReport:
    mon, hi = find_monitors(sheets)
    system = _system_from(sheets)
    title = ""
    report_dt = None
    rows: list[list[str]] = []
    if mon:
        title = next((x for r in mon.rows[:hi] for x in r if x), "")
        report_dt = parse_dt(title)
        hdr = [x.lower() for x in mon.rows[hi]]
        ix = {k: (hdr.index(k) if k in hdr else None) for k in
              ("monitoring task", "transaction", "description", "selection period", "ok", "not ok", "evidence", "recommendation")}
        for r in mon.rows[hi + 1:]:
            if cell(r, ix["transaction"]) or cell(r, ix["monitoring task"]):
                rows.append([cell(r, ix[k]) for k in ix])
    ctx = Ctx(report_dt, system.sid)

    used: set = set()
    plan = []
    for task, tcode, desc, period, ok, notok, evid, sap_rec in rows:
        sheet = resolve_sheet(task, tcode, sheets, used)
        if sheet:
            used.add(sheet)
        sap_status = "ok" if ok.strip() else "not_ok" if notok.strip() else "unknown"
        plan.append((task, tcode, desc, period, sap_status, evid, sap_rec, sheet))
    # Sheets with no Monitors row still get analysed (e.g. no Monitors tab at all).
    if not mon:
        for n in sheets:
            plan.append((n, n, "", "", "unknown", "", "", n))
            used.add(n)

    # Snapshot time = latest timestamp in the "current snapshot" tabs; ages of
    # locks and backups are measured against it, not against the import time.
    stamps = []
    for name in ("SM12", "SM04", "AL08"):
        for t in (sheets[name].tables if name in sheets else []):
            i = col(t, "date/time", "dialog time", "date")
            stamps += [d for d in (parse_dt(cell(r, i)) for r in t.rows) if d]
    if stamps:
        snap = max(stamps)
        if not report_dt or abs((snap - report_dt).days) <= 2:
            ctx.snapshot_dt = snap
    checks: list[Check] = []
    seen_ids: Counter = Counter()
    for task, tcode, desc, period, sap_status, evid, sap_rec, sheet in plan:
        cid = slug(tcode or task, task)
        seen_ids[cid] += 1
        if seen_ids[cid] > 1:
            cid = f"{cid}-{seen_ids[cid]}"
        base = dict(
            id=cid, category=_category(tcode or (sheet or "")), task=task or tcode, tcode=tcode,
            description=desc, period=period, sap_status=sap_status, sap_evidence=evid,
            triage=("ok" if sap_status == "ok" else "review" if sap_status == "not_ok" else "no_data"),
            headline=evid or ("Passed." if sap_status == "ok" else "No evidence text."),
            recommendation=sap_rec or None, sheet=sheet)
        extra_tables = []
        if sheet:
            sh = sheets[sheet]
            base["tables"] = _tables_out(sh)
            if not sh.tables and sh.text.strip():
                base["text"] = sh.text[:MAX_TEXT_CHARS]
            fn = ANALYSERS.get(sheet.upper())
            # D-051: with no SAP verdict (neither OK nor Not OK ticked — e.g. a
            # direct pull that could not collect this tab) an empty tab is not
            # evidence of health, so no analyser may read it as "all clear".
            has_data = any(t.rows for t in sh.tables) or (not sh.tables and len(
                [ln for ln in sh.text.splitlines() if ln.strip()]) > 4)
            if sap_status == "unknown" and not has_data:
                fn = None
            if fn and (sh.tables or sh.text.strip()):
                try:
                    upd = fn(sh, ctx, base)
                    extra_tables = upd.pop("_extra_tables", [])
                    base.update({k: v for k, v in upd.items() if v is not None or k == "recommendation"})
                    if sap_status == "unknown" and base.get("triage") == "ok":
                        # Concerns found in the evidence still count; a clean
                        # reading without SAP's verdict is only "no evidence".
                        base["triage"] = "no_data"
                        base["analysis_note"] = ("SAP gave no OK / Not OK verdict for this check; the "
                                                 "evidence shown raises no concern but could not be "
                                                 "fully evaluated. " + (evid or ""))
                    if base.get("recommendation") is None and sap_rec:
                        base["recommendation"] = sap_rec
                except Exception as exc:  # noqa: BLE001 — never fail the import
                    base["analysis_note"] = (f"Sentinel could not analyse this tab "
                                             f"({type(exc).__name__}: {exc}); SAP's verdict is shown.")
        elif sap_status == "not_ok":
            base["analysis_note"] = "No evidence tab found for this check; SAP's verdict is shown."
        if extra_tables:
            base["tables"] = extra_tables + base.get("tables", [])
            if sheet and sheet.upper() == "CERT_INFO":
                base["text"] = sheets[sheet].text[:MAX_TEXT_CHARS]
                base["tables"] = extra_tables
        checks.append(Check(**base))

    tri = Counter(ch.triage for ch in checks)
    summary = Summary(total=len(checks),
                      sap_ok=sum(ch.sap_status == "ok" for ch in checks),
                      sap_not_ok=sum(ch.sap_status == "not_ok" for ch in checks),
                      ok=tri["ok"], expected=tri["expected"], review=tri["review"],
                      act=tri["act"], no_data=tri["no_data"])
    extra = [n for n in sheets if n not in used and (not mon or n != mon.name)]
    return HealthReport(
        id=uuid.uuid4().hex[:12], filename=filename,
        imported_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        report_date=report_dt.date().isoformat() if report_dt else None,
        title=title, system=system, summary=summary, checks=checks, extra_sheets=extra)
