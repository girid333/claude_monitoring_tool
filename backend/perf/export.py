"""Excel and EarlyWatch-style PDF exports of a Performance view (D-052).

Both are built from the exact payload the dashboard shows (POSTed back by
the browser), so the file always matches what the user looked at.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape as xml_escape
from typing import Optional

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

STATUS = {"good": "0CA30C", "warning": "FAB219", "serious": "EC835A", "critical": "D03B3B", "unknown": "9A9A9A"}
LABEL = {"good": "Good", "warning": "Warning", "serious": "Serious", "critical": "Critical", "unknown": "No data"}
SERIES = {"cpu": "3987E5", "db": "D95926", "wait": "199E70", "other": "C98500"}   # validated categorical order


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        v = f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"
    return f"{v} {unit}".strip() if unit else str(v)


def _us(v) -> str:
    if v is None:
        return "–"
    return f"{v / 1000:.2f} ms" if v >= 1000 else f"{round(v)} µs"


def _where(c: dict) -> str:
    c = c or {}
    if c.get("error") or c.get("provider") == "none":
        return ""
    return " · ".join(x for x in (c.get("provider"), c.get("zone"), c.get("vm_size"),
                                  f"PG {c['placement_group']}" if c.get("placement_group") else "") if x)


def _placement(r: dict) -> str:
    out = []
    if r.get("same_host_as_db"):
        out.append("same host as DB")
    if "same_zone_as_db" in r:
        out.append("same zone as DB" if r["same_zone_as_db"] else "other zone than DB")
    if r.get("same_pg_as_db"):
        out.append("same placement group")
    return ", ".join(out)


def landscape_rows(land: dict) -> dict:
    """Table rows shared by the workbook and the PDF."""
    app = land.get("app") or {}
    apps = [[("★ " if r.get("nearest") else "") + r["name"], r["host"],
             "this instance" if r.get("local") else ("not running now" if r.get("historic")
                                                     else ("active" if r["active"] else "inactive")),
             _where(r.get("cloud")), _placement(r),
             f"{r['busy_dia']} / {r['total_dia']}" if r.get("total_dia") else "–",
             "–" if r.get("cpu") is None else f"{r['cpu']:.0f} %",
             "–" if r.get("dia_resp_ms") is None else f"{r['dia_resp_ms']:,.1f}",
             _us(r.get("db_us_per_call")), _us(r.get("rtt_smon_us")), _us(r.get("rtt_hana_us")),
             r.get("latency_rank") or "–"] for r in app.get("servers", [])]
    hana = land.get("hana") or {}
    hosts = [[h["host"], h["role"], "yes" if h["active"] else "no", h["status"],
              _where((land.get("cloud") or {}).get(h["host"])) or " ".join(x for x in (h.get("platform"), h.get("model")) if x)]
             for h in hana.get("hosts") or []]
    sr = hana.get("hsr") or {}
    hsr = [[f"{x['host']}:{x['port']}", f"{x['secondary_host']} ({x['secondary_site']})", x["mode"], x["status"],
            x["secondary_active"], "–" if x["backlog_s"] is None else x["backlog_s"], x["backlog_mb"],
            "–" if x["shipping_delay_s"] is None else x["shipping_delay_s"], x["details"]] for x in sr.get("services", [])]
    return {"apps": apps, "hosts": hosts, "hsr": hsr}


def nearest_sentence(land: dict) -> str:
    app = land.get("app") or {}
    near = next((r for r in app.get("servers", []) if r.get("nearest")), None)
    if not near:
        return "No application server could be ranked by latency to the database."
    extra = _placement(near)
    if near.get("latency_us") is None:
        return (f"Only application server: {near['name']} ({near['host']}){' · ' + extra if extra else ''}. "
                "No latency source yet — " + " ".join(app.get("how_to_measure") or []))
    what = "network round trip" if app.get("measured") else "DB time per DB call"
    return (f"Closest application server to the database: {near['name']} ({near['host']}) — {what} "
            f"{_us(near.get('latency_us'))}{' · ' + extra if extra else ''}. Source: {app.get('source_label', '')}.")


def _title(d: dict) -> str:
    when = f"day {d.get('date')}" if d.get("mode") == "day" else f"live {str(d.get('as_of', ''))[:16]}"
    return f"Performance — {d.get('sid', '?')} {d.get('client', '')} — {when}"


# ── Excel ─────────────────────────────────────────────────────────────────

_H = Font(name="Arial", bold=True, color="FFFFFF")
_HF = PatternFill("solid", start_color="1F2937")
_B = Font(name="Arial", size=10)
_THIN = Side(style="thin", color="D0D0D0")
_BOX = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)


def _sheet(wb: Workbook, name: str, title: str, header: list[str], rows: list[list], widths: Optional[list[int]] = None):
    ws = wb.create_sheet(name[:31])
    ws["A1"] = title
    ws["A1"].data_type = "s"
    ws["A1"].font = Font(name="Arial", bold=True, size=13)
    ws.append([])
    ws.append(header)
    for c in ws[3]:
        c.font, c.fill, c.border = _H, _HF, _BOX
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for r in rows:
        ws.append(r)
    for row in ws.iter_rows(min_row=4, max_row=ws.max_row):
        for c in row:
            c.font, c.border = _B, _BOX
            if c.data_type == "f":         # SAP text such as "=…" must stay text, never become a formula
                c.data_type = "s"
    for i, w in enumerate(widths or [18] * len(header), 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A4"
    return ws


def to_xlsx(d: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    ov = wb.create_sheet("Overview")
    ov["A1"] = _title(d)
    ov["A1"].font = Font(name="Arial", bold=True, size=14)
    o = d.get("overall") or {}
    ov["A3"], ov["B3"] = "Overall rating", f"{o.get('grade', '–')}  (score {o.get('score', '–')} / 100)"
    ov["A4"], ov["B4"] = "System", f"{d.get('sid')} · release {d.get('release')} · DB {d.get('db')} · host {d.get('host')}"
    ov["A5"], ov["B5"] = "Data as of", str(d.get("as_of", ""))
    for r in range(3, 6):
        ov[f"A{r}"].font = Font(name="Arial", bold=True)
        ov[f"B{r}"].font = _B
    ov.append([])
    ov.append(["Area", "KPI", "Value", "Unit", "Rating", "Target", "Detail"])
    hdr = ov.max_row
    for c in ov[hdr]:
        c.font, c.fill, c.border = _H, _HF, _BOX
    for k in d.get("kpis", []):
        ov.append([k.get("group"), k.get("label"), k.get("value"), k.get("unit"), LABEL.get(k.get("rating"), "–"),
                   k.get("target"), k.get("detail")])
        cell = ov.cell(ov.max_row, 5)
        cell.fill = PatternFill("solid", start_color=STATUS.get(k.get("rating"), "9A9A9A"))
        cell.font = Font(name="Arial", bold=True, color="000000" if k.get("rating") in ("warning", "serious") else "FFFFFF")
        for c in ov[ov.max_row]:
            c.border = _BOX
            if c.column != 5:
                c.font = _B
    for i, w in enumerate([8, 30, 12, 7, 11, 22, 60], 1):
        ov.column_dimensions[get_column_letter(i)].width = w

    if d.get("findings"):
        _sheet(wb, "Findings", "Findings and recommendations", ["Rating", "Finding", "Detail", "Recommendation"],
               [[LABEL.get(f["rating"]), f["title"], f["detail"], f["recommendation"]] for f in d["findings"]],
               [11, 40, 45, 80])

    hours = d.get("hourly") or []
    if hours:
        ws = _sheet(wb, "Hourly profile", "Dialog response time per hour (ms per step) and steps",
                    ["Hour", "Steps (all)", "Dialog steps", "Avg response", "CPU", "DB", "Wait", "Other"],
                    [[h.get("label") or f"{h['hour']:02d}:00", h["steps"], h["dia_steps"], h["avg_resp"], h["cpu"],
                      h["db"], h["wait"], h["other"]] for h in hours], [14, 12, 12, 13, 10, 10, 10, 10])
        ch = BarChart()
        ch.type, ch.grouping, ch.overlap = "col", "stacked", 100
        ch.title, ch.y_axis.title = "Dialog response time composition", "ms per step"
        n = len(hours) + 3
        ch.add_data(Reference(ws, min_col=5, max_col=8, min_row=3, max_row=n), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=4, max_row=n))
        for s, key in zip(ch.series, ("cpu", "db", "wait", "other")):
            s.graphicalProperties.solidFill = SERIES[key]
            s.graphicalProperties.line.solidFill = SERIES[key]
        ch.height, ch.width = 9, 22
        ws.add_chart(ch, "J3")

    w = d.get("workload") or {}
    if w.get("tasks"):
        _sheet(wb, "Task types", f"Workload by task type (ST03N) — {w.get('date')}",
               ["Task type", "Steps", "Avg resp ms", "Avg CPU ms", "Avg DB ms", "Avg wait ms", "Avg roll wait ms",
                "Avg GUI ms", "Avg load ms", "Total resp s"],
               [[t["name"], t["steps"], round(t["avg_resp"], 1), round(t["avg_cpu"], 1), round(t["avg_db"], 1),
                 round(t["avg_wait"], 1), round(t["avg_rollwait"], 1), round(t["avg_gui"], 1), round(t["avg_load"], 1),
                 round(t["total_resp_s"], 1)] for t in w["tasks"]], [16, 12, 12, 12, 12, 12, 15, 12, 12, 13])
    if w.get("top"):
        _sheet(wb, "Top transactions", "Top transactions/programs by total response time",
               ["Transaction / program", "Task type", "Steps", "Share %", "Total s", "Avg resp ms", "Avg CPU ms",
                "Avg DB ms", "Users"],
               [[t["name"], t["task"], t["steps"], t["share"], t["total_s"], t["avg_resp"], t["avg_cpu"], t["avg_db"],
                 ("–" if t["users"] is None else t["users"])] for t in w["top"]], [40, 12, 10, 10, 10, 12, 12, 12, 8])

    s = d.get("sap") or {}
    if s.get("servers"):
        rows = []
        for srv in s["servers"]:
            for typ, b in sorted(srv["wp"].items()):
                rows.append([srv["name"], typ, b["total"], b["running"], b["waiting"], b["hold"], b["stopped"]])
        _sheet(wb, "Work processes", "Work processes per instance (SM50)",
               ["Instance", "Type", "Total", "Running", "Waiting", "On hold", "Stopped"], rows, [28, 8, 9, 9, 9, 9, 9])
    if (s.get("buffers") or {}).get("buffers"):
        _sheet(wb, "Buffers", "SAP buffers (ST02)", ["Buffer", "Hit ratio %", "Free space %", "Swaps", "Requests"],
               [[b["name"], b["hit"], b["free_pct"], b["swaps"], b["requests"]] for b in s["buffers"]["buffers"]],
               [16, 12, 13, 10, 14])

    land = d.get("landscape") or {}
    if land:
        lr = landscape_rows(land)
        _sheet(wb, "App servers", nearest_sentence(land)[:250],
                    ["Instance", "Host", "State", "Cloud placement", "vs database", "Dialog WPs busy", "CPU",
                     "Dialog resp ms", "DB time / call", "RTT SMON", "RTT HANA trace", "Latency rank"],
                    lr["apps"], [22, 14, 14, 36, 30, 12, 8, 12, 12, 11, 13, 10])
        if lr["hosts"]:
            _sheet(wb, "HANA hosts", "HANA hosts (M_LANDSCAPE_HOST_CONFIGURATION) — current state",
                   ["Host", "Role", "Active", "Status", "Platform / placement"], lr["hosts"], [14, 12, 8, 10, 50])
        if lr["hsr"]:
            sr = land["hana"]["hsr"]
            _sheet(wb, "System replication", f"HANA system replication {sr['primary_site']} → "
                   + ", ".join(f"{x['site']} ({x['mode']}, {x['status']})" for x in sr["sites"]),
                   ["Service", "Secondary", "Mode", "Status", "Secondary active", "Backlog s", "Backlog MB",
                    "Shipping delay s", "Details"], lr["hsr"], [16, 22, 8, 10, 10, 10, 10, 14, 50])
    hana = d.get("hana") or {}
    hh = d.get("hana_hours") or hana.get("today") or []
    if hh:
        ws = _sheet(wb, "HANA hourly", "HANA CPU and memory per hour (M_LOAD_HISTORY_HOST)",
                    ["Hour", "CPU avg %", "CPU max %", "Memory avg GB", "Memory max GB", "Allocation limit GB"],
                    [[f"{h['hour']:02d}:00", h["cpu_avg"], h["cpu_max"], h["mem_avg_gb"], h["mem_max_gb"], h["limit_gb"]]
                     for h in hh], [10, 12, 12, 15, 15, 18])
        lc = LineChart()
        lc.title, lc.y_axis.title = "HANA CPU %", "%"
        n = len(hh) + 3
        lc.add_data(Reference(ws, min_col=2, max_col=3, min_row=3, max_row=n), titles_from_data=True)
        lc.set_categories(Reference(ws, min_col=1, min_row=4, max_row=n))
        lc.height, lc.width = 8, 18
        ws.add_chart(lc, "H3")
    if hana.get("top_sql"):
        _sheet(wb, "HANA top SQL", "Top SQL statements by total execution time (M_SQL_PLAN_CACHE)",
               ["Statement hash", "Executions", "Total s", "Avg ms", "Share %", "Statement (first 90 chars)"],
               [[q["hash"], q["executions"], q["total_s"], q["avg_ms"], q["share"], q["statement"]] for q in hana["top_sql"]],
               [16, 12, 10, 10, 9, 90])
    sm = (d.get("smon") or {}).get("series") or []
    if sm:
        _sheet(wb, "SMON", "Snapshot monitoring (/SDF/SMON) — all instances",
               ["Time", "Active WPs", "Active dialog WPs", "CPU %", "Free memory %", "Dialog queue", "Update queue",
                "Enqueue queue", "Users", "Sessions", "Locks"],
               [[p["time"], p["act_wps"], p["act_dia"], p["cpu"], p["free_mem"], p["diaq"], p["updq"], p["enqq"],
                 p["users"], p["sessions"], p["locks"]] for p in sm], [8] + [12] * 10)
    if d.get("notes"):
        _sheet(wb, "Notes", "Sources that returned no data, and why", ["Note"], [[n] for n in d["notes"]], [140])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ── PDF (EarlyWatch-style) ────────────────────────────────────────────────

def esc(v) -> str:
    """SAP names like <AD_MSBUF> or SQL with '<' are text, not reportlab markup."""
    return xml_escape(str(v or ""))


def to_pdf(d: dict) -> bytes:
    from reportlab.graphics.charts.barcharts import VerticalBarChart
    from reportlab.graphics.charts.linecharts import HorizontalLineChart
    from reportlab.graphics.widgets.markers import makeMarker
    from reportlab.graphics.shapes import Circle, Drawing, Rect, String
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    hexc = lambda h: colors.HexColor("#" + h)  # noqa: E731
    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="Helvetica-Bold", fontSize=15, spaceAfter=6,
                        textColor=hexc("111827"))
    H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=12, spaceBefore=10,
                        spaceAfter=4, textColor=hexc("111827"))
    P = ParagraphStyle("p", parent=ss["BodyText"], fontName="Helvetica", fontSize=8.5, leading=11)
    S = ParagraphStyle("s", parent=P, fontSize=7.5, leading=9.5, textColor=hexc("4B5563"))
    o = d.get("overall") or {}
    rating = o.get("rating") if o.get("rating") in STATUS else "unknown"
    story = []

    def table(data, widths, header=True, status_col=None):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
        st = [("FONT", (0, 0), (-1, -1), "Helvetica", 7.5), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("GRID", (0, 0), (-1, -1), 0.25, hexc("D1D5DB")), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
              ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
        if header:
            st += [("BACKGROUND", (0, 0), (-1, 0), hexc("1F2937")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                   ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7.5)]
        if status_col is not None:
            for i, row in enumerate(data[1:], 1):
                key = next((k for k, v in LABEL.items() if v == row[status_col]), "unknown")
                st += [("BACKGROUND", (status_col, i), (status_col, i), hexc(STATUS[key])),
                       ("FONT", (status_col, i), (status_col, i), "Helvetica-Bold", 7.5)]
        t.setStyle(TableStyle(st))
        return t

    # cover band: rating badge + system facts
    band = Drawing(180 * mm, 34 * mm)
    band.add(Rect(0, 0, 180 * mm, 34 * mm, fillColor=hexc("111827"), strokeColor=None))
    band.add(Circle(20 * mm, 17 * mm, 12 * mm, fillColor=hexc(STATUS[rating]), strokeColor=None))
    band.add(String(20 * mm, 13.5 * mm, str(o.get("grade", "–")), fontName="Helvetica-Bold", fontSize=26,
                    fillColor=colors.white, textAnchor="middle"))
    band.add(String(38 * mm, 22 * mm, f"Performance Report  {d.get('sid', '')} {d.get('client', '')}",
                    fontName="Helvetica-Bold", fontSize=16, fillColor=colors.white))
    when = f"Day analysis {d.get('date')}" if d.get("mode") == "day" else f"Live status {str(d.get('as_of', ''))[:16]}"
    band.add(String(38 * mm, 14 * mm, f"{when}  ·  score {o.get('score', '–')}/100  ·  overall {LABEL.get(rating)}",
                    fontName="Helvetica", fontSize=9.5, fillColor=hexc("D1D5DB")))
    band.add(String(38 * mm, 7.5 * mm, f"Release {d.get('release', '')} · database {d.get('db', '')} · host "
                    f"{d.get('host', '')}", fontName="Helvetica", fontSize=8, fillColor=hexc("9CA3AF")))
    story += [band, Spacer(1, 6)]
    cnt = o.get("counts") or {}
    story.append(Paragraph(
        f"<b>Summary.</b> {len(d.get('kpis', []))} key figures rated: {cnt.get('good', 0)} good, "
        f"{cnt.get('warning', 0)} warning, {cnt.get('serious', 0)} serious, {cnt.get('critical', 0)} critical. "
        "Ratings follow SAP EarlyWatch guidance (dialog response ≤ 1 s, DB and CPU time each ≤ 40 % of response, "
        "wait ≤ 10 %). Each rating is shown with its label, not colour alone.", P))

    story.append(Paragraph("Key performance indicators", H2))
    data = [["Area", "KPI", "Value", "Rating", "Target", "Detail"]]
    for k in d.get("kpis", []):
        data.append([k.get("group"), Paragraph(esc(k.get("label", "")), P), _fmt(k.get("value"), k.get("unit", "")),
                     LABEL.get(k.get("rating"), "–"), k.get("target", ""), Paragraph(esc(k.get("detail", "")), S)])
    story.append(table(data, [12 * mm, 40 * mm, 22 * mm, 17 * mm, 25 * mm, 64 * mm], status_col=3))

    hours = d.get("hourly") or []
    if any(h.get("dia_steps") for h in hours):
        dr = Drawing(180 * mm, 62 * mm)
        bc = VerticalBarChart()
        bc.x, bc.y, bc.width, bc.height = 14 * mm, 10 * mm, 158 * mm, 44 * mm
        bc.categoryAxis.style = "stacked"
        bc.data = [[h[k] for h in hours] for k in ("cpu", "db", "wait", "other")]
        bc.categoryAxis.categoryNames = [(h.get("label") or f"{h['hour']:02d}")[-5:-3] if h.get("label")
                                         else f"{h['hour']:02d}" for h in hours]
        bc.categoryAxis.labels.fontSize = 6
        bc.valueAxis.labels.fontSize = 6
        bc.valueAxis.valueMin = 0
        bc.barSpacing, bc.groupSpacing = 0, 2
        for i, key in enumerate(("cpu", "db", "wait", "other")):
            bc.bars[i].fillColor = hexc(SERIES[key])
            bc.bars[i].strokeColor = colors.white
            bc.bars[i].strokeWidth = 0.4
        dr.add(bc)
        for i, (key, lab) in enumerate((("cpu", "CPU"), ("db", "DB"), ("wait", "Wait"), ("other", "Other"))):
            dr.add(Rect(14 * mm + i * 22 * mm, 57 * mm, 3 * mm, 3 * mm, fillColor=hexc(SERIES[key]), strokeColor=None))
            dr.add(String(18 * mm + i * 22 * mm, 57.4 * mm, lab, fontName="Helvetica", fontSize=7))
        dr.add(String(120 * mm, 57.4 * mm, "ms per dialog step, by hour", fontName="Helvetica", fontSize=7,
                      fillColor=hexc("4B5563")))
        story += [Paragraph("Dialog response time by hour", H2), dr]

    w = d.get("workload") or {}
    if w.get("tasks"):
        data = [["Task type", "Steps", "Avg resp", "CPU", "DB", "Wait", "Roll wait", "GUI"]]
        for t in w["tasks"][:12]:
            data.append([t["name"], f"{t['steps']:,}"] + [f"{t[k]:,.1f}" for k in
                                                         ("avg_resp", "avg_cpu", "avg_db", "avg_wait", "avg_rollwait", "avg_gui")])
        story += [Paragraph(f"Workload by task type (ms per step) — {w.get('date')}", H2),
                  table(data, [32 * mm, 22 * mm] + [21 * mm] * 6)]
    if w.get("top"):
        data = [["Transaction / program", "Task", "Steps", "Share", "Avg resp", "Avg CPU", "Avg DB"]]
        for t in w["top"][:12]:
            data.append([Paragraph(esc(t["name"][:60]), S), Paragraph(esc(t["task"]), S), f"{t['steps']:,}", f"{t['share']} %",
                         f"{t['avg_resp']:,.1f}", f"{t['avg_cpu']:,.1f}", f"{t['avg_db']:,.1f}"])
        story += [Paragraph("Top transactions by total response time", H2),
                  table(data, [56 * mm, 27 * mm, 16 * mm, 16 * mm, 21 * mm, 21 * mm, 22 * mm])]

    hana = d.get("hana") or {}
    hh = d.get("hana_hours") or hana.get("today") or []
    if hh or hana:
        story.append(Paragraph("SAP HANA", H2))
    if len(hh) > 1:
        dr = Drawing(180 * mm, 50 * mm)
        lc = HorizontalLineChart()
        lc.x, lc.y, lc.width, lc.height = 14 * mm, 8 * mm, 158 * mm, 36 * mm
        # one slot per hour; hours without HANA history (system down) are gaps, not interpolated
        by = {h["hour"]: h for h in hh}
        slots = [by.get(i) for i in range(24 if d.get("mode") == "day" else max(by) + 1)]
        cpu = [h["cpu_max"] if h else None for h in slots]
        mem = [(round(h["mem_max_gb"] / h["limit_gb"] * 100, 1) if h["limit_gb"] else 0) if h else None for h in slots]
        # reportlab joins across None, so each unbroken run of hours becomes its own line
        rows, keys = [], []
        for key, vals in (("cpu", cpu), ("db", mem)):
            run: list = [None] * len(vals)
            for i, v in enumerate(vals + [None]):
                if v is not None:
                    run[i] = v
                elif any(x is not None for x in run):
                    rows.append(run)
                    keys.append(key)
                    run = [None] * len(vals)
        lc.data = rows
        lc.categoryAxis.categoryNames = [f"{i:02d}" for i in range(len(slots))]
        lc.categoryAxis.labels.fontSize = 6
        lc.valueAxis.labels.fontSize = 6
        lc.valueAxis.valueMin, lc.valueAxis.valueMax = 0, 100
        for i, key in enumerate(keys):                      # markers keep isolated hours visible
            lc.lines[i].strokeColor = hexc(SERIES[key])
            lc.lines[i].strokeWidth = 1.6
            lc.lines[i].symbol = makeMarker("FilledCircle", size=3.2, fillColor=hexc(SERIES[key]))
        dr.add(lc)
        for i, (key, lab) in enumerate((("cpu", "CPU max %"), ("db", "Memory used % of limit"))):
            dr.add(Rect(14 * mm + i * 40 * mm, 46 * mm, 3 * mm, 3 * mm, fillColor=hexc(SERIES[key]), strokeColor=None))
            dr.add(String(18 * mm + i * 40 * mm, 46.4 * mm, lab, fontName="Helvetica", fontSize=7))
        story.append(dr)
    if hana.get("top_sql"):
        data = [["Hash", "Executions", "Total s", "Avg ms", "Share", "Statement"]]
        for q in hana["top_sql"][:10]:
            data.append([q["hash"], f"{q['executions']:,}", f"{q['total_s']:,.1f}", f"{q['avg_ms']:,.2f}",
                         f"{q['share']} %", Paragraph(esc(q["statement"]), S)])
        story.append(KeepTogether([Paragraph("Top SQL statements by total execution time", H2),
                                   table(data, [22 * mm, 18 * mm, 16 * mm, 16 * mm, 14 * mm, 94 * mm])]))

    land = d.get("landscape") or {}
    if land:
        lr = landscape_rows(land)
        story += [Paragraph("System landscape", H2), Paragraph(esc(nearest_sentence(land)), P)]
        if lr["apps"]:
            data = [["Instance", "Placement", "Dialog resp ms", "DB time / call", "RTT SMON", "RTT HANA", "Rank"]]
            for r in lr["apps"]:
                data.append([Paragraph(f"<b>{esc(r[0])}</b><br/>{esc(r[1])} · {esc(r[2])}", S),
                             Paragraph(esc(" · ".join(x for x in (r[3], r[4]) if x)) or "–", S),
                             r[7], r[8], r[9], r[10], str(r[11])])
            story.append(table(data, [40 * mm, 46 * mm, 20 * mm, 20 * mm, 18 * mm, 18 * mm, 12 * mm]))
        if lr["hosts"]:
            data = [["HANA host", "Role", "Active", "Status", "Platform / placement"]] + \
                [[r[0], r[1], r[2], r[3], Paragraph(esc(r[4]), S)] for r in lr["hosts"]]
            story += [Spacer(1, 6), table(data, [30 * mm, 22 * mm, 16 * mm, 18 * mm, 88 * mm])]
        if lr["hsr"]:
            sr = land["hana"]["hsr"]
            story.append(Paragraph(esc(f"System replication {sr['primary_site']} → " + ", ".join(
                f"{x['site']} ({x['mode']}, {x['status'].title()})" for x in sr["sites"])), P))
            data = [["Service", "Secondary", "Status", "Backlog s", "Shipping delay s", "Details"]] + \
                [[r[0], Paragraph(esc(r[1]), S), r[3], str(r[5]), str(r[7]), Paragraph(esc(r[8]), S)] for r in lr["hsr"]]
            story.append(table(data, [28 * mm, 34 * mm, 18 * mm, 18 * mm, 24 * mm, 56 * mm]))
        for t in (land.get("hana") or {}).get("takeovers") or []:
            story.append(Paragraph(esc(f"Takeover {t['start']} {t['from_site']} → {t['to_site']} ({t['type']})"), P))

    story.append(Paragraph("Findings and recommendations", H2))
    if d.get("findings"):
        data = [["Rating", "Finding", "Recommendation"]]
        for f in d["findings"]:
            data.append([LABEL.get(f["rating"]), Paragraph(f"<b>{esc(f['title'])}</b><br/>{esc(f['detail'])}", S),
                         Paragraph(esc(f["recommendation"]), S)])
        story.append(table(data, [18 * mm, 62 * mm, 100 * mm], status_col=0))
    else:
        story.append(Paragraph("No key figure is outside its target range.", P))
    if d.get("notes"):
        story += [Paragraph("Notes on data sources", H2)] + [Paragraph("• " + esc(n), S) for n in d["notes"][:20]]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(hexc("6B7280"))
        canvas.drawString(15 * mm, 8 * mm, f"Sentinel · {_title(d)}")
        canvas.drawRightString(195 * mm, 8 * mm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title=_title(d), author="Sentinel").build(story, onFirstPage=footer,
                                                                                     onLaterPages=footer)
    return buf.getvalue()
