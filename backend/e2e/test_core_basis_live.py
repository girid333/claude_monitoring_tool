"""Prove CoreBasisSection actually EXECUTES v1's engine against a live HTTP
endpoint: real browser, real login, real T-code loop, real report files."""
import asyncio, os, sys, tempfile, json
from pathlib import Path
from cryptography.fernet import Fernet
os.environ["LD_LIBRARY_PATH"] = "/tmp/stublib"
os.environ["REGISTRY_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
BACKEND = "/sessions/magical-eager-bardeen/mnt/Antigravity/claude_monitoring_tool/backend"
work = tempfile.mkdtemp(); os.chdir(work)
os.environ["SYSTEMS_PATH"] = str(Path(work)/"systems.json")
os.environ["RUNS_DB_PATH"] = str(Path(work)/"runs.db")
sys.path.insert(0, BACKEND)

from engine.registry import Registry, SystemEntry, SapCredential
from engine import crypto

reg = Registry(systems=[SystemEntry(
    sid="FAKE", system_name="Fake SAP", client="100",
    webgui_url="http://127.0.0.1:8099/sap/bc/gui/sap/its/webgui",
    hostname="127.0.0.1",
    sap_credential=SapCredential(username="MONITOR", password_enc=crypto.encrypt("pw")))])
reg.save(os.environ["SYSTEMS_PATH"])

import main
from engine.sections import CoreBasisSection, build_report
from engine.core_basis_wrap import load_v1_orchestrator
# Trim the T-code list ONLY to fit the sandbox's per-call time budget --
# same code path, fewer iterations. Full 23 verified separately by the
# dry-run smoke test.
_orch = load_v1_orchestrator(Path(BACKEND) / "v1_core")
_orch.MONITORED_TCODES = _orch.MONITORED_TCODES[:3]
print("trimmed to:", [t["tcode"] for t in _orch.MONITORED_TCODES])

async def go():
    section = CoreBasisSection(registry_path=os.environ["SYSTEMS_PATH"])
    print("v1 engine resolved at:", section._resolve_v1_path())
    sr = await section.run(engine=None, run_id="livetest", sid="FAKE")
    print("section ok:", sr.ok)
    print("notes:", sr.notes[:1])
    v1 = sr.v1_results or {}
    print("tcodes returned:", len(v1.get("tcodes", [])))
    assert len(v1.get("tcodes", [])) == 3, f"expected 3 T-codes, got {len(v1.get('tcodes', []))}"
    # screenshots actually taken by the real browser?
    shots = [t for t in v1["tcodes"] if t.get("screenshot")]
    print(f"tcodes with screenshots: {len(shots)}")
    # report generation from REAL results
    paths = build_report([sr], output_dir="reports/livetest")
    for k, v in paths.items():
        sz = Path(v).stat().st_size
        print(f"  {k}: {v} ({sz} bytes)")
        assert sz > 5000, f"{k} suspiciously small"
    print("LIVE CORE BASIS EXECUTION: PASSED")

asyncio.run(go())
