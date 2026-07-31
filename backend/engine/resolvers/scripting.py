"""ScriptingResolver — SAP GUI Scripting API over WebGUI (D-001).

All interaction goes through page.evaluate() into the injected scripting
session. IDs in flows are window-relative (D-008); this resolver re-anchors
them under the live session prefix at runtime.

NOTE (needs live-system verification, Phase 1 task): the exact JS entry
point for the scripting session in WebGUI varies by kernel/ITS patch level.
_SESSION_JS tries the known access paths; the capability probe (probe.py)
reports which one is live on a given system. Extend the list there first,
then here, if a target system exposes a different path.
"""

from __future__ import annotations

from typing import Any

from ..schema import Target
from .base import Resolution, ResolveError, Resolver

_SESSION_JS = """
() => {
    // Known access paths to the WebGUI scripting session object.
    const candidates = [
        () => window.sap?.scripting?.getSession?.(),
        () => window.session,
        () => window.parent?.session,
    ];
    for (const c of candidates) {
        try { const s = c(); if (s && s.findById) return true; } catch (e) {}
    }
    return false;
}
"""


def _js_with_session(body: str) -> str:
    """Wrap a JS body with session acquisition; body may use `S`."""
    return f"""
    (args) => {{
        const candidates = [
            () => window.sap?.scripting?.getSession?.(),
            () => window.session,
            () => window.parent?.session,
        ];
        let S = null;
        for (const c of candidates) {{
            try {{ const s = c(); if (s && s.findById) {{ S = s; break; }} }} catch (e) {{}}
        }}
        if (!S) throw new Error("scripting session not reachable");
        {body}
    }}
    """


class ScriptingResolver(Resolver):
    name = "scripting"

    async def is_available(self) -> bool:
        try:
            return bool(await self.page.evaluate(_SESSION_JS))
        except Exception:
            return False

    async def resolve(self, target: Target) -> Resolution:
        found = await self.page.evaluate(
            _js_with_session("return !!S.findById(args.sid, false);"),
            {"sid": target.sid},
        )
        if not found:
            raise ResolveError(target, tried=[f"findById({target.sid})"])
        return Resolution(handle=target.sid, confidence=1.0,
                          via=f"findById:{target.sid}", resolver=self.name)

    async def set_value(self, res: Resolution, value: Any) -> None:
        await self.page.evaluate(
            _js_with_session("""
                const el = S.findById(args.sid);
                if (typeof value === 'boolean' || el.type === 'GuiCheckBox') {
                    el.selected = !!args.value;
                } else {
                    el.text = String(args.value);
                }
            """),
            {"sid": res.handle, "value": value},
        )

    async def press(self, res: Resolution) -> None:
        await self.page.evaluate(
            _js_with_session("S.findById(args.sid).press();"),
            {"sid": res.handle},
        )

    async def send_vkey(self, vkey: int, window_sid: str = "wnd[0]") -> None:
        await self.page.evaluate(
            _js_with_session("S.findById(args.wnd).sendVKey(args.vkey);"),
            {"wnd": window_sid, "vkey": vkey},
        )

    async def read_table(self, res: Resolution) -> list[dict[str, str]]:
        return await self.page.evaluate(
            _js_with_session("""
                const t = S.findById(args.sid);
                const out = [];
                const cols = [];
                for (let c = 0; c < t.Columns.Count; c++) cols.push(t.Columns.ElementAt(c).Name);
                const n = Math.min(t.RowCount, t.VisibleRowCount * 20); // page through later
                for (let r = 0; r < n; r++) {
                    const row = {};
                    for (const col of cols) {
                        try { row[col] = t.GetCellValue(r, col); } catch (e) { row[col] = null; }
                    }
                    out.push(row);
                }
                return out;
            """),
            {"sid": res.handle},
        )

    async def read_statusbar(self) -> dict[str, str]:
        return await self.page.evaluate(
            _js_with_session("""
                const sb = S.findById("wnd[0]/sbar", false);
                if (!sb) return {type: "", text: ""};
                return {type: sb.MessageType || "", text: sb.Text || ""};
            """),
            {},
        )
