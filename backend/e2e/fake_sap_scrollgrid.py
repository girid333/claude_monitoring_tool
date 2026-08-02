"""Fake SAP with a tall inner-scrolling ALV-like grid (mirrors SE16)."""
from http.server import BaseHTTPRequestHandler, HTTPServer
rows = "".join(
    f"<tr><td>ROW_{i:03d}_JOBNAME</td><td>{15000000+i*137}</td><td>%NEWSTEP</td></tr>"
    for i in range(200))
LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN"></form></body></html>"""
SESSION = f"""<html><body style="margin:0;font:13px sans-serif">
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h2 style="margin:6px">Data Browser: Table TBTCO</h2>
<div id="grid" class="lsGridContainer"
     style="height:600px;overflow-y:scroll;border:1px solid #ccc">
  <table id="alv"><thead><tr><th>JOBNAME</th><th>JOBCOUNT</th><th>INTREPORT</th></tr></thead>
  <tbody>{rows}</tbody></table>
</div></body></html>"""
class H(BaseHTTPRequestHandler):
    def _s(self, b):
        self.send_response(200); self.send_header("Content-Type","text/html")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b.encode())
    def do_GET(self): self._s(LOGIN)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0))); self._s(SESSION)
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1", 8097), H).serve_forever()
