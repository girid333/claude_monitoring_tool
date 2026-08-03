"""D-046: eCATT record/replay over RFC.

The actual goal: use SAP's OWN test tool for record and replay, instead of
this project maintaining a bespoke recorder.

Division of labour — this is the point of the approach:
  - RECORD happens inside SAP, in SECATT, using SAP's own recorder. It
    produces a Test Script (object type ECSC) and usually a Test
    Configuration (ECTC) that binds a script to variants/data. Nothing
    here records anything, so there is no recorder for us to maintain and
    no fragile screen-scraping to keep in step with SAP releases.
  - REPLAY is triggered from here via `ECATT_EXECUTE`, which is
    RFC-enabled, over the SOAP transport in soap_client.py. It returns
    E_RESULT_XML, which carries the pass/fail outcome and log.

Prerequisite gate (checked by `ecatt_status`): eCATT must be PERMITTED on
the client. SCC4's "CATT and eCATT Restrictions" writes T000-CATTKENNZ,
and the near-universal recommendation is to leave eCATT switched OFF in
production because it can change data. If it's off, ECATT_EXECUTE will
refuse regardless of authorizations — so this is checked and reported
plainly BEFORE offering to run anything, rather than surfacing as a
confusing runtime failure.

Isolation: this module is separate from basis_checks.py and touches
nothing in the existing Core BASIS section, WebGUI engine, or run
pipeline. Giri's working monitoring is not affected by anything here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .soap_client import RfcSoapClient, RfcSoapError

# T000-CATTKENNZ (domain CCCATT) — SCC4's "CATT and eCATT Restrictions".
# Blank is the locked-down default; the others progressively permit eCATT.
CATT_FLAG_MEANING = {
    "": ("not_allowed", "eCATT and CATT are NOT allowed on this client"),
    " ": ("not_allowed", "eCATT and CATT are NOT allowed on this client"),
    "X": ("allowed", "eCATT and CATT allowed"),
    "E": ("allowed", "eCATT allowed (CATT not allowed)"),
    "T": ("trusted_only", "eCATT allowed only for trusted RFC connections"),
}

OBJECT_KINDS = {
    "ECSC": "Test script",
    "ECTC": "Test configuration",
    "ECTD": "Test data container",
    "ECSD": "System data container",
}


def _read_table(client: RfcSoapClient, table: str, fields: list[str],
                 where: Optional[str] = None, rowcount: int = 500) -> list[dict]:
    """RFC_READ_TABLE with the fixed-width unpacking already handled.

    DATA and OPTIONS are TABLES parameters — SAP returns nothing at all if
    they aren't declared in the request (learned the hard way in D-045)."""
    params: dict[str, Any] = {
        "QUERY_TABLE": table,
        "DELIMITER": "",
        "FIELDS": [{"FIELDNAME": f} for f in fields],
        "ROWCOUNT": rowcount,
        "DATA": [],
        "OPTIONS": [{"TEXT": where}] if where else [],
    }
    reply = client.call("RFC_READ_TABLE", **params)
    cols = []
    for f in (reply.get("FIELDS") or []):
        if isinstance(f, dict):
            cols.append((f.get("FIELDNAME", "").strip(),
                          int(f.get("OFFSET", 0) or 0), int(f.get("LENGTH", 0) or 0)))
    out = []
    data = reply.get("DATA") or []
    if isinstance(data, dict):
        data = [data]
    for d in data:
        line = d.get("WA", "") if isinstance(d, dict) else str(d)
        out.append({name: line[off:off + ln].strip() for name, off, ln in cols})
    return out


@dataclass
class EcattStatus:
    ok: bool
    allowed: bool
    state: str
    message: str
    client: str = ""
    client_name: str = ""
    raw_flag: str = ""
    counts: Optional[dict] = None
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "allowed": self.allowed, "state": self.state,
            "message": self.message, "client": self.client,
            "client_name": self.client_name, "raw_flag": self.raw_flag,
            "counts": self.counts or {}, "error": self.error,
        }


def ecatt_status(client: RfcSoapClient) -> EcattStatus:
    """Is eCATT usable on this client, and what already exists?

    Answers the gating question before any replay is attempted."""
    sap_client = client.target.client
    try:
        rows = _read_table(client, "T000", ["MANDT", "MTEXT", "CCCATEGORY", "CATTKENNZ"],
                            where=f"MANDT EQ '{sap_client}'", rowcount=10)
    except RfcSoapError as exc:
        return EcattStatus(ok=False, allowed=False, state="unknown",
                            message="Could not read the client settings table T000.",
                            error=exc.message)
    if not rows:
        return EcattStatus(ok=False, allowed=False, state="unknown",
                            message=f"No T000 entry found for client {sap_client}.")

    row = rows[0]
    flag = (row.get("CATTKENNZ") or "").strip()
    state, meaning = CATT_FLAG_MEANING.get(flag, ("unknown", f"Unrecognised flag {flag!r}"))
    allowed = state in ("allowed", "trusted_only")

    counts = {}
    for kind in ("ECSC", "ECTC"):
        try:
            counts[kind] = len(list_objects(client, kind, limit=500))
        except RfcSoapError:
            counts[kind] = None

    if allowed:
        message = (f"{meaning}. eCATT scripts can be executed on this client.")
    else:
        message = (
            f"{meaning}. ECATT_EXECUTE will refuse to run here regardless of user "
            f"authorizations. A BASIS admin can change this in SCC4 → client "
            f"{sap_client} → 'CATT and eCATT Restrictions'. Note this is commonly "
            f"left disabled on production clients on purpose, because eCATT can "
            f"change data.")

    return EcattStatus(ok=True, allowed=allowed, state=state, message=message,
                        client=sap_client, client_name=(row.get("MTEXT") or "").strip(),
                        raw_flag=flag or "(blank)", counts=counts)


def list_objects(client: RfcSoapClient, kind: str = "ECSC", limit: int = 500) -> list[dict]:
    """List eCATT repository objects of one kind.

    Uses TADIR rather than eCATT's own internal tables: eCATT objects are
    ordinary repository objects, TADIR is transparent and universally
    readable via RFC_READ_TABLE, and its layout doesn't drift between
    releases the way the ECTC_*/ECSCR_* internals might."""
    kind = kind.upper()
    if kind not in OBJECT_KINDS:
        raise ValueError(f"unknown eCATT object kind: {kind}")
    rows = _read_table(
        client, "TADIR", ["PGMID", "OBJECT", "OBJ_NAME", "DEVCLASS", "AUTHOR"],
        where=f"PGMID EQ 'R3TR' AND OBJECT EQ '{kind}'", rowcount=limit)
    return [{
        "name": r.get("OBJ_NAME", ""), "kind": kind,
        "kind_label": OBJECT_KINDS[kind],
        "package": r.get("DEVCLASS", ""), "author": r.get("AUTHOR", ""),
    } for r in rows if r.get("OBJ_NAME")]
