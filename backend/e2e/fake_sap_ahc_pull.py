"""Fake SAP system for the D-051 direct pull (SOAP RFC), port 8091 by default.

A whole small system with KNOWN findings, so the test can assert the pulled
workbook says exactly what a real AHC would. ADVERSARIAL (SKILL §5) where
F4H was:
  - SAP's clock is UTC+5:30 (RFCTZONE 19800) and every timestamp is in SAP
    local time. A pull that used the container's UTC clock would miss the
    syslog entries and dumps of the last 5.5 h — exactly what happened live.
  - RFC_READ_TABLE answers only with a DELIMITER (fixed-width offsets were
    unreliable on F4H) and rejects unknown fields / WHERE fields.
  - Namespaced modules must arrive as "_-SDF_-..." — a raw "/" element is a
    "Document Format Error", as on F4H.
  - Syslog has continuation ("p") records after a message; one message ID is
    "Very High" by MONBEW, the rest informational.
  - SXPG_COMMAND_EXECUTE runs the SM69 hdbsql wrapper only for the configured
    command name; any other name is COMMAND_NOT_FOUND.
Every called module is recorded (GET /calls) so the test can prove the pull
stayed read-only. GET /reset clears the log; /hana/off removes the SM69 command.
"""
import base64
import datetime as dt
import json
import re
import sys
import xml.etree.ElementTree as ET
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
USER, PASSWORD = "MONITOR", "secret"
TZ = 19800                                     # +5:30
CALLS: list[str] = []
STATE = {"hana": True, "sick_error": False, "sapctl": True, "port_closed": False}
PERF = {"smon": True, "upd": False, "today": True, "tcdet": True, "work": True,
        # D-052b landscape: 3 app servers, HANA scale-out + HSR, cloud metadata, HANA client network trace
        "multi": False, "hsr_bad": False, "cloud": False, "nettrace": False}      # /perf/... switches; upd off keeps the AHC SM13 check unchanged
JOBS: dict = {}          # jobcount -> {"name", "program", "status"}
SM69 = "ZAHC_HDBSQL"


def sap_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=TZ)


def ago(**kw) -> dt.datetime:
    return sap_now() - dt.timedelta(**kw)


def _cert(cn: str, days: int) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=30))
         .not_valid_after(now + dt.timedelta(days=days)).sign(key, hashes.SHA256()))
    return base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode()


OWN_SYS = _cert("QAS", 4000)
OWN_SSL = _cert("*.example.test", 10)          # expires inside the 42-day horizon
LIST_CA = _cert("Example Root CA", 3000)

ENV = ('<?xml version="1.0" encoding="utf-8"?><soap-env:Envelope '
       'xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/"><soap-env:Body>{}'
       '</soap-env:Body></soap-env:Envelope>')


def fault(t):
    return ENV.format(f"<soap-env:Fault><faultcode>SOAP-ENV:Server</faultcode><faultstring>{t}</faultstring></soap-env:Fault>")


def esc(v):
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def items(rows):
    return "".join("<item>" + "".join(f"<{k}>{esc(v)}</{k}>" for k, v in r.items()) + "</item>" for r in rows)


def resp(fm, inner):
    tag = fm.replace("/", "_-")
    return ENV.format(f'<rfc:{tag}.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">{inner}</rfc:{tag}.Response>')


def d(t: dt.datetime) -> str:
    return t.strftime("%Y%m%d")


def tm(t: dt.datetime) -> str:
    return t.strftime("%H%M%S")


def tables() -> dict:
    n = sap_now()
    y = n - dt.timedelta(days=1)
    return {
        "TSL1T": (["SPRAS", "AREA", "SUBID", "TXT"],
                  [["E", "AB", "0", "Run-time error &a occurred"], ["E", "Q0", "2", "Stops work process &a (PID = &b)"],
                   ["E", "R4", "8", "Soft cancel request"], ["E", "E0", "A", "&a"]]),
        "TSL1D": (["AREA", "SUBID", "MONBEW"], [["AB", "0", "RA"], ["Q0", "2", "WH"], ["R4", "8", "WH"], ["E0", "A", "WH"]]),
        "SNAP_BEG": (["DATUM", "UZEIT", "AHOST", "UNAME", "MANDT", "MODNO", "SEQNO", "XHOLD", "FLIST"],
                     [[d(ago(hours=1)), tm(ago(hours=1)), "qas_QAS_00", "BATCHUSR", "100", "0000000004", "000", "",
                       "FC020CONVT_NO_NUMBER        AP008ZREPORT1TD032ABCDEF0123456789ABCDEF0123456789"],
                      [d(ago(days=3)), "010000", "qas_QAS_00", "OLDUSER", "100", "0000000001", "000", "",
                       "FC010OLD_DUMP  "]]),
        "VBHDR": (["VBKEY", "VBMANDT", "VBUSR", "VBDATE", "VBTCODE", "VBRC", "VBSTATE", "VBNAME"],
                  [["K1", "100", "ANNA", d(ago(days=5)), "VA01", "0", "", ""],
                   ["K2", "100", "BOB", d(ago(days=5)), "VA02", "12", "", ""]] if PERF["upd"] else []),
        "SWNCMONIINDEX": (["COMPONENT", "ASSIGNDSYS", "PERIODTYPE", "PERIODSTRT"],
                          [[comp, "QAS", "D", day_] for day_ in workload_index_days()
                           for comp in swnc_components(day_)]
                          + [["TOTAL", "QAS", "W", d(ago(days=7))], ["prd_PRD_00", "PRD", "D", d(ago(days=60))]]),
        "DD02L": (["TABNAME", "AS4LOCAL", "TABCLASS"], [["/SDF/SMON_HEADER", "A", "TRANSP"]] if PERF["smon"] else []),
        "/SDF/SMON_HEADER": (["GUID", "DATUM", "TIME", "SERVER", "ACT_WPS", "ACT_DIA", "USR_TOTAL", "SYS_TOTAL",
                              "IDLE_TOTAL", "FREE_MEM_PERC", "DIAQ", "UPDQ", "ENQQ", "USERS", "SESSIONS", "SM12_CNT", "NETRTT"],
                             [["G"] + r[:2] + r[2:] for r in smon_rows()] if PERF["smon"] else []),
        "TSP01": (["RQIDENT", "RQCLIENT", "RQOWNER", "RQCRETIME", "RQERROR", "RQPJSERR", "RQPJHERR", "RQTITLE"],
                  [["4711", "100", "GIRI", ago(hours=2).strftime("%Y%m%d%H%M%S") + "00", "", "00001", "00000", "Bad print"],
                   ["4712", "100", "GIRI", ago(hours=3).strftime("%Y%m%d%H%M%S") + "00", "", "00000", "00000", "Fine"]]),
        "TBTCO": (["JOBNAME", "SDLUNAME", "STRTDATE", "STRTTIME", "ENDDATE", "ENDTIME", "SDLSTRTDT", "SDLSTRTTM", "STATUS"],
                  [["Z_NIGHTLY", "BATCHUSR", d(ago(hours=5)), tm(ago(hours=5)), d(ago(hours=5)), tm(ago(hours=5, seconds=-90)),
                    d(ago(hours=5)), tm(ago(hours=5)), "A"],
                   ["Z_LATE", "BATCHUSR", d(ago(hours=6)), tm(ago(hours=6)), d(ago(hours=6)), tm(ago(hours=6)),
                    d(ago(hours=7)), tm(ago(hours=7)), "F"],
                   ["Z_OK", "BATCHUSR", d(ago(hours=8)), tm(ago(hours=8)), d(ago(hours=8)), tm(ago(hours=8)),
                    d(ago(hours=8)), tm(ago(hours=8)), "F"]]),
        "SRT_MMASTER": (["MESSAGE_STATE", "CREATED_AT", "OB_IF_NAME", "IB_IF_NAME", "LOCATION", "ADAPTER_TYPE",
                         "CREATED_BY", "ERROR_COUNTER"], []),
        "SOES": (["SNDDAT", "SNDTIM", "SNDART", "SCDATE", "SCTIME", "DELIVER", "NOT_DELI", "STATUS", "MSGID", "MSGTY", "MSGV1"],
                 [[d(ago(hours=1)), tm(ago(hours=1)), "INT", d(ago(hours=1)), tm(ago(hours=1)), "X", "", "73", "", "", "a@x.test"]]),
        "RZLLITAB": (["CLASSNAME", "APPLSERVER", "GROUPTYPE", "IP_ADDRESS", "RESP_TIME", "USERS"],
                     [["PUBLIC", "qas_QAS_00", " ", "10.0.0.5", "12", "7"], ["PUBLIC", "old_DEV_00", " ", "", "", ""],
                      ["parallel_generators", "qas_QAS_00", "S", "", "", ""]]),
        "T000": (["MANDT", "MTEXT", "ORT01", "MWAER", "CCCATEGORY", "CHANGEDATE"],
                 [["000", "SAP AG", "Walldorf", "EUR", "S", "20250101"], ["100", "Quality", "Pune", "INR", "T", d(y)]]),
        "TPFBA": (["BANAME", "SYSNAME", "TYPE", "TXT"], [["DUMMY", "QAS", "T", "Self-Configured Operation Mode"]]),
        "RFCDES": (["RFCDEST", "RFCTYPE", "RFCOPTIONS"], [
            ["QASCLNT100", "3", "H=qas,S=00,M=100,U=RFCUSER,L=E,v=%_PWD,"],
            ["PRDCLNT100", "3", "H=prd,S=00,M=100,U=RFCUSER,L=E,v=%_PWD,"],     # logon fails
            ["DEVCLNT100", "3", "H=dev,S=00,M=100,U=RFCUSER,L=E,"],             # host down
            ["ZOMBIE_TCP", "T", "H=legacy,N=prog,"],                            # never answers
            ["Z_SKIPPED", "T", "H=x,"],
            ["NONE", "I", ""]]),
        "RFCDOC": (["RFCLANG", "RFCDEST", "RFCDOC1"], [["E", "QASCLNT100", "QAS client 100"]]),
        "RFCATTRIB": (["RFCDEST", "CUNAME", "CUDATE", "MUNAME", "MUDATE"],
                      [["QASCLNT100", "BASIS", "20240101", "GIRI", "20260915"]]),
    }


def _match(row: dict, where: str) -> bool:
    """Tiny WHERE evaluator: EQ/NE/GE/IN, AND/OR with parentheses, enough for the collectors."""
    if not where.strip():
        return True
    expr = where
    for f in sorted(row, key=len, reverse=True):
        expr = re.sub(rf"\b{f}\b", f"_r[{f!r}]", expr)
    expr = re.sub(r"\bEQ\b", "==", expr)
    expr = re.sub(r"\bNE\b", "!=", expr)
    expr = re.sub(r"\bGE\b", ">=", expr)
    expr = re.sub(r"\bIN\b", " in ", expr)
    expr = re.sub(r"\bAND\b", " and ", expr)
    expr = re.sub(r"\bOR\b", " or ", expr)
    try:
        return bool(eval(expr, {"__builtins__": {}}, {"_r": row}))  # noqa: S307 — test fixture
    except Exception:  # noqa: BLE001
        raise ValueError("OPTION_NOT_VALID")


def read_table(fn) -> str:
    g = lambda n: next((c for c in fn if c.tag.rsplit("}", 1)[-1] == n), None)  # noqa: E731
    table = (g("QUERY_TABLE").text or "").strip()
    delim = (g("DELIMITER").text or "") if g("DELIMITER") is not None else ""
    no_data = g("NO_DATA") is not None and (g("NO_DATA").text or "").strip() == "X"
    want = [(i.findtext("FIELDNAME") or "").strip() for i in (g("FIELDS") or [])]
    where = " ".join((i.findtext("TEXT") or "") for i in (g("OPTIONS") or []))
    t = tables().get(table)
    if not t:
        return fault("TABLE_NOT_AVAILABLE")
    names, rows = t
    if not no_data and delim != "|":
        return fault("DATA_BUFFER_EXCEEDED")       # this fake only speaks delimiter mode
    if any(w not in names for w in want):
        return fault("FIELD_NOT_VALID")
    sel = want or names
    fx = "".join(f"<item><FIELDNAME>{n}</FIELDNAME><OFFSET>0</OFFSET><LENGTH>30</LENGTH><TYPE>C</TYPE></item>" for n in sel)
    if no_data:
        return resp("RFC_READ_TABLE", f"<FIELDS>{fx}</FIELDS><DATA/>")
    out = []
    for r in rows:
        rd = dict(zip(names, r))
        try:
            if not _match(rd, where):
                continue
        except ValueError:
            return fault("OPTION_NOT_VALID")
        out.append({"WA": "|".join(rd[n] for n in sel)})
    return resp("RFC_READ_TABLE", f"<FIELDS>{fx}</FIELDS><DATA>{items(out)}</DATA>")


def handle(fm, fn, body) -> str:
    n = sap_now()
    if fm == "RFC_SYSTEM_INFO":
        return resp(fm, f"<RFCSI_EXPORT><RFCSYSID>QAS</RFCSYSID><RFCSAPRL>758</RFCSAPRL><RFCDBSYS>HDB</RFCDBSYS>"
                        f"<RFCTZONE>{TZ}</RFCTZONE><RFCHOST>qas</RFCHOST><RFCDEST>qas_QAS_00</RFCDEST>"
                        f"<RFCDBHOST>hdb</RFCDBHOST></RFCSI_EXPORT>")
    if fm == "RFC_READ_TABLE":
        return read_table(fn)
    if fm == "TH_SERVER_LIST":
        srv = [{"NAME": "qas_QAS_00", "HOST": "qas", "SERV": "sapdp00", "MSGTYPES": "uw==", "STATE": "AQ=="}]
        if PERF["multi"]:
            srv += [{"NAME": "qas_QAS_01", "HOST": "qasapp1", "SERV": "sapdp01", "MSGTYPES": "AQ==", "STATE": "AQ=="},
                    {"NAME": "qas_QAS_02", "HOST": "qasapp2", "SERV": "sapdp02", "MSGTYPES": "AQ==", "STATE": "AQ=="}]
        return resp(fm, "<LIST>" + items(srv) + "</LIST>")
    if fm == "TH_USER_LIST":
        return resp(fm, "<USRLIST>" + items([{"MANDT": "100", "BNAME": "GIRI", "TERM": "laptop", "TCODE": "SM50",
                                              "ZEIT": tm(n), "EXTMODI": "2", "TYPE": "4", "RFC_TYPE": ""}]) + "</USRLIST>")
    if fm == "TH_WPINFO" and "<SRVNAME>qas_QAS_02</SRVNAME>" in body:     # busy instance
        return resp(fm, "<WPLIST>" + items([{"WP_INDEX": str(i), "WP_TYP": "DIA", "WP_PID": str(300 + i),
                                             "WP_STATUS": "Running" if i < 3 else "Waiting"} for i in range(4)]) + "</WPLIST>")
    if fm == "TH_WPINFO":
        return resp(fm, "<WPLIST>" + items([{"WP_INDEX": str(i), "WP_TYP": "DIA" if i < 3 else "BTC", "WP_PID": str(100 + i),
                                             "WP_STATUS": "Waiting"} for i in range(5)]) + "</WPLIST>")
    if fm == "TH_GET_PARAMETER":
        return resp(fm, "<PARAMETER_VALUE>/usr/sap/QAS/D00/work</PARAMETER_VALUE><RC>0</RC>")
    if fm == "RZL_READ_DIR_LOCAL":
        return resp(fm, "<FILE_TBL>" + items([{"NAME": ".", "SIZE": "0000004096"}, {"NAME": "dev_w0", "SIZE": "0000001200"},
                                              {"NAME": "stderr0", "SIZE": "0000000000"}]) + "</FILE_TBL>")
    if fm == "ENQUEUE_READ":
        old, new = ago(hours=30), ago(minutes=10)
        rows = [{"GCLIENT": "100", "GUNAME": "BATCHUSR", "GTDATE": old.strftime("%Y-%m-%d"), "GTTIME": old.strftime("%H:%M:%S"),
                 "GMODE": "E", "GNAME": "VBAK", "GARG": "1000000123", "GUSE": "0", "GUSEVB": "1"},
                {"GCLIENT": "000", "GUNAME": "SAP_WSRT", "GTDATE": new.strftime("%Y-%m-%d"), "GTTIME": new.strftime("%H:%M:%S"),
                 "GMODE": "E", "GNAME": "SPBGM_FUNCTION_INDICATOR", "GARG": "X", "GUSE": "0", "GUSEVB": "1"}]
        return resp(fm, f"<NUMBER>{len(rows)}</NUMBER><ENQ>{items(rows)}</ENQ>")
    if fm == "RSLG_READ_SLG":
        frm = re.search(r"<IM_FILTER_DATETIME_FROM>(\d+)<", body).group(1)
        to = re.search(r"<IM_FILTER_DATETIME_TO>(\d+)<", body).group(1)
        ent = []
        for when, typ, data in [(ago(hours=1), "mAB0", "&aCONVT_NO_NUMBER"), (ago(hours=1), "pE0A", "&aReport ZREPORT1"),
                                (ago(hours=2), "mQ02", "19  810623"), (ago(hours=3), "mR48", "x"),
                                (ago(hours=30), "mAB0", "&aOLD_DUMP")]:
            s = when.strftime("%Y%m%d%H%M%S")
            if frm <= s <= to:
                ent.append({"SLGTYPE": typ, "SLGDATTIM": s + "00", "SLGPROC": "0123400004DI", "SLGUSER": "BATCHUSR",
                            "SLGMAND": "100", "SLGDATA": data})
        return resp(fm, f"<EX_POS>0</EX_POS><T_SYSLOG_ENTRIES>{items(ent)}</T_SYSLOG_ENTRIES>")
    if fm == "SAPTUNE_GET_SUMMARY_STATISTIC":
        return resp(fm, "<EX_GLOB_MEM_USAGE><TOTAL>1000</TOTAL><USED>500</USED><PEAK>600</PEAK></EX_GLOB_MEM_USAGE>"
                        "<EXTENDED_MEMORY_USAGE><USED>10</USED><TOTAL>100</TOTAL></EXTENDED_MEMORY_USAGE>"
                        "<SAP_CURSOR_CACHE_TOTAL><ID_HITR>95</ID_HITR><STMT_HITR>70</STMT_HITR></SAP_CURSOR_CACHE_TOTAL>")
    if fm == "SAPTUNE_BUFFER_NAMES":
        return resp(fm, "<BUFFERNAMES>" + items([{"BUFFERNAME": "PXA"}, {"BUFFERNAME": "CUA"}, {"BUFFERNAME": "CTLGA"}]) + "</BUFFERNAMES>")
    if fm == "SAPTUNE_BUFFER_STATISTICS":
        # CTLGA (catalog buffer) runs a low hit ratio on quiet systems and is not rated (F4H: 78 %)
        hit = "4500" if "PXA" in body else ("2500" if "CTLGA" in body else "4995")
        return resp(fm, f"<BUFFER_STATISTIC><ALLOC_SIZE>5000</ALLOC_SIZE><AVAIL_SIZE>2500</AVAIL_SIZE><MAX_OBJCTS>100</MAX_OBJCTS>"
                        f"<ACT_OBJCTS>40</ACT_OBJCTS><REQUEST>5000</REQUEST><HIT>{hit}</HIT><SWAP>0</SWAP>"
                        f"<DB_ACCESS>12</DB_ACCESS></BUFFER_STATISTIC>")
    if fm == "GET_CPU_ALL":
        return resp(fm, "<TF_CPU_ALL>" + items([{"NBR_CPU": "8", "LOAD1_AVG": "37", "LOAD5_AVG": "30", "LOAD15_AVG": "20",
                                                 "USR_TOTAL": "10", "SYS_TOTAL": "5", "IDLE_TOTAL": "85", "WAIT_TRUE": "0",
                                                 "INT_SEC": "1500", "CS_SEC": "4000"}]) + "</TF_CPU_ALL>")
    if fm == "GET_MEM_ALL":
        return resp(fm, "<TF_MEM_ALL>" + items([{"PHYS_MEM": "32602616", "FREE_MEM": "3458292", "SWAP_CONF": "41943036",
                                                 "SWAP_SIZE": "41943036", "SWAP_FREE": "41943036"}]) + "</TF_MEM_ALL>")
    if fm == "SWNC_COLLECTOR_GET_AGGREGATES":
        day = re.search(r"<PERIODSTRT>([\d-]+)</PERIODSTRT>", body).group(1).replace("-", "")
        comp = re.search(r"<COMPONENT>(.*?)</COMPONENT>", body).group(1)
        if day not in workload_index_days() or comp not in swnc_components(day):
            return fault("NO_DATA_FOUND")
        part = None if comp == "TOTAL" else swnc_components(day).index(comp)
        usec = "<FACTOR>1</FACTOR>" in body              # FACTOR=1: microseconds instead of ms
        return resp(fm, swnc_day(day, re.search(r"<TCDET\b", body) is not None and PERF["tcdet"], part,
                                 len(swnc_components(day)), 1000 if usec else 1))
    if fm == "GWY_READ_CONNECTIONS":
        if "<GWSERV>sapgw00</GWSERV>" not in body:
            return fault("GWY_GET_GWSERV_FAILED")
        return resp(fm, "<CONNECTIONS/>")
    if fm == "SSFR_GET_ALL_STRUST_IDENTITIES":
        return resp(fm, "<ET_STRUST_IDENTITIES>" + items([
            {"PSE_CONTEXT": "PROG", "PSE_APPLIC": "<SYST>", "PSE_DESCRIPT": "System"},
            {"PSE_CONTEXT": "SSLS", "PSE_APPLIC": "DFAULT", "PSE_DESCRIPT": "SSL server Standard"},
            {"PSE_CONTEXT": "SMIM", "PSE_APPLIC": "DFAULT", "PSE_DESCRIPT": "SMIME Standard"}]) + "</ET_STRUST_IDENTITIES>")
    if fm == "SSFR_GET_OWNCERTIFICATE":
        if "<PSE_CONTEXT>SMIM</PSE_CONTEXT>" in body:
            return fault("SSF_PSE_NOT_FOUND")
        cert = OWN_SSL if "<PSE_CONTEXT>SSLS</PSE_CONTEXT>" in body else OWN_SYS
        return resp(fm, f"<EV_CERTIFICATE>{cert}</EV_CERTIFICATE>")
    if fm == "SSFR_GET_CERTIFICATELIST":
        lst = [LIST_CA] if "<PSE_CONTEXT>SSLS</PSE_CONTEXT>" in body else []
        return resp(fm, "<ET_CERTIFICATELIST>" + "".join(f"<item>{c}</item>" for c in lst) + "</ET_CERTIFICATELIST>")
    if fm == "_-SDF_-SLIC_READ_LICENSES":
        return resp("/SDF/SLIC_READ_LICENSES", "<COUNT>1</COUNT><CUSTKEY>J1</CUSTKEY><LICENSES>" + items([
            {"SYSTEMID": "QAS", "HWKEY": "J1", "SWPRODUCT": "NetWeaver_HDB", "END_DATE": "99991231", "VALIDITY": "Valid"}])
            + "</LICENSES>")
    if fm == "DEST_CHECK_CONNECTION":
        name = re.search(r"<NAME>(.*?)</NAME>", body).group(1)
        if name == "NONE":
            return fault("CHECK_NOT_POSSIBLE")
        if name == "ZOMBIE_TCP":
            time.sleep(8)                       # longer than the test's AHC_RFC_TIMEOUT
        res, err, auth, aerr = {
            "QASCLNT100": ("", "", "", ""),
            "PRDCLNT100": ("", "", "E", "Name or password is incorrect (repeat logon)"),
            "DEVCLNT100": ("E", "Connection to host dev, service sapgw00 failed: NIEHOST_UNKNOWN", "N", ""),
        }.get(name, ("", "", "N", ""))
        return resp(fm, f"<AUTHORIZATION_ERROR_TEXT>{aerr}</AUTHORIZATION_ERROR_TEXT>"
                        f"<AUTHORIZATION_TEST_RESULT>{auth}</AUTHORIZATION_TEST_RESULT>"
                        f"<CONNECTION_ERROR_TEXT>{err}</CONNECTION_ERROR_TEXT><CONNECTION_PROPERTIES/>"
                        f"<CONNECTION_TEST_RESULT>{res}</CONNECTION_TEST_RESULT>")
    if fm.startswith("BAPI_XMI_") or fm.startswith("BAPI_XBP_"):
        return xbp(fm, body)
    if fm == "SXPG_COMMAND_EXECUTE" and "<COMMANDNAME>ZAHC_CLOUDMETA</COMMANDNAME>" in body:
        if not PERF["cloud"]:
            return fault("COMMAND_NOT_FOUND")
        host = (re.search(r"<TARGETSYSTEM>(.*?)</TARGETSYSTEM>", body) or [None, ""])[1]
        meta = CLOUD.get(host)
        out = ([f"{k}={v}" for k, v in meta.items()] if meta else ["provider=none", f"error=no cloud metadata service on {host}"])
        return resp(fm, "<STATUS>O</STATUS><EXITCODE>0</EXITCODE><EXEC_PROTOCOL>"
                    + items([{"MESSAGE": m} for m in out]) + "</EXEC_PROTOCOL>")
    if fm == "SXPG_COMMAND_EXECUTE":
        if STATE["sapctl"] and "<COMMANDNAME>ZAHC_SAPCONTROL</COMMANDNAME>" in body:
            add = re.search(r"<ADDITIONAL_PARAMETERS>(.*?)</ADDITIONAL_PARAMETERS>", body, re.S).group(1)
            out, code = sapcontrol(add)
            return resp(fm, f"<STATUS>{'O' if code == 0 else 'E'}</STATUS><EXITCODE>{code}</EXITCODE><EXEC_PROTOCOL>"
                        + items([{"MESSAGE": m} for m in out]) + "</EXEC_PROTOCOL>")
        if not STATE["hana"] or f"<COMMANDNAME>{SM69}</COMMANDNAME>" not in body:
            return fault("COMMAND_NOT_FOUND")
        sql = re.search(r"<ADDITIONAL_PARAMETERS>(.*?)</ADDITIONAL_PARAMETERS>", body, re.S).group(1)
        sql = sql.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
        if len(sql) > 230:                  # F4H: SM69 parameters + SQL share 255 chars
            return fault("TOO_MANY_PARAMETERS")
        out, code = hdbsql(sql)
        lines = [line[:128] for line in out]   # SXPG cuts protocol lines, it does not wrap
        return resp(fm, f"<STATUS>{'O' if code == 0 else 'E'}</STATUS><EXITCODE>{code}</EXITCODE><EXEC_PROTOCOL>"
                    + items([{"LENGTH": str(len(m)), "MESSAGE": m} for m in lines]) + "</EXEC_PROTOCOL>")
    return fault(f"unhandled {fm}")


LONG_ACTION = ("Set the log mode to \"normal\". Investigate why the backup is missing, schedule a complete data backup in the backup "
               "console and verify that backups are written to the configured destination before next run.")


def hana_views() -> dict:
    """View name -> (columns, rows, default order). Row order below IS the
    ORDER BY the collectors use (the fake does not sort)."""
    return {**_landscape_views(), **_hana_views()}


def _landscape_views() -> dict:
    """D-052b: scale-out (hdb master, hdb2 worker, hdb3 standby) + HSR to site SEC, or a single host."""
    multi = PERF["multi"]
    land = (["HOST", "HOST_ACTIVE", "HOST_STATUS", "INDEXSERVER_ACTUAL_ROLE", "NAMESERVER_ACTUAL_ROLE"],
            [["hdb", "YES", "OK", "MASTER", "MASTER"], ["hdb2", "YES", "OK", "SLAVE", "SLAVE"],
             ["hdb3", "NO", "IGNORE", "STANDBY", "SLAVE"]] if multi else [["hdb", "YES", "OK", "MASTER", "MASTER"]])
    info = (["HOST", "KEY", "VALUE"],
            [[h, k, v] for h in (("hdb", "hdb2", "hdb3") if multi else ("hdb",))
             for k, v in (("hw_manufacturer", "Amazon EC2"), ("hw_model", "r6i.8xlarge"))])
    st = "ERROR" if PERF["hsr_bad"] else "ACTIVE"
    sites = (["SITE_NAME", "SECONDARY_SITE_NAME", "REPLICATION_MODE", "REPLICATION_STATUS", "OPERATION_MODE", "TIER"],
             [["PRIM", "SEC", "SYNC", st, "logreplay", "2"]] if multi else [])
    now = sap_now()
    ts = lambda x: f"{x:%Y-%m-%d %H:%M:%S}.000000000"  # noqa: E731
    lag = 420 if PERF["hsr_bad"] else 2
    svc = (["HOST", "PORT", "SECONDARY_HOST", "SECONDARY_SITE_NAME", "REPLICATION_MODE", "REPLICATION_STATUS",
            "SECONDARY_ACTIVE_STATUS", "LAST_LOG_POSITION_TIME", "SHIPPED_LOG_POSITION_TIME", "BACKLOG_SIZE",
            "BACKLOG_TIME", "SECONDARY_RECONNECT_COUNT", "SECONDARY_FAILOVER_COUNT", "REPLICATION_STATUS_DETAILS"],
           [[h, "30003", h.replace("hdb", "hdbs"), "SEC", "SYNC", st if h == "hdb2" else "ACTIVE",
             "NO" if PERF["hsr_bad"] and h == "hdb2" else "YES", ts(now), ts(now - dt.timedelta(seconds=lag)),
             str(5 * 1024 ** 2), str(lag * 1000000), "1", "0",
             "Connection refused by secondary site SEC" if PERF["hsr_bad"] and h == "hdb2" else ""]
            for h in ("hdb", "hdb2")] if multi else [])
    return {"M_SYSTEM_REPLICATION_TAKEOVER_HISTORY": (["TAKEOVER_START_TIME"], []),
            "M_LANDSCAPE_HOST_CONFIGURATION": land, "M_HOST_INFORMATION": info,
            "M_SERVICE_REPLICATION": svc, "M_SYSTEM_REPLICATION": sites}


def _hana_views() -> dict:
    t = lambda d: d.strftime("%Y-%m-%d %H:%M:%S.%f000")  # noqa: E731  hdbsql timestamp format
    b = ago(hours=4)
    cat = (["ENTRY_ID", "SYS_START_TIME", "SYS_END_TIME", "STATE_NAME", "MESSAGE", "ENTRY_TYPE_NAME"],
           [["9", t(b), t(b + dt.timedelta(minutes=5)), "successful", "<ok>", "complete data backup"]])
    files = (["ENTRY_ID", "SOURCE_ID", "SOURCE_TYPE_NAME", "HOST", "SERVICE_TYPE_NAME", "DESTINATION_PATH", "BACKUP_SIZE"],
             [["9", "0", "topology", "hdb", "nameserver", "/hana/backup/data/COMPLETE_DATA_BACKUP_databackup_0_1", "3610"],
              ["9", "2", "volume", "hdb", "indexserver", "/hana/backup/data/COMPLETE_DATA_BACKUP_databackup_2_1", "279810408448"]])
    alerts = (["ALERT_TIMESTAMP", "ALERT_ID", "ALERT_DETAILS", "ALERT_USERACTION", "ALERT_RATING"],
              [[t(ago(hours=1)), "36", "The last data backup is 3 days old and the long description of this alert "
                "keeps going well past one hundred characters so it must be read in chunks.", LONG_ACTION, "4"],
               [t(ago(hours=1)), "62", "The password for database user X will expire on 2099-01-01",
                "Change the password", "3"]])
    avail = (["EVENT_TIME", "GUID", "IS_ORIGIN", "TRACE_HOST", "EVENT_NAME", "EVENT_DETAIL", "ERROR_MESSAGE",
              "SYSTEM_ACTIVE", "SYSTEM_STATUS", "HOST", "HOST_ACTIVE", "HOST_STATUS", "DATABASE_NAME",
              "DATABASE_ACTIVE", "SERVICE_NAME", "PORT", "VOLUME_ID", "SERVICE_ACTIVE", "HOST_CONFIG_ROLES",
              "HOST_ACTUAL_ROLES", "STORAGE_CONFIG_PARTITION", "STORAGE_ACTUAL_PARTITION", "TARGET_HOST",
              "TARGET_HOST_CONFIG_ROLES", "TARGET_HOST_ACTUAL_ROLES", "TARGET_STORAGE_CONFIG_PARTITION",
              "TARGET_STORAGE_ACTUAL_PARTITION", "SITE_ID"],
             [[t(ago(days=10 - i)), f"5b3c1e2a-0000-4000-8000-00000000000{i}", "TRUE", "hdb",
               "SERVICE_STARTED" if i % 2 else "SERVICE_STOPPED", "", "", "YES", "OK", "hdb", "YES", "OK", "QAS",
               None, "indexserver", "30040", "2", "YES", "WORKER", "WORKER", "1", "1", None, None, None, "0", "0", "0"]
              for i in (1, 0)])         # ORDER BY EVENT_TIME DESC
    blocked = (["HOST", "PORT", "BLOCKED_TRANSACTION_ID", "BLOCKED_UPDATE_TRANSACTION_ID", "BLOCKED_CONNECTION_ID",
                "LOCK_OWNER_TRANSACTION_ID", "LOCK_OWNER_UPDATE_TRANSACTION_ID", "LOCK_OWNER_CONNECTION_ID",
                "BLOCKED_TIME", "WAITING_RECORD_ID", "WAITING_SCHEMA_NAME", "WAITING_TABLE_NAME",
                "WAITING_OBJECT_NAME", "WAITING_OBJECT_TYPE", "WAITING_PART_ID", "LOCK_TYPE", "LOCK_MODE"], [])
    join = (["C." + c for c in cat[0]] + ["F." + c for c in files[0]],
            [cr + fr for cr in cat[1] for fr in files[1] if cr[0] == fr[0]])
    lk = lambda hw, prod: "".join(f"{len(v):04d}{v}" for v in ("LK", "QAS", hw, prod))  # noqa: E731
    lv = lambda start, end: "".join(f"{len(v):04d}{v}" for v in (  # noqa: E731
        "2147483647", "P", "MIIBOwYJKoZIhvcNAQcCoIIBLDCCASgCAQExCzAJBgUrDgMCGgUAMAsGCSqGSIb3DQEHAT" * 6,
        "0021277862", start, end, "000000000801035261"))
    likey = (["LIKEY", "VALUE"], sorted([
        ["0002LC0003QAS0002J10013NetWeaver_HDB", "0008202610010024Qm5fVk5ldDNVpgMl6QUOig=="],
        [lk("J1", "Maintenance_HDB"), lv("20251011", "20260112")],          # expired
        [lk("J1", "NetWeaver_HDB"), lv("20251011", "99991231")],
        [lk("H2", "NetWeaver_HDB"), lv("20240101", "99991231")],            # other (inactive) hardware key
    ]))
    if "LIKEY LIKE '0002LK%'" in _LAST_SQL[0]:
        likey = (likey[0], [r for r in likey[1] if r[0].startswith("0002LK")])
    return {"FROM SAPHANADB.SAPLIKEY": likey, "M_BACKUP_CATALOG C JOIN": join, "STATISTICS_CURRENT_ALERTS": alerts,
            "M_SYSTEM_AVAILABILITY": avail, "M_BLOCKED_TRANSACTIONS": blocked}


def _q(v):
    if v is None:
        return "?"
    return v if re.fullmatch(r"-?\d+", v) else '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LAST_SQL = [""]
LOBS = {"VALUE"}            # hdbsql prints LOB columns cut to 32 characters


def sapcontrol(add: str) -> tuple[list[str], int]:
    """sapcontrol as F4H printed it: blank, timestamp, function, OK/FAIL, body."""
    nr = (re.search(r"-nr (\d\d)", add) or [None, "00"])[1]
    fn = re.findall(r"-function (\w+)", add)[-1]
    args = add.split(f"-function {fn}", 1)[1].split()
    head = ["", "01.10.2026 14:30:18", fn]
    if fn == "GetSystemInstanceList":
        return head + ["OK", "hostname, instanceNr, httpPort, httpsPort, startPriority, features, dispstatus",
                       "qas, 1, 50113, 50114, 1, MESSAGESERVER|ENQUE, GREEN",
                       "qas, 0, 50013, 50014, 3, ABAP|GATEWAY|ICMAN|IGS, GREEN"], 0
    if fn == "GetAccessPointList":
        aps = ([("3901", "MS", "msg_server", "Yes"), ("3601", "MS", "msg_server", "Yes"),
                ("8101", "HTTP", "msg_server", "Yes"), ("44401", "HTTPS", "msg_server",
                                                         "No" if STATE["port_closed"] else "Yes")]
               if nr == "01" else [("3300", "RFC", "gwrd", "Yes"), ("80", "HTTP", "icm", "Yes")])
        return head + ["OK", "address, port, protocol, processname, active"] + \
            [f"{ip}, {p}, {pr}, {pn}, {a}" for p, pr, pn, a in aps for ip in ("127.0.0.1", "10.0.0.5")], 0
    if fn == "ParameterValue":
        params = {"ms/server_port_0": "PROT=HTTP,PORT=8101",
                  "ms/server_port_1": "PROT=HTTPS,PORT=44401,TIMEOUT=30,PROCTIMEOUT=120"} if nr == "01" else {}
        if args and args[0] in params:
            return head + ["OK", "", params[args[0]]], 0
        return head + ["FAIL: Invalid parameter", "External program terminated with exit code 1"], 1
    return head + ["FAIL: function not handled by fake"], 1


def workload_index_days() -> list[str]:
    """SAP keeps 40 days of daily aggregates in this fake (newest = today)."""
    # /perf/work/off: collector stopped two days ago (no aggregates for today/yesterday)
    return [(sap_now() - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(0 if PERF["work"] else 2, 40)]


def swnc_components(day: str) -> list[str]:
    """Like F4H, the last 20 days only exist per instance (no TOTAL built); older days have TOTAL."""
    recent = day >= (sap_now() - dt.timedelta(days=19)).strftime("%Y%m%d")
    if not recent:
        return ["TOTAL"]
    return ["qas_QAS_00", "qas_QAS_01", "qas_QAS_02"] if PERF["multi"] else ["qas_QAS_00", "qas_QAS_01"]


def _tt(code: str) -> str:
    return base64.b64encode(bytes.fromhex(code)).decode()   # RAW(1) over SOAP: "01" → "AQ=="


def swnc_day(day: str, with_tcdet: bool, part=None, parts: int = 2, scale: int = 1) -> str:
    """SWNC_COLLECTOR_GET_AGGREGATES as F4H returns it: times in MILLISECONDS summed over steps,
    base64 task types, TIME 'HH--HH', ENTRY_ID in TIMES. part 0/1 = one instance's share."""
    slow = day == (sap_now() - dt.timedelta(days=3)).strftime("%Y%m%d")      # one bad day in history
    f = 1.8 if slow else 1.0

    def cnt(n: float) -> int:
        n = int(n)
        return n if part is None else (n // parts + (n % parts if part == 0 else 0))

    # DB calls per step differ per instance, so the DB time per call ranks them (01 lowest, 02 highest)
    calls_per_step = {None: 1000, 0: 1000, 1: 1400, 2: 600}[part]

    def row(code: str, n: float, **ms) -> dict:
        c = cnt(n)
        return {"TASKTYPE": _tt(code), "COUNT": str(c), "PHYCALLS": str(c * calls_per_step),
                **{k: f"{v * c * scale:.0f}" for k, v in ms.items()}}

    tasks = [
        row("01", 12000, RESPTI=850 * f, CPUTI=200, DBTI=300 * f, QUEUETI=20 * f * f, ROLLWAITTI=30, GUITIME=60,
            LOADGENTI=10, PROCTI=450, LOCKTI=0),
        row("04", 800, RESPTI=42000, CPUTI=9000, READDIRTI=5000, READSEQTI=20000, CHNGTI=1000, QUEUETI=0),
        row("FE", 0, RESPTI=0),                                              # empty line: must be skipped
        row("FE", 5000, RESPTI=120, CPUTI=40, DBTI=50),
        row("02", 900, RESPTI=90, CPUTI=30, DBTI=45),
    ]
    times = []
    for h in range(24):
        busy = 1.0 if 8 <= h <= 18 else 0.15
        n = round(1000 * busy)
        peak = h == 10                              # dialog queue at the morning peak
        resp, wait = (1600, 400) if peak else (800, 15)
        slot = f"{h:02d}--{(h + 1) % 24:02d}"
        times.append({**row("01", n * 0.6, RESPTI=resp * f, CPUTI=200, READSEQTI=300 * f, QUEUETI=wait),
                      "TIME": slot, "ENTRY_ID": "VA01".ljust(72) + "T"})
        times.append({**row("01", n - int(n * 0.6), RESPTI=resp * f, CPUTI=200, READSEQTI=300 * f, QUEUETI=wait),
                      "TIME": slot, "ENTRY_ID": "SE16".ljust(72) + "T"})
        times.append({**row("04", 33, RESPTI=40000), "TIME": slot, "ENTRY_ID": "ZREPORT_SLOW".ljust(72) + "R"})
    tcdet = [
        {**row("01", 3000, RESPTI=1400, CPUTI=300, DBTI=700), "ENTRY_ID": "VA01", "ACCOUNT": "ANNA"},
        {**row("01", 1000, RESPTI=1400, CPUTI=300, DBTI=700), "ENTRY_ID": "VA01", "ACCOUNT": "BOB"},  # 2nd user
        {**row("04", 10, RESPTI=900000, CPUTI=200000, READSEQTI=600000),
         "ENTRY_ID": "ZREPORT_SLOW                            BATCHJOB_A", "ACCOUNT": "BATCH"},
        {**row("01", 500, RESPTI=300, CPUTI=80, DBTI=150), "ENTRY_ID": "SE16", "ACCOUNT": "ANNA"},
        {**row("01", 1, RESPTI=1), "ENTRY_ID": "", "ACCOUNT": "X"},                    # blank: skipped
    ]
    out = "<TASKTYPE>" + items(tasks) + "</TASKTYPE><TIMES>" + items(times) + "</TIMES>"
    return out + ("<TCDET>" + items(tcdet) + "</TCDET>" if with_tcdet else "<TCDET/>")


# server, active dialog WPs, NETRTT µs (SMON "Network RTT (E. Acc.DB)"), idle %
SMON_SERVERS = [("qas_QAS_00", 4, 450, "60"), ("qas_QAS_01", 2, 210, "60"), ("qas_QAS_02", 3, 900, "20")]


def smon_rows() -> list[list[str]]:
    """Today: one snapshot per minute for the last 3 h on two servers; plus 3 days ago."""
    rows = []
    for back in (range(180, -1, -1) if PERF["today"] else []):
        t = ago(minutes=back)
        for srv, base, rtt, idle in SMON_SERVERS[: 3 if PERF["multi"] else 2]:
            q = "6" if back == 30 and srv == "qas_QAS_00" else "0"
            rows.append([d(t), tm(t), srv, str(base + 2), str(base), "30", "10", idle, "22", q, "0", "0",
                         "12", "20", "40", str(rtt + back % 7)])
    old = ago(days=3).replace(hour=10, minute=0, second=0)
    for k in range(0, 60, 5):
        t = old + dt.timedelta(minutes=k)
        rows.append([d(t), tm(t), "qas_QAS_00", "9", "8", "70", "20", "10", "8", "15", "2", "0", "40", "60", "300",
                     "450"])
    return rows


RET = "<RETURN><TYPE></TYPE><MESSAGE></MESSAGE></RETURN>"

# ZAHC_CLOUDMETA per host: DB + qas + qasapp1 in one AZ and placement group, qasapp2 in another AZ
CLOUD = {
    "hdb": {"provider": "AWS", "region": "eu-central-1", "zone": "eu-central-1a", "vm_size": "r6i.8xlarge",
            "placement_group": "sap-qas-pg", "instance_id": "i-0db"},
    "qas": {"provider": "AWS", "region": "eu-central-1", "zone": "eu-central-1a", "vm_size": "m6i.2xlarge",
            "placement_group": "sap-qas-pg", "instance_id": "i-0a0"},
    "qasapp1": {"provider": "AWS", "region": "eu-central-1", "zone": "eu-central-1a", "vm_size": "m6i.2xlarge",
                "placement_group": "sap-qas-pg", "instance_id": "i-0a1"},
    "hdb2": {"provider": "AWS", "region": "eu-central-1", "zone": "eu-central-1a", "vm_size": "r6i.8xlarge",
             "placement_group": "sap-qas-pg", "instance_id": "i-0db2"},
    "qasapp2": {"provider": "AWS", "region": "eu-central-1", "zone": "eu-central-1b", "vm_size": "m6i.2xlarge",
                "instance_id": "i-0a2"},
}


def xbp(fm: str, body: str) -> str:
    """SAP's external job interface, enough for run-report-and-read-spool."""
    g = lambda tag: (re.search(f"<{tag}>(.*?)</{tag}>", body, re.S) or [None, ""])[1]  # noqa: E731
    if fm in ("BAPI_XMI_LOGON", "BAPI_XMI_LOGOFF"):
        return resp(fm, RET + "<SESSIONID>AC1C91A0</SESSIONID>")
    if fm == "BAPI_XBP_JOB_OPEN":
        jc = f"{13563700 + len(JOBS):08d}"
        JOBS[jc] = {"name": g("JOBNAME"), "program": "", "status": "P"}
        return resp(fm, RET + f"<JOBCOUNT>{jc}</JOBCOUNT>")
    job = JOBS.get(g("JOBCOUNT"))
    if job is None or job["name"] != g("JOBNAME"):
        return resp(fm, "<RETURN><TYPE>E</TYPE><MESSAGE>Job does not exist</MESSAGE></RETURN>")
    if fm == "BAPI_XBP_JOB_ADD_ABAP_STEP":
        job["program"] = g("ABAP_PROGRAM_NAME")
        return resp(fm, RET + "<STEP_NUMBER>1</STEP_NUMBER>")
    if fm == "BAPI_XBP_JOB_CLOSE":
        job["status"] = "S"
        return resp(fm, RET)
    if fm == "BAPI_XBP_JOB_START_IMMEDIATELY":
        job["status"] = "F" if job["program"] == "RSICC000" else "A"
        return resp(fm, RET)
    if fm == "BAPI_XBP_JOB_STATUS_GET":
        return resp(fm, RET + f"<STATUS>{job['status']}</STATUS>")
    if fm == "BAPI_XBP_JOB_SPOOLLIST_READ":
        box = "-" * 74
        msg = ("Database release 2.00.059 is not supported with kernel 793" if STATE["sick_error"]
               else "no errors reported")
        lines = [box, "|SAP System Check" + " " * 56 + "|", "|" + "-" * 72 + "|", f"|{msg:<72}|", box]
        return resp(fm, RET + "<SPOOL_LIST>" + items([{"LINE": line} for line in lines]) + "</SPOOL_LIST>")
    if fm == "BAPI_XBP_JOB_DELETE":
        del JOBS[g("JOBCOUNT")]
        return resp(fm, RET)
    return fault(f"unhandled {fm}")


def hdbsql(sql: str) -> tuple[list[str], int]:
    """hdbsql -x -j -F '|' : |HEAD|HEAD| then |"text"|123|?| rows."""
    _LAST_SQL[0] = sql
    if sql.startswith('"'):
        return ['* 257: sql syntax error: incorrect syntax near "\"": line 1 col 1 (at pos 1) SQLSTATE: HY000'], 3
    fmt = lambda vals: "|" + "|".join(vals) + "|"  # noqa: E731
    if "DUMMY" in sql:
        return ["|PING|", "|1|"], 0
    if "M_DISKS" in sql:
        return ["|USAGE_TYPE|USED|TOTAL|", '|"DATA"|107374182400|214748364800|', '|"LOG"|10737418240|107374182400|'], 0
    if "SYS.TABLES" in sql and "SAPLIKEY" in sql:
        return ["|SCHEMA_NAME|", '|"SAPHANADB"|'], 0
    if "SAPLIKEY" in sql and "FROM SAPHANADB.SAPLIKEY" not in sql:
        return ["* 259: invalid table name:  Could not find table/view SAPLIKEY in schema AHCMON"], 3
    perf = hana_perf(sql)
    if perf is not None:
        return perf, 0
    if "M_HOST_RESOURCE_UTILIZATION" in sql:
        return ["|HOST|USED_PHYSICAL_MEMORY|FREE_PHYSICAL_MEMORY|", '|"hdb"|53687091200|53687091200|'], 0
    m = re.match(r"SELECT (?:TOP (\d+) )?(.*?) FROM (.*)$", sql, re.S)
    view = next((v for k, v in hana_views().items() if k in sql), None)
    if not m or not view:
        return ["* 259: invalid table name"], 3
    top, cols_sql = m.group(1), m.group(2)
    names, rows = view
    rows = rows[: int(top)] if top else rows
    sub = re.fullmatch(r"SUBSTR\(TO_NVARCHAR\(([\w.]+)\), (\d+), (\d+)\) AS P", cols_sql.strip())
    if sub:
        col, k, n = sub.group(1), int(sub.group(2)), int(sub.group(3))
        i = _index(names, col)
        return ["|P|"] + [fmt([_q((r[i] or "")[k - 1:k - 1 + n])]) for r in rows], 0
    want = [c.strip() for c in cols_sql.split(",")]
    idx = [_index(names, c) for c in want]
    if None in idx:
        return [f"* 260: invalid column name: {want[idx.index(None)]}"], 3
    lob = lambda c, v: v[:32] if c.split(".")[-1] in LOBS and v else v  # noqa: E731
    return [fmt([c.split(".")[-1] for c in want])] + [fmt([_q(lob(names[i], r[i])) for i in idx]) for r in rows], 0


def hana_perf(sql: str):
    """Performance-dashboard HANA reads (D-052), as hdbsql prints them."""
    n = sap_now()
    if "VIEW_COLUMNS" in sql and "M_LOAD_HISTORY_HOST" in sql:
        return ["|COLUMN_NAME|"] + [f'|"{c}"|' for c in ("TIME", "HOST", "CPU", "MEMORY_USED",
                                                           "MEMORY_ALLOCATION_LIMIT", "DISK_USED", "DISK_SIZE")]
    if "M_LOAD_HISTORY_HOST" in sql and "MIN(TIME)" in sql:
        return ["|D|", f'|"{(n - dt.timedelta(days=30)).date()}"|']
    if "M_LOAD_HISTORY_HOST" in sql and "GROUP BY HOST, HOUR(TIME)" in sql:
        day = re.search(r"TIME >= '([\d-]+)'", sql).group(1)
        head = ["|O|H|C|CM|"] if "AVG(CPU)" in sql else ["|O|H|M|MM|L|"]
        if day < str((n - dt.timedelta(days=30)).date()):
            return head
        hours = range(24) if day < str(n.date()) else range(n.hour + 1)
        out = []
        for host in (("hdb", "hdb2") if PERF["multi"] else ("hdb",)):
            for h in hours:
                if "AVG(CPU)" in sql:
                    out.append(f'|"{host}"|{h}|{30 + (40 if h == 10 else 0)}.5|{45 + (47 if h == 10 else 0)}.0|')
                else:   # hdb2 is the fuller host (30 of 32 GB at 10:00) in scale-out mode
                    mm = (30 if h == 10 else 23) if host == "hdb2" else (28 if h == 10 else 22)
                    out.append(f'|"{host}"|{h}|{20 * GB_}|{mm * GB_}|{32 * GB_}|')
        return head + out
    if "M_LOAD_HISTORY_HOST" in sql and "ORDER BY TIME DESC" in sql:
        cols = re.match(r"SELECT (?:TOP \d+ )?(.*?) FROM", sql).group(1).split(", ")
        out = ["|" + "|".join(cols) + "|"]
        for host, cpu, used in ((("hdb", "37", 26), ("hdb2", "64", 29)) if PERF["multi"] else (("hdb", "37", 26),)):
            vals = {"HOST": f'"{host}"', "TIME": f'"{n:%Y-%m-%d %H:%M:%S}.000000000"', "CPU": cpu,
                    "MEMORY_USED": str(used * GB_), "MEMORY_ALLOCATION_LIMIT": str(32 * GB_), "DISK_USED": str(100 * GB_),
                    "DISK_SIZE": str(200 * GB_)}
            out.append("|" + "|".join(vals[c] for c in cols) + "|")
        return out
    if "FROM M_SQL_CLIENT_NETWORK_IO" in sql:
        rows = [("qas", 520, 4000), ("qasapp1", 180, 3000), ("qasapp2", 1250, 2000)] if PERF["nettrace"] else []
        return ["|H|N|D|"] + [f'|"{h}"|{c}|{d}|' for h, d, c in rows[: 3 if PERF["multi"] else 1]]
    if "FROM M_SYSTEM_REPLICATION_TAKEOVER_HISTORY" in sql:
        day = re.search(r"TAKEOVER_START_TIME >= '([\d-]+)'", sql).group(1)
        t = (n - dt.timedelta(days=3)).replace(hour=3, minute=12, second=0)
        cols = re.match(r"SELECT (?:TOP \d+ )?(.*?) FROM", sql).group(1).split(", ")
        vals = {"TAKEOVER_START_TIME": f'"{t:%Y-%m-%d %H:%M:%S}.000000000"',
                "TAKEOVER_END_TIME": f'"{t + dt.timedelta(minutes=2):%Y-%m-%d %H:%M:%S}.000000000"',
                "SITE_NAME": '"PRIM"', "SOURCE_SITE_NAME": '"SEC"', "TAKEOVER_TYPE": '"TAKEOVER"',
                "REPLICATION_MODE": '"SYNC"'}
        hit = PERF["multi"] and day == str(t.date())
        return ["|" + "|".join(cols) + "|"] + (["|" + "|".join(vals[c] for c in cols) + "|"] if hit else [])
    if "FROM M_CONNECTIONS" in sql:
        return ["|N|R|", "|142|6|"]
    if "COUNT(*) AS N FROM M_BLOCKED_TRANSACTIONS" in sql:
        return ["|N|", "|0|"]
    if "STATISTICS_CURRENT_ALERTS" in sql and "GROUP BY ALERT_RATING" in sql:
        return ["|R|N|", "|4|1|", "|3|2|"]
    if "FROM M_CS_UNLOADS" in sql:
        return ["|N|", "|3|"]
    if "FROM M_SAVEPOINTS" in sql:
        return ["|A|M|", "|850000|4200000|"]
    if "FROM M_SQL_PLAN_CACHE" in sql and "AS S" in sql:
        strip = "REPLACE(" in sql
        fix = lambda s: (s.replace('"', "") if strip else s)[:90].replace('"', '\\"')  # noqa: E731
        return ["|S|"] + [f'|"{fix(s)}"|' for s in (
            'SELECT * FROM "VBAK" WHERE "MANDT" = ? AND "ERDAT" > ? -- \"hot\" statement from VA01 reporting run',
            'UPDATE "NRIV" SET "NRLEVEL" = ? WHERE "CLIENT" = ? AND "OBJECT" = ?')]
    if "FROM M_SQL_PLAN_CACHE" in sql:
        return ["|STATEMENT_HASH|EXECUTION_COUNT|TOTAL_EXECUTION_TIME|AVG_EXECUTION_TIME|",
                '|"a1b2c3d4e5f60718293a4b5c6d7e8f90"|1200|900000000|750000|',
                '|"ffeeddccbbaa99887766554433221100"|55000|300000000|5454|']
    return None


GB_ = 1024 ** 3


def _index(names, col):
    if col in names:
        return names.index(col)
    bare = [n.split(".")[-1] for n in names]
    return bare.index(col) if "." not in col and bare.count(col) == 1 else None


class H(BaseHTTPRequestHandler):
    def _send(self, body, status=200, ctype="text/xml; charset=\"utf-8\""):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/calls"):
            return self._send(json.dumps(CALLS), ctype="application/json")
        if self.path.startswith("/reset"):
            CALLS.clear()
            STATE["hana"] = True
            STATE["sick_error"] = False
            STATE["sapctl"] = True
            STATE["port_closed"] = False
            PERF.update(smon=True, upd=False, today=True, tcdet=True, work=True, multi=False, hsr_bad=False, cloud=False,
                        nettrace=False)
            return self._send("ok", ctype="text/plain")
        if self.path.startswith("/jobs"):
            return self._send(json.dumps(JOBS), ctype="application/json")
        if self.path.startswith("/perf/"):
            k, v = self.path.split("/")[2:4]
            PERF[k] = v == "on"
            return self._send("ok", ctype="text/plain")
        if self.path.startswith("/sapctl/off"):
            STATE["sapctl"] = False
            return self._send("ok", ctype="text/plain")
        if self.path.startswith("/port/closed"):
            STATE["port_closed"] = True
            return self._send("ok", ctype="text/plain")
        if self.path.startswith("/sick/error"):
            STATE["sick_error"] = True
            return self._send("ok", ctype="text/plain")
        if self.path.startswith("/hana/off"):
            STATE["hana"] = False
            return self._send("ok", ctype="text/plain")
        self._send("<html>webgui</html>", ctype="text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic ") or base64.b64decode(auth[6:]).decode() != f"{USER}:{PASSWORD}":
            return self._send("unauthorized", 401, "text/html")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return self._send(fault("Document Format Error"), 500)
        fn = next(c for c in next(e for e in root if e.tag.endswith("Body")))
        fm = fn.tag.rsplit("}", 1)[-1]
        CALLS.append(fm)
        out = handle(fm, fn, body)
        self._send(out, 500 if "<soap-env:Fault>" in out else 200)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True          # a hanging RFC test must not block other calls
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
