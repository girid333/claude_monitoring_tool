"""SpreadsheetML writer reproducing SAP's Automated Health Check layout (D-051).

Every tab follows the sample exactly (ahc/layout.json): tab order, column
widths, styles, the two title rows, the header row at row 5 (after a
two-row blank span), the all-blank row that keeps the header's cell count,
then data. The D-050 parser (backend/health/) relies on that signature, so
a workbook written here imports like one SAP produced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

LAYOUT = json.loads((Path(__file__).with_name("layout.json")).read_text(encoding="utf-8"))

HEAD = """<?xml version="1.0"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:html="http://www.w3.org/TR/REC-html40">
 <DocumentProperties xmlns="urn:schemas-microsoft-com:office:office">
  <Author>Sentinel</Author>
  <Version>16.00</Version>
 </DocumentProperties>
 <OfficeDocumentSettings xmlns="urn:schemas-microsoft-com:office:office">
  <AllowPNG/>
 </OfficeDocumentSettings>
 <ExcelWorkbook xmlns="urn:schemas-microsoft-com:office:excel">
  <WindowHeight>10116</WindowHeight>
  <WindowWidth>23040</WindowWidth>
  <ProtectStructure>False</ProtectStructure>
  <ProtectWindows>False</ProtectWindows>
 </ExcelWorkbook>
"""
WS_OPTS = ('<WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">'
           "<ProtectObjects>False</ProtectObjects><ProtectScenarios>False</ProtectScenarios>"
           "</WorksheetOptions>")

TITLE, HEADER, BODY, OK_X, NOT_OK_X = "s73", "s69", "s70", "s71", "s72"
GREEN, RED, CYAN = "s75", "s76", "s77"
MON_TITLE = "m1878003756736"


@dataclass
class Block:
    """One table on a tab. `title`/`subtitle` are the two title rows; a block
    with neither (SMLG's 2nd/3rd table) starts after a blank span instead.
    `styles` colours individual cells: {(row_idx, col_idx): style_id}."""
    columns: list[str]
    rows: list[list[str]] = field(default_factory=list)
    title: Optional[str] = None
    subtitle: Optional[str] = None
    subtitle_merge: int = 3
    caption: Optional[str] = None       # DB02_Memory "Host ..." row above the header
    lead_blank: bool = False            # SM37 starts each block with an empty row
    styles: dict = field(default_factory=dict)


@dataclass
class TextTab:
    """Report-style tab (Cert_Info, SICK, RZ03, RZ04): one header cell then lines."""
    title: str
    subtitle: str
    header: str
    lines: list[tuple[str, Optional[str]]]   # (text, style or None)


@dataclass
class Verdict:
    ok: Optional[bool]          # None = not collected / not decidable -> neither box ticked
    evidence: str
    recommendation: str = ""


class _Rows:
    def __init__(self):
        self.out: list[str] = []
        self.n = 0              # last written row number (1-based)
        self.pending_index = False
        self.width = 1

    def row(self, cells: list[str], attrs: str = "", style_default: Optional[str] = None) -> None:
        idx = f' ss:Index="{self.n + 1}"' if self.pending_index else ""
        self.pending_index = False
        self.out.append(f"<Row{idx}{attrs}>" + "".join(cells) + "</Row>")
        self.n += 1

    def empty(self) -> None:
        idx = f' ss:Index="{self.n + 1}"' if self.pending_index else ""
        self.pending_index = False
        self.out.append(f'<Row{idx} ss:AutoFitHeight="0"/>')
        self.n += 1

    def blank_span(self) -> None:
        idx = f' ss:Index="{self.n + 1}"' if self.pending_index else ""
        self.out.append(f'<Row{idx} ss:AutoFitHeight="0" ss:Span="1"/>')
        self.n += 2
        self.pending_index = True


def _cell(v: str, style: Optional[str] = BODY, merge: int = 0) -> str:
    st = f' ss:StyleID="{style}"' if style else ""
    mg = f' ss:MergeAcross="{merge}"' if merge else ""
    if v is None:
        return f"<Cell{mg}{st}/>"
    return f'<Cell{mg}{st}><Data ss:Type="String">{escape(str(v))}</Data></Cell>'


def _title_rows(r: _Rows, title: str, subtitle: str, sub_merge: int) -> None:
    r.row([_cell(title, TITLE, 3)], ' ss:AutoFitHeight="0"')
    r.row([_cell(subtitle, TITLE, sub_merge)], ' ss:AutoFitHeight="0" ss:Height="27"')


def _highlight(sheet: str, i: int, j: int, row: list[str], n: int) -> Optional[str]:
    """SAP's own cell colouring, as observed in the sample export: the AHC
    program highlights problem values (and a few fixed columns) per tab."""
    v = (row[j] if j < len(row) else "").strip()
    if sheet == "SM51" and j == 3:
        return GREEN if v == "Active" else RED
    if sheet == "SM21" and j == 7 and v == "Very High Priority":
        return RED
    if sheet == "AL08" and j <= 2:
        return CYAN
    if (sheet, j) in (("SM50", 10), ("SM66", 11)) and v == "High":
        return "s78"
    if sheet == "SM04" and j == 7 and v == "High":
        return NOT_OK_X
    if sheet == "SMGW" and j == 6 and v:
        return GREEN if v == "Connected" else RED
    if sheet == "ST11" and j == 3 and v:
        return RED if v.startswith("stderr") else GREEN
    if sheet == "SLICENSE" and j in (0, 10):
        status = (row[10] if len(row) > 10 else "").strip()
        return OK_X if status == "Valid" else (None if status.startswith("Valid") else NOT_OK_X)
    if sheet == "ST06" and j in (1, 2, 3):
        return "s82" if _st06_memory(row) else "s80"
    if sheet == "DB02_Alerts":
        return "s83"
    if sheet == "DB02_Log" and i == n - 1:
        return "s78"                      # the most recent availability event
    return None


_MEMORY_WORDS = ("memory", "swap", "pages")


def _st06_memory(row: list[str]) -> bool:
    d = (row[2] if len(row) > 2 else "").lower()
    return any(w in d for w in _MEMORY_WORDS)


def _block(r: _Rows, b: Block, first: bool, meta: Optional[dict] = None, sheet: str = "") -> None:
    meta = meta or {}
    if b.lead_blank:
        r.empty()
    if b.title is not None:
        sub_merge = meta["sub_merge"] if first and meta.get("sub_merge") is not None else b.subtitle_merge
        _title_rows(r, b.title, b.subtitle or "", sub_merge)
        if first and meta.get("span_blank") is False:
            r.empty()
        else:
            r.blank_span()
    elif not first:
        r.blank_span()
    if b.caption:
        r.row([_cell(b.caption, HEADER)])
    hh = meta.get("header_height") if first else None
    r.row([_cell(c, HEADER) for c in b.columns], f' ss:Height="{hh}"' if hh else "")
    r.row(["<Cell ss:StyleID=\"s70\"/>" for _ in b.columns])     # keeps the header's cell count
    for i, row in enumerate(b.rows):
        vals = (list(row) + [""] * len(b.columns))[: len(b.columns)]
        r.row([_cell(v, b.styles.get((i, j)) or _highlight(sheet, i, j, vals, len(b.rows)) or BODY)
               for j, v in enumerate(vals)])
    r.width = max(r.width, len(b.columns), 4)


def _text(r: _Rows, t: TextTab, meta: Optional[dict] = None) -> None:
    sub_merge = (meta or {}).get("sub_merge") or 0
    r.row([_cell(t.title, TITLE, 3)], ' ss:AutoFitHeight="0"')
    r.row([_cell(t.subtitle, TITLE, sub_merge)], ' ss:AutoFitHeight="0" ss:Height="27"')
    r.blank_span()
    r.row([_cell(t.header, HEADER)])
    r.row(['<Cell ss:StyleID="s70"/>'])
    for text, style in t.lines:
        r.row([_cell(text, style)])
    r.width = 4


def _sheet(name: str, columns_xml: str, body: list[str], width: int, nrows: int) -> str:
    return (f'<Worksheet ss:Name="{escape(name)}"><Table ss:ExpandedColumnCount="{max(width, 1)}" '
            f'ss:ExpandedRowCount="{nrows + 1}" x:FullColumns="1" x:FullRows="1" '
            f'ss:DefaultRowHeight="14.4">{columns_xml}' + "".join(body) + f"</Table>{WS_OPTS}</Worksheet>\n")


def monitors_sheet(title: str, verdicts: list[Verdict]) -> str:
    r = _Rows()
    r.row([_cell("               " + title, MON_TITLE, 7)],
          ' ss:AutoFitHeight="0" ss:Height="40.050000000000004"')
    r.row([_cell(h, HEADER) for h in ("Monitoring Task", "Transaction", "Description", "Selection Period",
                                       "OK", "Not OK", "Evidence", "Recommendation")],
          ' ss:AutoFitHeight="0" ss:Height="27"')
    for m, v in zip(LAYOUT["monitors"], verdicts):
        ok = _cell("X", OK_X) if v.ok is True else _cell("")
        nok = _cell("X", NOT_OK_X) if v.ok is False else _cell("")
        r.row([_cell(m["task"]), _cell(m["tcode"]), _cell(m["description"]), _cell(m["period"]),
               ok, nok, _cell(v.evidence), _cell(v.recommendation)])
    cols = next(s["columns_xml"] for s in LAYOUT["sheets"] if s["name"] == "Monitors")
    return _sheet("Monitors", cols, r.out, 8, r.n)


def write_workbook(title: str, verdicts: list[Verdict], tabs: dict[str, object]) -> bytes:
    """`tabs` maps each layout sheet name to a list[Block] or a TextTab.
    Tabs are emitted in the sample's order; a missing tab is still emitted
    (empty table with the sample's headers) so the structure never drifts."""
    if len(verdicts) != len(LAYOUT["monitors"]):
        raise ValueError(f"need {len(LAYOUT['monitors'])} verdicts, got {len(verdicts)}")
    parts = [HEAD, " " + LAYOUT["styles"] + "\n", monitors_sheet(title, verdicts)]
    for s in LAYOUT["sheets"]:
        if s["name"] == "Monitors":
            continue
        content = tabs.get(s["name"])
        r = _Rows()
        if isinstance(content, TextTab):
            _text(r, content, s)
        else:
            blocks = content or [Block(columns=c, title=s["name"], subtitle="Not collected")
                                 for c in s["tables"][:1]] or [Block(columns=["Info"], title=s["name"])]
            for i, b in enumerate(blocks):
                _block(r, b, first=(i == 0), meta=s, sheet=s["name"])
        parts.append(_sheet(s["name"], s["columns_xml"], r.out, r.width, r.n))
    parts.append("</Workbook>\n")
    return "".join(parts).encode("utf-8")
