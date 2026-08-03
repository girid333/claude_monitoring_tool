# SAP GUI Bridge Agent — Architecture Plan (D-044)

Status: **SANDBOX BUILT (Phase A + agent skeleton).** Per Giri's explicit
instruction, this shipped as an isolated, additive section — a new "SAP
GUI (sandbox)" page, a new backend module (`engine/agent_hub.py`) mounted
with one `include_router()` line, and a separate flow storage directory
(`flows_sapgui/`). It is **not merged** into the existing WebGUI/Core
BASIS pipeline described in §4.4 below, and that section's "reuse the same
flow library/pipeline/reports" framing is **superseded** — merging is a
future decision, contingent on the sandbox proving useful. WebGUI
record/replay and Core BASIS are untouched: verified by a full re-run of
every existing E2E suite after this work (see SKILL.md D-044 entry).

What's real vs. best-effort, concretely:
- Agent Hub protocol (pairing, WebSocket registry, job dispatch, SSE
  fan-out, flow storage, replay-time overrides): built and proven with a
  real running server + a fake agent (`agent/sapgui_bridge_agent.py
  --fake`) — `backend/e2e/test_sapgui_agent_hub.py`, 18/18 checks.
- Real COM connect/pair/probe and REPLAY (`findById`, `.press()`,
  `.selected`, `.sendVKey`, `HardCopy`): implemented using standard,
  documented SAP GUI Scripting API members — confident in the approach,
  unverified against a real session (needs Giri's workstation).
- Real RECORD: SAP GUI Scripting has no public "subscribe to user
  actions" event stream, so the agent polls the active window's control
  tree and diffs it. Workable, but will need tuning against real screens —
  treat as a first cut, not finished.

---

## 1. Goal

Offer **two recording/replay modes per flow**, selectable at record time:

| Mode | Recorder | Replayer | Runs where |
|------|----------|----------|-----------|
| `webgui` (existing) | Headless Chromium + CDP remote control | Container, fully server-side | Anywhere, anytime (scheduled) |
| `sapgui` (new) | SAP GUI Scripting API on the user's Windows workstation | A connected **bridge agent** | Only while an agent with SAP GUI is online |

Why: complex screens (deep ALV interaction, attachments, user exits,
multi-window dialogs) are natively and completely exposed by the SAP GUI
Scripting API — the same foundation CBTA and Tricentis build on — whereas
WebGUI's DOM requires porting each widget class individually.

## 2. The constraint that shapes everything

SAP GUI scripting is **COM/OLE automation**: it can only execute on the
Windows machine where SAP GUI is installed and the session is running.
The backend is a Linux container. Therefore:

- The container can NEVER drive SAP GUI directly. No network setting
  changes this.
- The workstation the user browses the app FROM is the machine that has
  SAP GUI, the SAProuter config, and the correct host/IP resolution.
- **All connectivity is OUTBOUND from the workstation to the server** —
  the agent dials the backend the same way the browser already does. The
  container never resolves the workstation's hostname, never opens a
  connection to it, never traverses a client-side firewall.

This is exactly CBTA's client-component model.

## 3. Architecture

```
┌───────────────────────────── Linux container ─────────────────────────────┐
│  FastAPI backend                                                          │
│  ┌──────────────┐  ┌───────────────┐  ┌────────────────────────────────┐  │
│  │ Run pipeline │  │ Flow library  │  │ NEW: Agent Hub                 │  │
│  │ (unchanged)  │  │ (shared)      │  │  /api/agent/ws (WebSocket)     │  │
│  │              │  │  engine:      │  │  - pairing (one-time token)    │  │
│  │ webgui flows │  │  webgui|sapgui│  │  - agent registry (in-mem+DB)  │  │
│  │ run in-place │  │               │  │  - job dispatch + result recv  │  │
│  └──────────────┘  └───────────────┘  └────────────▲───────────────────┘  │
└───────────────────────────────────────────────────┼──────────────────────┘
                                   outbound WSS     │
┌── Windows workstation (user's, or a dedicated always-on VM) ──────────────┐
│  Browser ──► app URL (unchanged)                  │                       │
│  ┌────────────────────────────────────────────────┴───────────────────┐   │
│  │ NEW: sapgui-bridge-agent.exe  (Python, PyInstaller single file)    │   │
│  │  - connects outbound to /api/agent/ws with paired token            │   │
│  │  - COM: win32com GetObject("SAPGUI") → GuiApplication → sessions   │   │
│  │  - RECORD: session.Record = True → event stream → Flow JSON steps  │   │
│  │  - REPLAY: findById(sid).press/…, HardCopy screenshots             │   │
│  │  - prereq self-check: GUI installed? scripting enabled? session?   │   │
│  └────────────────────────────────────────────────────────────────────┘   │
│  SAP GUI for Windows ──► SAP systems (workstation's own routing)          │
└───────────────────────────────────────────────────────────────────────────┘
```

## 4. Components in detail

### 4.1 Backend: Agent Hub (new module `engine/agent_hub.py`)

- `WS /api/agent/ws` — agents connect outbound, authenticate with their
  paired token, then hold the socket open. Heartbeat every 15s (reuse the
  D-029 keepalive pattern).
- `POST /api/agent/pair/start` — UI generates a one-time pairing code
  (shown as text + copyable string). Agent is launched with this code
  once; it exchanges the code for a long-lived agent token (stored
  encrypted with the existing `crypto.py` Fernet key, D-023 pattern).
- `GET /api/agents` — connected/known agents for the UI: hostname,
  username, SAP GUI version, scripting-enabled status, last seen.
- Job protocol (JSON over the socket):
  - server→agent: `{"kind":"record_start","sid":...,"tcode":...}`,
    `{"kind":"record_stop"}`, `{"kind":"replay","flow":{...},"run_id":...,"overrides":{...}}`, `{"kind":"probe"}`
  - agent→server: `{"kind":"step", ...}` (live, feeds the same recorder
    status endpoint the UI already polls), `{"kind":"screenshot","b64":...}`,
    `{"kind":"step_result",...}` / `{"kind":"replay_done",...}` (mapped
    into the SAME `FlowRunResult`/`StepResult` shapes → same RunStore,
    same SSE events, same reports — zero changes to the report pipeline).

### 4.2 Windows bridge agent (new top-level dir `agent/`)

- Python 3.12 + `pywin32` (COM) + `websockets`; packaged with PyInstaller
  into a single `sapgui-bridge-agent.exe` (no Python install needed).
- On start: tray/console app; prereq self-check (SAP GUI installed?
  scripting activated in GUI options? server reachable?), then connects.
- **Recording**: attaches to the user's existing open SAP GUI session
  (`GetObject("SAPGUI").GetScriptingEngine`), subscribes to the scripting
  event stream. Every user action arrives as (session, target-ID, method,
  params) — precisely the semantic IDs our `Target.sid` field was designed
  for (D-001/D-002). Steps map 1:1 into the existing Flow JSON acts
  (`set`, `press`, `select`, `vkey`, `start_tcode`). Manual capture
  buttons in the web UI send `capture` markers through the socket, same
  as the WebGUI recorder.
- **Replay**: `session.findById(sid)` + `.text = value` / `.press()` /
  `.selected = bool` / `.sendVKey n`; screenshots via
  `session.findById("wnd[0]").HardCopy(path)` uploaded as base64.
  Overrides (D-043) apply server-side before dispatch — the agent just
  executes the flow it's given.
- Attachments/file dialogs: native dialogs are visible to the agent
  (it runs ON the workstation) — automatable with pywinauto later; logged
  as a phase-2 agent feature, not in the first cut.

### 4.3 Frontend

- **Recorder page**: mode toggle "WebGUI (server) / SAP GUI (this
  workstation)". SAPGUI mode shows agent status for *your* workstation
  (detected by pairing) and disables Start until an agent is connected
  and prereqs pass. Live step list works unchanged (same status
  endpoint).
- **Landscape**: flow chips show an engine badge (`WEBGUI` / `SAPGUI`).
  Executing a SAPGUI flow with no agent online → clear pre-flight error
  before the run starts, not a mid-run failure.
- **New "Agents" section** on the Systems page: paired agents, connection
  state, "Generate pairing code" button, download link for the .exe.

### 4.4 What does NOT change

Flow library, RunStore, SSE events, report composer, Reports UI, override
mechanism, step viewer (`describe_steps` gains sid-target labels — they
already render), Core BASIS section. A `sapgui` flow is just a Flow whose
steps carry `sid` targets and whose execution is dispatched instead of
run in-process.

## 5. Security

- Pairing: one-time code (5-min TTL) → per-agent Fernet-encrypted token;
  revocable from the Agents UI.
- The agent only ever connects OUT to the server URL the user gives it;
  it accepts no inbound connections.
- Replay jobs only reach an agent for systems that agent declared it can
  reach (probe result), and the UI shows which agent will execute.
- SAP credentials are NOT sent to the agent for recording (the user is
  already logged into SAP GUI themselves). For unattended replay on a
  dedicated VM agent, the agent starts sessions via `sapshcut`/logon pad
  using registry credentials delivered per-job over the WSS channel and
  held only in memory — flagged for explicit review before that phase.

## 6. Prerequisites (Giri's side)

1. `sapgui/user_scripting = TRUE` (RZ11, server profile) on target
   systems; ideally also `user_scripting_per_user` review.
2. SAP GUI for Windows on the workstation with scripting enabled
   (Options → Accessibility & Scripting → Scripting: enable, and disable
   the "notify" popups for a smooth recording).
3. For scheduled/unattended SAPGUI replays: one always-on Windows VM
   with SAP GUI + agent (the container alone can never run these).

## 7. Honest limitations

- SAPGUI flows can only run while a suitable agent is online — that's
  physics, not a design gap. WebGUI mode stays the only fully
  container-native mode.
- First cut records from an ALREADY-OPEN SAP GUI session (user logs in
  themselves); agent-initiated logon comes with the unattended-replay
  phase.
- Multi-window (wnd[1]+ popups) is naturally covered by scripting IDs —
  one of the main wins over WebGUI mode.
- Agent is Windows-only (SAP GUI for Java scripting is too limited to be
  worth targeting).

## 8. Delivery phases (each with its own E2E verification)

| Phase | Deliverable | Verified by |
|-------|-------------|-------------|
| A | Agent Hub (WS endpoint, pairing, registry, UI Agents section) | E2E with a FAKE agent script (Linux-runnable) driving the real WS protocol |
| B | Agent skeleton .exe: connect, pair, prereq self-check, probe | Giri runs it on his workstation against his deployment |
| C | SAPGUI recording → Flow JSON in the shared library + step viewer | Fake scripting-engine mock for CI; live validation by Giri on a real T-code |
| D | SAPGUI replay dispatch + results/screenshots into the normal pipeline + reports | Same pair: mocked E2E + live run |
| E | Unattended replay (dedicated VM agent, credential delivery) + attachments via pywinauto | Scoped after A–D are proven live |

Phases A and C/D get the same treatment as everything since D-030: real
protocol-level E2E tests committed under `backend/e2e/`, using a fake
agent (and a fake scripting engine inside it) that runs on Linux — the
COM layer is isolated behind one interface so everything above it is
testable in CI, and only the thin COM binding needs Giri's workstation.

## 9. Open questions for Giri (answer before Phase A starts)

1. Is there (or can there be) one always-on Windows machine for
   unattended/scheduled SAPGUI replays, or is "user's workstation, only
   while online" acceptable for now?
2. Are you able to set `sapgui/user_scripting = TRUE` on your home SAP
   system to validate recording in Phase B/C?
3. Agent distribution: is a downloadable .exe from the app UI acceptable,
   or do you want a signed installer eventually?
