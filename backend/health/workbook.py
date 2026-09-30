"""Workbook reader for SAP AHC exports.

SAP's Automated Health Check emits Excel 2003 "XML Spreadsheet"
(SpreadsheetML) with an .XLS extension. Real exports are hostile input:
  - illegal XML 1.0 char refs (&#0; runs after "NONE" in DB02 sheets)
  - ss:MergeAcross / ss:Index gaps shift column positions
  - several tables per sheet (ST02 per server, SMLG x3), key/value blocks
    (DB02_Memory) and fixed-width text reports (Cert_Info, SICK)
.xlsx is accepted too (same row model). Legacy binary BIFF .xls is rejected
with a precise instruction instead of a stack trace.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

SS = "{urn:schemas-microsoft-com:office:spreadsheet}"
MAX_BYTES = 40 * 1024 * 1024

# Char refs illegal in XML 1.0: &#0;-&#8;, &#11;, &#12;, &#14;-&#31; (dec or hex)
_ILLEGAL_REF = re.compile(
    r"&#(?:0*(?:[0-8]|1[124-9]|2[0-9]|3[01])|x0*(?:[0-8bBcCeEfF]|1[0-9a-fA-F]));")
_ILLEGAL_RAW = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class WorkbookError(ValueError):
    """User-facing: message says what's wrong and how to fix it."""


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[str]]


@dataclass
class Sheet:
    name: str
    rows: list[list[str]]
    tables: list[Table] = field(default_factory=list)
    text: str = ""          # for report-style sheets (Cert_Info, SICK, RZ03...)
    from_xlsx: bool = False  # xlsx loses the blank-row cell counts SpreadsheetML keeps


def read_workbook(data: bytes, filename: str = "") -> dict[str, Sheet]:
    if len(data) > MAX_BYTES:
        raise WorkbookError(f"File is {len(data)//1_048_576} MB; the limit is "
                            f"{MAX_BYTES//1_048_576} MB.")
    if data[:4] == b"PK\x03\x04":
        sheets = _read_xlsx(data)
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        raise WorkbookError(
            "This is a legacy binary Excel file. Open it in Excel and save it "
            "as 'XML Spreadsheet 2003' or '.xlsx', then import that file.")
    else:
        sheets = _read_spreadsheetml(data)
    if not sheets:
        raise WorkbookError("The file contains no worksheets.")
    for sh in sheets.values():
        sh.tables = extract_tables(sh.rows, loose_blank=sh.from_xlsx)
        sh.text = "\n".join(" ".join(c for c in r if c).rstrip() for r in sh.rows)
    return sheets


def _read_spreadsheetml(data: bytes) -> dict[str, Sheet]:
    text = data.decode("utf-8-sig", errors="replace")
    head = text[:4096]
    if "<Workbook" not in text[:20000]:
        raise WorkbookError("Not an SAP health check workbook: expected an XML "
                            "Spreadsheet (SpreadsheetML) or .xlsx file.")
    # Uploaded XML never needs a DTD; refusing one closes entity-expansion attacks.
    if "<!DOCTYPE" in text or "<!ENTITY" in head:
        raise WorkbookError("The file declares a DOCTYPE, which SAP exports never "
                            "do. Refusing to parse it.")
    text = _ILLEGAL_REF.sub("", text)
    text = _ILLEGAL_RAW.sub("", text)
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise WorkbookError(f"The XML is damaged and cannot be read ({exc}).") from exc

    out: dict[str, Sheet] = {}
    for ws in root.iter(SS + "Worksheet"):
        name = ws.get(SS + "Name") or f"Sheet{len(out)+1}"
        rows: list[list[str]] = []
        table = ws.find(SS + "Table")
        if table is not None:
            for r in table.findall(SS + "Row"):
                row: list[str] = []
                for c in r.findall(SS + "Cell"):
                    idx = c.get(SS + "Index")
                    if idx and idx.isdigit():
                        while len(row) < int(idx) - 1:
                            row.append("")
                    d = c.find(SS + "Data")
                    row.append(_clean("".join(d.itertext())) if d is not None else "")
                    ma = c.get(SS + "MergeAcross")
                    if ma and ma.isdigit():
                        row.extend([""] * int(ma))
                rows.append(row)
        out[name] = Sheet(name=name, rows=rows)
    return out


def _read_xlsx(data: bytes) -> dict[str, Sheet]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise WorkbookError("xlsx support needs openpyxl installed.") from exc
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise WorkbookError(f"The .xlsx file cannot be opened ({exc}).") from exc
    out: dict[str, Sheet] = {}
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            vals = [_clean("" if v is None else str(v)) for v in r]
            while vals and not vals[-1]:       # openpyxl pads every row to sheet width
                vals.pop()
            rows.append(vals)
        out[ws.title] = Sheet(name=ws.title, rows=rows, from_xlsx=True)
    return out


def _clean(s: str) -> str:
    return s.replace("\r", "").strip()


def _nonempty(row: list[str]) -> int:
    return sum(1 for c in row if c)


_VALUE = re.compile(r"^-?[\d.,]+%?$|\d{2}[./]\d{2}[./]\d{4}|\d{4}-\d{2}-\d{2}")


def _is_header(rows: list[list[str]], i: int, loose_blank: bool) -> bool:
    """AHC's signature: a label row (>=2 filled cells, no values) after a
    blank/title row, followed by an all-blank row. In SpreadsheetML that
    blank row keeps the header's cell count (data separators are <Row/>);
    xlsx loses the distinction, hence loose_blank."""
    r, nxt = rows[i], rows[i + 1]
    if _nonempty(r) < 2 or _nonempty(nxt) != 0:
        return False
    if not loose_blank and len(nxt) < 2:
        return False
    if i > 0 and _nonempty(rows[i - 1]) > 1:
        return False
    return not any(_VALUE.search(c) for c in r if c)


def extract_tables(rows: list[list[str]], loose_blank: bool = False) -> list[Table]:
    """Split a sheet into tables. Data runs from the row after a header's
    blank row to the next header or end of sheet."""
    tables: list[Table] = []
    n = len(rows)
    header_idx = [i for i in range(n - 1) if _is_header(rows, i, loose_blank)]
    for k, h in enumerate(header_idx):
        stop = header_idx[k + 1] if k + 1 < len(header_idx) else n
        cols = rows[h]
        while cols and not cols[-1]:
            cols = cols[:-1]
        body: list[list[str]] = []
        for r in rows[h + 2: stop]:
            if _nonempty(r) == 0:   # SAP separates some tables' rows with empty <Row/>s
                continue
            # Section-title rows ("ST02 | | |", "Tune Summary (...)") are short
            # and carry one value; real data rows span the header's width.
            if _nonempty(r) == 1 and len(r) < max(3, len(cols) // 2 + 1):
                continue
            body.append((r + [""] * len(cols))[: max(len(cols), 1)])
        # Title: nearest preceding single-text row, skipping blanks.
        title = ""
        for j in range(h - 1, max(-1, h - 4), -1):
            if _nonempty(rows[j]) == 1:
                title = next(c for c in rows[j] if c)
                break
            if _nonempty(rows[j]) > 1:
                break
        # Next header's own title row isn't data: trim a trailing title-only row.
        if body and k + 1 < len(header_idx) and _nonempty(body[-1]) == 1 \
                and body[-1][0] and body[-1][0] == _first(rows, header_idx[k + 1]):
            body.pop()
        tables.append(Table(title=title, columns=cols, rows=body))
    return tables


def _first(rows: list[list[str]], header_i: int) -> str:
    for j in range(header_i - 1, max(-1, header_i - 4), -1):
        if _nonempty(rows[j]):
            return next(c for c in rows[j] if c)
    return ""
