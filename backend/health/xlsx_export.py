"""Convert an AHC workbook (SpreadsheetML 2003 "XML Spreadsheet") to .xlsx.

SAP's Automated Health Check — and the direct pull that reproduces it (D-051)
— produce SpreadsheetML saved as .XLS. Current Excel versions warn about the
extension, Excel for Mac and other viewers often refuse it, and Excel rejects
the whole file on any structural slip. Export therefore always delivers a real
Office Open XML workbook, keeping what the AHC layout relies on: tab order,
cell values, fonts, fills, borders, alignment and wrapping, merged ranges,
column widths and row heights.

Works for SAP's own exports too (they can contain XML-illegal control
characters, which are dropped). An .xlsx input is returned unchanged.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .workbook import _ILLEGAL_RAW, _ILLEGAL_REF

SS = "urn:schemas-microsoft-com:office:spreadsheet"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HALIGN = {"Left": "left", "Center": "center", "Right": "right", "Justify": "justify",
           "Fill": "fill", "CenterAcrossSelection": "centerContinuous", "Distributed": "distributed"}
_VALIGN = {"Top": "top", "Center": "center", "Bottom": "bottom", "Justify": "justify",
           "Distributed": "distributed"}
_LINE = {("Continuous", "1"): "thin", ("Continuous", "2"): "medium", ("Continuous", "3"): "thick",
         ("Dash", "1"): "dashed", ("Dash", "2"): "mediumDashed", ("Dot", "1"): "dotted",
         ("Double", "3"): "double", ("DashDot", "1"): "dashDot", ("DashDotDot", "1"): "dashDotDot"}


def _a(el, name: str) -> Optional[str]:
    return el.get(f"{{{SS}}}{name}") if el is not None else None


def _q(tag: str) -> str:
    return f"{{{SS}}}{tag}"


def _color(c: Optional[str]) -> Optional[str]:
    if not c or not re.fullmatch(r"#[0-9A-Fa-f]{6}", c):
        return None
    return "FF" + c[1:].upper()


class _Style:
    def __init__(self, el, parent: Optional["_Style"] = None):
        p = parent
        al, fo, it, bo = (el.find(_q(t)) for t in ("Alignment", "Font", "Interior", "Borders"))
        nf = el.find(_q("NumberFormat"))
        if al is not None:
            self.alignment = Alignment(horizontal=_HALIGN.get(_a(al, "Horizontal") or ""),
                                       vertical=_VALIGN.get(_a(al, "Vertical") or ""),
                                       wrap_text=_a(al, "WrapText") == "1",
                                       indent=int(_a(al, "Indent") or 0))
        else:
            self.alignment = p.alignment if p else None
        if fo is not None:
            self.font = Font(name=_a(fo, "FontName") or (p.font.name if p and p.font else "Calibri"),
                             size=float(_a(fo, "Size") or (p.font.sz if p and p.font else 11) or 11),
                             bold=_a(fo, "Bold") == "1", italic=_a(fo, "Italic") == "1",
                             strike=_a(fo, "StrikeThrough") == "1",
                             underline="single" if _a(fo, "Underline") == "Single" else
                             ("double" if _a(fo, "Underline") == "Double" else None),
                             color=_color(_a(fo, "Color")))
        else:
            self.font = p.font if p else None
        fill = _color(_a(it, "Color")) if it is not None else None
        if fill and (_a(it, "Pattern") or "Solid") != "None":
            self.fill = PatternFill("solid", start_color=fill, end_color=fill)
        else:
            self.fill = p.fill if (p and it is None) else None
        if bo is not None:
            sides = {}
            for b in bo.findall(_q("Border")):
                pos = (_a(b, "Position") or "").lower()
                st = _LINE.get((_a(b, "LineStyle") or "Continuous", _a(b, "Weight") or "1"), "thin")
                if pos in ("left", "right", "top", "bottom"):
                    sides[pos] = Side(style=st, color=_color(_a(b, "Color")) or "FF000000")
            self.border = Border(**sides) if sides else None
        else:
            self.border = p.border if p else None
        fmt = _a(nf, "Format") if nf is not None else None
        self.number_format = {"General Number": "General", "Fixed": "0.00", "Percent": "0.00%",
                              "Short Date": "dd.mm.yyyy"}.get(fmt or "", fmt) or (p.number_format if p else None)

    def apply(self, cell) -> None:
        if self.font is not None:
            cell.font = self.font
        if self.fill is not None:
            cell.fill = self.fill
        if self.border is not None:
            cell.border = self.border
        if self.alignment is not None:
            cell.alignment = self.alignment
        if self.number_format:
            cell.number_format = self.number_format


def _styles(root) -> dict[str, _Style]:
    els = {(_a(s, "ID") or ""): s for s in root.iter(_q("Style"))}
    done: dict[str, _Style] = {}

    def build(sid: str, depth: int = 0) -> Optional[_Style]:
        if sid in done:
            return done[sid]
        el = els.get(sid)
        if el is None or depth > 10:
            return None
        parent = build(_a(el, "Parent") or ("Default" if sid != "Default" else ""), depth + 1)
        done[sid] = _Style(el, parent)
        return done[sid]

    for sid in els:
        build(sid)
    return done


def _value(data):
    if data is None:
        return None
    t, text = _a(data, "Type") or "String", "".join(data.itertext())
    text = _CTRL.sub("", text)
    if t == "Number":
        try:
            f = float(text)
            return int(f) if f.is_integer() and abs(f) < 1e15 and "." not in text and "e" not in text.lower() else f
        except ValueError:
            return text
    if t == "Boolean":
        return text.strip() in ("1", "true", "TRUE")
    return text[:32767]


def _col_width(points: float) -> float:
    # SpreadsheetML widths are points; openpyxl wants Excel character units.
    return max(0.0, round((points * 96 / 72 - 5) / 7, 2))


def spreadsheetml_to_xlsx(data: bytes) -> bytes:
    if data[:2] == b"PK":
        return data                                     # already .xlsx
    text = data.decode("utf-8-sig", errors="replace")
    text = _ILLEGAL_RAW.sub("", _ILLEGAL_REF.sub("", text))
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text)
    root = ET.fromstring(text)
    styles = _styles(root)
    default = styles.get("Default")
    wb = Workbook()
    wb.remove(wb.active)
    used: set[str] = set()
    for ws_el in root.findall(_q("Worksheet")):
        name = re.sub(r"[\[\]:*?/\\]", "_", _a(ws_el, "Name") or "Sheet")[:31] or "Sheet"
        base, k = name, 1
        while name.lower() in used:
            k += 1
            name = f"{base[:28]}_{k}"
        used.add(name.lower())
        ws = wb.create_sheet(name)
        table = ws_el.find(_q("Table"))
        if table is None:
            continue
        default_h = _a(table, "DefaultRowHeight")
        if default_h:
            ws.sheet_format.defaultRowHeight = float(default_h)
        col = 0
        for c in table.findall(_q("Column")):
            col = int(_a(c, "Index")) if _a(c, "Index") else col + 1
            span = int(_a(c, "Span") or 0)
            for j in range(col, col + span + 1):
                dim = ws.column_dimensions[get_column_letter(j)]
                if _a(c, "Width"):
                    dim.width = _col_width(float(_a(c, "Width")))
                if _a(c, "Hidden") == "1":
                    dim.hidden = True
            col += span
        merges: list[tuple[int, int, int, int]] = []
        taken: set[tuple[int, int]] = set()
        r = 0
        for row in table.findall(_q("Row")):
            r = int(_a(row, "Index")) if _a(row, "Index") else r + 1
            if _a(row, "Height"):
                ws.row_dimensions[r].height = float(_a(row, "Height"))
            if _a(row, "Hidden") == "1":
                ws.row_dimensions[r].hidden = True
            c = 0
            for cell_el in row.findall(_q("Cell")):
                c = int(_a(cell_el, "Index")) if _a(cell_el, "Index") else c + 1
                while (r, c) in taken:                  # cell covered by a MergeDown from above
                    c += 1
                across, down = int(_a(cell_el, "MergeAcross") or 0), int(_a(cell_el, "MergeDown") or 0)
                cell = ws.cell(row=r, column=c)
                v = _value(cell_el.find(_q("Data")))
                if v not in (None, ""):
                    cell.value = v
                st = styles.get(_a(cell_el, "StyleID") or "") or default
                if st:
                    st.apply(cell)
                if across or down:
                    merges.append((r, c, r + down, c + across))
                    for rr in range(r, r + down + 1):
                        for cc in range(c, c + across + 1):
                            if (rr, cc) != (r, c):
                                taken.add((rr, cc))
                    if st:                                # borders on every cell of the merged range
                        for rr in range(r, r + down + 1):
                            for cc in range(c, c + across + 1):
                                if (rr, cc) != (r, c):
                                    st.apply(ws.cell(row=rr, column=cc))
                c += across
            r += int(_a(row, "Span") or 0)
        for r1, c1, r2, c2 in merges:
            ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)
    if not wb.worksheets:
        wb.create_sheet("Sheet1")
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
