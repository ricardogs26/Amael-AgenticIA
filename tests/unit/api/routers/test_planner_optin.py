"""
El brief diario es opt-in y va al número de su dueño.

Dos bugs vivían aquí. El primero: `_send_brief_whatsapp` buscaba
`identity_type = 'phone'`, un tipo que nadie escribe —el bridge registra
'whatsapp'—, así que la consulta no devolvía nada nunca y todos los briefs
caían al fallback `admin_phone`. El 6-ago-2026 eso significó que los resúmenes
de calendario y correo de seis personas se entregaran en el teléfono del admin.

El segundo: el recorrido tomaba a cualquier usuario `active`. Estar dado de
alta en la plataforma no es haber pedido un WhatsApp a las 7am, así que ahora
hace falta `daily_brief_enabled = TRUE`.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _env():
    os.environ.setdefault("INTERNAL_API_SECRET", "test-secret-" + "x" * 20)
    os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)
    os.environ.setdefault("SESSION_SECRET_KEY", "x" * 32)
    os.environ.setdefault("POSTGRES_PASSWORD", "test-pw")
    os.environ.setdefault("MINIO_ACCESS_KEY", "test-ak")
    os.environ.setdefault("MINIO_SECRET_KEY", "test-sk")


class _Cursor:
    """Cursor falso que devuelve `rows` y guarda el SQL ejecutado."""

    def __init__(self, rows, log):
        self._rows = rows
        self._log = log

    def execute(self, sql, params=None):
        self._log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_connection(rows, log):
    class _Conn:
        def cursor(self):
            return _Cursor(rows, log)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _get_connection():
        return _Conn()

    return _get_connection


def test_el_brief_va_al_whatsapp_del_usuario(monkeypatch):
    """Se consulta 'whatsapp', no 'phone'."""
    from interfaces.api.routers import planner

    log: list = []
    monkeypatch.setattr("storage.postgres.client.get_connection",
                        _fake_connection([("5215550001111",)], log))

    enviados: list[dict] = []
    monkeypatch.setattr("httpx.post",
                        lambda url, **kw: enviados.append({"url": url, **kw.get("json", {})})
                        or type("R", (), {"status_code": 200})())

    planner._send_brief_whatsapp("lucero@example.com", "tu día")

    sql = log[0][0]
    assert "identity_type = 'whatsapp'" in sql, "sigue buscando un tipo que nadie escribe"
    assert "ORDER BY id" in sql, "con dos números registrados la fila sería arbitraria"
    assert len(enviados) == 1
    assert enviados[0]["phoneNumber"] == "5215550001111"


def test_sin_numero_propio_no_se_manda_a_nadie(monkeypatch):
    """El fallback a admin_phone era una fuga: el brief ajeno al admin."""
    from interfaces.api.routers import planner

    log: list = []
    monkeypatch.setattr("storage.postgres.client.get_connection",
                        _fake_connection([], log))

    enviados: list = []
    monkeypatch.setattr("httpx.post",
                        lambda url, **kw: enviados.append(url)
                        or type("R", (), {"status_code": 200})())

    planner._send_brief_whatsapp("sin-numero@example.com", "tu día")

    assert enviados == [], "se redirigió el brief de alguien más"


@pytest.mark.asyncio
async def test_solo_recorre_a_quien_lo_pidio(monkeypatch):
    """El SELECT exige daily_brief_enabled y excluye al bot de servicio."""
    from interfaces.api.routers import planner

    log: list = []
    monkeypatch.setattr("storage.postgres.client.get_connection",
                        _fake_connection([("ricardogs26@gmail.com",)], log))
    monkeypatch.setattr(planner, "_check_planner_rate_limit", lambda: None)

    procesados: list[str] = []

    async def _organize(user_email):
        procesados.append(user_email)
        return {"summary": "tu día"}

    monkeypatch.setattr("agents.productivity.day_planner.organize_day_for_user",
                        _organize, raising=False)
    monkeypatch.setattr(planner, "_send_brief_whatsapp", lambda u, s: None)

    res = await planner.run_daily_planner()

    sql, params = log[0]
    assert "daily_brief_enabled = TRUE" in sql, "vuelve a recorrer a todo usuario activo"
    assert params == (planner._BOT_USER,), "el bot de servicio no queda excluido"
    assert procesados == ["ricardogs26@gmail.com"]
    assert res.processed == 1
