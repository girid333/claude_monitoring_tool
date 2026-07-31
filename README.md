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

## Not in scope here yet

- v1 integration (D-017: wrap `../sap_monitoring` orchestrator as the
  "Core BASIS" section provider) — Phase 5
- Recorder — Phase 2, after replay is proven on two heterogeneous systems
- Frontend — build against `design/mockup.html` and the D-016 token system;
  requires SSE run events + per-system status endpoints

## Working agreement

Every session: append what happened to SKILL.md's Session Log. Decisions get
D-numbers. The mockup and token system in D-016 are the design authority.
