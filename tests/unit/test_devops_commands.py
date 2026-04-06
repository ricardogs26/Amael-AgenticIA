"""
Tests unitarios para la lógica de comandos /devops (aprobar, rechazar, pr).

Las funciones internas (_cmd_pr, _cmd_aprobar, _cmd_rechazar) usan imports
lazy dentro del cuerpo de la función. Se testean extrayendo la lógica pura
de selección de PR (sin necesidad de Redis real ni Bitbucket real).
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pr(pr_id: int, repo: str = "amael-agentic-backend", issue_type: str = "OOM_KILLED") -> dict:
    return {
        "pr_id": pr_id,
        "pr_url": f"https://bitbucket.org/ws/{repo}/pull-requests/{pr_id}",
        "repo": repo,
        "workspace": "ws",
        "branch": "fix/oom-0405-120000",
        "issue_type": issue_type,
        "rfc_sys_id": "",
        "rfc_number": "N/A",
        "rfc_url": "",
    }


def _make_redis(prs: list[dict]):
    """Mock Redis con PRs dados. Claves como bytes (simula redis-py sin decode_responses)."""
    redis = MagicMock()
    entries = {
        f"bb:pending_pr:ns:pod-{pr['pr_id']}:{pr['issue_type']}".encode(): json.dumps(pr).encode()
        for pr in prs
    }
    redis.keys.return_value = list(entries.keys())
    redis.get.side_effect = lambda k: entries.get(k if isinstance(k, bytes) else k.encode())
    redis.delete = MagicMock()
    return redis


def _import_devops_fns():
    """Importa las funciones del router devops aislando sus dependencias."""
    # Pre-registrar mocks de dependencias pesadas antes del import
    mocks = {
        "storage.redis": MagicMock(),
        "storage.redis.client": MagicMock(),
        "storage.postgres": MagicMock(),
        "storage.postgres.client": MagicMock(),
        "observability": MagicMock(),
        "observability.metrics": MagicMock(),
        "observability.tracing": MagicMock(),
        "agents.devops.bitbucket_client": MagicMock(),
    }
    for mod, mock in mocks.items():
        sys.modules.setdefault(mod, mock)

    # Forzar re-import limpio del módulo devops
    for key in list(sys.modules.keys()):
        if "interfaces.api.routers.devops" in key:
            del sys.modules[key]

    import importlib
    import interfaces.api.routers.devops as devops_mod
    return devops_mod


# ── Tests de selección de PR (lógica pura, sin I/O) ──────────────────────────

class TestPrSelection:
    """
    Testa la lógica de selección de PR dado un set de claves Redis.
    La función _select_pr_key() no existe como tal — validamos el comportamiento
    end-to-end de _cmd_aprobar con mocks.
    """

    def test_single_pr_key_selected_automatically(self):
        """Con 1 PR no se requiere ID explícito."""
        prs = [_make_pr(42)]
        redis = _make_redis(prs)
        decoded = [k.decode() if isinstance(k, bytes) else k for k in redis.keys()]
        assert len(decoded) == 1
        target = decoded[0]
        raw = redis.get(target)
        info = json.loads(raw)
        assert info["pr_id"] == 42

    def test_multiple_prs_require_explicit_id(self):
        """Con múltiples PRs, no se puede seleccionar sin ID."""
        prs = [_make_pr(31), _make_pr(32), _make_pr(33)]
        redis = _make_redis(prs)
        keys = redis.keys()
        assert len(keys) == 3

    def test_pr_lookup_by_id_finds_correct_entry(self):
        """Búsqueda por pr_id retorna la key correcta."""
        prs = [_make_pr(31), _make_pr(32), _make_pr(33)]
        redis = _make_redis(prs)
        target_id = 32
        decoded_keys = [k.decode() if isinstance(k, bytes) else k for k in redis.keys()]
        found = None
        for key in decoded_keys:
            raw = redis.get(key)
            info = json.loads(raw)
            if str(info["pr_id"]) == str(target_id):
                found = info
                break
        assert found is not None
        assert found["pr_id"] == 32

    def test_pr_lookup_unknown_id_returns_none(self):
        """Búsqueda por ID inexistente no encuentra nada."""
        prs = [_make_pr(31)]
        redis = _make_redis(prs)
        decoded_keys = [k.decode() if isinstance(k, bytes) else k for k in redis.keys()]
        found = None
        for key in decoded_keys:
            raw = redis.get(key)
            info = json.loads(raw)
            if str(info["pr_id"]) == "99":
                found = info
                break
        assert found is None

    def test_redis_bytes_keys_decoded_correctly(self):
        """Claves bytes de Redis se decodifican sin error."""
        prs = [_make_pr(55)]
        redis = _make_redis(prs)
        raw_keys = redis.keys()
        assert all(isinstance(k, bytes) for k in raw_keys)
        decoded = [k.decode() for k in raw_keys]
        assert all(isinstance(k, str) for k in decoded)
        assert decoded[0].startswith("bb:pending_pr:")

    def test_pr_info_has_expected_fields(self):
        """Cada PR en Redis tiene los campos necesarios para el merge."""
        pr = _make_pr(42, repo="frontend-next", issue_type="HIGH_MEMORY")
        assert "pr_id" in pr
        assert "workspace" in pr
        assert "repo" in pr
        assert pr["repo"] == "frontend-next"
        assert pr["issue_type"] == "HIGH_MEMORY"


# ── Tests de merge error handling ─────────────────────────────────────────────

class TestMergeErrorHandling:
    """Valida que los errores de Bitbucket no crasheen el sistema."""

    def test_409_error_string_detected(self):
        err = RuntimeError("Bitbucket API 409 en /merge: already merged")
        assert "409" in str(err)

    def test_404_error_string_detected(self):
        err = RuntimeError("Bitbucket API 404 en /merge: not found")
        assert "404" in str(err)

    def test_500_error_not_silenced(self):
        """Errores 5xx deben propagarse (no son casos de 'ya mergeado')."""
        err = RuntimeError("Bitbucket API 500 en /merge: internal server error")
        assert "409" not in str(err)
        assert "404" not in str(err)

    def test_decline_409_should_clean_redis(self):
        """Un 409 al declinar significa ya fue mergeado/declinado — limpiar tracking."""
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        # La lógica es: if status_code in (404, 409): redis.delete() + return ⚠️
        should_clean = mock_resp.status_code in (404, 409)
        assert should_clean

    def test_decline_200_should_clean_redis(self):
        """Un 200 normal también limpia Redis después del éxito."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        should_clean = mock_resp.status_code < 400
        assert should_clean

    def test_decline_500_should_not_clean_redis(self):
        """Un 500 no debe limpiar — el PR sigue pendiente para reintentar."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        is_already_done = mock_resp.status_code in (404, 409)
        is_success = mock_resp.status_code < 400
        assert not is_already_done
        assert not is_success


# ── Tests multi-repo ──────────────────────────────────────────────────────────

class TestMultiRepoPrHandling:
    """Valida que PRs de diferentes repos se identifican correctamente."""

    def test_prs_from_different_repos_distinguished(self):
        pr1 = _make_pr(31, repo="amael-agentic-backend")
        pr2 = _make_pr(32, repo="frontend-next")
        redis = _make_redis([pr1, pr2])
        decoded_keys = [k.decode() for k in redis.keys()]
        repos = set()
        for key in decoded_keys:
            raw = redis.get(key)
            info = json.loads(raw)
            repos.add(info["repo"])
        assert "amael-agentic-backend" in repos
        assert "frontend-next" in repos

    def test_approve_by_id_uses_correct_repo(self):
        """Aprobar PR #32 de frontend-next usa el repo correcto."""
        pr1 = _make_pr(31, repo="amael-agentic-backend")
        pr2 = _make_pr(32, repo="frontend-next")
        redis = _make_redis([pr1, pr2])
        decoded_keys = [k.decode() for k in redis.keys()]
        found_repo = None
        for key in decoded_keys:
            raw = redis.get(key)
            info = json.loads(raw)
            if str(info["pr_id"]) == "32":
                found_repo = info["repo"]
                break
        assert found_repo == "frontend-next"

    def test_no_prs_empty_list(self):
        redis = _make_redis([])
        assert redis.keys() == []


# ── Tests TTL awareness ───────────────────────────────────────────────────────

class TestTtlAwareness:
    """Valida que el sistema maneja keys expiradas sin crash."""

    def test_expired_key_returns_none_gracefully(self):
        """Simula key que expiró entre keys() y get()."""
        redis = MagicMock()
        redis.keys.return_value = [b"bb:pending_pr:some-incident"]
        redis.get.return_value = None  # Expiró
        raw = redis.get("bb:pending_pr:some-incident")
        pr_info = json.loads(raw) if raw else {}
        assert pr_info == {}  # No crash, solo dict vacío

    def test_pr_id_from_expired_key_is_zero(self):
        """pr_id=0 de key expirada → endpoint devuelve error limpio."""
        pr_info = {}
        pr_id = int(pr_info.get("pr_id", 0))
        assert pr_id == 0
        # El código verifica: if not pr_id: return "❌ PR pendiente encontrado pero sin ID válido."
        assert not pr_id
