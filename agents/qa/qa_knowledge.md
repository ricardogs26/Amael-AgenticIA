# Catálogo de pruebas del cluster — base de conocimiento de Phanuel

Este documento es la memoria de Phanuel: qué se prueba en cada componente, con
qué comando, y qué correr después de tocar qué. Se carga al arrancar (patrón
`_load_kb`, igual que Raphael con `vault_knowledge.md`).

> **Regla de oro**: tras un cambio, corre el subconjunto que lo cubre, no todo.
> «Todo» es para antes de un merge a `main`. El desglose «cuándo correr qué» de
> abajo es lo que Phanuel usa para recomendar.

---

## Resumen por componente

| Componente | Repo | Tests | CI | Disparo de Phanuel |
|---|---|---|---|---|
| **amael-agentic-backend** | `ricardogs26/Amael-AgenticIA` | 521 unit + 2 integration + 2 contract | `ci.yml` (workflow_dispatch) | `run tests backend` |
| **trader-service** | `ricardogs26/trader-service` | 107 unit | `ci.yml` (workflow_dispatch) | `run tests trader` |
| **frontend-next** | `ricardogs26/amael-ia` | `tsc` + `next build` (sin unit) | — | manual |
| **whatsapp-bridge** | `ricardogs26/amael-ia` | `node --check` (sintaxis) | — | manual |

**Raphael y Camael** comparten la suite de `Amael-AgenticIA` (mismo repo): sus
tests viven en `tests/unit/agents/sre/` y `tests/unit/agents/devops/`.

---

## amael-agentic-backend (521 tests)

**Comando raíz** (lo que corre el CI):
```bash
pytest tests/ -m "not e2e" --tb=short -q
ruff check .
```

**Suites y qué cubren:**

| Área | Ruta | Cubre |
|---|---|---|
| SRE (Raphael) | `tests/unit/agents/sre/` (16 archivos) | observer (OOM/lápidas, HIGH_RESTARTS, deployment degraded), healer (rollback, RFC), consolidador (fusión + drenaje), commands, watchdog |
| Agentes | `tests/unit/agents/` (9) | scheduler/Cassiel, memoria/Zaphkiel (contexto, importancia, consolidación, búsqueda), skills procedurales, grouper, day-planner |
| Grafo | `tests/unit/test_agent_graph.py` | topología derivada del código, tráfico, capa de conocimiento |
| Routers API | `tests/unit/api/routers/` (3) | camael handoff, planner opt-in, identity pairing |
| Observabilidad | `tests/unit/test_prometheus_url.py` | URL de Prometheus, NaN/Inf en SLO |
| DevOps (Camael) | `tests/unit/agents/devops/` (2) | discovery de manifests, github client |
| Seguridad | `tests/unit/test_security.py`, `test_validator_extended.py` | prompt injection, límites |
| Integration | `tests/integration/` | lifespan de camael_service (rutas montadas) |
| Contract | `tests/contract/` | clientes raphael/camael (contratos entre servicios) |

**Cuándo correr qué:**
- Tocaste `agents/sre/observer.py` o `healer.py` → `pytest tests/unit/agents/sre/ -q`
- Tocaste memoria (Zaphkiel) → `pytest tests/unit/agents/test_memory_*.py tests/unit/agents/test_session_search.py -q`
- Tocaste el scheduler (Cassiel) → `pytest tests/unit/agents/test_scheduler.py -q`
- Tocaste skills procedurales → `pytest tests/unit/skills/ -q`
- Tocaste el grafo de agentes → `pytest tests/unit/test_agent_graph.py -q`
- Antes de merge a `main` → todo: `pytest tests/ -m "not e2e" -q` + `ruff check .`

**Gotchas de la suite** (fallos que NO son del cambio):
- Los tests instalan `.[all]`, no `.[dev]`: `apscheduler` vive en el extra `sre`.
- 6 tests de `test_camael_router` y `test_bitbucket_discovery` fueron
  orden-dependientes hasta 1.15.3 (settings singleton) — ya resueltos.
- Requiere entorno mínimo: `POSTGRES_HOST`, `JWT_SECRET_KEY` (≥28 chars), etc.
  (el CI los inyecta; en local, ver `tests/conftest`).

---

## trader-service (107 tests)

**Comando raíz:**
```bash
PYTHONPATH="$PWD" pytest tests/ --tb=short -q
ruff check .
```
> `PYTHONPATH=.` porque el paquete no se instala (solo `requirements.txt`, sin
> pyproject). Los tests son **unit puros**: mockean broker (Alpaca), LLM y DB.

**Suites y qué cubren:**

| Archivo | Cubre |
|---|---|
| `test_analyzer_degenerate.py` | respuesta degenerada del LLM → camino de error; guard de presupuesto de prompt; NO se manda num_ctx cliente (la «ruleta del runner») |
| `test_analyzer_retry.py` | reintento solo cuando Ollama no escucha, no en JSON malo |
| `test_confidence_metrics.py` | métrica de confianza separada por action (operar vs hold) |
| `test_daily_quota.py` | cupo diario por clase de activo |
| `test_economics.py` | comisiones reales, P&L por operación |
| `test_entry_flags.py` | señales calculadas en código (candidato_buy, umbral_6h…) |
| `test_split_cycles.py` | un ciclo por clase (cripto/bolsa no compiten) |
| `test_symbol_cooldown.py` | cooldown por símbolo (churn) |
| `test_target_cost_gate.py` | puerta de rentabilidad neta |
| `test_orphan_positions.py` | posiciones fuera de whitelist entran solo para vender |
| `test_*_persistence.py` / `test_orders_hold_filter.py` | persistencia de fees/holds, filtro de holds |
| `test_prompt_thresholds.py` | el prompt cita los umbrales del policy engine |

**Cuándo correr qué:**
- Tocaste `agents/trader/analyzer.py` → `pytest tests/test_analyzer_*.py tests/test_prompt_thresholds.py -q`
- Tocaste `policy.py` → `pytest tests/test_daily_quota.py tests/test_target_cost_gate.py tests/test_symbol_cooldown.py -q`
- Tocaste `loop.py` / ciclos → `pytest tests/test_split_cycles.py tests/test_orphan_positions.py -q`
- Antes de desplegar a mano → todo (opera dinero real, sin excepción).

---

## frontend-next (sin unit tests)

**Comando raíz:**
```bash
npx tsc --noEmit    # type check
npx next build      # build de producción
```
No hay Jest/Vitest. La validación es que compile y buildee. Tras tocar
`app/admin/graph/` o `catalog.ts`, correr ambos antes de desplegar.

---

## whatsapp-bridge (sintaxis)

**Comando raíz:**
```bash
node --check index.js
```
El bridge corre `index.js` desde el ConfigMap `whatsapp-bridge-code`, no la
imagen — un error de sintaxis tumba el pod. `node --check` es el gate mínimo
antes de aplicar el ConfigMap.

---

## Cómo Phanuel ejecuta (multi-repo)

`run tests <componente>` dispara el `workflow_dispatch` del `ci.yml` del repo
correspondiente y hace polling del resultado:

- `backend` / `amael` / `agentic` → `Amael-AgenticIA`
- `trader` → `trader-service`

Componentes sin CI (frontend, bridge): Phanuel da el comando exacto de arriba
para correrlo a mano — no hay workflow que disparar todavía.
