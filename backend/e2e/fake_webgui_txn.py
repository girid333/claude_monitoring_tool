"""D-053: an ADVERSARIAL WebGUI transaction, built from F4H's real markup.

What it copies from the real system (verified 2026-10-06 on F4H):
  * every control carries its SAP GUI ID in `lsdata` JSON
    ({"SID":"wnd[0]/usr/ctxtEKKO-LIFNR","Type":"GuiCTextField"});
  * the message bar is id="wnd[0]/sbar_msg" with messageType in lsdata, an
    lsMessageBar__icon--Error|Warning|Ok icon and a "-txt" text span;
  * the window title lives in the element named by the header's title_id;
  * positional ids (M0:46) — here they CHANGE on every roundtrip, so a replay
    that leans on them breaks, as it does after a real layout change.

What makes it hard, on purpose:
  * roundtrips are AJAX: the server answers after 400 ms and the busy layer
    only appears after 150 ms, so "no busy layer" is briefly true right after
    a click (the race the old wait had);
  * Enter is read from the fields at keydown time — Enter before the value
    is in the field means "Fill in all required entry fields";
  * a decoy "Continue as Draft" button next to "Continue", contract numbers
    10 / 100 / 1000 in the same grid, and a "Yes" button on the main screen
    behind the save dialog;
  * a "Large Quantity" dialog that only appears when a quantity is > 100;
  * /fault/popup makes a surprise "System Message" dialog cover the screen.
"""
import json
import re
import sys
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8089

LOGIN = """<html><body><form method="post" action="/webgui">
<input type="text" name="sap-user"><input type="password" name="sap-password">
<input type="submit" id="sapwp_BUTTON_LOGIN" value="Log On"></form></body></html>"""


def fresh():
    return {"screen": "home", "fields": {}, "popup": None, "msg": ("", ""), "gen": 0,
            "pos": {}, "counter": 4500001233, "vendor": "", "large_ok": False,
            "contract": "", "fault_popup": False, "items": [], "display": None,
            "log": []}


S = fresh()

ITEM_ROWS = 3
CONTRACTS = [("10", "Frame contract steel"), ("100", "Frame contract copper"),
             ("1000", "Frame contract services")]


def ls(sid, typ, extra=None):
    d = {"0": "x", "9": {"SID": sid, "Type": typ, **(extra or {})}}
    return escape(json.dumps(d, separators=(",", ":")), quote=True)


class Ids:
    def __init__(self, gen):
        self.gen, self.n = gen, 0

    def __call__(self):
        self.n += 1
        return f"M{self.gen}:{self.n}"


def field(ids, label, sid, value, typ="GuiCTextField"):
    return (f'<tr><td><span class="lsLabel">{label}</span></td><td>'
            f'<input type="text" id="{ids()}" class="lsField__input" aria-label="Input Field" '
            f'lsdata=\'{ls(sid, typ)}\' value="{escape(value, quote=True)}"></td></tr>')


def button(ids, text, sid, fcode, title=""):
    return (f'<div role="button" id="{ids()}" class="lsButton" tabindex="0" data-fcode="{fcode}" '
            f'title="{escape(title or text)}" lsdata=\'{ls(sid, "GuiButton")}\' '
            f'style="display:inline-block;padding:4px 10px;margin:2px;border:1px solid #888;cursor:pointer">'
            f'<span class="lsButton__text">{text}</span></div>')


def screen_html():
    ids = Ids(S["gen"])
    f = S["fields"]
    sc = S["screen"]
    if sc == "initial":
        title = "Create Purchase Order: Initial Screen"
        body = ('<table role="presentation">'
                + field(ids, "Order Type", "wnd[0]/usr/ctxtEKKO-BSART", f.get("wnd[0]/usr/ctxtEKKO-BSART", "NB"))
                + field(ids, "Vendor", "wnd[0]/usr/ctxtEKKO-LIFNR", f.get("wnd[0]/usr/ctxtEKKO-LIFNR", ""))
                + "</table>"
                + button(ids, "Continue as Draft", "wnd[0]/tbar[1]/btn[6]", "DRAFT")
                + button(ids, "Continue", "wnd[0]/tbar[1]/btn[5]", "ENTE", "Continue (Enter)"))
    elif sc == "items":
        title = f"Create Purchase Order: Vendor {S['vendor']}"
        rows = []
        for r in range(ITEM_ROWS):
            m = S["items"][r][0] if r < len(S["items"]) else ""
            q = S["items"][r][1] if r < len(S["items"]) else ""
            rows.append(
                f'<tr><td>{r + 1}</td>'
                f'<td><input type="text" id="{ids()}" class="lsField__input" aria-label="Input Field" '
                f'lsdata=\'{ls(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/ctxtITEM-MATNR[0,{r}]", "GuiCTextField")}\' value="{escape(m)}"></td>'
                f'<td><input type="text" id="{ids()}" class="lsField__input" aria-label="Input Field" '
                f'lsdata=\'{ls(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/txtITEM-MENGE[1,{r}]", "GuiTextField")}\' value="{escape(q)}"></td></tr>')
        grid = "".join(
            f'<tr aria-rowindex="{i + 2}"><td aria-colindex="1" data-dbl="{c}">{c}</td>'
            f'<td aria-colindex="2" data-dbl="{c}">{d}</td></tr>' for i, (c, d) in enumerate(CONTRACTS))
        urgent = "true" if f.get("wnd[0]/usr/chkEKKO-URGENT") else "false"
        body = (button(ids, "Yes", "wnd[0]/tbar[1]/btn[20]", "WRONGYES")
                + f'<span role="checkbox" id="{ids()}" aria-checked="{urgent}" tabindex="0" class="lsCheckBox" '
                  f'lsdata=\'{ls("wnd[0]/usr/chkEKKO-URGENT", "GuiCheckBox")}\' '
                  f'style="display:inline-block;width:14px;height:14px;border:1px solid #333"></span> Urgent'
                + f'<table id="{ids()}" lsdata=\'{ls("wnd[0]/usr/tblSAPMZPOTC_ITEMS", "GuiTableControl")}\'>'
                  '<tr><th>Item</th><th>Material</th><th>Quantity</th></tr>' + "".join(rows) + "</table>"
                + '<p>Contracts</p>'
                + f'<table role="grid" id="{ids()}" lsdata=\'{ls("wnd[0]/usr/cntlGRID/shellcont/shell", "GuiShell")}\'>'
                  '<tr aria-rowindex="1"><th aria-colindex="1">Contract</th><th aria-colindex="2">Description</th></tr>'
                + grid + "</table>")
    elif sc == "contract":
        title = f"Display Contract {S['contract']}"
        body = f'<p id="{ids()}">Contract {escape(S["contract"])} details</p>'
    elif sc == "display":
        title = "Display Purchase Order"
        body = ('<table role="presentation">'
                + field(ids, "Purchase Order", "wnd[0]/usr/ctxtPO-EBELN", f.get("wnd[0]/usr/ctxtPO-EBELN", ""))
                + "</table>")
        if S["display"]:
            po = S["pos"][S["display"]]
            lines = "".join(f'<tr><td>{escape(m)}</td><td>{escape(q)}</td></tr>' for m, q in po["items"])
            body += (f'<div>Vendor <input type="text" id="{ids()}" readonly class="lsField__input" '
                     f'lsdata=\'{ls("wnd[0]/usr/txtPO-LIFNR", "GuiTextField")}\' value="{escape(po["vendor"])}"></div>'
                     f'<table role="grid" id="{ids()}" lsdata=\'{ls("wnd[0]/usr/cntlITEMS/shellcont/shell", "GuiShell")}\'>'
                     f'<tr><th>Material</th><th>Quantity</th></tr>{lines}</table>')
    else:
        title, body = "SAP Easy Access", "<p>Welcome</p>"

    popup = ""
    if S["popup"]:
        kind = S["popup"]
        if kind == "large":
            ptitle, ptext = "Large Quantity", "A quantity above 100 was entered."
            btns = button(ids, "Continue", "wnd[1]/tbar[0]/btn[0]", "LARGE_OK")
        elif kind == "save":
            ptitle, ptext = "Save Document", "Do you want to save the purchase order?"
            btns = (button(ids, "Yes", "wnd[1]/usr/btnSPOP-OPTION1", "SAVE_YES")
                    + button(ids, "No", "wnd[1]/usr/btnSPOP-OPTION2", "SAVE_NO"))
        else:
            ptitle, ptext = "System Message", "The system will be shut down at 18:00."
            btns = button(ids, "Acknowledge", "wnd[1]/tbar[0]/btn[0]", "SYSMSG_OK")
        tid = ids()
        popup = ('<div style="position:fixed;left:0;top:0;right:0;bottom:0;z-index:90;background:rgba(0,0,0,.25)"></div>'
                 f'<div class="lsPopupWindow lsPopupWindow--dialog" role="dialog" aria-labelledby="{tid}" '
                 f'lsdata=\'{ls("wnd[1]", "GuiModalWindow")}\' '
                 'style="position:fixed;top:140px;left:320px;z-index:100;background:#fff;border:1px solid #333;padding:14px">'
                 f'<div id="{tid}" class="lsPopupWindow__title">{ptitle}</div><p>{ptext}</p>{btns}</div>')

    mtype, mtext = S["msg"]
    icon = {"E": "Error", "W": "Warning", "S": "Ok", "I": "Info", "A": "Stop"}.get(mtype, "Text")
    msg = (f'<div id="wnd[0]/sbar_msg" class="lsMessageBar" '
           f'lsdata=\'{escape(json.dumps({"x": 0, "11": {"SID": "wnd[0]/sbar_msg", "Type": "MESSAGEBAR", "messageType": mtype, "applicationText": ""}}, separators=(",", ":")), quote=True)}\'>'
           f'<span id="wnd[0]/sbar_msg-icon" class="lsMessageBar__icon lsMessageBar__icon--{icon}"></span>'
           f'<span id="wnd[0]/sbar_msg-txt" class="lsMessageBar__text">{escape(mtext)}</span></div>')
    return {"title": title, "usr": body, "popup": popup, "msg": msg}


SHELL = """<html><head><title></title></head><body class="lsPage">
<div id="cuaheader" title_id="M0:35" lsdata='{"5":{"SID":"wnd[0]/titl","Type":"FioriTitleBar"}}'><span id="M0:35"></span></div>
<div class="lsBlockLayer" id="block" style="display:none;position:fixed;left:0;top:0;right:0;bottom:0;z-index:200"></div>
<div lsdata='{"5":{"SID":"wnd[0]/tbar[0]","Type":"GuiToolBar"}}'>
 <input type="text" title="Command Field" id="okcd" lsdata='{"0":"x","9":{"SID":"wnd[0]/tbar[0]/okcd","Type":"GuiOkCodeField"}}'>
 <div role="button" id="tb_save" title="Save (Ctrl+S)" data-fcode="SAVE" lsdata='{"16":{"SID":"wnd[0]/tbar[0]/btn[11]","Type":"GuiButton"}}' style="display:inline-block;padding:4px;border:1px solid #888">Save</div>
 <div role="button" id="tb_back" title="Back (F3)" data-fcode="BACK" lsdata='{"16":{"SID":"wnd[0]/tbar[0]/btn[3]","Type":"GuiButton"}}' style="display:inline-block;padding:4px;border:1px solid #888">Back</div>
</div>
<div id="usr" lsdata='{"3":{"SID":"wnd[0]/usr","Type":"GuiUserArea"}}'></div>
<div id="msgarea"></div>
<div id="popups"></div>
<script>
const $ = id => document.getElementById(id);
function render(j) {
  $('M0:35').textContent = j.title; $('usr').innerHTML = j.usr;
  $('msgarea').innerHTML = j.msg; $('popups').innerHTML = j.popup;
  $('okcd').value = '';                       // SAP clears the command field every roundtrip
}
function sidOf(e) {
  while (e && e.getAttribute) {
    const m = (e.getAttribute('lsdata') || '').match(/"SID":"([^"]+)"/);
    if (m) return m[1]; e = e.parentElement;
  }
  return null;
}
function collect() {
  const f = {};
  document.querySelectorAll('[lsdata]').forEach(e => {
    const m = (e.getAttribute('lsdata') || '').match(/"SID":"([^"]+)"/);
    if (!m) return;
    if (e.tagName === 'INPUT') f[m[1]] = e.value;
    else if (e.getAttribute('role') === 'checkbox') f[m[1]] = e.getAttribute('aria-checked') === 'true';
  });
  return f;
}
let busy = false;
async function rt(action) {
  if (busy) return; busy = true;
  const t = setTimeout(() => { $('block').style.display = 'block'; }, 150);   // busy layer comes LATE
  try {
    const r = await fetch('/delta', { method: 'POST', body: JSON.stringify({ action, fields: collect() }) });
    render(await r.json());
  } finally { clearTimeout(t); $('block').style.display = 'none'; busy = false; }
}
document.addEventListener('keydown', e => {
  let k = e.key;
  if ((e.ctrlKey || e.metaKey) && (k === 's' || k === 'S')) k = 'Control+S';
  else if (e.shiftKey && /^F\\d+$/.test(k)) k = 'Shift+' + k;
  if (['Enter', 'F3', 'F8', 'Shift+F5', 'Control+S', 'Escape'].indexOf(k) < 0) return;
  e.preventDefault();
  rt({ key: k, focus: sidOf(document.activeElement) });
});
document.addEventListener('click', e => {
  const cb = e.target.closest('[role=checkbox]');
  if (cb) { cb.setAttribute('aria-checked', cb.getAttribute('aria-checked') === 'true' ? 'false' : 'true'); return; }
  const b = e.target.closest('[data-fcode]'); if (b) rt({ fcode: b.dataset.fcode });
});
document.addEventListener('dblclick', e => {
  const td = e.target.closest('[data-dbl]'); if (td) rt({ dbl: td.dataset.dbl });
});
render(%INITIAL%);
</script></body></html>"""


def act(action, fields):
    S["log"].append(action)
    S["fields"].update(fields or {})
    S["gen"] += 1
    f = S["fields"]
    S["msg"] = ("", "")
    key, fcode, dbl = action.get("key"), action.get("fcode"), action.get("dbl")
    okcd = (f.get("wnd[0]/tbar[0]/okcd") or "").strip()
    if key == "Enter" and okcd:
        f["wnd[0]/tbar[0]/okcd"] = ""
        goto(okcd.lstrip("/").lstrip("nN").upper())
        return
    if S["popup"]:
        p = S["popup"]
        if p == "large" and fcode == "LARGE_OK":
            S["large_ok"] = True
            S["popup"] = "save"
        elif p == "save" and fcode == "SAVE_YES":
            S["popup"] = None
            save()
        elif p == "save" and fcode == "SAVE_NO":
            S["popup"] = None
        elif p == "sysmsg" and fcode == "SYSMSG_OK":
            S["popup"] = None
        return                                   # a modal swallows everything else
    sc = S["screen"]
    if fcode == "WRONGYES":
        S["msg"] = ("E", "Wrong 'Yes' pressed on the main screen")
        return
    if sc == "initial":
        if key == "Shift+F5":
            S["screen"], S["display"] = "display", None
            return
        if fcode == "DRAFT":
            S["msg"] = ("W", "Draft mode is not available")
            return
        if key == "Enter" or fcode == "ENTE":
            v = (f.get("wnd[0]/usr/ctxtEKKO-LIFNR") or "").strip()
            if not v:
                S["msg"] = ("E", "Fill in all required entry fields")
            elif v == "200000":
                S["msg"] = ("E", f"Vendor {v} is blocked for purchasing")
            else:
                S["vendor"], S["screen"], S["large_ok"] = v, "items", False
                S["items"] = []
                S["popup"] = "sysmsg" if S["fault_popup"] else None
            return
    if sc == "items":
        S["items"] = [((f.get(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/ctxtITEM-MATNR[0,{r}]") or "").strip(),
                       (f.get(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/txtITEM-MENGE[1,{r}]") or "").strip())
                      for r in range(ITEM_ROWS)]
        if dbl:
            S["contract"], S["screen"] = dbl, "contract"
            return
        if key == "F3" or fcode == "BACK":
            S["screen"] = "initial"
            return
        if key == "Control+S" or fcode == "SAVE":
            items = [(m, q) for m, q in S["items"] if m]
            if not items:
                S["msg"] = ("E", "Enter at least one item")
                return
            big = any(q.isdigit() and int(q) > 100 for _, q in items)
            S["popup"] = "large" if big and not S["large_ok"] else "save"
            return
        return
    if sc == "contract" and (key == "F3" or fcode == "BACK"):
        S["screen"] = "items"
        return
    if sc == "display":
        if key == "Enter":
            n = (f.get("wnd[0]/usr/ctxtPO-EBELN") or "").strip()
            if n in S["pos"]:
                S["display"] = n
                S["msg"] = ("S", f"Purchase order {n} displayed")
            else:
                S["display"] = None
                S["msg"] = ("E", f"Purchase order {n} does not exist")
            return
        if key == "F3":
            S["screen"] = "initial"


def save():
    S["counter"] += 1
    n = str(S["counter"])
    S["pos"][n] = {"vendor": S["vendor"], "items": [(m, q) for m, q in S["items"] if m],
                   "urgent": bool(S["fields"].get("wnd[0]/usr/chkEKKO-URGENT"))}
    S["screen"], S["items"], S["large_ok"] = "initial", [], False
    S["fields"]["wnd[0]/usr/ctxtEKKO-LIFNR"] = ""
    S["fields"]["wnd[0]/usr/chkEKKO-URGENT"] = False
    for r in range(ITEM_ROWS):
        S["fields"].pop(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/ctxtITEM-MATNR[0,{r}]", None)
        S["fields"].pop(f"wnd[0]/usr/tblSAPMZPOTC_ITEMS/txtITEM-MENGE[1,{r}]", None)
    S["msg"] = ("S", f"Standard PO created under the number {n}")


def goto(tc):
    S["popup"], S["display"] = None, None
    S["screen"] = {"ZPO": "initial", "ZPODISP": "display"}.get(tc, "home")
    if tc not in ("ZPO", "ZPODISP"):
        S["msg"] = ("E", f"Transaction {tc} does not exist")


class H(BaseHTTPRequestHandler):
    def _send(self, body: str, ctype="text/html") -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _shell(self):
        return SHELL.replace("%INITIAL%", json.dumps(screen_html()))

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/reset":
            S.clear(); S.update(fresh()); return self._send("{}", "application/json")
        if u.path == "/state":
            return self._send(json.dumps({k: S[k] for k in ("screen", "pos", "msg", "popup", "log")}),
                              "application/json")
        if u.path == "/fault/popup":
            S["fault_popup"] = True; return self._send("{}", "application/json")
        q = parse_qs(u.query)
        if "~transaction" in q:
            goto(q["~transaction"][0].upper().lstrip("*").split()[0])
            return self._send(self._shell())
        self._send(LOGIN)

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode() if n else ""
        if self.path.startswith("/delta"):
            import time
            time.sleep(0.4)                      # SAP takes a moment
            body = json.loads(raw or "{}")
            act(body.get("action") or {}, body.get("fields") or {})
            return self._send(json.dumps(screen_html()), "application/json")
        # login post → SAP Easy Access
        S["screen"], S["popup"] = "home", None
        self._send(self._shell())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", PORT), H).serve_forever()
