"""
Tests: un pod que cae por falta de internet no se remedia con ROLLOUT_RESTART.

Incidente 23-oct-2026: Starlink murió a las 02:31 con el enlace arriba y el
ER605 siguió mandándole conexiones. El github-runner pide su token a
api.github.com en cada arranque y entró en CrashLoop con `curl: (35)` /
`curl: (28)`; Raphael lo reinició dos veces («verify:ok») sin arreglar nada.
"""
from __future__ import annotations

from agents.sre import healer
from agents.sre.models import Anomaly

RUNNER_LOG = (
    "Configuring\n"
    "curl: (35) OpenSSL SSL_connect: Connection reset by peer in connection "
    "to api.github.com:443\n"
    "Could not get registration token\n"
)


def _anomaly(issue_type="CRASH_LOOP", metadata=None) -> Anomaly:
    return Anomaly(
        issue_type=issue_type,
        severity="HIGH",
        namespace="amael-ia",
        resource_name="github-runner-deployment-7d9f-abcde",
        resource_type="Pod",
        details="CrashLoopBackOff",
        owner_name="github-runner-deployment",
        metadata=metadata or {"owner_kind": "Deployment"},
    )


def test_detecta_curl_35_del_runner():
    assert "curl: (35)" in healer.find_external_network_error(RUNNER_LOG)


def test_detecta_curl_28_y_dns():
    assert healer.find_external_network_error("curl: (28) Connection timed out after 10001 ms")
    assert healer.find_external_network_error("Could not resolve host: api.github.com")


def test_destino_interno_no_cuenta_como_internet():
    """Una caída hacia postgres/redis puede ser del pod o del cluster, no de la WAN."""
    log = (
        'connection to server at "postgres-service" (10.152.183.5), port 5432 '
        "failed: Connection timed out\n"
        "dial tcp redis-service.amael-ia.svc.cluster.local:6379: i/o timeout\n"
    )
    assert healer.find_external_network_error(log) is None


def test_log_sin_errores_de_red():
    assert healer.find_external_network_error("Traceback...\nKeyError: 'x'") is None
    assert healer.find_external_network_error("") is None


def test_detect_marca_metadata(monkeypatch):
    monkeypatch.setattr(healer, "_get_pod_logs", lambda *_a, **_k: RUNNER_LOG)
    a = _anomaly()
    assert healer.detect_external_network_failure(a)
    assert "curl: (35)" in a.metadata["external_network"]
    assert a.metadata["owner_kind"] == "Deployment"  # no pisa lo que ya había


def test_detect_ignora_tipos_sin_log(monkeypatch):
    llamado = []
    monkeypatch.setattr(healer, "_get_pod_logs", lambda *a, **k: llamado.append(1) or RUNNER_LOG)
    assert healer.detect_external_network_failure(_anomaly("HIGH_CPU")) is None
    assert not llamado


def test_red_externa_notifica_y_no_reinicia(monkeypatch):
    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda *_a: True)
    a = _anomaly(metadata={"owner_kind": "Deployment", "external_network": "curl: (35) x"})
    assert healer.decide_action(a, confidence=1.0) == "NOTIFY_HUMAN"


def test_sin_red_externa_sigue_reiniciando(monkeypatch):
    monkeypatch.setattr(healer, "_restarted_within_verification_window", lambda *_a: False)
    monkeypatch.setattr(healer, "_deployment_exists", lambda *_a: True)
    assert healer.decide_action(_anomaly(), confidence=0.99) == "ROLLOUT_RESTART"


def test_mensaje_dice_que_es_la_red(monkeypatch):
    monkeypatch.setattr("agents.sre.autonomy.apply_mode_to_action", lambda a, c: a)
    enviados = []
    a = _anomaly(metadata={"owner_kind": "Deployment", "external_network": "curl: (35) reset"})
    healer.execute_sre_action(a, "NOTIFY_HUMAN", lambda m, s: enviados.append(m))
    assert "red EXTERNA" in enviados[0] and "curl: (35)" in enviados[0]
