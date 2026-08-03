"""Fake SAP selection screen for D-043: a radio GROUP rendered as ARIA
spans (role="radio", aria-checked — SAP's real shape, same family as the
D-040 checkboxes), a native <select> dropdown (SAP genuinely uses these,
e.g. SM37's "Or after event"), and a plain text field for override testing.

The radio group has SAP-like semantics baked into the page's own click
handler: selecting one deselects the others.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer

LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""

SESSION = """<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h1>SAP Easy Access</h1>
<label for="matnr">Material</label>
<input type="text" id="matnr" name="RM07M-MATNR" class="lsField__input">
<div id="radio-row" role="radiogroup">
<span role="radio" aria-checked="true" tabindex="0" class="lsRadio">Standard view</span>
<span role="radio" aria-checked="false" tabindex="0" class="lsRadio">Detail view</span>
<span role="radio" aria-checked="false" tabindex="0" class="lsRadio">Grid view</span>
</div>
<label for="evt">Or after event</label>
<select id="evt" name="BTCH2170-EVENTID" class="lsCombo">
<option value="">-</option>
<option value="SAP_END_OF_DAY">SAP_END_OF_DAY</option>
<option value="SAP_NEW_MONTH">SAP_NEW_MONTH</option>
</select>
<button id="btn_exec" class="lsButton" title="Execute">Execute</button>
<div id="result">Ready</div>
<script>
document.querySelectorAll('[role="radio"]').forEach(function (el) {
  el.addEventListener('click', function () {
    document.querySelectorAll('[role="radio"]').forEach(function (o) {
      o.setAttribute('aria-checked', (o === el).toString());
    });
  });
});
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    def _s(self, b):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b.encode())

    def do_GET(self):
        self._s(SESSION if "sess" in self.path else LOGIN)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._s(SESSION)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8096), H).serve_forever()
