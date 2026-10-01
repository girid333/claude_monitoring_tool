"""Health report data model — the API contract with the dashboard."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ok        SAP passed it and nothing in the evidence contradicts that
# expected  SAP flagged it, but the evidence shows by-design behaviour
# review    worth a look; no clear breakage
# act       something is broken or about to break
# no_data   the check has no evidence tab to analyse
Triage = Literal["ok", "expected", "review", "act", "no_data"]
Tone = Literal["neutral", "good", "warn", "bad"]


class Metric(BaseModel):
    label: str
    value: str
    tone: Tone = "neutral"


class BarItem(BaseModel):
    label: str
    value: float
    tone: Tone = "neutral"
    note: str = ""


class Chart(BaseModel):
    title: str
    unit: str = ""
    items: list[BarItem]


class TableOut(BaseModel):
    title: str = ""
    columns: list[str]
    rows: list[list[str]]
    total: int


class Check(BaseModel):
    id: str
    category: str
    task: str
    tcode: str
    description: str = ""
    period: str = ""
    sap_status: Literal["ok", "not_ok", "unknown"]
    sap_evidence: str = ""
    triage: Triage
    headline: str
    findings: list[str] = Field(default_factory=list)
    recommendation: Optional[str] = None
    metrics: list[Metric] = Field(default_factory=list)
    charts: list[Chart] = Field(default_factory=list)
    sheet: Optional[str] = None
    tables: list[TableOut] = Field(default_factory=list)
    text: Optional[str] = None
    analysis_note: Optional[str] = None   # set when a tab couldn't be analysed


class Summary(BaseModel):
    total: int
    sap_ok: int
    sap_not_ok: int
    ok: int
    expected: int
    review: int
    act: int
    no_data: int


class SystemInfo(BaseModel):
    sid: str = ""
    client: str = ""
    instances: list[str] = Field(default_factory=list)


class HealthReport(BaseModel):
    id: str
    filename: str
    imported_at: str
    report_date: Optional[str] = None       # ISO date the health check ran
    title: str = ""
    system: SystemInfo
    summary: Summary
    checks: list[Check]
    extra_sheets: list[str] = Field(default_factory=list)  # tabs no check points to
    # D-051: "import" (uploaded SAP export) or "sap_pull" (collected over SOAP RFC)
    source: Literal["import", "sap_pull"] = "import"
    has_workbook: bool = False                 # original/generated .XLS kept for export


class ReportListItem(BaseModel):
    id: str
    filename: str
    imported_at: str
    report_date: Optional[str]
    sid: str
    client: str
    summary: Summary
    source: Literal["import", "sap_pull"] = "import"
