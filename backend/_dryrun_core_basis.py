"""Manual smoke test for CoreBasisSection (D-017) — not wired into any test
runner, run directly: `python _dryrun_core_basis.py` from backend/.

Exercises CoreBasisSection.run() end-to-end with SAPAutomator.take_screenshots
and RFC connectivity mocked out, since no environment so far (this sandbox,
and per README/SKILL.md OQ-2, no reference SIDs yet either) has a live SAP
system, Playwright browser binaries, or pyrfc/nwrfcsdk all at once. Confirms:
  - v1's orchestrator + sap_automator import cleanly via the sys.path shim
    without modifying any sap_monitoring file
  - the pyrfc-missing stub lets `import orchestrator` succeed
  - ResilientRfcClient's connect() never raises the v1 abort, and
    availability_log correctly reports "not available" for this run
  - CoreBasisSection.run() returns a populated SectionResult without
    exceptions, with v1_results carrying the (mocked) tcode data through
"""
import asyncio
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from engine.core_basis_wrap import ResilientRfcClient, build_v1_config, load_v1_orchestrator  # noqa: E402
from engine.sections import CoreBasisSection  # noqa: E402


class _FakeSysEntry:
    sid = "Q4H"
    webgui_url = "https://q4h.example.internal:44300/sap/bc/gui/sap/its/webgui"
    client = "100"


class _FakeRegistry:
    def get(self, sid):
        assert sid == "Q4H"
        return _FakeSysEntry()


class _FakeCreds:
    def get(self, sid):
        return {"user": "MONITOR", "password": "not-a-real-secret"}


async def main() -> None:
    # 1. import shim works and doesn't touch v1 files
    orch_mod = load_v1_orchestrator("../../sap_monitoring")
    assert orch_mod.SAPRfcClient is ResilientRfcClient, "monkeypatch didn't take"
    print("[ok] v1 orchestrator imported; SAPRfcClient patched to ResilientRfcClient")

    # 2. config builder
    cfg = build_v1_config(_FakeSysEntry(), {"user": "MONITOR", "password": "x"})
    assert cfg["hostname"] == "q4h.example.internal"
    assert cfg["sap_client"] == "100"
    print("[ok] build_v1_config:", {k: v for k, v in cfg.items() if k != "sap_password"})

    # 3. ResilientRfcClient never raises v1's abort even when unavailable
    c = ResilientRfcClient(ashost="nope", sysnr="00", client="100", user="u", passwd="p")
    assert c.connect() is True  # gate always passes
    resp = c.execute_tcode_check("SM50")
    assert resp["status"] == "error" and "unavailable" in resp["message"]
    print("[ok] ResilientRfcClient.connect() always True; execute_tcode_check degrades honestly")

    # 4. full CoreBasisSection.run(), with SAPAutomator + Registry/Creds mocked
    fake_screenshot_map = {tc["tcode"]: f"/tmp/{tc['tcode']}.png"
                            for tc in orch_mod.MONITORED_TCODES}

    async def fake_take_screenshots(self, tcode_list, status_callback=None):
        return fake_screenshot_map

    with mock.patch("engine.registry.Registry.load", return_value=_FakeRegistry()), \
         mock.patch("engine.registry.EnvCredentialProvider.get",
                     lambda self, sid: {"user": "MONITOR", "password": "x"}), \
         mock.patch.object(orch_mod.SAPAutomator, "take_screenshots",
                            fake_take_screenshots):
        section = CoreBasisSection()
        result = await section.run(engine=None, run_id="dryrun1", sid="Q4H")

    assert result.v1_results is not None, "v1_results should be populated"
    assert len(result.v1_results["tcodes"]) == len(orch_mod.MONITORED_TCODES)
    assert any("RFC unavailable" in n or "RFC enrichment unavailable" in n
               for n in result.notes), result.notes
    print(f"[ok] CoreBasisSection.run() -> ok={result.ok}, "
          f"{len(result.v1_results['tcodes'])} tcodes, notes={result.notes}")
    print("ALL DRY-RUN CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
