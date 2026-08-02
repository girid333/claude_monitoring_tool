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

  function domMeta(el) {
    const txt = ((el.innerText !== undefined ? el.innerText : el.value) || "").trim();
    return {
      attr: semanticAttr(el),
      label: labelFor(el),
      role: el.getAttribute ? el.getAttribute("role") : null,
      css_hint: cssHint(el),
      pos_id: el.id || null,
      text: txt ? txt.slice(0, 80) : null,
      tag: el.tagName ? el.tagName.toLowerCase() : null,
      input_type: el.type || null,
      nth: nthAmongSimilar(el),
      path: structuralPath(el),
      frame_url: location.href,
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

  document.addEventListener("change", ev => {
    const el = ev.target;
    const v = (el.type === "checkbox" || el.type === "radio") ? el.checked : el.value;
    emit("set", ev, el, v);
  }, true);

  document.addEventListener("click", ev => {
    const el = (ev.target.closest && ev.target.closest(
      "button,[role=button],a,input[type=submit],input[type=button]," +
      "[role=tab],[role=treeitem],[role=checkbox],td,tr")) || ev.target;
    // a click that merely focuses a text field isn't a step — its change is
    if (el.tagName === "INPUT" && ["text", "password", "search"].indexOf(el.type) >= 0) return;
    emit("click", ev, el);
  }, true);

  document.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || /^F\d{1,2}$/.test(ev.key)) emit("key", ev, ev.target);
  }, true);
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
        out = []
        for s in self._coalesce(self.events):
            t = s.target
            label = None
            if t and t.dom:
                label = (t.dom.label or t.dom.text
                         or (list(t.dom.attr.values())[0] if t.dom.attr else None)
                         or t.dom.pos_id)
            if s.act == Act.START_TCODE:
                label = f"T-code {s.value}"
            elif s.act == Act.CAPTURE:
                label = "screenshot"
            out.append({
                "act": s.act.value,
                "target": label or (t.sid if t else None) or "—",
                "value": s.value if not isinstance(s.value, dict) else None,
                "resolved_by": ("scripting" if (t and t.sid)
                                 else "dom" if t
                                 else "manual" if s.act in (Act.START_TCODE, Act.CAPTURE)
                                 else "n/a"),
            })
        return out

    # ── D-034: manually injected steps ──────────────────────────────────
    # Recorded flows need two things the DOM can't tell us: where the flow
    # STARTS (the T-code) and where the user wants EVIDENCE captured. Both
    # are pushed into the same event list so ordering with real interactions
    # is preserved naturally.

    def add_start_tcode(self, tcode: str) -> None:
        self.events.append({"kind": "start_tcode", "value": tcode})

    def add_capture(self, note: str = "") -> None:
        self.events.append({"kind": "capture", "value": note})

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

        return steps


def _same_target(a: Target, b: Target) -> bool:
    if a.sid and b.sid:
        return a.sid == b.sid
    if a.dom and b.dom:
        return (a.dom.pos_id or a.dom.path) == (b.dom.pos_id or b.dom.path)
    return False


def save_flow(flow: Flow, directory: str | Path = "flows") -> Path:
    path = Path(directory) / f"{flow.flow_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(flow.model_dump_json(indent=2, exclude_none=True))
    return path
