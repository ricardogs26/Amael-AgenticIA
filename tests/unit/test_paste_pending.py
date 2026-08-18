"""Tests de paste:pending — texto reenviado sin instrucción (ver
interfaces/api/routers/chat.py). Toda la detección es pura (sin LLM,
sin Redis real): regex sobre los primeros 200 chars + longitud."""
from __future__ import annotations

import pytest

from interfaces.api.routers import chat

AVISO_ESCOLAR = (
    "Estimados Padres de Familia: Por este medio comunicamos los detalles "
    "del evento escolar de fin de curso, programado para el proximo mes en "
    "las instalaciones del plantel. Favor de tomar nota de horarios y "
    "participar activamente en las actividades organizadas por el comite. "
    "Gracias por su atencion y compromiso con la comunidad escolar durante "
    "todo el ciclo. Un cordial saludo del comite organizador y del cuerpo "
    "docente de la institucion educativa para este ciclo escolar en curso."
)
assert len(AVISO_ESCOLAR) > 400  # el propio test asegura el fixture

CORREO_PEGADO = (
    "Fwd: Confirmacion de tu pedido. Hola, te escribimos para confirmar la "
    "recepcion de tu pedido numero 48213, el cual sera entregado en un "
    "plazo de cinco a siete dias habiles en el domicilio registrado. "
    "Gracias por tu compra y por confiar en nosotros para tus envios."
)


class TestHasInstruction:
    @pytest.mark.parametrize("texto", [
        "resume esto: " + AVISO_ESCOLAR,
        "recuérdame ir por los útiles el lunes",
        "¿puedes revisar esto?",
    ])
    def test_positivos(self, texto):
        assert chat._has_instruction(texto) is True

    def test_aviso_escolar_puro_sin_instruccion(self):
        assert chat._has_instruction(AVISO_ESCOLAR) is False

    def test_correo_pegado_sin_peticion(self):
        assert chat._has_instruction(CORREO_PEGADO) is False


class TestIsBarePaste:
    def test_aviso_largo_sin_instruccion_es_bare_paste(self):
        assert chat._is_bare_paste(AVISO_ESCOLAR) is True

    def test_mismo_aviso_con_instruccion_al_frente_no_es_bare_paste(self):
        assert chat._is_bare_paste("resume esto:\n" + AVISO_ESCOLAR) is False

    def test_mensaje_corto_no_es_bare_paste(self):
        assert chat._is_bare_paste("hola, ¿cómo estás?") is False


class TestMergePaste:
    def test_formato_exacto(self):
        out = chat._merge_paste("resume esto", "Estimados Padres de Familia...")
        assert out == (
            "resume esto\n\n[Texto reenviado previamente por el usuario]:\n"
            "Estimados Padres de Familia..."
        )


class TestRedisHelpers:
    def test_set_pending_paste_hace_setex_truncado(self, monkeypatch):
        llamadas = {}

        class _FakeRedis:
            def setex(self, key, ttl, value):
                llamadas["key"] = key
                llamadas["ttl"] = ttl
                llamadas["value"] = value

        import storage.redis.client as redis_client_mod
        monkeypatch.setattr(redis_client_mod, "get_redis_client", lambda: _FakeRedis())

        texto_largo = "x" * 5000
        chat._set_pending_paste("u@x.com", texto_largo)

        assert llamadas["key"] == "paste:pending:u@x.com"
        assert llamadas["ttl"] == 600
        assert len(llamadas["value"]) == 4000

    def test_pop_pending_paste_hace_getdel(self, monkeypatch):
        class _FakeRedis:
            def getdel(self, key):
                assert key == "paste:pending:u@x.com"
                return b"Estimados Padres..."

        import storage.redis.client as redis_client_mod
        monkeypatch.setattr(redis_client_mod, "get_redis_client", lambda: _FakeRedis())

        assert chat._pop_pending_paste("u@x.com") == "Estimados Padres..."

    def test_pop_pending_paste_sin_redis_devuelve_none(self, monkeypatch):
        import storage.redis.client as redis_client_mod

        def _boom():
            raise ConnectionError("redis caído")

        monkeypatch.setattr(redis_client_mod, "get_redis_client", _boom)
        assert chat._pop_pending_paste("nadie@x.com") is None


class TestWiringRouteWithFollowup:
    """`_route_with_followup` es el hook compartido por /chat y
    /chat/stream — el merge de paste:pending vive ahí para cubrir ambos
    endpoints sin duplicar lógica de ruteo."""

    async def test_paste_pendiente_se_fusiona_en_respuesta_corta(self, monkeypatch):
        monkeypatch.setattr(chat, "_pop_pending_paste",
                            lambda user: "Estimados Padres de Familia...")
        q, decision = await chat._route_with_followup("u@x.com", "resume esto")
        assert q == (
            "resume esto\n\n[Texto reenviado previamente por el usuario]:\n"
            "Estimados Padres de Familia..."
        )
        assert decision is not None

    async def test_sin_paste_pendiente_rutea_normal(self, monkeypatch):
        monkeypatch.setattr(chat, "_pop_pending_paste", lambda user: None)
        import agents.scheduler.agent as cassiel_mod
        monkeypatch.setattr(cassiel_mod, "pop_followup", lambda user: None)
        q, decision = await chat._route_with_followup("u@x.com", "¿qué pods hay en el cluster?")
        assert q == "¿qué pods hay en el cluster?"
        assert decision.routing_reason != "cassiel_followup"

    async def test_paste_gana_sobre_followup_y_no_lo_consume(self, monkeypatch):
        # Si ambos existen, el paste merge gana este turno; pop_followup NO
        # debe llamarse (el followup se deja intacto para la próxima vez).
        monkeypatch.setattr(chat, "_pop_pending_paste",
                            lambda user: "Estimados Padres de Familia...")
        import agents.scheduler.agent as cassiel_mod

        def _pop_followup_no_deberia_llamarse(user):
            raise AssertionError("pop_followup no debe consumirse cuando gana el paste merge")

        monkeypatch.setattr(cassiel_mod, "pop_followup", _pop_followup_no_deberia_llamarse)
        q, decision = await chat._route_with_followup("u@x.com", "resume esto")
        assert "Estimados Padres de Familia..." in q
