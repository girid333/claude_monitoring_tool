"""Minimal fake SAP WebGUI: login page + OK-code field, enough for v1's
BasicLoginPlugin + sap_automator navigation to actually execute."""
from http.server import BaseHTTPRequestHandler, HTTPServer

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
<table><tr><td>Status</td><td>OK</td></tr></table></div>
</body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())
    def do_GET(self):
        self._send(SESSION if "logged" in self.path or "~transaction" in self.path else LOGIN)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self._send(SESSION)
    def log_message(self, *a): pass

HTTPServer(("127.0.0.1", 8099), H).serve_forever()
