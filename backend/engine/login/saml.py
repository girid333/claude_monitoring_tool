"""SamlLoginPlugin (D-015) — skeleton; needs the reference SSO system to build.

Strategy:
  1. Persistent browser context (storage_state per sid) so live SSO cookies
     skip the IdP entirely on most runs.
  2. Cold path: goto webgui URL -> follow IdP redirect -> submit IdP form
     (IdP-specific sub-plugin: ADFS / Azure AD / SAP IAS templates) -> land
     back on WebGUI.
  3. MFA CANNOT be answered headlessly. Mitigations (preference order, per
     D-015): conditional-access exemption for the monitoring user
     (IP-restricted), X.509 client cert instead of SAML, or long-lived
     cookies + alerting when interactive re-auth is required.

TODO Phase 1 (blocked on OQ-2 reference SSO system):
  - implement cookie warm/reuse via context.storage_state
  - implement one concrete IdP form template
  - expiry detection -> raise NeedsInteractiveAuth for the scheduler to alert
"""

from __future__ import annotations

from playwright.async_api import Page

from .base import LoginPlugin


class NeedsInteractiveAuth(Exception):
    """Raised when SSO cookies are expired and the IdP demands MFA."""


class SamlLoginPlugin(LoginPlugin):
    name = "saml"

    async def login(self, page: Page) -> None:
        raise NotImplementedError(
            "SAML plugin lands with the reference SSO system (OQ-2). "
            "See module docstring for the agreed strategy (D-015)."
        )
