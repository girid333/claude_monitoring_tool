"""System registry (D-006, extended D-021, D-023) + interim credential
handling.

Registry file (systems.json) holds NO SAP WebGUI login credentials — that
part of D-018 is unchanged. Those come from a CredentialProvider
(login/base.py):

INTERIM provider (until OQ-8's WebGUI-login piece is decided): environment
variables SAP_<SID>_USER / SAP_<SID>_PASSWORD. This keeps WebGUI login
secrets out of files entirely. Stopgap, not final.

D-023 SUPERSEDES D-018 for SSH credentials specifically (Giri's explicit
request, 2026-08-01): SSH usernames/passwords/key-paths for the app/dialog
servers and DB hosts a Core BASIS run SSHes into for `df -h` filesystem
checks now live IN the registry, per-host, because a real landscape can
have different SSH credentials per box — env vars keyed only by SID can't
express that. Passwords are never written in plaintext: `engine/crypto.py`
(Fernet, key from REGISTRY_ENCRYPTION_KEY) encrypts them before
`Registry.save()` ever touches disk. SSH key-based auth stores only a path
to a key file already present on the backend host's filesystem — never the
key material itself. SAP WebGUI login credentials are NOT part of this
change and remain env-var-only.

D-021: the registry also carries the v1-parity topology fields
CoreBasisSection needs (hostname, instance_number, db_hosts, servers,
url_checks) — editable via CRUD (main.py /api/systems) instead of by
hand-editing JSON.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .login.base import CredentialProvider


class ServerCredential(BaseModel):
    """D-023: an SSH credential, encrypted at rest. `password_enc` is a
    Fernet ciphertext (engine/crypto.py) — never plaintext on disk. For
    auth_method="key", `key_path` points at a private key file that already
    exists on the backend host; the key's contents are never stored here."""

    auth_method: str = "password"  # "password" | "key"
    username: str = ""
    password_enc: str = ""         # empty when auth_method == "key"
    key_path: str = ""             # empty when auth_method == "password"

    def is_set(self) -> bool:
        if not self.username:
            return False
        return bool(self.key_path) if self.auth_method == "key" else bool(self.password_enc)


class ServerEntry(BaseModel):
    """One app/dialog server or DB host (D-023). `credential=None` means
    "inherit the system's default `ssh_credential`" — matches v1's model of
    a system-wide SSH credential with optional per-server override."""

    hostname: str
    credential: ServerCredential | None = None

    def effective_credential(self, system_default: "ServerCredential") -> "ServerCredential":
        return self.credential if self.credential is not None else system_default


class SystemEntry(BaseModel):
    sid: str
    webgui_url: str
    client: str
    release_tag: str = "UNKNOWN"      # set from probe (e.g. "S4_2023")
    auth: str = "basic"               # login plugin name
    tags: list[str] = Field(default_factory=list)
    rfc_enrichment: bool = False      # D-013: optional, never a gate

    # D-021 — v1-parity connection topology (non-secret):
    hostname: str = ""                # RFC ashost; blank => derive from webgui_url host
    instance_number: str = "00"
    url_checks: list[str] = Field(default_factory=list)  # health-check URLs

    # D-023 — app/dialog servers and DB hosts, each independently 0/1/many,
    # each optionally overriding the system's default SSH credential:
    servers: list[ServerEntry] = Field(default_factory=list)
    db_hosts: list[ServerEntry] = Field(default_factory=list)  # was singular db_host: str pre-D-023
    ssh_auth_method: str = "password"  # "password" | "key" — default for ssh_credential below
    ssh_credential: ServerCredential = Field(default_factory=ServerCredential)  # D-023: default/fallback


def _migrate_system_dict(d: dict) -> None:
    """D-023 backward-compat shim, mutates `d` in place. Pre-D-023
    systems.json entries have `servers: list[str]` and a singular
    `db_host: str`; upgrade both to the new `ServerEntry`-list shape so an
    existing registry (e.g. Giri's already-added F4H system) doesn't break
    on the first load after this upgrade, instead of forcing a re-entry."""
    servers = d.get("servers")
    if servers and isinstance(servers[0], str):
        d["servers"] = [{"hostname": h} for h in servers]
    db_host = d.pop("db_host", None)
    if db_host and not d.get("db_hosts"):
        d["db_hosts"] = [{"hostname": db_host}]


class Registry(BaseModel):
    systems: list[SystemEntry]

    @classmethod
    def load(cls, path: str | Path = "systems.json") -> "Registry":
        p = Path(path)
        if not p.exists():
            return cls(systems=[])
        raw = json.loads(p.read_text())
        for sys_raw in raw.get("systems", []):
            _migrate_system_dict(sys_raw)
        return cls(**raw)

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


def credential_status(entry: SystemEntry) -> dict:
    """D-023: presence-only status for the Systems page's badges. SAP
    WebGUI login is still env-var-only (D-018 unchanged for that piece) —
    checked against env vars. SSH credentials are now registry-stored
    (D-023) — checked against the entry's own encrypted fields, never
    decrypted here; `ServerCredential.is_set()` only looks at whether
    password_enc/key_path is non-empty, not what it contains."""
    sap_ok = bool(os.environ.get(f"SAP_{entry.sid}_USER")) and bool(os.environ.get(f"SAP_{entry.sid}_PASSWORD"))
    hosts = entry.servers + entry.db_hosts
    host_status = [
        {"hostname": h.hostname, "credential_set": h.effective_credential(entry.ssh_credential).is_set()}
        for h in hosts
    ]
    return {
        "sap_credentials": sap_ok,
        "ssh_default_credential": entry.ssh_credential.is_set(),
        "ssh_hosts": host_status,
    }
