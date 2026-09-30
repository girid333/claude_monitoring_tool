"""Fake SAP for the D-051 probe: SOAP RFC at /sap/bc/soap/rfc plus a minimal
WebGUI (login + OK-code screen) on the same host, like a real ICM.

ADVERSARIAL (SKILL §5) — each case is one a real system produces:
  - RFC_READ_TABLE honours NO_DATA (structure only) and faults with
    DATA_BUFFER_EXCEEDED when the requested fields exceed 512 chars
    (SOOS is 30 x 20 chars wide; RFCDES.RFCOPTIONS alone is 500).
  - RZLLITAB has no GROUPTYPE field on this "release", so a WHERE on it
    faults OPTION_NOT_VALID and the probe must report the missing field.
  - TSP01 -> NOT_AUTHORIZED, SNAP_BEG/TPFBA/TPFID/SRT_MMASTER -> TABLE_NOT_AVAILABLE,
    VBHDR exists but is empty (reachable != has data).
  - TFDIR says GET_CPU_ALL is NOT remote-enabled: calling it anyway is a
    probe bug, so every FM call is logged (GET /calls) for the test.
  - GWY_READ_CONNECTIONS is remote-enabled but faults; SWNC faults
    NO_DATA_FOUND; SAPTUNE returns exports only; ENQUEUE_READ returns ONE
    lock as a single <item> and only when ENQ is declared.
  - discovery (TFDIR LIKE) turns up RSLG_*/SLIC_* modules the probe must
    list and never call.
"""
import base64
import json
import sys
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8092
USER, PASSWORD = "MONITOR", "secret"
CALLS: list[str] = []

ENV = ('<?xml version="1.0" encoding="utf-8"?><soap-env:Envelope '
       'xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/"><soap-env:Body>{}'
       '</soap-env:Body></soap-env:Envelope>')


def fault(text):
    return ENV.format(f"<soap-env:Fault><faultcode>SOAP-ENV:Server</faultcode>"
                      f"<faultstring>{text}</faultstring></soap-env:Fault>")


def resp(fm, inner):
    return ENV.format(f'<rfc:{fm}.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">'
                      f"{inner}</rfc:{fm}.Response>")


def items(rows):
    return "".join("<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + "</item>"
                   for r in rows)


TABLES = {
    "TBTCO": ([("JOBNAME", 32), ("JOBCOUNT", 8), ("STATUS", 1), ("SDLUNAME", 12),
               ("STRTDATE", 8), ("STRTTIME", 6), ("ENDDATE", 8), ("ENDTIME", 6)],
              [["SAP_COLLECTOR_FOR_PERFMONITOR", "10150100", "F", "DDIC", "20260929", "101501",
                "20260929", "101509"],
               ["Z_NIGHTLY_LOAD", "02000100", "A", "BATCHUSR", "20260929", "020001",
                "20260929", "020412"]]),
    "T000": ([("MANDT", 3), ("MTEXT", 25), ("CCCATEGORY", 1), ("CCCORACTIV", 1),
              ("CCNOCLIIND", 1)], [["000", "SAP AG", "S", "2", "1"], ["100", "Home S/4", "C", "1", "3"]]),
    "VBHDR": ([("VBKEY", 32), ("VBUSR", 12), ("VBDATE", 14), ("VBRC", 4), ("VBTCODE", 20)], []),
    "RZLLITAB": ([("CLASSNAME", 20), ("APPLSERVER", 40)],
                 [["PUBLIC", "nwrhel9_F4H_00"], ["PUBLIC", "devhost_DEV_00"]]),
    "SOOS": ([(f"FLD{i:02d}", 20) for i in range(30)],
             [[f"v{i}" for i in range(30)]]),
    "RFCDES": ([("RFCDEST", 32), ("RFCTYPE", 1), ("RFCOPTIONS", 500)],
               [["NONE", "3", "H=%%SELF%%"], ["SM_F4HCLNT100_READ", "3", "H=nwrhel9,S=00"]]),
}
FAULT_TABLES = {"TSP01": "NOT_AUTHORIZED", "SNAP_BEG": "TABLE_NOT_AVAILABLE",
                "TPFBA": "TABLE_NOT_AVAILABLE", "TPFID": "TABLE_NOT_AVAILABLE",
                "SRT_MMASTER": "TABLE_NOT_AVAILABLE"}
REMOTE = {"TH_SERVER_LIST", "TH_WPINFO", "TH_USER_LIST", "ENQUEUE_READ",
          "SWNC_COLLECTOR_GET_AGGREGATES", "GET_MEM_ALL", "SAPTUNE_GET_SUMMARY_STATISTIC",
          "GWY_READ_CONNECTIONS", "RFC_READ_TABLE", "RFC_SYSTEM_INFO"}   # NOT GET_CPU_ALL
DISCOVERABLE = {"RSLG": ["RSLG_READ_SYSLOG_REMOTE"], "SLIC": ["SLIC_GET_LICENCES"],
                "SICK": [], "SAPTUNE": ["SAPTUNE_GET_SUMMARY_STATISTIC"]}


def local(tag):
    return tag.rsplit("}", 1)[-1]


def read_table(fn) -> str:
    get = lambda n: next((c for c in fn if local(c.tag) == n), None)  # noqa: E731
    table = (get("QUERY_TABLE").text or "").strip()
    no_data = (get("NO_DATA") is not None and (get("NO_DATA").text or "").strip() == "X")
    wanted = [(it.findtext("FIELDNAME") or "").strip() for it in (get("FIELDS") or [])]
    where = " ".join((it.findtext("TEXT") or "") for it in (get("OPTIONS") or []))
    rowcount = int((get("ROWCOUNT").text or "0").strip() or 0) if get("ROWCOUNT") is not None else 0

    if table == "TFDIR":
        if " IN (" in where:
            rows = [[n, "R"] for n in sorted(REMOTE) if f"'{n}'" in where]
        else:
            rows = [[n, "R"] for k, ns in DISCOVERABLE.items() if k in where for n in ns]
        defs = [("FUNCNAME", 30), ("FMODE", 1)]
    elif table == "TFTIT":
        defs, rows = [("SPRAS", 1), ("FUNCNAME", 30), ("STEXT", 74)], []
    elif table in FAULT_TABLES:
        return fault(FAULT_TABLES[table])
    elif table in TABLES:
        defs, rows = TABLES[table]
    else:
        return fault("TABLE_NOT_AVAILABLE")

    names = [n for n, _ in defs]
    for tok in where.replace("(", " ").split():
        if tok.isupper() and tok.isidentifier() and tok not in ("EQ", "NE", "GE", "LE", "GT",
                                                                  "LT", "AND", "OR", "IN", "LIKE",
                                                                  "NOT") and tok not in names:
            return fault("OPTION_NOT_VALID")
    sel = [d for d in defs if d[0] in wanted] if wanted else defs
    if any(w not in names for w in wanted):
        return fault("FIELD_NOT_VALID")
    if not no_data and sum(ln for _, ln in sel) > 512:
        return fault("DATA_BUFFER_EXCEEDED")
    off, fx = 0, []
    for n, ln in sel:
        fx.append({"FIELDNAME": n, "OFFSET": off, "LENGTH": ln, "TYPE": "C", "FIELDTEXT": n})
        off += ln
    data = [] if no_data else [
        {"WA": "".join(str(r[names.index(n)]).ljust(ln) for n, ln in sel)}
        for r in rows[: rowcount or None]]
    return resp("RFC_READ_TABLE", f"<FIELDS>{items(fx)}</FIELDS><DATA>{items(data)}</DATA>")


def handle_fm(fm, fn, body) -> str:
    declared = lambda t: f"<{t}" in body  # noqa: E731
    if fm == "RFC_SYSTEM_INFO":
        return resp(fm, "<RFCSI_EXPORT><RFCSYSID>F4H</RFCSYSID><RFCSAPRL>758</RFCSAPRL>"
                        "<RFCKERNRL>793</RFCKERNRL><RFCDBSYS>HDB</RFCDBSYS></RFCSI_EXPORT>")
    if fm == "RFC_READ_TABLE":
        return read_table(fn)
    if fm not in REMOTE:
        return fault(f"Function module {fm} is not remote-enabled")
    if fm == "TH_SERVER_LIST":
        rows = [{"NAME": "nwrhel9_F4H_00", "HOST": "nwrhel9", "SERV": "sapdp00"}] if declared("LIST") else []
        return resp(fm, f"<LIST>{items(rows)}</LIST>")
    if fm == "TH_WPINFO":
        return resp(fm, "<WPLIST>" + items([{"WP_TYP": "DIA", "WP_STATUS": "running"},
                                             {"WP_TYP": "BTC", "WP_STATUS": "waiting"}]) + "</WPLIST>")
    if fm == "TH_USER_LIST":
        return resp(fm, "<USRLIST>" + items([{"BNAME": "GIRID", "TCODE": "SM50", "PRIORITY": "0"},
                                              {"BNAME": "SAPSYS", "TCODE": "BGRFC", "PRIORITY": "1"}])
                    + "</USRLIST>")
    if fm == "ENQUEUE_READ":
        rows = [{"GNAME": "VBAK", "GUNAME": "BATCHUSR", "GTDATE": "20260928"}] if declared("ENQ") else []
        return resp(fm, f"<NUMBER>{len(rows)}</NUMBER><ENQ>{items(rows)}</ENQ>")
    if fm == "SWNC_COLLECTOR_GET_AGGREGATES":
        return fault("NO_DATA_FOUND")
    if fm == "SAPTUNE_GET_SUMMARY_STATISTIC":
        return resp(fm, "<SAPMEMORY_HITRATIO>99.1</SAPMEMORY_HITRATIO><SWAPS>0</SWAPS>")
    if fm == "GET_MEM_ALL":
        return resp(fm, "<TF_MEM_ALL>" + items([{"PHYS_MEM": "32768000", "FREE_MEM": "1200000"}])
                    + "</TF_MEM_ALL>")
    if fm == "GWY_READ_CONNECTIONS":
        return fault("FU_NOT_FOUND")
    return fault(f"unhandled {fm}")


LOGIN = """<html><body>
<form id="LOGIN_FORM" method="post" action="/sap/bc/gui/sap/its/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="text" name="sap-client" value="100">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On">
</form></body></html>"""
SESSION = """<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<div id="content"><h1>SAP Easy Access</h1>
<table><tr><td>Status</td><td>OK</td></tr><tr><td>Installation check</td><td>No errors</td></tr>
</table></div></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200, ctype="text/html"):
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/calls"):
            self._send(json.dumps(CALLS), ctype="application/json")
            return
        self._send(SESSION if ("logged" in self.path or "~transaction" in self.path) else LOGIN)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if "/sap/bc/soap/rfc" not in self.path:
            self._send(SESSION)
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic ") or base64.b64decode(auth[6:]).decode() != f"{USER}:{PASSWORD}":
            self._send("unauthorized", 401)
            return
        root = ET.fromstring(body)
        fn = next(c for c in next(e for e in root if local(e.tag) == "Body"))
        fm = local(fn.tag)
        CALLS.append(fm)
        out = handle_fm(fm, fn, body)
        self._send(out, 500 if "<soap-env:Fault>" in out else 200, 'text/xml; charset="utf-8"')

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
