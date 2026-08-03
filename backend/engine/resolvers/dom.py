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

from typing import Any

from playwright.async_api import Locator

from ..schema import Target
from .base import Resolution, ResolveError, Resolver


class DomResolver(Resolver):
    name = "dom"

    async def resolve(self, target: Target) -> Resolution:
        tried: list[str] = []
        dom = target.dom

        if dom is None:
            raise ResolveError(target, tried=["<no dom candidates recorded>"])

        # Tier 0 — D-040/D-043: positional ARIA-toggle match. SAP renders
        # checkboxes AND radio buttons as ARIA spans (role="checkbox"/
        # "radio") that usually have no distinguishing name/title, so the
        # generic attr/label chain below tends to either miss them or,
        # worse, match the SAME element for every widget on the screen.
        # Position among all same-role elements is what's actually stable —
        # ported from v1's SM37 job-overview fix
        # (get_by_role("checkbox").nth(i)) and generalized.
        if dom.role in ("checkbox", "radio") and dom.checkbox_index is not None:
            loc = self.page.locator(f'[role="{dom.role}"]').nth(dom.checkbox_index)
            tried.append(f"{dom.role}_index:{dom.checkbox_index}")
            if await self._usable(loc):
                return Resolution(loc, 0.9, f"{dom.role}_index:{dom.checkbox_index}", self.name)

        # Tier 1 — semantic attributes. D-042: `nth` was captured
        # (nthAmongSimilar) specifically to disambiguate when the same
        # attr value matches multiple elements, but this tier hardcoded
        # `.first` and never actually consulted it — a latent gap since
        # D-033, now fixed alongside the same bug in tier 2.
        if dom.attr:
            selector = "".join(f'[{k}*="{v}"]' if k == "id" else f'[{k}="{v}"]'
                               for k, v in dom.attr.items())
            idx0 = dom.nth or 0
            loc = self.page.locator(selector).nth(idx0)
            tried.append(f"attr:{selector}#{idx0}")
            if await self._usable(loc):
                return Resolution(loc, 0.95, f"attr:{selector}#{idx0}", self.name)

        # Tier 2 — role + accessible name / visible label.
        # D-042: `dom.nth` disambiguates when MULTIPLE elements share the
        # identical label — SAP's own date-range widgets are the concrete
        # case that surfaced this (e.g. both the "From" and "To" boxes in
        # SM37's Job Start Condition carry the same generic aria-label).
        # `.first` silently picked the SAME field for both recorded SET
        # steps; only one of the two ever actually landed at replay.
        idx = dom.nth or 0
        if dom.role and dom.label:
            loc = self.page.get_by_role(dom.role, name=dom.label)
            cand = loc.nth(idx)
            tried.append(f"role:{dom.role}+label:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.85, f"role:{dom.role}:{dom.label}#{idx}", self.name)
        if dom.label:
            loc = self.page.get_by_label(dom.label)
            cand = loc.nth(idx)
            tried.append(f"label:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.85, f"label:{dom.label}#{idx}", self.name)
            loc = self.page.get_by_text(dom.label, exact=True)
            cand = loc.nth(idx)
            tried.append(f"text:{dom.label}#{idx}")
            if await self._usable(cand):
                return Resolution(cand, 0.80, f"text:{dom.label}#{idx}", self.name)

        # Tier 2b — D-033: visible text of the element itself (buttons, tabs,
        # tree nodes and table cells are overwhelmingly identified this way in
        # WebGUI, and it's what the recorder captures most reliably).
        if dom.text:
            tag = dom.tag or "*"
            loc = self.page.locator(tag, has_text=dom.text).first
            tried.append(f"tag+text:{tag}:{dom.text}")
            if await self._usable(loc):
                return Resolution(loc, 0.78, f"tag+text:{tag}:{dom.text}", self.name)
            loc = self.page.get_by_text(dom.text, exact=True).first
            tried.append(f"text:{dom.text}")
            if await self._usable(loc):
                return Resolution(loc, 0.75, f"text:{dom.text}", self.name)

        # Tier 3 — D-033: SAP UI-library class fragment, disambiguated by the
        # element's tag and (when known) its input type. Deliberately narrow:
        # a bare class fragment matches dozens of nodes, so this only counts
        # as a hit when it resolves to something usable AND we can pin the
        # tag — hence the modest confidence, which flags it in the report.
        if dom.css_hint:
            tag = dom.tag or "*"
            sel = f'{tag}.{dom.css_hint}'
            if dom.input_type:
                sel += f'[type="{dom.input_type}"]'
            loc = self.page.locator(sel)
            tried.append(f"css_hint:{sel}")
            idx = dom.nth or 0
            try:
                if await loc.count() > idx:
                    cand = loc.nth(idx)
                    if await self._usable(cand):
                        return Resolution(cand, 0.60, f"css_hint:{sel}#{idx}", self.name)
            except Exception:  # noqa: BLE001
                pass

        # Tier 4 — pos_id: exact positional WebGUI id. Fast when the screen is
        # unchanged, worthless after a layout shift — always low confidence.
        if dom.pos_id:
            loc = self.page.locator(f'[id="{dom.pos_id}"]').first
            tried.append(f"pos_id:{dom.pos_id}")
            if await self._usable(loc):
                return Resolution(loc, 0.40, f"pos_id:{dom.pos_id}", self.name)

        # Tier 5 — D-033: structural CSS path. Last resort by definition; a
        # match here means "same shape of DOM", not "same element", so it is
        # always flagged (well below the 0.75 threshold).
        if dom.path:
            loc = self.page.locator(dom.path).first
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
        loc: Locator = res.handle
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

    async def press(self, res: Resolution) -> None:
        await res.handle.click()

    async def send_vkey(self, vkey: int, window_sid: str = "wnd[0]") -> None:
        # DOM mode has no sendVKey; map the common ones to keyboard events.
        keymap = {0: "Enter", 3: "F3", 8: "F8", 11: "Control+S", 12: "Escape"}
        if vkey not in keymap:
            raise NotImplementedError(f"vkey {vkey} has no DOM keyboard mapping yet")
        await self.page.keyboard.press(keymap[vkey])

    async def read_table(self, res: Resolution) -> list[dict[str, str]]:
        # TODO Phase 1 (hard part, deliberately confronted via SM37):
        # ALV grid DOM parsing. Port the header-x-offset technique from v1
        # (SMICM :2050-2124, STRUST :2212-2270) as the fallback, but first
        # attempt semantic row/cell extraction from the grid's DOM structure.
        raise NotImplementedError("DOM table read lands with the SM37 flow")

    async def read_statusbar(self) -> dict[str, str]:
        loc = self.page.locator('[id*="msgBar"], .lsMessageBar').first
        if await self._usable(loc):
            text = (await loc.inner_text()).strip()
            return {"type": "", "text": text}
        return {"type": "", "text": ""}
