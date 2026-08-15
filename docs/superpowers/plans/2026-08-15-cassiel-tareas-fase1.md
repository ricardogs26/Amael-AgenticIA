# Cassiel Tareas — Fase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cassiel captura tareas pendientes por WhatsApp, las lista priorizadas (`/pendientes`), permite cerrarlas/cancelarlas/posponerlas conversando, y manda nudges diarios según prioridad.

**Architecture:** Nueva tabla `user_tasks` con CRUD en `agents/scheduler/tasks_storage.py` (archivo nuevo — `storage.py` se queda con `user_jobs`). El LLM de Cassiel aprende acciones de tarea en su JSON (solo traduce; validación y matching en código). Los nudges son un cron job 9:00 México en el AsyncIOScheduler existente de `runner.py`, con lock Redis entre réplicas y entrega por el bridge.

**Tech Stack:** Python 3.11, psycopg2 (patrón `get_connection()` existente), ChatOllama `format=json reasoning=False`, APScheduler, Redis, pytest (`asyncio_mode=auto`).

**Spec:** `docs/superpowers/plans/../specs/2026-08-15-cassiel-tareas-design.md`

## Global Constraints

- Validación SIEMPRE en código; el LLM solo traduce (lección trader 1.0.31 / memoria `project_llm_no_compara_numeros`).
- `user_id` viene del JWT/contexto, JAMÁS del JSON del LLM.
- SQL: valores parametrizados; f-strings solo para columnas constantes con `# nosec B608` (patrón de `storage.py`).
- Enums: `category ∈ {personal, laboral}`, `priority ∈ {alta, media, baja}`, `status ∈ {pending, done, cancelled}`.
- Tope: 50 tareas `pending` por usuario (`TASKS_MAX_PENDING_PER_USER` env).
- Orden efectivo: vencida/hoy > alta > media > baja; desempate por `due_date` asc, luego `created_at` asc.
- Nudges: alta=diario, media=cada 3 días, baja=nunca; máx 1/tarea/día y 3/usuario/día.
- Ruff: line-length 100. Tests con `pytest tests/unit/agents/test_cassiel_tasks.py -q`.
- Commits pequeños por task; NO subir la versión del manifest hasta el task final (el push a main dispara CI+deploy).

---

### Task 1: tasks_storage — DDL, dataclass y create con validación

**Files:**
- Create: `agents/scheduler/tasks_storage.py`
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Produces: `Task` dataclass; `init_tasks_db()`; `validate_task_fields(category, priority, status) -> None` (pura, lanza ValueError); `create_task(user_id, title, *, description="", category="personal", priority="media", estimated_minutes=None, due_date=None, needs_scheduling=False) -> Task`; constantes `CATEGORIES`, `PRIORITIES`, `STATUSES`, `MAX_PENDING_PER_USER`.

- [ ] **Step 1: Write failing tests (validación pura + tope)**

```python
# tests/unit/agents/test_cassiel_tasks.py
"""Tests de Cassiel tareas pendientes (Fase 1). Toda la lógica decidible
se prueba PURA (sin DB): validación, orden, matching, nudges."""
from __future__ import annotations

from datetime import date, datetime, timedelta, UTC

import pytest

from agents.scheduler import tasks_storage as ts


def _task(**kw):
    base = dict(
        id=1, user_id="u@x.com", title="t", description="", category="personal",
        priority="media", estimated_minutes=None, due_date=None, status="pending",
        needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
        created_at=datetime(2026, 8, 1, tzinfo=UTC), completed_at=None,
    )
    base.update(kw)
    return ts.Task(**base)


class TestValidacion:
    def test_valores_correctos_pasan(self):
        ts.validate_task_fields("personal", "alta", "pending")  # no lanza

    @pytest.mark.parametrize("cat,prio,status", [
        ("trabajo", "alta", "pending"),      # category inventada por el LLM
        ("personal", "urgente", "pending"),  # priority inventada
        ("laboral", "media", "abierta"),     # status inventado
    ])
    def test_valores_inventados_lanzan(self, cat, prio, status):
        with pytest.raises(ValueError):
            ts.validate_task_fields(cat, prio, status)
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/unit/agents/test_cassiel_tasks.py -q`
Expected: FAIL `ModuleNotFoundError` / `AttributeError`

- [ ] **Step 3: Implement `tasks_storage.py` (DDL + dataclass + validación + create)**

```python
"""
Storage de tareas pendientes — tabla `user_tasks` en PostgreSQL.

Hermana de storage.py (user_jobs): un job es un cron que ejecuta un prompt;
una tarea tiene estado, prioridad y ciclo de vida. Toda decisión validable
vive AQUÍ, no en el prompt de Cassiel.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

logger = logging.getLogger("agents.scheduler.tasks_storage")

CATEGORIES = ("personal", "laboral")
PRIORITIES = ("alta", "media", "baja")
STATUSES   = ("pending", "done", "cancelled")
MAX_PENDING_PER_USER = int(os.environ.get("TASKS_MAX_PENDING_PER_USER", "50"))

_DDL = """
CREATE TABLE IF NOT EXISTS user_tasks (
    id                SERIAL PRIMARY KEY,
    user_id           TEXT NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    category          TEXT NOT NULL DEFAULT 'personal'
                      CHECK (category IN ('personal','laboral')),
    priority          TEXT NOT NULL DEFAULT 'media'
                      CHECK (priority IN ('alta','media','baja')),
    estimated_minutes INT,
    due_date          DATE,
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','done','cancelled')),
    needs_scheduling  BOOLEAN NOT NULL DEFAULT FALSE,
    calendar_event_id TEXT,
    last_nudge_at     TIMESTAMPTZ,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_user_tasks_user ON user_tasks (user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_tasks_due  ON user_tasks (status, due_date);
"""


@dataclass
class Task:
    id: int
    user_id: str
    title: str
    description: str
    category: str
    priority: str
    estimated_minutes: int | None
    due_date: date | None
    status: str
    needs_scheduling: bool
    calendar_event_id: str | None
    last_nudge_at: datetime | None
    created_at: datetime
    completed_at: datetime | None


def init_tasks_db() -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
    logger.info("[tasks] Tabla user_tasks lista.")


def validate_task_fields(category: str, priority: str, status: str) -> None:
    """El LLM propone estos campos; aquí se decide si son válidos."""
    if category not in CATEGORIES:
        raise ValueError(f"Categoría inválida: {category!r} ({'|'.join(CATEGORIES)})")
    if priority not in PRIORITIES:
        raise ValueError(f"Prioridad inválida: {priority!r} ({'|'.join(PRIORITIES)})")
    if status not in STATUSES:
        raise ValueError(f"Estado inválido: {status!r} ({'|'.join(STATUSES)})")


_COLS = ("id, user_id, title, description, category, priority, estimated_minutes, "
         "due_date, status, needs_scheduling, calendar_event_id, last_nudge_at, "
         "created_at, completed_at")

_INSERT_SQL = f"""
    INSERT INTO user_tasks
        (user_id, title, description, category, priority, estimated_minutes,
         due_date, needs_scheduling)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING {_COLS}
"""  # nosec B608
_LIST_SQL = f"""
    SELECT {_COLS} FROM user_tasks
    WHERE user_id = %s AND status = 'pending'
    ORDER BY id
"""  # nosec B608


def _row_to_task(row: tuple) -> Task:
    return Task(*row)


def create_task(
    user_id: str, title: str, *, description: str = "",
    category: str = "personal", priority: str = "media",
    estimated_minutes: int | None = None, due_date: date | None = None,
    needs_scheduling: bool = False,
) -> Task:
    from storage.postgres.client import get_connection

    validate_task_fields(category, priority, "pending")
    if not title.strip():
        raise ValueError("La tarea necesita un título.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM user_tasks WHERE user_id = %s AND status = 'pending'",
                (user_id,),
            )
            if cur.fetchone()[0] >= MAX_PENDING_PER_USER:
                raise ValueError(
                    f"Ya tienes {MAX_PENDING_PER_USER} pendientes — cierra o "
                    f"cancela algunas antes de anotar más."
                )
            cur.execute(
                _INSERT_SQL,
                (user_id, title.strip()[:120], description[:1000], category,
                 priority, estimated_minutes, due_date, needs_scheduling),
            )
            task = _row_to_task(cur.fetchone())
        conn.commit()
    logger.info(f"[tasks] Tarea #{task.id} creada para {user_id}: {task.title!r}")
    return task


def list_pending(user_id: str) -> list[Task]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_LIST_SQL, (user_id,))
            return [_row_to_task(r) for r in cur.fetchall()]
```

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** `git commit -m "feat(cassiel): tabla user_tasks + validación en código"`

---

### Task 2: Orden determinista de prioridad

**Files:**
- Modify: `agents/scheduler/tasks_storage.py`
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Produces: `sort_key(task: Task, today: date) -> tuple`; `sorted_pending(tasks: list[Task], today: date) -> list[Task]`.

- [ ] **Step 1: Failing tests**

```python
class TestOrden:
    def test_vencida_gana_a_alta_sin_fecha(self):
        hoy = date(2026, 8, 15)
        vencida_baja = _task(id=1, priority="baja", due_date=date(2026, 8, 10))
        alta_sin_fecha = _task(id=2, priority="alta", due_date=None)
        orden = ts.sorted_pending([alta_sin_fecha, vencida_baja], hoy)
        assert [t.id for t in orden] == [1, 2]

    def test_desempate_por_fecha_mas_proxima(self):
        hoy = date(2026, 8, 15)
        lejana  = _task(id=1, priority="media", due_date=date(2026, 8, 30))
        proxima = _task(id=2, priority="media", due_date=date(2026, 8, 20))
        orden = ts.sorted_pending([lejana, proxima], hoy)
        assert [t.id for t in orden] == [2, 1]

    def test_prioridad_ordena_sin_fechas(self):
        hoy = date(2026, 8, 15)
        tareas = [_task(id=1, priority="baja"), _task(id=2, priority="alta"),
                  _task(id=3, priority="media")]
        assert [t.id for t in ts.sorted_pending(tareas, hoy)] == [2, 3, 1]
```

- [ ] **Step 2: Run → FAIL** (`sorted_pending` no existe)
- [ ] **Step 3: Implement**

```python
def sort_key(task: Task, today: date) -> tuple:
    """Orden EFECTIVO (spec §1): vencida/hoy > alta > media > baja;
    desempate por due_date más próxima, luego created_at."""
    overdue = task.due_date is not None and task.due_date <= today
    return (
        0 if overdue else 1,
        PRIORITIES.index(task.priority),
        task.due_date or date.max,
        task.created_at,
    )


def sorted_pending(tasks: list[Task], today: date) -> list[Task]:
    return sorted(tasks, key=lambda t: sort_key(t, today))
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git commit -m "feat(cassiel): orden determinista de pendientes"`

---

### Task 3: Matching y transiciones (cerrar / cancelar / posponer)

**Files:**
- Modify: `agents/scheduler/tasks_storage.py`
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Produces: `match_tasks(tasks: list[Task], ref: str) -> list[Task]` (pura); `find_task(user_id, ref) -> Task | None` (lanza ValueError con candidatos si hay 2+, patrón de `storage.find_job`); `set_status(task_id, user_id, status) -> bool` (fija `completed_at` si done); `postpone_task(task_id, user_id, new_due: date) -> bool` (resetea `last_nudge_at` a NULL).

- [ ] **Step 1: Failing tests**

```python
class TestMatch:
    def test_id_numerico(self):
        tareas = [_task(id=7, title="comprar café")]
        assert ts.match_tasks(tareas, "7") == tareas

    def test_substring_case_insensitive(self):
        tareas = [_task(id=1, title="Revisar contrato de renta"),
                  _task(id=2, title="Comprar café")]
        assert [t.id for t in ts.match_tasks(tareas, "café")] == [2]

    def test_ambiguedad_devuelve_todos(self):
        tareas = [_task(id=1, title="llamar al banco"),
                  _task(id=2, title="pagar el banco")]
        assert len(ts.match_tasks(tareas, "banco")) == 2

    def test_sin_coincidencia_lista_vacia(self):
        assert ts.match_tasks([_task(id=1, title="x")], "zzz") == []
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**

```python
def match_tasks(tasks: list[Task], ref: str) -> list[Task]:
    ref = ref.strip()
    if ref.isdigit():
        return [t for t in tasks if t.id == int(ref)]
    return [t for t in tasks if ref.lower() in t.title.lower()]


def find_task(user_id: str, ref: str) -> Task | None:
    """1 candidato → Task; 0 → None; 2+ → ValueError con opciones (como
    find_job: adivinar cuál cerrar es peor que preguntar)."""
    candidatos = match_tasks(list_pending(user_id), ref)
    if len(candidatos) > 1:
        opciones = ", ".join(f"#{t.id} {t.title!r}" for t in candidatos)
        raise ValueError(f"Hay varias pendientes que coinciden con {ref!r}: {opciones}")
    return candidatos[0] if candidatos else None


def set_status(task_id: int, user_id: str, status: str) -> bool:
    validate_task_fields("personal", "media", status)  # solo valida status
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET status = %s, "
                "completed_at = CASE WHEN %s = 'done' THEN NOW() ELSE completed_at END "
                "WHERE id = %s AND user_id = %s AND status = 'pending'",
                (status, status, task_id, user_id),
            )
            cambiado = cur.rowcount > 0
        conn.commit()
    return cambiado


def postpone_task(task_id: int, user_id: str, new_due: date) -> bool:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET due_date = %s, last_nudge_at = NULL "
                "WHERE id = %s AND user_id = %s AND status = 'pending'",
                (new_due, task_id, user_id),
            )
            cambiado = cur.rowcount > 0
        conn.commit()
    return cambiado
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git commit -m "feat(cassiel): match y transiciones de tareas"`

---

### Task 4: Selección de nudges (pura) + marcado

**Files:**
- Modify: `agents/scheduler/tasks_storage.py`
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Produces: `nudge_eligible(task: Task, today: date) -> bool` (pura); `select_nudges(tasks: list[Task], today: date, cap: int = 3) -> list[Task]` (pura); `mark_nudged(task_id: int) -> None`; `all_pending_with_due() -> list[Task]` (todas las pendientes con due_date, todos los usuarios).

- [ ] **Step 1: Failing tests**

```python
class TestNudges:
    HOY = date(2026, 8, 15)

    def test_alta_vencida_elegible_cada_dia(self):
        t = _task(priority="alta", due_date=date(2026, 8, 10))
        assert ts.nudge_eligible(t, self.HOY)

    def test_media_cada_3_dias(self):
        t3 = _task(priority="media", due_date=date(2026, 8, 12))  # 3 días
        t2 = _task(priority="media", due_date=date(2026, 8, 13))  # 2 días
        hoy_mismo = _task(priority="media", due_date=self.HOY)
        assert ts.nudge_eligible(t3, self.HOY)
        assert not ts.nudge_eligible(t2, self.HOY)
        assert ts.nudge_eligible(hoy_mismo, self.HOY)   # el día que toca, siempre

    def test_baja_nunca_nudge_salvo_hoy(self):
        vencida = _task(priority="baja", due_date=date(2026, 8, 1))
        hoy = _task(priority="baja", due_date=self.HOY)
        assert not ts.nudge_eligible(vencida, self.HOY)
        assert ts.nudge_eligible(hoy, self.HOY)

    def test_ya_nudgeada_hoy_no_repite(self):
        t = _task(priority="alta", due_date=date(2026, 8, 10),
                  last_nudge_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC))
        assert not ts.nudge_eligible(t, self.HOY)

    def test_sin_fecha_no_nudge(self):
        assert not ts.nudge_eligible(_task(priority="alta", due_date=None), self.HOY)

    def test_cap_gana_lo_prioritario(self):
        tareas = [
            _task(id=1, priority="baja", due_date=self.HOY),
            _task(id=2, priority="alta", due_date=date(2026, 8, 1)),
            _task(id=3, priority="alta", due_date=date(2026, 8, 5)),
            _task(id=4, priority="media", due_date=self.HOY),
        ]
        sel = ts.select_nudges(tareas, self.HOY, cap=3)
        assert [t.id for t in sel] == [2, 3, 4]
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Implement**

```python
def nudge_eligible(task: Task, today: date) -> bool:
    """Spec §3: el día del due SIEMPRE avisa; vencidas: alta diario,
    media cada 3 días, baja solo brief (nunca nudge suelto).
    Máx 1 nudge por tarea al día vía last_nudge_at."""
    if task.status != "pending" or task.due_date is None or task.due_date > today:
        return False
    if task.last_nudge_at is not None and task.last_nudge_at.date() >= today:
        return False
    if task.due_date == today:
        return True
    dias_vencida = (today - task.due_date).days
    if task.priority == "alta":
        return True
    if task.priority == "media":
        return dias_vencida % 3 == 0
    return False


def select_nudges(tasks: list[Task], today: date, cap: int = 3) -> list[Task]:
    elegibles = [t for t in tasks if nudge_eligible(t, today)]
    return sorted_pending(elegibles, today)[:cap]


def mark_nudged(task_id: int) -> None:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_tasks SET last_nudge_at = NOW() WHERE id = %s",
                (task_id,),
            )
        conn.commit()


_ALL_DUE_SQL = f"""
    SELECT {_COLS} FROM user_tasks
    WHERE status = 'pending' AND due_date IS NOT NULL
"""  # nosec B608


def all_pending_with_due() -> list[Task]:
    from storage.postgres.client import get_connection
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_ALL_DUE_SQL)
            return [_row_to_task(r) for r in cur.fetchall()]
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** `git commit -m "feat(cassiel): selección de nudges con escalado y tope"`

---

### Task 5: Cassiel — acciones de tarea en el LLM y `_apply`

**Files:**
- Modify: `agents/scheduler/agent.py` (`_SYSTEM`, `_apply`, helper Redis de pregunta pendiente)
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Consumes: todo `tasks_storage` de Tasks 1–4.
- Produces: acciones nuevas del JSON: `task_create`, `task_list`, `task_done`, `task_cancel`, `task_postpone`; helpers `_set_pending_question(user, task_id, field)`, `_pop_pending_question(user) -> dict | None` (Redis TTL 600, clave `task:pending_question:<user>`).

- [ ] **Step 1: Extender `_SYSTEM`** — añadir al esquema (mismo bloque, después de `"clarification"`):

```
  "action": ... | "task_create" | "task_list" | "task_done" | "task_cancel" | "task_postpone",
  "task": {                        // solo task_create
    "title": "corto", "description": "",
    "category": "personal" | "laboral",
    "priority": "alta" | "media" | "baja",
    "estimated_minutes": 30,
    "due_date": "YYYY-MM-DD" | null,
    "needs_scheduling": false
  },
  "task_ref": "id o parte del título",   // done/cancel/postpone
  "new_due": "YYYY-MM-DD",               // solo task_postpone
  "filter": "personal" | "laboral" | "hoy" | null   // task_list
```

Y a las reglas:

```
- Tarea pendiente SIN horario recurrente («tengo que», «necesito», «anota»,
  «recuérdame X» sin cuándo) → task_create. Con cron/hora explícita → create (job).
- Infiere category/priority/estimated_minutes con sentido común; due_date SOLO
  si el usuario dio fecha — no la inventes.
- «ya lo hice / ya compré X» → task_done con task_ref. «cancela» → task_cancel.
  «mejor el lunes» → task_postpone con new_due.
- «/pendientes» o «qué tengo pendiente» → task_list (filter si lo dijo).
```

- [ ] **Step 2: Failing tests de `_apply` con dicts** (sin LLM — se prueba el código):

```python
class TestApply:
    @pytest.fixture
    def agent(self, monkeypatch):
        from agents.scheduler.agent import CassielAgent
        a = CassielAgent.__new__(CassielAgent)   # sin __init__: solo se usa _apply
        return a

    def test_task_create_valida_y_confirma(self, agent, monkeypatch):
        creado = {}
        def fake_create(user_id, title, **kw):
            creado.update(user_id=user_id, title=title, **kw)
            return _task(id=9, title=title, **{k: v for k, v in kw.items()
                                               if k in ("category", "priority")})
        monkeypatch.setattr(ts, "create_task", fake_create)
        out = agent._apply(
            {"action": "task_create",
             "task": {"title": "comprar café", "category": "personal",
                      "priority": "baja", "estimated_minutes": 15}},
            "u@x.com", "America/Mexico_City",
        )
        assert creado["user_id"] == "u@x.com" and "café" in out

    def test_task_create_categoria_inventada_no_revienta(self, agent, monkeypatch):
        # El LLM inventó "trabajo": create_task lanza ValueError y _apply
        # la convierte en respuesta legible (patrón execute() actual).
        def boom(*a, **k):
            raise ValueError("Categoría inválida: 'trabajo'")
        monkeypatch.setattr(ts, "create_task", boom)
        out = agent._apply(
            {"action": "task_create", "task": {"title": "x", "category": "trabajo"}},
            "u@x.com", "America/Mexico_City",
        )
        assert "inválida" in out.lower() or "categoría" in out.lower()

    def test_task_done_ambiguo_pregunta(self, agent, monkeypatch):
        def ambiguo(user_id, ref):
            raise ValueError("Hay varias pendientes que coinciden con 'banco': …")
        monkeypatch.setattr(ts, "find_task", ambiguo)
        out = agent._apply({"action": "task_done", "task_ref": "banco"},
                           "u@x.com", "America/Mexico_City")
        assert "varias" in out.lower()

    def test_task_list_ordena_y_filtra(self, agent, monkeypatch):
        tareas = [_task(id=1, title="a", category="laboral", priority="baja"),
                  _task(id=2, title="b", category="personal", priority="alta")]
        monkeypatch.setattr(ts, "list_pending", lambda u: tareas)
        out = agent._apply({"action": "task_list", "filter": "personal"},
                           "u@x.com", "America/Mexico_City")
        assert "b" in out and "a" not in out
```

Nota de implementación: `_apply` importa `tasks_storage` con `from agents.scheduler
import tasks_storage` a nivel función (patrón actual con `storage`) — los tests
monkeypatchean `agents.scheduler.tasks_storage.<fn>`.

- [ ] **Step 3: Run → FAIL**
- [ ] **Step 4: Implement — ramas nuevas en `_apply`** (después de la rama `"list"`):

```python
        if action == "task_create":
            t = parsed.get("task") or {}
            titulo = str(t.get("title") or "").strip()
            if not titulo:
                return "¿Qué tarea anoto? Dímela en una frase corta."
            due = None
            if t.get("due_date"):
                due = date.fromisoformat(str(t["due_date"]))   # ValueError → legible
            task = tasks_storage.create_task(
                user_id=user_email, title=titulo,
                description=str(t.get("description") or ""),
                category=str(t.get("category") or "personal"),
                priority=str(t.get("priority") or "media"),
                estimated_minutes=(int(t["estimated_minutes"])
                                   if t.get("estimated_minutes") else None),
                due_date=due,
                needs_scheduling=bool(t.get("needs_scheduling")),
            )
            extra = ""
            if due is None and task.priority == "alta":
                self._set_pending_question(user_email, task.id, "due_date")
                extra = " ¿Para cuándo la necesitas?"
            mins = f", ~{task.estimated_minutes} min" if task.estimated_minutes else ""
            return (f"📝 Anotada #{task.id}: *{task.title}* — {task.category}, "
                    f"prioridad {task.priority}{mins}."
                    f"{f' Fecha: {due}.' if due else ''}{extra}")

        if action == "task_list":
            tareas = tasks_storage.list_pending(user_email)
            filtro = str(parsed.get("filter") or "").lower() or None
            hoy = datetime.now(ZoneInfo(tz_name)).date()
            if filtro in ("personal", "laboral"):
                tareas = [t for t in tareas if t.category == filtro]
            elif filtro == "hoy":
                tareas = [t for t in tareas
                          if t.due_date is not None and t.due_date <= hoy]
            if not tareas:
                return "No tienes pendientes. 🎉" if not filtro else \
                       f"Sin pendientes con filtro {filtro!r}."
            lineas = []
            for t in tasks_storage.sorted_pending(tareas, hoy):
                marca = ("🔴" if t.due_date and t.due_date < hoy else
                         "🟡" if t.due_date == hoy else "•")
                fecha = f" — para {t.due_date}" if t.due_date else ""
                mins = f" (~{t.estimated_minutes}m)" if t.estimated_minutes else ""
                lineas.append(f"{marca} #{t.id} {t.title} [{t.category}/"
                              f"{t.priority}]{mins}{fecha}")
            return "Tus pendientes:\n" + "\n".join(lineas)

        if action in ("task_done", "task_cancel", "task_postpone"):
            ref = str(parsed.get("task_ref") or "").strip()
            if not ref:
                return "¿Cuál pendiente? Dame su número o parte del nombre."
            task = tasks_storage.find_task(user_email, ref)   # ValueError si 2+
            if not task:
                return f"No encontré ningún pendiente que coincida con {ref!r}."
            if action == "task_done":
                tasks_storage.set_status(task.id, user_email, "done")
                return f"✅ Cerrada #{task.id}: *{task.title}*."
            if action == "task_cancel":
                tasks_storage.set_status(task.id, user_email, "cancelled")
                return f"🗑 Cancelada #{task.id}: *{task.title}*."
            nueva = date.fromisoformat(str(parsed.get("new_due") or ""))
            tasks_storage.postpone_task(task.id, user_email, nueva)
            return f"⏭ #{task.id} *{task.title}* pospuesta al {nueva}."
```

Y los helpers de pregunta pendiente (en `agent.py`):

```python
    _PENDING_TTL_S = 600

    def _set_pending_question(self, user: str, task_id: int, field: str) -> None:
        try:
            from storage.redis.client import get_redis_client
            get_redis_client().setex(
                f"task:pending_question:{user}", self._PENDING_TTL_S,
                json.dumps({"task_id": task_id, "field": field}),
            )
        except Exception as exc:
            logger.debug(f"[cassiel] pending_question no guardada: {exc}")

    def _pop_pending_question(self, user: str) -> dict | None:
        try:
            from storage.redis.client import get_redis_client
            raw = get_redis_client().getdel(f"task:pending_question:{user}")
            return json.loads(raw) if raw else None
        except Exception:
            return None
```

En `execute()`, ANTES de `_parse_intent`: si hay pregunta pendiente, se anexa al
contexto del LLM: `"Pregunta pendiente al usuario: fecha para la tarea
#<id>. Si este mensaje la responde, action=task_postpone con task_ref=<id> y
new_due."` (el pop es GETDEL: si el mensaje no la responde, simplemente se
pierde la pregunta — spec §2.5).

`date`/`ZoneInfo` ya están importados en `agent.py` (líneas 22-24).

- [ ] **Step 5: Run → PASS**; correr también `pytest tests/unit/agents/test_scheduler.py -q` (no romper jobs)
- [ ] **Step 6: Commit** `git commit -m "feat(cassiel): acciones de tarea (crear/listar/cerrar/cancelar/posponer)"`

---

### Task 6: Router — frases de tarea al intent `reminder`

**Files:**
- Modify: `orchestration/agent_router.py:40-44` (el patrón `reminder` existente)
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Consumes: patrón regex `reminder` actual (agent_router.py:40).
- Produces: el mismo intent `reminder` atrapa además: `tengo que`, `necesito + verbo`, `pendiente(s)`, `anota(me)`, `apunta`, `/pendientes`, `ya lo hice`, `ya compré/pagué/terminé`.

- [ ] **Step 1: Failing tests**

```python
class TestRuteo:
    @pytest.mark.parametrize("frase", [
        "recuérdame comprar café el día de súper",
        "tengo que revisar el contrato de la renta",
        "anota: colgar el cuadro del vision board",
        "/pendientes",
        "/pendientes laboral",
        "¿qué tengo pendiente?",
        "ya compré el café",
        "ya lo hice",
        "cancela la del café",
    ])
    async def test_frases_de_tarea_rutean_a_reminder(self, frase):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == "reminder", f"{frase!r} → {decision.intent!r}"

    @pytest.mark.parametrize("frase,intent", [
        ("recuerda lo que te dije del proyecto", "memory"),
        ("agenda una reunión con Marco el jueves", "productivity"),
        ("necesito el estado del cluster", "kubernetes"),
    ])
    async def test_no_se_come_otros_intents(self, frase, intent):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == intent
```

- [ ] **Step 2: Run → FAIL**
- [ ] **Step 3: Ampliar el patrón** — en el regex `reminder` existente, añadir alternativas (misma tupla, antes de `tareas?\s+programadas?`):

```
r"tengo\s+que\b|anota(me)?\b|apunta\b|/?pendientes?\b|"
r"ya\s+lo\s+hice|ya\s+(compr[eé]|pagu[eé]|termin[eé]|fui)\b|"
r"cancela\s+la\s+de\b|"
```

OJO: «necesito el estado del cluster» debe seguir en `kubernetes` — la regla
`kubernetes` va DESPUÉS de `reminder` en la lista, así que NO se agrega
`necesito` a secas: solo `necesito` seguido de infinitivo común de trámite
(`necesito\s+(agendar|comprar|llamar|pagar|revisar|renovar|llevar|recoger)`).
Ajustar hasta que los 3 negativos pasen.

- [ ] **Step 4: Run → PASS** (los 9 positivos y 3 negativos, y `test_scheduler.py` completo)
- [ ] **Step 5: Commit** `git commit -m "feat(router): frases de tarea pendiente rutean a Cassiel"`

---

### Task 7: Nudges — cron 9:00 México en runner + entrega compartida

**Files:**
- Modify: `agents/scheduler/runner.py`
- Test: `tests/unit/agents/test_cassiel_tasks.py`

**Interfaces:**
- Consumes: `tasks_storage.all_pending_with_due()`, `select_nudges()`, `mark_nudged()`; `_acquire_daily_lock()` existente (runner.py:66).
- Produces: `deliver_whatsapp(user_id: str, title: str, text: str) -> None` (extraída de `_deliver_whatsapp`, que pasa a llamarla con `job.user_id, job.title, text`); job APScheduler `task_nudges` cron 9:00 America/Mexico_City.

- [ ] **Step 1: Refactor entrega** — en `runner.py`, extraer el cuerpo de `_deliver_whatsapp(job, text)` a:

```python
def deliver_whatsapp(user_id: str, title: str, text: str) -> None:
    # (cuerpo actual de _deliver_whatsapp, con user_id/title en vez de job.*)
```

y dejar `_deliver_whatsapp(job, text)` como `deliver_whatsapp(job.user_id, job.title, text)`. Correr `pytest tests/unit/agents/test_scheduler.py -q` → PASS (sin regresión).

- [ ] **Step 2: Failing test del armado del mensaje** (puro):

```python
class TestNudgeMessage:
    def test_formato_agrupa_por_usuario(self):
        from agents.scheduler.runner import _format_nudge
        hoy = date(2026, 8, 15)
        tareas = [_task(id=1, title="contrato renta", priority="alta",
                        due_date=date(2026, 8, 12)),
                  _task(id=2, title="comprar café", priority="media",
                        due_date=hoy)]
        msg = _format_nudge(tareas, hoy)
        assert "contrato renta" in msg and "3 día" in msg   # días de atraso
        assert "comprar café" in msg and "hoy" in msg
```

- [ ] **Step 3: Implement en `runner.py`**

```python
async def _task_nudges() -> None:
    """Nudges diarios de pendientes (spec §3). Cron 9:00 México; el lock evita
    doble envío con 2 réplicas (mismo patrón que la consolidación)."""
    if not _acquire_daily_lock("task_nudges"):
        logger.info("[scheduler] Nudges: otra réplica los tomó.")
        return
    from collections import defaultdict
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    from agents.scheduler import tasks_storage

    hoy = datetime.now(ZoneInfo("America/Mexico_City")).date()
    por_usuario: dict[str, list] = defaultdict(list)
    for t in tasks_storage.all_pending_with_due():
        por_usuario[t.user_id].append(t)

    for user_id, tareas in por_usuario.items():
        sel = tasks_storage.select_nudges(tareas, hoy)
        if not sel:
            continue
        try:
            deliver_whatsapp(user_id, "Pendientes", _format_nudge(sel, hoy))
            for t in sel:
                tasks_storage.mark_nudged(t.id)
        except Exception as exc:
            # Un usuario sin WhatsApp no debe tumbar los nudges de los demás.
            logger.warning(f"[scheduler] Nudge a {user_id} falló: {exc}")


def _format_nudge(tareas, hoy) -> str:
    lineas = []
    for t in tareas:
        atraso = (hoy - t.due_date).days
        cuando = "para hoy" if atraso == 0 else f"{atraso} día(s) de atraso"
        mins = f" (~{t.estimated_minutes}m)" if t.estimated_minutes else ""
        lineas.append(f"• #{t.id} {t.title}{mins} — {cuando}")
    return "Tienes pendientes que necesitan atención:\n" + "\n".join(lineas) + \
           "\n\nResponde «ya lo hice» o «pospón la de X al lunes»."
```

Y en `start_scheduler_loop()`, después del job de consolidación:

```python
    _scheduler.add_job(
        _task_nudges, "cron", hour=9, minute=0,
        timezone="America/Mexico_City",
        id="task_nudges", replace_existing=True,
        max_instances=1,
    )
```

(el log de `next_run_time` ya recorre todos los jobs — lección runbooks nivel 3).
`datetime` ya se importa en Task 7 vía el módulo — verificar imports arriba del archivo.

- [ ] **Step 4: Run → PASS** (nuevo test + `test_scheduler.py`)
- [ ] **Step 5: Commit** `git commit -m "feat(cassiel): nudges diarios 9:00 con lock y entrega compartida"`

---

### Task 8: Wire-up — init en lifespan, métricas, deploy

**Files:**
- Modify: `main.py` (lifespan: llamar `init_tasks_db()` junto a `init_scheduler_db()` — buscar la llamada existente)
- Modify: `observability/metrics.py` (3 contadores nuevos)
- Modify: `k8s/agents/05-backend-deployment.yaml` (bump de versión — ÚLTIMO paso)

**Interfaces:**
- Consumes: todo lo anterior.
- Produces: `amael_tasks_total{status}` (Counter, incrementa en create/set_status), `amael_task_nudges_total` (Counter, incrementa por nudge enviado), en `observability/metrics.py` siguiendo el patrón de los contadores existentes; incrementos desde `tasks_storage.create_task/set_status` y `runner._task_nudges`.

- [ ] **Step 1: Métricas** — en `observability/metrics.py`, junto a los Counters existentes:

```python
TASKS_TOTAL = Counter(
    "amael_tasks_total", "Tareas pendientes por transición de estado", ["status"]
)
TASK_NUDGES_TOTAL = Counter(
    "amael_task_nudges_total", "Nudges de pendientes enviados"
)
```

Incrementar: `TASKS_TOTAL.labels(status="pending").inc()` al final de `create_task`;
`TASKS_TOTAL.labels(status=status).inc()` en `set_status` cuando `cambiado`;
`TASK_NUDGES_TOTAL.inc()` por tarea nudgeada en `_task_nudges`.
(Import perezoso dentro de la función, patrón del repo, para no acoplar storage a prometheus en tests.)

- [ ] **Step 2: Lifespan** — en `main.py`, localizar `init_scheduler_db()` y añadir en la línea siguiente `init_tasks_db()` (import desde `agents.scheduler.tasks_storage`).

- [ ] **Step 3: Suite completa + lint**

Run: `python3 -m pytest tests/ -q -m "not e2e"` y `ruff check .`
Expected: verde completo.

- [ ] **Step 4: Bump + deploy por CI**

```bash
sed -i 's|amael-agentic-backend:1\.16\.[0-9]*|amael-agentic-backend:1.17.0|' k8s/agents/05-backend-deployment.yaml
git add -A
git commit -m "1.17.0: Cassiel gestor de pendientes — Fase 1 (captura, /pendientes, nudges)"
git push   # CI: tests → build → deploy
```

(Verificar el tag vigente antes del sed — el CI pudo haberlo movido.)

- [ ] **Step 5: E2E manual (tras rollout)** — por WhatsApp:
  1. «tengo que revisar el contrato de la renta» → anotada con categoría/prioridad inferidas
  2. «/pendientes» → lista priorizada
  3. «ya revisé el contrato» → cerrada
  4. «anota: comprar café» + «/pendientes personal» → aparece
  5. «pospón la del café al lunes» → fecha movida
  6. Verificar en Postgres: `SELECT * FROM user_tasks` refleja todo.

- [ ] **Step 6: Documentar** — nota en el vault (`analisis/2026-08-XX-cassiel-fase1.md`) + actualizar tabla CLAUDE.md con 1.17.0.

---

## Self-review (hecho al escribir)

- **Cobertura del spec Fase 1**: modelo (T1-T2), captura (T5-T6), transiciones (T3, T5), nudges (T4, T7), errores legibles (T5), métricas (T8). El brief/nocturno y agenda son Fase 2/3 — fuera de este plan a propósito. `/ayuda` va en Fase 2 (decisión del spec).
- **Sin placeholders**: cada step trae código o comando concreto.
- **Consistencia de nombres**: `tasks_storage.{create_task, list_pending, sorted_pending, sort_key, match_tasks, find_task, set_status, postpone_task, nudge_eligible, select_nudges, mark_nudged, all_pending_with_due}` — verificados entre tasks.
