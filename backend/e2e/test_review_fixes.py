"""Regression tests for the bugs found in the 2026-10-03 full review (before the D-052 commit).
Pure unit tests with small stubs — no fake server needed."""
import datetime as dt
import io
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from engine.rfc.soap_client import RfcSoapError, parse_response  # noqa: E402

from ahc.sap import Sap  # noqa: E402


class StubSap(Sap):
    """Answers RFC calls from a dict of fm -> callable(params) and records them."""

    def __init__(self, answers):
        self.answers, self.calls, self.log = answers, [], []

    def call(self, fm, _timeout_s=None, **kw):
        self.calls.append(fm)
        self.log.append((fm, kw))
        a = self.answers.get(fm)
        if a is None:
            raise RfcSoapError(f"{fm}: unexpected", kind="fault", function=fm)
        return a(kw) if callable(a) else a


def test_sap_now_adds_dst():
    s = StubSap({})
    utc = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert round((s.sap_now({"RFCTZONE": "3600", "RFCDAYST": "X"}) - utc).total_seconds() / 3600) == 2
    assert round((s.sap_now({"RFCTZONE": "19800", "RFCDAYST": ""}) - utc).total_seconds() / 60) == 330


def test_read_table_pipe_inside_a_value():
    fields = [{"FIELDNAME": "JOBNAME", "LENGTH": "000032", "TYPE": "C"},
              {"FIELDNAME": "STATUS", "LENGTH": "000001", "TYPE": "C"}]
    s = StubSap({"RFC_READ_TABLE": lambda kw: {"FIELDS": fields} if kw.get("NO_DATA") else
                 {"FIELDS": fields, "DATA": [{"WA": "Z_LOAD|DAILY|A"}]}})
    assert s.read("TBTCO", ["JOBNAME", "STATUS"]) == [{"JOBNAME": "Z_LOAD|DAILY", "STATUS": "A"}]


def test_soap_fault_without_children_is_an_rfc_error():
    xml = (b'<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/"><soap-env:Body>'
           b'<soap-env:Fault>Internal error</soap-env:Fault></soap-env:Body></soap-env:Envelope>')
    with pytest.raises(RfcSoapError) as e:
        parse_response(xml, "X")
    assert "Internal error" in e.value.message


# ── AHC collectors ──────────────────────────────────────────────────────
def ctx(sap, now=dt.datetime(2026, 10, 3, 9, 0)):
    from ahc.collect import Ctx
    return Ctx(sap=sap, sid="QAS", client="100", now=now, servers=[{"name": "qas_QAS_00", "host": "qas", "active": True}])


def test_al08_session_from_yesterday_is_dated_yesterday():
    from ahc.collect import c_al08
    blocks, _ = c_al08(ctx(StubSap({})), [{"MANDT": "100", "BNAME": "DEV", "ZEIT": "223000", "EXTMODI": "1"},
                                          {"MANDT": "100", "BNAME": "OPS", "ZEIT": "083000", "EXTMODI": "1"}])
    times = [r[5] for r in blocks[0].rows]
    assert times == ["02.10.2026 22:30:00", "03.10.2026 08:30:00"]


def test_st06_without_cpu_data_is_not_ok():
    from ahc.collect import c_st06
    s = StubSap({"GET_CPU_ALL": {"TF_CPU_ALL": []}, "GET_MEM_ALL": {"TF_MEM_ALL": []}})
    _, v = c_st06(ctx(s))
    assert v.ok is None and "Not collected" in v.evidence


def test_sm21_empty_type_does_not_drop_the_tab():
    from ahc.collect import c_sm21
    entries = [{"SLGTYPE": "", "SLGDATTIM": "20261003083000", "SLGPROC": "", "SLGUSER": ""},
               {"SLGTYPE": "AAB0", "SLGDATTIM": "20261003084000", "SLGPROC": "DIA  00001", "SLGUSER": "X"}]
    s = StubSap({"RSLG_READ_SLG": {"T_SYSLOG_ENTRIES": entries},
                 "RFC_READ_TABLE": lambda kw: {"FIELDS": [], "DATA": []}})
    blocks, v = c_sm21(ctx(s))
    assert v is not None


def test_srt_moni_created_at_is_utc():
    from ahc.collect import c_srt_moni
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=5, minutes=30)   # IST
    created_utc = now - dt.timedelta(hours=5, minutes=30) - dt.timedelta(hours=20)   # 20 h ago, in UTC
    fields = ["MESSAGE_STATE", "CREATED_AT", "OB_IF_NAME", "IB_IF_NAME", "LOCATION", "ADAPTER_TYPE", "CREATED_BY",
              "ERROR_COUNTER"]
    row = ["ERR", created_utc.strftime("%Y%m%d%H%M%S") + ".0000000", "IF_X", "", "", "SOAP", "U", "001"]
    s = StubSap({"RFC_READ_TABLE": lambda kw: {"FIELDS": [{"FIELDNAME": f} for f in fields]} if kw.get("NO_DATA") else
                 {"FIELDS": [{"FIELDNAME": f} for f in fields], "DATA": [{"WA": "|".join(row)}]}})
    blocks, v = c_srt_moni(ctx(s, now))
    assert len(blocks[0].rows) == 1 and v.ok is False                # 20 h old: inside the 24 h window
    assert blocks[0].rows[0][2][:2] == f"{created_utc + dt.timedelta(hours=5, minutes=30):%H}"


def test_cert_subject_with_oid_or_st_is_read():
    from health.analyze import _CERT
    assert _CERT.match("1.2.840.113549.1.9.1=ops@x.test,CN=qas  12.10.2026")
    assert _CERT.match("ST=Karnataka,CN=qas  12.10.2026")
    assert _CERT.match("CN=qas, O=X  12.10.2026")


def test_xlsx_export_keeps_formula_like_text_as_text():
    from openpyxl import Workbook, load_workbook
    from perf.export import _sheet
    wb = Workbook()
    _sheet(wb, "T", "=title", ["a"], [['=HYPERLINK("http://evil","x")']])
    b = io.BytesIO()
    wb.save(b)
    ws = load_workbook(io.BytesIO(b.getvalue()))["T"]
    assert ws["A4"].data_type == "s" and ws["A1"].data_type == "s"


# ── performance ─────────────────────────────────────────────────────────
def test_smon_live_window_reads_yesterdays_tail():
    from perf.collect import smon_read
    seen = {}

    def rt(kw):
        if kw.get("NO_DATA"):
            return {"FIELDS": [{"FIELDNAME": "DATUM"}, {"FIELDNAME": "TIME"}]}
        seen["where"] = " ".join(o["TEXT"] for o in kw["OPTIONS"])
        return {"FIELDS": [{"FIELDNAME": "DATUM"}, {"FIELDNAME": "TIME"}], "DATA": []}
    smon_read(StubSap({"RFC_READ_TABLE": rt}), "/SDF/SMON_HEADER", ["DATUM", "TIME"], dt.date(2026, 10, 3),
              dt.datetime(2026, 10, 2, 23, 10))
    assert "DATUM EQ '20261002' AND TIME GE '231000'" in seen["where"] and "DATUM EQ '20261003'" in seen["where"]


def test_top_sql_text_matched_by_hash_and_newlines_removed():
    from perf.collect import top_sql

    class H:
        def table(self, cols, source, top=None, lobs=()):
            return [{"STATEMENT_HASH": "aaaaaaaaaaaa11", "EXECUTION_COUNT": "1", "TOTAL_EXECUTION_TIME": "3000000",
                     "AVG_EXECUTION_TIME": "1"},
                    {"STATEMENT_HASH": "bbbbbbbbbbbb22", "EXECUTION_COUNT": "1", "TOTAL_EXECUTION_TIME": "1000000",
                     "AVG_EXECUTION_TIME": "1"}]

        def query(self, sql):
            from ahc.hana import MAX_SQL, sxpg_safe
            assert len(sxpg_safe(sql)) <= MAX_SQL and "CHAR(10)" in sql
            return [{"K": "bbbbbbbbbbbb", "S": "UPDATE B"}, {"K": "aaaaaaaaaaaa", "S": "SELECT A"}]
    out = top_sql(H())
    assert [o["statement"] for o in out] == ["SELECT A", "UPDATE B"]


def test_db12_chunk_query_fits_sxpg():
    from ahc.hana import MAX_SQL, sxpg_safe
    src = ("FROM M_BACKUP_CATALOG C JOIN M_BACKUP_CATALOG_FILES F ON C.ENTRY_ID=F.ENTRY_ID "
           "WHERE C.ENTRY_TYPE_NAME LIKE 'complete d%' ORDER BY C.ENTRY_ID DESC,SOURCE_ID")
    assert len(sxpg_safe(f"SELECT TOP 10 SUBSTR(TO_NVARCHAR(F.DESTINATION_PATH),1901,100) P {src}")) <= MAX_SQL
    import inspect

    import ahc.collect as ac
    assert src.split("WHERE")[1] in inspect.getsource(ac.c_db12).replace('"\n                      "', "")


def test_scheduler_failure_deletes_the_half_built_job():
    from perf import smon_sched
    jobs = []

    def open_(kw):
        jobs.append("J1")
        return {"JOBCOUNT": "J1", "RETURN": {}}

    def delete(kw):
        jobs.remove(kw["JOBCOUNT"])
        return {"RETURN": {}}
    s = StubSap({"BAPI_XBP_JOB_OPEN": open_, "BAPI_XBP_JOB_ADD_ABAP_STEP": {"RETURN": {}},
                 "BAPI_XBP_JOB_CLOSE": {"RETURN": {"TYPE": "E", "MESSAGE": "No authorization to release"}},
                 "BAPI_XBP_JOB_DELETE": delete})
    with pytest.raises(RfcSoapError):
        smon_sched._job(s, lambda fm, **kw: smon_sched._check(fm, s.call(fm, **kw)), "SMON",
                        [("/SDF/SMON_START", []), ("/SDF/SMON_REORG", [])])
    assert jobs == [] and "BAPI_XBP_JOB_DELETE" in s.calls


def test_schedule_twice_at_once_is_refused():
    from perf import smon_sched
    lock = smon_sched._LOCKS["QAS-test"]
    lock.acquire()
    try:
        with pytest.raises(smon_sched.JobError) as e:
            smon_sched.schedule(StubSap({}), dt.datetime.now(), "U", lock_key="QAS-test")
        assert "already in progress" in e.value.message
    finally:
        lock.release()


def test_export_survives_bad_rating():
    from perf import export
    v = {"mode": "live", "sid": "QAS", "kpis": [], "overall": {"rating": "bogus"}}
    assert export.to_pdf(v)[:5] == b"%PDF-"
