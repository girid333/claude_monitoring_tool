"""D-053: what a WebGUI screen says about itself — read from the DOM.

eCATT and CBTA get three things from SAP GUI for free that a browser replay
has to work out from the page:

  * the SAP control ID of every element (wnd[0]/usr/ctxtFIELD) — WebGUI
    (Lightspeed) keeps it in each control's `lsdata` JSON, e.g.
    {"SID":"wnd[0]/sbar_msg","Type":"MESSAGEBAR","messageType":"E",...};
  * the status bar message and its TYPE (S/I/W/E/A) — the message bar is
    id="wnd[0]/sbar_msg" with `messageType` in its lsdata and an icon class
    lsMessageBar__icon--Error|Warning|Ok|Info|Stop|...;
  * whether a modal dialog is open, and its title — .lsPopupWindow / .urPW
    or an ARIA dialog.

All three were verified against F4H's real WebGUI markup (2026-10-06):
lsdata SIDs, the message bar structure and the icon class names come from
the page and the theme CSS the system serves. The JS here is shared by the
recorder (record time) and the resolver/engine (replay time) so both sides
read the screen the same way.
"""

from __future__ import annotations

import re
from typing import Any

# Containers whose SID is not an element the user acts on. A click inside a
# field must not be recorded as "click wnd[0]/usr".
CONTAINER_TYPES = [
    "GuiMainWindow", "GuiModalWindow", "GuiFrameWindow", "GuiUserArea",
    "GuiToolBar", "GuiToolbar", "FioriTitleBar", "GuiTitlebar", "FioriStatusBar",
    "GuiStatusbar", "MESSAGEBAR", "GuiTabStrip", "GuiSimpleContainer",
    "GuiScrollContainer", "GuiContainerShell", "GuiCustomControl", "GuiDockShell",
    "GuiSplitterShell", "GuiSplitter", "GuiShell", "GuiGridView", "GuiBox",
    "GuiTableControl", "GuiTree", "GuiMenubar", "GuiContextMenu", "GuiDialogShell",
]

POPUP_SELECTOR = ".lsPopupWindow, .urPW, [role='dialog'], [role='alertdialog']"

# Shared helpers, injected as a prefix into every snippet below.
_HELPERS = r"""
const __CONTAINERS = new Set(%CONTAINERS%);
const __POPUP_SEL = %POPUP%;
function __vis(e) {
  if (!e || !e.getClientRects) return false;
  if (!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)) return false;
  const s = getComputedStyle(e);
  return s.visibility !== 'hidden' && s.display !== 'none';
}
function __lsSid(el) {
  // {sid, type} from this element's own lsdata, or null.
  const raw = el && el.getAttribute && el.getAttribute('lsdata');
  if (!raw || raw.indexOf('"SID"') < 0) return null;
  try {
    const d = JSON.parse(raw);
    for (const k in d) {
      const v = d[k];
      if (v && typeof v === 'object' && typeof v.SID === 'string') return { sid: v.SID, type: v.Type || '', data: v };
    }
  } catch (e) {
    const m = raw.match(/"SID"\s*:\s*"([^"]+)"(?:\s*,\s*"Type"\s*:\s*"([^"]*)")?/);
    if (m) return { sid: m[1], type: m[2] || '', data: {} };
  }
  return null;
}
function __guiSid(el) {
  // The SAP control this element belongs to: its own lsdata or a close
  // ancestor's (Lightspeed sometimes puts lsdata on a wrapper). Containers
  // are skipped — they identify a region, not the element acted on.
  let cur = el;
  for (let d = 0; cur && cur.nodeType === 1 && d < 5; d++, cur = cur.parentElement) {
    const s = __lsSid(cur);
    if (s) {
      if (__CONTAINERS.has(s.type) || s.sid.indexOf('wnd[') !== 0) return null;
      return s;
    }
  }
  return null;
}
function __anySid(el) {
  let cur = el;
  for (let d = 0; cur && cur.nodeType === 1 && d < 12; d++, cur = cur.parentElement) {
    const s = __lsSid(cur); if (s) return s.sid;
  }
  return null;
}
const __TABLE_TYPES = new Set(['GuiTableControl', 'GuiShell', 'GuiGridView', 'GuiCtrlGridView']);
function __tableSid(table) {
  // Only the table's OWN control ID (or that of the grid control wrapping
  // it) — a container ID like wnd[0]/usr matches every table on the screen.
  let cur = table;
  for (let d = 0; cur && cur.nodeType === 1 && d < 4; d++, cur = cur.parentElement) {
    const s = __lsSid(cur);
    if (s) return (d === 0 || __TABLE_TYPES.has(s.type)) ? s.sid : null;
  }
  return null;
}
function __headersOf(table) {
  return [...table.querySelectorAll('th,[role="columnheader"]')].filter(h =>
    h.closest('table,[role="grid"],[role="table"],[role="treegrid"]') === table);
}
function __popups() {
  const all = [...document.querySelectorAll(__POPUP_SEL)].filter(__vis);
  // outermost only — Lightspeed nests several popup classes in one dialog
  return all.filter(p => !all.some(o => o !== p && o.contains(p)));
}
function __popupTitle(p) {
  if (!p) return '';
  const lb = p.getAttribute('aria-labelledby');
  const byId = lb ? document.getElementById(lb.split(' ')[0]) : null;
  const t = p.getAttribute('aria-label') || (byId && byId.innerText)
         || ((p.querySelector('[class*="Title"],[class*="title"],h1,h2,h3') || {}).innerText) || '';
  return t.trim().split('\n')[0].trim();
}
function __mainTitle() {
  const hdr = document.querySelector('[title_id]');
  if (hdr) {
    const t = document.getElementById(hdr.getAttribute('title_id'));
    if (t && (t.innerText || '').trim()) return t.innerText.trim().split('\n')[0].trim();
  }
  if ((document.title || '').trim()) return document.title.trim();
  const h = document.querySelector('h1');
  return h ? (h.innerText || '').trim() : '';
}
function __windowOf(el) {
  const s = __anySid(el);
  const m = s && s.match(/^wnd\[(\d+)\]/);
  if (m) return m[1] === '0' ? 'main' : 'wnd[' + m[1] + ']';
  const pops = __popups();
  return pops.some(p => p.contains(el)) ? 'popup' : 'main';
}
function __screen(el) {
  const pops = __popups();
  if (pops.length) {
    const p = el ? (pops.find(x => x.contains(el)) || null) : pops[pops.length - 1];
    if (p) return __popupTitle(p);
  }
  return __mainTitle();
}
function __tableOf(td) {
  const tr = td.closest('tr,[role="row"]');
  if (!tr) return null;
  const table = tr.closest('table,[role="grid"],[role="table"],[role="treegrid"]');
  if (!table || table.getAttribute('role') === 'presentation') return null;
  const heads = [...table.querySelectorAll('th,[role="columnheader"]')]
    .filter(h => h.closest('table,[role="grid"],[role="table"],[role="treegrid"]') === table);
  const isGrid = ['grid', 'table', 'treegrid'].indexOf(table.getAttribute('role') || '') >= 0;
  if (!heads.length && !isGrid) return null;   // a layout table, not a data table
  return { tr, table, heads };
}
function __rowsOf(table) {
  return [...table.querySelectorAll('tr,[role="row"]')].filter(r =>
    r.closest('table,[role="grid"],[role="table"],[role="treegrid"]') === table
    && r.querySelector('td,[role="gridcell"]'));
}
function __cellsOf(tr) {
  return [...tr.children].filter(c => c.matches('td,th,[role="gridcell"],[role="rowheader"]'));
}
function __cellOf(el) {
  const td = el.closest && el.closest('td,[role="gridcell"]');
  if (!td) return null;
  const t = __tableOf(td);
  if (!t) return null;
  const row = __rowsOf(t.table).indexOf(t.tr);
  const col = __cellsOf(t.tr).indexOf(td);
  if (row < 0 || col < 0) return null;
  const ac = td.getAttribute('aria-colindex');
  let head = null;
  if (ac) head = t.heads.find(h => h.getAttribute('aria-colindex') === ac) || null;
  if (!head) head = t.heads[col] || null;
  const tid = t.table.id && !/^M\d+:/.test(t.table.id) ? t.table.id : null;
  return {
    row, col,
    aria_row: t.tr.getAttribute('aria-rowindex'), aria_col: ac,
    column: head ? (head.innerText || '').trim().split('\n')[0] : null,
    table_sid: __tableSid(t.table), table_id: tid,
    headers: t.heads.slice(0, 8).map(h => (h.innerText || '').trim().split('\n')[0]).join('|'),
  };
}
function __valueOf(el) {
  if (!el) return null;
  const tag = (el.tagName || '').toLowerCase();
  if (tag === 'input' && (el.type === 'checkbox' || el.type === 'radio')) return el.checked;
  if (['input', 'select', 'textarea'].indexOf(tag) >= 0) return el.value;
  const aria = el.getAttribute && el.getAttribute('aria-checked');
  if (aria !== null && aria !== undefined) return aria === 'true';
  const inner = el.querySelector && el.querySelector('input,select,textarea');
  if (inner) return inner.value;
  return (el.innerText || '').trim();
}
function __statusbar() {
  const out = { type: '', text: '' };
  const bar = document.getElementById('wnd[0]/sbar_msg')
           || [...document.querySelectorAll('.lsMessageBar,[id*="msgBar"],[id="statusbar"],[role="status"]')].find(__vis);
  if (!bar) return out;
  const s = __lsSid(bar);
  const txt = document.getElementById((bar.id || '') + '-txt');
  out.text = ((txt ? txt.innerText : bar.innerText) || '').trim();
  if (s && s.data) {
    out.type = String(s.data.messageType || '').toUpperCase();
    if (!out.text && s.data.applicationText) out.text = String(s.data.applicationText).trim();
  }
  if (!out.type) {
    const icon = bar.querySelector('[class*="lsMessageBar__icon--"]') || bar;
    const cls = (icon.className && icon.className.baseVal !== undefined) ? icon.className.baseVal : (icon.className || '');
    const map = [['--Error', 'E'], ['--Stop', 'A'], ['--Warning', 'W'], ['--Alert', 'W'],
                 ['--Ok', 'S'], ['--Info', 'I'], ['--Notif', 'I']];
    for (const [k, v] of map) if (cls.indexOf('lsMessageBar__icon' + k) >= 0) { out.type = v; break; }
  }
  if (!out.text) out.type = '';
  return out;
}
"""


def _helpers() -> str:
    import json
    return (_HELPERS.replace("%CONTAINERS%", json.dumps(CONTAINER_TYPES))
            .replace("%POPUP%", json.dumps(POPUP_SELECTOR)))


HELPERS_JS = _helpers()

SCREEN_JS = "() => {" + HELPERS_JS + """
  const pops = __popups();
  return { title: __mainTitle(), popup: pops.length > 0,
           popup_title: pops.length ? __popupTitle(pops[pops.length - 1]) : '',
           popup_kind: pops.length ? ((pops[pops.length - 1].className || '').match(
               /lsPopupWindow--(error|warning|information|question|success)/) || [,''])[1] : '' };
}"""

STATUSBAR_JS = "() => {" + HELPERS_JS + " return __statusbar(); }"

# Marks the topmost open dialog so the resolver can scope a Locator to it.
MARK_POPUP_JS = "(mark) => {" + HELPERS_JS + """
  document.querySelectorAll('[data-sentinel-popup]').forEach(e => e.removeAttribute('data-sentinel-popup'));
  const pops = __popups();
  if (!pops.length) return false;
  pops[pops.length - 1].setAttribute('data-sentinel-popup', mark);
  return true;
}"""

_ACTIONABLE = ("input:not([type=hidden]),select,textarea,button,a,[role='button'],[role='checkbox'],"
               "[role='radio'],[role='tab'],[role='textbox'],[role='combobox'],[role='link']")

# Find the element carrying a SAP control ID and mark it (or its actionable
# child — lsdata can sit on a wrapper around the real <input>).
MARK_SID_JS = "([sid, mark, scope]) => {" + HELPERS_JS + """
  const root = scope ? (document.querySelector('[data-sentinel-popup="' + scope + '"]') || document) : document;
  // Exact SID match on the parsed lsdata (a substring test would let
  // wnd[0]/usr/txtA also match wnd[0]/usr/txtAB).
  const hits = [...root.querySelectorAll('[lsdata]')].filter(e => {
    const raw = e.getAttribute('lsdata') || '';
    if (raw.indexOf(sid) < 0) return false;
    const s = __lsSid(e);
    return !!s && s.sid === sid;
  });
  let el = hits.find(__vis) || null;
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  const own = ['input', 'select', 'textarea', 'button', 'a'].indexOf(tag) >= 0 || el.getAttribute('role');
  if (!own) {
    const inner = [...el.querySelectorAll(%ACT%)].find(__vis);
    if (inner) el = inner;
  }
  document.querySelectorAll('[data-sentinel-hit]').forEach(e => e.removeAttribute('data-sentinel-hit'));
  el.setAttribute('data-sentinel-hit', mark);
  return true;
}""".replace("%ACT%", repr(_ACTIONABLE))

# Positional table cell lookup (eCATT style: row/column, never cell text).
MARK_CELL_JS = "([cell, mark, scope]) => {" + HELPERS_JS + """
  const root = scope ? (document.querySelector('[data-sentinel-popup="' + scope + '"]') || document) : document;
  let tables = [...root.querySelectorAll('table,[role="grid"],[role="table"],[role="treegrid"]')].filter(__vis);
  const sig = t => __headersOf(t).slice(0, 8).map(h => (h.innerText || '').trim().split('\\n')[0]).join('|');
  let cands = [];
  if (cell.table_id) cands = tables.filter(t => t.id === cell.table_id);
  if (!cands.length && cell.table_sid) cands = tables.filter(t => __tableSid(t) === cell.table_sid);
  // Several candidates (or none by id): the column headers decide.
  if (cell.headers) {
    const byHead = (cands.length ? cands : tables).filter(t => sig(t) === cell.headers);
    if (byHead.length || !cands.length) cands = byHead;
  }
  for (const t of cands) {
    let tr = null;
    if (cell.aria_row) tr = [...t.querySelectorAll('[aria-rowindex="' + cell.aria_row + '"]')].find(r =>
        r.closest('table,[role="grid"],[role="table"],[role="treegrid"]') === t) || null;
    if (!tr) tr = __rowsOf(t)[cell.row] || null;
    if (!tr) continue;
    let td = null;
    if (cell.aria_col) td = __cellsOf(tr).find(c => c.getAttribute('aria-colindex') === cell.aria_col) || null;
    if (!td) td = __cellsOf(tr)[cell.col] || null;
    if (!td) continue;
    if (cell.column) {
      // the cell must sit under the recorded column header
      const heads = __headersOf(t);
      const ac = td.getAttribute('aria-colindex');
      const head = (ac && heads.find(h => h.getAttribute('aria-colindex') === ac)) || heads[__cellsOf(tr).indexOf(td)];
      if (head && (head.innerText || '').trim().split('\\n')[0] !== cell.column) continue;
    }
    document.querySelectorAll('[data-sentinel-hit]').forEach(e => e.removeAttribute('data-sentinel-hit'));
    td.setAttribute('data-sentinel-hit', mark);
    return true;
  }
  return false;
}"""

# Read a data table / ALV grid into rows keyed by column header.
READ_TABLE_JS = "(el) => {" + HELPERS_JS + """
  let table = el.closest('table,[role="grid"],[role="table"],[role="treegrid"]');
  if (!table || table.getAttribute('role') === 'presentation' || !__rowsOf(table).length) {
    const inner = [...el.querySelectorAll('table,[role="grid"],[role="table"],[role="treegrid"]')]
      .filter(t => t.getAttribute('role') !== 'presentation')
      .sort((a, b) => __rowsOf(b).length - __rowsOf(a).length);
    table = inner[0] || table;
  }
  if (!table) return { headers: [], rows: [] };
  const heads = [...table.querySelectorAll('th,[role="columnheader"]')].filter(h =>
    h.closest('table,[role="grid"],[role="table"],[role="treegrid"]') === table);
  const names = heads.map((h, i) => (h.innerText || '').trim().split('\\n')[0] || ('col' + (i + 1)));
  const byAria = {};
  heads.forEach((h, i) => { const a = h.getAttribute('aria-colindex'); if (a) byAria[a] = names[i]; });
  const rows = [];
  for (const tr of __rowsOf(table)) {
    const cells = __cellsOf(tr);
    if (!cells.length) continue;
    const r = {};
    cells.forEach((c, i) => {
      const a = c.getAttribute('aria-colindex');
      const k = (a && byAria[a]) || names[i] || ('col' + (i + 1));
      const inp = c.querySelector('input,select,textarea');
      r[k] = ((inp ? inp.value : c.innerText) || '').trim();
    });
    if (Object.values(r).some(v => v !== '')) rows.push(r);
  }
  return { headers: names, rows };
}"""

VALUE_JS = "(el) => {" + HELPERS_JS + " return __valueOf(el); }"


def normalize_title(title: str | None) -> str:
    """Screen titles carry data ("Display PO 4500001234"); compare shapes."""
    t = re.sub(r"\d+", "#", (title or "").strip().lower())
    return re.sub(r"\s+", " ", t)


async def _first_frame_result(page, js: str, arg: Any = None, want=lambda r: bool(r)):
    last = None
    for frame in page.frames:
        try:
            r = await (frame.evaluate(js, arg) if arg is not None else frame.evaluate(js))
        except Exception:  # noqa: BLE001,PERF203 - detached/cross-origin frame
            continue
        last = r if last is None else last
        if want(r):
            return r, frame
    return last, None


async def read_statusbar(page) -> dict[str, str]:
    r, _ = await _first_frame_result(page, STATUSBAR_JS, want=lambda r: bool(r and r.get("text")))
    return r or {"type": "", "text": ""}


async def read_screen(page) -> dict[str, Any]:
    r, _ = await _first_frame_result(page, SCREEN_JS,
                                     want=lambda r: bool(r and (r.get("title") or r.get("popup"))))
    return r or {"title": "", "popup": False, "popup_title": "", "popup_kind": ""}
