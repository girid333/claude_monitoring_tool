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
| 1 | Flow schema (final) + replay engine core with **both resolvers**; hand-written flows for SM37 (ALV-heavy on purpose), ST22, SM12; capability probe; login plugins for 2 most-different systems | ⬜ NEXT |
| 2 | Recorder (scripting event stream + DOM metadata snapshot; start/stop UI; parameter marking) | ⬜ |
| 3 | Parameterization & datasets (JSON/Excel, runtime expressions, per-run variants) | ⬜ |
| 4 | Verification layer (structured extraction, check types, severities) wired into anomaly detection | ⬜ |
| 5 | Integration: sections registry; recorded flows merge into existing Word/PDF pipeline alongside v1 BASIS section | ⬜ |
| 6 | Robustness: popup interceptor, session-loss recovery, DOM self-healing + LLM repair mode, scheduling, run history/drift | ⬜ |

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
  one SSO/SAML system. **Still open: the concrete SIDs**, plus confirmation
  of the MFA-exemption path for the SSO system's monitoring user.
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

*(Append new dated entries here every working session: what was built/decided,
what changed, what's next.)*
