"""CLI runner: python -m engine.run --sid Q4H --flow flows/sm37_cancelled_jobs.json"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sid", required=True)
    ap.add_argument("--flow", required=True)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--registry", default="systems.json")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    from .datasets import load as load_dataset
    from .engine import ReplayEngine
    from .login.base import SystemTarget
    from .login.basic import BasicLoginPlugin
    from .probe import probe
    from .recovery import dismiss_known_popups, with_recovery
    from .registry import EnvCredentialProvider, Registry
    from .schema import Flow
    from .store import RunStore

    reg = Registry.load(args.registry)
    system = reg.get(args.sid)
    flow = Flow(**json.loads(Path(args.flow).read_text()))
    iterations = load_dataset(args.dataset) if args.dataset else [{}]
    run_id = uuid.uuid4().hex[:12]

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        page = await browser.new_page()
        login = BasicLoginPlugin(
            SystemTarget(system.sid, system.webgui_url, system.client),
            EnvCredentialProvider())
        await login.login(page)
        pr = await probe(page)
        print(f"[probe] mode={pr.mode} info={pr.session_info} notes={pr.notes}")

        engine = ReplayEngine(page, pr.mode, run_id, system.sid,
                              Path("run_artifacts") / run_id)
        store = RunStore()
        exit_code = 0
        for params in iterations:
            await dismiss_known_popups(page)
            result = await with_recovery(page, login,
                                         lambda: engine.run(flow, params))
            store.save(result)
            print(f"[{result.flow_id}] ok={result.ok} mode={result.mode} "
                  f"checks={sum(c.ok for c in result.checks)}/{len(result.checks)} "
                  f"error={result.error}")
            for c in result.checks:
                if not c.ok:
                    print(f"  ! {c.severity.value.upper()}: {c.message} "
                          f"(actual={c.actual})")
                    exit_code = 1
        await browser.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
