"""D-045: call any RFC-enabled function module over SAP's SOAP/HTTP
service (`/sap/bc/soap/rfc`) — no pyrfc, no NW RFC SDK, no binaries.

Wire format (SAP is strict about the namespace spelling):

    POST /sap/bc/soap/rfc?sap-client=100      Basic auth
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <urn:RFC_SYSTEM_INFO xmlns:urn="urn:sap-com:document:sap:rfc:functions"/>
      </soap:Body>
    </soap:Envelope>

Parameter shapes map to XML like this:
    scalar     "X"                     -> <NAME>X</NAME>
    structure  {"A": 1}                -> <NAME><A>1</A></NAME>
    table      [{"A": 1}, {"A": 2}]    -> <NAME><item><A>1</A></item>…</NAME>

Responses come back as `<FUNCTION_NAME.Response>` with the same shapes;
repeated `<item>` children are parsed back into lists.

Design rule learned the hard way on this project (D-044): NEVER fail
silently. Every failure mode here — service disabled, wrong credentials,
missing authorization, SOAP fault, ABAP exception — raises with a
specific, actionable message rather than returning empty data that looks
like "nothing found".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

RFC_NS = "urn:sap-com:document:sap:rfc:functions"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP_PATH = "/sap/bc/soap/rfc"


class RfcSoapError(RuntimeError):
    """Any failure calling an RFC FM over SOAP, with a human-actionable
    message. `kind` lets callers distinguish setup problems (which the UI
    should explain how to fix) from per-call problems."""

    def __init__(self, message: str, kind: str = "error",
                 detail: str = "", function: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind          # auth | not_enabled | fault | abap_exception | transport | parse
        self.detail = detail
        self.function = function


def derive_soap_url(webgui_url: str) -> str:
    """The SOAP RFC service lives on the SAME ICM host/port as WebGUI, so
    an already-registered system needs no new configuration — we just swap
    the path. (Deliberate: adding another URL field to the Systems form
    would be exactly the kind of extra setup step we're trying to remove.)"""
    parts = urlsplit(webgui_url)
    if not parts.scheme or not parts.netloc:
        raise RfcSoapError(
            f"Cannot derive the SOAP RFC endpoint from webgui_url={webgui_url!r} — "
            "it must be a full URL like http://host:8000/sap/bc/gui/sap/its/webgui",
            kind="transport")
    return urlunsplit((parts.scheme, parts.netloc, SOAP_PATH, "", ""))


# ── XML building ─────────────────────────────────────────────────────────

def _append_value(parent: ET.Element, name: str, value: Any) -> None:
    el = ET.SubElement(parent, name)
    if isinstance(value, dict):                       # structure
        for k, v in value.items():
            _append_value(el, k, v)
    elif isinstance(value, (list, tuple)):            # table
        for row in value:
            item = ET.SubElement(el, "item")
            if isinstance(row, dict):
                for k, v in row.items():
                    _append_value(item, k, v)
            else:
                item.text = _scalar(row)
    else:
        el.text = _scalar(value)


def _scalar(value: Any) -> str:
    if value is None:
        return ""
    if value is True:
        return "X"        # ABAP boolean convention
    if value is False:
        return " "
    return str(value)


def build_envelope(function: str, params: dict[str, Any]) -> bytes:
    env = ET.Element(f"{{{SOAP_NS}}}Envelope")
    body = ET.SubElement(env, f"{{{SOAP_NS}}}Body")
    fn = ET.SubElement(body, f"{{{RFC_NS}}}{function}")
    for name, value in (params or {}).items():
        _append_value(fn, name, value)
    ET.register_namespace("soap", SOAP_NS)
    ET.register_namespace("urn", RFC_NS)
    return b'<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(env, encoding="utf-8")


# ── XML parsing ──────────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_element(el: ET.Element) -> Any:
    children = list(el)
    if not children:
        return (el.text or "").strip()
    # a table: one or more <item> children
    if all(_localname(c.tag) == "item" for c in children):
        return [_parse_element(c) for c in children]
    out: dict[str, Any] = {}
    for c in children:
        name = _localname(c.tag)
        parsed = _parse_element(c)
        if name in out:  # repeated non-item tags -> list, don't silently drop
            if not isinstance(out[name], list):
                out[name] = [out[name]]
            out[name].append(parsed)
        else:
            out[name] = parsed
    return out


def parse_response(xml_bytes: bytes, function: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        snippet = xml_bytes[:300].decode("utf-8", "replace")
        raise RfcSoapError(
            f"{function}: response was not valid XML — the endpoint may not be the SOAP "
            f"RFC service (got: {snippet!r})", kind="parse", function=function) from exc

    body = None
    for el in root.iter():
        if _localname(el.tag) == "Body":
            body = el
            break
    if body is None:
        raise RfcSoapError(f"{function}: SOAP response had no Body element",
                            kind="parse", function=function)

    for child in body:
        name = _localname(child.tag)
        if name == "Fault":
            parsed = _parse_element(child)
            fault = parsed.get("faultstring") or parsed.get("faultcode") or str(parsed)
            raise RfcSoapError(
                f"{function}: SAP returned a SOAP fault — {fault}",
                kind="fault", detail=str(parsed), function=function)
        if name.endswith(".Response") or name == function:
            result = _parse_element(child)
            return result if isinstance(result, dict) else {"value": result}

    # Unexpected but real: some errors come back as an empty/odd body.
    raise RfcSoapError(
        f"{function}: no '{function}.Response' element in the SOAP body",
        kind="parse", detail=xml_bytes[:400].decode("utf-8", "replace"), function=function)


# ── Client ───────────────────────────────────────────────────────────────

@dataclass
class RfcTarget:
    soap_url: str
    username: str
    password: str
    client: str
    verify_tls: bool = False   # SAP dev/test ICMs commonly use self-signed certs


class RfcSoapClient:
    def __init__(self, target: RfcTarget, timeout_s: float = 30.0) -> None:
        self.target = target
        self.timeout_s = timeout_s

    @classmethod
    def from_system(cls, webgui_url: str, client: str, username: str,
                     password: str, **kw) -> "RfcSoapClient":
        return cls(RfcTarget(soap_url=derive_soap_url(webgui_url), username=username,
                              password=password, client=client), **kw)

    def call(self, function: str, **params: Any) -> dict[str, Any]:
        body = build_envelope(function, params)
        url = self.target.soap_url
        try:
            resp = httpx.post(
                url,
                content=body,
                params={"sap-client": self.target.client},
                headers={"Content-Type": 'text/xml; charset="utf-8"',
                         "SOAPAction": '""'},
                auth=(self.target.username, self.target.password),
                timeout=self.timeout_s,
                verify=self.target.verify_tls,
                follow_redirects=True,
                trust_env=False,   # never route SAP calls through an ambient proxy
            )
        except httpx.RequestError as exc:
            raise RfcSoapError(
                f"{function}: could not reach the SAP SOAP RFC endpoint at {url} ({exc}). "
                "Check the host/port is reachable from the container.",
                kind="transport", function=function) from exc

        if resp.status_code in (401, 403):
            raise RfcSoapError(
                f"{function}: SAP rejected the credentials or the user lacks authorization "
                f"(HTTP {resp.status_code}). The user needs S_RFC authorization for this "
                "function group, and access to the /sap/bc/soap/rfc service.",
                kind="auth", detail=resp.text[:400], function=function)
        if resp.status_code == 404:
            raise RfcSoapError(
                f"{function}: the SOAP RFC service is not active on this system "
                f"(HTTP 404 at {url}). A BASIS admin can activate it in transaction SICF "
                "under /sap/bc/soap/rfc.",
                kind="not_enabled", detail=resp.text[:400], function=function)
        if resp.status_code >= 400 and resp.status_code != 500:
            # 500 can legitimately carry a SOAP Fault body, so let it through to the parser.
            raise RfcSoapError(
                f"{function}: HTTP {resp.status_code} from {url}",
                kind="transport", detail=resp.text[:400], function=function)

        return parse_response(resp.content, function)

    def ping(self) -> dict[str, Any]:
        """Connection test. RFC_SYSTEM_INFO is present on every ABAP system
        and needs no parameters, which makes it the cleanest probe of
        'can we reach it, authenticate, and call an FM at all'."""
        return self.call("RFC_SYSTEM_INFO")
