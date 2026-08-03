"""Flow schema — the single contract between recorder, replay engine, and checks.

Implements D-008 (step kinds, window-relative IDs, {{param}} placeholders)
and D-012 (DOM candidate ranking: attribute > text/role > class fragment >
computed coordinate). See SKILL.md for the decision log.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class DomCandidates(BaseModel):
    """DOM-mode identification candidates, captured at record time.

    Ranked resolution order per D-012 (mirrors v1's proven selector priority):
      1. attr        — semantic attributes: name / title / id-substring
      2. label       — visible text or aria role+name (Playwright get_by_*)
      3. css_hint    — SAP UI-library class fragment (e.g. 'lsField__input')
      4. pos_id      — positional WebGUI id, last resort, low confidence

    D-033 additions (recorder is DOM-first now — see Target.sid): `text`,
    `tag`, `input_type`, `nth` and `path` give the resolver enough to find
    an element on a scripting-disabled system, which is the common case.
    """

    attr: Optional[dict[str, str]] = None      # e.g. {"name": "sap-user"} or {"title": "Job name"}
    label: Optional[str] = None                # visible label / accessible name
    role: Optional[str] = None                 # aria role, used with label
    css_hint: Optional[str] = None             # class fragment, not full selector
    pos_id: Optional[str] = None               # e.g. "M0:46:::4:20"
    text: Optional[str] = None                 # D-033: trimmed visible text
    tag: Optional[str] = None                  # D-033: lowercase tag name
    input_type: Optional[str] = None           # D-033: input's type attribute
    nth: Optional[int] = None                  # D-033: index among identical matches
    path: Optional[str] = None                 # D-033: structural CSS path, last resort
    frame_url: Optional[str] = None            # D-033: iframe the element lives in
    # D-040: SAP WebGUI checkboxes are ARIA widgets — <span role="checkbox"
    # aria-checked="true|false"> — not native <input type="checkbox">, and
    # they usually carry no distinguishing name/title/label. `checkbox_index`
    # is the element's 0-based position among every role="checkbox" element
    # on the screen at record time — a stable, deterministic key for a
    # selection screen that always renders the same checkboxes in the same
    # order (e.g. SM37 job overview: Scheduled/Released/Ready/Active/
    # Finished/Cancelled).
    checkbox_index: Optional[int] = None


class Target(BaseModel):
    """Abstract target descriptor (D-002): scripting ID + DOM candidates.

    D-033: `sid` is now OPTIONAL. The original schema required it because
    recording was assumed to happen on a scripting-enabled system (D-004).
    In practice SAP WebGUI installations commonly run with
    `sapgui/user_scripting = FALSE` — v1 monitors them fine without it — so
    a recorder that can only emit scripting IDs records nothing usable.
    DOM-only targets are first-class (that's D-003's whole premise); the
    scripting ID is captured as a bonus when the API happens to be there.
    """

    sid: Optional[str] = Field(
        None, description="Window-relative scripting ID when available, e.g. wnd[0]/usr/ctxtBTCH2170-FROM_DATE")
    dom: Optional[DomCandidates] = None


class Act(str, Enum):
    START_TCODE = "start_tcode"   # via OK-code field (v1-proven fast path)
    SET = "set"                   # text / checkbox / radio / combo value
    PRESS = "press"               # button press
    SELECT = "select"             # row / node / tab selection
    VKEY = "vkey"                 # virtual key (8=F8, 0=Enter, 3=F3, ...)
    WAIT_READY = "wait_ready"     # explicit sync barrier (see sync.py)
    SCROLL = "scroll"             # D-037: restore a scroll position before capturing
    CAPTURE = "capture"           # screenshot and/or structured read
    CHECK = "check"               # assertion on last capture


class CheckOp(str, Enum):
    EQ = "=="
    NE = "!="
    LT = "<"
    GT = ">"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    REGEX = "regex"


class Severity(str, Enum):
    """Severity model (D-014). Report chips map 1:1 to these."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Step(BaseModel):
    act: Act
    target: Optional[Target] = None
    value: Any = None

    # capture-specific
    mode: Optional[list[Literal["screenshot", "table", "statusbar", "field"]]] = None
    # D-039: paginated capture — scroll a page at a time and shoot each one,
    # stopping when the view stops changing (or after `pages`, max 25).
    pages: Optional[int] = None

    # check-specific
    type: Optional[Literal["row_count", "cell_value", "statusbar_type", "field_value", "text"]] = None
    op: Optional[CheckOp] = None
    column: Optional[str] = None
    severity: Optional[Severity] = None
    message: Optional[str] = None

    # bookkeeping
    note: Optional[str] = None                 # recorder-written human note
    timeout_s: float = 30.0


class Flow(BaseModel):
    flow_id: str
    description: str = ""
    applies_to: list[str] = Field(default_factory=lambda: ["*"])  # release-tag globs (D-006)
    params: dict[str, Any] = Field(default_factory=dict)          # defaults; datasets override
    steps: list[Step]

    schema_version: int = 1


class StepResult(BaseModel):
    index: int
    act: Act
    ok: bool
    resolver: Optional[Literal["scripting", "dom"]] = None
    confidence: Optional[float] = None          # DOM resolver confidence (D-003)
    detail: str = ""
    screenshot_path: Optional[str] = None
    screenshot_paths: Optional[list[str]] = None   # D-039: paginated capture
    extracted: Optional[Any] = None             # structured capture payload


class CheckResult(BaseModel):
    ok: bool
    severity: Severity
    message: str
    actual: Any = None
    expected: Any = None


class FlowRunResult(BaseModel):
    """Everything keyed by run_id + sid (D-010)."""

    run_id: str
    sid: str
    flow_id: str
    mode: Literal["scripting", "dom"]
    steps: list[StepResult] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    ok: bool = False
    error: Optional[str] = None
