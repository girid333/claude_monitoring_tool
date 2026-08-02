"""Test launcher: trims MONITORED_TCODES so an E2E run fits the sandbox's
per-call time budget, then serves the real app unmodified."""
import os, sys, types
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
sys.path.insert(0, BACKEND)
LIMIT = int(os.environ.get("CB_TCODE_LIMIT", "0"))
if LIMIT:
    stub = types.ModuleType("sap_rfc_client")
    class SAPRfcClient:
        def __init__(self, *a, **k): pass
    stub.SAPRfcClient = SAPRfcClient
    sys.modules.setdefault("sap_rfc_client", stub)
    sys.path.insert(0, os.path.join(BACKEND, "v1_core", "backend"))
    import orchestrator
    orchestrator.MONITORED_TCODES[:] = orchestrator.MONITORED_TCODES[:LIMIT]
    print(f"[test] trimmed to {[t['tcode'] for t in orchestrator.MONITORED_TCODES]}", flush=True)
import uvicorn
uvicorn.run("main:app", host="127.0.0.1", port=8000, log_level="warning")
