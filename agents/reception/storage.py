"""Persistencia de leads (Postgres). Funciones puras arriba, SQL abajo."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger("agents.reception.storage")

STATUSES = ("open", "approved", "rejected")
FIELDS   = ("name", "company", "reason")

_DDL = """
CREATE TABLE IF NOT EXISTS leads (
    id            SERIAL PRIMARY KEY,
    phone         TEXT UNIQUE NOT NULL,
    name          TEXT,
    company       TEXT,
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','approved','rejected')),
    message_count INT  NOT NULL DEFAULT 0,
    notified_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lead_messages (
    id       SERIAL PRIMARY KEY,
    lead_id  INT NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    role     TEXT NOT NULL CHECK (role IN ('visitor','amael','ricardo')),
    content  TEXT NOT NULL,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lead_messages_lead ON lead_messages (lead_id, id DESC);
"""


@dataclass
class Lead:
    id: int
    phone: str
    name: str | None
    company: str | None
    reason: str | None
    status: str
    message_count: int
    notified_at: datetime | None
    created_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return all(getattr(self, f) for f in FIELDS)


def merge_fields(lead: Lead, extracted: dict) -> dict:
    """
    Qué campos escribir a partir de lo que devolvió el LLM: solo los que
    vienen NO vacíos y que el lead aún no tiene. El LLM nunca pisa un dato
    ya capturado (una paráfrasis posterior no debe cambiar la empresa).
    """
    out: dict = {}
    for f in FIELDS:
        if getattr(lead, f):
            continue
        val = extracted.get(f)
        if isinstance(val, str) and val.strip():
            out[f] = val.strip()[:200]
    return out


# ── SQL ───────────────────────────────────────────────────────────────────────

def _conn():
    from storage.postgres.client import get_connection
    return get_connection()


def ensure_schema() -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL)


_COLS = "id, phone, name, company, reason, status, message_count, notified_at, created_at"
# SQL en constantes de módulo (Bandit B608 en CI): ninguna toma entrada del usuario.
_SQL_UPSERT = (
    "INSERT INTO leads (phone) VALUES (%s) "
    "ON CONFLICT (phone) DO UPDATE SET updated_at = now() "
    f"RETURNING {_COLS}"
)
_SQL_GET       = f"SELECT {_COLS} FROM leads WHERE id = %s"
_SQL_LIST_OPEN = f"SELECT {_COLS} FROM leads WHERE status = 'open' ORDER BY updated_at DESC LIMIT %s"
_SQL_SET = {  # columna → sentencia fija; nada se concatena desde fuera
    "name":    "UPDATE leads SET name = %s, updated_at = now() WHERE id = %s",
    "company": "UPDATE leads SET company = %s, updated_at = now() WHERE id = %s",
    "reason":  "UPDATE leads SET reason = %s, updated_at = now() WHERE id = %s",
}


def _row(r) -> Lead:
    return Lead(*r)


def get_or_create(phone: str) -> Lead:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_UPSERT, (phone,))
            return _row(cur.fetchone())


def get(lead_id: int) -> Lead | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_GET, (lead_id,))
            r = cur.fetchone()
            return _row(r) if r else None


def list_open(limit: int = 10) -> list[Lead]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_LIST_OPEN, (limit,))
            return [_row(r) for r in cur.fetchall()]


def update_fields(lead_id: int, **campos) -> None:
    campos = {k: v for k, v in campos.items() if k in _SQL_SET}
    if not campos:
        return
    with _conn() as conn:
        with conn.cursor() as cur:
            for k, v in campos.items():
                cur.execute(_SQL_SET[k], (v, lead_id))


def set_status(lead_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status}")
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = %s, updated_at = now() WHERE id = %s",
                (status, lead_id),
            )


def bump_count(lead_id: int) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET message_count = message_count + 1, updated_at = now() "
                "WHERE id = %s RETURNING message_count",
                (lead_id,),
            )
            return int(cur.fetchone()[0])


def mark_notified(lead_id: int) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE leads SET notified_at = now() WHERE id = %s", (lead_id,))


def add_message(lead_id: int, role: str, content: str) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lead_messages (lead_id, role, content) VALUES (%s, %s, %s)",
                (lead_id, role, content[:2000]),
            )


def recent_messages(lead_id: int, n: int = 8) -> list[tuple[str, str]]:
    """Últimos n mensajes en orden cronológico."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM lead_messages WHERE lead_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (lead_id, n),
            )
            rows = cur.fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]
