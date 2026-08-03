"""Fake SAP WebGUI selection screen with ARIA-widget checkboxes (D-040).

Mirrors what v1 found in SM37's job-overview screen: checkboxes are
`<span role="checkbox" aria-checked="true|false">`, not native
`<input type="checkbox">`, toggled by SAP's own click handler (not native
browser semantics) — exactly the shape that broke `set_checked()`/blind
click-based replay.

Deliberately gives the six checkboxes NO id/name/title/aria-label — SAP's
real ones commonly don't carry one either — so the DOM resolver is forced
down the new checkbox_index positional tier rather than any semantic-
attribute shortcut.

Serves two different sets of STARTING aria-checked defaults ("record" vs
"replay" phase, switched via GET /phase/<name>) so a test can prove replay
sets the ABSOLUTE recorded end-state rather than blindly toggling — a
blind toggle only happens to look correct when the starting state matches
what it was during recording.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer

# index: 0..5. Record-time defaults vs replay-time defaults deliberately
# differ for 0, 1, 5 (already-at-target at replay time — a blind toggle
# would wrongly flip these) and match for 3, 4 (both approaches agree).
DEFAULTS = {
    "record": [True, False, True, False, True, False],
    "replay": [False, True, True, False, True, False],
}
PHASE = {"name": "record"}

LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""

SCRIPT = """<script>
document.querySelectorAll('[role="checkbox"]').forEach(function (el) {
  el.addEventListener('click', function () {
    var cur = el.getAttribute('aria-checked') === 'true';
    el.setAttribute('aria-checked', (!cur).toString());
  });
});
</script>"""


def _session_html() -> str:
    defaults = DEFAULTS[PHASE["name"]]
    spans = "\n".join(
        f'<span role="checkbox" aria-checked="{"true" if v else "false"}" '
        f'tabindex="0" class="lsCheckBox">box{i}</span>'
        for i, v in enumerate(defaults)
    )
    return f"""<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h1>SAP Easy Access</h1>
<div id="checkbox-row">
{spans}
</div>
<button id="btn_exec" class="lsButton" title="Execute">Execute</button>
<div id="result">Ready</div>
{SCRIPT}
</body></html>"""


class H(BaseHTTPRequestHandler):
    def _s(self, b: str) -> None:
        body = b.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/phase/"):
            name = self.path.split("/phase/", 1)[1].strip("/")
            if name in DEFAULTS:
                PHASE["name"] = name
            self._s("ok")
            return
        self._s(_session_html() if "sess" in self.path else LOGIN)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._s(_session_html())

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8099), H).serve_forever()
