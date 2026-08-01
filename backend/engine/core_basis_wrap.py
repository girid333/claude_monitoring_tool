"""D-017/CoreBasisSection support: import + resiliently wrap v1's orchestrator
(../sap_monitoring) WITHOUT modifying any v1 file.

Two problems solved here, both without touching sap_monitoring/backend/*.py:

1. Import-time coupling to RFC. v1's orchestrator.py does
   `from sap_rfc_client import SAPRfcClient` at module scope, and
   sap_rfc_client.py does `from pyrfc import Connection, RFCError` at module
   scope. On any host without the NetWeaver RFC SDK + pyrfc installed,
   `import orchestrator` raises ModuleNotFoundError before a single line of
   v1 code runs. If pyrfc *is* importable, we leave the real module alone.

2. Runtime coupling to RFC (D-013). v1's run_sap_checks() raises and aborts
   the whole run if RFC login fails, even though RFC supplies real data for
   only ~4 of 23 T-codes (see SKILL.md §6.2). Rather than duplicating v1's
   ~175-line run_sap_checks() loop to remove the abort, we monkeypatch the
   *name* `orchestrator.SAPRfcClient` (a module attribute, resolved at call
   time when run_sap_checks() executes `SAPRfcClient(...)`) to point at
   ResilientRfcClient. Its connect() always reports success to v1's abort
   check, while internally tracking whether the real RFC connection actually
   came up; execute_tcode_check()/get_filesystem_data() degrade to honest
   "RFC unavailable" error payloads v1's own extract_anomalies() already
   knows how to render. This is a wrap, not an edit: no v1 source line
   changes, and the v1 loop's structure/behavior for the 19 non-RFC T-codes
   (screenshot + regex analysis) is completely untouched.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

_PYRFC_AVAILABLE: bool | None = None


def _pyrfc_available() -> bool:
    global _PYRFC_AVAILABLE
    if _PYRFC_AVAILABLE is None:
        try:
            importlib.import_module("pyrfc")
            _PYRFC_AVAILABLE = True
        except Exception:
            _PYRFC_AVAILABLE = False
    return _PYRFC_AVAILABLE


def _install_sap_rfc_client_stub() -> None:
    """Only used when pyrfc itself can't be imported in this environment.
    Gives orchestrator.py's top-level `from sap_rfc_client import
    SAPRfcClient` something importable so v1 loads at all. ResilientRfcClient
    (below) is what actually runs during a section; this stub only needs to
    exist, not be used."""

    if "sap_rfc_client" in sys.modules:
        return

    stub = ModuleType("sap_rfc_client")

    class SAPRfcClient:  # noqa: N801 - matches v1's public name
        def __init__(self, ashost=None, sysnr=None, client=None, user=None, passwd=None):
            pass

        def connect(self) -> bool:
            return False

        def disconnect(self) -> None:
            pass

        def execute_tcode_check(self, tcode: str) -> dict:
            return {"status": "error", "message": "pyrfc/nwrfcsdk not installed on this host"}

        def get_filesystem_data(self) -> dict:
            return {"status": "error", "message": "pyrfc/nwrfcsdk not installed on this host"}

        def get_system_info(self) -> dict:
            return {}

    stub.SAPRfcClient = SAPRfcClient
    sys.modules["sap_rfc_client"] = stub


class ResilientRfcClient:
    """Drop-in proxy for v1's SAPRfcClient (D-013).

    connect() never signals failure upward — v1's `if not login_success:
    raise` gate never fires — while the real connection (if pyrfc is
    available) is still attempted underneath. Every other call degrades to
    an honest error payload when the real client isn't available/connected,
    which v1's own anomaly extraction already renders as "RFC Error - ...".

    `availability_log` is a class-level list of bools, one per connect()
    call, reset per CoreBasisSection run so the section can report whether
    RFC enrichment actually worked this run. NOT concurrency-safe across
    parallel systems (D-010 is single-system-at-a-time today); if the engine
    ever parallelizes per-system runs, this needs to move to an
    instance/run-scoped sink instead of a class attribute.
    """

    availability_log: list[bool] = []

    def __init__(self, ashost=None, sysnr=None, client=None, user=None, passwd=None):
        self._real = None
        self._available = False
        if _pyrfc_available():
            try:
                real_mod = importlib.import_module("sap_rfc_client")
                self._real = real_mod.SAPRfcClient(
                    ashost=ashost, sysnr=sysnr, client=client, user=user, passwd=passwd)
            except Exception:
                self._real = None

    def connect(self) -> bool:
        if self._real is not None:
            try:
                self._available = bool(self._real.connect())
            except Exception:
                self._available = False
        else:
            self._available = False
        ResilientRfcClient.availability_log.append(self._available)
        return True  # D-013: never gate v1's run on RFC

    def disconnect(self) -> None:
        if self._real is not None:
            try:
                self._real.disconnect()
            except Exception:
                pass

    def execute_tcode_check(self, tcode: str) -> dict:
        if self._available and self._real is not None:
            try:
                return self._real.execute_tcode_check(tcode)
            except Exception as exc:
                return {"status": "error", "message": f"RFC call failed: {exc}"}
        return {"status": "error",
                "message": "RFC unavailable this run (D-013: optional enrichment, "
                            "WebGUI monitoring continues)"}

    def get_filesystem_data(self) -> dict:
        if self._available and self._real is not None:
            try:
                return self._real.get_filesystem_data()
            except Exception as exc:
                return {"status": "error", "message": str(exc)}
        return {"status": "error", "message": "RFC unavailable this run"}

    def get_system_info(self) -> dict:
        if self._available and self._real is not None:
            try:
                return self._real.get_system_info()
            except Exception:
                return {}
        return {}

    @classmethod
    def reset_log(cls) -> None:
        cls.availability_log = []

    @classmethod
    def any_available(cls) -> bool:
        return any(cls.availability_log)


def load_v1_orchestrator(v1_path: str | Path):
    """Make ../sap_monitoring/backend importable and return its orchestrator
    module with SAPRfcClient monkeypatched to ResilientRfcClient. Idempotent
    across repeated calls (e.g. once per system in a multi-system run).

    Never writes to or imports-and-mutates sap_monitoring source files; the
    only mutation is the `orchestrator` *module attribute* `SAPRfcClient`,
    which is how run_sap_checks() looks up the name at call time.
    """

    v1_backend = (Path(v1_path) / "backend").resolve()
    if not v1_backend.is_dir():
        raise FileNotFoundError(f"v1 backend not found at {v1_backend}")

    path_str = str(v1_backend)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

    if not _pyrfc_available():
        _install_sap_rfc_client_stub()

    orchestrator_mod = importlib.import_module("orchestrator")
    orchestrator_mod.SAPRfcClient = ResilientRfcClient
    return orchestrator_mod


def build_v1_config(sys_entry, creds: dict[str, str]) -> dict:
    """Map a v2 SystemEntry (registry.py, D-006/D-021/D-023) + SAP WebGUI
    credentials (D-018) into the flat config dict v1's ReportOrchestrator
    expects.

    D-021: connection topology (hostname, instance_number, url_checks) lives
    in the registry, CRUD-editable via /api/systems.

    D-023: SSH credentials for app/dialog servers and DB hosts now live in
    the registry too (encrypted at rest, engine/crypto.py) instead of env
    vars — decrypted here, at the last moment before handing off to v1,
    never logged or persisted. This required NO changes to v1's
    orchestrator.py (`sap_automator.py`/`orchestrator.py` stay untouched on
    disk, per D-017/D-019's "wrap not edit" rule): its
    `run_ssh_filesystem_checks()` already natively walks `config["servers"]`
    as a list of dicts with an optional `use_custom_auth` per-entry
    override — exactly the shape D-023 needed, already present in v1's code
    before this change. v1 only ever treats ONE db host specially
    (`config["db_host"] or config["hostname"]`, always using the *global*
    ssh_username/ssh_password/etc keys, no per-host override for that one
    slot) — rather than edit that to support multiple DB hosts, every
    registry `db_hosts` entry is folded into the same `servers` list
    v1 already resolves per-host, and `db_host` is left empty so v1's own
    fallback (implicit target = `hostname`, using the system's default
    ssh_credential) is the only thing using that single-slot mechanism —
    unchanged v1 behavior, not a regression.
    """

    from . import crypto

    host = sys_entry.hostname or (urlparse(sys_entry.webgui_url).hostname or sys_entry.webgui_url)

    def resolve(cred) -> dict:
        return {
            "ssh_auth_method": cred.auth_method,
            "ssh_username": cred.username,
            "ssh_password": crypto.decrypt(cred.password_enc) if cred.auth_method == "password" else "",
            "ssh_key_path": cred.key_path,
        }

    default_ssh = resolve(sys_entry.ssh_credential)

    def server_dict(entry) -> dict:
        r = resolve(entry.effective_credential(sys_entry.ssh_credential))
        return {"hostname": entry.hostname, "use_custom_auth": True, **r}

    servers_for_v1 = [server_dict(e) for e in (sys_entry.servers + sys_entry.db_hosts)]

    return {
        "hostname": host,
        "instance_number": sys_entry.instance_number or "00",
        "sap_client": sys_entry.client,
        "sap_username": creds["user"],
        "sap_password": creds["password"],
        "webgui_url": sys_entry.webgui_url,
        "servers": servers_for_v1,
        "url_checks": list(sys_entry.url_checks),
        "ssh_username": default_ssh["ssh_username"],
        "ssh_password": default_ssh["ssh_password"],
        "ssh_auth_method": default_ssh["ssh_auth_method"],
        "ssh_key_path": default_ssh["ssh_key_path"],
        # D-023: DB hosts now flow through `servers` above (per-host creds);
        # leaving this blank makes v1 fall back to its one implicit
        # `hostname` target using the default ssh_credential resolved above.
        "db_host": "",
    }
