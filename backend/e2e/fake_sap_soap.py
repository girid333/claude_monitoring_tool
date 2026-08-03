"""Fake SAP ICM speaking the /sap/bc/soap/rfc dialect (D-045).

Deliberately adversarial in the ways a real system is:
  - demands Basic auth, 401s without it (proves we actually send creds)
  - namespaced response XML exactly as SAP emits it (proves the parser
    doesn't depend on convenient namespace-free XML)
  - returns a single-row table as ONE <item> and a multi-row table as
    several (the shape that trips naive parsers into dropping rows)
  - one FM returns a genuine SOAP Fault (proves errors surface loudly
    instead of looking like "no data found")
  - one path 404s, simulating the SICF service being inactive — the
    single most likely real-world setup failure
"""
import base64
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

USER, PASSWORD = "MONITOR", "secret"

SYSTEM_INFO = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:RFC_SYSTEM_INFO.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<RFCSI_EXPORT>
<RFCPROTO>011</RFCPROTO>
<RFCSYSID>F4H</RFCSYSID>
<RFCHOST>sapf4h01</RFCHOST>
<RFCDBSYS>HDB</RFCDBSYS>
<RFCDBHOST>hanadb01</RFCDBHOST>
<RFCSAPRL>757</RFCSAPRL>
<RFCKERNRL>789</RFCKERNRL>
<RFCOPSYS>Linux</RFCOPSYS>
<RFCTZONE>3600</RFCTZONE>
</RFCSI_EXPORT>
</rfc:RFC_SYSTEM_INFO.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# Three work processes: one healthy, one STOPPED, one in PRIV mode.
WPINFO = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:TH_WPINFO.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<WPLIST>
<item><WP_INDEX>0</WP_INDEX><WP_TYP>DIA</WP_TYP><WP_PID>3011</WP_PID>
<WP_STATUS>running</WP_STATUS><WP_BNAME>GIRID</WP_BNAME><WP_REPORT>SAPMSSY1</WP_REPORT>
<WP_ELTIME>2</WP_ELTIME><WP_IPRIV> </WP_IPRIV><WP_WAITING></WP_WAITING></item>
<item><WP_INDEX>1</WP_INDEX><WP_TYP>BTC</WP_TYP><WP_PID>3012</WP_PID>
<WP_STATUS>stopped</WP_STATUS><WP_BNAME></WP_BNAME><WP_REPORT></WP_REPORT>
<WP_ELTIME>0</WP_ELTIME><WP_IPRIV> </WP_IPRIV><WP_WAITING>PRIV</WP_WAITING></item>
<item><WP_INDEX>2</WP_INDEX><WP_TYP>DIA</WP_TYP><WP_PID>3013</WP_PID>
<WP_STATUS>running</WP_STATUS><WP_BNAME>BATCHUSR</WP_BNAME><WP_REPORT>ZHEAVY</WP_REPORT>
<WP_ELTIME>9999</WP_ELTIME><WP_IPRIV>X</WP_IPRIV><WP_WAITING></WP_WAITING></item>
</WPLIST>
</rfc:TH_WPINFO.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# SINGLE-row table rendered as one <item> — the shape that makes naive
# parsers return a dict instead of a list and silently lose the row.
SERVER_LIST = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:TH_SERVER_LIST.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<SERVER_LIST>
<item><NAME>sapf4h01_F4H_00</NAME><HOST>sapf4h01</HOST><SERVTYPE>DIA BTC UPD</SERVTYPE></item>
</SERVER_LIST>
</rfc:TH_SERVER_LIST.Response>
</soap-env:Body>
</soap-env:Envelope>"""

XMI_LOGON = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:BAPI_XMI_LOGON.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<SESSIONID>0000000042</SESSIONID>
<RETURN><TYPE>S</TYPE><MESSAGE>Logon successful</MESSAGE></RETURN>
</rfc:BAPI_XMI_LOGON.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# Two jobs, one CANCELLED (status A) -> must become an anomaly.
JOB_SELECT = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:BAPI_XBP_JOB_SELECT.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<JOB_HEAD>
<item><JOBNAME>NIGHTLY_BATCH</JOBNAME><STATUS>F</STATUS><SDLUNAME>GIRID</SDLUNAME>
<STRTDATE>20260803</STRTDATE><STRTTIME>020000</STRTTIME></item>
<item><JOBNAME>Z_FAILED_LOAD</JOBNAME><STATUS>A</STATUS><SDLUNAME>BATCHUSR</SDLUNAME>
<STRTDATE>20260803</STRTDATE><STRTTIME>031500</STRTTIME></item>
</JOB_HEAD>
</rfc:BAPI_XBP_JOB_SELECT.Response>
</soap-env:Body>
</soap-env:Envelope>"""

XMI_LOGOFF = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:BAPI_XMI_LOGOFF.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<RETURN><TYPE>S</TYPE></RETURN>
</rfc:BAPI_XMI_LOGOFF.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# RFC_READ_TABLE against TBTCO (the transparent table behind SM37):
# fixed-width rows + FIELDS offsets, exactly as the real FM returns.
# Layout: JOBNAME(0,32) SDLUNAME(32,12) STATUS(44,1) STRTDATE(45,8) STRTTIME(53,6)
_ROW1 = "NIGHTLY_BATCH".ljust(32) + "GIRID".ljust(12) + "F" + "20260803" + "020000"
_ROW2 = "Z_FAILED_LOAD".ljust(32) + "BATCHUSR".ljust(12) + "A" + "20260803" + "031500"

READ_TABLE = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:RFC_READ_TABLE.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<FIELDS>
<item><FIELDNAME>JOBNAME</FIELDNAME><OFFSET>0</OFFSET><LENGTH>32</LENGTH></item>
<item><FIELDNAME>SDLUNAME</FIELDNAME><OFFSET>32</OFFSET><LENGTH>12</LENGTH></item>
<item><FIELDNAME>STATUS</FIELDNAME><OFFSET>44</OFFSET><LENGTH>1</LENGTH></item>
<item><FIELDNAME>STRTDATE</FIELDNAME><OFFSET>45</OFFSET><LENGTH>8</LENGTH></item>
<item><FIELDNAME>STRTTIME</FIELDNAME><OFFSET>53</OFFSET><LENGTH>6</LENGTH></item>
</FIELDS>
<DATA>
<item><WA>""" + _ROW1 + """</WA></item>
<item><WA>""" + _ROW2 + """</WA></item>
</DATA>
</rfc:RFC_READ_TABLE.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# A real SOAP Fault, e.g. missing S_RFC authorization for the FM.
FAULT = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<soap-env:Fault>
<faultcode>SOAP-ENV:Server</faultcode>
<faultstring>No RFC authorization for function module Z_DOES_NOT_EXIST.</faultstring>
</soap-env:Fault>
</soap-env:Body>
</soap-env:Envelope>"""

# Reproduces the exact live-F4H failure: the FM answers successfully but
# the table arrives under a DIFFERENT name than the one we hard-coded
# (release variation). A name-keyed parser silently reports zero rows here;
# _first_table()'s fallback must still find the data.
WPINFO_ALT_NAME = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:TH_WPINFO_ALT.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<WP_TABLE>
<item><WP_INDEX>0</WP_INDEX><WP_TYP>DIA</WP_TYP><WP_PID>4001</WP_PID>
<WP_STATUS>running</WP_STATUS><WP_BNAME>GIRID</WP_BNAME><WP_REPORT>SAPMSSY1</WP_REPORT>
<WP_ELTIME>1</WP_ELTIME><WP_IPRIV> </WP_IPRIV><WP_WAITING></WP_WAITING></item>
<item><WP_INDEX>1</WP_INDEX><WP_TYP>BTC</WP_TYP><WP_PID>4002</WP_PID>
<WP_STATUS>stopped</WP_STATUS><WP_BNAME></WP_BNAME><WP_REPORT></WP_REPORT>
<WP_ELTIME>0</WP_ELTIME><WP_IPRIV> </WP_IPRIV><WP_WAITING>PRIV</WP_WAITING></item>
</WP_TABLE>
</rfc:TH_WPINFO_ALT.Response>
</soap-env:Body>
</soap-env:Envelope>"""

# SNAP is a CLUSTER table — RFC_READ_TABLE genuinely cannot read it, and a
# real system answers with this fault (observed live on F4H).
TABLE_NOT_AVAILABLE = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<soap-env:Fault>
<faultcode>SOAP-ENV:Server</faultcode>
<faultstring>TABLE_NOT_AVAILABLE</faultstring>
</soap-env:Fault>
</soap-env:Body>
</soap-env:Envelope>"""

RESPONSES = {
    "RFC_SYSTEM_INFO": SYSTEM_INFO, "TH_WPINFO_ALT": WPINFO_ALT_NAME,
    "TH_WPINFO": WPINFO,
    "TH_SERVER_LIST": SERVER_LIST, "BAPI_XMI_LOGON": XMI_LOGON,
    "BAPI_XBP_JOB_SELECT": JOB_SELECT, "BAPI_XMI_LOGOFF": XMI_LOGOFF,
    "RFC_READ_TABLE": READ_TABLE,
}


# Which TABLES parameter each FM returns. Real SAP only sends a TABLES
# parameter back if the CALLER declared it in the request envelope — which
# is precisely why the first live F4H sweep got perfect data from
# RFC_SYSTEM_INFO (an EXPORT structure) and empty results from every
# table-returning FM. Reproducing that here turns the bug into a permanent
# regression guard instead of a one-off fix.
TABLE_PARAM = {
    "TH_WPINFO": "WPLIST",
    "TH_WPINFO_ALT": "WP_TABLE",
    "TH_SERVER_LIST": "SERVER_LIST",
    "BAPI_XBP_JOB_SELECT": "JOB_HEAD",
    "RFC_READ_TABLE": "DATA",
}

_TABLE_BLOCK = re.compile(
    r"<(?P<name>[A-Z_0-9]+)>\s*<item>.*?</item>\s*</(?P=name)>", re.S)


def _apply_table_rules(function: str, xml: str, request_body: str) -> str:
    """Strip the data table from the reply unless the request declared it."""
    table = TABLE_PARAM.get(function)
    if not table:
        return xml
    declared = (f"<{table}>" in request_body) or (f"<{table} />" in request_body) \
        or (f"<{table}/>" in request_body)
    if declared:
        return xml
    return re.sub(rf"<{table}>.*?</{table}>", f"<{table}/>", xml, flags=re.S)


class H(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        # /inactive simulates the SICF service being switched off.
        if self.path.startswith("/inactive"):
            self._send("<html>Not found</html>", 404)
            return

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SAP"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        user, _, pwd = base64.b64decode(auth.split(" ", 1)[1]).decode().partition(":")
        if (user, pwd) != (USER, PASSWORD):
            self._send("<html>unauthorized</html>", 401)
            return

        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        # SNAP via RFC_READ_TABLE must fault exactly as a real system does.
        if "RFC_READ_TABLE" in body and "SNAP" in body:
            self._send(TABLE_NOT_AVAILABLE, 500)
            return
        for name, xml in RESPONSES.items():
            if name in body:
                self._send(_apply_table_rules(name, xml, body))
                return
        self._send(FAULT, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8095), H).serve_forever()
