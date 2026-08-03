"""D-045: RFC-over-SOAP capability.

Why this exists (and why NOT pyrfc): the classic Python RFC client
(`pyrfc`) needs SAP's NW RFC SDK — proprietary binaries that are
deliberately absent from the backend container image (see
backend/Dockerfile, D-022). SAP's ICM, however, exposes every
RFC-enabled function module over plain HTTP at `/sap/bc/soap/rfc`, using
the same host and the same credentials already stored (encrypted) in the
system registry for WebGUI login.

That makes this path uniquely well-suited to this deployment:
  - no SDK, no binary dependencies, no extra container layers
  - no Windows workstation, no COM, no bridge agent, no pairing
  - fully headless => runs unattended and on a schedule in the container
  - reuses `SystemEntry.webgui_url` + `sap_credential` verbatim, so an
    already-configured system needs zero new configuration

Scope honesty: this covers anything exposed as an RFC-enabled function
module (all the standard BASIS interfaces, plus any custom Z_* FM marked
remote-enabled). It is NOT a replacement for screen record/replay on
arbitrary custom transactions that have no API — the WebGUI engine
remains the answer there.
"""
