"""wait_ready — the single synchronization barrier for all replay steps.

Design mandate (Known Risks, SKILL.md §6.4): v1 stacked three defensive
layers (busy-overlay poll + networkidle + flat residual sleeps). This module
must fully replace all of them — never add a flat sleep on top.

Model: v1's one genuinely clean wait (ST03N, sap_automator.py:1512-1524):
poll for a positive, observable condition until it holds, with a hard
timeout. Here the condition is layered:

  1. WebGUI busy indicators absent (.lsBlockLayer, #ls-loading)
  2. If scripting mode: session.Busy === false
  3. Optional caller-supplied predicate (e.g. "button X disappeared")

All polling is event-loop friendly (asyncio), 150ms interval.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from playwright.async_api import Page

BUSY_SELECTORS = [".lsBlockLayer", "#ls-loading", ".urBlockUiTransparent"]
POLL_INTERVAL_S = 0.15


class WaitTimeout(Exception):
    pass


async def _busy_overlay_visible(page: Page) -> bool:
    for sel in BUSY_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=50):
                return True
        except Exception:
            continue
    return False


async def _scripting_busy(page: Page) -> Optional[bool]:
    """Returns session.Busy if scripting is reachable, else None."""
    try:
        return await page.evaluate(
            """() => {
                try {
                    const s = window?.sap?.scripting?.getSession?.()
                        ?? window?.session ?? null;
                    if (s && typeof s.Busy !== 'undefined') return !!s.Busy;
                } catch (e) {}
                return null;
            }"""
        )
    except Exception:
        return None


async def wait_ready(
    page: Page,
    timeout_s: float = 30.0,
    scripting_mode: bool = False,
    predicate: Optional[Callable[[Page], Awaitable[bool]]] = None,
    settle_polls: int = 2,
) -> None:
    """Block until the WebGUI screen is interactable.

    settle_polls: the ready condition must hold for N consecutive polls
    (handles WebGUI's brief overlay flicker between roundtrips) — this is
    the principled replacement for v1's flat 1s post-wait.
    """
    deadline = time.monotonic() + timeout_s
    consecutive_ok = 0

    while time.monotonic() < deadline:
        ready = not await _busy_overlay_visible(page)

        if ready and scripting_mode:
            busy = await _scripting_busy(page)
            if busy is True:
                ready = False

        if ready and predicate is not None:
            ready = await predicate(page)

        if ready:
            consecutive_ok += 1
            if consecutive_ok >= settle_polls:
                return
        else:
            consecutive_ok = 0

        await asyncio.sleep(POLL_INTERVAL_S)

    raise WaitTimeout(f"wait_ready exceeded {timeout_s}s")
