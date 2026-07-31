"""Resolver contract (D-002): given an abstract Target, produce something
the engine can act on, plus a confidence score.

Two implementations:
  - ScriptingResolver: exact, confidence 1.0, requires sapgui/user_scripting
  - DomResolver: ranked candidates per D-012, confidence < 1.0, works everywhere

Confidence semantics (D-003): the engine records confidence per step; any
step resolved below CONFIDENCE_FLAG_THRESHOLD is flagged in the report,
never silently accepted.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from ..schema import Target

CONFIDENCE_FLAG_THRESHOLD = 0.75


@dataclass
class Resolution:
    handle: Any                 # scripting id string OR Playwright Locator
    confidence: float           # 1.0 exact ... 0.0 unresolvable
    via: str                    # human-readable: which tier matched
    resolver: str               # "scripting" | "dom"


class ResolveError(Exception):
    def __init__(self, target: Target, tried: list[str]):
        self.target = target
        self.tried = tried
        super().__init__(f"Could not resolve target sid={target.sid!r}; tried: {tried}")


class Resolver(ABC):
    name: str

    def __init__(self, page: Page):
        self.page = page

    @abstractmethod
    async def resolve(self, target: Target) -> Resolution: ...

    @abstractmethod
    async def set_value(self, res: Resolution, value: Any) -> None: ...

    @abstractmethod
    async def press(self, res: Resolution) -> None: ...

    @abstractmethod
    async def send_vkey(self, vkey: int, window_sid: str = "wnd[0]") -> None: ...

    @abstractmethod
    async def read_table(self, res: Resolution) -> list[dict[str, str]]: ...

    @abstractmethod
    async def read_statusbar(self) -> dict[str, str]: ...
