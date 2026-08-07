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


# ── Vault sellado (7-ago-2026) ────────────────────────────────────────────────
#
# Un pod de Vault que reinicia arranca sellado: el proceso responde y el
# Deployment se ve sano, así que check_deployment no ve nada. Esa mañana el
# brief de las 7:00 dijo «no se encontraron credenciales de Google Calendar,
# autoriza el acceso» — la autorización estaba intacta y Vault llevaba cuatro
# horas sellado. El watchdog tiene que nombrar la causa real.

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _vault(monkeypatch, payload=None, exc=None):
    import requests

    def _get(url, timeout=None):
        assert "/v1/sys/seal-status" in url
        if exc:
            raise exc
        return _Resp(payload)

    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(watchdog, "_VAULT_ADDR", "http://vault.vault:8200")


def test_vault_abierto_no_reporta(monkeypatch):
    _vault(monkeypatch, {"sealed": False, "initialized": True})
    assert watchdog.check_vault() is None


def test_vault_sellado_se_reporta(monkeypatch):
    _vault(monkeypatch, {"sealed": True, "initialized": True, "progress": 0, "t": 3})

    problem = watchdog.check_vault()

    assert problem is not None
    assert "SELLADO" in problem and "0/3" in problem
    # El aviso dice qué se rompe, no solo que algo pasó.
    assert "brief" in problem.lower()


def test_vault_sin_inicializar_se_reporta(monkeypatch):
    _vault(monkeypatch, {"sealed": True, "initialized": False})
    assert "NO INICIALIZADO" in watchdog.check_vault()


def test_vault_incomunicado_se_reporta(monkeypatch):
    """No poder preguntar no prueba que esté sellado, pero tampoco se calla."""
    _vault(monkeypatch, exc=RuntimeError("connection refused"))

    problem = watchdog.check_vault()

    assert "no se pudo consultar" in problem
    assert "SELLADO" not in problem


def test_vault_desactivable(monkeypatch):
    monkeypatch.setattr(watchdog, "_VAULT_ADDR", "")
    assert watchdog.check_vault() is None


def test_la_clave_de_dedup_sale_del_aviso(monkeypatch):
    """main() usa p.split('`')[1] — el aviso debe traer el recurso ahí."""
    _vault(monkeypatch, {"sealed": True, "initialized": True, "progress": 1, "t": 3})

    assert watchdog.check_vault().split("`")[1] == "vault/vault-0"


def test_el_whatsapp_incluye_como_abrir_vault(monkeypatch):
    enviado = {}

    class _Requests:
        @staticmethod
        def post(url, json=None, timeout=None):
            enviado["text"] = json["text"]
            return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(watchdog, "_PHONE", "5210000000000")
    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

    watchdog.send_alert(["`vault/vault-0` SELLADO (0/3 llaves)."])

    assert "vault operator unseal" in enviado["text"]
    assert "vault.root" in enviado["text"]
