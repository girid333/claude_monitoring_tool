"""Health Check Dashboard (D-050): ingest SAP Automated Health Check (CAS/AHC)
workbooks, triage every flagged check with evidence, serve a dashboard.

Self-contained, additive module: own storage dir, one include_router() line,
no imports from engine/ or v1_core/ (isolation per the post-D-044 convention).
"""
