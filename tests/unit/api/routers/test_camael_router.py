"""Integration tests del router /api/camael/* — endpoints del camael-service."""
from __future__ import annotations

import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    # Forzar secret antes de importar el router
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret-" + "x" * 20)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")

    from interfaces.api.routers.camael import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


AUTH_HEADER = {"Authorization": "Bearer " + ("test-secret-" + "x" * 20)}


def test_handoff_requires_auth(client):
    resp = client.post("/api/camael/handoff", json={"incident_key": "x"})
    assert resp.status_code == 401


def test_handoff_happy_path(client, monkeypatch):
    """POST /handoff acepta y delega a agents.devops.agent.handle_handoff."""
    calls = []

    async def fake_handle(payload):
        calls.append(payload)
        return {"pr_id": "PR-42", "rfc_number": "CHG0042"}

    import sys, types
    fake_mod = types.ModuleType("agents.devops.agent")
    fake_mod.handle_handoff = fake_handle
    monkeypatch.setitem(sys.modules, "agents.devops.agent", fake_mod)

    body = {
        "incident_key":    "oom:demo:amael-ia",
        "issue_type":      "OOM_KILLED",
        "severity":        "HIGH",
        "namespace":       "amael-ia",
        "deployment_name": "demo-oom",
        "reason":          "memory limit exceeded",
        "raphael_action":  "ROLLOUT_RESTART",
        "triggered_at":    "2026-04-23T10:00:00Z",
        "context":         {},
    }
    resp = client.post("/api/camael/handoff", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 202
    data = resp.json()
    assert data["accepted"] is True
    assert data["pr_id"] == "PR-42"
    assert calls[0]["incident_key"] == "oom:demo:amael-ia"


def test_handoff_rejects_unsupported_issue(client, monkeypatch):
    """Si agent.handle_handoff retorna None → 400."""
    async def fake_handle(payload):
        return None  # no soportado

    import sys, types
    fake_mod = types.ModuleType("agents.devops.agent")
    fake_mod.handle_handoff = fake_handle
    monkeypatch.setitem(sys.modules, "agents.devops.agent", fake_mod)

    body = {
        "incident_key":    "unknown:x:y",
        "issue_type":      "WEIRD",
        "severity":        "LOW",
        "namespace":       "amael-ia",
        "deployment_name": "x",
        "reason":          "?",
        "raphael_action":  "NOTIFY_HUMAN",
        "triggered_at":    "2026-04-23T10:00:00Z",
        "context":         {},
    }
    resp = client.post("/api/camael/handoff", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 400


def test_update_rfc_closed(client, monkeypatch):
    calls = []

    class FakeSn:
        def is_configured(self):
            return True
        async def close_rfc(self, sys_id, message):
            calls.append(("close", sys_id, message))
        async def fail_rfc(self, sys_id, message):
            calls.append(("fail", sys_id, message))

    import sys as _sys, types
    m = types.ModuleType("agents.devops.servicenow_client")
    fake_sn = FakeSn()
    m.is_configured = fake_sn.is_configured
    m.close_rfc = fake_sn.close_rfc
    m.fail_rfc = fake_sn.fail_rfc
    monkeypatch.setitem(_sys.modules, "agents.devops.servicenow_client", m)

    body = {
        "result":     "closed",
        "message":    "Healthy post-deploy",
        "deployment": "demo-oom",
        "namespace":  "amael-ia",
    }
    resp = client.patch("/api/camael/rfc/SN-123", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert calls == [("close", "SN-123", "Healthy post-deploy")]


def test_update_rfc_review(client, monkeypatch):
    calls = []

    class FakeSn:
        def is_configured(self):
            return True
        async def close_rfc(self, sys_id, message):
            calls.append(("close", sys_id, message))
        async def fail_rfc(self, sys_id, message):
            calls.append(("fail", sys_id, message))

    import sys as _sys, types
    m = types.ModuleType("agents.devops.servicenow_client")
    fake_sn = FakeSn()
    m.is_configured = fake_sn.is_configured
    m.close_rfc = fake_sn.close_rfc
    m.fail_rfc = fake_sn.fail_rfc
    monkeypatch.setitem(_sys.modules, "agents.devops.servicenow_client", m)

    body = {"result": "review", "message": "Failed verification"}
    resp = client.patch("/api/camael/rfc/SN-456", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert calls == [("fail", "SN-456", "Failed verification")]


def test_update_rfc_invalid_result(client):
    body = {"result": "pancake", "message": "..."}
    resp = client.patch("/api/camael/rfc/SN-789", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 422  # Pydantic validation error
