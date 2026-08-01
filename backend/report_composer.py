"""D-014: v2 report composer. Inherited from v1's `report_generator.py`
(sap_monitoring/backend/report_generator.py, 805 lines, customer-accepted
Word+PDF output) and refactored, NOT rebuilt:

  1. Hardcoded, machine-specific cover-image path removed — `cover_image_path`
     is now a constructor argument, default None (no cover page).
  2. Severity model added. v1's anomalies were a flat list of strings with no
     severity; a heuristic classifier (`classify_v1_anomaly_severity`) grades
     them so the Executive Summary and Detailed Status sections can render
     severity chips consistent with the D-016 color tokens. v2 FlowSection
     results (Functional Sanity) carry REAL severity from CheckResult
     (D-009), not a heuristic — no guessing needed there.
  3. Sections are pluggable: `extra_sections` renders recorded-flow results
     (FlowSection, via `sections.flow_results_to_report_rows`) in a new
     "4. Functional Sanity" section using the same document, so Core BASIS
     (v1, unchanged rendering below) and Functional Sanity (v2) come out of
     one report per D-017.

All per-T-code screenshot embedding logic (SM21/ST22/DB02/ST03N/SCC4/SMLG/
SMGW/SMICM/STRUST special cases) is copied verbatim from v1 — it's proven,
customer-accepted output and D-014 says inherit, not rewrite it.
"""

import os
from datetime import datetime

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.section import WD_ORIENT
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image,
                                 Table, TableStyle, PageBreak)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# D-016 semantic palette, extended with "medium" (amber) and "ok" (green) —
# v1 only ever had two states (Action Required / Normal), D-014 needs five.
SEVERITY_HEX = {
    "critical": "#FF453A",
    "high": "#FF9F0A",
    "medium": "#FFD60A",
    "low": "#0A84FF",
    "info": "#8E8E93",
    "ok": "#30D158",
}
SEVERITY_LABEL = {
    "critical": "CRITICAL", "high": "HIGH", "medium": "MEDIUM",
    "low": "LOW", "info": "INFO", "ok": "OK",
}


def classify_v1_anomaly_severity(text: str) -> str:
    """v1's anomaly strings carry no severity (Known Risks, SKILL.md §5).
    Heuristic classification so the report can render something better than
    a flat 'Action Required'. RFC-unavailable notes (D-013/D-019: expected
    degradation, not a real system problem) are graded INFO, not HIGH —
    otherwise every run without RFC access would look alarming for no
    reason."""
    t = text.lower()
    if "rfc unavailable" in t or "rfc error" in t:
        return "info"
    if "critical" in t:
        return "critical"
    if any(k in t for k in ("cancel", "fail", "dump", "runtime error")):
        return "high"
    if any(k in t for k in ("warning", "expir", "overflow")):
        return "medium"
    return "medium"


def _rgb(hex_color: str) -> RGBColor:
    hex_color = hex_color.lstrip("#")
    return RGBColor(int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


class ReportGenerator:
    def __init__(self, results: dict, output_dir: str = "reports",
                 cover_image_path: str | None = None,
                 extra_sections: list[dict] | None = None):
        """
        results: v1's raw results dict (SectionResult.v1_results from
            CoreBasisSection) — {"tcodes": [...], "anomalies": [...],
            "external_checks": [...], "ssh_filesystem_data": [...]}.
            May be {} if Core BASIS wasn't part of this run.
        cover_image_path: optional cover page image; None = no cover (D-014
            refactor #1 — v1 hardcoded a machine-specific path here).
        extra_sections: list of {"title": str, "rows": list[dict]}, each
            `rows` shaped like sections.flow_results_to_report_rows() output
            (D-014 refactor #3 — Functional Sanity / recorded flows).
        """
        self.results = results
        self.output_dir = output_dir
        self.cover_image_path = cover_image_path
        self.extra_sections = extra_sections or []
        os.makedirs(self.output_dir, exist_ok=True)
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Word ──────────────────────────────────────────────────────────

    def generate_word(self) -> str:
        filepath = os.path.join(self.output_dir, f"SAP_Monitoring_Report_{self.timestamp}.docx")
        doc = Document()

        section = doc.sections[0]
        section.orientation = WD_ORIENT.LANDSCAPE
        section.page_width = Inches(16.5)
        section.page_height = Inches(11.7)

        gold_orange = RGBColor(230, 159, 0)
        blue_grey = RGBColor(60, 123, 153)
        try:
            style1 = doc.styles['Heading 1']
            style1.font.color.rgb = gold_orange
            style2 = doc.styles['Heading 2']
            style2.font.color.rgb = blue_grey
        except KeyError:
            pass

        title = doc.add_heading('SAP Monitoring Report v2.0', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.runs[0].font.color.rgb = gold_orange

        doc.add_paragraph(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}").alignment = WD_ALIGN_PARAGRAPH.CENTER
        doc.add_paragraph("_" * 100).alignment = WD_ALIGN_PARAGRAPH.CENTER

        if self.cover_image_path and os.path.exists(self.cover_image_path):
            doc.add_picture(self.cover_image_path, width=Inches(10.0))

        doc.add_page_break()

        # 1. Executive Summary — combines Core BASIS (heuristic severity) and
        # Functional Sanity (real severity) anomaly counts.
        doc.add_heading('1. Executive Summary', level=1)
        v1_anomalies = self.results.get("anomalies", [])
        flow_anomaly_rows = [
            (row["flow_id"], a) for sec in self.extra_sections for row in sec["rows"]
            for a in row.get("anomalies", [])
        ]
        total_anomalies = len(v1_anomalies) + len(flow_anomaly_rows)
        if total_anomalies == 0:
            doc.add_paragraph("No anomalies detected. All monitored systems and flows are operating normally.")
        else:
            doc.add_paragraph(f"A total of {total_anomalies} anomalies were detected across Core BASIS and Functional Sanity sections. Please review the relevant sections below.")

            table = doc.add_table(rows=1, cols=4)
            table.style = 'Medium Shading 1 Accent 4'
            hdr_cells = table.rows[0].cells
            hdr_cells[0].text = 'Component'
            hdr_cells[1].text = 'Anomaly Count'
            hdr_cells[2].text = 'Worst Severity'
            hdr_cells[3].text = 'Status'

            for tc_result in self.results.get("tcodes", []):
                tc_anomalies = tc_result.get("anomalies", [])
                if not tc_anomalies:
                    continue
                sevs = [classify_v1_anomaly_severity(a) for a in tc_anomalies]
                worst = _worst_severity(sevs)
                row_cells = table.add_row().cells
                row_cells[0].text = f"{tc_result['tcode']} ({tc_result['name']})"
                row_cells[1].text = str(len(tc_anomalies))
                row_cells[2].text = SEVERITY_LABEL[worst]
                _color_run(row_cells[2].paragraphs[0].runs[0] if row_cells[2].paragraphs[0].runs
                           else row_cells[2].paragraphs[0].add_run(SEVERITY_LABEL[worst]), worst)
                row_cells[3].text = "Action Required"

            for sec in self.extra_sections:
                for row in sec["rows"]:
                    if not row.get("anomalies"):
                        continue
                    row_cells = table.add_row().cells
                    row_cells[0].text = f"{row['flow_id']} ({sec['title']})"
                    row_cells[1].text = str(len(row["anomalies"]))
                    worst = row.get("worst_severity") or "medium"
                    row_cells[2].text = SEVERITY_LABEL.get(worst, worst.upper())
                    row_cells[3].text = "Action Required"

            doc.add_paragraph()

        doc.add_page_break()
        # 2. Infrastructure & Server Health (unchanged from v1)
        doc.add_heading('2. Infrastructure & Server Health', level=1)
        for check in self.results.get("external_checks", []):
            check_type = check.get("type", "")
            check_status = check.get("status", "")
            check_data = check.get("data", [])

            if check_data and isinstance(check_data, list) and len(check_data) > 0:
                p = doc.add_paragraph()
                p.add_run(f"{check_type}").bold = True
                p.add_run(f" (Source: {check.get('method', 'RFC')})")

                keys = list(check_data[0].keys())
                table = doc.add_table(rows=1, cols=len(keys))
                table.style = 'Medium Shading 1 Accent 4'
                hdr_cells = table.rows[0].cells
                for i, key in enumerate(keys):
                    hdr_cells[i].text = str(key)
                    for paragraph in hdr_cells[i].paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(8)
                            run.font.bold = True

                for item in check_data:
                    row_cells = table.add_row().cells
                    for i, key in enumerate(keys):
                        row_cells[i].text = str(item.get(key, ""))
                        for paragraph in row_cells[i].paragraphs:
                            for run in paragraph.runs:
                                run.font.size = Pt(7)
                doc.add_paragraph()

            elif "url" in check:
                p = doc.add_paragraph()
                p.add_run("URL Reachability Check").bold = True
                doc.add_paragraph(f"{check['url']} → {check_status} ({check.get('details', '')})")
                doc.add_paragraph()
            elif "details" in check:
                p = doc.add_paragraph()
                p.add_run(f"{check_type}").bold = True
                doc.add_paragraph(f"{check_status} - {check.get('details', '')}")
                doc.add_paragraph()
            else:
                p = doc.add_paragraph()
                p.add_run(f"{check_type}").bold = True
                doc.add_paragraph(f"{check_status}")
                doc.add_paragraph()

        ssh_fs = self.results.get("ssh_filesystem_data", [])
        if ssh_fs:
            for fs_entry in ssh_fs:
                server = fs_entry.get("server", "Unknown")
                data = fs_entry.get("data", [])
                if data:
                    p = doc.add_paragraph()
                    p.add_run(f"OS Filesystem Snapshot (via SSH): {server}").bold = True

                    keys = list(data[0].keys())
                    table = doc.add_table(rows=1, cols=len(keys))
                    table.style = 'Medium Shading 2 Accent 2'
                    hdr_cells = table.rows[0].cells
                    for i, key in enumerate(keys):
                        hdr_cells[i].text = str(key)
                        for paragraph in hdr_cells[i].paragraphs:
                            for run in paragraph.runs:
                                run.font.size = Pt(8)

                    for item in data:
                        row_cells = table.add_row().cells
                        for i, key in enumerate(keys):
                            row_cells[i].text = str(item.get(key, ""))
                            for paragraph in row_cells[i].paragraphs:
                                for run in paragraph.runs:
                                    run.font.size = Pt(7)
                    doc.add_paragraph()

        # 3. Detailed Status — Core BASIS (v1 tcodes), unchanged rendering,
        # anomaly bullets now colored by heuristic severity.
        if self.results.get("tcodes"):
            doc.add_page_break()
            doc.add_heading('3. Detailed Status — Core BASIS', level=1)
            for i, tcode_result in enumerate(self.results.get("tcodes", [])):
                if i > 0:
                    doc.add_page_break()
                doc.add_heading(f"{tcode_result['tcode']} - {tcode_result['name']}", level=2)

                analysis = tcode_result.get("analysis_summary", "Analysis completed.")
                p = doc.add_paragraph()
                p.add_run("System Analysis: ").bold = True
                p.add_run(analysis)

                _embed_v1_screenshots(doc, tcode_result)

                if tcode_result["anomalies"]:
                    doc.add_paragraph("Anomalies Detected:", style='List Bullet')
                    for anom in tcode_result["anomalies"]:
                        sev = classify_v1_anomaly_severity(anom)
                        para = doc.add_paragraph(style='List Bullet 2')
                        run = para.add_run(f"[{SEVERITY_LABEL[sev]}] {anom}")
                        run.font.color.rgb = _rgb(SEVERITY_HEX[sev])
                else:
                    p = doc.add_paragraph()
                    run = p.add_run("Status: Normal")
                    run.font.color.rgb = _rgb(SEVERITY_HEX["ok"])

                doc.add_page_break()

        # 4. Functional Sanity — v2 recorded flows (D-017), real severity
        # from CheckResult, low-confidence DOM steps flagged per D-003.
        if self.extra_sections:
            doc.add_page_break()
            doc.add_heading('4. Functional Sanity', level=1)
            for sec in self.extra_sections:
                doc.add_heading(sec["title"], level=2)
                for row in sec["rows"]:
                    doc.add_heading(f"{row['flow_id']} ({row['sid']}, {row['mode']} mode)", level=3)
                    if row.get("error"):
                        p = doc.add_paragraph()
                        run = p.add_run(f"Run error: {row['error']}")
                        run.font.color.rgb = _rgb(SEVERITY_HEX["critical"])

                    for shot in row.get("screenshots", []):
                        if shot and os.path.exists(shot):
                            try:
                                doc.add_picture(shot, width=Inches(14.0))
                            except Exception as e:  # noqa: BLE001
                                doc.add_paragraph(f"[Image load error: {e}]")

                    if row.get("anomalies"):
                        doc.add_paragraph("Failed checks:", style='List Bullet')
                        for a in row["anomalies"]:
                            sev = a["severity"]
                            para = doc.add_paragraph(style='List Bullet 2')
                            run = para.add_run(
                                f"[{SEVERITY_LABEL.get(sev, sev.upper())}] {a['message']} "
                                f"(actual={a['actual']!r}, expected={a['expected']!r})")
                            run.font.color.rgb = _rgb(SEVERITY_HEX.get(sev, SEVERITY_HEX["medium"]))
                    else:
                        p = doc.add_paragraph()
                        run = p.add_run("Status: OK — all checks passed")
                        run.font.color.rgb = _rgb(SEVERITY_HEX["ok"])

                    if row.get("low_confidence_steps"):
                        p = doc.add_paragraph()
                        run = p.add_run(
                            f"⚠ DOM resolver confidence below threshold on step(s) "
                            f"{row['low_confidence_steps']} — verify this flow still "
                            f"matches the live screen (D-003).")
                        run.font.color.rgb = _rgb(SEVERITY_HEX["medium"])
                        run.italic = True

        doc.save(filepath)
        return filepath

    # ── PDF ───────────────────────────────────────────────────────────

    def generate_pdf(self) -> str:
        filepath = os.path.join(self.output_dir, f"SAP_Monitoring_Report_{self.timestamp}.pdf")
        doc = SimpleDocTemplate(filepath, pagesize=landscape(A3))
        styles = getSampleStyleSheet()
        elements = []

        gold_orange = colors.HexColor("#E69F00")
        blue_grey = colors.HexColor("#3C7B99")

        h1_style = ParagraphStyle('StylishHeading1', parent=styles['Heading1'],
                                   textColor=gold_orange, fontSize=22, spaceAfter=12)
        h2_style = ParagraphStyle('StylishHeading2', parent=styles['Heading2'],
                                   textColor=blue_grey, fontSize=16, spaceAfter=10)
        h3_style = ParagraphStyle('StylishHeading3', parent=styles['Heading3'],
                                   fontSize=13, spaceAfter=8)

        title_style = ParagraphStyle('CoverTitle', parent=styles['Title'],
                                      textColor=gold_orange, fontSize=28)
        elements.append(Paragraph("SAP Monitoring Report v2.0", title_style))
        elements.append(Paragraph(f"Report Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        elements.append(Spacer(1, 20))

        if self.cover_image_path and os.path.exists(self.cover_image_path):
            elements.append(Image(self.cover_image_path, width=8 * inch, height=4 * inch))

        elements.append(PageBreak())

        # 1. Executive Summary
        elements.append(Paragraph("1. Executive Summary", h1_style))
        v1_anomalies = self.results.get("anomalies", [])
        flow_anomaly_rows = [
            a for sec in self.extra_sections for row in sec["rows"]
            for a in row.get("anomalies", [])
        ]
        total_anomalies = len(v1_anomalies) + len(flow_anomaly_rows)
        if total_anomalies == 0:
            elements.append(Paragraph("No anomalies detected. All monitored systems and flows are operating normally.", styles['Normal']))
        else:
            elements.append(Paragraph(f"A total of {total_anomalies} anomalies were detected across Core BASIS and Functional Sanity sections.", styles['Normal']))
            elements.append(Spacer(1, 10))

            table_data = [["Component", "Anomaly Count", "Worst Severity", "Status"]]
            for tcode_result in self.results.get("tcodes", []):
                tc_anomalies = tcode_result.get("anomalies", [])
                if not tc_anomalies:
                    continue
                worst = _worst_severity([classify_v1_anomaly_severity(a) for a in tc_anomalies])
                table_data.append([f"{tcode_result['tcode']} ({tcode_result['name']})",
                                    str(len(tc_anomalies)), SEVERITY_LABEL[worst], "Action Required"])
            for sec in self.extra_sections:
                for row in sec["rows"]:
                    if not row.get("anomalies"):
                        continue
                    worst = row.get("worst_severity") or "medium"
                    table_data.append([f"{row['flow_id']} ({sec['title']})",
                                        str(len(row["anomalies"])),
                                        SEVERITY_LABEL.get(worst, worst.upper()), "Action Required"])

            if len(table_data) > 1:
                t = Table(table_data)
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), gold_orange),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#FFFFFF')),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ]))
                elements.append(t)

        elements.append(PageBreak())

        # 2. Infrastructure & Server Health (unchanged from v1)
        elements.append(Paragraph("2. Infrastructure & Server Health", h1_style))
        for check in self.results.get("external_checks", []):
            check_type = check.get("type", "")
            check_status = check.get("status", "")
            check_data = check.get("data", [])

            if check_data and isinstance(check_data, list) and len(check_data) > 0:
                method = check.get('method', 'RFC')
                elements.append(Paragraph(f"<b>{check_type}</b> (Source: {method})", styles['Normal']))
                elements.append(Spacer(1, 5))

                keys = list(check_data[0].keys())
                table_data = [keys]
                for item in check_data:
                    table_data.append([str(item.get(k, "")) for k in keys])

                t = Table(table_data)
                t.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E69F00')),
                    ('TEXTCOLOR', (0, 0), (-1, 0), colors.black),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                    ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#FFFFFF')),
                    ('GRID', (0, 0), (-1, -1), 1, colors.black),
                    ('FONTSIZE', (0, 0), (-1, 0), 8),
                    ('FONTSIZE', (0, 1), (-1, -1), 7),
                    ('TOPPADDING', (0, 1), (-1, -1), 4),
                    ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#ECF0F1'), colors.white]),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#BDC3C7'))
                ]))
                elements.append(t)
                elements.append(Spacer(1, 10))

            elif "url" in check:
                elements.append(Paragraph("<b>URL Reachability Check</b>", styles['Normal']))
                elements.append(Paragraph(f"{check['url']} → {check_status} ({check.get('details', '')})", styles['Normal']))
                elements.append(Spacer(1, 15))
            elif "details" in check:
                elements.append(Paragraph(f"<b>{check_type}</b>", styles['Normal']))
                elements.append(Paragraph(f"{check_status} - {check.get('details', '')}", styles['Normal']))
                elements.append(Spacer(1, 15))
            else:
                elements.append(Paragraph(f"<b>{check_type}</b>", styles['Normal']))
                elements.append(Paragraph(f"{check_status}", styles['Normal']))
                elements.append(Spacer(1, 15))

        ssh_fs = self.results.get("ssh_filesystem_data", [])
        if ssh_fs:
            for fs_entry in ssh_fs:
                server = fs_entry.get("server", "Unknown")
                data = fs_entry.get("data", [])
                if data:
                    elements.append(Paragraph(f"<b>OS Filesystem Snapshot (via SSH): {server}</b>", styles['Normal']))
                    elements.append(Spacer(1, 5))
                    keys = list(data[0].keys())
                    table_data = [keys]
                    for item in data:
                        table_data.append([str(item.get(k, "")) for k in keys])
                    t = Table(table_data)
                    t.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2980B9')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                        ('FONTSIZE', (0, 0), (-1, 0), 8),
                        ('FONTSIZE', (0, 1), (-1, -1), 7),
                        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#D6EAF8'), colors.white]),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#3498DB'))
                    ]))
                    elements.append(t)
                    elements.append(Spacer(1, 10))

        # 3. Detailed Status — Core BASIS
        if self.results.get("tcodes"):
            elements.append(PageBreak())
            elements.append(Paragraph("3. Detailed Status — Core BASIS", h1_style))
            for i, tcode_result in enumerate(self.results.get("tcodes", [])):
                if i > 0:
                    elements.append(PageBreak())
                elements.append(Paragraph(f"{tcode_result['tcode']} - {tcode_result['name']}", h2_style))

                analysis = tcode_result.get("analysis_summary", "Analysis completed.")
                elements.append(Paragraph(f"<b>System Analysis:</b> {analysis}", styles['Normal']))
                elements.append(Spacer(1, 5))

                _embed_v1_screenshots_pdf(elements, styles, tcode_result)

                if tcode_result["anomalies"]:
                    elements.append(Paragraph("Anomalies Detected:", styles['Normal']))
                    for anom in tcode_result["anomalies"]:
                        sev = classify_v1_anomaly_severity(anom)
                        c = SEVERITY_HEX[sev]
                        elements.append(Paragraph(
                            f"• <font color='{c}'><b>[{SEVERITY_LABEL[sev]}]</b></font> {anom}", styles['Normal']))
                else:
                    elements.append(Paragraph(
                        f"<font color='{SEVERITY_HEX['ok']}'>Status: Normal</font>", styles['Normal']))

                elements.append(Spacer(1, 20))

        # 4. Functional Sanity
        if self.extra_sections:
            elements.append(PageBreak())
            elements.append(Paragraph("4. Functional Sanity", h1_style))
            for sec in self.extra_sections:
                elements.append(Paragraph(sec["title"], h2_style))
                for row in sec["rows"]:
                    elements.append(Paragraph(f"{row['flow_id']} ({row['sid']}, {row['mode']} mode)", h3_style))
                    if row.get("error"):
                        elements.append(Paragraph(
                            f"<font color='{SEVERITY_HEX['critical']}'>Run error: {row['error']}</font>",
                            styles['Normal']))

                    for shot in row.get("screenshots", []):
                        if shot and os.path.exists(shot):
                            try:
                                elements.append(Image(shot, width=14.0 * inch, height=7.5 * inch))
                                elements.append(Spacer(1, 10))
                            except Exception as e:  # noqa: BLE001
                                elements.append(Paragraph(f"[Image load error: {e}]", styles['Normal']))

                    if row.get("anomalies"):
                        elements.append(Paragraph("Failed checks:", styles['Normal']))
                        for a in row["anomalies"]:
                            sev = a["severity"]
                            c = SEVERITY_HEX.get(sev, SEVERITY_HEX["medium"])
                            elements.append(Paragraph(
                                f"• <font color='{c}'><b>[{SEVERITY_LABEL.get(sev, sev.upper())}]</b></font> "
                                f"{a['message']} (actual={a['actual']!r}, expected={a['expected']!r})",
                                styles['Normal']))
                    else:
                        elements.append(Paragraph(
                            f"<font color='{SEVERITY_HEX['ok']}'>Status: OK — all checks passed</font>",
                            styles['Normal']))

                    if row.get("low_confidence_steps"):
                        elements.append(Paragraph(
                            f"<font color='{SEVERITY_HEX['medium']}'><i>⚠ DOM resolver confidence below "
                            f"threshold on step(s) {row['low_confidence_steps']} — verify this flow still "
                            f"matches the live screen (D-003).</i></font>", styles['Normal']))

                    elements.append(Spacer(1, 15))

        doc.build(elements)
        return filepath


def _worst_severity(sevs: list[str]) -> str:
    order = ["critical", "high", "medium", "low", "info"]
    for s in order:
        if s in sevs:
            return s
    return "info"


def _color_run(run, sev: str) -> None:
    run.font.color.rgb = _rgb(SEVERITY_HEX.get(sev, SEVERITY_HEX["medium"]))


def _embed_v1_screenshots(doc, tcode_result: dict) -> None:
    """Verbatim from v1's report_generator.py Word path — per-T-code special
    screenshot handling (SM21 multi-page, ST22 today/yesterday, DB02/ST03N/
    SCC4/SMLG/SMGW/SMICM/STRUST extra views), else the single generic shot."""
    tcode = tcode_result['tcode']

    sm21_pages = tcode_result.get("sm21_pages", [])
    if tcode == 'SM21' and sm21_pages:
        doc.add_paragraph("Visual Evidence (SAP WebGUI - System Log Pages):")
        for pi, ss_path in enumerate(sm21_pages):
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                try:
                    doc.add_paragraph(f"Page {pi + 1}:")
                    doc.add_picture(ss_path, width=Inches(14.0))
                except Exception:
                    pass
    elif tcode == 'ST22' and tcode_result.get("st22_pages", []):
        st22_pages = tcode_result["st22_pages"]
        day_labels = ["Today", "Yesterday"]
        doc.add_paragraph("Visual Evidence (SAP WebGUI - ABAP Dumps):")
        for pi, ss_path in enumerate(st22_pages):
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                try:
                    label = day_labels[pi] if pi < len(day_labels) else f"Page {pi + 1}"
                    doc.add_paragraph(f"{label}:")
                    doc.add_picture(ss_path, width=Inches(14.0))
                except Exception:
                    pass
    else:
        screenshot_path = tcode_result.get("screenshot")
        if screenshot_path and os.path.exists(screenshot_path):
            if os.path.getsize(screenshot_path) > 5000:
                doc.add_paragraph("Visual Evidence (SAP WebGUI):")
                try:
                    doc.add_picture(screenshot_path, width=Inches(14.0))
                except Exception as e:  # noqa: BLE001
                    doc.add_paragraph(f"[Image load error: {e}]")
            else:
                doc.add_paragraph("[Screenshot not available - WebGUI capture returned blank]")

    for key, label_prefix, labels in [
        ("db02_pages", "Additional DB02 Views:", ["Overview", "Performance Graph"]),
        ("ST03N_pages", "Additional ST03N Views:",
         ["Workload Overview", "Transaction Profile", "Time Profile", "Response Time Distribution"]),
        ("SCC4_pages", "SCC4 Client Configurations:", None),
        ("SMLG_pages", "SMLG Load Distribution Views:", ["Load Distribution"]),
        ("SMGW_pages", "SMGW Logged-On Clients:", ["Logged On Clients"]),
        ("SMICM_pages", "SMICM ICM Services Views:", ["ICM Services List"]),
        ("STRUST_pages", "STRUST Expiring Certificates:", None),
    ]:
        want_tcode = key.split("_")[0].upper()
        if tcode != want_tcode:
            continue
        pages = tcode_result.get(key, [])
        if key == "db02_pages" and not pages:
            continue
        if len(pages) <= (1 if key != "db02_pages" else 0):
            continue
        doc.add_paragraph(label_prefix)
        start = 0 if key == "db02_pages" else 1
        for pi in range(start, len(pages)):
            ss_path = pages[pi]
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                try:
                    idx = pi if key == "db02_pages" else pi - 1
                    label = labels[idx] if labels and idx < len(labels) else f"Page {pi + 1}"
                    doc.add_paragraph(f"{label}:" if key not in ("SCC4_pages", "STRUST_pages")
                                       else (f"Client {pi}:" if key == "SCC4_pages" else f"Anomaly Detail {pi}:"))
                    doc.add_picture(ss_path, width=Inches(14.0))
                except Exception:
                    pass


def _embed_v1_screenshots_pdf(elements, styles, tcode_result: dict) -> None:
    """PDF twin of _embed_v1_screenshots — verbatim v1 logic, reportlab flavor."""
    tcode = tcode_result['tcode']

    sm21_pages = tcode_result.get("sm21_pages", [])
    if tcode == 'SM21' and sm21_pages:
        elements.append(Paragraph("Visual Evidence (SAP WebGUI - System Log Pages):", styles['Normal']))
        for pi, ss_path in enumerate(sm21_pages):
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                elements.append(Paragraph(f"Page {pi + 1}:", styles['Normal']))
                try:
                    elements.append(Image(ss_path, width=14.0 * inch, height=7.5 * inch))
                except Exception:
                    pass
                elements.append(Spacer(1, 10))
    elif tcode == 'ST22' and tcode_result.get("st22_pages", []):
        st22_pages = tcode_result["st22_pages"]
        day_labels = ["Today", "Yesterday"]
        elements.append(Paragraph("Visual Evidence (SAP WebGUI - ABAP Dumps):", styles['Normal']))
        for pi, ss_path in enumerate(st22_pages):
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                label = day_labels[pi] if pi < len(day_labels) else f"Page {pi + 1}"
                elements.append(Paragraph(f"{label}:", styles['Normal']))
                try:
                    elements.append(Image(ss_path, width=14.0 * inch, height=7.5 * inch))
                except Exception:
                    pass
                elements.append(Spacer(1, 10))
    else:
        screenshot_path = tcode_result.get("screenshot")
        if screenshot_path and os.path.exists(screenshot_path):
            if os.path.getsize(screenshot_path) > 5000:
                elements.append(Paragraph("Visual Evidence (SAP WebGUI):", styles['Normal']))
                try:
                    elements.append(Image(screenshot_path, width=14.0 * inch, height=7.5 * inch))
                except Exception as e:  # noqa: BLE001
                    elements.append(Paragraph(f"[Image load error: {e}]", styles['Normal']))
                elements.append(Spacer(1, 10))
            else:
                elements.append(Paragraph("[Screenshot not available - WebGUI capture returned blank]", styles['Normal']))
                elements.append(Spacer(1, 5))

    for key, label_prefix, labels in [
        ("db02_pages", "Additional DB02 Views:", ["Overview", "Performance Graph"]),
        ("ST03N_pages", "Additional ST03N Views:",
         ["Workload Overview", "Transaction Profile", "Time Profile", "Response Time Distribution"]),
        ("SCC4_pages", "SCC4 Client Configurations:", None),
        ("SMLG_pages", "SMLG Load Distribution Views:", ["Load Distribution"]),
        ("SMGW_pages", "SMGW Logged-On Clients:", ["Logged On Clients"]),
        ("SMICM_pages", "SMICM ICM Services Views:", ["ICM Services List"]),
        ("STRUST_pages", "STRUST Expiring Certificates:", None),
    ]:
        want_tcode = key.split("_")[0].upper()
        if tcode != want_tcode:
            continue
        pages = tcode_result.get(key, [])
        if key == "db02_pages" and not pages:
            continue
        if len(pages) <= (1 if key != "db02_pages" else 0):
            continue
        elements.append(Paragraph(label_prefix, styles['Normal']))
        start = 0 if key == "db02_pages" else 1
        for pi in range(start, len(pages)):
            ss_path = pages[pi]
            if os.path.exists(ss_path) and os.path.getsize(ss_path) > 5000:
                idx = pi if key == "db02_pages" else pi - 1
                label = labels[idx] if labels and idx < len(labels) else f"Page {pi + 1}"
                caption = f"{label}:" if key not in ("SCC4_pages", "STRUST_pages") else (
                    f"Client {pi}:" if key == "SCC4_pages" else f"Anomaly Detail {pi}:")
                elements.append(Paragraph(caption, styles['Normal']))
                try:
                    elements.append(Image(ss_path, width=14.0 * inch, height=7.5 * inch))
                except Exception:
                    pass
                elements.append(Spacer(1, 10))
