"""sapcontrol through the ABAP system, over SOAP RFC only (D-051).

SMMS's Services view (message server ports) has no remote-enabled ABAP
interface, and its report RSMONMS_ALV prints only the instance list in batch.
sapstartsrv knows the same facts, so an SM69 command wraps `sapcontrol`,
executed with SXPG_COMMAND_EXECUTE:

  SM69: ZAHC_SAPCONTROL, OS Linux, command `sapcontrol`,
        parameters `-nr <nn> -function GetAccessPointList`,
        "additional parameters allowed" = X   (the -nr/-function we append win)

Only READ functions are ever sent (ALLOWED_FUNCTIONS) and every argument is
validated. NOTE for BASIS: with additional parameters allowed, anyone holding
S_LOG_COM for this command could append e.g. `-function Stop`; restrict
S_LOG_COM to the monitoring user, or point SM69 at a wrapper script that only
passes these three functions (see SKILL.md D-051).

Verified on F4H (2026-10-01): GetSystemInstanceList finds the MESSAGESERVER
instance (01); ParameterValue ms/server_port_0 there = "PROT=HTTP,PORT=8101"
(ms/server_port_1 -> "FAIL: Invalid parameter", exit 1); GetAccessPointList
-nr 01 lists 8101 HTTP msg_server Yes.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from engine.rfc.soap_client import RfcSoapError

from .sap import Sap, as_rows

DEFAULT_COMMAND = "ZAHC_SAPCONTROL"
ALLOWED_FUNCTIONS = {"GetSystemInstanceList", "GetAccessPointList", "ParameterValue"}
_ARG = re.compile(r"[A-Za-z0-9_/.\-]{1,80}")


class SapControlViaSm69:
    def __init__(self, sap: Sap, command: Optional[str] = None):
        self.sap = sap
        self.command = (command or os.environ.get("AHC_SAPCONTROL_COMMAND") or DEFAULT_COMMAND).strip()
        self._why = ""

    def run(self, nr: Optional[str], function: str, *args: str) -> tuple[bool, list[str]]:
        """(ok, output lines after the "OK"/"FAIL" status line)."""
        if function not in ALLOWED_FUNCTIONS:
            raise ValueError(f"sapcontrol function {function} is not allowed")
        if nr is not None and not re.fullmatch(r"\d\d", nr):
            raise ValueError(f"bad instance number {nr!r}")
        if not all(_ARG.fullmatch(a) for a in args):
            raise ValueError(f"bad sapcontrol argument in {args!r}")
        add = " ".join(([f"-nr {nr}"] if nr is not None else []) + [f"-function {function}", *args])
        try:
            r = self.sap.call("SXPG_COMMAND_EXECUTE", COMMANDNAME=self.command,
                              ADDITIONAL_PARAMETERS=add, EXEC_PROTOCOL=[])
        except RfcSoapError as exc:
            if "COMMAND_NOT_FOUND" in exc.message:
                self._why = (f"SM69 command {self.command} does not exist — a BASIS admin must create it "
                             "(sapcontrol wrapper; see SKILL.md D-051)")
            raise
        lines = [(m.get("MESSAGE") or "").rstrip() for m in as_rows(r.get("EXEC_PROTOCOL"))]
        try:
            i = next(k for k, line in enumerate(lines) if line.strip() == function)
        except StopIteration:
            raise RfcSoapError(f"{self.command}: unexpected sapcontrol output: " + " ".join(lines)[:200],
                               kind="fault", function="SXPG_COMMAND_EXECUTE")
        status = lines[i + 1].strip() if i + 1 < len(lines) else ""
        body = [line for line in lines[i + 2:] if line.strip()
                and not line.startswith("External program terminated")]
        return status == "OK", body

    def table(self, nr: Optional[str], function: str) -> list[dict]:
        ok, body = self.run(nr, function)
        if not ok or not body:
            return []
        head = [h.strip() for h in body[0].split(",")]
        return [dict(zip(head, [v.strip() for v in line.split(",")])) for line in body[1:]]

    def parameter(self, nr: Optional[str], name: str) -> Optional[str]:
        ok, body = self.run(nr, "ParameterValue", name)
        return body[-1].strip() if ok and body else None

    def unavailable_reason(self) -> str:
        return self._why


def parse_server_port(value: str) -> dict:
    """"PROT=HTTP,PORT=8101,TIMEOUT=20" -> {"PROT": "HTTP", "PORT": "8101", ...}"""
    out = {}
    for part in value.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out
