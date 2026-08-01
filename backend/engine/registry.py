"""System registry (D-006) + interim credential handling.

Registry file (systems.json) holds NO credentials — that shape is final.
Credentials come from a CredentialProvider (login/base.py).

INTERIM provider (until OQ-8 is decided): environment variables
  SAP_<SID>_USER / SAP_<SID>_PASSWORD
This keeps secrets out of files entirely. It is a stopgap, not the OQ-8
answer — encryption-at-rest for a real secret store is still to be decided.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .login.base import CredentialProvider


class SystemEntry(BaseModel):
    sid: str
    webgui_url: str
    client: str
    release_tag: str = "UNKNOWN"      # set from probe (e.g. "S4_2023")
    auth: str = "basic"               # login plugin name
    tags: list[str] = Field(default_factory=list)
    rfc_enrichment: bool = False      # D-013: optional, never a gate


class Registry(BaseModel):
    systems: list[SystemEntry]

    @classmethod
    def load(cls, path: str | Path = "systems.json") -> "Registry":
        return cls(**json.loads(Path(path).read_text()))

    def get(self, sid: str) -> SystemEntry:
        for s in self.systems:
            if s.sid == sid:
                return s
        raise KeyError(sid)

    def matching(self, applies_to: list[str]) -> list[SystemEntry]:
        """Flow compatibility resolution (D-006): glob match on release_tag."""
        return [s for s in self.systems
                if any(fnmatch.fnmatch(s.release_tag, pat) for pat in applies_to)]


class EnvCredentialProvider(CredentialProvider):
    def get(self, sid: str) -> dict[str, str]:
        user = os.environ.get(f"SAP_{sid}_USER")
        password = os.environ.get(f"SAP_{sid}_PASSWORD")
        if not user or not password:
            raise RuntimeError(
                f"Set SAP_{sid}_USER and SAP_{sid}_PASSWORD environment "
                f"variables (interim credential handling pending OQ-8)")
        return {"user": user, "password": password}
