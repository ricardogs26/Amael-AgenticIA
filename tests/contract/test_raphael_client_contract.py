"""
Contract tests — clients/raphael_client.py ↔ docs/api/raphael-service.openapi.yaml

Valida que en modo `remote`:
  - Método HTTP, path y query params coinciden con el OpenAPI
  - Header Authorization Bearer con INTERNAL_API_SECRET
  - El parseo de respuesta respeta el schema declarado
"""
from __future__ import annotations

import httpx
import pytest


# ══════════════════════════════════════════════════════════════════════════════
# Helpers de respuesta canned (respetan los schemas del OpenAPI)
# ══════════════════════════════════════════════════════════════════════════════

def _loop_status_body() -> dict:
    return {
        "is_leader": True,
        "circuit_breaker_state": "closed",
        "maintenance_active": False,
        "last_run_ts": "2026-04-22T10:15:00Z",
        "last_run_result": "ok_clean",
        "anomalies_last_run": 0,
        "slo_targets_count": 3,
        "uptime_seconds": 3600,
    }


def _incident_body() -> dict:
    return {
        "id": 42,
        "incident_key": "amael-demo-oom::OOM_KILLED::2026-04-22T14:30:00Z",
        "issue_type": "OOM_KILLED",
        "severity": "HIGH",
        "resource_name": "amael-demo-oom-abc123",
        "owner_name": "amael-demo-oom",
        "namespace": "amael-ia",
        "action_taken": "ROLLOUT_RESTART",
        "action_result": "success",
        "confidence": 0.87,
        "diagnosis": "Memory limit exceeded under load",
        "created_at": "2026-04-22T14:30:00Z",
        "verified_at": "2026-04-22T14:40:00Z",
    }


def _slo_target_body() -> dict:
    return {
        "service": "amael-agentic-backend",
        "handler": "/api/chat",
        "target": 0.995,
        "window_seconds": 86400,
        "current_availability": 0.998,
        "burn_rate_1h": 0.1,
        "burn_rate_6h": 0.2,
        "budget_remaining_pct": 85.3,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestRaphaelReadEndpoints:
    """GET endpoints — contract shape and response parsing."""

    def test_get_loop_state(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json=_loop_status_body()))
        from clients.raphael_client import get_loop_state

        result = get_loop_state()

        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/api/sre/loop/status"
        assert captured[0].headers["authorization"].startswith("Bearer ")
        assert result["is_leader"] is True
        assert result["circuit_breaker_state"] == "closed"

    def test_get_recent_incidents_with_limit(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json=[_incident_body()]))
        from clients.raphael_client import get_recent_incidents

        result = get_recent_incidents(limit=10)

        assert len(captured) == 1
        assert captured[0].method == "GET"
        assert captured[0].url.path == "/api/sre/incidents"
        assert captured[0].url.params["limit"] == "10"
        assert isinstance(result, list)
        assert result[0]["issue_type"] == "OOM_KILLED"

    def test_get_recent_postmortems_with_limit(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json=[]))
        from clients.raphael_client import get_recent_postmortems

        result = get_recent_postmortems(limit=3)

        assert captured[0].url.path == "/api/sre/postmortems"
        assert captured[0].url.params["limit"] == "3"
        assert result == []

    def test_get_historical_success_rate_normalizes_rows(self, mock_raphael):
        """El OpenAPI devuelve {window_days, rows[]}; el client debe extraer rows."""
        body = {
            "window_days": 7,
            "rows": [
                {"issue_type": "OOM_KILLED", "action": "ROLLOUT_RESTART",
                 "total": 10, "success": 9, "success_rate": 0.9}
            ],
        }
        captured = mock_raphael(lambda req: httpx.Response(200, json=body))
        from clients.raphael_client import get_historical_success_rate

        result = get_historical_success_rate(days=7)

        assert captured[0].url.path == "/api/sre/learning/stats"
        assert captured[0].url.params["window_days"] == "7"
        assert isinstance(result, list)
        assert result[0]["success_rate"] == 0.9

    def test_get_slo_burn_rates(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json=[_slo_target_body()]))
        from clients.raphael_client import get_slo_burn_rates

        result = get_slo_burn_rates()

        assert captured[0].method == "GET"
        assert captured[0].url.path == "/api/sre/slo/status"
        assert result[0]["target"] == 0.995

    def test_load_slo_targets_uses_slo_status_endpoint(self, mock_raphael):
        """En modo remote, load_slo_targets reutiliza /api/sre/slo/status."""
        captured = mock_raphael(lambda req: httpx.Response(200, json=[_slo_target_body()]))
        from clients.raphael_client import load_slo_targets

        load_slo_targets()

        assert captured[0].url.path == "/api/sre/slo/status"


class TestRaphaelWriteEndpoints:
    """POST/DELETE endpoints — body shape and auth."""

    def test_activate_maintenance_sends_duration_minutes(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(
            200, json={"active": True, "expires_at": "2026-04-22T11:15:00Z", "reason": "test"}
        ))
        from clients.raphael_client import activate_maintenance

        activate_maintenance(minutes=60, reason="deploy window")

        import json as _json
        body = _json.loads(captured[0].content)
        assert captured[0].method == "POST"
        assert captured[0].url.path == "/api/sre/maintenance"
        assert body["duration_minutes"] == 60
        assert body["reason"] == "deploy window"

    def test_deactivate_maintenance(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json={"active": False}))
        from clients.raphael_client import deactivate_maintenance

        deactivate_maintenance()

        assert captured[0].method == "DELETE"
        assert captured[0].url.path == "/api/sre/maintenance"


class TestRaphaelAuthHeader:
    """INTERNAL_API_SECRET debe viajar en cada request como Bearer."""

    def test_bearer_header_present(self, mock_raphael):
        captured = mock_raphael(lambda req: httpx.Response(200, json=_loop_status_body()))
        from clients.raphael_client import get_loop_state

        get_loop_state()

        auth = captured[0].headers["authorization"]
        assert auth.startswith("Bearer ")
        assert len(auth) > len("Bearer ") + 10  # hay token real, no vacío
