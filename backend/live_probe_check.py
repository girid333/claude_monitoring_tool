"""Standalone OQ-2 resolution script — run this on a machine that can
actually reach the target SAP system (Cowork's sandbox cannot: its network
is allowlisted and blocked nwrhel9.techlabhome.in outright — see SKILL.md
Session 8). This is NOT part of the FastAPI app; it's a one-shot manual
check you run yourself.

What it does: logs into WebGUI (D-007 basic-auth plugin), runs the
capability probe (D-005) to find out whether SAP GUI Scripting is enabled,
and takes one screenshot as visual proof the login actually worked. That's
enough to answer OQ-2's first question for this system: is TEST a
scripting-enabled reference system, or a DOM-only one?

Usage (from claude_monitoring_tool/backend/, with your venv active —
playwright, pydantic, etc. installed, and `playwright install chromium` run
at least once):

    SAP_TEST_USER=youruser SAP_TEST_PASSWORD=yourpass \\
        python live_probe_check.py \\
        --sid TEST \\
        --url "https://nwrhel9.techlabhome.in/sap/bc/gui/sap/its/webgui/" \\
        --client 100

Credentials NEVER touch disk here — they're read from the env vars you set
on the command line for this one process (D-018). Add --headed to watch the
browser instead of running headless; useful the first time, to eyeball any
unexpected popups BasicLoginPlugin doesn't already dismiss (SKILL.md §6.8 /
Known Risks — v1 had no re-login logic and this plugin only handles the
"multiple logon" dialog by exact button text).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sid", required=True, help="System ID, e.g. TEST")
    ap.add_argument("--url", required=True, help="WebGUI base URL")
    ap.add_argument("--client", required=True, help="SAP client, e.g. 100")
    ap.add_argument("--headed", action="store_true",
                     help="Show the browser window instead of headless")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    from engine.login.base import SystemTarget
    from engine.login.basic import BasicLoginPlugin
    from engine.probe import probe
    from engine.registry import EnvCredentialProvider

    creds = EnvCredentialProvider()
    try:
        creds.get(args.sid)  # fail fast with a clear message if env vars are unset
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"→ Launching {'headed' if args.headed else 'headless'} Chromium...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"→ Logging into {args.sid} at {args.url} (client {args.client})...")
        try:
            login = BasicLoginPlugin(SystemTarget(args.sid, args.url, args.client), creds)
            await login.login(page)
        except Exception as exc:  # noqa: BLE001
            print(f"✗ Login failed: {exc}")
            shot = Path("live_probe_login_failure.png")
            try:
                await page.screenshot(path=str(shot))
                print(f"  Screenshot of failure state saved to {shot}")
            except Exception:
                pass
            await browser.close()
            return 1

        print("✓ Login sequence completed (no exception) — probing capability...")
        result = await probe(page)

        print()
        print("=== Capability Probe Result (D-005) ===")
        print(f"  webgui_reachable   : {result.webgui_reachable}")
        print(f"  scripting_available: {result.scripting_available}")
        print(f"  resolver mode      : {result.mode}")
        print(f"  session_info       : {result.session_info}")
        for note in result.notes:
            print(f"  note: {note}")
        print()

        if not result.webgui_reachable:
            print("✗ WebGUI shell not detected — login likely landed on an error "
                  "page or unexpected screen. Check the screenshot below.")

        shot = Path("live_probe_screenshot.png")
        await page.screenshot(path=str(shot), full_page=True)
        print(f"→ Screenshot saved to {shot} — open it to visually confirm the "
              f"logged-in WebGUI screen.")

        await browser.close()

    print()
    print("Next step for OQ-2: if scripting_available is True, this system "
          "can be a scripting-enabled reference system (recorder + "
          "scripting resolver). If False, it's DOM-only — still usable for "
          "replay (D-003), just not for recording (D-004).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
