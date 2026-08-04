"""D-048: the API-flow schema — what replaces a recorded click sequence.

A flow is an ordered list of typed OPERATIONS plus the wiring between
them. Three things make it equivalent to (and more flexible than) a
recording:

  1. STEP-TO-STEP BINDING. A step can `extract` values from its result,
     and later steps reference them as {{step_id.name}}. That is what
     makes this a flow rather than a list of independent calls — reading
     an alert then confirming that specific alert, creating an object
     then reading it back.
  2. PARAMETERS. {{param.name}} is substituted at replay time, so one
     flow is a template you run with different inputs — the same need
     D-043's overrides addressed, but first-class here.
  3. CHECKS. Assertions over step results, so a flow says whether the
     system behaved correctly, not merely that calls returned.

Every operation is bound to something DISCOVERED (an entity set or
function import from $metadata, or a remote-enabled function module), so
there are no guessed names anywhere in a flow.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# {{s1.guid}} / {{param.date}} — deliberately the same double-brace style
# already used by the WebGUI flow schema (D-008) so the concept is familiar.
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

STEP_KINDS = ("odata_read", "odata_function", "rfc_call")
CHECK_OPS = ("row_count_min", "row_count_max", "equals", "contains",
             "not_empty", "step_ok")


@dataclass
class FlowStep:
    id: str
    kind: str
    description: str = ""
    # odata_read / odata_function
    service: str = ""
    entity_set: str = ""
    function_import: str = ""
    filter: str = ""
    select: str = ""
    top: int = 20
    # rfc_call
    function: str = ""
    tables: list[str] = field(default_factory=list)
    # shared
    params: dict[str, Any] = field(default_factory=dict)
    # name -> path into this step's result, e.g. "rows[0].AlertGuid"
    extract: dict[str, str] = field(default_factory=dict)
    # a step may be allowed to fail without failing the flow
    optional: bool = False

    @staticmethod
    def from_dict(d: dict) -> "FlowStep":
        known = {f for f in FlowStep.__dataclass_fields__}  # noqa: SLF001
        return FlowStep(**{k: v for k, v in d.items() if k in known})

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class FlowCheck:
    step: str
    op: str
    value: Any = None
    path: str = ""
    description: str = ""

    @staticmethod
    def from_dict(d: dict) -> "FlowCheck":
        known = {f for f in FlowCheck.__dataclass_fields__}  # noqa: SLF001
        return FlowCheck(**{k: v for k, v in d.items() if k in known})

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ApiFlow:
    flow_id: str
    description: str = ""
    applies_to: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)   # defaults
    steps: list[FlowStep] = field(default_factory=list)
    checks: list[FlowCheck] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict) -> "ApiFlow":
        return ApiFlow(
            flow_id=d.get("flow_id", ""),
            description=d.get("description", ""),
            applies_to=d.get("applies_to", []) or [],
            params=d.get("params", {}) or {},
            steps=[FlowStep.from_dict(s) for s in d.get("steps", [])],
            checks=[FlowCheck.from_dict(c) for c in d.get("checks", [])],
        )

    def as_dict(self) -> dict:
        return {
            "flow_id": self.flow_id, "description": self.description,
            "applies_to": self.applies_to, "params": self.params,
            "steps": [s.as_dict() for s in self.steps],
            "checks": [c.as_dict() for c in self.checks],
        }

    def validate(self) -> list[str]:
        """Structural problems, reported ALL AT ONCE.

        Catching these before execution matters: a flow that references a
        later step or an unknown parameter would otherwise fail halfway
        through, having already performed real calls against SAP."""
        problems: list[str] = []
        if not self.flow_id:
            problems.append("flow_id is required")
        seen: set[str] = set()
        for i, step in enumerate(self.steps):
            where = f"step {i + 1}" + (f" ({step.id})" if step.id else "")
            if not step.id:
                problems.append(f"{where}: id is required")
            elif step.id in seen:
                problems.append(f"{where}: duplicate step id {step.id!r}")
            seen.add(step.id)
            if step.kind not in STEP_KINDS:
                problems.append(f"{where}: unknown kind {step.kind!r} "
                                 f"(expected one of {', '.join(STEP_KINDS)})")
            if step.kind == "odata_read" and not (step.service and step.entity_set):
                problems.append(f"{where}: odata_read needs service and entity_set")
            if step.kind == "odata_function" and not (step.service and step.function_import):
                problems.append(f"{where}: odata_function needs service and function_import")
            if step.kind == "rfc_call" and not step.function:
                problems.append(f"{where}: rfc_call needs function")

            # references may only point BACKWARD, to steps already run
            for ref_step, ref_name in self._references(step):
                if ref_step == "param":
                    if ref_name not in self.params:
                        problems.append(
                            f"{where}: references {{{{param.{ref_name}}}}} but the flow "
                            f"declares no parameter {ref_name!r}")
                elif ref_step not in seen or ref_step == step.id:
                    problems.append(
                        f"{where}: references {{{{{ref_step}.{ref_name}}}}} which is not "
                        "an earlier step")
        for c in self.checks:
            if c.step and c.step not in seen:
                problems.append(f"check references unknown step {c.step!r}")
            if c.op not in CHECK_OPS:
                problems.append(f"check has unknown op {c.op!r} "
                                 f"(expected one of {', '.join(CHECK_OPS)})")
        return problems

    @staticmethod
    def _references(step: FlowStep) -> list[tuple[str, str]]:
        blob = json.dumps(step.as_dict())
        return [(m.group(1), m.group(2)) for m in PLACEHOLDER.finditer(blob)]


# ── storage (its own directory, like every other additive capability) ────

def flows_dir() -> Path:
    import os
    return Path(os.environ.get("API_FLOWS_DIR", "flows_api"))


def safe_flow_id(flow_id: str) -> bool:
    return bool(flow_id) and "/" not in flow_id and "\\" not in flow_id \
        and not flow_id.startswith(".")


def save_flow(flow: ApiFlow) -> Path:
    d = flows_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{flow.flow_id}.json"
    path.write_text(json.dumps(flow.as_dict(), indent=2))
    return path


def load_flow(flow_id: str) -> ApiFlow:
    path = flows_dir() / f"{flow_id}.json"
    return ApiFlow.from_dict(json.loads(path.read_text()))


def list_flows() -> list[dict]:
    d = flows_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except Exception:  # noqa: BLE001,PERF203 - skip unreadable files
            continue
        out.append({
            "flow_id": data.get("flow_id", p.stem),
            "description": data.get("description", ""),
            "applies_to": data.get("applies_to", []),
            "step_count": len(data.get("steps", [])),
            "check_count": len(data.get("checks", [])),
            "params": list((data.get("params") or {}).keys()),
        })
    return out


def delete_flow(flow_id: str) -> bool:
    path = flows_dir() / f"{flow_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True
