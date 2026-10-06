"""D-049: the evidence document for a data-driven run.

Audience matters here and shapes every choice: this goes to AUDITORS and
TESTERS, not to an engineer debugging a pipeline. So it opens with what
was run, against which system, by whom and when; then a summary table
that can be scanned in seconds; then per-iteration evidence showing the
input data alongside the screenshots captured while that specific row was
executing.

The per-iteration structure is the whole point. A single pile of
screenshots proves nothing about WHICH input produced WHICH result — an
auditor needs row 7's data next to row 7's screens.
"""

from __future__ import annotations

import os
from datetime import datetime

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from flow_report import _pdf_image_ok, _rgb, _safe


def _esc(v) -> str:
    from xml.sax.saxutils import escape
    return escape(str(v))


class DataRunReportGenerator:
    """One document per data-driven run.

    `result` is DataRunResult.as_dict(); `bindings` describes which
    spreadsheet column fed which recorded field, which the document states
    explicitly so a reviewer can see exactly what was varied."""

    def __init__(self, result: dict, bindings: list[dict], sid: str,
                 output_dir: str = "reports", description: str = "",
                 executed_by: str = ""):
        self.result = result
        self.bindings = bindings or []
        self.sid = sid
        self.description = description
        self.executed_by = executed_by
        self.flow_id = result.get("flow_id", "flow")
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.basename = f"SAP_DataRun_{_safe(self.flow_id)}_{self.stamp}"

    # ── Word ────────────────────────────────────────────────────────────

    def generate_word(self) -> str:
        doc = Document()
        for section in doc.sections:
            section.orientation = WD_ORIENT.LANDSCAPE
            section.page_width, section.page_height = section.page_height, section.page_width

        total = self.result.get("total", 0)
        passed = self.result.get("passed", 0)
        failed = self.result.get("failed", 0)

        title = doc.add_heading(f"Test Execution Evidence — {self.flow_id}", 0)
        for run in title.runs:
            run.font.color.rgb = _rgb("E28C00")

        meta = doc.add_paragraph()
        meta.alignment = WD_ALIGN_PARAGRAPH.LEFT
        lines = [
            f"System: {self.sid}",
            f"Executed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Executed by: {self.executed_by or 'SAP Sentinel'}",
            f"Iterations: {total}   Passed: {passed}   Failed: {failed}",
        ]
        if self.description:
            lines.insert(0, self.description)
        for line in lines:
            meta.add_run(line + "\n").font.size = Pt(9)

        verdict = "PASSED" if failed == 0 and total > 0 else (
            "NO DATA" if total == 0 else "FAILURES PRESENT")
        colour = "1E8E3E" if verdict == "PASSED" else "C5221F"
        v = doc.add_paragraph()
        run = v.add_run(f"Result: {verdict}")
        run.bold = True
        run.font.size = Pt(13)
        run.font.color.rgb = _rgb(colour)

        # ── what was varied ──────────────────────────────────────────
        if self.bindings:
            doc.add_heading("Data-driven fields", level=1)
            doc.add_paragraph(
                "Each iteration below re-ran the same recorded transaction with the "
                "values listed for that row. Only the fields in this table were varied; "
                "every other recorded step was replayed exactly as captured."
            ).runs[0].font.size = Pt(9)
            t = doc.add_table(rows=1, cols=3)
            t.style = "Light Grid Accent 1"
            for i, h in enumerate(("Spreadsheet column", "Screen field", "Recorded sample")):
                t.rows[0].cells[i].text = h
            for b in self.bindings:
                c = t.add_row().cells
                c[0].text = str(b.get("column", ""))
                c[1].text = str(b.get("label", ""))
                c[2].text = "" if b.get("recorded_value") is None else str(b["recorded_value"])

        # ── summary table ────────────────────────────────────────────
        doc.add_heading("Execution summary", level=1)
        iterations = self.result.get("iterations", [])
        key_cols = [b.get("column") for b in self.bindings][:3]
        headers = ["#"] + key_cols + ["Status", "Result", "Error"]
        table = doc.add_table(rows=1, cols=len(headers))
        table.style = "Light Grid Accent 1"
        for i, h in enumerate(headers):
            cell = table.rows[0].cells[i]
            cell.text = str(h)
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
        for it in iterations:
            cells = table.add_row().cells
            cells[0].text = str(it.get("index", ""))
            for j, col in enumerate(key_cols, start=1):
                cells[j].text = str(it.get("inputs", {}).get(col, ""))
            status_cell = cells[len(key_cols) + 1]
            status_cell.text = "PASS" if it.get("ok") else "FAIL"
            for p in status_cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.color.rgb = _rgb("1E8E3E" if it.get("ok") else "C5221F")
            cells[len(key_cols) + 2].text = str(it.get("outcome", ""))[:120]
            cells[len(key_cols) + 3].text = str(it.get("error", ""))[:200]

        # ── per-iteration evidence ───────────────────────────────────
        doc.add_page_break()
        doc.add_heading("Evidence by iteration", level=1)
        for it in iterations:
            idx = it.get("index")
            ok = it.get("ok")
            h = doc.add_heading(
                f"Iteration {idx} — {'PASSED' if ok else 'FAILED'}", level=2)
            for r in h.runs:
                r.font.color.rgb = _rgb("1E8E3E" if ok else "C5221F")

            inputs = it.get("inputs", {})
            if inputs:
                it_table = doc.add_table(rows=1, cols=2)
                it_table.style = "Light List Accent 1"
                it_table.rows[0].cells[0].text = "Field"
                it_table.rows[0].cells[1].text = "Value for this iteration"
                for k, v in inputs.items():
                    c = it_table.add_row().cells
                    c[0].text = str(k)
                    c[1].text = "" if v is None else str(v)

            if it.get("outcome"):
                p = doc.add_paragraph()
                r = p.add_run(f"Outcome: {it['outcome']}")
                r.bold = True
                r.font.size = Pt(10)
            if it.get("exports"):
                p = doc.add_paragraph()
                r = p.add_run("Values read: " + ", ".join(f"{k} = {v}" for k, v in it["exports"].items()))
                r.font.size = Pt(9.5)
            if it.get("error"):
                p = doc.add_paragraph()
                r = p.add_run(f"Error: {it['error']}")
                r.font.color.rgb = _rgb("C5221F")
                r.font.size = Pt(9.5)
            for note in it.get("notes", []):
                doc.add_paragraph(note).runs[0].font.size = Pt(8.5)

            shots = [s for s in it.get("screenshots", []) if s and os.path.exists(s)]
            if shots:
                for shot in shots:
                    try:
                        doc.add_picture(shot, width=Inches(9.0))
                    except Exception:  # noqa: BLE001,PERF203 - one bad image
                        doc.add_paragraph(f"[screenshot could not be embedded: {shot}]")
            else:
                doc.add_paragraph("No screenshots captured for this iteration."
                                   ).runs[0].font.size = Pt(9)
            doc.add_page_break()

        path = os.path.join(self.output_dir, f"{self.basename}.docx")
        doc.save(path)
        return path

    # ── PDF ─────────────────────────────────────────────────────────────

    def generate_pdf(self) -> str:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (Image, PageBreak, Paragraph,
                                         SimpleDocTemplate, Spacer, Table, TableStyle)

        path = os.path.join(self.output_dir, f"{self.basename}.pdf")
        doc = SimpleDocTemplate(path, pagesize=landscape(A4),
                                 leftMargin=28, rightMargin=28,
                                 topMargin=28, bottomMargin=28)
        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("h1x", parent=styles["Heading1"], textColor=colors.HexColor("#E28C00"))
        small = ParagraphStyle("small", parent=styles["Normal"], fontSize=8.5)
        story = []

        total = self.result.get("total", 0)
        passed = self.result.get("passed", 0)
        failed = self.result.get("failed", 0)
        story.append(Paragraph(f"Test Execution Evidence — {self.flow_id}", h1))
        story.append(Paragraph(
            f"System: {self.sid} &nbsp;|&nbsp; "
            f"Executed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp; "
            f"By: {self.executed_by or 'SAP Sentinel'}<br/>"
            f"Iterations: {total} &nbsp; Passed: {passed} &nbsp; Failed: {failed}", small))
        story.append(Spacer(1, 10))

        key_cols = [b.get("column") for b in self.bindings][:3]
        data = [["#"] + key_cols + ["Status", "Result"]]
        for it in self.result.get("iterations", []):
            data.append(
                [str(it.get("index", ""))]
                + [str(it.get("inputs", {}).get(c, ""))[:22] for c in key_cols]
                + ["PASS" if it.get("ok") else "FAIL",
                   (str(it.get("outcome") or it.get("error") or ""))[:60]])
        table = Table(data, repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(table)

        for it in self.result.get("iterations", []):
            story.append(PageBreak())
            ok = it.get("ok")
            story.append(Paragraph(
                f"Iteration {it.get('index')} — {'PASSED' if ok else 'FAILED'}",
                ParagraphStyle("it", parent=styles["Heading2"],
                                textColor=colors.HexColor("#1E8E3E" if ok else "#C5221F"))))
            inputs = it.get("inputs", {})
            if inputs:
                story.append(Paragraph(
                    " &nbsp;|&nbsp; ".join(f"<b>{k}</b>: {v}" for k, v in inputs.items()),
                    small))
            if it.get("outcome"):
                story.append(Paragraph(f"<b>Outcome:</b> {it['outcome']}", small))
            if it.get("exports"):
                story.append(Paragraph("<b>Values read:</b> " + ", ".join(
                    f"{_esc(k)} = {_esc(v)}" for k, v in it["exports"].items()), small))
            if it.get("error"):
                story.append(Paragraph(
                    f"<font color='#C5221F'><b>Error:</b> {it['error']}</font>", small))
            story.append(Spacer(1, 6))
            for shot in it.get("screenshots", []):
                # D-035: reportlab loads images lazily, so a corrupt file would
                # abort the WHOLE pdf at build time — validate first.
                if _pdf_image_ok(shot):
                    story.append(Image(shot, width=9.4 * inch, height=4.6 * inch,
                                        kind="proportional"))
                    story.append(Spacer(1, 6))

        doc.build(story)
        return path
