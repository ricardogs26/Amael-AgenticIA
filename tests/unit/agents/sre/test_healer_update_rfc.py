"""Verifica que healer._update_rfc_state delega a camael_client (no importa
servicenow_client directo — rompe coupling cross-agent)."""
from __future__ import annotations

import asyncio
import pytest


def test_update_rfc_state_calls_camael_client_closed(monkeypatch):
    from agents.sre import healer

    calls = []

    async def fake_update_rfc(sys_id, result, message, **kwargs):
        calls.append({
            "sys_id": sys_id,
            "result": result,
            "message": message,
            **kwargs,
        })

    monkeypatch.setattr(
        "clients.camael_client.update_rfc", fake_update_rfc
    )

    asyncio.run(healer._update_rfc_state(
        rfc_info={"sys_id": "SN-123", "number": "CHG0001"},
        deployment_name="demo-oom",
        namespace="amael-ia",
        success=True,
        reason="",
    ))

    assert len(calls) == 1
    assert calls[0]["sys_id"] == "SN-123"
    assert calls[0]["result"] == "closed"
    assert calls[0]["deployment"] == "demo-oom"
    assert calls[0]["namespace"] == "amael-ia"
    assert "exitoso" in calls[0]["message"].lower() or "healthy" in calls[0]["message"].lower()


def test_update_rfc_state_calls_camael_client_review_on_failure(monkeypatch):
    from agents.sre import healer

    calls = []

    async def fake_update_rfc(sys_id, result, message, **kwargs):
        calls.append({"sys_id": sys_id, "result": result, **kwargs})

    monkeypatch.setattr(
        "clients.camael_client.update_rfc", fake_update_rfc
    )

    asyncio.run(healer._update_rfc_state(
        rfc_info={"sys_id": "SN-456", "number": "CHG0002"},
        deployment_name="demo-crashloop",
        namespace="amael-ia",
        success=False,
        reason="Pod still crashing after 5min",
    ))

    assert calls == [{
        "sys_id": "SN-456",
        "result": "review",
        "deployment": "demo-crashloop",
        "namespace": "amael-ia",
    }]


def test_update_rfc_state_skips_when_no_sys_id(monkeypatch):
    from agents.sre import healer

    calls = []

    async def fake_update_rfc(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(
        "clients.camael_client.update_rfc", fake_update_rfc
    )

    asyncio.run(healer._update_rfc_state(
        rfc_info={"number": "CHG-no-sysid"},  # falta sys_id
        deployment_name="demo",
        namespace="amael-ia",
        success=True,
        reason="",
    ))

    assert calls == []


def test_update_rfc_state_does_not_import_agents_devops(monkeypatch):
    """healer ya NO debe importar agents.devops.servicenow_client directamente."""
    import importlib
    import agents.sre.healer as healer_mod
    source = open(healer_mod.__file__).read()
    assert "from agents.devops import servicenow_client" not in source
    assert "from agents.devops.servicenow_client" not in source
