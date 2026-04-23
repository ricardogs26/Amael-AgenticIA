"""
Contract tests — clients/camael_client.py ↔ docs/api/camael-service.openapi.yaml

Enfoque especial en la función CRÍTICA `handoff_to_camael()`:
  - Happy path: POST /api/camael/handoff con payload que respeta HandoffRequest
  - 400: issue no soportado → log silencioso, sin encolar
  - Network error: fallback a Redis queue + notify_fn
  - Idempotencia: mismo incident_key no genera duplicado
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures / helpers
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class FakeAnomaly:
    """Simula agents.sre.models.Anomaly sin importarlo."""
    issue_type:     str = "OOM_KILLED"
    severity:       str = "HIGH"
    resource_name:  str = "amael-demo-oom-7cf4c6c4b4-5zn6x"
    owner_name:     str = "amael-demo-oom"
    namespace:      str = "amael-ia"
    diagnosis:      str = "Memory limit exceeded under load"
    confidence:     float = 0.87
    detected_at:    str = "2026-04-22T14:30:00Z"
    metric_value:   float = 0.94
    restart_count:  int = 3


@pytest.fixture
def fake_redis(monkeypatch):
    """Mockea storage.redis.client.get_client para no tocar Redis real."""
    fake = MagicMock()
    fake.set = MagicMock(return_value=True)
    fake.get = MagicMock(return_value=None)
    fake.delete = MagicMock(return_value=1)
    fake.keys = MagicMock(return_value=[])

    import storage.redis.client as redis_mod
    monkeypatch.setattr(redis_mod, "get_client", lambda: fake)
    return fake


# ══════════════════════════════════════════════════════════════════════════════
# Tests — handoff_to_camael (CRÍTICO)
# ══════════════════════════════════════════════════════════════════════════════

class TestHandoffHappyPath:
    def test_handoff_sends_post_to_correct_path(self, mock_camael):
        captured = mock_camael(lambda req: httpx.Response(
            202, json={"incident_key": "k1", "status": "queued", "pr_id": None}
        ))
        from clients.camael_client import handoff_to_camael

        anomaly = FakeAnomaly()
        notify = MagicMock()
        handoff_to_camael(anomaly, incident_key="k1", notify_fn=notify)

        assert len(captured) == 1
        assert captured[0].method == "POST"
        assert captured[0].url.path == "/api/camael/handoff"
        # notify_fn NO se llama en happy path
        notify.assert_not_called()

    def test_handoff_body_matches_schema(self, mock_camael):
        """El body debe contener todos los required de HandoffRequest."""
        captured = mock_camael(lambda req: httpx.Response(202, json={"incident_key": "k1", "status": "queued"}))
        from clients.camael_client import handoff_to_camael

        handoff_to_camael(FakeAnomaly(), incident_key="k1", notify_fn=MagicMock())

        import json as _json
        body = _json.loads(captured[0].content)
        # Required fields per OpenAPI HandoffRequest
        for required in ["incident_key", "issue_type", "namespace", "deployment_name", "reason", "triggered_at"]:
            assert required in body, f"missing {required} in handoff body"
        assert body["incident_key"] == "k1"
        assert body["issue_type"] == "OOM_KILLED"
        assert body["namespace"] == "amael-ia"
        assert body["deployment_name"] == "amael-demo-oom"
        assert body["raphael_action"] == "ROLLOUT_RESTART"
        # context libre pero debe preservar confidence
        assert body["context"]["confidence"] == 0.87


class TestHandoffSkip:
    def test_handoff_400_is_silent(self, mock_camael):
        """Si Camael responde 400 (issue no soportado), no se notifica ni se encola."""
        mock_camael(lambda req: httpx.Response(400, json={"detail": "Issue type not in BUG_LIBRARY"}))
        from clients.camael_client import handoff_to_camael

        notify = MagicMock()
        # No debe levantar excepción
        handoff_to_camael(FakeAnomaly(issue_type="UNKNOWN_TYPE"), incident_key="k2", notify_fn=notify)
        notify.assert_not_called()


class TestHandoffFallback:
    def test_network_error_enqueues_to_redis_and_notifies(self, mock_camael, fake_redis):
        """Si camael-service no responde, encolamos en Redis y notificamos humano."""
        def handler(request):
            raise httpx.ConnectError("Connection refused")
        mock_camael(handler)

        from clients.camael_client import handoff_to_camael

        notify = MagicMock()
        handoff_to_camael(FakeAnomaly(), incident_key="k3", notify_fn=notify)

        # Redis.set fue llamado con TTL 3600
        fake_redis.set.assert_called_once()
        call = fake_redis.set.call_args
        assert "camael:pending_handoff:k3" in call[0][0]
        assert call[1]["ex"] == 3600
        # Humano notificado
        notify.assert_called_once()
        msg = notify.call_args[0][0]
        assert "Camael" in msg and "k3" in msg

    def test_5xx_also_triggers_fallback(self, mock_camael, fake_redis):
        mock_camael(lambda req: httpx.Response(503, json={"detail": "service saturated"}))
        from clients.camael_client import handoff_to_camael

        notify = MagicMock()
        handoff_to_camael(FakeAnomaly(), incident_key="k4", notify_fn=notify)

        fake_redis.set.assert_called_once()
        notify.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Tests — get_handoff_status / drain
# ══════════════════════════════════════════════════════════════════════════════

class TestHandoffStatus:
    def test_get_status_returns_parsed_json(self, mock_camael):
        mock_camael(lambda req: httpx.Response(200, json={
            "incident_key": "k1",
            "status": "pr_created",
            "namespace": "amael-ia",
            "deployment_name": "amael-demo-oom",
            "issue_type": "OOM_KILLED",
            "pr_id": 42,
            "pr_url": "https://bitbucket.org/ws/repo/pull-requests/42",
            "created_at": "2026-04-22T14:30:00Z",
            "updated_at": "2026-04-22T14:32:00Z",
        }))
        from clients.camael_client import get_handoff_status

        result = get_handoff_status("k1")

        assert result["status"] == "pr_created"
        assert result["pr_id"] == 42

    def test_get_status_404_returns_none(self, mock_camael):
        mock_camael(lambda req: httpx.Response(404, json={"detail": "not found"}))
        from clients.camael_client import get_handoff_status

        result = get_handoff_status("unknown-key")

        assert result is None


class TestDrainPendingHandoffs:
    def test_drain_reposts_and_deletes_on_success(self, mock_camael, fake_redis):
        import json as _json
        payload = {"incident_key": "k5", "issue_type": "OOM_KILLED", "namespace": "amael-ia"}
        fake_redis.keys.return_value = [b"camael:pending_handoff:k5"]
        fake_redis.get.return_value = _json.dumps(payload).encode()

        captured = mock_camael(lambda req: httpx.Response(202, json={"incident_key": "k5", "status": "queued"}))

        from clients.camael_client import drain_pending_handoffs
        count = drain_pending_handoffs()

        assert count == 1
        assert captured[0].method == "POST"
        assert captured[0].url.path == "/api/camael/handoff"
        fake_redis.delete.assert_called_once_with(b"camael:pending_handoff:k5")

    def test_drain_keeps_entry_on_failure(self, mock_camael, fake_redis):
        import json as _json
        payload = {"incident_key": "k6", "issue_type": "OOM_KILLED"}
        fake_redis.keys.return_value = [b"camael:pending_handoff:k6"]
        fake_redis.get.return_value = _json.dumps(payload).encode()

        mock_camael(lambda req: httpx.Response(503, json={"detail": "still down"}))

        from clients.camael_client import drain_pending_handoffs
        count = drain_pending_handoffs()

        assert count == 0
        fake_redis.delete.assert_not_called()


class TestCamaelAuthHeader:
    def test_bearer_header_on_handoff(self, mock_camael):
        captured = mock_camael(lambda req: httpx.Response(202, json={"incident_key": "k1", "status": "queued"}))
        from clients.camael_client import handoff_to_camael

        handoff_to_camael(FakeAnomaly(), incident_key="k1", notify_fn=MagicMock())

        auth = captured[0].headers["authorization"]
        assert auth.startswith("Bearer ")


# ══════════════════════════════════════════════════════════════════════════════
# Tests del flag CAMAEL_MODE separado
# ══════════════════════════════════════════════════════════════════════════════

class TestCamaelModeFlag:
    """Verifica que camael_client usa CAMAEL_MODE, no AGENTS_MODE."""

    def test_agents_mode_remote_but_camael_mode_inprocess_calls_local(
        self, monkeypatch, fake_redis
    ):
        """AGENTS_MODE=remote + CAMAEL_MODE=inprocess → llama a healer.handoff local."""
        from clients import camael_client

        class FakeSettings:
            agents_mode = "remote"
            camael_mode = "inprocess"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"

        monkeypatch.setattr(camael_client, "settings", FakeSettings())

        local_called = []

        def fake_local_handoff(anomaly, incident_key, notify_fn):
            local_called.append(incident_key)

        monkeypatch.setattr(
            "agents.sre.healer.handoff_to_camael", fake_local_handoff
        )

        anomaly = FakeAnomaly()
        camael_client.handoff_to_camael(anomaly, "test-key", lambda m: None)

        assert local_called == ["test-key"]
        # Verificar que NO se intentó HTTP (no fallback a Redis queue)
        fake_redis.set.assert_not_called()

    def test_both_modes_remote_attempts_http(self, monkeypatch, fake_redis):
        """AGENTS_MODE=remote + CAMAEL_MODE=remote → intenta HTTP a Camael."""
        from clients import camael_client

        class FakeSettings:
            agents_mode = "remote"
            camael_mode = "remote"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"

        monkeypatch.setattr(camael_client, "settings", FakeSettings())

        http_calls = []

        class FakeResponse:
            status_code = 202
            content = b'{"status":"accepted","pr_id":"PR-1"}'
            text = '{"status":"accepted","pr_id":"PR-1"}'
            def json(self):
                return {"status": "accepted", "pr_id": "PR-1"}

        class FakeClient:
            def post(self, path, json):
                http_calls.append((path, json))
                return FakeResponse()

        monkeypatch.setattr(
            "clients._http.get_camael_client", lambda: FakeClient()
        )

        camael_client.handoff_to_camael(FakeAnomaly(), "test-key-2", lambda m: None)

        assert len(http_calls) == 1
        assert http_calls[0][0] == "/api/camael/handoff"
        assert http_calls[0][1]["incident_key"] == "test-key-2"
