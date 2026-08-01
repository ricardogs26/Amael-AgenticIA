"""Pruebas del watchdog externo (agents/sre/watchdog.py)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.sre import watchdog


def _dep(desired: int, available):
    return SimpleNamespace(
        spec=SimpleNamespace(replicas=desired),
        status=SimpleNamespace(available_replicas=available),
    )


def test_deployment_sano_no_reporta():
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _dep(1, 1)
    assert watchdog.check_deployment(api, "amael-ia", "ollama-deployment") is None


def test_cero_replicas_disponibles_es_problema():
    """El caso ollama: 1 deseada, 0 disponible porque los pods huérfanos
    retenían la GPU."""
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _dep(1, 0)

    problem = watchdog.check_deployment(api, "amael-ia", "ollama-deployment")

    assert problem is not None
    assert "SIN RÉPLICAS DISPONIBLES" in problem
    assert "ollama-deployment" in problem


def test_available_replicas_none_cuenta_como_cero():
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _dep(1, None)
    assert "SIN RÉPLICAS" in watchdog.check_deployment(api, "amael-ia", "ollama-deployment")


def test_degradado_parcial_se_reporta():
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _dep(2, 1)
    assert "degradado" in watchdog.check_deployment(api, "amael-ia", "demo")


def test_escalado_a_cero_a_proposito_no_alerta():
    """Los deployments demo viven en replicas: 0 hasta el día de la demo."""
    api = MagicMock()
    api.read_namespaced_deployment.return_value = _dep(0, 0)
    assert watchdog.check_deployment(api, "amael-ia", "demo-oom") is None


def test_deployment_inexistente_se_reporta():
    api = MagicMock()
    api.read_namespaced_deployment.side_effect = SimpleNamespace  # placeholder
    exc = Exception("boom")
    exc.status = 404
    api.read_namespaced_deployment.side_effect = exc

    assert "NO EXISTE" in watchdog.check_deployment(api, "amael-ia", "fantasma")


def test_parse_targets_ignora_entradas_sin_namespace():
    assert watchdog._parse_targets("amael-ia/ollama, ,suelto,vault/vault-0") == [
        ("amael-ia", "ollama"),
        ("vault", "vault-0"),
    ]
