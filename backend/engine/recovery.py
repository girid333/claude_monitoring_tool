"""Phase 6 robustness: popup interceptor + session recovery.

Popups: known WebGUI dialogs are dismissed by registered handlers between
steps. Unknown modal windows fail the step with a screenshot — never
guess-click on unknown dialogs (could confirm a destructive action).

Recovery: on step failure, check logged-out state; if so, re-login and
restart the flow ONCE. Second failure surfaces as a run error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from playwright.async_api import Page

from .login.base import LoginPlugin


@dataclass
class PopupRule:
    name: str
    detect_selector: str          # visible => popup present
    action_text: str | None      # exact button text to click, None = press Escape


DEFAULT_POPUP_RULES: list[PopupRule] = [
    PopupRule("multiple_logon", 'text="is already logged on"', "Continue"),
    PopupRule("license_notice", 'text="SAP NetWeaver"', None),
    PopupRule("scripting_notification",
              'text="A script is trying to attach"', "OK"),
    # Extend from live-system observation; keep detection SPECIFIC.
]


async def dismiss_known_popups(page: Page,
                               rules: list[PopupRule] = DEFAULT_POPUP_RULES) -> list[str]:
    dismissed = []
    for rule in rules:
        try:
            loc = page.locator(rule.detect_selector).first
            if await loc.is_visible(timeout=200):
                if rule.action_text:
                    await page.get_by_text(rule.action_text, exact=True).first.click()
                else:
                    await page.keyboard.press("Escape")
                dismissed.append(rule.name)
        except Exception:
            continue
    return dismissed


async def with_recovery(page: Page, login: LoginPlugin,
                        run_flow: Callable[[], Awaitable],
                        max_relogins: int = 1):
    """Run a flow; on failure caused by session loss, re-login and retry."""
    attempt = 0
    while True:
        result = await run_flow()
        if result.ok or attempt >= max_relogins:
            return result
        if await login.detect_logged_out(page):
            attempt += 1
            result.error = (result.error or "") + " [session lost -> re-login retry]"
            await login.login(page)
            continue
        return result
