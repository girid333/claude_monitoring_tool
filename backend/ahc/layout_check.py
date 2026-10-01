"""Row-layout signature of an AHC SpreadsheetML workbook (D-051).

Used by the tests to compare a generated workbook with SAP's own export
without depending on cell values: per sheet, the style of the title cell and
the 1-based row number of the first table header (style s69).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from health.workbook import _ILLEGAL_RAW, _ILLEGAL_REF

SS = "urn:schemas-microsoft-com:office:spreadsheet"


def _a(el, name):
    return el.get(f"{{{SS}}}{name}")


def sheet_signature(data: bytes) -> dict[str, dict]:
    text = data.decode("utf-8-sig", errors="replace")
    root = ET.fromstring(_ILLEGAL_RAW.sub("", _ILLEGAL_REF.sub("", text)))
    out: dict[str, dict] = {}
    for ws in root.findall(f"{{{SS}}}Worksheet"):
        table = ws.find(f"{{{SS}}}Table")
        rowno, header, title = 0, None, None
        for row in (table if table is not None else []):
            if row.tag != f"{{{SS}}}Row":
                continue
            idx = _a(row, "Index")
            rowno = int(idx) if idx else rowno + 1
            cells = row.findall(f"{{{SS}}}Cell")
            if title is None and cells:
                title = _a(cells[0], "StyleID")
            if header is None and cells and _a(cells[0], "StyleID") == "s69":
                header = rowno
            span = _a(row, "Span")
            if span:
                rowno += int(span)
        out[_a(ws, "Name")] = {"title_style": title, "header_row": header}
    return out


def excel_problems(data: bytes) -> list[tuple[str, str]]:
    """Structural rules Excel enforces when loading SpreadsheetML — any hit
    makes Excel refuse or "repair" the file. Empty list = loads cleanly."""
    text = data.decode("utf-8-sig", "replace")
    probs = []
    if _ILLEGAL_REF.search(text) or _ILLEGAL_RAW.search(text):
        probs.append(("*", "illegal XML control characters present"))
    root = ET.fromstring(_ILLEGAL_RAW.sub("", _ILLEGAL_REF.sub("", text)))
    styles = {_a(s, "ID") for s in root.iter(f"{{{SS}}}Style")}
    names = set()
    for ws in root.findall(f"{{{SS}}}Worksheet"):
        nm = _a(ws, "Name")
        t = ws.find(f"{{{SS}}}Table")
        if len(nm) > 31 or any(ch in nm for ch in '[]:*?/\\'):
            probs.append((nm, "bad sheet name"))
        if nm.lower() in names:
            probs.append((nm, "duplicate sheet name"))
        names.add(nm.lower())
        ecc, erc = int(_a(t, "ExpandedColumnCount") or 0), int(_a(t, "ExpandedRowCount") or 0)
        ncol = 0
        for c in [c for c in t if c.tag == f"{{{SS}}}Column"]:
            ncol = int(_a(c, "Index")) if _a(c, "Index") else ncol + 1
            ncol += int(_a(c, "Span") or 0)
        if ecc and ncol > ecc:
            probs.append((nm, f"{ncol} Column defs > ExpandedColumnCount {ecc}"))
        r = maxc = 0
        for row in [x for x in t if x.tag == f"{{{SS}}}Row"]:
            idx = _a(row, "Index")
            if idx:
                if int(idx) <= r:
                    probs.append((nm, f"row Index {idx} not increasing (at {r})"))
                r = int(idx)
            else:
                r += 1
            c = 0
            for cell in row.findall(f"{{{SS}}}Cell"):
                ci = _a(cell, "Index")
                if ci:
                    if int(ci) <= c:
                        probs.append((nm, f"row {r}: cell Index {ci} not increasing"))
                    c = int(ci)
                else:
                    c += 1
                c += int(_a(cell, "MergeAcross") or 0)
                st = _a(cell, "StyleID")
                if st and st not in styles:
                    probs.append((nm, f"row {r}: unknown style {st}"))
                d = cell.find(f"{{{SS}}}Data")
                if d is not None and _a(d, "Type") == "Number":
                    try:
                        float(d.text)
                    except Exception:
                        probs.append((nm, f"row {r}: bad number {d.text!r}"))
                if d is not None and d.text and len(d.text) > 32767:
                    probs.append((nm, "cell > 32767 chars"))
            maxc = max(maxc, c)
            r += int(_a(row, "Span") or 0)
        if ecc and maxc > ecc:
            probs.append((nm, f"cells reach column {maxc} > ExpandedColumnCount {ecc}"))
        if erc and r > erc:
            probs.append((nm, f"rows reach {r} > ExpandedRowCount {erc}"))
    return probs
