"""Capability probe (D-005) — first action against any system.

Decides WHICH resolver mode is used and with what confidence — never
whether the system can be monitored. RFC is NOT involved (D-013).

Output feeds the system registry (release tag) and the UI's per-system
status chip (design D-016: probe result shown in sidebar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page

from .resolvers.scripting import ScriptingResolver


@dataclass
class ProbeResult:
    webgui_reachable: bool = False
    scripting_available: bool = False
    mode: str = "dom"                       # "scripting" | "dom"
    session_info: dict = field(default_factory=dict)   # SID, client, release, ...
    notes: list[str] = field(default_factory=list)


async def probe(page: Page) -> ProbeResult:
    """Run after login (login plugins own everything before this point)."""
    result = ProbeResult()

    # 1. WebGUI reachable: the shell container must exist.
    try:
        await page.wait_for_selector("body", timeout=10_000)
        shell = await page.locator(".lsShell, #sapLSFrame, .urPage").count()
        result.webgui_reachable = shell > 0 or "webgui" in page.url
    except Exception as exc:
        result.notes.append(f"webgui shell not detected: {exc}")
        return result

    # 2. Scripting availability.
    scripting = ScriptingResolver(page)
    result.scripting_available = await scripting.is_available()
    result.mode = "scripting" if result.scripting_available else "dom"

    # 3. session.info metadata (scripting mode only).
    if result.scripting_available:
        try:
            result.session_info = await page.evaluate(
                """() => {
                    const candidates = [
                        () => window.sap?.scripting?.getSession?.(),
                        () => window.session,
                    ];
                    for (const c of candidates) {
                        try {
                            const s = c();
                            if (s?.Info) {
                                const i = s.Info;
                                return {
                                    sid: i.SystemName, client: i.Client,
                                    release: i.SAPRelease ?? null,
                                    user: i.User, language: i.Language,
                                    transaction: i.Transaction,
                                    response_time: i.ResponseTime ?? null,
                                };
                            }
                        } catch (e) {}
                    }
                    return {};
                }"""
            )
        except Exception as exc:
            result.notes.append(f"session.info read failed: {exc}")
    else:
        result.notes.append(
            "scripting unavailable — DOM mode; replay is best-effort and "
            "low-confidence resolutions will be flagged (D-003)"
        )

    return result
