"""骨架验证：healthz 与 version。"""
from fastapi.testclient import TestClient
from fd_shilun_api import __version__
from fd_shilun_api.main import app

client = TestClient(app)

def test_healthz() -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["service"] == "fd-shilun-api"
