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


# ── PAT de GitHub ───────────────────────────────────────────────────────────
# Caso disparador: 12-ago-2026. El PAT clásico del github-runner (creado el
# 17-mar) expiró; el runner llevaba ~5 días en CrashLoop con 401 y Camael no
# podía abrir PRs. Ningún chequeo de réplicas lo habría dicho a tiempo.

def _github(monkeypatch, status=200, headers=None, exc=None):
    import requests

    def _get(url, headers=None, timeout=None):
        if exc:
            raise exc
        return SimpleNamespace(status_code=status, headers=_HEADERS, text="{}")

    _HEADERS = headers or {}
    monkeypatch.setattr(requests, "get", _get)
    monkeypatch.setattr(watchdog, "_GH_TOKEN", "ghp_falso")


def test_github_token_valido_no_reporta(monkeypatch):
    _github(monkeypatch, status=200)
    assert watchdog.check_github_token() is None


def test_github_token_expirado_se_reporta(monkeypatch):
    _github(monkeypatch, status=401)

    problem = watchdog.check_github_token()

    assert problem is not None
    assert "401" in problem or "INVÁLIDO" in problem
    # Dice qué se rompe, no solo que algo pasó.
    assert "runner" in problem.lower()
    assert "camael" in problem.lower()


def test_github_token_por_expirar_avisa_antes(monkeypatch):
    """El valor está en avisar ANTES del 401, no en confirmarlo cinco días
    después. GitHub manda la fecha en un header de la respuesta."""
    _github(
        monkeypatch,
        status=200,
        headers={"github-authentication-token-expiration": "2026-08-15 00:00:00 UTC"},
    )

    problem = watchdog.check_github_token()

    assert problem is not None
    assert "expira" in problem.lower()


def test_github_token_con_expiracion_lejana_no_reporta(monkeypatch):
    _github(
        monkeypatch,
        status=200,
        headers={"github-authentication-token-expiration": "2027-01-01 00:00:00 UTC"},
    )
    assert watchdog.check_github_token() is None


def test_github_expiracion_ilegible_no_inventa_problema(monkeypatch):
    """Un header con formato inesperado no es prueba de nada: el token
    respondió 200. Callar aquí es correcto; el 401 sigue cubierto."""
    _github(monkeypatch, status=200, headers={"github-authentication-token-expiration": "manana"})
    assert watchdog.check_github_token() is None


def test_github_incomunicado_se_reporta(monkeypatch):
    _github(monkeypatch, exc=RuntimeError("connection refused"))

    problem = watchdog.check_github_token()

    assert "no se pudo consultar" in problem
    assert "INVÁLIDO" not in problem


def test_github_desactivable_sin_token(monkeypatch):
    """Sin secreto montado el chequeo se apaga: un watchdog que alerta por su
    propia configuración incompleta enseña a ignorarlo."""
    monkeypatch.setattr(watchdog, "_GH_TOKEN", "")
    assert watchdog.check_github_token() is None


def test_la_clave_de_dedup_de_github_sale_del_aviso(monkeypatch):
    _github(monkeypatch, status=401)
    assert watchdog.check_github_token().split("`")[1] == "github/pat"


def test_el_whatsapp_incluye_como_rotar_el_pat(monkeypatch):
    enviado = {}

    class _Requests:
        @staticmethod
        def post(url, json=None, timeout=None):
            enviado["text"] = json["text"]
            return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(watchdog, "_PHONE", "5210000000000")
    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

    watchdog.send_alert(["`github/pat` INVÁLIDO (401)."])

    assert "github-runner-secret" in enviado["text"]


def test_aviso_preventivo_no_se_disfraza_de_caida(monkeypatch):
    """Un PAT que expira en 5 días no es un servicio caído. Titularlo así (y
    mandar a mirar pods) es el ruido que enseña a ignorar al watchdog."""
    enviado = {}

    class _Requests:
        @staticmethod
        def post(url, json=None, timeout=None):
            enviado["text"] = json["text"]
            return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(watchdog, "_PHONE", "5210000000000")
    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

    watchdog.send_alert(["`github/pat` expira en 5.0 días (2026-08-17 00:00:00 UTC)."])

    assert "caído" not in enviado["text"]
    assert "kubectl get pods" not in enviado["text"]
    assert "github-runner-secret" in enviado["text"]


def test_una_caida_real_sigue_mandando_a_mirar_pods(monkeypatch):
    enviado = {}

    class _Requests:
        @staticmethod
        def post(url, json=None, timeout=None):
            enviado["text"] = json["text"]
            return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(watchdog, "_PHONE", "5210000000000")
    monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

    watchdog.send_alert(
        [
            "`amael-ia/ollama-deployment` SIN RÉPLICAS DISPONIBLES (0/1).",
            "`github/pat` expira en 5.0 días (2026-08-17 00:00:00 UTC).",
        ]
    )

    assert "caído" in enviado["text"]
    assert "kubectl get pods" in enviado["text"]
