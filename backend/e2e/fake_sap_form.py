"""Fake SAP WebGUI with a real form, for record+replay round-trip testing."""
from http.server import BaseHTTPRequestHandler, HTTPServer
LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""
SESSION = """<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h1>SAP Easy Access</h1>
<label for="jobname">Job name</label>
<input type="text" id="jobname" name="BTCH2170-JOBNAME" class="lsField__input">
<input type="checkbox" id="aborted" name="BTCH2170-ABORTED" class="lsCheckBox">
<button id="btn_exec" class="lsButton" title="Execute">Execute</button>
<div id="result">Ready</div>
</body></html>"""
class H(BaseHTTPRequestHandler):
    def _s(self, b):
        self.send_response(200); self.send_header("Content-Type","text/html")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b.encode())
    def do_GET(self): self._s(SESSION if "sess" in self.path else LOGIN)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0))); self._s(SESSION)
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1", 8098), H).serve_forever()
