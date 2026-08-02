"""Fake SAP whose grid scrolls via a CUSTOM scrollbar widget — scrollTop
NEVER changes, rows are repainted instead. This is the SAP ALV behaviour that
defeated the scroll-position approach (D-037)."""
from http.server import BaseHTTPRequestHandler, HTTPServer
LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN"></form></body></html>"""
SESSION = """<html><body style="margin:0;font:13px monospace">
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h2 id="hdr" style="margin:6px">Data Browser: Table TBTCO</h2>
<div id="grid" class="lsGridContainer" style="height:560px;overflow:hidden;border:1px solid #ccc">
  <table id="alv"><thead><tr><th>JOBNAME</th><th>JOBCOUNT</th></tr></thead>
  <tbody id="body"></tbody></table>
</div>
<div id="pos" style="padding:6px">offset 0</div>
<script>
// Virtualised rows: the "scroll" repaints content; scrollTop stays 0 forever,
// exactly like SAP's ALV scrollbar widget.
var offset = 0, PAGE = 20, TOTAL = 150;
function paint() {
  var h = "";
  for (var i = offset; i < Math.min(offset + PAGE, TOTAL); i++)
    h += "<tr><td>ROW_" + String(i).padStart(3,"0") + "_JOBNAME</td><td>" + (15000000 + i*137) + "</td></tr>";
  document.getElementById("body").innerHTML = h;
  document.getElementById("pos").textContent = "offset " + offset;
}
document.getElementById("grid").addEventListener("wheel", function (e) {
  e.preventDefault();
  offset = Math.max(0, Math.min(TOTAL - PAGE, offset + Math.round(e.deltaY / 40)));
  paint();
}, {passive:false});
paint();
</script></body></html>"""
class H(BaseHTTPRequestHandler):
    def _s(self, b):
        self.send_response(200); self.send_header("Content-Type","text/html")
        self.send_header("Content-Length", str(len(b))); self.end_headers()
        self.wfile.write(b.encode())
    def do_GET(self): self._s(LOGIN)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0))); self._s(SESSION)
    def log_message(self,*a): pass
HTTPServer(("127.0.0.1", 8096), H).serve_forever()
