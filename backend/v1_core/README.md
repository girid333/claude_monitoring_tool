# v1_core — vendored copy of v1's engine (D-028)

Verbatim, UNMODIFIED copies of the three files CoreBasisSection needs from
`sap_monitoring/backend/`:

- `backend/orchestrator.py`
- `backend/sap_automator.py`
- `backend/sap_rfc_client.py`

Why: the Docker image's build context is `claude_monitoring_tool/backend`
only — the sibling `sap_monitoring` repo is never inside the container, so
without this copy Core BASIS cannot run in any containerized deployment.

Resolution order (engine/sections.py CoreBasisSection):
1. `V1_PATH` env var (explicit override, e.g. a volume-mounted live v1)
2. `../../sap_monitoring` relative to CWD (the dev layout — preferred when
   present so a locally-updated v1 wins over this snapshot)
3. this vendored copy (the container default)

The "wrap not edit" rule (D-017/D-019) still applies: NEVER modify these
files. If v1 changes upstream, re-copy them verbatim.
