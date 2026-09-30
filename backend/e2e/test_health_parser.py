"""Health dashboard: workbook reader + analysers against the adversarial
synthetic fixture. Set HEALTH_REAL_AHC=/path/to/real.XLS to also run the
real-export regression (kept out of the repo — it's customer data)."""

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))


import io
import os
import sys
from pathlib import Path

import pytest

from health.analyze import analyze, num, parse_dt
from health.workbook import WorkbookError, extract_tables, read_workbook

FIX = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIX))
from make_ahc_fixture import build  # noqa: E402


@pytest.fixture(scope="module")
def data() -> bytes:
    return build().encode("utf-8")


@pytest.fixture(scope="module")
def rep(data):
    return analyze(read_workbook(data), "ahc_synthetic.xls")


def by(rep, tcode, sheet=None):
    return next(c for c in rep.checks if c.tcode == tcode and (sheet is None or c.sheet == sheet))


# ── reader ────────────────────────────────────────────────────────────────

def test_nul_char_refs_are_stripped_not_fatal(data):
    sh = read_workbook(data)
    assert "RZ12" in sh
    assert any("NONE" == c for r in sh["RZ12"].rows for c in r)


def test_merge_across_and_index_keep_columns_aligned(data):
    sh = read_workbook(data)
    rz = [r for r in sh["RZ12"].rows if r and r[0] == "NONE"][0]
    assert rz[3] == "x"                                  # ss:Index=4 honoured
    assert sh["Monitors"].rows[0][0].startswith("Technical Daily")
    assert len(sh["Monitors"].rows[0]) == 8              # MergeAcross=7 expanded


def test_blank_rows_between_data_rows_do_not_end_table(data):
    st06 = read_workbook(data)["ST06"].tables[0]
    assert len(st06.rows) == 5


def test_multi_table_sheet_split(data):
    tables = read_workbook(data)["SMLG"].tables
    assert [t.columns[0] for t in tables] == ["Logon Group", "Application Server"]


def test_doctype_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><Workbook/>'
    with pytest.raises(WorkbookError, match="DOCTYPE"):
        read_workbook(evil)


def test_binary_xls_gets_actionable_message():
    with pytest.raises(WorkbookError, match="XML Spreadsheet 2003"):
        read_workbook(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)


def test_garbage_rejected():
    with pytest.raises(WorkbookError):
        read_workbook(b"hello world")


def test_xlsx_gives_same_checks(data, rep):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for name, sh in read_workbook(data).items():
        ws = wb.create_sheet(name)
        for r in sh.rows:
            ws.append(r if r else [None])
    buf = io.BytesIO()
    wb.save(buf)
    rep2 = analyze(read_workbook(buf.getvalue()), "x.xlsx")
    assert [(c.tcode, c.triage) for c in rep2.checks] == [(c.tcode, c.triage) for c in rep.checks]


def test_number_and_date_parsing():
    assert num("1.366", european=True) == 1366
    assert num("0,16", european=True) == 0.16
    assert num("99.96") == 99.96
    assert parse_dt("23/09/2026 --11:56:05").hour == 11
    assert parse_dt("will expire on 2026-10-06 at 02:19:09").day == 6


# ── analysers ─────────────────────────────────────────────────────────────

def test_report_metadata(rep):
    assert rep.system.sid == "QAS" and rep.system.client == "100"
    assert rep.report_date == "2026-03-15"
    assert rep.extra_sheets == ["RZ12"]
    s = rep.summary
    assert s.total == 12 and s.sap_ok + s.sap_not_ok == 12
    assert s.ok + s.expected + s.review + s.act + s.no_data == 12


def test_sm21_names_job_and_user(rep):
    c = by(rep, "SM21")
    assert c.triage == "act"
    assert "EBI" in c.headline
    f = c.findings[0]
    assert "R_JR_BTCJOBS_GENERATOR" in f and "SAP_SYSTEM" in f
    assert any("R48" in x for x in c.findings)


def test_sm12_separates_stale_from_by_design(rep):
    c = by(rep, "SM12")
    assert c.triage == "act"                      # VBAK held 34 h
    assert "VBAK" in c.findings[0]
    assert any("by design" in x for x in c.findings)
    assert not any("EKKO" in x for x in c.findings)   # 30 min old: not stale


def test_smlg_foreign_instance(rep):
    c = by(rep, "SMLG")
    assert c.triage == "act" and "DEV" in c.headline and "PUBLIC" in c.headline


def test_cert_expired_own_first(rep):
    c = by(rep, "Certificate Info")
    assert c.triage == "act"
    assert "expired" in c.findings[0] and "own certificate" in c.findings[0]
    assert "OWN certificate" in c.recommendation
    assert c.tables and c.tables[0].columns[0] == "Subject"


def test_slicense_invalid_is_act(rep):
    assert by(rep, "SLICENSE").triage == "act"


def test_db12_failed_backup(rep):
    c = by(rep, "DB12")
    assert c.triage == "act" and "failed" in c.headline


def test_db02_high_alert_act_and_log_expected(rep):
    assert by(rep, "DB02", "DB02_Alerts").triage == "act"
    log = by(rep, "DB02", "DB02_Log")
    assert log.triage == "expected" and "stable" in log.headline


def test_sm04_bgrfc_only_is_expected(rep):
    assert by(rep, "SM04").triage == "expected"


def test_st06_european_numbers(rep):
    c = by(rep, "ST06")
    assert "88%" in c.headline and c.triage == "review"


def test_missing_tab_keeps_sap_verdict(rep):
    c = by(rep, "ZZ99")
    assert c.triage == "review" and c.analysis_note and c.sheet is None


def test_analyser_crash_never_fails_import(data, monkeypatch):
    import health.analyze as A
    def boom(*a, **k):
        raise RuntimeError("synthetic failure")
    monkeypatch.setitem(A.ANALYSERS, "SM21", boom)
    rep = A.analyze(read_workbook(data), "x")
    c = next(c for c in rep.checks if c.tcode == "SM21")
    assert "could not analyse" in c.analysis_note
    assert c.triage == "review"                   # SAP said Not OK


def test_no_monitors_tab_still_analyses():
    xml = build().replace('ss:Name="Monitors"', 'ss:Name="Summary"').replace(
        "Monitoring Task", "Task").replace(">Transaction<", ">Tx<")
    rep = analyze(read_workbook(xml.encode()), "x")
    assert any(c.sheet == "SM21" and c.triage == "act" for c in rep.checks)


@pytest.mark.skipif(not os.environ.get("HEALTH_REAL_AHC"), reason="real export not provided")
def test_real_export_regression():
    rep = analyze(read_workbook(Path(os.environ["HEALTH_REAL_AHC"]).read_bytes()), "real")
    s = rep.summary
    assert s.total == 33 and s.sap_not_ok == 8
    assert not [c for c in rep.checks if c.analysis_note]
    lock = next(c for c in rep.checks if c.tcode == "SM12")
    assert lock.triage == "expected"               # 7 locks, all daemons
    assert next(c for c in rep.checks if c.tcode == "SLICENSE").triage == "expected"
    assert next(c for c in rep.checks if c.tcode == "SMLG").triage == "act"
