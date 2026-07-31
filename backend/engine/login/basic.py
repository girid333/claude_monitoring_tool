"""BasicLoginPlugin — direct port of v1's proven path (SKILL.md §6.8):
JS-injected credential fill (dodges SAP's CSS visibility tricks that break
Playwright .fill()), submit via login button with form.submit() fallback,
then dismiss the "multiple logon" popup if present.
"""

from __future__ import annotations

from playwright.async_api import Page

from .base import LoginPlugin


class BasicLoginPlugin(LoginPlugin):
    name = "basic"

    async def login(self, page: Page) -> None:
        creds = self.credentials.get(self.system.sid)
        url = f"{self.system.webgui_url}?sap-client={self.system.client}"
        await page.goto(url, wait_until="domcontentloaded")

        await page.wait_for_selector('input[name="sap-user"]', timeout=15_000)
        # v1 lesson: inject values via JS, then dispatch change events.
        await page.evaluate(
            """(c) => {
                const u = document.querySelector('input[name="sap-user"]');
                const p = document.querySelector('input[name="sap-password"]');
                u.value = c.user; p.value = c.password;
                for (const el of [u, p])
                    el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            {"user": creds["user"], "password": creds["password"]},
        )

        btn = page.locator("#sapwp_BUTTON_LOGIN")
        if await btn.count() > 0:
            await btn.click()
        else:
            await page.evaluate("document.querySelector('form')?.submit()")

        # Multiple-logon popup (exact-text buttons, per v1)
        for label in ("Continue", "Yes"):
            try:
                b = page.get_by_text(label, exact=True).first
                if await b.is_visible(timeout=3000):
                    await b.click()
                    break
            except Exception:
                continue
