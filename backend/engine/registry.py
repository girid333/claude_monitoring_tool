"""System registry (D-006, extended D-021) + interim credential handling.

Registry file (systems.json) holds NO credentials — that shape is final
(D-018). Credentials come from a CredentialProvider (login/base.py).

INTERIM provider (until OQ-8 is decided): environment variables
  SAP_<SID>_USER / SAP_<SID>_PASSWORD (+ SSH equivalents, see below)
This keeps secrets out of files entirely. It is a stopgap, not the OQ-8
answer — encryption-at-rest for a real secret store is still to be decided.

D-021: the registry now also carries the v1-parity fields CoreBasisSection
needs (hostname, instance_number, db_host, servers, url_checks,
ssh_auth_method) — all of these are non-secret connection topology, not
credentials, so they belong in the file per the same line D-006 already
drew. The dividing line stays crisp: identity (usernames) + secret material
(passwords, key paths) => env vars only; everything about WHERE to connect
=> registry, now editable via CRUD (main.py /api/systems) instead of by
hand-editing JSON.
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

    # D-021 — v1-parity connection topology (non-secret, CRUD-editable):
    hostname: str = ""                # RFC ashost; blank => derive from webgui_url host
    instance_number: str = "00"
    db_host: str = ""                 # blank => falls back to hostname
    servers: list[str] = Field(default_factory=list)   # extra app/dialog servers for SSH df -h
    url_checks: list[str] = Field(default_factory=list)  # health-check URLs
    ssh_auth_method: str = "password"  # "password" | "key" | "mfa" — a strategy choice, not a secret


class Registry(BaseModel):
    systems: list[SystemEntry]

    @classmethod
    def load(cls, path: str | Path = "systems.json") -> "Registry":
        p = Path(path)
        if not p.exists():
            return cls(systems=[])
        return cls(**json.loads(p.read_text()))

    def save(self, path: str | Path = "systems.json") -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n")

    def get(self, sid: str) -> SystemEntry:
        for s in self.systems:
            if s.sid == sid:
                return s
        raise KeyError(sid)

    def upsert(self, entry: SystemEntry) -> None:
        """Add entry, or replace the existing one with the same sid."""
        for i, s in enumerate(self.systems):
            if s.sid == entry.sid:
                self.systems[i] = entry
                return
        self.systems.append(entry)

    def remove(self, sid: str) -> bool:
        before = len(self.systems)
        self.systems = [s for s in self.systems if s.sid != sid]
        return len(self.systems) < before

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


def credential_status(sid: str, ssh_auth_method: str = "password") -> dict[str, bool]:
    """D-021: presence-only check for the Systems page's status badges.
    NEVER returns values, only booleans — this is the one credential-related
    read the UI is allowed to see."""
    sap_ok = bool(os.environ.get(f"SAP_{sid}_USER")) and bool(os.environ.get(f"SAP_{sid}_PASSWORD"))
    ssh_user_set = bool(os.environ.get(f"SAP_{sid}_SSH_USER"))
    if ssh_auth_method == "key":
        ssh_secret_set = bool(os.environ.get(f"SAP_{sid}_SSH_KEY_PATH"))
    else:
        ssh_secret_set = bool(os.environ.get(f"SAP_{sid}_SSH_PASSWORD"))
    return {
        "sap_credentials": sap_ok,
        "ssh_credentials": ssh_user_set and ssh_secret_set,
    }
