"""Recorder (Phase 2) — DOM-first capture, D-033.

WHAT CHANGED AND WHY (supersedes the D-004 scaffold):

The original recorder resolved every interaction to a SAP GUI Scripting ID
via `GuiSession.FindByPosition`, and therefore only worked on systems with
`sapgui/user_scripting = TRUE`. That assumption doesn't survive contact with
reality: v1 monitors SAP WebGUI entirely through the DOM (§6 — zero findById
usage anywhere), and locked-down customer systems keep scripting disabled on
purpose. A recorder that can only emit scripting IDs records `UNRESOLVED:`
placeholders on exactly the systems this tool exists to serve.

So capture is now DOM-first, exactly mirroring v1's proven selector priority
(D-012): semantic attributes > visible text/role > SAP class fragment >
structural path. The scripting ID is still captured opportunistically when
the API happens to be present — it just isn't required any more (Target.sid
is Optional as of D-033).

The recorder also no longer needs a headed browser: the human drives a
headless containerised Chromium remotely over CDP screencast (see
recorder_service.py), so recording works in the same deployment that runs
the monitoring.
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from ..schema import Act, DomCandidates, Flow, Step, Target

# Injected into every frame. Captures rich, ranked identification metadata
# at the moment of interaction — the DOM is guaranteed to still look the way
# the user saw it, which is what makes replay in a later session work.
_INJECT_JS = r"""
(() => {
  if (window.__rec_active) return;
  window.__rec_active = true;

  // ---- optional scripting ID (bonus, never required) ----
  function session() {
    const cands = [
      () => window.sap && window.sap.scripting && window.sap.scripting.getSession(),
      () => window.session,
      () => window.parent && window.parent.session,
    ];
    for (const f of cands) { try { const s = f(); if (s && s.findById) return s; } catch (e) {} }
    return null;
  }
  function scriptingIdAt(x, y) {
    try {
      const s = session();
      if (!s || !s.FindByPosition) return null;
      const el = s.FindByPosition(x, y);
      return (el && el.Id) ? el.Id.replace(/^.*?(wnd\[\d+\])/, "$1") : null;
    } catch (e) { return null; }
  }

  // ---- DOM candidates, ranked per D-012 ----
  function labelFor(el) {
    if (el.labels && el.labels[0]) return (el.labels[0].innerText || "").trim();
    const al = el.getAttribute && el.getAttribute("aria-label");
    if (al) return al.trim();
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return (l.innerText || "").trim();
      } catch (e) {}
    }
    // SAP often puts the caption in the preceding cell/div
    const p = el.closest ? el.closest("td, div") : null;
    if (p && p.previousElementSibling) {
      const t = (p.previousElementSibling.innerText || "").trim();
      if (t && t.length < 60) return t;
    }
    return null;
  }

  function semanticAttr(el) {
    for (const a of ["name", "title", "aria-label", "data-testid"]) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) { const o = {}; o[a] = v; return o; }
    }
    // SAP ids are usually positional, but some carry meaning (e.g. *okcd*)
    if (el.id && /[a-zA-Z]{3,}/.test(el.id) && !/^M\d+:/.test(el.id)) return { id: el.id };
    return null;
  }

  function cssHint(el) {
    return [...(el.classList || [])].find(c => /^(ls|ur)[A-Za-z]/.test(c)) || null;
  }

  function structuralPath(el) {
    const parts = [];
    let cur = el;
    for (let d = 0; cur && cur.nodeType === 1 && d < 6; d++, cur = cur.parentElement) {
      let seg = cur.tagName.toLowerCase();
      const par = cur.parentElement;
      if (par) {
        const sibs = [...par.children].filter(c => c.tagName === cur.tagName);
        if (sibs.length > 1) seg += ":nth-of-type(" + (sibs.indexOf(cur) + 1) + ")";
      }
      parts.unshift(seg);
      if (cur.id && !/^M\d+:/.test(cur.id)) {
        try { parts.unshift("#" + CSS.escape(cur.id)); } catch (e) {}
        break;
      }
    }
    return parts.join(" > ");
  }

  function nthAmongSimilar(el) {
    try {
      const attr = semanticAttr(el);
      if (!attr) return null;
      const k = Object.keys(attr)[0];
      const sel = el.tagName.toLowerCase() + "[" + k + '="' + CSS.escape(attr[k]) + '"]';
      const all = [...document.querySelectorAll(sel)];
      return all.length > 1 ? all.indexOf(el) : null;
    } catch (e) { return null; }
  }

  // D-042: SAP's real date-range widgets (SM37's "Job Start Condition"
  // From/To, for example) commonly give EVERY sub-field the same generic
  // accessible label (e.g. aria-label="Input Field") — there's nothing
  // attr-based to key on (nthAmongSimilar only fires for semanticAttr
  // matches), so the resolver's label tier matched ".first" for every one
  // of them: two recorded SET steps for two different date boxes both
  // landed on the SAME box at replay, and the other silently kept SAP's
  // default. This is the SAME shape of bug D-040 fixed for checkboxes,
  // generalized: when the label ITSELF is ambiguous, position among
  // same-tag/same-type elements sharing that identical label is the
  // fallback identity.
  function nthAmongLabel(el, label) {
    if (!label) return null;
    try {
      const tag = el.tagName.toLowerCase();
      const type = el.type || "";
      const all = [...document.querySelectorAll(tag)].filter(e => (e.type || "") === type);
      const matches = all.filter(e => labelFor(e) === label);
      return matches.length > 1 ? matches.indexOf(el) : null;
    } catch (e) { return null; }
  }

  // D-040: SAP renders checkboxes as ARIA spans (role="checkbox",
  // aria-checked="true|false"), not native <input type="checkbox">, and they
  // rarely carry a distinguishing name/title. Position among all
  // role="checkbox" elements on the screen is what's stable — SAP always
  // renders a selection screen's checkboxes in the same order.
  function checkboxIndex(el) {
    if (!(el.getAttribute && el.getAttribute("role") === "checkbox")) return null;
    try {
      const all = [...document.querySelectorAll('[role="checkbox"]')];
      const i = all.indexOf(el);
      return i >= 0 ? i : null;
    } catch (e) { return null; }
  }

  function domMeta(el) {
    const txt = ((el.innerText !== undefined ? el.innerText : el.value) || "").trim();
    const lbl = labelFor(el);
    // D-042: attr-based disambiguation first (tier 1 is tried first at
    // replay); only fall back to label-based disambiguation when there's
    // no semantic attr at all, since that's the tier that will actually
    // consume this nth value in that case.
    const nth = nthAmongSimilar(el);
    return {
      attr: semanticAttr(el),
      label: lbl,
      role: el.getAttribute ? el.getAttribute("role") : null,
      css_hint: cssHint(el),
      pos_id: el.id || null,
      text: txt ? txt.slice(0, 80) : null,
      tag: el.tagName ? el.tagName.toLowerCase() : null,
      input_type: el.type || null,
      nth: nth !== null ? nth : nthAmongLabel(el, lbl),
      path: structuralPath(el),
      frame_url: location.href,
      checkbox_index: checkboxIndex(el),
    };
  }

  function emit(kind, ev, el, value) {
    try {
      window.__rec_emit({
        kind: kind,
        value: value === undefined ? null : value,
        sid: scriptingIdAt(ev.clientX || 0, ev.clientY || 0),
        dom: domMeta(el),
        key: ev.key || null,
        ts: Date.now(),
      });
    } catch (e) { /* never break the page being recorded */ }
  }

  // D-042: remembers each field's value AS OF the moment it gained focus,
  // so the blur listener below can tell "the user actually edited this"
  // from "the user merely clicked in and back out again". Without this,
  // just clicking into the (already-prefilled) Job Name field and
  // clicking elsewhere recorded a phantom `set Job Name = "*"` step —
  // visible in a real recording via the new step viewer (D-041).
  const __focusVals = new WeakMap();
  document.addEventListener("focusin", ev => {
    const el = ev.target;
    if (el && el.tagName === "INPUT") __focusVals.set(el, el.value);
  }, true);

  document.addEventListener("change", ev => {
    const el = ev.target;
    const v = (el.type === "checkbox" || el.type === "radio") ? el.checked : el.value;
    emit("set", ev, el, v);
  }, true);

  // D-041: redundant capture on blur, for text/date-style fields. 'change'
  // SHOULD already fire on blur for a modified <input>, but this is a
  // cheap, harmless safety net for any SAP field whose value gets set by a
  // helper widget (e.g. a calendar-icon date picker) in a way that doesn't
  // reliably dispatch a native 'change'. 'blur' doesn't bubble, so this has
  // to be a capture-phase listener on document (still fires top-down).
  // Coalescing in _coalesce() already collapses a change+blur pair on the
  // same field into a single SET step, so this can't double-record.
  document.addEventListener("blur", ev => {
    const el = ev.target;
    if (!el || !el.tagName || el.tagName !== "INPUT") return;
    if (["text", "search"].indexOf(el.type) < 0) return;
    // Never the OK-code/command field: it's ALWAYS tool-driven (T-code
    // navigation writes and submits it programmatically — tcode_nav.py
    // uses these exact three signals to find it), and it gets focused +
    // blurred as a side effect of that navigation even while the recorder
    // is paused. A stray blur landing just after resume() produced a
    // phantom "set okcd" step ahead of the real recorded action — caught
    // by test_tcode_capture_flows.py's step count regressing from 4 to 5.
    const title = el.getAttribute("title") || "";
    const name = el.getAttribute("name") || "";
    if (title === "Command Field" || name === "~OkCode"
        || (el.id || "").toLowerCase().indexOf("okcd") >= 0) return;
    // D-042: only a REAL edit is worth a step. Without this check, merely
    // clicking into a field and clicking away (its value unchanged since
    // focus) recorded a phantom set — confirmed by a real recording shown
    // through the new step viewer (`set Job Name = "*"`, `set User Name =
    // "*"`, neither ever actually typed).
    const before = __focusVals.has(el) ? __focusVals.get(el) : null;
    if (before === el.value) return;
    emit("set", ev, el, el.value);
  }, true);

  document.addEventListener("click", ev => {
    const el = (ev.target.closest && ev.target.closest(
      "button,[role=button],a,input[type=submit],input[type=button]," +
      "[role=tab],[role=treeitem],[role=checkbox],td,tr")) || ev.target;
    // a click that merely focuses a text field isn't a step — its change is
    if (el.tagName === "INPUT" && ["text", "password", "search"].indexOf(el.type) >= 0) return;

    // D-040: a checkbox click is not "press this button" — it's "set this
    // checkbox to a specific state". Recording it as a blind click/press
    // means replay always TOGGLES, which only happens to match if the
    // screen's starting state is identical every time. Recording the
    // END state (captured here, in the capture phase, BEFORE SAP's own
    // handler flips aria-checked) makes replay an absolute set instead —
    // it lands on the right state even if the starting state differs.
    if (el.getAttribute && el.getAttribute("role") === "checkbox") {
      const wasChecked = el.getAttribute("aria-checked") === "true";
      emit("set", ev, el, !wasChecked);
      return;
    }
    emit("click", ev, el);
  }, true);

  document.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || /^F\d{1,2}$/.test(ev.key)) emit("key", ev, ev.target);
  }, true);

  // NOTE (D-038): scrolling is NOT captured here.
  // A DOM 'scroll' listener only fires when something actually changes
  // scrollTop, and SAP WebGUI's ALV grids frequently scroll through their
  // own scrollbar widget (repainting rows) without ever touching scrollTop —
  // so on a real system this recorded nothing and every replayed screenshot
  // came back identical. Scroll is captured server-side as the WHEEL GESTURE
  // that produced it (recorder_service.dispatch_input), which reproduces on
  // replay regardless of how the grid implements scrolling.
})();
"""

# SAP virtual keys: Enter=0, F1..F12 = 1..12 (D-008).
_KEY_TO_VKEY = {"Enter": 0, **{f"F{i}": i for i in range(1, 13)}}


class Recorder:
    def __init__(self, page: Page):
        self.page = page
        self.events: list[dict] = []
        self._started = False
        # D-034: while paused, DOM events are dropped. Needed because the
        # tool itself drives the page during programmatic T-code navigation
        # (goto_tcode writes to the OK-code field and presses Enter) — those
        # are OUR actions, not the user's, and recording them produced a
        # duplicate, brittle `set okcd=/nSM37` + `vkey 0` pair alongside the
        # clean synthetic start_tcode step.
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def _on_event(self, ev: dict) -> None:
        if not self.paused:
            self.events.append(ev)

    async def start(self) -> None:
        if not self._started:
            await self.page.expose_binding(
                "__rec_emit", lambda _src, ev: self._on_event(ev))
            # Re-inject on every navigation (WebGUI re-renders constantly)
            await self.page.add_init_script(_INJECT_JS)
            self._started = True
        await self.refresh()

    async def refresh(self) -> None:
        """(Re-)arm capture in every frame — WebGUI sometimes hosts the
        session inside an iframe, and new frames appear after roundtrips."""
        for frame in self.page.frames:
            try:
                await frame.evaluate(_INJECT_JS)
            except Exception:  # noqa: BLE001,PERF203 - detached/cross-origin frame
                continue

    def steps_preview(self) -> list[dict]:
        """Human-readable step list for the live recorder UI (D-033)."""
        return describe_steps(self._coalesce(self.events))

    # ── D-034: manually injected steps ──────────────────────────────────
    # Recorded flows need two things the DOM can't tell us: where the flow
    # STARTS (the T-code) and where the user wants EVIDENCE captured. Both
    # are pushed into the same event list so ordering with real interactions
    # is preserved naturally.

    def add_start_tcode(self, tcode: str) -> None:
        self.events.append({"kind": "start_tcode", "value": tcode})

    def add_capture(self, note: str = "") -> None:
        self.events.append({"kind": "capture", "value": note})

    def add_capture_pages(self, max_pages: int = 25, x: float = 0, y: float = 0,
                           page_delta: float = 600) -> None:
        """D-039: capture the whole scrollable list, one page at a time."""
        self.events.append({"kind": "capture_pages",
                             "value": {"pages": max(1, min(int(max_pages), 25)),
                                        "x": x, "y": y, "page_delta": page_delta}})

    def add_wheel(self, x: float, y: float, dx: float, dy: float) -> None:
        """D-038: record a scroll GESTURE (not a scroll position).

        Replaying the same wheel at the same coordinates works whether the
        target scrolls natively or through SAP's own scrollbar widget —
        recording `scrollTop` only works for the former, which is why the
        first attempt (D-037) produced identical screenshots on a real ALV
        grid. Consecutive wheels at roughly the same spot accumulate, so a
        long flick becomes one step rather than forty."""
        if self.paused:
            return
        last = self.events[-1] if self.events else None
        if (last and last.get("kind") == "wheel"
                and abs(last["value"]["x"] - x) < 40
                and abs(last["value"]["y"] - y) < 40):
            last["value"]["dy"] += dy
            last["value"]["dx"] += dx
            return
        self.events.append({"kind": "wheel",
                             "value": {"x": x, "y": y, "dx": dx, "dy": dy}})

    async def stop(self, flow_id: str, description: str = "") -> Flow:
        steps = self._coalesce(self.events)
        self.events = []
        return Flow(flow_id=flow_id, description=description, steps=steps)

    def _coalesce(self, events: list[dict]) -> list[Step]:
        steps: list[Step] = []
        for ev in events:
            dom_raw = {k: v for k, v in (ev.get("dom") or {}).items() if v is not None}
            target = None
            if ev.get("sid") or dom_raw:
                target = Target(sid=ev.get("sid"),
                                 dom=DomCandidates(**dom_raw) if dom_raw else None)

            kind = ev.get("kind")
            if kind == "start_tcode":
                steps.append(Step(act=Act.START_TCODE, value=ev.get("value")))
                steps.append(Step(act=Act.WAIT_READY))

            elif kind == "capture":
                # D-034: screenshot evidence at a point the user chose.
                steps.append(Step(act=Act.CAPTURE, mode=["screenshot"]))

            elif kind == "capture_pages":
                v = ev.get("value") or {}
                steps.append(Step(act=Act.CAPTURE, mode=["screenshot"],
                                   pages=v.get("pages", 25), value=v))

            elif kind == "set":
                # Collapse repeated edits of the same field into one step
                if (steps and steps[-1].act == Act.SET and steps[-1].target and target
                        and _same_target(steps[-1].target, target)):
                    steps[-1].value = ev.get("value")
                    continue
                steps.append(Step(act=Act.SET, target=target, value=ev.get("value")))

            elif kind == "key" and ev.get("key") in _KEY_TO_VKEY:
                steps.append(Step(act=Act.VKEY, value=_KEY_TO_VKEY[ev["key"]]))
                steps.append(Step(act=Act.WAIT_READY))

            elif kind == "click":
                steps.append(Step(act=Act.PRESS, target=target))
                steps.append(Step(act=Act.WAIT_READY))

            elif kind == "wheel":
                # D-038: a scroll gesture — replayed as a real wheel event.
                steps.append(Step(act=Act.SCROLL, value=ev.get("value")))

            elif kind == "scroll":
                # Legacy D-037 shape (scroll position). Still honoured on
                # replay so flows recorded before D-038 keep working.
                if (steps and steps[-1].act == Act.SCROLL
                        and _same_scroll_target(steps[-1].target, target)):
                    steps[-1].value = ev.get("value")
                    continue
                steps.append(Step(act=Act.SCROLL, target=target, value=ev.get("value")))

        return steps


def _same_scroll_target(a: Target | None, b: Target | None) -> bool:
    if a is None and b is None:
        return True                      # both document-level
    if a is None or b is None:
        return False
    return _same_target(a, b)


def _same_target(a: Target, b: Target) -> bool:
    if a.sid and b.sid:
        return a.sid == b.sid
    if a.dom and b.dom:
        # D-040: checkbox spans commonly have neither pos_id nor path, so the
        # fallback below used to compare None == None -> True, silently
        # coalescing DIFFERENT checkboxes' "set" events into one step (only
        # the last value survived). checkbox_index is the reliable identity
        # here; when either side has one, compare on that alone.
        if a.dom.checkbox_index is not None or b.dom.checkbox_index is not None:
            return a.dom.checkbox_index == b.dom.checkbox_index
        key_a, key_b = a.dom.pos_id or a.dom.path, b.dom.pos_id or b.dom.path
        return key_a is not None and key_a == key_b
    return False


def describe_steps(steps: list[Step]) -> list[dict]:
    """D-041: human-readable step list, usable for BOTH a live in-progress
    recording (Recorder.steps_preview) AND a already-saved flow loaded back
    off disk (main.py's flow-detail endpoint) — Giri asked to be able to see
    exactly what got recorded for a saved flow, not just while recording, so
    this needed to stop being a Recorder-only method."""
    out = []
    for s in steps:
        t = s.target
        label = None
        low_confidence_label = False
        if t and t.dom:
            label = (t.dom.label or t.dom.text
                     or (list(t.dom.attr.values())[0] if t.dom.attr else None)
                     or t.dom.pos_id)
            if not label:
                # D-042: falling all the way through to "—" hides exactly
                # the steps most worth scrutinizing — a target with no
                # semantic identity at all is the weakest kind of match
                # (bottom of the resolver's own priority order), and
                # showing nothing made two such steps in a real recording
                # impossible to tell apart or explain. css_hint/path are
                # what the resolver actually falls back to for these, so
                # show that instead of nothing.
                label = t.dom.css_hint or t.dom.path
                low_confidence_label = bool(label)
        checkbox_i = t.dom.checkbox_index if (t and t.dom) else None
        if s.act == Act.START_TCODE:
            label = f"T-code {s.value}"
        elif s.act == Act.CAPTURE:
            label = (f"screenshot — all pages (max {s.pages})" if s.pages
                     else "screenshot")
        elif s.act == Act.SCROLL:
            v = s.value if isinstance(s.value, dict) else {}
            label = (f"scroll {int(v.get('dy', 0))}px" if "dy" in v
                     else f"scroll to {v.get('top', 0)}px")
        elif checkbox_i is not None:
            label = f"checkbox #{checkbox_i}" + (f" ({label})" if label else "")
        value = s.value if not isinstance(s.value, dict) else None
        if s.act == Act.SET and isinstance(value, bool) and checkbox_i is not None:
            value = "check" if value else "uncheck"
        target = label or (t.sid if t else None) or "—"
        if low_confidence_label:
            target = f"⚠ {target}"
        out.append({
            "act": s.act.value,
            "target": target,
            "value": value,
            "resolved_by": ("scripting" if (t and t.sid)
                             else "dom" if t
                             else "manual" if s.act in (Act.START_TCODE, Act.CAPTURE)
                             else "n/a"),
        })
    return out


def save_flow(flow: Flow, directory: str | Path = "flows") -> Path:
    path = Path(directory) / f"{flow.flow_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(flow.model_dump_json(indent=2, exclude_none=True))
    return path
