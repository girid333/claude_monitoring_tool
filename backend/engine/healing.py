"""Phase 6: self-healing hooks for the DOM resolver (D-003).

When a DOM resolution fails, capture evidence and enqueue a repair item.
Repair execution (LLM-assisted locator suggestion) runs OUT of band — a
failed resolution never silently self-modifies a flow mid-run; repairs are
proposals that a human (or a supervised Claude session) applies to the flow
file, keeping flows deterministic and reviewable.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from playwright.async_api import Page

from .schema import Target


async def capture_repair_item(page: Page, target: Target, flow_id: str,
                              step_index: int, repair_dir: Path) -> Path:
    repair_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = repair_dir / f"{flow_id}_step{step_index}_{stamp}"

    await page.screenshot(path=f"{base}.png", full_page=True)
    # Trimmed DOM snapshot: inputs/buttons/labels only, for locator repair.
    dom = await page.evaluate(
        """() => [...document.querySelectorAll('input,button,select,a,[role]')]
            .slice(0, 800)
            .map(e => ({tag: e.tagName, id: e.id, name: e.name || null,
                        title: e.title || null, role: e.getAttribute('role'),
                        text: (e.innerText || e.value || '').slice(0, 60)}))"""
    )
    item = {
        "flow_id": flow_id, "step_index": step_index,
        "target": target.model_dump(), "captured_at": stamp,
        "dom_elements": dom, "screenshot": f"{base}.png",
        "suggested_fix": None,   # filled by the repair pass
    }
    path = Path(f"{base}.repair.json")
    path.write_text(json.dumps(item, indent=2))
    return path

# Repair pass (TODO, supervised): feed screenshot + dom_elements + target to
# an LLM, get a ranked DomCandidates suggestion, write into suggested_fix.
# A human reviews and applies it to the flow file (git diff = audit trail).
