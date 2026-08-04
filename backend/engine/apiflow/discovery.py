"""D-047: what does this SAP system actually expose?

Two catalogues, both reachable over the already-working transport:

  1. OData / Gateway services — via SAP's own catalog service. A 404 here
     is a real answer ("Gateway isn't active"), not a failure.
  2. RFC-enabled function modules — from TFDIR (FMODE = 'R' marks
     remote-enabled) joined to TFTIT for descriptions. This is where
     custom Z_* modules show up, which is the cheapest route to covering
     bespoke transactions that have no Fiori app.

Everything degrades honestly: if one catalogue is unavailable, the other
is still returned, and the unavailable one says why.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..rfc.soap_client import RfcSoapClient, RfcSoapError

# SAP's Gateway catalog is itself an OData service. v=2 is the long-standing
# path; the unversioned one is tried as a fallback for older Gateway hubs.
CATALOG_PATHS = [
    "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection",
    "/sap/opu/odata/iwfnd/catalogservice/ServiceCollection",
]


@dataclass
class Catalogue:
    odata_available: bool = False
    odata_services: list[dict] = field(default_factory=list)
    odata_note: str = ""
    rfc_available: bool = False
    rfc_functions: list[dict] = field(default_factory=list)
    rfc_note: str = ""
    rfc_total_estimate: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "odata": {
                "available": self.odata_available,
                "count": len(self.odata_services),
                "services": self.odata_services,
                "note": self.odata_note,
            },
            "rfc": {
                "available": self.rfc_available,
                "count": len(self.rfc_functions),
                "functions": self.rfc_functions,
                "note": self.rfc_note,
                "total_estimate": self.rfc_total_estimate,
            },
        }


def _read_table(client: RfcSoapClient, table: str, fields: list[str],
                 where: Optional[str] = None, rowcount: int = 200) -> list[dict]:
    """RFC_READ_TABLE with fixed-width unpacking. DATA and OPTIONS must be
    declared as TABLES parameters or SAP returns nothing (D-045)."""
    reply = client.call(
        "RFC_READ_TABLE", QUERY_TABLE=table, DELIMITER="",
        FIELDS=[{"FIELDNAME": f} for f in fields], ROWCOUNT=rowcount,
        DATA=[], OPTIONS=[{"TEXT": where}] if where else [])
    cols = []
    for f in (reply.get("FIELDS") or []):
        if isinstance(f, dict):
            cols.append((f.get("FIELDNAME", "").strip(),
                          int(f.get("OFFSET", 0) or 0), int(f.get("LENGTH", 0) or 0)))
    data = reply.get("DATA") or []
    if isinstance(data, dict):
        data = [data]
    out = []
    for d in data:
        line = d.get("WA", "") if isinstance(d, dict) else str(d)
        out.append({name: line[off:off + ln].strip() for name, off, ln in cols})
    return out


def discover_odata(client: RfcSoapClient, limit: int = 200) -> tuple[bool, list[dict], str]:
    """List activated OData services via SAP's Gateway catalog service."""
    last_status = None
    for path in CATALOG_PATHS:
        try:
            status, text = client.http_get(path, params={"$format": "json"})
        except RfcSoapError as exc:
            return False, [], f"Could not reach the Gateway catalog: {exc.message}"
        last_status = status
        if status == 404:
            continue
        if status in (401, 403):
            return False, [], (
                "The Gateway catalog exists but this user is not authorized to read it "
                "(HTTP %d). The monitoring user needs access to the "
                "/sap/opu/odata/IWFND/CATALOGSERVICE service." % status)
        if status >= 400:
            return False, [], f"Gateway catalog returned HTTP {status}."
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False, [], ("Gateway catalog responded but not with JSON — the service "
                                "may be active without the JSON format enabled.")
        results = (payload.get("d") or {}).get("results")
        if results is None:
            results = payload.get("value") or []
        services = []
        for r in results[:limit]:
            if not isinstance(r, dict):
                continue
            services.append({
                "id": r.get("ID") or r.get("TechnicalServiceName") or "",
                "title": r.get("Title") or r.get("Description") or "",
                "service": r.get("TechnicalServiceName") or "",
                "version": str(r.get("TechnicalServiceVersion") or ""),
                "url": r.get("ServiceUrl") or "",
            })
        note = (f"{len(services)} activated OData service(s) found."
                if services else
                "The Gateway catalog is reachable but reports no activated services.")
        return True, services, note

    return False, [], (
        "No Gateway catalog service responded (HTTP %s). This system most likely has no "
        "active Fiori/OData layer, so flows here should be built from RFC-enabled "
        "function modules instead." % last_status)


def discover_rfc_functions(client: RfcSoapClient, pattern: str = "",
                            limit: int = 200,
                            custom_only: bool = False) -> tuple[bool, list[dict], str]:
    """Search remote-enabled function modules.

    TFDIR-FMODE = 'R' is what marks a function module remote-enabled, which
    is exactly the set callable over the transport this project already
    uses. Custom modules (Z*/Y*) are the interesting ones for bespoke
    transactions, hence `custom_only`."""
    clauses = ["FMODE EQ 'R'"]
    if custom_only:
        clauses.append("( FUNCNAME LIKE 'Z%' OR FUNCNAME LIKE 'Y%' )")
    elif pattern:
        safe = re.sub(r"[^A-Za-z0-9_%*/]", "", pattern).upper().replace("*", "%")
        if safe:
            if "%" not in safe:
                safe = f"%{safe}%"
            clauses.append(f"FUNCNAME LIKE '{safe}'")
    where = " AND ".join(clauses)

    try:
        rows = _read_table(client, "TFDIR", ["FUNCNAME", "FMODE"], where=where,
                            rowcount=limit)
    except RfcSoapError as exc:
        return False, [], (
            f"Could not read TFDIR: {exc.message} This usually means the user lacks "
            "S_TABU_* authorization for it.")

    names = [r.get("FUNCNAME", "") for r in rows if r.get("FUNCNAME")]
    titles: dict[str, str] = {}
    if names:
        # Descriptions are a nice-to-have; never let them fail the search.
        try:
            sample = names[:60]
            in_list = ",".join(f"'{n}'" for n in sample)
            trows = _read_table(client, "TFTIT", ["SPRAS", "FUNCNAME", "STEXT"],
                                 where=f"SPRAS EQ 'E' AND FUNCNAME IN ({in_list})",
                                 rowcount=limit)
            titles = {t.get("FUNCNAME", ""): t.get("STEXT", "") for t in trows}
        except RfcSoapError:
            titles = {}

    functions = [{
        "name": n,
        "description": titles.get(n, ""),
        "custom": n.startswith(("Z", "Y")),
    } for n in names]
    note = (f"{len(functions)} remote-enabled function module(s)"
            + (" matching your search." if (pattern or custom_only) else " (showing first "
               f"{limit}; use a search term to narrow).")) if functions else \
           "No remote-enabled function modules matched."
    return True, functions, note


def discover(client: RfcSoapClient, rfc_pattern: str = "",
             custom_only: bool = False, limit: int = 200) -> Catalogue:
    """Full capability inventory. Each half degrades independently — an
    unavailable Gateway never hides the RFC catalogue, and vice versa."""
    cat = Catalogue()
    cat.odata_available, cat.odata_services, cat.odata_note = discover_odata(client, limit)
    cat.rfc_available, cat.rfc_functions, cat.rfc_note = discover_rfc_functions(
        client, rfc_pattern, limit, custom_only)
    return cat
