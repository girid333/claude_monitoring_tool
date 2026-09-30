"""Health dashboard HTTP API, through a real FastAPI app with the router
mounted exactly as main.py will mount it."""

import sys as _sys
from pathlib import Path as _P
_sys.path.insert(0, str(_P(__file__).resolve().parents[1]))


import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from make_ahc_fixture import build  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALTH_STORAGE_DIR", str(tmp_path))
    import health.router as R
    R._store = None
    app = FastAPI()
    app.include_router(R.router)
    return TestClient(app)


def upload(client, data=None, name="ahc.xls"):
    return client.post("/api/health/reports",
                       files={"file": (name, data if data is not None else build().encode(), "application/vnd.ms-excel")})


def test_import_list_get_latest_delete(client):
    assert client.get("/api/health/reports").json() == []
    assert client.get("/api/health/reports/latest").json() is None
    r = upload(client)
    assert r.status_code == 201, r.text
    rep = r.json()
    assert rep["system"]["sid"] == "QAS" and rep["summary"]["act"] == 7
    lst = client.get("/api/health/reports").json()
    assert [x["id"] for x in lst] == [rep["id"]]
    assert client.get("/api/health/reports/latest").json()["id"] == rep["id"]
    assert client.get(f"/api/health/reports/{rep['id']}").json()["filename"] == "ahc.xls"
    assert client.delete(f"/api/health/reports/{rep['id']}").status_code == 204
    assert client.get(f"/api/health/reports/{rep['id']}").status_code == 404


def test_newest_report_date_is_latest(client):
    a = upload(client).json()
    newer = build().replace("15/03/2026", "16/03/2026")
    b = upload(client, newer.encode()).json()
    assert client.get("/api/health/reports/latest").json()["id"] == b["id"] != a["id"]


@pytest.mark.parametrize("payload,needle", [
    (b"", "empty"),
    (b"not a workbook", "Not an SAP health check"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, "XML Spreadsheet 2003"),
    (b'<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"></Workbook>', "no worksheets"),
])
def test_bad_uploads_get_actionable_errors(client, payload, needle):
    r = upload(client, payload)
    assert r.status_code in (400, 422)
    assert needle.lower() in r.json()["detail"].lower()


def test_ids_cannot_traverse(client):
    for rid in ["../../etc/passwd", "..%2F..%2Fx", "ABCDEF123456", "x" * 12]:
        assert client.get(f"/api/health/reports/{rid}").status_code == 404
        assert client.delete(f"/api/health/reports/{rid}").status_code == 404


def test_corrupt_store_file_does_not_break_list(client, tmp_path):
    upload(client)
    (tmp_path / "deadbeef0000.json").write_text("{broken")
    assert len(client.get("/api/health/reports").json()) == 1
