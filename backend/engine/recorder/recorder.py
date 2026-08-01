"""Recorder (Phase 2, D-004): runs ONLY against scripting-enabled systems.

Mechanism: inject a capture script; every click/change is resolved to a
scripting ID via GuiSession.FindByPosition (VERIFY on live system — entry
point may differ by kernel patch), and DOM metadata (attr/label/pos_id) is
snapshotted at the same instant, making the recorded flow DOM-replayable.

Usage:
    rec = Recorder(page)
    await rec.start()
    ... user drives WebGUI in the headed browser ...
    flow = await rec.stop(flow_id="my_flow")

Event coalescing: consecutive change events on the same target collapse to
one `set`; Enter/F-keys become `vkey`; a `wait_ready` is inserted after
every server-roundtrip-triggering step (vkey / button press).
"""

from __future__ import annotations

from pathlib import Path

from playwright.async_api import Page

from ..schema import Act, DomCandidates, Flow, Step, Target

_INJECT_JS = r"""
(() => {
  if (window.__rec_active) return;
  window.__rec_active = true;

  function session() {
    const c = [
      () => window.sap?.scripting?.getSession?.(),
      () => window.session,
      () => window.parent?.session,
    ];
    for (const f of c) { try { const s = f(); if (s?.findById) return s; } catch (e) {} }
    return null;
  }

  function scriptingIdAt(x, y) {
    try {
      const s = session();
      if (!s?.FindByPosition) return null;
      const el = s.FindByPosition(x, y);
      // Strip session prefix -> window-relative (D-008)
      return el?.Id?.replace(/^.*?(wnd\[\d+\])/, "$1") ?? null;
    } catch (e) { return null; }
  }

  function domMeta(el) {
    return {
      attr: el.name ? {name: el.name} : (el.title ? {title: el.title} : null),
      label: el.labels?.[0]?.innerText?.trim()
          || el.getAttribute("aria-label")
          || el.title || null,
      role: el.getAttribute("role") || null,
      css_hint: [...el.classList].find(c => c.startsWith("ls") || c.startsWith("ur")) || null,
      pos_id: el.id || null,
    };
  }

  function emit(kind, ev, el, value) {
    window.__rec_emit({
      kind, value: value ?? null,
      sid: scriptingIdAt(ev.clientX ?? 0, ev.clientY ?? 0),
      dom: domMeta(el),
      tag: el.tagName, type: el.type ?? null,
      key: ev.key ?? null, ts: Date.now(),
    });
  }

  document.addEventListener("change", ev => {
    const el = ev.target;
    const v = el.type === "checkbox" ? el.checked : el.value;
    emit("set", ev, el, v);
  }, true);

  document.addEventListener("click", ev => {
    const el = ev.target.closest("button,[role=button],a,[role=tab],[role=treeitem],tr,td") || ev.target;
    emit("click", ev, el);
  }, true);

  document.addEventListener("keydown", ev => {
    if (ev.key === "Enter" || /^F\d+$/.test(ev.key))
      emit("key", ev, ev.target);
  }, true);
})();
"""

_KEY_TO_VKEY = {"Enter": 0, "F3": 3, "F8": 8, "F5": 5, "F2": 2, "F4": 4}

_ROUNDTRIP_KINDS = {"key", "click"}


class Recorder:
    def __init__(self, page: Page):
        self.page = page
        self.events: list[dict] = []
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self.page.expose_binding(
                "__rec_emit", lambda _src, ev: self.events.append(ev))
            self._started = True
        await self.page.evaluate(_INJECT_JS)
        # Re-inject after every navigation/roundtrip re-render:
        await self.page.add_init_script(_INJECT_JS)

    async def stop(self, flow_id: str, description: str = "") -> Flow:
        steps = self._coalesce(self.events)
        self.events = []
        return Flow(flow_id=flow_id, description=description, steps=steps)

    def _coalesce(self, events: list[dict]) -> list[Step]:
        steps: list[Step] = []
        for ev in events:
            target = None
            if ev.get("sid") or ev.get("dom"):
                target = Target(
                    sid=ev.get("sid") or f"UNRESOLVED:{ev['dom'].get('pos_id')}",
                    dom=DomCandidates(**{k: v for k, v in (ev.get("dom") or {}).items()
                                         if v is not None}))

            if ev["kind"] == "set":
                # Collapse repeated sets on the same target
                if (steps and steps[-1].act == Act.SET and steps[-1].target
                        and target and steps[-1].target.sid == target.sid):
                    steps[-1].value = ev["value"]
                    continue
                steps.append(Step(act=Act.SET, target=target, value=ev["value"]))

            elif ev["kind"] == "key" and ev.get("key") in _KEY_TO_VKEY:
                steps.append(Step(act=Act.VKEY, value=_KEY_TO_VKEY[ev["key"]]))
                steps.append(Step(act=Act.WAIT_READY))

            elif ev["kind"] == "click":
                steps.append(Step(act=Act.PRESS, target=target))
                steps.append(Step(act=Act.WAIT_READY))

        return steps


def save_flow(flow: Flow, directory: str | Path = "flows") -> Path:
    path = Path(directory) / f"{flow.flow_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(flow.model_dump_json(indent=2, exclude_none=True))
    return path
