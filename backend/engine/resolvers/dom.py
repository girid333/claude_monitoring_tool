"""DomResolver — works on scripting-disabled systems (D-003, core deliverable).

Ranking per D-012, mirroring v1's proven selector priority:
  tier 1  attr         confidence 0.95   name/title/id-substring attributes
  tier 2  label/role   confidence 0.85   Playwright get_by_role / get_by_text
  tier 3  css_hint     confidence 0.65   SAP UI-library class fragment
  tier 4  pos_id       confidence 0.40   positional WebGUI id (flagged)

Any resolution below CONFIDENCE_FLAG_THRESHOLD (0.75) is flagged in the
report per D-003 — tiers 3-4 always flag.

Phase 1 scope: tiers 1-2 implemented; tier 3-4 stubs raise until built.
Tree-widget resolution (ST03N/SM12-style): port v1's parent-walk logic
(sap_automator.py: st03n_click_tree_node :242, click_tree_node :949) here
as a dedicated strategy — do not reinvent it.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from playwright.async_api import Locator

from ..schema import Target
from ..webgui_dom import (MARK_CELL_JS, MARK_POPUP_JS, MARK_SID_JS, READ_TABLE_JS,
                          VALUE_JS, read_screen, read_statusbar)
from .base import Resolution, ResolveError, Resolver


class UnexpectedDialog(ResolveError):
    """D-053: the step belongs to the main screen but a dialog is covering it.
    eCATT stops here too — typing "behind" a modal never does what was
    recorded, and the click usually lands on the dialog instead."""

    def __init__(self, target: Target, title: str):
        self.title = title
        super().__init__(target, tried=[f"unexpected dialog open: {title or '(untitled)'}"])


class DomResolver(Resolver):
    name = "dom"

    async def _scope(self, target: Target):
        """Return (root, scope_mark). Steps recorded inside a dialog are
        looked up inside the dialog that is open now; steps recorded on the
        main screen refuse to run while an unrecorded dialog is open."""
        dom = target.dom
        window = dom.window if dom else None
        mark = uuid.uuid4().hex[:8]
        has_popup = False
        popup_frame = None
        for frame in self.page.frames:
            try:
                if await frame.evaluate(MARK_POPUP_JS, mark):
                    has_popup, popup_frame = True, frame
                    break
            except Exception:  # noqa: BLE001,PERF203
                continue
        wants_popup = bool(window) and window not in ("main", "wnd[0]")
        if not window and target.sid:
            wants_popup = not target.sid.startswith("wnd[0]")
            window = "main" if not wants_popup else "popup"
        if wants_popup:
            if not has_popup:
                raise ResolveError(target, tried=["recorded inside a dialog, but no dialog is open"])
            return popup_frame.locator(f'[data-sentinel-popup="{mark}"]'), mark
        if window in ("main", "wnd[0]") and has_popup:
            title = (await read_screen(self.page)).get("popup_title", "")
            raise UnexpectedDialog(target, title)
        return self.page, None

    async def _by_mark(self, js: str, arg: Any) -> Locator | None:
        for frame in self.page.frames:
            try:
                if await frame.evaluate(js, arg):
                    return frame.locator(f'[data-sentinel-hit="{arg[1]}"]').first
            except Exception:  # noqa: BLE001,PERF203
                continue
        return None

    async def resolve(self, target: Target) -> Resolution:
        tried: list[str] = []
        dom = target.dom
        root, scope = await self._scope(target)

        # Tier S — D-053: the SAP control ID from WebGUI's own lsdata. This
        # is the identity eCATT/CBTA record (wnd[0]/usr/ctxtFIELD); it does
        # not depend on labels, layout, language or theme.
        if target.sid:
            loc = await self._by_mark(MARK_SID_JS, [target.sid, uuid.uuid4().hex[:10], scope])
            tried.append(f"sap_id:{target.sid}")
            if loc is not None and await self._usable(loc):
                return Resolution(loc, 0.98, f"sap_id:{target.sid}", self.name)

        if dom is None:
            raise ResolveError(target, tried=tried or ["<no dom candidates recorded>"])

        # Tier 0 — D-040/D-043: positional ARIA-toggle match. SAP renders
        # checkboxes AND radio buttons as ARIA spans (role="checkbox"/
        # "radio") that usually have no distinguishing name/title, so the
        # generic attr/label chain below tends to either miss them or,
        # worse, match the SAME element for every widget on the screen.
        # Position among all same-role elements is what's actually stable —
        # ported from v1's SM37 job-overview fix
        # (get_by_role("checkbox").nth(i)) and generalized.
        if dom.role in ("checkbox", "radio") and dom.checkbox_index is not None:
            loc = root.locator(f'[role="{dom.role}"]').nth(dom.checkbox_index)
            tried.append(f"{dom.role}_index:{dom.checkbox_index}")
            if await self._usable(loc):
                return Resolution(loc, 0.9, f"{dom.role}_index:{dom.checkbox_index}", self.name)

        # Tier C — D-053: table cell by POSITION (row/column), the way eCATT
        # records table controls. The cell's text is data: matching on it
        # picked the first cell CONTAINING "10" (so "100" too) and broke as
        # soon as the list content changed.
        if dom.cell:
            loc = await self._by_mark(MARK_CELL_JS, [dom.cell, uuid.uuid4().hex[:10], scope])
            c = dom.cell
            tried.append(f"cell:r{c.get('row')}c{c.get('col')}")
            if loc is not None and await self._usable(loc):
                return Resolution(loc, 0.9, f"cell:row {c.get('row')}, "
                                  f"{c.get('column') or 'col ' + str(c.get('col'))}", self.name)

        # Tier 1 — semantic attributes. D-042: `nth` was captured
        # (nthAmongSimilar) specifically to disambiguate when the same
        # attr value matches multiple elements.
        if dom.attr:
            selector = "".join(f'[{k}*="{v}"]' if k == "id" else f'[{k}="{v}"]'
                               for k, v in dom.attr.items())
            idx0 = dom.nth or 0
            loc = root.locator(selector).nth(idx0)
            tried.append(f"attr:{selector}#{idx0}")
            if await self._usable(loc):
                return Resolution(loc, 0.95, f"attr:{selector}#{idx0}", self.name)

        # Tier 2 — role + accessible name / visible label (D-042: nth).
        idx = dom.nth or 0
        if dom.role and dom.label:
            cand = root.get_by_role(dom.role, name=dom.label).nth(idx)
            tried.append(f"role:{dom.role}+label:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.85, f"role:{dom.role}:{dom.label}#{idx}", self.name)
        if dom.label:
            cand = root.get_by_label(dom.label).nth(idx)
            tried.append(f"label:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.85, f"label:{dom.label}#{idx}", self.name)
            cand = root.get_by_text(dom.label, exact=True).nth(idx)
            tried.append(f"text:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.80, f"text:{dom.label}#{idx}", self.name)

        # Tier 2b — visible text of the element itself. D-053: EXACT first —
        # `has_text` is a substring match, so "Save" also hit "Save as
        # Variant" and a cell "10" hit "100". A substring hit is still tried
        # last, but flagged as low confidence.
        if dom.text:
            tag = dom.tag or "*"
            exact = re.compile(r"^\s*" + re.escape(dom.text) + r"\s*$")
            loc = root.locator(tag).filter(has_text=exact).first
            tried.append(f"tag+text:{tag}:{dom.text}")
            if await self._usable(loc):
                return Resolution(loc, 0.78, f"tag+text:{tag}:{dom.text}", self.name)
            loc = root.get_by_text(dom.text, exact=True).first
            tried.append(f"text:{dom.text}")
            if await self._usable(loc):
                return Resolution(loc, 0.75, f"text:{dom.text}", self.name)

        # D-053: when the SAP control ID was recorded and is NOT on the screen,
        # the weak tiers below (class fragment, substring text, positional id,
        # DOM path) only find "something that looks similar" — on a dialog
        # that means the FIRST button, i.e. possibly "Yes" instead of the
        # recorded "Continue". eCATT stops there; so do we.
        if target.sid and dom.sid_type:
            raise ResolveError(target, tried)

        # Tier 3 — D-033: SAP UI-library class fragment, disambiguated by tag.
        if dom.css_hint:
            tag = dom.tag or "*"
            sel = f'{tag}.{dom.css_hint}'
            if dom.input_type:
                sel += f'[type="{dom.input_type}"]'
            loc = root.locator(sel)
            tried.append(f"css_hint:{sel}")
            idx = dom.nth or 0
            try:
                if await loc.count() > idx:
                    cand = loc.nth(idx)
                    if await self._usable(cand):
                        return Resolution(cand, 0.60, f"css_hint:{sel}#{idx}", self.name)
            except Exception:  # noqa: BLE001
                pass

        if dom.text:
            tag = dom.tag or "*"
            loc = root.locator(tag, has_text=dom.text).first
            tried.append(f"tag+text~:{tag}:{dom.text}")
            if await self._usable(loc):
                return Resolution(loc, 0.55, f"tag+contains-text:{tag}:{dom.text}", self.name)

        # Tier 4 — pos_id: exact positional WebGUI id. Always low confidence.
        if dom.pos_id:
            loc = root.locator(f'[id="{dom.pos_id}"]').first
            tried.append(f"pos_id:{dom.pos_id}")
            if await self._usable(loc):
                return Resolution(loc, 0.40, f"pos_id:{dom.pos_id}", self.name)

        # Tier 5 — structural CSS path. Last resort, always flagged.
        if dom.path:
            loc = root.locator(dom.path).first
            tried.append(f"path:{dom.path}")
            if await self._usable(loc):
                return Resolution(loc, 0.35, f"path:{dom.path}", self.name)

        raise ResolveError(target, tried)

    async def _usable(self, loc: Locator) -> bool:
        try:
            return await loc.count() > 0 and await loc.first.is_visible(timeout=200)
        except Exception:
            return False

    async def set_value(self, res: Resolution, value: Any) -> None:
        loc: Locator = await self._editable(res.handle)
        if isinstance(value, bool):
            await self._set_checkbox(loc, value)
            return
        # D-043: native <select> dropdowns (SAP does use real selects for
        # some fields, e.g. SM37's "Or after event") need select_option —
        # fill() throws on a select, and the JS fallback set the value
        # without SAP's own change handling seeing a proper selection.
        try:
            tag = await loc.evaluate("el => el.tagName.toLowerCase()")
        except Exception:  # noqa: BLE001
            tag = ""
        if tag == "select":
            try:
                await loc.select_option(value=str(value))
            except Exception:  # noqa: BLE001 — value may be the visible label
                await loc.select_option(label=str(value))
            return
        # v1 lesson (§6.8): JS-injected value beats .fill() on some SAP
        # inputs with CSS visibility tricks. Try fill, fall back to JS.
        try:
            await loc.fill(str(value), timeout=2000)
        except Exception:
            await loc.evaluate(
                "(el, v) => { el.value = v; el.dispatchEvent(new Event('change', {bubbles:true})); }",
                str(value),
            )

    async def _set_checkbox(self, loc: Locator, want: bool) -> None:
        """D-040: SAP WebGUI checkboxes are ARIA widgets — `<span
        role="checkbox" aria-checked="true|false">`, not a real `<input
        type="checkbox">`. Playwright's `set_checked()`/`check()` require a
        genuine checkbox/radio input and either throw or silently no-op on
        the ARIA span, which is why recorded unchecks weren't replaying.
        This mirrors the fix v1 already proved out for SM37's job-overview
        screen (sap_automator.py): read the CURRENT state from
        `aria-checked`, and only click when it differs from what's wanted —
        a click always TOGGLES, there's no direct "set" on these widgets, so
        blindly clicking can just as easily undo the desired state as apply
        it.
        """
        role = await loc.get_attribute("role")
        aria = await loc.get_attribute("aria-checked")
        if role == "checkbox" or aria is not None:
            have = aria == "true"
            if have != want:
                await loc.click()
                await self.page.wait_for_timeout(400)  # let SAP repaint aria-checked
            return
        # Genuine native input/role-less element — Playwright's own
        # primitive is correct here; only fall back to click if it balks.
        try:
            await loc.set_checked(want)
        except Exception:  # noqa: BLE001
            have = await loc.is_checked()
            if have != want:
                await loc.click()

    async def _editable(self, loc: Locator) -> Locator:
        """D-053: a table cell or a Lightspeed wrapper resolved for a SET —
        type into the field inside it, not into the <td>."""
        try:
            own = await loc.evaluate(
                "el => ['input','select','textarea'].includes(el.tagName.toLowerCase())"
                " || ['checkbox','radio','textbox','combobox'].includes(el.getAttribute('role'))")
            if own:
                return loc
            inner = loc.locator("input:not([type=hidden]),select,textarea,"
                                "[role=checkbox],[role=radio],[role=textbox]").first
            if await inner.count():
                return inner
        except Exception:  # noqa: BLE001
            pass
        return loc

    async def press(self, res: Resolution, double: bool = False) -> None:
        if double:
            await res.handle.dblclick()
        else:
            await res.handle.click()

    async def read_value(self, res: Resolution) -> Any:
        """D-053: the current value of a field/checkbox, or a text's content."""
        return await res.handle.evaluate(VALUE_JS)

    async def send_vkey(self, vkey: int, window_sid: str = "wnd[0]", key: str | None = None) -> None:
        # DOM mode has no sendVKey: press the key the user pressed (D-053),
        # or map the SAP virtual key back to its keyboard key.
        combo = key or vkey_to_key(vkey)
        if not combo:
            raise NotImplementedError(f"vkey {vkey} has no DOM keyboard mapping")
        await self.page.keyboard.press(combo)

    async def read_table(self, res: Resolution) -> list[dict[str, str]]:
        """D-053: read a data table / ALV grid into rows keyed by column
        header. Only the rows WebGUI has rendered are read — a long ALV list
        is virtualised, so row_count is "rows on screen" unless the list was
        scrolled or the layout shows all rows."""
        data = await res.handle.evaluate(READ_TABLE_JS)
        return list((data or {}).get("rows") or [])

    async def read_statusbar(self) -> dict[str, str]:
        return await read_statusbar(self.page)


# SAP virtual keys (SAP GUI numbering): 0 Enter, 1-12 F1-F12, 13-24 Shift+F1-
# F12, 25-36 Ctrl+F1-F12, 11 is also Ctrl+S (Save) and 12 also Esc (Cancel),
# 81/82 Page Up/Down. Keys pressed in WebGUI map back to these for scripting
# mode; DOM replay prefers the recorded key itself (Step.key).
def vkey_to_key(vkey: int) -> str | None:
    special = {0: "Enter", 11: "Control+S", 12: "Escape", 81: "PageUp", 82: "PageDown",
               80: "Control+PageUp", 83: "Control+PageDown"}
    if 37 <= vkey <= 48:
        return f"Control+Shift+F{vkey - 36}"
    if vkey in special:
        return special[vkey]
    if 1 <= vkey <= 12:
        return f"F{vkey}"
    if 13 <= vkey <= 24:
        return f"Shift+F{vkey - 12}"
    if 25 <= vkey <= 36:
        return f"Control+F{vkey - 24}"
    return None


def key_to_vkey(key: str) -> int | None:
    k = key.replace("Ctrl+", "Control+")
    if k == "Enter":
        return 0
    if k in ("Control+S",):
        return 11
    if k == "Escape":
        return 12
    if k == "PageUp":
        return 81
    if k == "PageDown":
        return 82
    if k == "Control+PageUp":
        return 80
    if k == "Control+PageDown":
        return 83
    m = re.fullmatch(r"(Control\+Shift\+|Shift\+|Control\+)?F(\d{1,2})", k)
    if m and 1 <= int(m.group(2)) <= 12:
        n = int(m.group(2))
        return n + {"": 0, "Shift+": 12, "Control+": 24, "Control+Shift+": 36}[m.group(1) or ""]
    return None
