"""Compromisos desde correo: la parte determinista (sin LLM, sin Gmail).

Caso real 14-oct-2026: el recibo de la línea telefónica llega por correo,
Ricardo lo abre y el brief lo pierde para siempre (solo lee «no leídos»).
El LLM extrae; el CÓDIGO valida fechas, decide prioridad y deduplica.
"""
from __future__ import annotations

from datetime import date

from agents.productivity import commitment_extractor as ce

HOY = date(2026, 10, 14)

EMAILS = [
    {"id": "m1", "from": "Telcel <facturacion@telcel.com>", "subject": "Tu factura de octubre",
     "snippet": "Monto a pagar $489.00 con fecha límite 20 de octubre de 2026."},
    {"id": "m2", "from": "Dentalitos <citas@dentalitos.mx>", "subject": "Confirmación de cita",
     "snippet": "Cita de Regina el sábado 24 de octubre 13:30."},
    {"id": "m3", "from": "Medium Daily Digest <noreply@medium.com>", "subject": "Stories for you",
     "snippet": "Top stories today."},
]

PARSED = {"commitments": [
    {"email_index": 0, "type": "pago", "title": "Pagar factura Telcel octubre",
     "due_date": "2026-10-20", "amount": 489.0},
    {"email_index": 1, "type": "cita", "title": "Cita dental Regina",
     "due_date": "2026-10-24", "amount": None},
]}


class TestResolve:
    def test_extrae_pago_con_fecha_monto_y_remitente(self):
        res = ce.resolve_commitments(PARSED, EMAILS, HOY)
        pago = res[0]
        assert pago.title == "Pagar factura Telcel octubre"
        assert pago.due_date == date(2026, 10, 20)
        assert pago.amount == 489.0
        assert pago.sender == "facturacion@telcel.com"
        assert pago.source_ref == "gmail:m1"

    def test_prioridad_por_cercania_en_codigo(self):
        # ≤3 días alta, ≤7 media, después baja
        parsed = {"commitments": [
            {"email_index": 0, "type": "pago", "title": "a", "due_date": "2026-10-16"},
            {"email_index": 0, "type": "pago", "title": "b", "due_date": "2026-10-20"},
            {"email_index": 0, "type": "pago", "title": "c", "due_date": "2026-11-10"},
        ]}
        res = ce.resolve_commitments(parsed, EMAILS, HOY)
        assert [c.priority for c in res] == ["alta", "media", "baja"]

    def test_sin_fecha_se_descarta(self):
        parsed = {"commitments": [{"email_index": 0, "type": "pago", "title": "x", "due_date": None}]}
        assert ce.resolve_commitments(parsed, EMAILS, HOY) == []

    def test_fecha_pasada_o_muy_lejana_se_descarta(self):
        parsed = {"commitments": [
            {"email_index": 0, "type": "pago", "title": "vieja", "due_date": "2026-10-01"},
            {"email_index": 0, "type": "pago", "title": "lejana", "due_date": "2027-03-01"},
        ]}
        assert ce.resolve_commitments(parsed, EMAILS, HOY) == []

    def test_fecha_de_hoy_si_cuenta(self):
        parsed = {"commitments": [{"email_index": 0, "type": "pago", "title": "hoy", "due_date": "2026-10-14"}]}
        assert len(ce.resolve_commitments(parsed, EMAILS, HOY)) == 1

    def test_tipo_inventado_o_indice_fuera_de_rango_se_descarta(self):
        parsed = {"commitments": [
            {"email_index": 0, "type": "chisme", "title": "x", "due_date": "2026-10-20"},
            {"email_index": 9, "type": "pago", "title": "y", "due_date": "2026-10-20"},
        ]}
        assert ce.resolve_commitments(parsed, EMAILS, HOY) == []

    def test_json_sin_lista_no_truena(self):
        assert ce.resolve_commitments({}, EMAILS, HOY) == []
        assert ce.resolve_commitments({"commitments": "nada"}, EMAILS, HOY) == []

    def test_monto_invalido_queda_en_none(self):
        parsed = {"commitments": [{"email_index": 0, "type": "pago", "title": "x",
                                   "due_date": "2026-10-20", "amount": "cuatrocientos"}]}
        assert ce.resolve_commitments(parsed, EMAILS, HOY)[0].amount is None


class TestRegistro:
    def test_no_duplica_por_message_id_ni_registra_remitente_excluido(self):
        creados = []
        res = ce.resolve_commitments(PARSED, EMAILS, HOY)
        n = ce.register_commitments(
            "u@x.com", res,
            ya_registrado=lambda ref: ref == "gmail:m1",
            excluido=lambda sender: False,
            crear=lambda **kw: creados.append(kw),
        )
        assert n == 1 and creados[0]["source_ref"] == "gmail:m2"

    def test_remitente_excluido_no_se_registra(self):
        creados = []
        res = ce.resolve_commitments(PARSED, EMAILS, HOY)
        n = ce.register_commitments(
            "u@x.com", res,
            ya_registrado=lambda ref: False,
            excluido=lambda sender: sender.endswith("@telcel.com"),
            crear=lambda **kw: creados.append(kw),
        )
        assert n == 1 and creados[0]["source_sender"] == "citas@dentalitos.mx"

    def test_lo_registrado_lleva_origen_email_y_due_date(self):
        creados = []
        res = ce.resolve_commitments(PARSED, EMAILS, HOY)
        ce.register_commitments("u@x.com", res, ya_registrado=lambda r: False,
                                excluido=lambda s: False, crear=lambda **kw: creados.append(kw))
        kw = creados[0]
        assert kw["source"] == "email" and kw["due_date"] == date(2026, 10, 20)
        assert kw["amount"] == 489.0 and kw["user_id"] == "u@x.com"
        assert "telcel" in kw["description"].lower()


class TestPromptBatch:
    def test_el_prompt_numera_los_correos_y_da_la_fecha(self):
        texto = ce.format_emails_for_llm(EMAILS, HOY)
        assert "[0]" in texto and "[2]" in texto
        assert "Tu factura de octubre" in texto
        assert "2026-10-14" in texto


class TestDomiciliados:
    """Sondeo real 14-oct-2026: el 9b marcó como «pago» el recibo de CFE que
    dice «se cargará automáticamente… no necesitas hacer nada». El prompt lo
    prohíbe y no bastó: la regla va en código."""

    def test_un_cargo_automatico_no_es_compromiso(self):
        emails = [{"id": "c1", "from": "CFE <n@cfe.mx>", "subject": "Tu recibo de luz",
                   "snippet": "Tu recibo de $1,230.00 se cargará automáticamente a tu tarjeta "
                              "terminación 4411 el 18 de octubre. No necesitas hacer nada."}]
        parsed = {"commitments": [{"email_index": 0, "type": "pago", "title": "Pagar CFE",
                                   "due_date": "2026-10-18", "amount": 1230}]}
        assert ce.resolve_commitments(parsed, emails, HOY) == []

    def test_una_cita_del_mismo_remitente_si_pasa(self):
        emails = [{"id": "c2", "from": "CFE <n@cfe.mx>", "subject": "Visita técnica",
                   "snippet": "Un técnico acudirá el 18 de octubre entre 9 y 13 h. Es necesario que haya alguien."}]
        parsed = {"commitments": [{"email_index": 0, "type": "cita", "title": "Visita CFE",
                                   "due_date": "2026-10-18"}]}
        assert len(ce.resolve_commitments(parsed, emails, HOY)) == 1


class TestRuidoRealDeProduccion:
    """Primera corrida real (14-oct-2026, 40 correos): 4 de 5 «compromisos»
    eran ruido. Cada caso baja a una regla de código."""

    def _uno(self, sender, subject, snippet, tipo="pago", title="x", due="2026-10-16"):
        emails = [{"id": "r1", "from": sender, "subject": subject, "snippet": snippet}]
        parsed = {"commitments": [{"email_index": 0, "type": tipo, "title": title, "due_date": due}]}
        return ce.resolve_commitments(parsed, emails, HOY)

    def test_avisos_de_google_calendar_no_son_compromisos(self):
        # Son nuestros propios eventos (el planner los escribe) rebotando por correo.
        assert self._uno("Google Calendar <calendar-notification@google.com>",
                         "Notificación: Bloque Amael", "Deadline…", tipo="respuesta") == []

    def test_no_se_puede_responder_a_un_noreply(self):
        assert self._uno("Google Play <googleplay-noreply@google.com>", "Suscripción",
                         "…", tipo="respuesta") == []
        assert self._uno("GitHub <notifications@github.com>", "PR", "…", tipo="respuesta") == []

    def test_un_pago_desde_noreply_si_pasa(self):
        # Las facturas sí llegan de noreply: la regla anterior es solo para «respuesta».
        assert len(self._uno("Izzi <noreply@izzi.mx>", "Tu factura", "Vence el 16 de octubre")) == 1

    def test_lo_que_el_llm_marca_como_ya_hecho_se_descarta(self):
        assert self._uno("Google Play <googleplay-noreply@google.com>", "Suscripción cancelada",
                         "…", title="Cancelar suscripción Skillion (ya cancelado)") == []
        assert self._uno("Banco <avisos@banco.mx>", "Pago aplicado", "…",
                         title="Pagar tarjeta — ya pagado") == []

    def test_urgencia_de_marketing_no_es_vencimiento(self):
        assert self._uno("Southwest <southwestairlines@iluv.southwest.com>",
                         "¡Solo hoy! Puntos extra", "Compra puntos antes de fin del día",
                         tipo="vencimiento", title="Comprar puntos Southwest") == []

    def test_remitentes_excluidos_por_env_se_descartan(self, monkeypatch):
        monkeypatch.setattr(ce, "_EXCLUDED_SENDERS", frozenset({"avisos@spam.mx"}))
        assert self._uno("Spam <avisos@spam.mx>", "Paga ya", "Vence el 16") == []
