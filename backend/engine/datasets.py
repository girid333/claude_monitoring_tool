"""Dataset loading (Phase 3, D-008): flows and data are separate.

Formats:
  JSON  — {"dataset_id": ..., "values": {...}}            single iteration
          {"dataset_id": ..., "iterations": [{...}, ...]} multi-iteration
  XLSX  — sheet 1, header row = param names, one iteration per data row
          (OQ-5 pending; both supported so functional teams can use Excel)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: str | Path) -> list[dict[str, Any]]:
    """Returns a list of parameter dicts (one per iteration)."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text())
        if "iterations" in data:
            return list(data["iterations"])
        return [dict(data.get("values", {}))]

    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook  # optional dependency
        except ImportError as exc:
            raise RuntimeError("pip install openpyxl for Excel datasets") from exc
        ws = load_workbook(p, read_only=True, data_only=True).worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() for h in next(rows)]
        out = []
        for row in rows:
            if all(v is None for v in row):
                continue
            out.append({h: v for h, v in zip(header, row) if h})
        return out

    raise ValueError(f"unsupported dataset format: {p.suffix}")
