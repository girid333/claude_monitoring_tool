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
#
# Uses the REAL parameter name and field names confirmed live on F4H
# (release 758): the table is LIST (type MSXXLIST) with NAME/HOST/SERV/
# MSGTYPES — NOT SERVER_LIST, which isn't a parameter of this FM at all.
# MSG_INSTANCE is an EXPORT, and it was the ONLY thing that came back when
# the wrong table name was declared.
SERVER_LIST = """<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:TH_SERVER_LIST.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<MSG_INSTANCE/>
<LIST>
<item><NAME>nwrhel9_F4H_00</NAME><HOST>nwrhel9</HOST><SERV>sapdp00</SERV>
<MSGTYPES>DIA BTC UPD SPO UP2</MSGTYPES></item>
</LIST>
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
    "TH_SERVER_LIST": "LIST",
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


# D-046: the SCC4 eCATT flag this fake client reports. Overridable via the
# /ecatt-flag/<X> control path so a test can exercise BOTH the permitted and
# the locked-down case — the latter being the realistic production default.
ECATT_FLAG = "X"

# D-047: whether this fake system has an active Fiori/Gateway layer. Both
# states are real-world: plenty of systems have no Gateway at all, and the
# tool must say so plainly rather than appear broken.
GATEWAY_ACTIVE = True

# D-047b: realistic SAP EDMX — namespaced, with sap: annotations, a keyed
# entity type, and a function import carrying an HTTP method. Anything that
# only parses namespace-free XML will fail against this, exactly as it would
# against the real thing.
SERVICE_METADATA = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
           xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
           xmlns:sap="http://www.sap.com/Protocols/SAPData">
<edmx:DataServices m:DataServiceVersion="2.0">
<Schema Namespace="ALERTINBOX" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
  <EntityType Name="Alert">
    <Key><PropertyRef Name="AlertGuid"/></Key>
    <Property Name="AlertGuid" Type="Edm.String" Nullable="false" MaxLength="32"/>
    <Property Name="Severity" Type="Edm.Int32" Nullable="true"/>
    <Property Name="Description" Type="Edm.String" Nullable="true" MaxLength="255"/>
    <Property Name="Status" Type="Edm.String" Nullable="true" MaxLength="10"/>
  </EntityType>
  <EntityType Name="Forbidden">
    <Key><PropertyRef Name="Id"/></Key>
    <Property Name="Id" Type="Edm.String" Nullable="false"/>
  </EntityType>
  <EntityContainer Name="ALERTINBOX_Entities" m:IsDefaultEntityContainer="true">
    <EntitySet Name="AlertSet" EntityType="ALERTINBOX.Alert"
               sap:creatable="false" sap:updatable="true" sap:deletable="false"/>
    <EntitySet Name="ForbiddenSet" EntityType="ALERTINBOX.Forbidden"/>
    <FunctionImport Name="ConfirmAlert" ReturnType="ALERTINBOX.Alert"
                    m:HttpMethod="POST">
      <Parameter Name="AlertGuid" Type="Edm.String" Mode="In" Nullable="false"/>
      <Parameter Name="Comment" Type="Edm.String" Mode="In" Nullable="true"/>
    </FunctionImport>
  </EntityContainer>
</Schema>
</edmx:DataServices>
</edmx:Edmx>"""

ALERT_ROWS = """{"d":{"results":[
{"__metadata":{"uri":"x"},"AlertGuid":"AAA1","Severity":3,
 "Description":"Job SRSM_AMA_SELFMON_HEARTBEAT cancelled","Status":"NEW",
 "Nested":{"deep":"ignored"}},
{"__metadata":{"uri":"y"},"AlertGuid":"BBB2","Severity":1,
 "Description":"Work process in PRIV mode","Status":"ACK"}
]}}"""

ODATA_ERROR = """{"error":{"code":"SY/530",
 "message":{"lang":"en","value":"Not authorized for entity set ForbiddenSet"}}}"""

CATALOG_JSON = """{"d":{"results":[
{"ID":"ZSD_SALESORDER_SRV_0001","Title":"Sales Order Management",
 "TechnicalServiceName":"ZSD_SALESORDER_SRV","TechnicalServiceVersion":1,
 "ServiceUrl":"/sap/opu/odata/sap/ZSD_SALESORDER_SRV"},
{"ID":"API_BUSINESS_PARTNER_0001","Title":"Business Partner",
 "TechnicalServiceName":"API_BUSINESS_PARTNER","TechnicalServiceVersion":1,
 "ServiceUrl":"/sap/opu/odata/sap/API_BUSINESS_PARTNER"}
]}}"""


def _read_table_xml(fields: list[tuple[str, int]], rows: list[list[str]]) -> str:
    """Build an RFC_READ_TABLE reply with correct FIELDS offsets + fixed-width
    WA lines, so the client's real unpacking logic is exercised."""
    offset, field_xml = 0, []
    for name, length in fields:
        field_xml.append(f"<item><FIELDNAME>{name}</FIELDNAME>"
                          f"<OFFSET>{offset}</OFFSET><LENGTH>{length}</LENGTH></item>")
        offset += length
    data_xml = []
    for row in rows:
        line = "".join(str(v).ljust(ln) for v, (_, ln) in zip(row, fields))
        data_xml.append(f"<item><WA>{line}</WA></item>")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
<rfc:RFC_READ_TABLE.Response xmlns:rfc="urn:sap-com:document:sap:rfc:functions">
<FIELDS>{''.join(field_xml)}</FIELDS>
<DATA>{''.join(data_xml)}</DATA>
</rfc:RFC_READ_TABLE.Response>
</soap-env:Body>
</soap-env:Envelope>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        # test control: flip the simulated SCC4 eCATT restriction
        global ECATT_FLAG, GATEWAY_ACTIVE
        if self.path.startswith("/ecatt-flag/"):
            ECATT_FLAG = self.path.rsplit("/", 1)[-1].replace("blank", " ")
            self._send("<ok/>")
            return
        # test control: simulate a system with / without an active Gateway
        if self.path.startswith("/gateway/"):
            GATEWAY_ACTIVE = self.path.rsplit("/", 1)[-1] == "on"
            self._send("<ok/>")
            return
        # D-047b: $metadata for a service, and entity-set reads.
        if self.path.endswith("$metadata") or "$metadata" in self.path:
            self._send_xml(SERVICE_METADATA)
            return
        if "/AlertSet" in self.path:
            self._send_json(ALERT_ROWS)
            return
        if "/ForbiddenSet" in self.path:
            self._send_json(ODATA_ERROR, 403)
            return
        # D-047: SAP's OData catalog service.
        if "CATALOGSERVICE" in self.path.upper():
            if not GATEWAY_ACTIVE:
                self._send("<html>Not Found</html>", 404)
                return
            self._send_json(CATALOG_JSON)
            return
        self._send("<html/>")

    def _send_json(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_xml(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
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
        # eCATT (D-046): T000 carries the SCC4 gate, TADIR lists the objects.
        if "RFC_READ_TABLE" in body and "T000" in body:
            self._send(_read_table_xml(
                [("MANDT", 3), ("MTEXT", 25), ("CCCATEGORY", 1), ("CATTKENNZ", 1)],
                [["100", "Home S/4", "C", ECATT_FLAG]]))
            return
        # D-047: remote-enabled function module catalogue.
        if "RFC_READ_TABLE" in body and "TFDIR" in body:
            rows = [["Z_CREATE_ORDER", "R"], ["Z_CHECK_STOCK", "R"]] if "Z%" in body \
                else [["BAPI_SALESORDER_CREATEFROMDAT2", "R"], ["RFC_READ_TABLE", "R"],
                      ["Z_CREATE_ORDER", "R"]]
            self._send(_read_table_xml([("FUNCNAME", 30), ("FMODE", 1)], rows))
            return
        if "RFC_READ_TABLE" in body and "TFTIT" in body:
            self._send(_read_table_xml(
                [("SPRAS", 1), ("FUNCNAME", 30), ("STEXT", 40)],
                [["E", "Z_CREATE_ORDER", "Create a sales order"],
                 ["E", "Z_CHECK_STOCK", "Check stock availability"]]))
            return
        if "RFC_READ_TABLE" in body and "TADIR" in body:
            kind = "ECTC" if "ECTC" in body else "ECSC"
            names = ([["R3TR", kind, "Z_SM37_CHECK_CFG", "ZMON", "GIRID"]] if kind == "ECTC"
                      else [["R3TR", kind, "Z_SM37_CHECK", "ZMON", "GIRID"],
                            ["R3TR", kind, "Z_VA01_CREATE", "ZMON", "GIRID"]])
            self._send(_read_table_xml(
                [("PGMID", 4), ("OBJECT", 4), ("OBJ_NAME", 40),
                 ("DEVCLASS", 30), ("AUTHOR", 12)], names))
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
