"""T-code navigation (D-034) — ported from v1's proven implementation.

Why this exists: the replay engine's original START_TCODE looked for the
OK-code field with a single selector on the MAIN FRAME only
(`[id*="okcd"]`), and failed on a real system with:

    Could not resolve target sid='wnd[0]/tbar[0]/okcd';
    tried: ['attr:[id*="okcd"]', 'label:Command Field', 'text:Command Field']

v1 (`sap_automator.py:320-350`) has solved this for 23 T-codes across a real
landscape, and it does three things this didn't:

  1. tries THREE selectors — `input[title="Command Field"]`,
     `input[name="~OkCode"]`, `input[id*="okcd"]` — WebGUI renders different
     ones depending on release and theme,
  2. searches EVERY FRAME, because the session is often inside an iframe,
  3. falls back to the `~transaction` URL parameter when no OK field exists
     at all (e.g. straight after login on some releases).

This module is that logic, reused by both the replay engine and the
recorder so they navigate identically.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse

_OK_FIELD_JS = r"""(tc) => {
  const ok = document.querySelector('input[title="Command Field"]')
          || document.querySelector('input[name="~OkCode"]')
          || document.querySelector('input[id*="okcd"]');
  if (!ok) return false;
  ok.value = '/n' + tc;
  ok.dispatchEvent(new Event('input',  { bubbles: true }));
  ok.dispatchEvent(new Event('change', { bubbles: true }));
  const btn = document.querySelector('div[title="Enter"]')
           || document.querySelector('a[title="Enter"]');
  if (btn) { btn.click(); return true; }
  ok.focus();
  for (const type of ['keydown', 'keypress', 'keyup']) {
    ok.dispatchEvent(new KeyboardEvent(type, {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
  }
  const form = ok.closest('form');
  if (form && form.submit) { try { form.submit(); } catch (e) {} }
  return true;
}"""


async def goto_tcode(page, tcode: str, webgui_url: str = "",
                      client: str = "", language: str = "EN") -> str:
    """Navigate the session to `tcode`. Returns the method that worked:
    "okcode" or "url". Raises RuntimeError if neither did."""

    # 1. OK-code field, every frame (v1's "massive performance boost" path —
    #    no full page load, the session stays alive).
    for frame in page.frames:
        try:
            if await frame.evaluate(_OK_FIELD_JS, tcode):
                await page.wait_for_timeout(400)
                return "okcode"
        except Exception:  # noqa: BLE001,PERF203 - detached/cross-origin frame
            continue

    # 2. ~transaction URL parameter fallback.
    if webgui_url:
        parsed = urlparse(webgui_url)
        params = {"~transaction": tcode}
        if client:
            params["sap-client"] = client
        if language:
            params["sap-language"] = language
        url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(params)}"
        await page.goto(url, wait_until="domcontentloaded")
        return "url"

    raise RuntimeError(
        f"could not navigate to {tcode}: no OK-code field found in any frame "
        f"(tried title='Command Field', name='~OkCode', id*='okcd') and no "
        f"webgui_url was supplied for the ~transaction URL fallback")
