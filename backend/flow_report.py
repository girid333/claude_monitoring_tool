"""Functional-flow report template (D-035) — separate from Core BASIS.

WHY THIS IS ITS OWN TEMPLATE
`report_composer.ReportGenerator` renders v1's fixed four-section structure
(Executive Summary → Infrastructure & Server Health → Detailed Status →
Functional Sanity) with a page break before each. That's right for a Core
BASIS sweep, but a functional-flow run has no v1 data at all, so sections
1-3 rendered as *empty pages* and the file was titled "SAP Monitoring Report
v2.0" — indistinguishable from a BASIS report.

So recorded flows get their own document: named after the recording, with
only the sections a flow actually has, and no empty pages by construction —
every heading here is emitted only when there is content under it.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate,
                                 Spacer, Table, TableStyle)

SEV_COLORS = {
    "critical": "C00000", "high": "FF453A", "medium": "FF9F0A",
    "low": "0A84FF", "info": "8E8E93",
}


def _rgb(hex_color: str) -> RGBColor:
    return RGBColor(int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


def _pdf_image_ok(path: str) -> bool:
    """Force-load an image now so a corrupt file fails HERE instead of during
    `doc.build()`, where it would abort the entire PDF."""
    try:
        from reportlab.lib.utils import ImageReader
        ImageReader(path).getRGBData()
        return True
    except Exception:  # noqa: BLE001
        return False


def _safe(name: str) -> str:
    """Filesystem-safe version of a user-supplied flow name."""
    keep = [c if (c.isalnum() or c in "-_") else "_" for c in name.strip()]
    out = "".join(keep).strip("_")
    return out or "flow"


class FlowReportGenerator:
    """One document per recorded flow (D-035).

    `rows` is the output of `sections.flow_results_to_report_rows()`:
      {flow_id, sid, mode, ok, worst_severity, anomalies[], screenshots[],
       low_confidence_steps[], error}
    """

    def __init__(self, flow_id: str, rows: list[dict], output_dir: str = "reports",
                 description: str = ""):
        self.flow_id = flow_id
        self.rows = rows
        self.description = description
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # D-035: the file is named after the recording, not a generic title.
        self.basename = f"SAP_Flow_{_safe(flow_id)}_{self.stamp}"

    # ── shared helpers ──────────────────────────────────────────────────

    def _verdict(self) -> tuple[str, str]:
        if any(r.get("error") for r in self.rows):
            return "FAILED", "critical"
        worst = None
        order = ["critical", "high", "medium", "low", "info"]
        for s in order:
            if any(r.get("worst_severity") == s for r in self.rows):
                worst = s
                break
        if worst:
            return "ACTION REQUIRED", worst
        return "PASSED", "info"

    # ── Word ────────────────────────────────────────────────────────────

    def generate_word(self) -> str:
        doc = Document()
        for section in doc.sections:
            section.orientation = WD_ORIENT.LANDSCAPE
            section.page_width, section.page_height = section.page_height, section.page_width

        title = doc.add_heading(f"Functional Sanity Report — {self.flow_id}", 0)
        for run in title.runs:
            run.font.color.rgb = _rgb("E28C00")

        meta = doc.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.LEFT
        sids = sorted({r.get("sid", "") for r in self.rows if r.get("sid")})
        meta.add_run(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n").font.size = Pt(9)
        if sids:
            meta.add_run(f"System(s): {', '.join(sids)}\n").font.size = Pt(9)
        if self.description:
            meta.add_run(f"{self.description}\n").font.size = Pt(9)

        verdict, sev = self._verdict()
        vp = doc.add_paragraph()
        vr = vp.add_run(f"Result: {verdict}")
        vr.bold = True
        vr.font.size = Pt(13)
        vr.font.color.rgb = _rgb(SEV_COLORS.get(sev, "8E8E93"))

        # Summary table — always has content (one row per execution)
        doc.add_heading("Summary", level=1)
        table = doc.add_table(rows=1, cols=5)
        table.style = "Light Grid Accent 1"
        for i, h in enumerate(["Flow", "System", "Mode", "Findings", "Status"]):
            cell = table.rows[0].cells[i]
            cell.text = h
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
        for r in self.rows:
            cells = table.add_row().cells
            cells[0].text = str(r.get("flow_id", ""))
            cells[1].text = str(r.get("sid", ""))
            cells[2].text = str(r.get("mode", ""))
            cells[3].text = str(len(r.get("anomalies", [])))
            cells[4].text = "ERROR" if r.get("error") else ("OK" if r.get("ok") else "FINDINGS")

        # Per-execution detail. Every heading below is emitted only when the
        # corresponding content exists — that's what keeps the file free of
        # the empty pages the shared BASIS template produced.
        for r in self.rows:
            doc.add_page_break()
            doc.add_heading(f"{r.get('flow_id')} — {r.get('sid')} ({r.get('mode')} mode)", level=1)

            if r.get("error"):
                p = doc.add_paragraph()
                run = p.add_run(f"Execution error: {r['error']}")
                run.font.color.rgb = _rgb(SEV_COLORS["critical"])
                run.bold = True

            anomalies = r.get("anomalies") or []
            if anomalies:
                doc.add_heading("Findings", level=2)
                for a in anomalies:
                    p = doc.add_paragraph(style="List Bullet")
                    sv = str(a.get("severity", "info"))
                    sr = p.add_run(f"[{sv.upper()}] ")
                    sr.bold = True
                    sr.font.color.rgb = _rgb(SEV_COLORS.get(sv, "8E8E93"))
                    p.add_run(str(a.get("message", "")))
                    if a.get("actual") is not None:
                        p.add_run(f"  (actual: {a['actual']}, expected: {a.get('expected')})").font.size = Pt(8)
            elif not r.get("error"):
                doc.add_paragraph("No anomalies detected.")

            low = r.get("low_confidence_steps") or []
            if low:
                p = doc.add_paragraph()
                run = p.add_run(
                    f"⚠ {len(low)} step(s) matched with low confidence (indices {low}) — "
                    "the element was identified by a weaker fallback, so verify these "
                    "before trusting the result.")
                run.font.color.rgb = _rgb(SEV_COLORS["medium"])

            shots = [s for s in (r.get("screenshots") or []) if s and os.path.exists(s)]
            if shots:
                doc.add_heading("Captured evidence", level=2)
                for path in shots:
                    try:
                        doc.add_picture(path, width=Inches(13.5))
                        doc.add_paragraph(Path(path).name).runs[0].font.size = Pt(7)
                    except Exception:  # noqa: BLE001,PERF203 - unreadable image
                        doc.add_paragraph(f"[could not embed {path}]")

        path = os.path.join(self.output_dir, f"{self.basename}.docx")
        doc.save(path)
        return path

    # ── PDF ─────────────────────────────────────────────────────────────

    def generate_pdf(self) -> str:
        path = os.path.join(self.output_dir, f"{self.basename}.pdf")
        docp = SimpleDocTemplate(path, pagesize=landscape(A3),
                                  leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                                  topMargin=0.5 * inch, bottomMargin=0.5 * inch)
        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("fh1", parent=styles["Heading1"], textColor=colors.HexColor("#E28C00"))
        h2 = ParagraphStyle("fh2", parent=styles["Heading2"], textColor=colors.HexColor("#333333"))
        small = ParagraphStyle("fsmall", parent=styles["Normal"], fontSize=8)
        el: list = []

        el.append(Paragraph(f"Functional Sanity Report — {self.flow_id}", h1))
        sids = sorted({r.get("sid", "") for r in self.rows if r.get("sid")})
        el.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                             + (f" &nbsp;|&nbsp; System(s): {', '.join(sids)}" if sids else ""), small))
        if self.description:
            el.append(Paragraph(self.description, small))
        verdict, sev = self._verdict()
        el.append(Spacer(1, 8))
        el.append(Paragraph(f"<b>Result: {verdict}</b>",
                             ParagraphStyle("v", parent=styles["Normal"], fontSize=12,
                                             textColor=colors.HexColor("#" + SEV_COLORS.get(sev, "8E8E93")))))
        el.append(Spacer(1, 12))

        data = [["Flow", "System", "Mode", "Findings", "Status"]]
        for r in self.rows:
            data.append([str(r.get("flow_id", "")), str(r.get("sid", "")), str(r.get("mode", "")),
                          str(len(r.get("anomalies", []))),
                          "ERROR" if r.get("error") else ("OK" if r.get("ok") else "FINDINGS")])
        t = Table(data, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E28C00")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
        ]))
        el.append(t)

        for r in self.rows:
            el.append(PageBreak())
            el.append(Paragraph(f"{r.get('flow_id')} — {r.get('sid')} ({r.get('mode')} mode)", h1))
            if r.get("error"):
                el.append(Paragraph(f"<b>Execution error:</b> {r['error']}",
                                     ParagraphStyle("err", parent=styles["Normal"],
                                                     textColor=colors.HexColor("#C00000"))))
            anomalies = r.get("anomalies") or []
            if anomalies:
                el.append(Paragraph("Findings", h2))
                for a in anomalies:
                    sv = str(a.get("severity", "info"))
                    el.append(Paragraph(
                        f'<font color="#{SEV_COLORS.get(sv, "8E8E93")}"><b>[{sv.upper()}]</b></font> '
                        f'{a.get("message", "")}', styles["Normal"]))
            elif not r.get("error"):
                el.append(Paragraph("No anomalies detected.", styles["Normal"]))

            low = r.get("low_confidence_steps") or []
            if low:
                el.append(Paragraph(
                    f'<font color="#FF9F0A">⚠ {len(low)} step(s) matched with low '
                    f'confidence (indices {low}) — verify before trusting.</font>',
                    styles["Normal"]))

            shots = [s for s in (r.get("screenshots") or []) if s and os.path.exists(s)]
            if shots:
                el.append(Paragraph("Captured evidence", h2))
                for p_ in shots:
                    if not _pdf_image_ok(p_):
                        # reportlab loads images lazily at build() time, so a
                        # corrupt screenshot would otherwise blow up the whole
                        # report long after this loop's try/except is gone.
                        el.append(Paragraph(f"[unreadable image: {Path(p_).name}]", small))
                        continue
                    el.append(Image(p_, width=15 * inch, height=8 * inch, kind="proportional"))
                    el.append(Paragraph(Path(p_).name, small))
                    el.append(Spacer(1, 6))

        docp.build(el)
        return path
