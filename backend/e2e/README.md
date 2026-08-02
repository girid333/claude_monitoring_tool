# End-to-end tests (D-030)

Real-browser tests that drive the actual UI and the actual engine — added
after unit/TestClient-level checks repeatedly passed while the UI was
still broken for the user. TestClient proves endpoints answer; only these
prove a person clicking a button gets a result.

## What each test covers

| File | Proves |
|---|---|
| `test_ui_systems_runs.py` | Add System through the real modal; Runs-page launcher: select a system chip, Execute enables, live status text appears, an unreachable SAP host produces a VISIBLE red error panel (not silence); no client-side JS errors |
| `test_ui_landscape.py` | Landscape: card select reveals Core BASIS checkbox, Run sweep fires POST /api/run, Dynamic Island appears immediately, live status text, run always ends in a visible outcome |
| `test_core_basis_live.py` + `fake_sap_server.py` | The whole Core BASIS pipeline for real: v1 engine loaded from the vendored `v1_core` copy, real Chromium logs into a fake WebGUI, T-code navigation executes, screenshots are captured, and real Word+PDF reports are generated from the results |

## Running them

Backend and frontend must both be up, and CORS must allow the origin you
browse from (`CORS_ORIGINS=*` for local testing — compose already sets this):

```bash
# terminal 1
cd backend && CORS_ORIGINS='*' REGISTRY_ENCRYPTION_KEY=$(python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  uvicorn main:app --host 127.0.0.1 --port 8000

# terminal 2
cd frontend && npm run build && npm start

# terminal 3
python backend/e2e/test_ui_systems_runs.py
python backend/e2e/test_ui_landscape.py

# Core BASIS engine test (starts its own fake SAP on :8099)
python backend/e2e/fake_sap_server.py &
python backend/e2e/test_core_basis_live.py
```

Note: `test_core_basis_live.py` trims `MONITORED_TCODES` to 3 entries so it
finishes quickly — same code path, fewer iterations. `_dryrun_core_basis.py`
covers the full 23.

If Chromium fails to start with `libXdamage.so.1: cannot open shared object
file`, install the browser deps (`playwright install --with-deps chromium`).
