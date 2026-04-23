# Phase 3 — Camael Standalone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extraer el agente Camael (`agents/devops/*`) a su propio pod `camael-service:8003`, cerrando el split iniciado en Fase 2 con Raphael.

**Architecture:** Pod nuevo `camael-service` que absorbe `agents/devops/*` + router `/api/camael/*` nuevo para el handoff HTTP desde Raphael. Feature flag separado `CAMAEL_MODE` (independiente de `AGENTS_MODE`) con default `inprocess` para canary seguro. Fallback Redis WAL garantiza entrega si Camael está caído.

**Tech Stack:** FastAPI, httpx, Redis (WAL), PostgreSQL, Kubernetes (MicroK8s), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-04-23-phase-3-camael-standalone-design.md`

**Scaffolding ya existente (Fase 1):**
- `clients/camael_client.py` — `handoff_to_camael()`, `drain_pending_handoffs()` ya implementados (usa `agents_mode` — hay que cambiar a `camael_mode`)
- `clients/_http.py` — `get_camael_client()`, `get_camael_async_client()`
- `config/settings.py` — `agents_mode`, `camael_service_url` (default `http://camael-service:8003`)
- `k8s/rbac/06-camael-rbac.yaml` — ServiceAccount `camael-sa`, Role `camael-deployer`, ClusterRoleBinding para leer ArgoCD Applications
- `tests/contract/test_camael_client_contract.py` — contract tests para handoff
- `interfaces/api/routers/devops.py` — webhooks GitHub/Bitbucket (sin cambios en este plan)

**Working directory:** `/home/richardx/k8s-lab/Amael-AgenticIA` (rama `feature/agents-split`).

---

## Sub-phase 3.1 — Feature flag `CAMAEL_MODE` + WAL genérico

### Task 1: Agregar `camael_mode` a settings, separado de `agents_mode`

**Files:**
- Modify: `config/settings.py` (añadir campo tras `agents_mode`, ~línea 67)
- Test: `tests/unit/config/test_settings.py` (crear si no existe)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/config/test_settings.py
"""Unit tests for config/settings.py — feature flags split."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def test_camael_mode_default_is_inprocess():
    """CAMAEL_MODE debe defaultear a 'inprocess' para rollout seguro."""
    from config.settings import Settings
    env = {
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.camael_mode == "inprocess"


def test_camael_mode_independent_from_agents_mode():
    """AGENTS_MODE=remote no debe activar Camael remoto automáticamente."""
    from config.settings import Settings
    env = {
        "AGENTS_MODE": "remote",
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.agents_mode == "remote"
        assert s.camael_mode == "inprocess"  # ← canary independiente


def test_camael_mode_can_be_set_remote():
    from config.settings import Settings
    env = {
        "CAMAEL_MODE": "remote",
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
        assert s.camael_mode == "remote"


def test_camael_mode_rejects_invalid_value():
    """Valor inválido debe fallar al construir Settings (Pydantic Literal)."""
    from config.settings import Settings
    env = {
        "CAMAEL_MODE": "banana",
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(Exception):  # ValidationError de pydantic
            Settings()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/config/test_settings.py -v
```
Expected: FAIL en `test_camael_mode_default_is_inprocess` con `AttributeError: 'Settings' object has no attribute 'camael_mode'`

- [ ] **Step 3: Add `camael_mode` field to settings**

Edit `config/settings.py`. Localizar el bloque de `agents_mode` (~línea 61-74) y añadir `camael_mode` inmediatamente después de la declaración de `agents_mode` (antes de `raphael_service_url`):

```python
    # CAMAEL_MODE — flag SEPARADO de AGENTS_MODE para canary independiente.
    # Default "inprocess" incluso si AGENTS_MODE=remote, porque el canary de
    # Raphael (Fase 2) y el de Camael (Fase 3) se activan en commits distintos.
    # Ver docs/superpowers/specs/2026-04-23-phase-3-camael-standalone-design.md §7
    camael_mode: Literal["inprocess", "remote"] = Field(
        default="inprocess",
        alias="CAMAEL_MODE",
        description="inprocess: agents/devops/ corre dentro del proceso. "
                    "remote: se delega por HTTP a camael-service:8003.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/unit/config/test_settings.py -v
```
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add config/settings.py tests/unit/config/test_settings.py
git commit -m "feat(agents-split): Phase 3.1 — CAMAEL_MODE flag separado de AGENTS_MODE"
```

---

### Task 2: Actualizar `camael_client` para usar `camael_mode`

**Files:**
- Modify: `clients/camael_client.py` (funciones `_is_inprocess`, `handoff_to_camael`, `get_handoff_status`, `drain_pending_handoffs`)
- Test: `tests/contract/test_camael_client_contract.py` (añadir test de flag separado)

- [ ] **Step 1: Write the failing test**

Añadir al final de `tests/contract/test_camael_client_contract.py`:

```python
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
        # Verificar que NO se intentó HTTP
        assert fake_redis.db == {}

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
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/contract/test_camael_client_contract.py::TestCamaelModeFlag -v
```
Expected: ambos tests FALLAN porque `_is_inprocess()` actualmente lee `settings.agents_mode`

- [ ] **Step 3: Change `_is_inprocess` to read `camael_mode`**

Edit `clients/camael_client.py` línea 35:

```python
def _is_inprocess() -> bool:
    """Camael usa su propio flag CAMAEL_MODE independiente de AGENTS_MODE."""
    return settings.camael_mode == "inprocess"
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/contract/test_camael_client_contract.py -v
```
Expected: all PASS (nuevos + existentes siguen pasando)

- [ ] **Step 5: Commit**

```bash
git add clients/camael_client.py tests/contract/test_camael_client_contract.py
git commit -m "feat(agents-split): Phase 3.1 — camael_client usa CAMAEL_MODE (no AGENTS_MODE)"
```

---

### Task 3: Storage Redis WAL genérico (`storage/redis/wal.py`)

**Files:**
- Create: `storage/redis/wal.py`
- Create: `tests/unit/storage/redis/test_wal.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/storage/redis/test_wal.py
"""Unit tests for storage/redis/wal.py — Write-Ahead Log genérico."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_redis(monkeypatch):
    """Redis en memoria con set/get/keys/delete + TTL mocked."""
    class FakeRedis:
        def __init__(self):
            self.db: dict[str, str] = {}
            self.ttls: dict[str, int] = {}

        def set(self, key, value, ex=None):
            self.db[key] = value
            if ex:
                self.ttls[key] = ex
            return True

        def get(self, key):
            return self.db.get(key)

        def keys(self, pattern):
            import fnmatch
            return [k for k in self.db if fnmatch.fnmatch(k, pattern)]

        def delete(self, *keys):
            for k in keys:
                self.db.pop(k, None)
                self.ttls.pop(k, None)
            return len(keys)

        def ttl(self, key):
            return self.ttls.get(key, -1)

    fr = FakeRedis()
    monkeypatch.setattr(
        "storage.redis.client.get_client",
        lambda: fr,
    )
    return fr


def test_enqueue_persists_payload_with_ttl(fake_redis):
    from storage.redis.wal import enqueue

    ok = enqueue(
        topic="handoff",
        key="incident-123",
        payload={"foo": "bar"},
        ttl_seconds=86400,
    )
    assert ok is True
    assert "wal:camael:handoff:incident-123" in fake_redis.db
    data = json.loads(fake_redis.db["wal:camael:handoff:incident-123"])
    assert data == {"foo": "bar"}
    assert fake_redis.ttls["wal:camael:handoff:incident-123"] == 86400


def test_enqueue_idempotent_same_key(fake_redis):
    """Mismo key sobrescribe sin error (idempotencia por key)."""
    from storage.redis.wal import enqueue

    enqueue("handoff", "inc-1", {"v": 1}, ttl_seconds=3600)
    enqueue("handoff", "inc-1", {"v": 2}, ttl_seconds=3600)

    keys = fake_redis.keys("wal:camael:handoff:*")
    assert len(keys) == 1
    assert json.loads(fake_redis.db[keys[0]])["v"] == 2


def test_drain_processes_all_pending_and_deletes_on_success(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-1", {"id": 1}, ttl_seconds=3600)
    enqueue("handoff", "inc-2", {"id": 2}, ttl_seconds=3600)

    processed = []

    def consumer(payload: dict) -> bool:
        processed.append(payload)
        return True  # éxito → drain debe DEL la key

    count = drain("handoff", consumer)

    assert count == 2
    assert len(processed) == 2
    assert fake_redis.keys("wal:camael:handoff:*") == []


def test_drain_keeps_entry_on_failure(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-fail", {"id": 1}, ttl_seconds=3600)

    def consumer(payload: dict) -> bool:
        return False  # fallo → drain NO borra, deja para siguiente tick

    count = drain("handoff", consumer)

    assert count == 0
    assert "wal:camael:handoff:inc-fail" in fake_redis.db


def test_drain_keeps_entry_on_consumer_exception(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-boom", {"id": 1}, ttl_seconds=3600)

    def consumer(payload: dict) -> bool:
        raise RuntimeError("camael unreachable")

    count = drain("handoff", consumer)

    assert count == 0
    assert "wal:camael:handoff:inc-boom" in fake_redis.db


def test_pending_count_returns_number_of_entries(fake_redis):
    from storage.redis.wal import enqueue, pending_count

    assert pending_count("handoff") == 0
    enqueue("handoff", "a", {}, ttl_seconds=60)
    enqueue("handoff", "b", {}, ttl_seconds=60)
    enqueue("rfc_update", "c", {}, ttl_seconds=60)
    assert pending_count("handoff") == 2
    assert pending_count("rfc_update") == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/storage/redis/test_wal.py -v
```
Expected: FAIL con `ModuleNotFoundError: No module named 'storage.redis.wal'`

- [ ] **Step 3: Implement the WAL module**

Create `storage/redis/wal.py`:

```python
"""
storage.redis.wal — Write-Ahead Log genérico sobre Redis.

Usado como fallback cuando un servicio downstream (ej. camael-service) no está
disponible. El productor encola el payload; el consumidor lo drena cuando
se recupera conectividad.

Keys: wal:camael:{topic}:{key}   — namespace fijo "camael" (único consumidor hoy).
TTL:  24h por default (86400s) — eventos más viejos se pierden (idempotencia
      externa por incident_key debe proteger contra replays tardíos).

Topics soportados (convención, no enforced):
  - handoff       — Raphael → Camael handoff
  - rfc_update    — Raphael → Camael PATCH RFC post-verificación

Idempotencia: por `key` (incident_key, sys_id). Mismo key sobrescribe.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger("storage.redis.wal")

_KEY_TEMPLATE = "wal:camael:{topic}:{key}"
_DEFAULT_TTL_SECONDS = 86400  # 24h


def _make_key(topic: str, key: str) -> str:
    return _KEY_TEMPLATE.format(topic=topic, key=key)


def enqueue(
    topic: str,
    key: str,
    payload: dict[str, Any],
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> bool:
    """
    Encola un evento en el WAL. Idempotente por `key`.
    Retorna True si se persistió, False si Redis falla.
    """
    try:
        from storage.redis.client import get_client
        r = get_client()
        full_key = _make_key(topic, key)
        r.set(full_key, json.dumps(payload), ex=ttl_seconds)
        logger.warning(
            f"[wal] enqueued {full_key} (TTL {ttl_seconds}s)"
        )
        return True
    except Exception as exc:
        logger.error(f"[wal] enqueue FALLÓ topic={topic} key={key}: {exc}")
        return False


def drain(
    topic: str,
    consumer: Callable[[dict[str, Any]], bool],
) -> int:
    """
    Drena todas las entradas pendientes del topic.
    `consumer(payload)` debe retornar True en éxito (→ key se borra) o
    False/raise en fallo (→ key se conserva para siguiente tick).

    Retorna el número de entradas procesadas con éxito.
    """
    try:
        from storage.redis.client import get_client
        r = get_client()
        pattern = _make_key(topic, "*")
        keys = r.keys(pattern) or []
    except Exception as exc:
        logger.error(f"[wal] drain connect FALLÓ topic={topic}: {exc}")
        return 0

    ok = 0
    for full_key in keys:
        full_key_str = full_key if isinstance(full_key, str) else full_key.decode()
        try:
            raw = r.get(full_key_str)
            if raw is None:
                continue
            raw_str = raw if isinstance(raw, str) else raw.decode()
            payload = json.loads(raw_str)
            success = consumer(payload)
            if success:
                r.delete(full_key_str)
                ok += 1
                logger.info(f"[wal] drained {full_key_str}")
        except Exception as exc:
            logger.warning(f"[wal] consumer error on {full_key_str}: {exc}")
            # No borrar — retry en próximo tick
    return ok


def pending_count(topic: str) -> int:
    """Cuenta entradas pendientes del topic (para métricas / alertas)."""
    try:
        from storage.redis.client import get_client
        r = get_client()
        keys = r.keys(_make_key(topic, "*")) or []
        return len(keys)
    except Exception:
        return 0
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/unit/storage/redis/test_wal.py -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add storage/redis/wal.py tests/unit/storage/redis/test_wal.py
git commit -m "feat(agents-split): Phase 3.1 — storage.redis.wal WAL genérico"
```

---

## Sub-phase 3.2 — `update_rfc` en camael_client + router camael en backend

### Task 4: `update_rfc()` en `camael_client` con WAL fallback

**Files:**
- Modify: `clients/camael_client.py` (añadir `update_rfc`, refactor handoff para usar WAL genérico)
- Modify: `tests/contract/test_camael_client_contract.py` (añadir TestUpdateRfc)

- [ ] **Step 1: Write the failing test**

Añadir a `tests/contract/test_camael_client_contract.py`:

```python
class TestUpdateRfc:
    """update_rfc(sys_id, result, message) — contract contra /api/camael/rfc/{sys_id}."""

    def _inprocess_settings(self):
        class S:
            camael_mode = "inprocess"
            agents_mode = "inprocess"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"
        return S()

    def _remote_settings(self):
        class S:
            camael_mode = "remote"
            agents_mode = "remote"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"
        return S()

    def test_inprocess_calls_servicenow_close_rfc(self, monkeypatch):
        from clients import camael_client
        monkeypatch.setattr(camael_client, "settings", self._inprocess_settings())

        calls = []

        class FakeSn:
            def is_configured(self):
                return True
            async def close_rfc(self, sys_id, message):
                calls.append(("close", sys_id, message))
            async def fail_rfc(self, sys_id, message):
                calls.append(("fail", sys_id, message))

        import sys, types
        fake_module = types.ModuleType("agents.devops.servicenow_client")
        fake_sn = FakeSn()
        fake_module.is_configured = fake_sn.is_configured
        fake_module.close_rfc = fake_sn.close_rfc
        fake_module.fail_rfc = fake_sn.fail_rfc
        monkeypatch.setitem(sys.modules, "agents.devops.servicenow_client", fake_module)

        import asyncio
        asyncio.run(camael_client.update_rfc(
            sys_id="SN123",
            result="closed",
            message="Healthy 5min post-deploy",
        ))
        assert calls == [("close", "SN123", "Healthy 5min post-deploy")]

    def test_remote_patch_http_on_success(self, monkeypatch):
        from clients import camael_client
        monkeypatch.setattr(camael_client, "settings", self._remote_settings())

        http_calls = []

        class FakeResp:
            status_code = 200
            content = b'{"sys_id":"SN123","state":"Closed"}'
            text = '{"sys_id":"SN123","state":"Closed"}'
            def json(self):
                return {"sys_id": "SN123", "state": "Closed"}
            def raise_for_status(self):
                pass

        class FakeClient:
            def patch(self, path, json):
                http_calls.append((path, json))
                return FakeResp()

        monkeypatch.setattr(
            "clients._http.get_camael_client", lambda: FakeClient()
        )

        import asyncio
        asyncio.run(camael_client.update_rfc(
            sys_id="SN123",
            result="closed",
            message="ok",
            deployment="demo-oom",
            namespace="amael-ia",
        ))
        assert http_calls == [
            (
                "/api/camael/rfc/SN123",
                {
                    "result": "closed",
                    "message": "ok",
                    "deployment": "demo-oom",
                    "namespace": "amael-ia",
                },
            )
        ]

    def test_remote_falls_back_to_wal_on_network_error(self, monkeypatch, fake_redis):
        from clients import camael_client
        monkeypatch.setattr(camael_client, "settings", self._remote_settings())

        class FakeClient:
            def patch(self, path, json):
                raise ConnectionError("camael unreachable")

        monkeypatch.setattr(
            "clients._http.get_camael_client", lambda: FakeClient()
        )

        import asyncio
        asyncio.run(camael_client.update_rfc(
            sys_id="SN456",
            result="review",
            message="Deploy failed",
            deployment="demo-oom",
            namespace="amael-ia",
        ))

        keys = fake_redis.keys("wal:camael:rfc_update:*")
        assert len(keys) == 1
        assert keys[0] == "wal:camael:rfc_update:SN456"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/contract/test_camael_client_contract.py::TestUpdateRfc -v
```
Expected: FAIL con `AttributeError: module 'clients.camael_client' has no attribute 'update_rfc'`

- [ ] **Step 3: Implement `update_rfc` in `camael_client.py`**

Añadir al final de `clients/camael_client.py`:

```python
# ── update_rfc — Raphael cierra / marca review del RFC post-verificación ──────

async def update_rfc(
    sys_id: str,
    result: str,
    message: str,
    deployment: str | None = None,
    namespace: str | None = None,
) -> None:
    """
    Actualiza el estado de un RFC ServiceNow tras la verificación post-deploy.

    Contrato (reemplaza `agents/sre/healer._update_rfc_state` + import directo
    de agents.devops.servicenow_client):

    - result="closed": el deployment está sano 5min post-deploy → cerrar RFC
    - result="review": verificación falló → marcar para revisión manual

    En CAMAEL_MODE=inprocess llama al módulo servicenow_client local.
    En CAMAEL_MODE=remote hace PATCH /api/camael/rfc/{sys_id}; si falla,
    encola en WAL (topic "rfc_update", key=sys_id).

    Idempotencia: ServiceNow acepta transiciones repetidas al mismo estado
    sin error; el WAL dedup por sys_id cubre el resto.
    """
    if _is_inprocess():
        try:
            from agents.devops import servicenow_client as sn
            if not sn.is_configured():
                return
            if result == "closed":
                await sn.close_rfc(sys_id, message)
            elif result == "review":
                await sn.fail_rfc(sys_id, message)
            else:
                logger.warning(f"[camael_client] update_rfc result inválido: {result}")
        except Exception as exc:
            logger.warning(f"[camael_client] update_rfc inprocess FALLÓ: {exc}")
        return

    # ── Remote path ────────────────────────────────────────────────────────────
    payload: dict[str, Any] = {"result": result, "message": message}
    if deployment is not None:
        payload["deployment"] = deployment
    if namespace is not None:
        payload["namespace"] = namespace

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
        resp = client.patch(f"/api/camael/rfc/{sys_id}", json=payload)
        if resp.status_code in (200, 204):
            logger.info(f"[camael_client] update_rfc OK {sys_id} result={result}")
            return
        if resp.status_code == 404:
            logger.warning(f"[camael_client] RFC {sys_id} no existe en Camael — skip")
            return
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text}")
    except Exception as exc:
        logger.error(f"[camael_client] update_rfc FALLÓ {sys_id}: {exc}")
        from storage.redis import wal
        wal.enqueue("rfc_update", sys_id, payload)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/contract/test_camael_client_contract.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add clients/camael_client.py tests/contract/test_camael_client_contract.py
git commit -m "feat(agents-split): Phase 3.2 — camael_client.update_rfc + WAL fallback"
```

---

### Task 5: Rewire `agents/sre/healer.py:_update_rfc_state` a `camael_client.update_rfc`

**Files:**
- Modify: `agents/sre/healer.py` (líneas 798-833)
- Test: `tests/unit/agents/sre/test_healer_update_rfc.py` (crear)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/agents/sre/test_healer_update_rfc.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/agents/sre/test_healer_update_rfc.py -v
```
Expected: FAIL — el último test falla porque el import sigue presente; los primeros fallan porque `_update_rfc_state` llama a `sn.close_rfc` directo.

- [ ] **Step 3: Rewire `_update_rfc_state`**

Edit `agents/sre/healer.py` — reemplazar la función completa (líneas 798-833):

```python
async def _update_rfc_state(
    rfc_info: dict,
    deployment_name: str,
    namespace: str,
    success: bool,
    reason: str,
) -> None:
    """
    Actualiza el RFC en ServiceNow según el resultado de la verificación.

    Delega a clients.camael_client.update_rfc — Camael es el único dueño de
    ServiceNow post Phase 3. Si Camael está caído, el cliente encola el
    evento en el WAL Redis (topic rfc_update, TTL 24h) para replay.
    """
    try:
        sys_id = rfc_info.get("sys_id", "")
        number = rfc_info.get("number", "N/A")
        if not sys_id:
            return

        if success:
            message = (
                f"Despliegue verificado como exitoso por Raphael (SRE).\n"
                f"Deployment {namespace}/{deployment_name} saludable 5 min post-deploy.\n"
                f"RFC {number} cerrado automáticamente."
            )
            from clients.camael_client import update_rfc
            await update_rfc(
                sys_id=sys_id,
                result="closed",
                message=message,
                deployment=deployment_name,
                namespace=namespace,
            )
            logger.info(f"[healer] RFC {number} → Closed (verificación exitosa)")
        else:
            message = (
                f"Verificación post-deploy fallida para {namespace}/{deployment_name}.\n"
                f"Razón: {reason}\n"
                f"RFC {number} requiere revisión manual."
            )
            from clients.camael_client import update_rfc
            await update_rfc(
                sys_id=sys_id,
                result="review",
                message=message,
                deployment=deployment_name,
                namespace=namespace,
            )
            logger.warning(f"[healer] RFC {number} → Review (verificación fallida)")
    except Exception as exc:
        logger.warning(f"[healer] _update_rfc_state error: {exc}")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/agents/sre/test_healer_update_rfc.py -v
pytest tests/contract/test_camael_client_contract.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add agents/sre/healer.py tests/unit/agents/sre/test_healer_update_rfc.py
git commit -m "fix(agents-split): Phase 3.2 — healer delega update_rfc a camael_client (rompe coupling)"
```

---

### Task 5b: Rewire `agents/sre/scheduler.py:420` a `camael_client.handoff_to_camael`

**Files:**
- Modify: `agents/sre/scheduler.py` (línea 420)
- Test: `tests/unit/agents/sre/test_scheduler_handoff.py` (crear)

**Por qué este task existe:** `scheduler.py:420` actualmente llama `healer.handoff_to_camael(...)` directo, que ejecuta GitOps in-process (Bitbucket + ServiceNow local). Para que `CAMAEL_MODE=remote` tenga efecto, el call site debe pasar por el dispatcher de `clients/camael_client.py` (que decide inprocess vs HTTP).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/agents/sre/test_scheduler_handoff.py
"""scheduler.py debe invocar el dispatcher camael_client, no healer directo."""
from __future__ import annotations


def test_scheduler_imports_camael_client_dispatcher(monkeypatch):
    """scheduler.py usa camael_client.handoff_to_camael (no healer.handoff_to_camael)."""
    import importlib
    import agents.sre.scheduler as sched_mod
    source = open(sched_mod.__file__).read()

    # Debe haber al menos una referencia al dispatcher
    assert (
        "camael_client.handoff_to_camael" in source
        or "from clients.camael_client import handoff_to_camael" in source
    ), "scheduler.py debería invocar clients.camael_client.handoff_to_camael"

    # Y NO debe tener el call site directo legacy
    assert "healer.handoff_to_camael(" not in source, (
        "scheduler.py ya NO debe llamar healer.handoff_to_camael directamente "
        "— debe pasar por el dispatcher camael_client"
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/agents/sre/test_scheduler_handoff.py -v
```
Expected: FAIL — source aún tiene `healer.handoff_to_camael(anomaly, anomaly.incident_key, reporter.notify_whatsapp_sre)`.

- [ ] **Step 3: Edit scheduler.py line 420**

```bash
grep -n "handoff_to_camael" agents/sre/scheduler.py
```

Localizar la línea (actualmente 420):

```python
                    healer.handoff_to_camael(anomaly, anomaly.incident_key, reporter.notify_whatsapp_sre)
```

Reemplazar por:

```python
                    from clients.camael_client import handoff_to_camael as _camael_handoff
                    _camael_handoff(anomaly, anomaly.incident_key, reporter.notify_whatsapp_sre)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/agents/sre/test_scheduler_handoff.py -v
```
Expected: PASS

Sanity check — ningún test existente se rompió:

```bash
pytest tests/unit/agents/sre/ -v
```
Expected: all PASS (or skip as before — no new failures).

- [ ] **Step 5: Commit**

```bash
git add agents/sre/scheduler.py tests/unit/agents/sre/test_scheduler_handoff.py
git commit -m "fix(agents-split): Phase 3.2 — scheduler.py usa camael_client dispatcher (no healer directo)"
```

---

### Task 6: Router `/api/camael/*` nuevo (endpoints del futuro camael-service)

**Files:**
- Create: `interfaces/api/routers/camael.py`
- Create: `tests/unit/api/routers/test_camael_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/api/routers/test_camael_router.py
"""Integration tests del router /api/camael/* — endpoints del camael-service."""
from __future__ import annotations

import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    # Forzar secret antes de importar el router
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)

    from interfaces.api.routers.camael import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


AUTH_HEADER = {"Authorization": "Bearer test-secret"}


def test_handoff_requires_auth(client):
    resp = client.post("/api/camael/handoff", json={"incident_key": "x"})
    assert resp.status_code == 401


def test_handoff_happy_path(client, monkeypatch):
    """POST /handoff acepta y delega a agents.devops.agent.handle_handoff."""
    calls = []

    async def fake_handle(payload):
        calls.append(payload)
        return {"pr_id": "PR-42", "rfc_number": "CHG0042"}

    monkeypatch.setattr(
        "agents.devops.agent.handle_handoff", fake_handle
    )

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

    monkeypatch.setattr(
        "agents.devops.agent.handle_handoff", fake_handle
    )

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


def test_update_rfc_invalid_result(client, monkeypatch):
    body = {"result": "pancake", "message": "..."}
    resp = client.patch("/api/camael/rfc/SN-789", json=body, headers=AUTH_HEADER)
    assert resp.status_code == 422  # Pydantic validation error
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/api/routers/test_camael_router.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'interfaces.api.routers.camael'`

- [ ] **Step 3: Create the router**

Create `interfaces/api/routers/camael.py`:

```python
"""
Router /api/camael — endpoints internos del camael-service.

Expone los puntos de entrada que Raphael y backend usan para delegar en Camael
cuando CAMAEL_MODE=remote:

  - POST /api/camael/handoff          — Raphael dispara handoff GitOps
  - PATCH /api/camael/rfc/{sys_id}    — Raphael actualiza RFC post-verificación

Autenticación: Bearer INTERNAL_API_SECRET (mismo esquema que raphael-service).

Nota: NO confundir con /api/devops/* (webhooks GitHub/Bitbucket existentes).
/api/camael/* son endpoints de agente-a-agente; /api/devops/* son webhooks
externos. Ambos viven en el pod camael-service pero tienen consumidores
distintos.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from interfaces.api.auth import require_internal_secret

logger = logging.getLogger("interfaces.api.camael")

router = APIRouter(prefix="/api/camael", tags=["camael"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class HandoffRequest(BaseModel):
    """Request body de POST /api/camael/handoff — contrato con Raphael."""
    incident_key:    str = Field(..., min_length=1, max_length=256)
    issue_type:      str = Field(..., max_length=64)
    severity:        Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "HIGH"
    namespace:       str = Field(..., max_length=128)
    deployment_name: str = Field(..., max_length=253)
    resource_name:   str | None = None
    owner_name:      str | None = None
    reason:          str = Field(..., max_length=2048)
    raphael_action:  str = Field(..., max_length=64)
    triggered_at:    str = Field(..., max_length=64)
    context:         dict[str, Any] = Field(default_factory=dict)


class HandoffResponse(BaseModel):
    accepted: bool
    job_id:   str
    pr_id:    str | None = None
    rfc_number: str | None = None


class RfcUpdateRequest(BaseModel):
    result:     Literal["closed", "review"]
    message:    str = Field(..., max_length=2048)
    deployment: str | None = None
    namespace:  str | None = None


class RfcUpdateResponse(BaseModel):
    sys_id: str
    result: str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/handoff", response_model=HandoffResponse, status_code=202)
async def handoff(
    payload: HandoffRequest,
    _: Annotated[None, Depends(require_internal_secret)],
) -> HandoffResponse:
    """
    Recibe un handoff desde Raphael y lo procesa vía agents.devops.agent.
    `agent.handle_handoff()` es idempotente por incident_key.
    """
    logger.info(
        f"[camael.handoff] incident={payload.incident_key} "
        f"issue={payload.issue_type} ns={payload.namespace} "
        f"deploy={payload.deployment_name}"
    )

    try:
        from agents.devops.agent import handle_handoff
    except ImportError as exc:
        logger.error(f"[camael.handoff] agents.devops.agent unavailable: {exc}")
        raise HTTPException(status_code=503, detail="camael_core_unavailable") from exc

    try:
        result = await handle_handoff(payload.model_dump())
    except Exception as exc:
        logger.error(f"[camael.handoff] handle_handoff FALLÓ: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"camael_error: {exc}") from exc

    if result is None:
        # Issue no soportado (ej. handoff para tipo que Camael no remedia).
        raise HTTPException(status_code=400, detail="issue_type_not_supported")

    return HandoffResponse(
        accepted=True,
        job_id=f"camael-handoff-{payload.incident_key}",
        pr_id=result.get("pr_id"),
        rfc_number=result.get("rfc_number"),
    )


@router.patch("/rfc/{sys_id}", response_model=RfcUpdateResponse)
async def update_rfc(
    sys_id: str,
    payload: RfcUpdateRequest,
    _: Annotated[None, Depends(require_internal_secret)],
) -> RfcUpdateResponse:
    """
    Actualiza el estado del RFC en ServiceNow.
    Invocado por Raphael al terminar la verificación post-deploy.
    """
    logger.info(
        f"[camael.rfc] sys_id={sys_id} result={payload.result} "
        f"deployment={payload.deployment} ns={payload.namespace}"
    )

    try:
        from agents.devops import servicenow_client as sn
    except ImportError as exc:
        logger.error(f"[camael.rfc] servicenow_client unavailable: {exc}")
        raise HTTPException(status_code=503, detail="servicenow_unavailable") from exc

    if not sn.is_configured():
        raise HTTPException(status_code=503, detail="servicenow_not_configured")

    try:
        if payload.result == "closed":
            await sn.close_rfc(sys_id, payload.message)
        elif payload.result == "review":
            await sn.fail_rfc(sys_id, payload.message)
    except Exception as exc:
        logger.error(f"[camael.rfc] servicenow FALLÓ {sys_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"servicenow_error: {exc}") from exc

    return RfcUpdateResponse(sys_id=sys_id, result=payload.result)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/api/routers/test_camael_router.py -v
```
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add interfaces/api/routers/camael.py tests/unit/api/routers/test_camael_router.py
git commit -m "feat(agents-split): Phase 3.2 — router /api/camael/* (handoff + rfc update)"
```

---

### Task 7: Gate en `main.py` backend — no cargar `agents/devops` cuando `CAMAEL_MODE=remote`

**Files:**
- Modify: `main.py` (backend — lifespan startup)
- Test: `tests/integration/test_backend_startup_flags.py` (crear)

- [ ] **Step 1: Inspect current main.py lifespan to find the devops startup hook**

```bash
grep -n "devops\|register_all_agents\|lifespan" main.py | head -20
```
Expected: listado de referencias a `agents.devops` o registros de agentes en startup.

- [ ] **Step 2: Write the failing test**

```python
# tests/integration/test_backend_startup_flags.py
"""Verifica que CAMAEL_MODE=remote no carga agents/devops en el backend."""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch


def test_camael_mode_remote_skips_devops_registration(monkeypatch):
    """Con CAMAEL_MODE=remote, el backend NO debe importar/registrar Camael."""
    # Limpiar imports previos
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("agents.devops"):
            del sys.modules[mod_name]

    env = {
        "CAMAEL_MODE": "remote",
        "AGENTS_MODE": "inprocess",
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=False):
        # Import perezoso de la función gate
        from main import _should_register_devops_inprocess
        assert _should_register_devops_inprocess() is False


def test_camael_mode_inprocess_loads_devops(monkeypatch):
    env = {
        "CAMAEL_MODE": "inprocess",
        "INTERNAL_API_SECRET": "x",
        "JWT_SECRET_KEY": "x" * 32,
        "SESSION_SECRET_KEY": "x" * 32,
    }
    with patch.dict(os.environ, env, clear=False):
        # Fuerza recarga de settings
        import config.settings
        importlib.reload(config.settings)

        from main import _should_register_devops_inprocess
        assert _should_register_devops_inprocess() is True
```

- [ ] **Step 3: Run test — expected failure**

```bash
pytest tests/integration/test_backend_startup_flags.py -v
```
Expected: FAIL — `cannot import name '_should_register_devops_inprocess' from 'main'`

- [ ] **Step 4: Add gate helper + conditional registration in main.py**

Añadir al inicio de `main.py` (después de imports, antes de `lifespan`):

```python
def _should_register_devops_inprocess() -> bool:
    """
    Fase 3 gate: si CAMAEL_MODE=remote, el backend NO registra los hooks de
    agents/devops/ (el pod camael-service los atiende). Si CAMAEL_MODE=inprocess
    (default), sigue comportamiento actual.
    """
    from config.settings import settings
    return settings.camael_mode == "inprocess"
```

Luego buscar en el `lifespan` de `main.py` dónde se registra Camael (grep típico: `register_all_agents()` o importación directa de `agents.devops.agent`). Envolver la(s) llamada(s) que dispara(n) carga de `agents/devops/*`:

```python
# ── Camael registration (gated by CAMAEL_MODE) ──────────────────────────────
if _should_register_devops_inprocess():
    logger.info("[startup] CAMAEL_MODE=inprocess — cargando agents/devops/")
    # Las líneas existentes que cargan Camael van aquí sin cambios.
    # (ej. from agents.devops.agent import ..., register_agent("camael", ...))
else:
    logger.info(
        "[startup] CAMAEL_MODE=remote — agents/devops NO se carga. "
        "Camael corre en camael-service:8003; backend delega vía clients.camael_client."
    )
```

**Importante:** el router `interfaces/api/routers/devops.py` (webhooks GitHub/Bitbucket) sigue registrándose **siempre** en el backend — esos webhooks se reciben aquí aunque Camael esté en otro pod. Solo los hooks que cargan `agents.devops.agent` / servicenow_client / bitbucket_client se gatean.

- [ ] **Step 5: Run tests**

```bash
pytest tests/integration/test_backend_startup_flags.py -v
```
Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
git add main.py tests/integration/test_backend_startup_flags.py
git commit -m "feat(agents-split): Phase 3.2 — backend main.py gate CAMAEL_MODE=remote"
```

---

## Sub-phase 3.3 — Raphael handoff vía WAL + drain consumer

### Task 8: `handoff_to_camael` en raphael usa WAL genérico (reemplaza queue ad-hoc)

**Files:**
- Modify: `clients/camael_client.py` (refactor `_enqueue_fallback` y `drain_pending_handoffs` para usar `storage.redis.wal`)
- Modify: `tests/contract/test_camael_client_contract.py` (ajustar fixture si el key pattern cambió)

- [ ] **Step 1: Check current key pattern in tests**

```bash
grep -n "camael:pending_handoff\|wal:camael:handoff" tests/contract/test_camael_client_contract.py | head
```

- [ ] **Step 2: Write/update the failing test**

Añadir a `tests/contract/test_camael_client_contract.py`:

```python
class TestHandoffUsesWal:
    """Fallback de handoff debe usar storage.redis.wal (no el key legacy ad-hoc)."""

    def test_handoff_enqueues_to_wal_on_network_error(self, monkeypatch, fake_redis):
        from clients import camael_client

        class S:
            camael_mode = "remote"
            agents_mode = "remote"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"

        monkeypatch.setattr(camael_client, "settings", S())

        class FakeClient:
            def post(self, path, json):
                raise ConnectionError("camael down")

        monkeypatch.setattr(
            "clients._http.get_camael_client", lambda: FakeClient()
        )

        camael_client.handoff_to_camael(
            FakeAnomaly(), "oom:demo:amael-ia", lambda m: None
        )

        keys = fake_redis.keys("wal:camael:handoff:*")
        assert keys == ["wal:camael:handoff:oom:demo:amael-ia"]

    def test_drain_uses_wal_module(self, monkeypatch, fake_redis):
        from clients import camael_client
        from storage.redis import wal

        class S:
            camael_mode = "remote"
            agents_mode = "remote"
            internal_api_secret = "x"
            camael_service_url = "http://camael-service:8003"

        monkeypatch.setattr(camael_client, "settings", S())

        # Pre-poblar WAL con un handoff pendiente
        wal.enqueue("handoff", "inc-1", {"incident_key": "inc-1", "foo": "bar"})

        http_calls = []

        class FakeResp:
            status_code = 202
            content = b'{"accepted":true}'
            def json(self):
                return {"accepted": True}

        class FakeClient:
            def post(self, path, json):
                http_calls.append((path, json))
                return FakeResp()

        monkeypatch.setattr(
            "clients._http.get_camael_client", lambda: FakeClient()
        )

        count = camael_client.drain_pending_handoffs()

        assert count == 1
        assert http_calls[0][0] == "/api/camael/handoff"
        # WAL debe quedar vacío
        assert fake_redis.keys("wal:camael:handoff:*") == []
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/contract/test_camael_client_contract.py::TestHandoffUsesWal -v
```
Expected: FAIL — fallback aún usa `camael:pending_handoff:*`

- [ ] **Step 4: Refactor `clients/camael_client.py`**

Reemplazar `_enqueue_fallback` (líneas 77-90) y `drain_pending_handoffs` (líneas 198-238) por:

```python
def _enqueue_fallback(incident_key: str, payload: dict[str, Any]) -> bool:
    """Encola el handoff en el WAL para replay posterior."""
    from storage.redis import wal
    return wal.enqueue("handoff", incident_key, payload)
```

Y:

```python
def drain_pending_handoffs() -> int:
    """
    Drena handoffs encolados en el WAL reintentando POST /api/camael/handoff.
    Llamado al arranque de camael-service y cada 5min (APScheduler tick).
    """
    if _is_inprocess():
        return 0

    from storage.redis import wal

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
    except Exception as exc:
        logger.error(f"[camael_client] drain: http client FALLÓ: {exc}")
        return 0

    def _consume(payload: dict[str, Any]) -> bool:
        try:
            resp = client.post("/api/camael/handoff", json=payload)
            if resp.status_code in (200, 202):
                logger.info(
                    f"[camael_client] drain: re-enviado {payload.get('incident_key')}"
                )
                return True
            if resp.status_code == 400:
                # Issue no soportado — evitar loop infinito, aceptar el drain.
                logger.info(
                    f"[camael_client] drain: descartado (400) {payload.get('incident_key')}"
                )
                return True
            return False
        except Exception as exc:
            logger.warning(f"[camael_client] drain consume FALLÓ: {exc}")
            return False

    return wal.drain("handoff", _consume)


def drain_pending_rfc_updates() -> int:
    """
    Drena actualizaciones de RFC encoladas en el WAL.
    Llamado al arranque de camael-service y cada 5min.
    """
    if _is_inprocess():
        return 0

    from storage.redis import wal

    try:
        from clients._http import get_camael_client
        client = get_camael_client()
    except Exception as exc:
        logger.error(f"[camael_client] drain_rfc: http client FALLÓ: {exc}")
        return 0

    def _consume(payload: dict[str, Any]) -> bool:
        # sys_id viene del key original — hay que reconstruirlo desde el payload
        # O bien: incluir sys_id DENTRO del payload al encolar (decisión: lo incluimos).
        sys_id = payload.get("_sys_id") or payload.get("sys_id")
        if not sys_id:
            logger.warning("[camael_client] drain_rfc: payload sin sys_id")
            return True  # descartar, no reintentar indefinidamente
        try:
            body = {k: v for k, v in payload.items() if k != "_sys_id"}
            resp = client.patch(f"/api/camael/rfc/{sys_id}", json=body)
            if resp.status_code in (200, 204, 404):
                return True
            return False
        except Exception as exc:
            logger.warning(f"[camael_client] drain_rfc consume FALLÓ: {exc}")
            return False

    return wal.drain("rfc_update", _consume)
```

**Ajuste en `update_rfc` para incluir `_sys_id` en payload WAL:**

Edit `update_rfc` — reemplazar la línea `wal.enqueue("rfc_update", sys_id, payload)` por:

```python
        from storage.redis import wal
        wal_payload = {**payload, "_sys_id": sys_id}
        wal.enqueue("rfc_update", sys_id, wal_payload)
```

También eliminar las constantes y helper legacy ya obsoletos al inicio del archivo:

```python
# Eliminar:
# _REDIS_PENDING_KEY = "camael:pending_handoff:{incident_key}"
# _REDIS_PENDING_TTL = 3600
```

Y actualizar `get_pending_handoff_count()` para usar el nuevo namespace:

```python
def get_pending_handoff_count() -> int:
    """Número de handoffs pendientes en el WAL (para dashboard / alertas)."""
    from storage.redis import wal
    return wal.pending_count("handoff")
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/contract/test_camael_client_contract.py -v
pytest tests/unit/storage/redis/test_wal.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add clients/camael_client.py tests/contract/test_camael_client_contract.py
git commit -m "refactor(agents-split): Phase 3.3 — camael_client usa storage.redis.wal (unifica topic handoff + rfc_update)"
```

---

## Sub-phase 3.4 — Kubernetes manifests de camael-service

### Task 9: ConfigMap + Deployment + Service + PDB

**Files:**
- Create: `k8s/agents/15-camael-deployment.yaml`
- Modify: `k8s/agents/kustomization.yaml` (añadir `15-camael-deployment.yaml`)

- [ ] **Step 1: Create deployment manifest**

Crear `k8s/agents/15-camael-deployment.yaml`. Basarse en `13-raphael-deployment.yaml` pero con:
- `name: camael-service`, `port: 8003`, `image: registry.richardx.dev/camael-service:1.0.0`
- `serviceAccountName: camael-sa` (ya existe en `k8s/rbac/06-camael-rbac.yaml`)
- Extra secrets: `BITBUCKET_*`, `SERVICENOW_*` (no `MINIO_*`)
- Env var gate: `CAMAEL_MODE=inprocess` (el pod mismo siempre corre Camael en proceso; el flag solo afecta a sus clientes externos)
- Liveness/readiness `/health`

```yaml
# camael-service — DevOps / GitOps Agent autónomo (extraído del backend)
#
# Fase 3 · feature/agents-split — ver docs/superpowers/specs/2026-04-23-phase-3-camael-standalone-design.md
#
# Este servicio absorbe agents/devops/ del backend amael-agentic-backend
# como pod independiente. Expone:
#   - POST /api/camael/handoff       ← Raphael (handoff GitOps)
#   - PATCH /api/camael/rfc/{sys_id} ← Raphael (update RFC post-verif)
#   - POST /api/devops/ci-hook       ← GitHub webhooks
#   - POST /api/devops/webhook/bitbucket ← Bitbucket webhooks
#
# Dependencias externas (ya existentes):
#   - ServiceAccount `camael-sa`           (k8s/rbac/06-camael-rbac.yaml)
#   - NetworkPolicies con label app=camael-service (k8s/config/03-agents-network-policies.yaml — verificar)
#   - Secret `amael-secrets` en amael-ia    (POSTGRES_PASSWORD, INTERNAL_API_SECRET, etc.)
#   - Secret `bitbucket-credentials` en amael-ia (BITBUCKET_*)
#   - Secret `servicenow-credentials` en amael-ia (SERVICENOW_*)

apiVersion: v1
kind: ConfigMap
metadata:
  name: camael-service-config
  namespace: amael-ia
  labels:
    app: camael-service
    tier: agents
data:
  OLLAMA_BASE_URL:  "http://ollama-service:11434"
  LLM_PROVIDER:     "ollama"
  LLM_MODEL:        "qwen3:14b"
  LLM_EMBED_MODEL:  "nomic-embed-text"

  POSTGRES_HOST:     "postgres-service"
  POSTGRES_PORT:     "5432"
  POSTGRES_DB:       "amael_db"
  POSTGRES_USER:     "amael_user"
  POSTGRES_POOL_MIN: "2"
  POSTGRES_POOL_MAX: "10"

  REDIS_HOST: "redis-service"
  REDIS_PORT: "6379"
  REDIS_DB:   "0"

  QDRANT_URL: "http://qdrant-service:6333"

  VAULT_ADDR: "http://vault.vault.svc.cluster.local:8200"
  VAULT_ROLE: "camael-service"

  OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector.observability.svc.cluster.local:4317"
  OTEL_SERVICE_NAME:           "camael-service"

  # camael-service siempre corre Camael in-process; el flag sólo afecta a sus
  # CLIENTES externos (backend, raphael) que lo consumen via HTTP.
  CAMAEL_MODE:  "inprocess"
  AGENTS_MODE:  "inprocess"

  ENVIRONMENT: "production"
  LOG_LEVEL:   "INFO"

  # Bitbucket / ServiceNow: host, workspace y repo son públicos (no secretos).
  BITBUCKET_WORKSPACE: "amael_agenticia"
  BITBUCKET_REPO:      "amael-agentic-backend"
  SERVICENOW_INSTANCE: "https://dev123456.service-now.com"  # TODO: verificar con secret real

---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: camael-service
  namespace: amael-ia
  labels:
    app: camael-service
    tier: agents
    version: "1.0.0"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: camael-service
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  template:
    metadata:
      labels:
        app: camael-service
        tier: agents
        version: "1.0.0"
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port:   "8003"
        prometheus.io/path:   "/metrics"
    spec:
      serviceAccountName: camael-sa
      securityContext:
        fsGroup: 1000
      terminationGracePeriodSeconds: 30

      initContainers:
        - name: wait-for-postgres
          image: busybox:1.36
          command: ["sh","-c","until nc -z postgres-service 5432; do echo waiting pg; sleep 3; done"]
        - name: wait-for-redis
          image: busybox:1.36
          command: ["sh","-c","until nc -z redis-service 6379; do echo waiting redis; sleep 3; done"]

      containers:
        - name: camael
          image: registry.richardx.dev/camael-service:1.0.0
          imagePullPolicy: IfNotPresent
          ports:
            - name: http
              containerPort: 8003

          envFrom:
            - configMapRef:
                name: camael-service-config

          env:
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: amael-secrets
                  key: POSTGRES_PASSWORD
            - name: INTERNAL_API_SECRET
              valueFrom:
                secretKeyRef:
                  name: google-auth-secret
                  key: internal_api_secret
            - name: JWT_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: google-auth-secret
                  key: jwt_secret_key
            - name: SESSION_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: google-auth-secret
                  key: session_secret_key

            - name: BITBUCKET_USERNAME
              valueFrom:
                secretKeyRef:
                  name: bitbucket-credentials
                  key: username
            - name: BITBUCKET_TOKEN
              valueFrom:
                secretKeyRef:
                  name: bitbucket-credentials
                  key: token
            - name: BITBUCKET_WORKSPACE
              valueFrom:
                secretKeyRef:
                  name: bitbucket-credentials
                  key: workspace

            - name: SERVICENOW_USERNAME
              valueFrom:
                secretKeyRef:
                  name: servicenow-credentials
                  key: username
            - name: SERVICENOW_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: servicenow-credentials
                  key: password

            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_NAMESPACE
              valueFrom:
                fieldRef:
                  fieldPath: metadata.namespace

          resources:
            requests:
              cpu:    "100m"
              memory: "256Mi"
            limits:
              cpu:    "500m"
              memory: "512Mi"

          livenessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 45
            periodSeconds:       30
            timeoutSeconds:      5
            failureThreshold:    3

          readinessProbe:
            httpGet:
              path: /health
              port: http
            initialDelaySeconds: 15
            periodSeconds:       10
            timeoutSeconds:      3
            failureThreshold:    2

          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities:
              drop:
                - ALL

---
apiVersion: v1
kind: Service
metadata:
  name: camael-service
  namespace: amael-ia
  labels:
    app: camael-service
    tier: agents
    monitor: amael-metrics
spec:
  type: ClusterIP
  selector:
    app: camael-service
  ports:
    - name:       http
      protocol:   TCP
      port:       8003
      targetPort: http

---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: camael-service-pdb
  namespace: amael-ia
  labels:
    app: camael-service
    tier: agents
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: camael-service
```

- [ ] **Step 2: Verify secrets exist in cluster before adding them to env**

```bash
kubectl get secret -n amael-ia bitbucket-credentials -o jsonpath='{.data}' 2>&1 | head -c 200; echo
kubectl get secret -n amael-ia servicenow-credentials -o jsonpath='{.data}' 2>&1 | head -c 200; echo
```

Si `servicenow-credentials` no existe, flaggearlo en el checkpoint go/no-go pre-3.6 (hay que crear el secret antes del deploy).

- [ ] **Step 3: Add manifest to kustomization**

Edit `k8s/agents/kustomization.yaml`. Añadir `15-camael-deployment.yaml` a la lista de resources (preservar orden existente).

- [ ] **Step 4: Dry-run apply**

```bash
kubectl apply --dry-run=client -f k8s/agents/15-camael-deployment.yaml -n amael-ia
```
Expected: todos los recursos `configured (dry run)` sin errores de schema.

- [ ] **Step 5: Commit**

```bash
git add k8s/agents/15-camael-deployment.yaml k8s/agents/kustomization.yaml
git commit -m "feat(agents-split): Phase 3.4 — K8s manifests para camael-service"
```

---

### Task 10: NetworkPolicy — permitir backend/raphael → camael-service

**Files:**
- Modify: `k8s/config/03-agents-network-policies.yaml`
- Test: manual (kubectl apply dry-run)

- [ ] **Step 1: Read current policies**

```bash
cat k8s/config/03-agents-network-policies.yaml
```

- [ ] **Step 2: Add policy stanza for camael-service**

Append al final del archivo (separar con `---`):

```yaml
---
# camael-service: solo admite tráfico del backend y de raphael-service
# (ambos lo invocan para /api/camael/* cuando CAMAEL_MODE=remote).
# Webhooks externos (GitHub, Bitbucket) entran por el ingress que sí queda abierto a Internet
# — para este plan, la superficie de webhooks sigue siendo el backend (no se muda en Fase 3).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: camael-service-ingress
  namespace: amael-ia
spec:
  podSelector:
    matchLabels:
      app: camael-service
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: amael-agentic-backend
        - podSelector:
            matchLabels:
              app: raphael-service
      ports:
        - protocol: TCP
          port: 8003

---
# camael-service egress — puede alcanzar Postgres, Redis, Qdrant, Ollama,
# Bitbucket (api.bitbucket.org:443), ServiceNow (*.service-now.com:443),
# y DNS (kube-dns).
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: camael-service-egress
  namespace: amael-ia
spec:
  podSelector:
    matchLabels:
      app: camael-service
  policyTypes:
    - Egress
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: postgres
      ports:
        - protocol: TCP
          port: 5432
    - to:
        - podSelector:
            matchLabels:
              app: redis
      ports:
        - protocol: TCP
          port: 6379
    - to:
        - podSelector:
            matchLabels:
              app: qdrant
      ports:
        - protocol: TCP
          port: 6333
    - to:
        - podSelector:
            matchLabels:
              app: ollama
      ports:
        - protocol: TCP
          port: 11434
    # kube-dns
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
          podSelector:
            matchLabels:
              k8s-app: kube-dns
      ports:
        - protocol: UDP
          port: 53
    # Bitbucket + ServiceNow (HTTPS outbound)
    - ports:
        - protocol: TCP
          port: 443
```

- [ ] **Step 3: Dry-run**

```bash
kubectl apply --dry-run=client -f k8s/config/03-agents-network-policies.yaml -n amael-ia
```
Expected: cada NetworkPolicy `configured (dry run)`.

- [ ] **Step 4: Commit**

```bash
git add k8s/config/03-agents-network-policies.yaml
git commit -m "feat(agents-split): Phase 3.4 — NetworkPolicies camael-service ingress/egress"
```

---

## Sub-phase 3.5 — `camael_service/` scaffolding + Dockerfile + imagen

### Task 11: `camael_service/main.py` entry point

**Files:**
- Create: `camael_service/__init__.py` (vacío)
- Create: `camael_service/main.py`
- Create: `tests/integration/camael_service/test_main_lifespan.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/camael_service/test_main_lifespan.py
"""Verifica que camael_service.main construye app y drena WAL al arrancar."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env():
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret")
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ["CAMAEL_MODE"] = "inprocess"
    os.environ["POSTGRES_HOST"] = "localhost"
    os.environ["REDIS_HOST"] = "localhost"
    yield


def test_app_has_camael_router_registered():
    """La app de camael_service debe montar /api/camael/*."""
    from camael_service.main import app

    paths = [r.path for r in app.routes]
    assert any("/api/camael/handoff" in p for p in paths)
    assert any("/api/camael/rfc/" in p for p in paths)


def test_health_endpoint_responds():
    from camael_service.main import app
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200


def test_metrics_endpoint_mounted():
    from camael_service.main import app
    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert b"python_info" in resp.content or b"process_cpu" in resp.content
```

- [ ] **Step 2: Run tests to verify failure**

```bash
pytest tests/integration/camael_service/test_main_lifespan.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'camael_service'`

- [ ] **Step 3: Create the module**

```bash
mkdir -p camael_service tests/integration/camael_service
touch camael_service/__init__.py tests/integration/camael_service/__init__.py
```

Create `camael_service/main.py`:

```python
"""
main.py — Entry point de camael-service (DevOps / GitOps Agent standalone).

camael-service es un microservicio FastAPI independiente que empaqueta
la lógica del agente Camael (agents.devops) extraída del backend
amael-agentic-backend en Fase 3.

Expone:
  - POST /api/camael/handoff      ← Raphael dispara handoff GitOps
  - PATCH /api/camael/rfc/{sys_id} ← Raphael update RFC post-verif
  - POST /api/devops/ci-hook      ← GitHub webhook
  - POST /api/devops/webhook/bitbucket ← Bitbucket webhook
  - GET /health                   ← Liveness/Readiness
  - GET /metrics                  ← Prometheus metrics

Secuencia de arranque:
  1. Logging estructurado
  2. PostgreSQL pool
  3. Redis client
  4. Drain WAL pendiente (handoffs + rfc_update que quedaron en Redis mientras
     Camael estaba caído — se procesan ANTES de aceptar tráfico nuevo)
  5. APScheduler con tick cada 5min para re-drenar WAL (backup ante nuevos fallos)
  6. OTel instrumentation + Prometheus metrics

Arranque:
    uvicorn camael_service.main:app --host 0.0.0.0 --port 8003
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app

from observability.logging import setup_logging

setup_logging()
logger = logging.getLogger("camael_service.main")

_scheduler = None  # APScheduler instance (module-level para shutdown clean)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown de camael-service."""
    global _scheduler

    logger.info("=== camael-service iniciando ===")
    from config.settings import settings

    # 1. PostgreSQL pool
    try:
        from storage.postgres.client import init_pool
        init_pool(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            min_conn=settings.postgres_pool_min,
            max_conn=settings.postgres_pool_max,
        )
        logger.info("[startup] PostgreSQL pool inicializado")
    except Exception as exc:
        logger.error(f"[startup] PostgreSQL FALLÓ: {exc}", exc_info=True)

    # 2. Redis
    try:
        from storage.redis.client import init_client
        init_client(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
        )
        logger.info("[startup] Redis client inicializado")
    except Exception as exc:
        logger.error(f"[startup] Redis FALLÓ: {exc}", exc_info=True)

    # 3. Drain WAL pendiente (ANTES de aceptar tráfico)
    # El drain se hace contra el propio camael-service (localhost:8003),
    # pero en este pod `_is_inprocess()` devolverá True (CAMAEL_MODE=inprocess
    # en la ConfigMap), así que `drain_pending_handoffs()` es no-op aquí.
    # En cambio procesamos directamente llamando a agents.devops.agent.
    try:
        await _drain_wal_local()
    except Exception as exc:
        logger.warning(f"[startup] drain WAL inicial falló: {exc}")

    # 4. APScheduler tick cada 5min
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(
            _drain_wal_local,
            trigger=IntervalTrigger(minutes=5),
            id="camael-wal-drain",
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logger.info("[startup] APScheduler tick 5min registrado")
    except Exception as exc:
        logger.warning(f"[startup] APScheduler falló: {exc}")

    # 5. OTel
    try:
        from observability.tracing import instrument_app, instrument_requests
        instrument_app(app)
        instrument_requests()
        logger.info("[startup] OpenTelemetry instrumentado")
    except Exception as exc:
        logger.warning(f"[startup] OTel falló: {exc}")

    logger.info("=== camael-service listo en :8003 ===")
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("=== camael-service apagando ===")
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
    try:
        from storage.postgres.client import close_pool
        close_pool()
    except Exception:
        pass
    logger.info("=== camael-service apagado ===")


async def _drain_wal_local() -> None:
    """
    Drena entradas WAL procesándolas localmente (NO vía HTTP).
    Este pod es camael-service, así que atiende el WAL directamente.
    """
    from storage.redis import wal

    # Handoffs
    def _consume_handoff(payload: dict) -> bool:
        try:
            import asyncio
            from agents.devops.agent import handle_handoff
            result = asyncio.run(handle_handoff(payload)) if not asyncio.get_event_loop().is_running() \
                     else asyncio.get_event_loop().run_until_complete(handle_handoff(payload))
            # si es None (issue no soportado) lo descartamos igual
            return True
        except Exception as exc:
            logger.warning(f"[wal-drain] handoff FALLÓ: {exc}")
            return False

    # RFC updates
    def _consume_rfc(payload: dict) -> bool:
        try:
            import asyncio
            from agents.devops import servicenow_client as sn
            if not sn.is_configured():
                return False
            sys_id = payload.get("_sys_id") or payload.get("sys_id")
            if not sys_id:
                return True  # drop malformed
            result = payload.get("result")
            message = payload.get("message", "")
            if result == "closed":
                asyncio.run(sn.close_rfc(sys_id, message))
            elif result == "review":
                asyncio.run(sn.fail_rfc(sys_id, message))
            return True
        except Exception as exc:
            logger.warning(f"[wal-drain] rfc_update FALLÓ: {exc}")
            return False

    drained_h = wal.drain("handoff", _consume_handoff)
    drained_r = wal.drain("rfc_update", _consume_rfc)
    if drained_h or drained_r:
        logger.info(f"[wal-drain] handoff={drained_h} rfc_update={drained_r}")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    from config.settings import settings

    app = FastAPI(
        title="camael-service",
        description=(
            "Microservicio DevOps/GitOps. Absorbe agents/devops/ del backend. "
            "Expone /api/camael/* (handoff + rfc update) y /api/devops/* (webhooks)."
        ),
        version="1.0.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    from observability.middleware import ObservabilityMiddleware
    app.add_middleware(ObservabilityMiddleware)

    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    from observability.health import build_health_router
    hr = build_health_router()
    if hr is not None:
        app.include_router(hr)
    else:
        @app.get("/health")
        def _fallback_health():
            return {"status": "ok", "service": "camael-service"}

    # Router interno Raphael ↔ Camael
    from interfaces.api.routers.camael import router as camael_router
    app.include_router(camael_router)

    # Router webhooks externos (GitHub, Bitbucket)
    from interfaces.api.routers.devops import router as devops_router
    app.include_router(devops_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "camael_service.main:app",
        host="0.0.0.0",
        port=8003,
        reload=True,
        log_config=None,
    )
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/integration/camael_service/test_main_lifespan.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add camael_service/ tests/integration/camael_service/
git commit -m "feat(agents-split): Phase 3.5 — camael_service/main.py scaffolding + WAL drain"
```

---

### Task 12: Dockerfile + build + push imagen `camael-service:1.0.0`

**Files:**
- Create: `camael_service/Dockerfile`

- [ ] **Step 1: Inspect raphael_service Dockerfile as reference**

```bash
cat raphael_service/Dockerfile
```

- [ ] **Step 2: Create `camael_service/Dockerfile` (copia adaptada)**

```dockerfile
# camael-service — DevOps / GitOps Agent standalone
# Base: misma imagen que raphael-service (monorepo compartido)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencias del sistema mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copiamos pyproject primero para aprovechar cache layer
COPY pyproject.toml ./
COPY README.md ./

# Instala dependencias del monorepo — ver pyproject.toml [project.optional-dependencies]
# Incluye agents/devops/* imports (httpx, anthropic, etc.)
RUN pip install --upgrade pip && \
    pip install -e ".[all]"

# Copiamos el código fuente después del pip install para que cambios en código
# no invaliden la capa de dependencias.
COPY agents/            ./agents/
COPY clients/           ./clients/
COPY camael_service/    ./camael_service/
COPY config/            ./config/
COPY core/              ./core/
COPY interfaces/        ./interfaces/
COPY observability/     ./observability/
COPY raphael_service/   ./raphael_service/
COPY skills/            ./skills/
COPY storage/           ./storage/
COPY tools/             ./tools/
COPY llm/               ./llm/

# Usuario no-root
RUN useradd --create-home --uid 1000 amael && \
    chown -R amael:amael /app
USER amael

EXPOSE 8003

CMD ["uvicorn", "camael_service.main:app", "--host", "0.0.0.0", "--port", "8003"]
```

- [ ] **Step 3: Build locally**

```bash
docker build -f camael_service/Dockerfile -t registry.richardx.dev/camael-service:1.0.0 .
```
Expected: build exitoso, imagen ~500MB.

- [ ] **Step 4: Sanity check — imagen arranca en local**

```bash
docker run --rm -d --name camael-test \
  -p 8003:8003 \
  -e INTERNAL_API_SECRET=test \
  -e JWT_SECRET_KEY=$(python3 -c 'print("x"*32)') \
  -e SESSION_SECRET_KEY=$(python3 -c 'print("x"*32)') \
  -e POSTGRES_HOST=postgres-service -e POSTGRES_USER=amael_user \
  -e POSTGRES_PASSWORD=dummy -e POSTGRES_DB=amael_db \
  -e REDIS_HOST=redis-service \
  registry.richardx.dev/camael-service:1.0.0

sleep 3
docker logs camael-test --tail 50
# health es OK aun si PG/Redis no resuelven (fallback stub):
curl -sf http://localhost:8003/health; echo
docker rm -f camael-test
```
Expected: logs muestran `=== camael-service listo en :8003 ===` (con warnings de PG/Redis connect esperables en local), `/health` responde 200.

- [ ] **Step 5: Push to registry**

```bash
docker push registry.richardx.dev/camael-service:1.0.0
```
Expected: push exitoso.

- [ ] **Step 6: Commit Dockerfile**

```bash
git add camael_service/Dockerfile
git commit -m "feat(agents-split): Phase 3.5 — Dockerfile camael-service:1.0.0"
```

---

## Sub-phase 3.6 — Deploy + canary + verificación E2E

### Task 13: Aplicar manifests y verificar pod sano

- [ ] **Step 1: Verify Vault role exists for camael-service**

```bash
kubectl exec -it -n vault vault-0 -- vault read auth/kubernetes/role/camael-service 2>&1 | head -20
```
If "No role found": crear antes de deploy (block 3.6).

```bash
# Solo si falta: crear role en Vault (requires VAULT_TOKEN)
kubectl exec -it -n vault vault-0 -- vault write auth/kubernetes/role/camael-service \
  bound_service_account_names=camael-sa \
  bound_service_account_namespaces=amael-ia \
  policies=amael-camael \
  ttl=1h
```

- [ ] **Step 2: Apply NetworkPolicies first**

```bash
kubectl apply -f k8s/config/03-agents-network-policies.yaml -n amael-ia
```
Expected: `camael-service-ingress created`, `camael-service-egress created`.

- [ ] **Step 3: Apply RBAC (si no está aplicado)**

```bash
kubectl apply -f k8s/rbac/06-camael-rbac.yaml
```
Expected: `serviceaccount camael-sa unchanged` (ya existe de Fase 1) u otros `unchanged`.

- [ ] **Step 4: Apply deployment manifest**

```bash
kubectl apply -f k8s/agents/15-camael-deployment.yaml -n amael-ia
kubectl rollout status deployment/camael-service -n amael-ia --timeout=180s
```
Expected: `deployment "camael-service" successfully rolled out`.

- [ ] **Step 5: Verify pod is Ready and responds to /health**

```bash
kubectl get pods -n amael-ia -l app=camael-service -o wide
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://camael-service:8003/health', timeout=10)
print(json.dumps(json.load(r), indent=2))
"
```
Expected: pod `1/1 Running`, `/health` returns `{"status": "ok", ...}`.

- [ ] **Step 6: If pod NOT Ready, debug and block**

```bash
kubectl describe pod -n amael-ia -l app=camael-service | tail -50
kubectl logs -n amael-ia deploy/camael-service --tail=100
```
Do NOT proceed with canary flip until pod is Ready and /health is 200.

---

### Task 14: Contract test E2E en cluster (backend y raphael → camael)

- [ ] **Step 1: Test from backend pod**

```bash
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import urllib.request, json, os
secret = os.environ['INTERNAL_API_SECRET']
req = urllib.request.Request(
    'http://camael-service:8003/api/camael/handoff',
    data=json.dumps({
        'incident_key':    'test-e2e-backend-1',
        'issue_type':      'OOM_KILLED',
        'severity':        'HIGH',
        'namespace':       'amael-ia',
        'deployment_name': 'amael-demo-oom',
        'reason':          'E2E test from backend',
        'raphael_action':  'ROLLOUT_RESTART',
        'triggered_at':    '2026-04-23T00:00:00Z',
        'context':         {'_e2e_test': True},
    }).encode(),
    headers={
        'Authorization': f'Bearer {secret}',
        'Content-Type': 'application/json',
    },
    method='POST',
)
try:
    resp = urllib.request.urlopen(req, timeout=20)
    print('STATUS:', resp.status)
    print(resp.read().decode())
except urllib.error.HTTPError as e:
    print('HTTP', e.code, e.reason)
    print(e.read().decode())
"
```
Expected: `STATUS: 202` y payload `{"accepted": true, ...}` **O** `STATUS: 400 issue_type_not_supported` si el flag `_e2e_test: true` no está soportado por el agent. Ambos son OK (confirma que el endpoint vive y la auth funciona).

- [ ] **Step 2: Test from raphael-service pod**

```bash
kubectl exec -n amael-ia deploy/raphael-service -- python3 -c "
import urllib.request, json, os
secret = os.environ['INTERNAL_API_SECRET']
req = urllib.request.Request(
    'http://camael-service:8003/api/camael/handoff',
    data=json.dumps({
        'incident_key':    'test-e2e-raphael-1',
        'issue_type':      'CRASH_LOOP',
        'severity':        'HIGH',
        'namespace':       'amael-ia',
        'deployment_name': 'amael-demo-crashloop',
        'reason':          'E2E test from raphael',
        'raphael_action':  'ROLLOUT_RESTART',
        'triggered_at':    '2026-04-23T00:00:00Z',
        'context':         {'_e2e_test': True},
    }).encode(),
    headers={
        'Authorization': f'Bearer {secret}',
        'Content-Type': 'application/json',
    },
    method='POST',
)
try:
    resp = urllib.request.urlopen(req, timeout=20)
    print('STATUS:', resp.status)
except urllib.error.HTTPError as e:
    print('HTTP', e.code)
    print(e.read().decode())
"
```
Expected: similar — 202 o 400 documentado. **Si falla con connection refused / timeout: NetworkPolicy bloqueando. Abortar canary.**

- [ ] **Step 3: Verify camael-service logs show the requests**

```bash
kubectl logs -n amael-ia deploy/camael-service --tail=30 | grep -i "camael.handoff\|test-e2e"
```
Expected: 2 líneas con `[camael.handoff] incident=test-e2e-...`.

- [ ] **Step 4: Clean up test data in camael**

Si el handoff procesó exitosamente (no 400), puede haber creado un PR de test en Bitbucket. Verificar manualmente y cerrar si aplica:

```bash
kubectl exec -n amael-ia deploy/camael-service -- python3 -c "
import redis, os
r = redis.Redis(host=os.environ.get('REDIS_HOST','redis-service'), port=6379, db=0, decode_responses=True)
for pattern in ['bb:pending_pr:test-e2e-*', 'sre:gitops:test-e2e-*']:
    for k in r.keys(pattern):
        r.delete(k); print(f'deleted {k}')
"
```

---

### Task 15: Canary flip — `CAMAEL_MODE=remote` en backend

- [ ] **Step 1: Capture baseline — qué está siendo servido hoy por backend**

```bash
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- printenv | grep -E "^(CAMAEL|AGENTS)_MODE="
```
Expected: `AGENTS_MODE=remote` (ya está de Fase 2). `CAMAEL_MODE` ausente → default inprocess.

- [ ] **Step 2: Set CAMAEL_MODE=remote on backend**

```bash
kubectl set env deployment/amael-agentic-deployment -n amael-ia \
  --containers=amael-agentic-backend CAMAEL_MODE=remote
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia --timeout=120s
```

- [ ] **Step 3: Verify gate applied in backend logs**

```bash
kubectl logs -n amael-ia deploy/amael-agentic-deployment --tail=100 | grep -i "camael_mode\|agents.devops"
```
Expected: línea `[startup] CAMAEL_MODE=remote — agents/devops NO se carga. Camael corre en camael-service:8003`.

- [ ] **Step 4: Smoke test backend endpoints de devops (webhooks siguen funcionando)**

```bash
# Webhook GitHub sigue en backend (no se muda) — verificar que la ruta existe
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://localhost:8000/health', timeout=5)
print(json.load(r))
"
```
Expected: `{"status": "ok"}` — backend sano.

---

### Task 16: Canary flip — `CAMAEL_MODE=remote` en raphael-service

- [ ] **Step 1: Set flag on raphael**

```bash
kubectl set env deployment/raphael-service -n amael-ia \
  --containers=raphael CAMAEL_MODE=remote
kubectl rollout status deployment/raphael-service -n amael-ia --timeout=120s
```

- [ ] **Step 2: Verify raphael loop sigue sano + ahora usa cliente remoto**

```bash
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import urllib.request, json
r = urllib.request.urlopen('http://raphael-service:8002/api/sre/loop/status', timeout=10)
print(json.dumps(json.load(r), indent=2))
"
```
Expected: `loop_enabled: true`, `is_leader: true`, `last_run_result: ok` — Raphael sigue funcionando.

- [ ] **Step 3: Verify raphael logs no muestran imports de agents.devops**

```bash
kubectl logs -n amael-ia deploy/raphael-service --tail=200 | grep -iE "agents.devops|from agents\.devops"
```
Expected: **vacío**. Si aparece algo → gate no aplicado, abortar.

---

### Task 17: End-to-end verificación post-canary (handoff real via demo)

- [ ] **Step 1: Pre-check — Redis WAL vacío**

```bash
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import redis, os
r = redis.Redis(host=os.environ.get('REDIS_HOST','redis-service'), port=6379, db=0, decode_responses=True)
for t in ['handoff', 'rfc_update']:
    keys = r.keys(f'wal:camael:{t}:*')
    print(f'{t}: {len(keys)} pending')
"
```
Expected: `handoff: 0 pending`, `rfc_update: 0 pending`.

- [ ] **Step 2: Trigger a demo OOM scenario (si hay tiempo y manifest disponible)**

> Nota: este paso es **opcional** en el plan. Sirve como validación funcional completa antes de declarar Fase 3 cerrada. Si no hay tiempo en esta sesión, saltar a step 4 y marcar "validación funcional pospuesta" en el commit de cierre.

Activar el demo OOM (reset completo) siguiendo el flow de CLAUDE.md "Demo Reset":

```bash
# Poner replicas=1 en Bitbucket + aplicar manifest local
kubectl apply -f k8s/agents/06-demo-oom.yaml -n amael-ia
```

Esperar ~2 min (ciclo SRE de 60s + detección + handoff).

```bash
# Ver logs de raphael durante la detección
kubectl logs -n amael-ia deploy/raphael-service --tail=200 | grep -E "OOM|handoff|camael_client" | tail -40
```
Expected: línea `[camael_client] handoff OK oom:amael-demo-oom:amael-ia ... pr_id=PR-XX`.

- [ ] **Step 3: Verify camael-service procesó el handoff**

```bash
kubectl logs -n amael-ia deploy/camael-service --tail=200 | grep -E "camael.handoff|amael-demo-oom"
```
Expected: `[camael.handoff] incident=oom:amael-demo-oom:amael-ia issue=OOM_KILLED ns=amael-ia deploy=amael-demo-oom`.

- [ ] **Step 4: Verify WAL is still empty (no fallbacks triggered)**

```bash
kubectl exec -n amael-ia deploy/amael-agentic-deployment -- python3 -c "
import redis, os
r = redis.Redis(host=os.environ.get('REDIS_HOST','redis-service'), port=6379, db=0, decode_responses=True)
for t in ['handoff', 'rfc_update']:
    keys = r.keys(f'wal:camael:{t}:*')
    print(f'{t}: {len(keys)} pending — {keys}')
"
```
Expected: 0 pending en ambos topics.

- [ ] **Step 5: Final pod health panel**

```bash
kubectl get pods -n amael-ia -l 'app in (amael-agentic-backend,raphael-service,camael-service,k8s-agent)' -o wide
kubectl get deploy -n amael-ia | grep -E 'amael-agentic|raphael|camael|k8s-agent'
kubectl get lease sre-agentic-leader -n amael-ia -o jsonpath='{.spec.holderIdentity}'; echo
```
Expected:
- 4 deployments sanos (backend, raphael-service, camael-service, k8s-agent)
- Lease holder = raphael-service-... (Raphael sigue siendo líder)

---

### Task 18: Commit de cierre Fase 3

- [ ] **Step 1: Bump backend version**

Edit `k8s/agents/05-backend-deployment.yaml` — encontrar la línea `image: registry.richardx.dev/amael-agentic-backend:1.11.0` y cambiar a `1.11.1`.

Luego build + push de imagen backend con los cambios acumulados de fases 3.1-3.3:

```bash
docker build -t registry.richardx.dev/amael-agentic-backend:1.11.1 .
docker push registry.richardx.dev/amael-agentic-backend:1.11.1
kubectl apply -f k8s/agents/05-backend-deployment.yaml -n amael-ia
kubectl rollout status deployment/amael-agentic-deployment -n amael-ia --timeout=180s
```

- [ ] **Step 2: Update top-level CLAUDE.md to reflect new service**

Edit `/home/richardx/k8s-lab/CLAUDE.md` añadiendo `camael-service` a la tabla de imágenes (línea cerca de `raphael-service`) y documentando `CAMAEL_MODE` en el bloque de feature flags.

- [ ] **Step 3: Run full test suite as sanity check**

```bash
pytest tests/ -x -q 2>&1 | tail -20
```
Expected: todos los tests pasan.

- [ ] **Step 4: Final commit**

```bash
git add k8s/agents/05-backend-deployment.yaml /home/richardx/k8s-lab/CLAUDE.md
git commit -m "$(cat <<'EOF'
feat(agents-split): Phase 3.6 — canary CAMAEL_MODE=remote aplicado

Fase 3 cerrada. camael-service corre aislado en su propio pod (imagen
camael-service:1.0.0) y recibe tráfico de:
  - backend (amael-agentic-deployment 1.11.1) cuando CAMAEL_MODE=remote
  - raphael-service (1.0.1) para handoff + update_rfc

Verificación end-to-end:
  - Lease sre-agentic-leader sigue en raphael-service
  - POST /api/camael/handoff OK desde backend y raphael
  - wal:camael:handoff y wal:camael:rfc_update vacíos (sin fallbacks activos)
  - Raphael logs confirman agents.devops NO importado
  - Backend logs confirman CAMAEL_MODE=remote gate activo

Rollback:
  kubectl set env deployment/amael-agentic-deployment -n amael-ia CAMAEL_MODE=inprocess
  kubectl set env deployment/raphael-service        -n amael-ia CAMAEL_MODE=inprocess
  kubectl scale deployment/camael-service           -n amael-ia --replicas=0

Deuda pendiente (fuera de scope, documentada en spec §10):
  - 403 de Raphael sobre prometheus-kube-prometheus-stack-prometheus
  - Dashboards Grafana Camael (Fase 4)
  - Duplicación de credenciales Bitbucket/SN en backend (se quita en Fase 5)

Fase 3 cerrada. Siguiente: Fase 4 (observabilidad Camael) o Fase 5 (cleanup).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Criterios de aceptación (de `docs/superpowers/specs/2026-04-23-phase-3-camael-standalone-design.md` §12)

- [ ] `camael-service` pod `1/1 Running` estable ≥24h
- [ ] ≥1 handoff real completado vía HTTP (log confirma en raphael + camael)
- [ ] ≥1 RFC cerrado vía `PATCH /api/camael/rfc/{id}` post-verificación exitosa (opcional — depende de si demo OOM se ejecutó)
- [ ] `CAMAEL_MODE=remote` aplicado en backend y raphael
- [ ] `redis_db_keys{pattern="wal:camael:*"} == 0` en estado estable
- [ ] Contract tests pasando (`pytest tests/contract/test_camael_client_contract.py`)
- [ ] Commit de cierre con bump de versión backend (`1.11.x`)
