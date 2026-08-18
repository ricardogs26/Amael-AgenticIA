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

# ── Casos adversarios (revisión) ──────────────────────────────────────────────
# "que"/"como" sin acento son los conectores más comunes del español — un
# aviso real los usa todo el tiempo sin ser una instrucción. "recordamos"
# tampoco es instrucción ("recuerda"/"recuérdame" sí). Ninguno de los tres
# textos de abajo lleva "?" ni "¿".

AVISO_COMPARTIMOS_QUE = (
    "Estimados Padres de Familia: Les compartimos que el proximo lunes se "
    "llevara a cabo la reunion informativa del ciclo escolar en el auditorio "
    "principal a las nueve de la manana. La asistencia es importante para "
    "conocer los detalles del programa academico y las actividades extra "
    "curriculares del semestre. Se solicita llegar con quince minutos de "
    "anticipacion y portar el gafete de identificacion correspondiente. "
    "Gracias por su atencion y su compromiso constante con la comunidad "
    "escolar durante todo el ciclo lectivo en curso."
)
assert len(AVISO_COMPARTIMOS_QUE) > 400

AVISO_RECORDAMOS_EL_PAGO = (
    "Estimados Padres de Familia: Les recordamos el pago de la colegiatura "
    "correspondiente al mes en curso, el cual debera realizarse a mas tardar "
    "el dia treinta en la caja de la institucion o mediante transferencia "
    "bancaria a la cuenta oficial del plantel. El comprobante de pago debe "
    "entregarse en la administracion escolar dentro de los tres dias "
    "posteriores a la transaccion para su debido registro contable. "
    "Agradecemos su puntualidad y compromiso con la institucion durante "
    "este ciclo escolar."
)
assert len(AVISO_RECORDAMOS_EL_PAGO) > 400

CORREO_BANCO_CON_QUE = (
    "Estimado cliente, le informamos que se ha registrado un cargo por "
    "el servicio de mantenimiento correspondiente al periodo actual en su "
    "cuenta bancaria. El monto sera reflejado en su proximo estado de "
    "cuenta junto con el desglose detallado de los conceptos aplicados "
    "durante el mes. Para mayor informacion sobre este cargo puede "
    "consultar los terminos y condiciones del contrato vigente firmado "
    "al momento de la apertura de la cuenta. Gracias por su preferencia."
)
assert len(CORREO_BANCO_CON_QUE) > 400


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

    # ── Adversarios: "que"/"como" sueltos y "record..." NO son instrucción ──
    def test_les_compartimos_que_no_es_instruccion(self):
        assert chat._has_instruction(AVISO_COMPARTIMOS_QUE) is False

    def test_les_recordamos_el_pago_no_es_instruccion(self):
        assert chat._has_instruction(AVISO_RECORDAMOS_EL_PAGO) is False

    def test_correo_banco_con_que_no_es_instruccion(self):
        assert chat._has_instruction(CORREO_BANCO_CON_QUE) is False

    def test_puedes_revisar_al_frente_si_es_instruccion(self):
        assert chat._has_instruction("¿puedes revisar esto?") is True

    def test_signo_de_cierre_lejos_del_inicio_no_cuenta(self):
        # Un "?" que aparece muy adelante en un texto largo (una cita, un
        # horario "9:00?") no debe convertir el aviso completo en pregunta.
        relleno = "x" * 300
        texto = AVISO_COMPARTIMOS_QUE + " " + relleno + " ¿o no?"
        assert chat._has_instruction(texto) is False

    def test_signo_de_cierre_cerca_del_inicio_si_cuenta(self):
        texto = "hola?" + "x" * 500
        assert chat._has_instruction(texto) is True


class TestIsBarePaste:
    def test_aviso_largo_sin_instruccion_es_bare_paste(self):
        assert chat._is_bare_paste(AVISO_ESCOLAR) is True

    def test_mismo_aviso_con_instruccion_al_frente_no_es_bare_paste(self):
        assert chat._is_bare_paste("resume esto:\n" + AVISO_ESCOLAR) is False

    def test_mensaje_corto_no_es_bare_paste(self):
        assert chat._is_bare_paste("hola, ¿cómo estás?") is False

    def test_les_compartimos_que_es_bare_paste(self):
        assert chat._is_bare_paste(AVISO_COMPARTIMOS_QUE) is True

    def test_les_recordamos_el_pago_es_bare_paste(self):
        assert chat._is_bare_paste(AVISO_RECORDAMOS_EL_PAGO) is True

    def test_correo_banco_con_que_es_bare_paste(self):
        assert chat._is_bare_paste(CORREO_BANCO_CON_QUE) is True


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

    async def test_mensaje_largo_no_hace_pop_el_paste_sobrevive(self, monkeypatch):
        # Regresión: _pop_pending_paste (GETDEL, destructivo) NO debe
        # llamarse si el mensaje actual es largo (>= 400) — de lo contrario
        # se borra un paste guardado sin llegar a fusionarse con nada.
        def _pop_no_deberia_llamarse(user):
            raise AssertionError("_pop_pending_paste no debe llamarse con mensaje largo")

        monkeypatch.setattr(chat, "_pop_pending_paste", _pop_no_deberia_llamarse)
        import agents.scheduler.agent as cassiel_mod
        monkeypatch.setattr(cassiel_mod, "pop_followup", lambda user: None)

        mensaje_largo = "¿qué pods hay en el cluster? " + "x" * 400
        q, decision = await chat._route_with_followup("u@x.com", mensaje_largo)
        assert q == mensaje_largo
