# Claude Monitoring Tool (v2)

SAP WebGUI record-and-replay monitoring agent. Extends the existing v1 BASIS
monitoring tool (`../sap_monitoring`) with a generic flow engine —
CBTA/Tricentis-style, but over WebGUI.

**Read `SKILL.md` first.** It is the project's memory: 17 logged decisions,
the code-verified v1 reference (§6), the phase plan, and open questions.
Nothing in this repo contradicts it; if you need to, log a superseding
decision there.

## Layout

```
SKILL.md                     ← project memory (single source of truth)
design/mockup.html           ← approved UI design reference (D-016)
backend/
  engine/
    schema.py                ← Flow/Step/Target/Check models (D-008, D-012)
    engine.py                ← ReplayEngine step executor (D-010: stateless)
    sync.py                  ← wait_ready barrier (replaces v1 sleep-stack)
    probe.py                 ← capability probe (D-005) → resolver mode
    resolvers/
      base.py                ← Resolver contract + confidence semantics (D-003)
      scripting.py           ← findById over WebGUI (D-001)
      dom.py                 ← ranked-candidate resolution (D-012)
    login/
      base.py                ← plugin contract + re-auth heuristic (D-007)
      basic.py               ← v1-proven JS-injected login (§6.8 port)
      saml.py                ← skeleton, blocked on reference SSO system
  flows/sm37_cancelled_jobs.json   ← Phase 1 reference flow
  datasets/daily.json
```

## Phase 1 status (from SKILL.md phase plan)

Done in this scaffold: schema, engine core, both resolver skeletons
(scripting complete-ish, DOM tiers 1–2), sync, probe, basic login,
SM37 reference flow.

Remaining before Phase 1 is DONE (needs live systems — see OQ-2):
- [ ] Verify scripting-session JS entry points on a real WebGUI
      (`resolvers/scripting.py` `_SESSION_JS`) and fix scripting IDs in the
      SM37 flow against the real screen
- [ ] DOM ALV table read (`resolvers/dom.py: read_table`) — the hard part,
      SM37 chosen to force it
- [ ] SAML login plugin against the reference SSO system (D-015)
- [ ] Credential provider implementation — blocked on OQ-8
      (encryption-at-rest decision)
- [ ] Runner CLI: `python -m engine.run --system <sid> --flow <id> --dataset <id>`
- [ ] Then: ST22 + SM12 flows

## Done at the code level (D-020) — pending live-system verification

Everything below is built and passes mocked/dry-run verification, but has
never run against a real SAP system (blocked on OQ-2's reference SIDs, and
Cowork's sandbox network can't reach candidate host `nwrhel9.techlabhome.in`
either — see `live_probe_check.py` below):

- v1 integration (D-017/D-019): `CoreBasisSection` wraps `../../sap_monitoring`'s
  orchestrator, monkeypatching RFC so it never gates the run. `main.py`'s
  `/api/run` accepts `include_core_basis: true`.
- Report composer (D-014/D-020): `report_composer.py` renders Core BASIS +
  Functional Sanity into one Word+PDF with severity chips, auto-generated at
  the end of every run, downloadable via `/api/reports`.
- Recorder (Phase 2, D-020): `engine/recorder_service.py` + `/api/recorder/*`
  + the Recorder page. Opens a HEADED browser — only usable when the backend
  runs on a machine with a display.
- Frontend (D-016/D-020): sidebar navigation (`Shell.tsx` — didn't exist
  before, pages were unreachable), Landscape/Runs/Recorder/Reports pages,
  `tsconfig.json` + TypeScript devDependencies (the frontend had never been
  built once before this). `next build` type-checks clean.

## Live-system checklist (run this yourself — see SKILL.md D-020e)

`backend/live_probe_check.py` is a standalone script (not part of the
FastAPI app) for exactly this. Run it from a machine that can reach your
target system:

```
SAP_<SID>_USER=... SAP_<SID>_PASSWORD=... python live_probe_check.py \
    --sid <SID> --url <webgui_url> --client <client>
```

It logs in, runs the capability probe, and screenshots the result — telling
you whether the system is scripting-enabled (candidate for the recorder,
D-004) or DOM-only. Once you know that:

- [ ] Verify scripting-session JS entry points on a real WebGUI
      (`resolvers/scripting.py` `_SESSION_JS`) and fix scripting IDs in the
      SM37 flow against the real screen
- [ ] DOM ALV table read (`resolvers/dom.py: read_table`) — the hard part,
      SM37 chosen to force it
- [ ] SAML login plugin against a reference SSO system (D-015) — still need
      a second reference system for this
- [ ] Credential provider implementation — blocked on OQ-8
      (encryption-at-rest decision)
- [ ] Try an actual recording session via the Recorder page against a
      scripting-enabled system
- [ ] Then: ST22 + SM12 flows

## Not in scope here yet

- "Recorded flows" / "Datasets" library pages from the mockup's sidebar
  (not part of the three priorities requested in D-020's session)

## Working agreement

Every session: append what happened to SKILL.md's Session Log. Decisions get
D-numbers. The mockup and token system in D-016 are the design authority.
