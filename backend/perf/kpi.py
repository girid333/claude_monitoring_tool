"""Performance ratings — EarlyWatch-style thresholds (D-052).

Sources (research 2026-10-02): SAP's rough guide and EWA practice —
average dialog response time ≤ 1000 ms good, > 1200 ms (EWA's classic
warning line) worth a look; DB time and CPU time each ≲ 40 % of response
time; wait (dispatcher queue) time ≲ 10 % of response time / 50 ms; roll
wait ≲ 200 ms; load+generation ≲ 10 %; buffer hit ratio ≥ 98–99 %;
no buffer swaps. Host CPU sustained > 70 % / 85 %, HANA memory used vs
allocation limit > 80 % / 90 %. A rating is one of good, warning, serious,
critical, unknown — the dashboard pairs each with an icon + label.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

RANK = {"good": 0, "unknown": 0, "warning": 1, "serious": 2, "critical": 3}


@dataclass
class Kpi:
    id: str
    label: str
    value: Optional[float]
    unit: str
    rating: str = "unknown"
    detail: str = ""
    target: str = ""
    group: str = "SAP"
    spark: list = field(default_factory=list)

    def dict(self) -> dict:
        return asdict(self)


def rate_high(v: Optional[float], warn: float, serious: float, critical: Optional[float] = None) -> str:
    """Higher is worse."""
    if v is None:
        return "unknown"
    if critical is not None and v >= critical:
        return "critical"
    if v >= serious:
        return "serious"
    if v >= warn:
        return "warning"
    return "good"


def rate_low(v: Optional[float], warn: float, serious: float, critical: Optional[float] = None) -> str:
    """Lower is worse."""
    if v is None:
        return "unknown"
    if critical is not None and v <= critical:
        return "critical"
    if v <= serious:
        return "serious"
    if v <= warn:
        return "warning"
    return "good"


def overall(kpis: list[Kpi]) -> dict:
    """Score 0–100: start at 100, deduct per KPI by severity; grade A–E."""
    known = [k for k in kpis if k.rating != "unknown"]
    if not known:
        return {"score": None, "grade": "–", "rating": "unknown", "counts": {}}
    pen = {"good": 0, "warning": 6, "serious": 15, "critical": 30}
    score = max(0, 100 - sum(pen[k.rating] for k in known))
    counts = {r: sum(1 for k in known if k.rating == r) for r in ("good", "warning", "serious", "critical")}
    worst = max(known, key=lambda k: RANK[k.rating]).rating
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "E"
    if counts["critical"] and grade in ("A", "B"):
        grade = "C"                      # a critical finding never reads as "good overall"
    return {"score": score, "grade": grade, "rating": worst, "counts": counts}


def dialog_kpis(task: dict) -> list[Kpi]:
    """KPIs from the DIALOG task-type line (averages in ms)."""
    if not task or not task.get("steps"):
        return [Kpi("dia_resp", "Dialog response time", None, "ms", detail="No dialog steps in this period",
                    target="≤ 1000 ms")]
    r = task["avg_resp"]
    share = lambda k: (task[k] / r * 100) if r else None  # noqa: E731
    out = [
        Kpi("dia_resp", "Dialog response time", r, "ms", rate_high(r, 1000, 1200, 2000),
            f"{task['steps']:,} dialog steps", "≤ 1000 ms"),
        Kpi("dia_db", "DB time per step", task["avg_db"], "ms",
            rate_high(share("avg_db"), 40, 50, 70), f"{share('avg_db') or 0:.0f} % of response time", "≤ 40 % of response"),
        Kpi("dia_cpu", "CPU time per step", task["avg_cpu"], "ms",
            rate_high(share("avg_cpu"), 40, 50, 70), f"{share('avg_cpu') or 0:.0f} % of response time", "≤ 40 % of response"),
        Kpi("dia_wait", "Wait time per step", task["avg_wait"], "ms",
            "critical" if task["avg_wait"] > 500 else rate_high(max(share("avg_wait") or 0, task["avg_wait"] / 5), 10, 20, 50),
            f"{share('avg_wait') or 0:.1f} % of response time — dispatcher queue", "≤ 10 % · ≤ 50 ms"),
        Kpi("dia_roll", "Roll wait per step", task["avg_rollwait"], "ms",
            rate_high(task["avg_rollwait"], 200, 400, 1000), "RFC/GUI round trips during the step", "≤ 200 ms"),
        Kpi("dia_gui", "GUI time per step", task["avg_gui"], "ms",
            rate_high(task["avg_gui"], 200, 400, 1000), "Front-end + network", "≤ 200 ms"),
    ]
    return out
