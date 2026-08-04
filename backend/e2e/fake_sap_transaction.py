"""Fake create-document transaction for D-049c, built to be ADVERSARIAL in
the ways a real business transaction is:

  - a successful save returns a document number in SAP's own phrasing;
  - one specific vendor is BLOCKED, returning a business error while every
    field interaction technically succeeded — the case that would wrongly
    read as PASS without screen-text checking;
  - after a failure the screen shows a MODAL that stays until dismissed,
    so a run without per-row recovery will have row N+1 typing into a
    blocked screen and failing for the wrong reason. That is precisely the
    silent corruption the recovery step exists to prevent.
  - the form does NOT reset itself after submit, so values persist —
    exactly like a real screen, and another reason recovery matters.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user" id="sap-user">
<input type="password" name="sap-password" id="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""

_counter = {"n": 4500001233}
BLOCKED_VENDOR = "200000"


def screen(message: str = "", modal: bool = False,
           vendor: str = "", material: str = "", qty: str = "") -> str:
    modal_html = ""
    if modal:
        # A real modal blocks the form beneath it until dismissed.
        modal_html = """
<div id="popup" style="position:fixed;top:0;left:0;right:0;bottom:0;
     background:rgba(0,0,0,.4);z-index:99">
  <div style="background:#fff;margin:80px auto;padding:20px;width:420px">
    <p id="popup-msg">Processing was terminated. Cancel the transaction?</p>
    <button id="popup-ok" onclick="document.getElementById('popup').remove()"
            title="Continue">Continue</button>
  </div>
</div>"""
    return f"""<html><body>
<div class="lsBlockLayer" style="display:none"></div>
<input type="text" title="Command Field" id="okcd" name="okcd">
<h1>Create Purchase Order</h1>
<form method="post" action="/webgui">
  <label for="vendor">Vendor</label>
  <input type="text" id="vendor" name="vendor" class="lsField__input" value="{vendor}">
  <label for="material">Material</label>
  <input type="text" id="material" name="material" class="lsField__input" value="{material}">
  <label for="qty">Quantity</label>
  <input type="text" id="qty" name="qty" class="lsField__input" value="{qty}">
  <button id="btn_save" class="lsButton" title="Save" name="action" value="save">Save</button>
</form>
<div id="statusbar">{message}</div>
{modal_html}
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
        if "reset" in self.path:
            _counter["n"] = 4500001233
            self._send("<ok/>")
            return
        # GET always lands on the logon screen (the login plugin posts from
        # here); the transaction screen is only reached after that POST.
        self._send(screen() if "sess" in self.path else LOGIN)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode()) if length else {}
        vendor = (form.get("vendor") or [""])[0]
        material = (form.get("material") or [""])[0]
        qty = (form.get("qty") or [""])[0]
        okcd = (form.get("okcd") or [""])[0]

        # OK-code navigation (/nME21N) resets to a clean screen — this is
        # what per-row recovery relies on.
        if okcd:
            self._send(screen())
            return
        if "action" not in form:          # login post
            self._send(screen())
            return

        if vendor == BLOCKED_VENDOR:
            self._send(screen(
                f"Vendor {vendor} is blocked for purchasing",
                modal=True, vendor=vendor, material=material, qty=qty))
            return
        if not vendor:
            self._send(screen("Please enter a vendor",
                               vendor=vendor, material=material, qty=qty))
            return

        _counter["n"] += 1
        self._send(screen(
            f"Standard PO created under the number {_counter['n']}",
            vendor=vendor, material=material, qty=qty))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8094), H).serve_forever()
