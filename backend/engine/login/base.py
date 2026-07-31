"""Login plugin contract (D-007). Login runs BEFORE the flow engine and the
probe — plain Playwright territory, the scripting API doesn't exist yet.

Central re-auth (Known Risks: v1 had none): the engine calls
detect_logged_out() between steps on failure; if True, it re-runs login()
and restarts the current flow.

Credentials arrive via an injected provider, never read from disk here —
encryption-at-rest is OQ-8, undecided; do not bake in plaintext handling.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from playwright.async_api import Page


@dataclass
class SystemTarget:
    sid: str
    webgui_url: str
    client: str


class CredentialProvider(ABC):
    """Implementation decided by OQ-8. Engine only ever sees this interface."""

    @abstractmethod
    def get(self, sid: str) -> dict[str, str]: ...


class LoginPlugin(ABC):
    name: str

    def __init__(self, system: SystemTarget, credentials: CredentialProvider):
        self.system = system
        self.credentials = credentials

    @abstractmethod
    async def login(self, page: Page) -> None: ...

    async def detect_logged_out(self, page: Page) -> bool:
        """Default heuristic: login form or ICM error page present."""
        try:
            if await page.locator('input[name="sap-user"]').count() > 0:
                return True
            body = (await page.locator("body").inner_text())[:500]
            return "500 SAP Internal Server Error" in body or "ICM" in body and "Error" in body
        except Exception:
            return False
