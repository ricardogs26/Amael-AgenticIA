"""Tests del dispatcher /sre (agents.sre.commands) y módulos portados del
legacy k8s-agent: autonomy (modos P8-B) y approvals (cola P7-S3)."""
from __future__ import annotations

import json

import pytest


class FakeRedis:
    """Redis mínimo en memoria para autonomy/approvals."""

    def __init__(self):
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def setex(self, key, ttl, value):
        self.data[key] = value
        return True

    def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)

    def keys(self, pattern="*"):
        prefix = pattern.rstrip("*")
        return [k for k in self.data if k.startswith(prefix)]

    def exists(self, key):
        return 1 if key in self.data else 0


@pytest.fixture()
def fake_redis(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr("storage.redis.client.get_client", lambda: r)
    return r


# ── autonomy ──────────────────────────────────────────────────────────────────

def test_get_agent_mode_default_standard(fake_redis):
    from agents.sre.autonomy import get_agent_mode
    assert get_agent_mode() == "standard"


def test_set_and_get_agent_mode(fake_redis):
    from agents.sre.autonomy import get_agent_mode, set_agent_mode
    assert set_agent_mode("observe") is True
    assert get_agent_mode() == "observe"


def test_set_agent_mode_invalid(fake_redis):
    from agents.sre.autonomy import set_agent_mode
    assert set_agent_mode("yolo") is False


@pytest.mark.parametrize(
    ("mode", "action", "confidence", "expected"),
    [
        ("observe",      "ROLLOUT_RESTART", 0.99, "OBSERVE_ONLY"),
        ("observe",      "NOTIFY_HUMAN",    0.99, "OBSERVE_ONLY"),
        ("conservative", "ROLLOUT_RESTART", 0.95, "ROLLOUT_RESTART"),
        ("conservative", "ROLLOUT_RESTART", 0.80, "OBSERVE_ONLY"),
        ("conservative", "SCALE_UP",        0.99, "OBSERVE_ONLY"),
        ("standard",     "ROLLOUT_RESTART", 0.80, "ROLLOUT_RESTART"),
        ("standard",     "SCALE_UP",        0.99, "NOTIFY_HUMAN"),
        ("full",         "SCALE_UP",        0.80, "SCALE_UP"),
    ],
)
def test_apply_mode_to_action(fake_redis, mode, action, confidence, expected):
    from agents.sre.autonomy import apply_mode_to_action, set_agent_mode
    set_agent_mode(mode)
    assert apply_mode_to_action(action, confidence) == expected


# ── approvals ─────────────────────────────────────────────────────────────────

def test_approval_roundtrip(fake_redis):
    from agents.sre.approvals import (
        get_pending_approval,
        list_pending_approvals,
        remove_approval,
        save_pending_approval,
    )

    save_pending_approval(
        "inc-1", "SCALE_UP", "demo-oom", "amael-ia", "HIGH_MEMORY",
        root_cause="memory leak", confidence=0.9,
    )
    pending = get_pending_approval()
    assert pending is not None
    assert pending["action"] == "SCALE_UP"
    assert pending["resource"] == "demo-oom"
    assert len(list_pending_approvals()) == 1

    remove_approval("inc-1")
    assert get_pending_approval() is None
    assert list_pending_approvals() == []


def test_get_pending_approval_returns_most_recent(fake_redis):
    from agents.sre.approvals import get_pending_approval, save_pending_approval

    save_pending_approval("inc-a", "SCALE_UP", "svc-a", "amael-ia", "HIGH_CPU")
    # Forzar timestamp más reciente en inc-b
    data = json.loads(fake_redis.data["sre:approval:inc-a"])
    data["created_at"] = "2000-01-01T00:00:00+00:00"
    fake_redis.data["sre:approval:inc-a"] = json.dumps(data)
    save_pending_approval("inc-b", "PATCH_RESOURCES", "svc-b", "amael-ia", "OOM_KILLED")

    assert get_pending_approval()["incident_key"] == "inc-b"


# ── helpers de commands ───────────────────────────────────────────────────────

def test_parse_duration():
    from agents.sre.commands import _parse_duration
    assert _parse_duration("30") == 30
    assert _parse_duration("2h") == 120
    assert _parse_duration("45m") == 45
    assert _parse_duration("") == 60
    assert _parse_duration("garbage") == 60


def test_parse_alert_context():
    from agents.sre.commands import _parse_alert_context
    text = "[SRE HIGH] ⚠️ HIGH: POD_FAILED\nRecurso: ollama-deployment-795 (amael-ia)"
    ctx = _parse_alert_context(text)
    assert ctx == {
        "issue": "POD_FAILED",
        "resource": "ollama-deployment-795",
        "namespace": "amael-ia",
    }
    assert _parse_alert_context(None) is None
    assert _parse_alert_context("texto sin formato") is None


# ── dispatcher ────────────────────────────────────────────────────────────────

def test_handle_command_ayuda_lists_all_commands(fake_redis):
    from agents.sre.commands import handle_command
    reply = handle_command("")
    for expected in ("estado", "briefing", "retro", "modo", "pendiente", "rollout"):
        assert expected in reply
    assert "STANDARD" in reply  # modo actual


def test_handle_command_unknown(fake_redis):
    from agents.sre.commands import handle_command
    reply = handle_command("comando-inexistente")
    assert "desconocido" in reply.lower()


def test_handle_command_modo_get_and_set(fake_redis):
    from agents.sre.commands import handle_command
    assert "STANDARD" in handle_command("modo") or "standard" in handle_command("modo")
    reply = handle_command("modo observe")
    assert "OBSERVE" in reply
    from agents.sre.autonomy import get_agent_mode
    assert get_agent_mode() == "observe"
    assert "inválido" in handle_command("modo yolo")


def test_handle_command_si_sin_aprobaciones(fake_redis):
    from agents.sre.commands import handle_command
    assert "Sin aprobaciones" in handle_command("si")
    assert "Sin aprobaciones" in handle_command("no")


def test_handle_command_pendiente_lista_aprobaciones(fake_redis):
    from agents.sre.approvals import save_pending_approval
    from agents.sre.commands import handle_command

    save_pending_approval(
        "inc-1", "SCALE_UP", "demo-oom", "amael-ia", "HIGH_MEMORY", confidence=0.9,
    )
    reply = handle_command("pendiente")
    assert "SCALE_UP" in reply
    assert "demo-oom" in reply


def test_handle_command_si_ejecuta_scale_up(fake_redis, monkeypatch):
    from agents.sre.approvals import save_pending_approval
    from agents.sre.commands import handle_command

    calls = []
    monkeypatch.setattr(
        "agents.sre.healer.scale_deployment_replicas",
        lambda resource, ns: calls.append((resource, ns)) or "✅ Scale-up ok",
    )
    save_pending_approval("inc-1", "SCALE_UP", "demo-oom", "amael-ia", "HIGH_MEMORY")
    reply = handle_command("si")
    assert calls == [("demo-oom", "amael-ia")]
    assert "SCALE_UP aprobado" in reply
    # La aprobación se consumió
    assert "Sin aprobaciones" in handle_command("si")


def test_handle_command_no_cancela(fake_redis):
    from agents.sre.approvals import save_pending_approval
    from agents.sre.commands import handle_command

    save_pending_approval("inc-1", "PATCH_RESOURCES", "demo-oom", "amael-ia", "OOM_KILLED")
    reply = handle_command("no")
    assert "cancelada" in reply.lower()
    assert "Sin aprobaciones" in handle_command("no")


def test_handle_command_rollout_sin_target(fake_redis):
    from agents.sre.commands import handle_command
    assert "rollout <deployment>" in handle_command("rollout")


def test_handle_command_rollout_con_target(fake_redis, monkeypatch):
    from agents.sre.commands import handle_command

    calls = []
    monkeypatch.setattr(
        "agents.sre.healer.rollout_restart",
        lambda name, ns: calls.append((name, ns)) or "✅ ROLLOUT_RESTART ejecutado",
    )
    reply = handle_command("rollout mi-servicio")
    assert calls == [("mi-servicio", "amael-ia")]
    assert "ROLLOUT_RESTART" in reply


def test_handle_command_silent_sin_quoted_text(fake_redis):
    from agents.sre.commands import handle_command
    reply = handle_command("silent 2h", quoted_text=None)
    assert "citando" in reply


def test_handle_command_silent_con_alerta_citada(fake_redis):
    from agents.sre.commands import handle_command

    quoted = "[SRE HIGH] ⚠️ HIGH: CRASH_LOOP\nRecurso: demo-crashloop (amael-ia)"
    reply = handle_command("silent 2h", quoted_text=quoted)
    assert "silenciada" in reply.lower()
    assert fake_redis.exists("sre:silent:amael-ia:demo-crashloop:CRASH_LOOP")


def test_handle_command_no_lanza_excepciones(fake_redis, monkeypatch):
    """El dispatcher captura errores internos y responde texto."""
    from agents.sre.commands import handle_command

    def _boom(*a, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr("agents.sre.reporter.get_recent_incidents", _boom)
    reply = handle_command("incidents")
    assert isinstance(reply, str)


# ── silenciado en el loop (scheduler) ─────────────────────────────────────────

def test_is_anomaly_silenced(fake_redis, monkeypatch):
    from agents.sre.models import Anomaly
    from agents.sre.scheduler import is_anomaly_silenced

    monkeypatch.setattr("storage.redis.get_client", lambda: fake_redis)
    anomaly = Anomaly(
        issue_type="CRASH_LOOP",
        severity="HIGH",
        namespace="amael-ia",
        resource_name="demo-crashloop-abc",
        resource_type="Pod",
        details="",
        owner_name="demo-crashloop",
    )
    assert is_anomaly_silenced(anomaly) is False
    fake_redis.set("sre:silent:amael-ia:demo-crashloop:CRASH_LOOP", "1")
    assert is_anomaly_silenced(anomaly) is True
