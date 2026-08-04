"""Fake SE16 for the D-049d UI test.

NOTE: the OK-code field is wrapped in its OWN form so that pressing Enter
actually submits it. Without that, /nSE16 navigation silently did nothing
and every data row after the first failed with "could not resolve target"
— a fixture defect that looked exactly like a product bug.

Original docstring follows.

Fake SE16 for the D-049d UI test — the scenario Giri is running first.

Two screens (selection → results), which is the point: it exercises
multi-screen navigation, so a data run has to re-enter the selection
screen for every row rather than just re-typing on one page.

Read-only by nature, which is why SE16 is a good first live test: no risk
of leaving junk data behind on a NetWeaver system.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""

# A few tables with different row counts, so each iteration's screenshot
# genuinely differs — otherwise the evidence would be indistinguishable.
TABLES = {
    "T000": [("100", "Home S/4", "C"), ("200", "Training", "T")],
    "TBTCO": [("NIGHTLY_BATCH", "F", "20260803"), ("Z_LOAD", "A", "20260803"),
              ("SAP_ALERT", "F", "20260802")],
    "USR02": [("GIRID", "20260804"), ("DDIC", "20260701")],
}


def selection(message: str = "", tabname: str = "") -> str:
    return f"""<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<form method="post" action="/webgui" id="okform"><input type="text" title="Command Field" id="okcd" name="okcd"></form>
<h1>Data Browser: Initial Screen</h1>
<form method="post" action="/webgui">
  <label for="tabname">Table Name</label>
  <input type="text" id="tabname" name="tabname" class="lsField__input" value="{tabname}">
  <button id="btn_exec" class="lsButton" title="Execute" name="action" value="exec">Execute</button>
</form>
<div id="statusbar">{message}</div>
</body></html>"""


def results(tabname: str, rows: list) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"""<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<form method="post" action="/webgui" id="okform"><input type="text" title="Command Field" id="okcd" name="okcd"></form>
<h1>Data Browser: Table {tabname} Select Entries</h1>
<table id="alv"><tbody>{body}</tbody></table>
<div id="statusbar">{len(rows)} Entries displayed for table {tabname}</div>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        self._send(selection() if "sess" in self.path else LOGIN)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(n).decode()) if n else {}
        okcd = (form.get("okcd") or [""])[0]
        if okcd:                     # /nSE16 returns to the selection screen
            self._send(selection())
            return
        if "action" not in form:     # login post
            self._send(selection())
            return
        tabname = (form.get("tabname") or [""])[0].strip().upper()
        if not tabname:
            self._send(selection("Please enter a table name"))
            return
        if tabname not in TABLES:
            self._send(selection(f"Table {tabname} does not exist", tabname))
            return
        self._send(results(tabname, TABLES[tabname]))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8093), H).serve_forever()
