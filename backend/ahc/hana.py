"""HANA SQL through the ABAP system, over SOAP RFC only (D-051).

SAP offers no supported "run this SQL" RFC (the probe found SHDB_ASYNC_SQL,
but its input format is undocumented). The supported route is an SM69
external command wrapping `hdbsql`, executed with SXPG_COMMAND_EXECUTE
(remote-enabled on F4H; v1 already uses it for `df -h`).

One-time BASIS setup (see docs in SKILL.md D-051):
  HANA:  CREATE USER AHCMON PASSWORD <pw> NO FORCE_FIRST_PASSWORD_CHANGE;
         GRANT MONITORING TO AHCMON;            -- read-only monitoring views
  OS (as <sid>adm on the app server):
         hdbuserstore SET AHCMON <dbhost>:3<nn>13@<SID> AHCMON <pw>
  SM69:  command ZAHC_HDBSQL, OS Linux,
         OS command  /usr/sap/<SID>/hdbclient/hdbsql
         parameters  -U AHCMON -x -j -F |
         "Additional parameters allowed" = X
  The monitoring user then needs S_LOG_COM for ZAHC_HDBSQL and S_RFC for
  SXPG. The HANA user has MONITORING only, so the command cannot change data.

Behaviour verified on F4H (2026-10-01):
  - SXPG passes ADDITIONAL_PARAMETERS to hdbsql verbatim — quotes are NOT
    stripped, so the SQL goes unquoted. The SM69 parameters and the SQL
    share 255 characters (230 chars of SQL worked, 235 was cut, longer
    gives TOO_MANY_PARAMETERS), so SQL is capped at MAX_SQL.
  - Output lines are "|COL|COL|" with strings in double quotes, and each
    protocol line is CUT at 128 characters (not wrapped). table() therefore
    reads wide results in column groups that fit, and splits a single long
    text column into SUBSTR chunks, then stitches the rows back together.
    Every multi-query read needs an ORDER BY that is stable across queries.
"""
from __future__ import annotations

import os
import re
from typing import Optional, Sequence

from engine.rfc.soap_client import RfcSoapError

from .sap import Sap, as_rows

DEFAULT_COMMAND = "ZAHC_HDBSQL"
MAX_SQL = 225          # SM69 parameters + SQL share SXPG's 255 chars (F4H: 230 ok, 235 cut)
LINE = 128             # SXPG protocol line width; longer output is cut
CHUNK = 100            # SUBSTR chunk for long text columns


class Truncated(Exception):
    """An hdbsql output line hit the protocol width, so it was cut."""


def _split(line: str) -> list[str]:
    """Parse one hdbsql -x -j -F '|' line: |"text"|123|?| (? = NULL; quotes inside
    text come as \\" — F4H — or "")."""
    line = line.strip()
    out, i, n = [], 0, len(line)
    if line.startswith("|"):
        i = 1
    while i < n:
        if line[i] == '"':
            j, buf = i + 1, []
            while j < n:
                if line[j] == "\\" and j + 1 < n:          # hdbsql escapes \" and \\ (seen on F4H)
                    buf.append(line[j + 1])
                    j += 2
                elif line[j] == '"' and j + 1 < n and line[j + 1] == '"':
                    buf.append('"')
                    j += 2
                elif line[j] == '"':
                    break
                else:
                    buf.append(line[j])
                    j += 1
            out.append("".join(buf))
            i = j + 2                      # skip closing quote and the separator
        else:
            j = line.find("|", i)
            j = n if j < 0 else j
            v = line[i:j]
            out.append("" if v == "?" else v)
            i = j + 1
    return out


class HanaViaSm69:
    def __init__(self, sap: Sap, command: Optional[str] = None):
        self.sap = sap
        self.command = (command or os.environ.get("AHC_HDBSQL_COMMAND") or DEFAULT_COMMAND).strip()
        self._ok: Optional[bool] = None
        self._why = ""

    def _run(self, sql: str) -> list[str]:
        if len(sql) > MAX_SQL:
            raise ValueError(f"SQL too long for SXPG ({len(sql)} > {MAX_SQL} chars)")
        r = self.sap.call("SXPG_COMMAND_EXECUTE", COMMANDNAME=self.command,
                          ADDITIONAL_PARAMETERS=sql, EXEC_PROTOCOL=[])
        status, code = (r.get("STATUS") or "").strip(), (r.get("EXITCODE") or "0").strip()
        lines = [(p.get("MESSAGE") or "").rstrip() for p in as_rows(r.get("EXEC_PROTOCOL"))]
        if status not in ("", "O") or code not in ("", "0"):
            raise RfcSoapError(f"{self.command} failed (status {status}, exit {code}): "
                               + " ".join(lines)[:200], kind="fault", function="SXPG_COMMAND_EXECUTE")
        return lines

    def available(self) -> bool:
        if self._ok is None:
            try:
                out = self._run("SELECT 1 AS PING FROM DUMMY")
                self._ok = any("PING" in line.upper() for line in out)
                if not self._ok:
                    self._why = f"SM69 command {self.command} ran but returned no hdbsql output"
            except RfcSoapError as exc:
                self._ok = False
                m = exc.message
                self._why = (f"SM69 command {self.command} does not exist — a BASIS admin must create it "
                             "(hdbsql wrapper; see SKILL.md D-051)" if "COMMAND_NOT_FOUND" in m
                             else f"SM69 command {self.command}: {m[:160]}")
        return bool(self._ok)

    def unavailable_reason(self) -> str:
        self.available()
        return self._why

    def query(self, sql: str) -> list[dict]:
        """One statement whose rows fit the protocol line; raises Truncated otherwise."""
        lines = [line for line in self._run(sql) if line.strip()]
        lines = [line for line in lines if not re.match(r"^\d+ rows? selected", line.strip())]
        if not lines:
            return []
        head = _split(lines[0])
        rows = [_split(line) for line in lines[1:]]
        # A cut line is not always 128 long: SXPG also drops trailing blanks.
        # A complete hdbsql row ends with "|" and has the header's field count.
        if any(len(line) >= LINE or not line.rstrip().endswith("|") for line in lines) \
                or any(len(r) != len(head) for r in rows):
            raise Truncated(sql)
        return [dict(zip(head, r)) for r in rows]

    def table(self, columns: Sequence[str], source: str, top: Optional[int] = None,
              lobs: Sequence[str] = ()) -> list[dict]:
        """SELECT [TOP n] <columns> <source>, read so no row is ever cut.

        `source` is everything after the column list ("FROM ... WHERE ...
        ORDER BY ..."); its ORDER BY must give the same row order on every
        call, because wide results are read in several statements. Name LOB
        columns (CLOB/NCLOB) in `lobs`."""
        head = f"SELECT TOP {top} " if top else "SELECT "
        cols = list(columns)
        # hdbsql prints LOB columns cut to 32 characters with a normal closing
        # "|", so a cut cannot be detected — LOB columns are always chunked.
        plain = [c for c in cols if c not in lobs]
        parts = self._groups(plain, head, source) if plain else []
        parts += [self._chunked(c, head, source) for c in cols if c in lobs]
        rows: list[dict] = []
        for part in parts:
            for i, r in enumerate(part):
                if i == len(rows):
                    rows.append({})
                rows[i].update(r)
        return [{c.split(".")[-1]: r.get(c.split(".")[-1], "") for c in cols} for r in rows]

    def _groups(self, cols: list[str], head: str, source: str) -> list[list[dict]]:
        sql = f"{head}{', '.join(cols)} {source}"
        if len(sql) <= MAX_SQL:
            try:
                return [self.query(sql)]
            except Truncated:
                pass
        if len(cols) > 1:
            mid = len(cols) // 2
            return self._groups(cols[:mid], head, source) + self._groups(cols[mid:], head, source)
        return [self._chunked(cols[0], head, source)]

    def _chunked(self, col: str, head: str, source: str) -> list[dict]:
        """One long text column, read CHUNK characters at a time."""
        name = col.split(".")[-1]
        rows: list[str] = []
        for k in range(1, 20 * CHUNK, CHUNK):
            part = self.query(f"{head}SUBSTR(TO_NVARCHAR({col}), {k}, {CHUNK}) AS P {source}")
            vals = [r.get("P", "") for r in part]
            rows = rows or [""] * len(vals)
            for i, v in enumerate(vals[: len(rows)]):
                rows[i] += v
            if all(len(v) < CHUNK for v in vals):
                break
        return [{name: v} for v in rows]
