"""Generate a synthetic SAP Automated Health Check workbook (SpreadsheetML).

Anonymised (system QAS, fake hosts) — the real customer export never goes in
the repo. ADVERSARIAL by design (SKILL §5): it reproduces every quirk found in
a real export — &#0; runs, ss:MergeAcross, ss:Index gaps, CRLF line endings,
empty <Row/> separators inside tables, multi-table sheets, fixed-width text
tabs — plus the situations the analysers must tell apart:

  SM21   EBI job-scheduling failure with continuation lines (act) + benign R48
  SM12   a genuinely stale VBAK lock (act) next to by-design bgRFC locks
  SMLG   a foreign instance (system DEV) in logon group PUBLIC (act)
  Cert   an EXPIRED own certificate + a partner cert inside the horizon (act)
  SLICENSE  one truly invalid license (act)
  DB02   a High alert (act); DB02_Log quiet for weeks (expected)
  DB12   a failed backup (act)
  SM04   only bgRFC schedulers at high priority (expected)
  ST06   European number formats with blank rows between data rows
  ZZ99   a Monitors row whose tab does not exist (SAP verdict kept, noted)
"""

from __future__ import annotations

import sys
from xml.sax.saxutils import escape

REPORT_DATE = "15/03/2026"


def cell(v: str = "", merge: int = 0, index: int = 0) -> str:
    attrs = ""
    if index:
        attrs += f' ss:Index="{index}"'
    if merge:
        attrs += f' ss:MergeAcross="{merge}"'
    data = f'<Data ss:Type="String">{escape(v)}</Data>' if v != "" else ""
    return f"<Cell{attrs}>{data}</Cell>"


def row(*vals: str) -> str:
    return "<Row>" + "".join(cell(v) for v in vals) + "</Row>"


def blank(n: int) -> str:
    return "<Row>" + "<Cell/>" * n + "</Row>"


EMPTY = "<Row/>"


def sheet(name: str, rows: list[str]) -> str:
    return (f'<Worksheet ss:Name="{name}"><Table>' + "\r\n".join(rows) +
            "</Table></Worksheet>")


def std(name: str, title: str, cols: list[str], data: list[list[str]]) -> str:
    return sheet(name, [row(name, "", "", ""), row(title, "", "", ""), EMPTY,
                        row(*cols), blank(len(cols)), *[row(*d) for d in data]])


def build() -> str:
    monitors = [
        ("Application Servers", "SM51", "Check that all servers are up", "Current Snapshot", "X", "", "All Servers are active"),
        ("SAP System Log", "SM21", "System logs analysis", "Last 24 hours", "", "X", "5 High Priority Syslog message(s) are found"),
        ("Lock Entries", "SM12", "Check for stale locks", "Current Snapshot", "", "X", "3 entries are locked for more than 5 hours"),
        ("CCMS Maintain Logon Group and Load Distribution", "SMLG", "Logon groups", "Current Snapshot", "", "X", "Instance not in Green status"),
        ("Certificate Validity and Expiry information for the system", "Certificate Info",
         "Check if Validity of any Certificate will be Expired in next 42 days", "NA", "", "X", "Certificate will be expired"),
        ("SAP License Administration", "SLICENSE", "Digitally Signed Licenses", "Current Snapshot", "", "X", "At Least One System ID Status is Invalid"),
        ("Backup Catalog (data backup)", "DB12", "Data backup catalog information", "Current Snapshot", "", "X", "Backup failed"),
        ("Current Alerts", "DB02", "Current Alerts with Check Information", "Current Snapshot", "", "X", "High Priority message is there"),
        ("System Availability Log", "DB02", "System Availability Log details in DB02", "Current Snapshot", "", "X", "Last Event Time is Greater than 24 Hrs"),
        ("User Sessions", "SM04", "User Sessions", "Current Snapshot", "", "X", "2  High Priority Session(s) found"),
        ("Review SAP server/system capacity", "ST06", "OS monitor", "Current Snapshot", "X", "", "OK"),
        ("Custom check", "ZZ99", "A check whose tab is missing", "Current Snapshot", "", "X", "Custom check failed"),
    ]
    mon_rows = [
        "<Row>" + cell(f"Technical Daily Health Check Report - {REPORT_DATE}", merge=7) + "</Row>",
        row("Monitoring Task", "Transaction", "Description", "Selection Period", "OK", "Not OK", "Evidence", "Recommendation"),
        *[row(*m, "") for m in monitors],
    ]
    sheets = [sheet("Monitors", mon_rows)]

    sheets.append(std("SM51", "2 AS Instance of SAP System QAS 100",
                      ["Application Server Instance", "Host", "Instance Services", "State"],
                      [["qashost01_QAS_00", "qashost01", "Dialog Batch", "Active"],
                       ["qashost02_QAS_00", "qashost02", "Dialog Batch", "Active"]]))

    sm21 = [
        ["14/03/2026", "10:00:00", "qashost01_QAS_00", "BTC", "010", "", "", "Very High Priority", "01", "EBI", "Error scheduling a batch job"],
        ["14/03/2026", "10:00:00", "qashost01_QAS_00", "BTC", "010", "", "", "Information", "08", "EJL", "> client 100"],
        ["14/03/2026", "10:00:00", "qashost01_QAS_00", "BTC", "010", "", "", "Information", "08", "EBC", "> Job R_JR_BTCJOBS_GENERATOR"],
        ["14/03/2026", "10:00:00", "qashost01_QAS_00", "BTC", "010", "", "", "Information", "08", "EBT", "> Batch user SAP_SYSTEM does not exist"],
        ["14/03/2026", "11:00:00", "qashost01_QAS_00", "BTC", "010", "", "", "Very High Priority", "01", "EBI", "Error scheduling a batch job"],
        ["14/03/2026", "12:00:00", "qashost02_QAS_00", "DIA", "003", "100", "JDOE", "Very High Priority", "01", "R48", "Soft cancel after GUI disconnect"],
        ["14/03/2026", "12:05:00", "qashost02_QAS_00", "DIA", "003", "100", "JDOE", "Very High Priority", "01", "R48", "Soft cancel after GUI disconnect"],
        ["14/03/2026", "12:10:00", "qashost02_QAS_00", "DIA", "004", "100", "", "Very High Priority", "01", "XYZ", "Something unclassified"],
        ["14/03/2026", "12:11:00", "qashost02_QAS_00", "DIA", "004", "100", "", "Information", "08", "Q02", "Stops work process 4"],
    ]
    sheets.append(std("SM21", "Syslog Messages in QAS 100",
                      ["Date", "Time", "Extended Instance Name", "Type", "Process No", "Client", "User",
                       "Priority", "Severity", "Message ID", "Message Text"], sm21))

    sheets.append(std("SM12", "Lock Entry List in QAS 100",
                      ["Client", "User", "Date/Time", "Lock Mode", "Table Name", "Lock Argument", "Counter 1.", "Counter 2."],
                      [["100", "JDOE", "15/03/2026 --11:30:00", "E", "EKKO", "100450000123", "", "1"],
                       ["100", "BATCHUSR", "14/03/2026 --02:00:00", "E", "VBAK", "1000000456", "", "1"],
                       ["100", "BGRFC_USER", "01/03/2026 --09:00:00", "E", "BGRFC_O_SERVER_REGISTRATION", "qashost01", "", "1"],
                       ["100", "BGRFC_USER", "01/03/2026 --09:00:00", "E", "BGRFC_I_SERVER_REGISTRATION", "qashost01", "", "1"]]))

    sheets.append(sheet("SMLG", [
        row("SMLG", "", "", ""), row("CCMS Logon Groups and Load Distribution", "", "", ""), EMPTY,
        row("Logon Group", "Instance", "Status"), blank(3),
        row("PUBLIC", "devhost01_DEV_00", "NOT Green"),
        row("SPACE", "qashost01_QAS_00", "Green"),
        row("SPACE", "qashost02_QAS_00", "Green"),
        EMPTY,
        row("Application Server", "State", "Resp. time(ms)", "Thrshd", "User", "Thrshd", "Time", "Quality", "Dialog Steps"),
        blank(9),
        row("qashost01_QAS_00", "Green", "3", "", "12", "", "11:54:07", "300", "10"),
    ]))

    cert_text = [
        "Cert_Info", "Certificate Validity Info of QAS 100", "",
        "15.03.2026      Overview of Installed Certificates                1",
        "-" * 60,
        "System                                                  PROG <SYST>", "",
        "Own Certificate",
        "CN=QAS                                                                            01.01.2038", "",
        "SSL server Standard                                     SSLS DFAULT", "",
        "Own Certificate",
        "CN=qashost01.example.test, O=Example Corp                                         10.03.2026", "",
        "Certificate List",
        "1  = CN=partner.example.org, O=Partner Inc                                        05.04.2026",
        "2  = CN=Example Root CA, O=Example Corp                                            01.01.2040",
    ]
    sheets.append(sheet("Cert_Info", [row(x) for x in cert_text]))

    sheets.append(std("SLICENSE", "SAP License Administration QAS 100",
                      ["Status", "SID", "Hardware Key", "SW product", "Limit", "Start Date", "End Date", "Type",
                       "Installation Number", "System No.", "Status Text"],
                      [["", "QAS", "A000000001", "NetWeaver_HDB", "2147483647", "01/01/2026", "31/12/9999", "0", "0012345678", "000000000001", "Valid"],
                       ["", "QAS", "B000000002", "NetWeaver_HDB", "2147483647", "01/01/2025", "01/02/2026", "0", "0012345678", "000000000001", "Invalid (expired)"]]))

    sheets.append(sheet("DB12", [
        row("Data Backup Catalog QAS", "", "", ""), row("Date: 15.03.2026 - Data Backup Catalog", "", "", ""), EMPTY,
        row("Start Time (SYS)", "End Time (SYS)", "Duration", "State", "Message", "Source ID", "Source Type Name",
            "Host", "Service Type Name", "Destination Path", "Size of Backup (Bytes)"), blank(11),
        row("14.03.2026 22:00:00", "14.03.2026 22:30:00", "00:30:00", "failed", "backint error", "2", "volume",
            "qasdb01", "indexserver", "/backint/x", "0"),
        row("13.03.2026 22:00:00", "13.03.2026 22:31:00", "00:31:00", "successful", "<ok>", "2", "volume",
            "qasdb01", "indexserver", "/backint/y", "120000000000"),
    ]))

    sheets.append(sheet("DB02_Alerts", [
        row("Current Alerts in system QAS", "", "", ""),
        row("Current Alerts: 0  Errors, 1  High, 1   Medium, 0  Low", "", "", ""), EMPTY,
        row("Timestamp (Timestamp unformatted)", "Alert ID", "Description", "User Action", "Priority"), blank(5),
        row("15.03.2026 09:00:00", "2", "Disk usage of volume 3 is above 90%", "Extend the volume", "High"),
        row("15.03.2026 09:00:00", "62", "The password for database user TECH01 will expire on 2026-04-30 at 10:00:00. If the password expires, the user will be locked.", "Change it", "Medium"),
    ]))

    sheets.append(sheet("DB02_Log", [
        row("System Availability Log in system QAS", "", "", ""), row("Date: 15.03.2026", "", "", ""), EMPTY,
        row("Event Time", "Guid", "Is Origin", "Trace Host", "Event Name"), blank(5),
        row("01.02.2026 00:03:57", "g1", "TRUE", "qasdb01", "SERVICE_STOPPED"),
        row("01.02.2026 00:53:37", "g2", "TRUE", "qasdb01", "SERVICE_STARTED"),
    ]))

    sheets.append(std("SM04", "User Sessions for AS Instance qashost01",
                      ["Client", "User ID", "Client Host", "Application", "Dialog Time", "Type of User Session",
                       "No. of Sessions", "Priority", "Memory", "ConvID", "Application Info."],
                      [["100", "BGRFC_USER", "", "", "15.03.2026 11:55:41", "BGRFC Scheduler", "1", "High", "4290", "1", ""],
                       ["100", "BGRFC_USER", "", "", "15.03.2026 11:56:08", "BGRFC Scheduler", "1", "High", "4290", "2", ""],
                       ["100", "JDOE", "pc01", "", "15.03.2026 11:58:00", "GUI", "1", "Medium", "2000", "3", ""]]))

    st06 = [row("ST06", "", "", ""), row("QAS CPU and Memory Utilization", "", "", ""), EMPTY,
            row("Server", "Monitoring Category", "Description", "Value", "Unit"), blank(5),
            row("qashost01_QAS_00", "CPU", "Context switches", "1.366", "/s"), EMPTY,
            row("qashost01_QAS_00", "", "Idle", "12", "%"), EMPTY,
            row("", "", "Number of CPUs", "16", ""), EMPTY,
            row("qashost02_QAS_00", "", "Idle", "61", "%"), EMPTY,
            row("qashost02_QAS_00", "", "Average processes waiting (  1 min)", "0,22", "")]
    sheets.append(sheet("ST06", st06))

    # A leftover tab with no Monitors row, and a NUL-ridden cell SAP really emits.
    sheets.append(sheet("RZ12", [row("RZ12", "", "", ""), row("CCMS: RFC Server Group Maintenance"), EMPTY,
                                 row("Logon Group", "Instance", "Status"), blank(3),
                                 "<Row>" + cell("NONE") +
                                 '<Cell><Data ss:Type="String">NONE&#0;&#0;&#x1;</Data></Cell>' +
                                 cell("x", index=4) + "</Row>"]))

    return ('<?xml version="1.0"?>\r\n<?mso-application progid="Excel.Sheet"?>\r\n'
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\r\n'
            + "\r\n".join(sheets) + "\r\n</Workbook>\r\n")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "ahc_synthetic.xls"
    with open(out, "w", encoding="utf-8", newline="") as f:
        f.write(build())
    print(out)
