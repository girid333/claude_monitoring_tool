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


# ── D-053: roundtrip-aware waiting ─────────────────────────────────────────
#
# wait_ready() alone has a race: right after a click, SAP has not yet shown
# its busy layer, so "no busy layer" holds for two polls and the engine moves
# on — onto the OLD screen. eCATT never has this problem because SAP GUI
# tells it when the roundtrip ends. In a browser the honest signal is the
# network: WebGUI sends every roundtrip as a request (XHR delta or a full
# document POST), so "a request started and every request has finished, and
# the screen has been quiet since" is the WebGUI equivalent of session.Busy.

_TRACKED_TYPES = {"document", "xhr", "fetch"}


class NetworkTracker:
    """Counts in-flight page requests that can carry a WebGUI roundtrip."""

    def __init__(self, page: Page):
        self.page = page
        self.inflight = 0
        self.started = 0                 # total requests seen, ever
        self.last_activity = time.monotonic()
        # request -> sequence number, so a wait only waits for requests that
        # started AFTER its action (a long-poll already open must not count)
        self._pending: dict = {}
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_done)
        page.on("requestfailed", self._on_done)

    def pending_since(self, mark: int) -> int:
        return sum(1 for seq in self._pending.values() if seq > mark)

    def _tracked(self, request) -> bool:
        try:
            return request.resource_type in _TRACKED_TYPES
        except Exception:  # noqa: BLE001
            return False

    def _on_request(self, request) -> None:
        if self._tracked(request):
            self.inflight += 1
            self.started += 1
            self._pending[request] = self.started
            self.last_activity = time.monotonic()

    def _on_done(self, request) -> None:
        if self._tracked(request):
            self.inflight = max(0, self.inflight - 1)
            self._pending.pop(request, None)
            self.last_activity = time.monotonic()


async def wait_roundtrip(page: Page, tracker: Optional[NetworkTracker], mark: int,
                         timeout_s: float = 30.0, grace_s: float = 1.2,
                         quiet_s: float = 0.3, scripting_mode: bool = False) -> bool:
    """Wait for the roundtrip triggered by an action to finish.

    `mark` is tracker.started taken just BEFORE the action. If no request
    starts within `grace_s` the action was client-side only (opening a
    dropdown, focusing a field) and we return after the normal readiness
    check. Returns True when a roundtrip was observed."""
    deadline = time.monotonic() + timeout_s
    saw = False
    if tracker is not None:
        grace_end = time.monotonic() + grace_s
        while time.monotonic() < grace_end:
            if tracker.started > mark:
                saw = True
                break
            await asyncio.sleep(0.05)
        if saw:
            while time.monotonic() < deadline:
                if (tracker.pending_since(mark) == 0
                        and time.monotonic() - tracker.last_activity >= quiet_s):
                    break
                await asyncio.sleep(0.05)
            try:
                await page.wait_for_load_state("domcontentloaded",
                                               timeout=max(1.0, deadline - time.monotonic()) * 1000)
            except Exception:  # noqa: BLE001 - frame swapped mid-wait; readiness check below decides
                pass
    await wait_ready(page, max(1.0, deadline - time.monotonic()), scripting_mode)
    return saw
