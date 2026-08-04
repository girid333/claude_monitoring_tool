"""D-047b: read an OData service's $metadata and turn it into a list of
concrete, callable OPERATIONS.

This is the piece that makes flow composition possible without recording
anything. `$metadata` is EDMX — SAP's own machine-readable contract for a
service — listing every entity set, its key fields, every property with
its type, and every function import with its HTTP method and parameters.

Two properties matter for this project:
  - It is DISCOVERED, not guessed. Every field name and type comes from
    the system itself, which is the opposite of the D-045 experience of
    guessing table/field names and getting silent empty results.
  - It is a CONTRACT SAP maintains across releases, unlike DOM structure
    or GUI control trees.

So a flow step becomes: "GET entity set X filtered by Y" or "call
function import Z with these parameters" — bound to real metadata, with
the UI able to show exactly which parameters exist and what type each is.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from ..rfc.soap_client import RfcSoapClient, RfcSoapError


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def sap_error_message(text: str) -> str:
    """Pull SAP's human-readable message out of an OData error payload.

    Worth its own function because the message is the whole diagnostic:
    a bare 'HTTP 500' is useless, whereas 'No System Alias found for
    Service X and user Y' tells you precisely what to configure. SAP emits
    this in JSON or XML depending on the request, so both are handled."""
    if not text:
        return ""
    for pattern in (
        r'"message"\s*:\s*{[^}]*?"value"\s*:\s*"((?:[^"\\]|\\.)+)"',
        r'"message"\s*:\s*"((?:[^"\\]|\\.)+)"',
        r"<message[^>]*>([^<]+)</message>",
        r"<errordetail>.*?<message>([^<]+)</message>",
    ):
        m = re.search(pattern, text, re.S)
        if m:
            return m.group(1).replace("\\n", " ").replace('\\"', '"').strip()
    return ""


def classify_failure(status: int, text: str) -> tuple[str, str]:
    """Map an OData failure to (state, actionable explanation).

    'No System Alias' is called out explicitly because it's the single most
    likely reason a service appears in the catalogue but can't be called on
    a Gateway hub — and because the fix is a specific, nameable action
    rather than a vague permissions hand-wave."""
    msg = sap_error_message(text)
    low = (msg or text or "").lower()
    if "system alias" in low:
        return "no_system_alias", (
            (msg or "No system alias found for this service.")
            + " The service is registered but Gateway has no route to a backend for it. "
              "A BASIS admin assigns one in /IWFND/MAINT_SERVICE → select the service → "
              "'Add System Alias'.")
    if status in (401, 403) or "not authorized" in low or "no authorization" in low:
        return "unauthorized", (msg or f"Not authorized (HTTP {status}).")
    if status == 404:
        return "not_found", (msg or "Service not found — it may not be activated in "
                                     "/IWFND/MAINT_SERVICE.")
    return "error", (msg or f"HTTP {status}.")


def service_path(service_url: str) -> str:
    """The catalog returns absolute URLs (https://host/sap/opu/odata/...),
    but calls must go to the host we're already authenticated against —
    which may differ (load balancer, different FQDN, http vs https). Keep
    only the path so the existing session and credentials still apply."""
    if service_url.startswith(("http://", "https://")):
        return urlsplit(service_url).path
    return service_url if service_url.startswith("/") else "/" + service_url


@dataclass
class ODataProperty:
    name: str
    type: str
    nullable: bool = True
    is_key: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "type": self.type,
                "nullable": self.nullable, "is_key": self.is_key}


@dataclass
class ODataOperation:
    """One callable thing: an entity set read, or a function import."""
    kind: str                 # "entityset" | "function"
    name: str
    http_method: str = "GET"
    entity_type: str = ""
    keys: list[str] = field(default_factory=list)
    properties: list[ODataProperty] = field(default_factory=list)
    parameters: list[ODataProperty] = field(default_factory=list)
    creatable: bool = False
    updatable: bool = False
    deletable: bool = False
    addressable: bool = True

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "name": self.name, "http_method": self.http_method,
            "entity_type": self.entity_type, "keys": self.keys,
            "properties": [p.as_dict() for p in self.properties],
            "parameters": [p.as_dict() for p in self.parameters],
            "creatable": self.creatable, "updatable": self.updatable,
            "deletable": self.deletable, "addressable": self.addressable,
        }


@dataclass
class ServiceMetadata:
    ok: bool
    service: str
    operations: list[ODataOperation] = field(default_factory=list)
    note: str = ""
    error: str = ""
    state: str = ""   # usable | no_system_alias | unauthorized | not_found | error

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "service": self.service, "state": self.state,
            "count": len(self.operations),
            "operations": [o.as_dict() for o in self.operations],
            "note": self.note, "error": self.error,
        }


def _bool_attr(el: ET.Element, *names: str, default: bool = False) -> bool:
    for key, value in el.attrib.items():
        if _local(key) in names:
            return str(value).lower() == "true"
    return default


def _attr(el: ET.Element, *names: str, default: str = "") -> str:
    for key, value in el.attrib.items():
        if _local(key) in names:
            return value
    return default


def parse_metadata(xml_text: str, service: str = "") -> ServiceMetadata:
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError as exc:
        return ServiceMetadata(ok=False, service=service,
                                error=f"$metadata was not valid XML: {exc}")

    # Entity types first — entity sets reference them by name.
    types: dict[str, tuple[list[str], list[ODataProperty]]] = {}
    for et in root.iter():
        if _local(et.tag) != "EntityType":
            continue
        name = _attr(et, "Name")
        keys: list[str] = []
        for child in et:
            if _local(child.tag) == "Key":
                keys = [_attr(pr, "Name") for pr in child
                        if _local(pr.tag) == "PropertyRef"]
        props = [
            ODataProperty(
                name=_attr(p, "Name"),
                type=_attr(p, "Type"),
                nullable=_bool_attr(p, "Nullable", default=True),
                is_key=_attr(p, "Name") in keys,
            )
            for p in et if _local(p.tag) == "Property"
        ]
        types[name] = (keys, props)

    operations: list[ODataOperation] = []
    for container in root.iter():
        if _local(container.tag) != "EntityContainer":
            continue
        for child in container:
            tag = _local(child.tag)
            if tag == "EntitySet":
                type_ref = _attr(child, "EntityType")
                short = type_ref.rsplit(".", 1)[-1]
                keys, props = types.get(short, ([], []))
                operations.append(ODataOperation(
                    kind="entityset", name=_attr(child, "Name"), http_method="GET",
                    entity_type=short, keys=keys, properties=props,
                    creatable=_bool_attr(child, "creatable", default=True),
                    updatable=_bool_attr(child, "updatable", default=True),
                    deletable=_bool_attr(child, "deletable", default=True),
                    addressable=_bool_attr(child, "addressable", default=True),
                ))
            elif tag == "FunctionImport":
                params = [
                    ODataProperty(name=_attr(p, "Name"), type=_attr(p, "Type"),
                                   nullable=_bool_attr(p, "Nullable", default=True))
                    for p in child if _local(p.tag) == "Parameter"
                ]
                operations.append(ODataOperation(
                    kind="function", name=_attr(child, "Name"),
                    http_method=(_attr(child, "HttpMethod", default="GET") or "GET").upper(),
                    parameters=params,
                ))

    sets = sum(1 for o in operations if o.kind == "entityset")
    funcs = len(operations) - sets
    note = (f"{sets} entity set(s) and {funcs} function import(s) available."
            if operations else
            "The service responded but exposes no entity sets or function imports.")
    return ServiceMetadata(ok=True, service=service, operations=operations, note=note)


def fetch_metadata(client: RfcSoapClient, service: str) -> ServiceMetadata:
    """Fetch and parse $metadata for one service.

    `service` accepts what the catalogue returns — an absolute URL or a
    path — because making the caller normalise it would just invite the
    host-mismatch bug described in service_path()."""
    path = service_path(service).rstrip("/")
    if not path.endswith("$metadata"):
        path = path + "/$metadata"
    try:
        status, text = client.http_get(path, accept="application/xml")
    except RfcSoapError as exc:
        return ServiceMetadata(ok=False, service=service, error=exc.message)

    if status >= 400:
        # Always surface SAP's own message. A bare "HTTP 500" hid the actual
        # cause on the live system ("No System Alias found for Service
        # ALERTINBOX_0001 and user GIRI"), which is the entire diagnosis.
        state, explanation = classify_failure(status, text)
        md = ServiceMetadata(ok=False, service=service, error=explanation)
        md.state = state
        return md
    md = parse_metadata(text, service)
    md.state = "usable"
    return md


def probe_services(client: RfcSoapClient, services: list[dict],
                    max_workers: int = 8, timeout_s: float = 15.0) -> dict:
    """Find out which registered services are ACTUALLY callable.

    A Gateway catalogue lists what's registered, not what works. On the
    live system 124 services were registered while the first one tried
    failed with 'No System Alias found' — registered but not routable.
    Composing flows against that list would mean discovering the dead ones
    one at a time, so this classifies them up front.

    Concurrent because doing 124 sequential round trips would be
    unpleasant; httpx.Client is thread-safe and the session/cookies are
    shared deliberately."""
    from concurrent.futures import ThreadPoolExecutor

    def probe(svc: dict) -> dict:
        url = svc.get("url") or svc.get("service") or ""
        name = svc.get("service") or svc.get("id") or url
        path = service_path(url).rstrip("/") + "/$metadata"
        try:
            status, text = client.http_get(path, accept="application/xml")
        except RfcSoapError as exc:
            return {**svc, "state": "error", "detail": exc.message, "operations": 0}
        if status < 400:
            md = parse_metadata(text, name)
            return {**svc, "state": "usable", "detail": md.note,
                    "operations": len(md.operations)}
        state, explanation = classify_failure(status, text)
        return {**svc, "state": state, "detail": explanation, "operations": 0}

    original = client.timeout_s
    client._http.timeout = timeout_s
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(probe, services))
    finally:
        client._http.timeout = original

    by_state: dict[str, int] = {}
    for r in results:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    usable = [r for r in results if r["state"] == "usable"]

    if usable:
        note = (f"{len(usable)} of {len(results)} registered service(s) are actually "
                f"callable and can be used to build flows.")
    else:
        note = (f"None of the {len(results)} registered services responded successfully. "
                "If most report 'no system alias', Gateway has no backend route "
                "configured for them (/IWFND/MAINT_SERVICE → Add System Alias).")

    return {
        "total": len(results),
        "usable_count": len(usable),
        "by_state": by_state,
        "note": note,
        "services": sorted(results,
                           key=lambda r: (r["state"] != "usable", -r.get("operations", 0),
                                          str(r.get("service", "")))),
    }


def read_entity_set(client: RfcSoapClient, service: str, entity_set: str,
                     top: int = 20, filter_expr: str = "",
                     select: str = "") -> dict:
    """Execute the most common operation: read rows from an entity set.

    Deliberately read-only. Write operations (POST/PUT/DELETE) need CSRF
    token handling and — far more importantly — a considered decision about
    letting this tool CHANGE data in a live SAP system. That belongs in an
    explicit, separately-reviewed step, not smuggled in alongside a
    read-only catalogue browser."""
    path = service_path(service).rstrip("/") + "/" + entity_set.lstrip("/")
    params: dict[str, Any] = {"$format": "json", "$top": max(1, min(top, 500))}
    if filter_expr:
        params["$filter"] = filter_expr
    if select:
        params["$select"] = select
    try:
        status, text = client.http_get(path, params=params)
    except RfcSoapError as exc:
        return {"ok": False, "error": exc.message, "entity_set": entity_set}

    if status >= 400:
        state, explanation = classify_failure(status, text)
        return {"ok": False, "entity_set": entity_set, "status": status,
                "state": state, "error": explanation}

    import json as _json
    try:
        payload = _json.loads(text)
    except _json.JSONDecodeError:
        return {"ok": False, "entity_set": entity_set,
                "error": "Response was not JSON — the service may not support $format=json."}
    rows = (payload.get("d") or {}).get("results")
    if rows is None:
        d = payload.get("d")
        rows = [d] if isinstance(d, dict) and d else (payload.get("value") or [])
    clean = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict):
            clean.append({k: v for k, v in r.items()
                          if k != "__metadata" and not isinstance(v, (dict, list))})
    return {"ok": True, "entity_set": entity_set, "count": len(clean), "rows": clean}
