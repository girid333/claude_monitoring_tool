---
name: sap-webgui-replay-agent
description: Project memory and working agreements for Giri's SAP WebGUI monitoring agent — a record-and-replay engine (CBTA/Tricentis-style, but via SAP WebGUI) extending an existing SAP BASIS monitoring tool. ALWAYS load this skill when the conversation involves SAP monitoring, SAP BASIS, WebGUI automation, SAP GUI scripting, T-code record/replay, the sap_monitoring project, or continuing/resuming work on this agent. Treat the Decision Log in this file as settled ground truth; do not re-litigate closed decisions, and append new dated entries whenever a decision is made or a milestone completes.
---

# SAP WebGUI Record-and-Replay Monitoring Agent — Project Memory

This file is the single source of truth for this project. It captures what was
decided, why, what exists, and what comes next — so any future session (Claude.ai
or Claude Code) can resume without losing context.

**Maintenance rule (for Claude):** When working on this project, (1) read this
file fully before proposing anything, (2) after any new decision, milestone, or
scope change, append a dated entry to the Decision Log and update Current Status,
(3) never silently contradict a logged decision — if a decision needs revisiting,
say so explicitly and log the supersession.

---

## 1. Project Overview

**Owner:** Giri
**Goal:** Evolve an existing SAP BASIS monitoring tool into an agent with a
generic **record-and-replay engine over SAP WebGUI**, so that functional
monitoring / sanity checks for arbitrary T-codes can be recorded once and
replayed on demand with the same or different input values — conceptually like
CBTA or Tricentis Tosca, but operating through WebGUI (SAP GUI for HTML).

**Existing asset (v1, working):**
- Location: `/Users/giri/Antigravity/sap_monitoring` (built with Antigravity)
- Stack: Python/FastAPI backend + Playwright (headless Chromium) driving
  WebGUI, `pyrfc`/`nwrfcsdk` for a parallel RFC path, SQLite for config,
  Next.js/React frontend. Full inventory in §6.
- Behavior: connects to SAP via WebGUI, navigates hand-built flows for BASIS
  monitoring T-codes, takes screenshots, detects anomalies, and produces a
  combined Word + PDF report. Works reliably.
- Important property: it does **not** require `sapgui/user_scripting` — this
  is confirmed (zero `findById`/`GuiSession` usage in the code). However, it
  does **not** key off raw positional HTML IDs (`M0:46:::2:34`-style) either
  — that was an unverified assumption corrected in §6. It actually favors
  semantic attribute/text/role selectors, falling back to *computed* pixel-
  coordinate ALV parsing only for two screens. See §6 for the full,
  code-verified breakdown (D-012 refines D-002 accordingly).

**Target (v2):** The existing tool becomes the "core BASIS" section of a larger
agent. New functional sanity-check sections are recorded flows, pluggable into
the same run + report pipeline.

---

## 2. Decision Log (settled — do not re-litigate)

**D-001 (2026-07-31) — Element identification strategy: SAP GUI Scripting API is primary.**
WebGUI supports the SAP GUI Scripting API when enabled. It provides semantic,
stable IDs derived from ABAP field names (e.g.
`wnd[0]/usr/ctxtBTCH2170-JOBNAME`) that survive sessions, users, and most
releases. Raw WebGUI DOM IDs are positional and layout-dependent. Recording is
built on the scripting API's event stream.

**D-002 (2026-07-31) — Hybrid dual-resolver architecture.**
Flow steps store an *abstract target descriptor* carrying multiple
identification candidates captured at record time: scripting ID (primary) +
DOM metadata (label text, `lsdata` attributes, tooltip, positional ID as last
resort). Replay engine has two backends:
- **Scripting resolver** — exact, used where the API is available.
- **DOM resolver** — ranks candidate attributes to locate elements where
  scripting is disabled.
Record once → replay anywhere.

**D-003 (2026-07-31) — DOM resolver is a CORE deliverable, not a later phase.**
Customers have strict security policies; many target systems will keep
`sapgui/user_scripting = FALSE`. Replay must be first-class on such systems.
The DOM resolver gets real investment: locator ranking, self-healing,
confidence scoring, and mismatch flagging in reports. The tool's value
proposition explicitly includes easing monitoring on locked-down systems.

**D-004 (2026-07-31) — Recording requires a scripting-enabled system; replay does not.**
A DOM-side recorder (reverse-engineering intent from positional IDs) is
deliberately NOT built. Flows are recorded on Giri's scripting-enabled
reference systems; the recorder snapshots DOM metadata alongside scripting IDs
at record time, making every flow automatically DOM-replayable.

**D-005 (2026-07-31) — Capability probe per system.**
First action against any system: probe login, scripting availability
(`session.findById` resolution, event emission), and `session.info` (SID,
client, release, response time). The probe decides *which resolver mode* is
used and with what confidence — not whether the system can be monitored.
Relevant parameters when scripting IS expected:
`sapgui/user_scripting = TRUE`,
`sapgui/user_scripting_disable_recording = FALSE` (needed for recording),
`sapgui/user_scripting_per_user` (if TRUE, user needs auth object S_SCR act 16),
`sapgui/user_scripting_force_notification = FALSE` (avoid popups on replay).

**D-006 (2026-07-31) — System registry + flow compatibility.**
Landscape is heterogeneous (ECC 6.0 through current S/4). Registry entries:
SID, WebGUI URL, client, release tag (from probe), auth strategy, tags.
Flows declare `applies_to` (e.g. `[ECC6_*, S4_*]`) and may have per-release
variants; a resolver picks the right variant per system at run time.

**D-007 (2026-07-31) — Pluggable login strategies.**
WebGUI auth varies (basic, SAML/IdP redirect, X.509, SSO cookie). Login is a
pluggable pre-flow step in plain Playwright territory (scripting API only
exists after session start). Central re-auth handling: detect login screen /
ICM error mid-flow → re-login → resume or restart. Dedicated monitoring user
per system with tightly scoped S_TCODE authorizations.

**D-008 (2026-07-31) — Flow schema principles.**
JSON/YAML flows; steps = ordered list of {act, target, value}. Step kinds:
`start_tcode`, `set`, `press`, `select`, `vkey`, `wait_ready`, `capture`,
`check`. IDs stored window-relative (strip `/app/con[0]/ses[0]`, re-anchor at
replay). Synchronization is explicit and busy-state based — never sleeps.
Parameters use `{{name}}` placeholders; datasets (JSON/Excel) are separate
from flows (CBTA-style); runtime expressions supported (`{{today}}`,
`{{today-1}}`, `{{system_id}}`).

**D-009 (2026-07-31) — Structured extraction first, screenshots as evidence.**
`capture` and `check` are first-class step types. Anomaly detection runs
primarily on structured reads (table rows/cells, status bar, row counts) with
severities; screenshots remain in the report as evidence, and existing
vision/heuristic analysis is a second layer for genuinely visual checks.
Checks carry `severity` and `message` that map into the Word/PDF report.

**D-010 (2026-07-31) — Stateless engine for future parallelism.**
Engine is single-system initially but stateless: all state keyed by
`run_id + sid` (sessions, screenshots, logs isolated). Parallel execution
across the landscape later becomes an orchestration-loop change, not a rewrite.

**D-011 (2026-07-31) — v1 tool migration is opportunistic, not big-bang.**
Existing hand-built BASIS navigations are effectively DOM-mode flows already.
They keep running as-is and get re-recorded through the new engine over time.

**D-012 (2026-07-31) — DOM candidate model refined from v1 code evidence (does not contradict D-002/D-003).**
Full read of v1's `sap_automator.py` (see §6) shows it deliberately avoids
raw auto-generated positional ids (`M0:46:::2:34`-style) in favor of, in
priority order: (1) semantic attributes (`name`, `title`, `id` substring —
e.g. `input[name="sap-user"]`, `input[title="Command Field"]`), (2) visible
text / role (Playwright `get_by_text`, `get_by_role("checkbox")`), (3) CSS
class fragments tied to SAP's UI library (`.lsBlockLayer`, `.urST5BaseBorder`),
(4) XPath-by-text with parent-walk for tree widgets (ST03N, SM12 tree nodes).
Raw pixel-coordinate parsing (compute column x-offset from header text, match
data cells within tolerance) is used only as a last resort for two ALV-heavy
screens (SMICM, STRUST) — and even then it's computed per page load, not a
fixed id string. **Action:** the DOM resolver's candidate ranking (D-002)
should mirror this order — attribute > text/role > class fragment > computed
coordinate — rather than treating "positional ID" as a single generic
fallback tier. No decision reversed; this sharpens the implementation target.

**D-013 (2026-07-31) — RFC demoted from run gate to optional enrichment (resolves OQ-6).**
v1 aborts an entire run if RFC login fails, though RFC supplies real data for
only 4 of 23 T-codes. This contradicts the locked-down-customer value
proposition: HTTPS WebGUI access is far easier to obtain than RFC ports +
NetWeaver SDK deployment. In v2 the capability probe (D-005) owns
connectivity/login verification against WebGUI directly. RFC becomes an
*optional enrichment plugin* — where available, structured reads
(`TH_WPINFO`, `ENQUEUE_READ`, filesystem stats) enrich results; where not,
the system still gets a full monitoring run. Existing `sap_rfc_client.py`
code is retained for the plugin.

**D-014 (2026-07-31) — Report composer: inherit and refactor, not rebuild (resolves OQ-7).**
`report_generator.py` (805 lines, Word+PDF, customer-accepted output) is kept
as the v2 report composer base. Required refactors: (1) remove the hardcoded
machine-specific cover-image path, (2) introduce the severity model — v1
anomalies are a flat string list, so severity must be added to both the check
schema and the report template, designed together in Phase 4/5, (3) make
sections pluggable so recorded flows and the v1 BASIS section render through
the same pipeline.

**D-015 (2026-07-31) — Phase 1 reference systems must span basic-auth AND SSO/SAML (closes OQ-2's auth dimension).**
Login plugins built in Phase 1: `basic` (port of v1's JS-injected fill path)
and `saml` (Playwright follows IdP redirect; persistent browser profile for
SSO cookies; expiry-triggered re-auth). Governance prerequisite to raise
EARLY with each customer's IdP team: unattended headless runs cannot answer
interactive MFA. Accepted mitigations, in preference order: conditional-
access/MFA exemption for the dedicated monitoring user (IP-restricted to the
monitoring host), X.509 client-certificate login instead of SAML, or
long-lived SSO cookies with automated re-auth on expiry. Remaining open in
OQ-2: which concrete SIDs serve as the two reference systems.

**D-016 (2026-07-31) — Frontend design system: Apple HIG-inspired, approved via mockup.**
Reference artifact: `sap-agent-ui-mockup.html` (keep in repo, e.g.
`design/mockup.html`). Tokens: bg `#0A0A0C`; translucent panels
`rgba(28,28,32,.72)` with `backdrop-filter: blur + saturate`; hairlines
`rgba(255,255,255,.08)`; text `#F5F5F7` / 60% / 35% tiers; semantic colors
blue `#0A84FF`, green `#30D158`, orange `#FF9F0A`, red `#FF453A`; radii
22/14/9px. Type: system font stack (`-apple-system, BlinkMacSystemFont, …`),
large-title 32/700 tracking -0.022em. Patterns: macOS-Settings sidebar,
tvOS focus cards (scale 1.028 + lift on hover) for the system grid,
traffic-light health dots, iOS inset-grouped lists for runs.
**Signature element: a Dynamic Island run-status capsule** — idle-quiet,
expands during bulk runs with live per-T-code chips (pending/running/done/
warned). LICENSING: SF Pro fonts and SF Symbols must never be shipped
(Apple-platform-only license) — system stack + Lucide icons only.
**Derived API requirements** (design as forcing function): live per-T-code
run events via SSE/WebSocket (replaces v1's 2s global poll), per-system
status endpoint (probe result, last-run summary, top anomaly), severity
model (D-014) surfaced as chips. Stack stays Next.js + React + Tailwind.

**D-017 (2026-07-31) — v1 integrated into the new UI as the "Core BASIS" section.**
v1's `orchestrator.py` run loop is wrapped as a *section provider*: called by
the v2 orchestrator like any other section, returning its existing results
dict unchanged. `sap_automator.py` (2,385 lines) is NOT modified for this;
FastAPI backend is retained and extended (SSE events, per-system status);
only the v1 Next.js single page (~420 lines) is replaced by the new design.
UI renders two section types side by side: "Core BASIS" (v1 engine, 23
T-codes, bespoke-vs-generic handler distinction made visible) and
"Functional sanity" (v2 recorded flows with dataset metadata). Both feed the
same report composer (D-014). Consistent with D-011 (opportunistic, not
big-bang migration).

**D-019 (2026-08-01) — CoreBasisSection wraps v1 via a monkeypatched RFC
proxy, not a duplicated loop; v1_path default was wrong and is fixed.**
Implementing D-017's contract surfaced two problems no prior session had hit
because nothing had actually tried to import v1 from v2 yet:
(a) `orchestrator.py` and `sap_rfc_client.py` both import `pyrfc` at module
scope, so `import orchestrator` raises ModuleNotFoundError on any host
without the NetWeaver RFC SDK — before a single line of v1 runs. Fixed with
an import-time shim (`engine/core_basis_wrap.py`) that injects a throwaway
`sap_rfc_client` stub into `sys.modules` ONLY when the real `pyrfc` can't be
imported, purely so v1's own top-level import succeeds; it is never used to
serve real calls.
(b) v1's `run_sap_checks()` still raises and aborts on RFC login failure
(the behavior D-013 demoted). Rather than duplicating v1's ~175-line
`run_sap_checks()` to remove the `raise`, `CoreBasisSection` monkeypatches
the module attribute `orchestrator.SAPRfcClient` (resolved dynamically at
call time, so this is a wrap of a name binding, not an edit of v1 source) to
`ResilientRfcClient`: a proxy whose `connect()` always returns `True` to
v1's abort check while tracking real availability underneath, and whose
`execute_tcode_check()`/`get_filesystem_data()` degrade to the same
`{"status": "error", ...}` shape v1's own `extract_anomalies()` already
renders as an "RFC Error" anomaly when unavailable. Net effect: RFC failure
never blocks the run (D-013 honored), the 19 non-RFC T-codes get full
WebGUI-only results, and the 4 RFC-dependent ones honestly report
unavailability instead of silently faking data. `sap_automator.py` and
`orchestrator.py` are untouched on disk — confirmed via mocked dry run (see
`backend/_dryrun_core_basis.py`, kept as a permanent manual smoke test since
no reference SID exists yet to test against a live system).
Also fixed: `CoreBasisSection`'s default `v1_path` was `"../sap_monitoring"`,
which resolves to a nonexistent `claude_monitoring_tool/sap_monitoring` when
`uvicorn` runs from `backend/` (README's own documented run command).
`sap_monitoring` is a sibling of `claude_monitoring_tool` under
`/Users/giri/Antigravity`, i.e. two levels up from `backend/`. Corrected to
`"../../sap_monitoring"`.

**D-020 (2026-08-01) — Report composer inherits v1's rendering verbatim,
adds a fifth section; recorder sessions are in-process/headed, not
run_id+sid-keyed; a real sqlite3 thread-safety bug was found and fixed.**
(a) `report_composer.py` (new, `backend/`) is v1's `report_generator.py`
copied and refactored per D-014's three required changes — cover path now a
constructor arg (default None), a `classify_v1_anomaly_severity()` heuristic
grades v1's flat anomaly strings (RFC-unavailable notes graded INFO, not
HIGH — D-013 already decided that's expected degradation, not a real
problem), and a new "4. Functional Sanity" section renders `FlowSection`
results with REAL severity from `CheckResult`, including a distinct warning
when `low_confidence_steps` is non-empty (D-003's "flag mismatches, never
guess"). All per-T-code screenshot special-casing (SM21/ST22/DB02/ST03N/
SCC4/SMLG/SMGW/SMICM/STRUST) is copied unchanged — verified by generating a
real docx+pdf from mock data and reading the text back out (both formats
render correctly; kept as `_dryrun_report.py`, a standing smoke test next to
`_dryrun_core_basis.py`). `sections.build_report()` is the glue: takes
whatever `SectionResult`s a run produced and calls the composer. Wired into
`main.py` — every `/api/run` that includes flows and/or Core BASIS now
auto-generates a report at the end, tracked in a new `report_runs` table,
downloadable via `/api/reports/{run_id}/{sid}/download/{docx|pdf}`.
(b) Recorder sessions (Phase 2 UI) are fundamentally different from bulk
runs: a human drives a HEADED browser window interactively (D-004), so
`engine/recorder_service.py` keeps sessions in an in-process dict keyed by a
random `recording_id`, NOT persisted to `RunStore`/SQLite and NOT
`run_id+sid`-keyed like D-010's stateless engine — a half-finished
interactive recording isn't a result worth persisting across a backend
restart, the human just starts over. `/api/recorder/start` launches
`headless=False` Chromium on whatever machine runs `uvicorn`; this only
works when that machine has a display (README's "run locally" model), not a
headless deployment. Recorder page (D-016 visual language) polls
`/api/recorder/status/{id}` every 1.5s for a live event count + tail.
(c) `/api/reports` crashed with `sqlite3.ProgrammingError: SQLite objects
created in a thread can only be used in that same thread` the first time it
was actually exercised (via FastAPI's `TestClient`) — a real, previously
undiscovered bug in `RunStore` (session 5 scaffold): FastAPI's sync `def`
endpoints run in a threadpool, but the `sqlite3.Connection` was created once
at module load in the main thread. Same latent bug existed in the original
`/api/runs`, just never triggered because nothing had called it yet. Fixed
with `check_same_thread=False` on the connection (documented as a stopgap —
a connection-per-request pool is the correct fix if this ever needs real
concurrent access).
(d) Frontend had no sidebar/navigation at all — `page.tsx` was a single
unstyled Landscape view with no way to reach any other route even after
building one. Added `app/components/Shell.tsx` (ports mockup.html's sidebar
verbatim: traffic lights, app icon, nav, probe chip) wired into
`app/layout.tsx`, plus `app/runs`, `app/recorder`, `app/reports` pages using
shared list/row/severity CSS classes ported into `globals.css`. Also
missing: `tsconfig.json` and the `typescript`/`@types/*` devDependencies —
the frontend had literally never been built once; `next build` auto-
generates these on first run, so they're now committed rather than
regenerated silently. Verified: `next build` type-checks clean, all four
routes (`/`, `/runs`, `/recorder`, `/reports`) serve HTTP 200 under
`next start`.
(e) OQ-2 progress: Giri provided a candidate reference system (`TEST` /
`nwrhel9.techlabhome.in`, client 100) and credentials in chat. Cowork's
sandbox network is allowlisted and cannot reach that host at all (proxy
returns `blocked-by-allowlist`, DNS unreachable) — confirmed, not assumed.
Credentials were not persisted anywhere. Wrote `backend/live_probe_check.py`
— a standalone script (not part of the FastAPI app) Giri runs himself on a
machine that can reach the host: logs in, runs the capability probe (D-005),
screenshots the result, reports whether scripting is available. This is the
actual mechanism to resolve OQ-2, still pending Giri running it.

### D-050 (2026-09-30) — Landing page, three sections, Health Check Dashboard.
Giri's direction: production readiness. Record/replay is unreliable beyond SM37,
so it moves to its own "Lab" section (/replay); Core BASIS monitoring (/basis)
must work flawlessly; new landing (/) and a CAS-style dashboard (/health).

**Health Check Dashboard = triage, not a re-skin.** Imports SAP's Automated
Health Check workbook (SpreadsheetML ".XLS" or .xlsx), re-reads each evidence
tab and classifies every check: act / review / expected / ok / no_data.
Module `backend/health/` (isolated; one include_router line; own store dir).
Built in Claude.ai against Giri's real R5P export (kept out of the repo): SAP's
8 flags -> 4 act, 1 review, 3 expected. Rings on landing + dashboard: outer =
passed SAP's check, inner = no action after triage; the gap is removed noise.

**Hard-won facts (real exports):**
- SAP emits `&#0;` char refs (DB02 "NONE&#0;&#0;…") — illegal XML; stripped
  before parsing. DOCTYPE refused (entity-expansion safety).
- Table signature: header row, then a blank row that KEEPS the header's cell
  count; data rows may be separated by zero-cell `<Row/>` (ST06) — so blank rows
  do not end a table. xlsx loses that distinction: headers additionally must
  follow a blank/title row and contain no numeric/date cells.
- ST06 numbers are European ("0,16", "1.366" = 1366); ST02 are not.
- Monitors→tab mapping is not 1:1: "Certificate Info"→Cert_Info,
  RSRFCCHK→RFC_Con, DB02 appears twice (Alerts / Availability Log), and
  "Memory Usage" is labelled DB12 but its tab is DB02_Memory.
- SAP false alarms the triage explains: SM12 long locks held by bgRFC server
  registration / FDC timer daemon / SMON; SLICENSE "Valid (inactive Hardware
  Key)" (HA partner); DB02 availability log "no event >24h" = stable;
  SM04 high priority for bgRFC schedulers.
- Real findings SAP only hinted at: foreign-SID instances in SMLG groups (a
  group with no local instance can't balance logons); the exact expiring
  certificate and PSE; SM21 EBI with continuation lines naming the job and
  missing batch user.
- Analysers never fail an import: a crash falls back to SAP's verdict with a
  visible note. `/api/health/reports/latest` returns null (not 404) when empty.
- UI regression worth keeping: grid tracks need minmax(0,1fr) or a wide
  evidence table pushes the drawer off-screen; the test was proven to fail
  on the original bug.

**D-050 integration (2026-09-30, Cowork) — how it landed in the repo.**
- Routes: `/` = Sentinel landing; `/basis` = Landscape (Core BASIS only —
  flow panel, step viewer and data-run panel removed, link to /replay added;
  the D-036 "Core BASIS off by default" checkbox kept as-is); `/health` =
  dashboard; `/replay` = the old Landscape's flow library / step viewer /
  replay / data runs, moved unmodified minus the Core BASIS checkbox;
  `/replay/recorder`, `/replay/apiflow`, `/replay/sapgui` (git mv).
  `/systems`, `/runs`, `/reports`, `/rfc` unchanged. Old `/recorder`,
  `/apiflow`, `/sapgui` redirect (next.config.ts). No API route changed;
  nothing under `engine/` changed.
- Chrome: `Shell` steps aside on `/` and `/health` (they render their own
  `.sx` TopBar); every other page gets the same TopBar (sticky `.sx`
  wrapper with `minHeight:0` — `.sx` is written for full pages) plus a
  section-scoped sidebar: Monitoring (Landscape/Systems/Runs/Reports/RFC)
  or Lab (Recordings/Recorder/API Flows/SAP GUI + Systems/Reports).
  Replay entry points are no longer in Monitoring's navigation.
- `lib/sentinelApi.apiBase()` delegates to `app/lib/api.ts` (D-024);
  tsconfig gained the `@/*` alias. TopBar's `aria-current` needed
  `"page" as const` (non-strict tsconfig widens it to string) and now also
  highlights Monitoring on /systems, /runs, /reports, /rfc.
- Backend: `backend/health/` copied as-is, one `include_router` line in
  main.py; compose sets `HEALTH_STORAGE_DIR=/data/health` (backend_data
  volume). python-multipart was already in requirements (D-049).
- Tests: health suites added (22 parser + 8 API + 7 UI, 1 opt-in skip).
  Pre-existing stale UI suites repaired against the D-031/D-036 UI and the
  new routes: `test_ui_landscape` (Run sweep/Bulk run → Execute + progress
  panel, plus D-050 assertions: no replay entry points, link to /replay),
  `test_ui_systems_runs` (launcher is /basis, not Runs),
  `test_ui_detail_and_delete` / `test_ui_progress_and_nav` (tick Core
  BASIS before Execute). `test_ui_datarun`, `test_ui_flow_mgmt_reports`,
  `test_ui_apiflow` repointed to /replay paths.
- **UI-suite fixtures (were undocumented, cost time):** datarun UI needs
  F4H → `fake_sap_se16` (:8093) plus a `se16_display` recording made with
  Start-at-T-code=SE16 and `/tmp/dr_data.xlsx`; rfc/apiflow UI need F4H →
  `fake_sap_soap` (:8095) with MONITOR/secret; flow_mgmt_reports needs the
  backend's CWD/RUNS_DB at `/tmp/e2e/data`. `killall node` does NOT stop
  `next start` (process is `next-server`) — a stale server silently serves
  the previous build.

### D-051 (2026-10-01) — Health check pulled directly from SAP (SOAP RFC) + Excel export.
- /health gets **Pull from SAP** (system picker + button) next to Import.
  `POST /api/health/pull {sid}` (backend/ahc/api.py) uses the registry's
  SAP credential (D-023) over `/sap/bc/soap/rfc`, writes SAP's AHC workbook
  (ahc/writer.py), then imports it through the unchanged D-050 parser —
  the parser is the acceptance test. The workbook is stored next to the
  report; `GET /api/health/reports/{id}/workbook` exports it ("Export to
  Excel"). Imports keep their original file too. Reports carry
  `source: import|sap_pull` and `has_workbook`.
- **Structure = the sample, exactly.** `ahc/layout.json` is generated from
  a real SAP export by `python -m ahc.tools.extract_layout <file>` (styles,
  column widths, Monitors rows, every table header, per-tab title-row
  details: subtitle merge, SM51's single blank row, header heights). No
  customer values are copied; "RFCs in table ZCAS_AHC_FILTER" becomes "All
  RFC destinations". SAP's cell highlighting is reproduced per tab
  (writer `_highlight`). DB02_Log is oldest-first, newest highlighted.
- **Read-only allowlist** (`ahc/sap.py READ_ONLY_FMS`): RFC_READ_TABLE,
  TH_*, ENQUEUE_READ, RSLG_READ_SLG, SAPTUNE_*, GET_CPU/MEM_ALL,
  SWNC_COLLECTOR_GET_AGGREGATES, GWY_READ_CONNECTIONS, RZL_READ_DIR_LOCAL,
  SSFR_*, /SDF/SLIC_READ_LICENSES, SXPG_COMMAND_EXECUTE (one SM69 command).
- **HANA tabs (DB12, DB02_*, DB01) via SM69 `ZAHC_HDBSQL`** run by
  SXPG_COMMAND_EXECUTE (ahc/hana.py; override name with
  `AHC_HDBSQL_COMMAND`). SHDB_ASYNC_SQL was rejected (undocumented
  serialized input → XSLT error). BASIS setup, once per system:
  HANA user AHCMON with role MONITORING only; as <sid>adm
  `hdbuserstore SET AHCMON <dbhost>:3<nn>13@<SID> AHCMON <pw>`; SM69
  command ZAHC_HDBSQL = `/usr/sap/<SID>/hdbclient/hdbsql` with parameters
  `-U AHCMON -x -j -F |`, "additional parameters allowed"; monitoring user
  needs S_LOG_COM for it; for SLICENSE also
  `GRANT SELECT ON <schema>.SAPLIKEY TO AHCMON`. Without the command those tabs are "Not
  collected" with the reason. Verified live on F4H (2026-10-01, all five
  HANA tabs collected): SXPG passes the SQL verbatim (quoting it is a HANA
  syntax error); SM69 parameters + SQL share 255 chars (230 chars of SQL
  ok, 235 cut, more = TOO_MANY_PARAMETERS → MAX_SQL 225); every protocol
  line is CUT at 128 chars with trailing blanks dropped, so
  `HanaViaSm69.table()` reads wide results in column groups, splits a long
  text column into SUBSTR chunks of 100 and stitches rows back (needs a
  stable ORDER BY); a cut line is detected by a missing closing "|" or a
  field count unlike the header; hdbsql escapes quotes in text as \".
  DB12 = M_BACKUP_CATALOG joined to M_BACKUP_CATALOG_FILES (one row per
  file, like SAP's export); DB02_Log reads all 28 M_SYSTEM_AVAILABILITY
  columns. hdbsql prints LOB columns cut to 32 chars with a normal closing
  "|" (undetectable), so LOBs are always read in chunks (`lobs=`).
- **SLICENSE from table SAPLIKEY** (/SDF/SLIC_READ_LICENSES returns no
  licenses on F4H, only CUSTKEY = active hardware key). LIKEY = 4-digit
  length-prefixed LK|SID|hw key|product (LC rows = last check, skipped);
  VALUE (NCLOB, so not via RFC_READ_TABLE) = limit|type P/T|signature|
  installation no|start|end|system no. Read via hdbsql; needs
  `GRANT SELECT ON <schema>.SAPLIKEY TO AHCMON` (granted on F4H). Status:
  end < today → Expired; hw key ≠ active → "Valid (inactive Hardware Key)";
  else Valid. Verified field-by-field against the SLICENSE screen on F4H.
  Without HANA access it falls back to the FM's rows.
- **Export = real .xlsx (bug fix 2026-10-01).** The .XLS export would not
  open in Excel: three generated tabs (SOST, DB02_Alerts, DB02_Memory) used
  more columns than their ss:ExpandedColumnCount, which makes Excel refuse
  the whole SpreadsheetML file; SAP's own exports also carry XML-illegal
  control characters. Fixes: writer derives ExpandedColumnCount/RowCount
  from what it wrote and strips control chars (`ahc/layout_check.
  excel_problems()` checks Excel's load rules in tests); GET
  /api/health/reports/{id}/workbook now returns .xlsx by default
  (health/xlsx_export.py, openpyxl: values, fonts, fills, borders,
  alignment/wrap, merges, column widths, row heights; works for SAP's own
  files too); `?format=xls` still returns the stored original.
- **SICK via a background job (the one non-read):** transaction SICK =
  report RSICC000. ahc/xbp.py runs it through SAP's remote-enabled XBP
  interface (BAPI_XMI_LOGON, BAPI_XBP_JOB_OPEN / ADD_ABAP_STEP / CLOSE /
  START_IMMEDIATELY / STATUS_GET / SPOOLLIST_READ, then JOB_DELETE) as job
  SENTINEL_AHC_SICK and parses the spool: "no errors reported" → OK,
  anything else → Not OK with the message. Verified on F4H: ~1 s, spool
  identical to the sample's box, no job left in TBTCO. Only reports in
  xbp.ALLOWED_REPORTS may run; AHC_NO_JOBS=1 switches it off. RFC user
  needs S_XMI_PROD + S_BTCH_JOB.
- **SMMS via sapcontrol (SM69 ZAHC_SAPCONTROL, read-only functions):**
  RSMONMS_ALV has no selection screen and prints only the instance list in
  batch; MS_GET_*SERVICE are not remote-enabled. ahc/sapctl.py runs
  `sapcontrol` through SXPG: GetSystemInstanceList → the MESSAGESERVER
  instance (F4H: 01), ParameterValue ms/server_port_N there (N = 0.. until
  "FAIL: Invalid parameter") → PROT/PORT/TIMEOUT/PROCTIMEOUT/EXTBIND/HOST/
  VCLIENT/ACLFILE, GetAccessPointList → msg_server port active → "Opened".
  Timeout/proc. time default to 20/60 when unset (as in the sample). Any
  port not opened → Not OK. SM69: command `sapcontrol`, parameters
  `-nr 00 -function GetAccessPointList`, additional parameters allowed (an
  appended -nr/-function overrides). Only GetSystemInstanceList,
  GetAccessPointList, ParameterValue are sent, arguments validated. RISK:
  with additional parameters allowed, anyone with S_LOG_COM for the command
  could append `-function Stop` — restrict S_LOG_COM to the monitoring
  user or point SM69 at a wrapper script that passes only those three
  functions. Verified on F4H: 8101 HTTP ms/server_port_0 Opened 20/60.
- **Not collectable over RFC → "Not collected", never OK:** RFC_Con
  connection tests (RSRFCCHK), SM04 session priority. Analyser
  rule: SAP status unknown + no data → triage no_data, not ok.
- **Gotchas found on F4H:** time windows use SAP's clock
  (UTC + RFC_SYSTEM_INFO.RFCTZONE), not the container's (5.5 h skew emptied
  SM21); RFC_READ_TABLE must use DELIMITER "|" (offsets misaligned);
  namespaced FMs go on the wire as `_-SDF_-...`; TSL1T key is SPRAS;
  syslog priority from TSL1D.MONBEW (RA/RE very high, GR warning, WH info);
  SNAP_BEG.FLIST = key(2)+len(3)+value (FC runtime error, AP program).
- Tests: `test_ahc_pull` (fake `fake_sap_ahc_pull.py` :8091, 14 incl. one
  opt-in vs a real export with HEALTH_REAL_AHC), `test_ui_health_pull`
  (UI phase, full backend). Never commit a real customer export.

---

## 3. Architecture Snapshot

```
                    ┌────────────────────────────────────────┐
                    │        Orchestrator / Scheduler        │
                    │  (sections registry, run_id, per-SID)  │
                    └───────┬───────────────────────┬────────┘
                            │                       │
              ┌─────────────▼──────────┐   ┌────────▼─────────┐
              │   Capability Probe     │   │  System Registry │
              │ (mode + session.info)  │   │ (SID/URL/release │
              └─────────────┬──────────┘   │  /auth/tags)     │
                            │              └──────────────────┘
              ┌─────────────▼───────────────────────────┐
              │             Replay Engine               │
              │  Login plugins → step executor →        │
              │  wait_ready sync → capture/check        │
              │  ┌───────────────┐  ┌────────────────┐  │
              │  │ Scripting     │  │ DOM resolver   │  │
              │  │ resolver      │  │ (ranked        │  │
              │  │ (findById)    │  │  locators,     │  │
              │  │               │  │  self-healing) │  │
              │  └───────────────┘  └────────────────┘  │
              └─────────────┬───────────────────────────┘
                            │
     ┌──────────────┐  ┌────▼──────────────┐  ┌───────────────────┐
     │  Recorder    │  │ Results store     │  │  Report Composer  │
     │ (scripting   │  │ (run_id+sid:      │  │  (Word + PDF,     │
     │  systems     │  │  captures, checks,│  │  sections,        │
     │  only)       │  │  screenshots)     │  │  verdicts)        │
     └──────────────┘  └───────────────────┘  └───────────────────┘
```

**Flow example (reference shape):**
```json
{
  "flow_id": "sm37_cancelled_jobs",
  "applies_to": ["ECC6_*", "S4_*"],
  "params": { "from_date": "{{today-1}}", "user": "*" },
  "steps": [
    { "act": "start_tcode", "value": "SM37" },
    { "act": "set",
      "target": {
        "sid": "wnd[0]/usr/ctxtBTCH2170-FROM_DATE",
        "dom": { "label": "From date", "lsdata_hint": "...", "pos_id": "M0:46:::4:20" }
      },
      "value": "{{from_date}}" },
    { "act": "set",
      "target": { "sid": "wnd[0]/usr/chkBTCH2170-ABORTED", "dom": { "label": "Canceled" } },
      "value": true },
    { "act": "vkey", "target": { "sid": "wnd[0]" }, "value": 8 },
    { "act": "wait_ready" },
    { "act": "capture", "mode": ["screenshot", "table"] },
    { "act": "check", "type": "row_count", "op": "==", "value": 0,
      "severity": "high", "message": "Cancelled jobs found in last 24h" }
  ]
}
```

---

## 4. Phase Plan

| Phase | Deliverable | Status |
|-------|-------------|--------|
| 0 | Existing v1 tool (DOM-based BASIS monitoring + Word/PDF report) | ✅ DONE |
| 1 | Flow schema (final) + replay engine core with **both resolvers**; hand-written flows for SM37 (ALV-heavy on purpose), ST22, SM12; capability probe; login plugins for 2 most-different systems | 🟨 SCAFFOLDED — blocked on live systems: scripting entry-point verify, DOM ALV read, SAML plugin, ST22/SM12 flows |
| 2 | Recorder (scripting event stream + DOM metadata snapshot; start/stop UI; parameter marking) | 🟨 SCAFFOLDED — recorder.py + inject JS; **recorder_service.py (session lifecycle) + /api/recorder/{start,stop,cancel,status,active} + Recorder page all built, D-020**; FindByPosition still needs live verify |
| 3 | Parameterization & datasets (JSON/Excel, runtime expressions, per-run variants) | ✅ BUILT — datasets.py (JSON + XLSX), expressions in engine; untested vs live flows |
| 4 | Verification layer (structured extraction, check types, severities) wired into anomaly detection | ✅ BUILT — checks.py: row_count, cell_value, statusbar_type, field_value, text |
| 5 | Integration: sections registry; recorded flows merge into existing Word/PDF pipeline alongside v1 BASIS section | ✅ BUILT — sections.py (FlowSection + CoreBasisSection, D-019); **report_composer.py renders both Core BASIS and Functional Sanity with severity chips into one Word+PDF report (D-014, D-020), auto-generated at the end of every `/api/run`, downloadable via `/api/reports`** |
| 6 | Robustness: popup interceptor, session-loss recovery, DOM self-healing + LLM repair mode, scheduling, run history/drift | 🟨 SCAFFOLDED — recovery.py, healing.py (repair-queue capture; LLM pass TODO), store.py (SQLite history, **thread-safety bug found+fixed D-020**); scheduler TODO |
| 7 | Frontend per D-016 (added) | ✅ BUILT — Next.js app: tokens, Shell/sidebar nav (was missing entirely — pages were unreachable), Landscape + Runs + Recorder + Reports pages, tsconfig/typescript devDeps added; **typechecks and builds clean (`next build`), all 4 routes verified serving 200 (D-020)**; Recorded flows/Datasets library pages still not built |

Legend: ✅ done · 🟨 scaffolded (code exists, needs live-system work or v1
context to finish) · ⬜ not started.
Backend API (main.py): /api/systems, /api/run, /api/events (SSE), /api/runs
— the D-016 API surface exists end to end.

Note (per D-003): DOM resolver work that was originally sketched as "Phase 6
robustness" is pulled forward — a first working DOM resolver ships in Phase 1;
Phase 6 hardens it (self-healing, repair mode).

---

## 5. Known Risks / Watch List

- **ALV grids in WebGUI** render differently from classic dynpro tables —
  hardest read/interact target. Deliberately confronted in Phase 1 via SM37.
- **Unexpected popups**: system messages, "continue in same session?",
  scripting notification (if force_notification not disabled), auth dialogs.
- **Session timeouts / ICM errors** mid-flow.
- **S/4 screen reworks & replaced transactions** across releases → flow
  variants (D-006).
- **DOM resolver confidence**: on scripting-disabled systems replay is
  best-effort; mismatches must be flagged in the report, never silently
  guessed.
- **Security posture**: dedicated locked-down replay users; replay on
  functional T-codes must be display/dry-run oriented unless explicitly
  designed otherwise.
- **v1 sync pattern is defensive-layered, not clean**: busy-overlay poll +
  `networkidle` + flat residual sleeps, stacked together (see §6.4). A
  `replace_sleeps.py` refactor script in the repo is direct evidence sleep-
  based waiting was already flagged as a problem once; the new engine's
  `wait_ready` must actually replace all three layers, not add a fourth.
- **No re-login / session-recovery in v1**: a mid-run WebGUI session drop
  (timeout, ICM error) is not detected — screenshots just silently come back
  blank or stale. D-007's "central re-auth handling" has no prior art to
  reuse; it must be built from scratch.
- **Plaintext credentials**: v1 stores SAP/SSH passwords unencrypted in
  SQLite (`database.py`). Don't carry this pattern into v2's system registry
  (D-006) without at least flagging it — decide encryption-at-rest before
  building the registry.
- **v1's report has no severity model**: anomalies are a flat string list
  (present = "Action Required", absent = "Normal"); no severity levels exist
  today despite D-009 assuming Word/PDF integration will carry them through.
  Phase 5 integration needs to introduce severity into the existing report
  template, not just consume one that's already there.

---

## 6. v1 Codebase Reference (verified 2026-07-31, resolves OQ-3)

Full read of `/Users/giri/Antigravity/sap_monitoring` (backend `main.py`,
`orchestrator.py`, `sap_automator.py` [2385 lines], `sap_rfc_client.py`,
`report_generator.py`, `database.py`, `config.json`, `requirements.txt`,
frontend `package.json`/`page.tsx`). Root-level `extracted_*.py`, `run_*_test.py`,
`insert_*.py`, `replace_*.py`, `fix_*.py`, `test_*.py`, `match_*.json` are
one-off debug/scratch artifacts, not architecture — ignore them in future
sessions unless a specific one is cited here.

**6.1 Automation stack & selectors.** Playwright (`playwright==1.40.0`),
headless Chromium (`sap_automator.py:58`). No SAP GUI Scripting API anywhere.
Selector priority actually used (see D-012): semantic attributes
(`input[name="sap-user"]`, `input[title="Command Field"]`,
`input[id*="okcd"]`) → Playwright text/role locators (`get_by_text("Execute",
exact=True)`, `get_by_role("checkbox")`) → XPath-by-text + parent-walk for
tree widgets (`st03n_click_tree_node()` line 242, `click_tree_node()` line
949, walks up to 6 parents for `className.includes('TreeNode')` /
`role="treeitem"`) → SAP UI-library CSS class fragments (`.lsBlockLayer`,
`#ls-loading` busy indicators; `.urST5BaseBorder` scroll container) →
computed pixel-coordinate ALV cell matching, last resort, only in SMICM
(`sap_automator.py:2050-2124`) and STRUST (`2212-2270`): locate a header
cell's x-offset by text (`text === 'Valid To'`), then match data-row cells
within ±10px. T-code navigation itself goes through the OK-code field first
(`~transaction` URL param as fallback, line 232-238/318) — tried as a
"massive performance boost" over full page navigation.

**6.2 RFC path (`sap_rfc_client.py` + `nwrfcsdk`) — separate from WebGUI, not
feeding it.** `pyrfc.Connection` wraps the native NetWeaver RFC SDK
(`nwrfcsdk/`, linked via Docker env vars `SAPNWRFC_HOME`/`LD_LIBRARY_PATH`).
`orchestrator.run_sap_checks()` (line 66) does an RFC login first and
**aborts the whole run if RFC fails** — even though RFC supplies real data
for only SM50/SM51 (`TH_WPINFO`/`TH_SERVER_LIST`), SM12 (`ENQUEUE_READ`), and
a best-effort SM37 (`BAPI_XBP_JOB_SELECT`, falls back to a stub message). The
other 17 of 23 monitored T-codes get a trivial `RFC_SYSTEM_INFO` stub — all
real content for those comes from the WebGUI/Playwright path. RFC also does
filesystem stats (`SXPG_COMMAND_EXECUTE` running `df -h`, falling back to
`RFC_SYSTEM_INFO`+`TH_SAPREL2`) and backs the frontend's standalone
"test connection" button. **Open decision for v2** (see OQ-6): keep RFC as a
connectivity/login gate, or let the new capability probe (D-005) subsume
that job against WebGUI directly and drop RFC as a hard dependency.

**6.3 T-code coverage.** `MONITORED_TCODES` registry, `orchestrator.py:7-31`,
23 entries: SM50, SM51, ST06, SM66, SM21, DB02, DB12, ST03N, SCC4, SMLG,
SMGW, AL08, SM12, SM13, SMICM, SM37, ST22, SM58, WE02, SMQ1, SMQ2, SOST,
STRUST. Twelve have bespoke handlers in `sap_automator.py` (SM21 :385, SM12
:651, SM37 :746, DB02 :920, SM13/SM58/WE02/SMQ1/SMQ2 share an "Execute"-click
handler :1279, ST22 :1312, ST03N :1416 — the most elaborate, full tree
navigation plus embedded SQL for HANA host stats, SCC4 :1637, SMLG :1813,
SMGW :1901, SMICM :2000, STRUST :2149); the rest fall through to a generic
screenshot-only path (`:2353-2364`). Confirms "every new T-code needs manual
build effort" is literally true today.

**6.4 Sync pattern.** `_wait_for_sap_ready()` (`sap_automator.py:21-53`)
polls `.lsBlockLayer`/`#ls-loading` every 200ms up to 8s, **then still adds a
flat `wait_for_timeout(1000)`** regardless; most navigations also wrap
`wait_for_load_state("networkidle")` in try/except plus another 2-3s
`asyncio.sleep`. Root `replace_sleeps.py` is a regex script that already
converted `asyncio.sleep(5|6|8|10)` calls into `_wait_for_sap_ready()` calls —
proof this was flagged before and only partially fixed. ST03N has the one
genuinely clean wait: polls up to 60×2s for a "Dialog" button to disappear
after triggering a server-side calculation (`:1512-1524`) — model the new
`wait_ready` step type on this, not on the busy-overlay-plus-sleep pattern.

**6.5 Anomaly detection — 100% regex/keyword/threshold, no vision/LLM calls
anywhere** (verified by grep across all backend `.py` + requirements.txt).
Examples: ST03N flags DIALOG response time > 1500ms (regex-extracted,
`:1571`); SCC4 string-matches expected client-security field values
(`:1751-1758`); STRUST parses "Valid To" dates (3 formats) and flags
certs expiring within 30 days (`:2162-2278`); SM21/ST22 use keyword→category
lookup tables (18 entries each) in `orchestrator.py` (`analyze_sm21_logs`
:357, `analyze_st22_dumps` :461). No severity field exists anywhere — see
Known Risks.

**6.6 Report generation** (`report_generator.py`, 805 lines). Both a
`python-docx` Word doc and a `reportlab` PDF from the same `results` dict, A3
landscape (screenshots are full-page), with a **hardcoded, machine-specific
cover-image path** (`:50,422`) that silently no-ops elsewhere via an
`os.path.exists` check — worth fixing before this becomes v2's shared report
composer. Sections: Executive Summary (anomaly count table) → Infrastructure
& Server Health (RFC filesystem, SSH `df -h` per server, URL health checks)
→ Detailed Status (one subsection per T-code, T-code-specific screenshot
embedding for the 12 bespoke handlers, generic screenshot otherwise, bullet
list of anomalies). Raw RFC data tables are explicitly disabled
(`if False: # Keep original logic for future reference`, lines 359/761) —
the report already relies on screenshots + text analysis, not RFC tables,
despite RFC being architecturally "primary."

**6.7 Orchestration & API.** No plugin/registry abstraction beyond the flat
`MONITORED_TCODES` list — adding a check today means adding a list entry plus
inline `if tcode_info["tcode"] == "X":` logic in one big loop
(`orchestrator.py:110`). `main.py` (FastAPI, CORS wide open) exposes system
CRUD (SQLite-backed), `POST /api/run/bulk` (sequential, one system at a time,
via `BackgroundTasks`), `GET /api/status` (single global in-memory run state,
no history, rejects concurrent runs with HTTP 400), `GET /api/download/zip`,
and an RFC-only `POST /api/test-connection`.

**6.8 Login/auth.** Plain JS-injected username/password fill
(`input[name="sap-user"]`/`sap-password`, deliberately bypassing Playwright's
`.fill()` to dodge SAP's CSS visibility tricks — comment at line 79), submit
via `#sapwp_BUTTON_LOGIN` or `form.submit()` fallback. Generic "multiple
logon" popup dismissal (click "Continue"/"Yes" by exact text) repeated before
every T-code nav. **No re-login logic** (see Known Risks). No SSO/SAML in
code at all — D-007's pluggable login strategies have no v1 equivalent to
migrate, only a single basic-auth path to keep as one plugin implementation.

**6.9 Config.** Root `config.json` (webgui_url/sap_username/sap_password) is
**dead** — only read by three throwaway test scripts, not by any real
application code. Actual runtime config is SQLite (`database.py`, `systems`
table): per-system hostname/SID/instance/client/credentials, WebGUI URL, SSH
auth (password/key/"Citadel MFA"), DB host, a JSON-serialized list of
app/dialog servers (each with optional per-server SSH override), and a
JSON-serialized list of health-check URLs. Passwords are plaintext (see Known
Risks). This SQLite shape is a reasonable starting draft for D-006's system
registry, minus the encryption gap.

**6.10 Frontend.** Next.js 16 / React 19, single page (`page.tsx`, ~420
lines), two views toggled by local state (no routing): System Inventory
(CRUD table + modal form mirroring the SQLite schema) and Batch Execution
(checkbox-select → trigger bulk run → poll `/api/status` every 2s → download
zip). No report preview in-browser, no live per-T-code status, no
screenshot/GPS streaming — the frontend only triggers runs and hands off the
zip; all real content lives in the generated Word/PDF.

---

## 7. Open Questions (to resolve in upcoming sessions)

- OQ-1: Final flow schema sign-off (step kinds, check types, severity scale)
  and its exact mapping into the existing Word/PDF report structure.
- OQ-2 (narrowed by D-015): auth dimension decided — one basic-auth system +
  one SSO/SAML system. **Candidate basic-auth system named 2026-08-01: `TEST`
  at `nwrhel9.techlabhome.in`, client 100 (D-020e)** — not yet confirmed
  scripting-vs-DOM; run `backend/live_probe_check.py` from a machine that can
  reach it (Cowork's sandbox can't) to get the actual answer. Still open: the
  SSO/SAML reference system, and MFA-exemption confirmation for that one.
- ~~OQ-3: v1 codebase specifics~~ — **resolved 2026-07-31, see §6.**
- OQ-4: Which functional sanity checks the first recorded sections should
  cover (candidates discussed: VA03 display dry-run, WE02 IDoc status, SM35
  batch input)?
- OQ-5: Dataset format preference — Excel-first (functional-team friendly) or
  JSON-first?
- ~~OQ-6: RFC path fate~~ — **resolved 2026-07-31 as D-013: demoted to
  optional enrichment plugin; capability probe owns connectivity.**
- ~~OQ-7: report composer~~ — **resolved 2026-07-31 as D-014: inherit
  `report_generator.py`, refactor (cover path, severity model, pluggable
  sections).**
- OQ-8 (new): Credential encryption-at-rest design for the v2 system
  registry (v1 stores plaintext in SQLite) — must be decided before Phase 1
  builds the registry schema.

**D-018 (2026-08-01) — Interim credential handling + self-healing safety principle.**
(a) Until OQ-8 is decided, credentials flow ONLY through the
CredentialProvider interface; the interim EnvCredentialProvider reads
SAP_<SID>_USER / SAP_<SID>_PASSWORD env vars — no secrets in files or the
registry. This is a stopgap, not the OQ-8 resolution. The systems.json
registry shape (no credential fields) IS final.
(b) Self-healing never self-modifies flows mid-run: failed DOM resolutions
produce repair items (screenshot + trimmed DOM + target) in a repair queue;
an out-of-band pass proposes fixes; a human (or supervised session) applies
them to flow files, so git diffs remain the audit trail and replay stays
deterministic.

---

## 8. Session Log

**2026-07-31 — Session 1 (planning).** Established goal, discovered v1 tool is
DOM-based and scripting-free, made decisions D-001…D-011, drafted architecture
+ flow schema + phase plan, elevated DOM resolver to core scope due to customer
security policies, created this skill file.

**2026-07-31 — Session 2 (v1 codebase deep-read, resolves OQ-3).** Read v1's
full backend (`main.py`, `orchestrator.py`, `sap_automator.py`,
`sap_rfc_client.py`, `report_generator.py`, `database.py`) plus frontend and
config. Corrected the "raw positional ID" assumption in favor of v1's actual
attribute/text/role/class selector priority (logged as D-012, refines not
reverses D-002). Discovered a previously-unknown parallel RFC path
(`pyrfc`/`nwrfcsdk`) that gates login but supplies real data for only ~4 of 23
T-codes. Confirmed sleep-based sync was a known, partially-fixed problem
(`replace_sleeps.py`). Found no re-login logic, no severity model in the
report, plaintext credential storage, and a dead root `config.json`. Added §6
as the durable code-verified reference; opened OQ-6/OQ-7; resolved OQ-3.

**2026-07-31 — Session 3 (decisions on OQ-2/6/7, Claude.ai).** Reviewed
session-2 code findings with Giri. Logged D-013 (RFC demoted to optional
enrichment; probe owns connectivity), D-014 (report composer inherited +
refactored: cover path, severity model, pluggable sections), D-015 (Phase 1
login plugins = basic + SAML; MFA-exemption/X.509/persistent-cookie
mitigations to raise with IdP teams early). Opened OQ-8 (credential
encryption-at-rest, blocker for registry schema). Next: Giri names the two
reference SIDs; then Phase 1 build can start in Claude Code — suggested
order: flow schema finalization → capability probe → login plugins →
hand-written SM37 flow with both resolvers.

**2026-07-31 — Session 4 (UI design direction, Claude.ai).** Giri requested
an Apple macOS/iOS/tvOS-style interface. Produced and approved
`sap-agent-ui-mockup.html` (Landscape view: sidebar, Dynamic-Island run
status, tvOS system cards, Core BASIS + Functional sanity sections, recent
runs). Logged D-016 (design tokens, signature element, SF licensing
constraint, derived API requirements: SSE run events, per-system status,
severity chips) and D-017 (v1 orchestrator wrapped as Core BASIS section
provider; automator untouched; only v1's frontend page replaced). Mockup to
be committed to the repo as the design reference for the frontend rebuild.

**2026-08-01 — Session 5 (phases 2–7 scaffold build, Claude.ai).** Built:
checks.py (all Phase 4 check types), datasets.py (JSON+XLSX), registry.py
(systems.json shape final, EnvCredentialProvider interim per D-018),
recovery.py (popup rules + with_recovery re-login wrapper), healing.py
(repair-queue capture per D-018b), store.py (SQLite run history),
sections.py (FlowSection working; CoreBasisSection = precise contract
docstring for Claude Code + report bridge with severity mapping),
recorder/recorder.py (inject JS: FindByPosition→scripting ID + DOM metadata
snapshot, event coalescing — needs live verify), engine/run.py CLI,
main.py (FastAPI: systems/run/SSE events/history — D-016 API surface),
frontend/ (Next.js: tokens css, Landscape page with Island on SSE).
Phase table updated with SCAFFOLDED statuses. Repo pushed to GitHub
(girid333/claude_monitoring_tool). Next: live-system work per README
checklist; CoreBasisSection wrap in Claude Code; reference SIDs (OQ-2) and
OQ-8 still open.

**2026-08-01 — Session 6 (handoff to Cowork, Claude.ai).** Giri is moving
day-to-day work to Cowork (Claude Desktop) pointed at
`/Users/giri/Antigravity`, which contains BOTH repos: `claude_monitoring_tool`
(v2, this project) and `sap_monitoring` (v1). Phases 2–7 zip extracted into
the local repo and pushed to GitHub (girid333/claude_monitoring_tool) — the
git repo is now the single source of truth; no more zip round-trips.
**Instructions for the first Cowork session (and any local session):**
(1) Read `claude_monitoring_tool/SKILL.md` (this file) + `README.md` fully
before doing anything. (2) Priority tasks, in order: wrap the v1
orchestrator as CoreBasisSection per the contract docstring in
`backend/engine/sections.py` (do NOT modify v1's `sap_automator.py`);
build the recorder start/stop UI; remaining frontend views (Runs, Recorder,
Reports) per `design/mockup.html` + D-016 tokens. (3) Live-system tasks
(blocked on OQ-2 reference SIDs): verify scripting JS entry points
(`resolvers/scripting.py` `_SESSION_JS`, recorder `FindByPosition`), DOM
ALV table read via the SM37 flow, SAML login plugin. (4) End EVERY session
by appending a dated entry here and committing SKILL.md. (5) Blockers only
Giri can resolve: OQ-2 (two reference SIDs + MFA-exemption confirmation)
and OQ-8 (credential encryption-at-rest; interim = env vars per D-018 —
never write plaintext secrets to any file).

**2026-08-01 — Session 7 (CoreBasisSection implemented, Cowork).** First
Cowork session picked up the Session 6 handoff (folder
`/Users/giri/Antigravity` connected). Completed the #1 priority task:
implemented `CoreBasisSection.run()` in `backend/engine/sections.py` per the
D-017 contract, backed by a new `backend/engine/core_basis_wrap.py`
(import shim + `ResilientRfcClient` proxy + v1 config builder — see D-019
for why and how). Wired it into `main.py`: `RunRequest.include_core_basis`
flag, `core_basis_start`/`core_basis_done` SSE events, and a new
`core_basis_runs` SQLite table in `RunStore` (v1's raw results dict doesn't
fit `FlowRunResult`'s schema, so it gets its own table rather than being
force-fit — kept `store.py` free of any import on `sections.py`/`engine.py`
so it stays importable without Playwright installed). Verified with a mocked
dry run (`_dryrun_core_basis.py`, kept in the repo as a standing manual
smoke test): v1 imports cleanly, RFC failure no longer aborts the run, all
23 T-codes' screenshot+analysis pipeline runs untouched, and the section
correctly reports `ok=False` with an anomaly count reflecting the (expected,
honest) "RFC unavailable" entries for the 4 RFC-dependent T-codes. No
sap_monitoring file was modified. Live-system verification (a real WebGUI +
RFC connection) remains blocked on OQ-2 (reference SIDs) as before — this
was a code-level integration task, not a live-system task.
**Not done this session** (still open for #1's full completion): extending
`report_generator.py` / `flow_results_to_report_rows` to actually render
`SectionResult.v1_results` in the Word/PDF output (D-014's report-composer
refactor) — right now `core_basis_runs` data is queryable via `RunStore` and
streamed over SSE, but doesn't yet appear in a generated report. Also not
started: the recorder start/stop UI and remaining frontend views (Runs,
Recorder, Reports), which were priorities #2 and #3 in the Session 6
handoff. Next: either finish the report-composer bridge for Core BASIS
results, or move to the recorder UI — Giri to say which.

**2026-08-01 — Session 8 (report bridge, recorder UI, frontend, Cowork).**
Giri said "complete everything, let's test it out" — the three remaining
Session 6/7 priorities plus a live test. Live test attempted first: Giri
gave a candidate system (TEST / nwrhel9.techlabhome.in / client 100) and
credentials in chat; confirmed via direct network test that Cowork's
sandbox cannot reach that host (allowlist-blocked), so pivoted to building
everything with mocked verification plus a standalone script Giri can run
himself (`live_probe_check.py`). Completed, in order: (1) report bridge —
`report_composer.py` inherits v1's `report_generator.py` rendering, adds
severity chips and a Functional Sanity section, auto-runs at the end of
`/api/run`, downloadable via new `/api/reports` endpoints; verified by
generating and reading back a real docx+pdf. (2) Recorder start/stop —
`recorder_service.py` (headed-browser session lifecycle) + 5 new
`/api/recorder/*` endpoints + a Recorder page. (3) Remaining frontend views
— Runs and Reports pages, PLUS a Shell/sidebar component that turned out to
be entirely missing (pages built before this session were unreachable —
there was no navigation at all), PLUS missing `tsconfig.json` and
TypeScript devDependencies (frontend had never been built once until this
session). Along the way found and fixed a real bug: `RunStore`'s sqlite3
connection wasn't thread-safe against FastAPI's threadpool
(`check_same_thread=False`), which would have crashed `/api/reports` and
silently already affected `/api/runs`. All work verified without a live
system: Python dry runs (`_dryrun_core_basis.py`, `_dryrun_report.py`, kept
as smoke tests), a FastAPI `TestClient` pass over every new/changed
endpoint, and `next build`/`next start` for the frontend (typechecks clean,
all 4 routes return 200). Logged as D-020. **Not done / still open:**
live-system verification of anything (scripting entry points, DOM ALV read,
SAML login, the recorder's actual FindByPosition call) — genuinely blocked
until Giri runs `live_probe_check.py` himself and reports back; "Recorded
flows" and "Datasets" library pages from the mockup's sidebar were not
built (weren't in the three requested priorities). Next: Giri runs the live
probe script and reports results — that unblocks the real Phase 1 live-
system checklist in README.md.

**2026-09-30 — D-050 integration (Cowork).** Integrated the Sentinel
package from `_incoming/sentinel_d050/` on branch `d050-landing-dashboard`:
route split (/, /basis, /health, /replay), TopBar across sections, Health
Check backend module + suites. Baseline run of every existing e2e suite
BEFORE changes, then again after: no regressions; four UI suites that were
already stale pre-D-050 repaired (details in the D-050 integration note).
Docker rebuild + hand verification on 192.168.0.165 left to Giri (not
reachable from the Cowork sandbox). NOTE: this repo SKILL.md was last
synced at Session 8 (D-020); D-021…D-049 live in the account-level skill
and should be synced back here.

**2026-10-01 — D-051 direct pull (Cowork).** Phase 1 probe (27/33 checks
sourceable over SOAP RFC), then Phase 2: collectors for all 33 checks,
SpreadsheetML writer matching the sample's layout, Pull from SAP + Export
to Excel on /health. After Giri created ZAHC_HDBSQL + the AHCMON
key, then SELECT on SAPLIKEY, SICK via an RSICC000 job and SMMS via
ZAHC_SAPCONTROL, the live F4H pull collects 29 of 33 checks in ~6 s (119
calls); not collected: RFC_Con, SM04 priority, ST11 mtimes, and ST03N (F4H
is a dummy system with no workload). Full suite green.

*(Append new dated entries here every working session: what was built/decided,
what changed, what's next.)*
