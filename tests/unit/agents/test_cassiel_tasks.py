"""Tests de Cassiel tareas pendientes (Fase 1). Toda la lógica decidible
se prueba PURA (sin DB): validación, orden, matching, nudges."""
from __future__ import annotations

from datetime import UTC, date, datetime

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


class TestOrden:
    def test_vencida_gana_a_alta_sin_fecha(self):
        hoy = date(2026, 8, 15)
        vencida_baja = _task(id=1, priority="baja", due_date=date(2026, 8, 10))
        alta_sin_fecha = _task(id=2, priority="alta", due_date=None)
        orden = ts.sorted_pending([alta_sin_fecha, vencida_baja], hoy)
        assert [t.id for t in orden] == [1, 2]

    def test_desempate_por_fecha_mas_proxima(self):
        hoy = date(2026, 8, 15)
        lejana = _task(id=1, priority="media", due_date=date(2026, 8, 30))
        proxima = _task(id=2, priority="media", due_date=date(2026, 8, 20))
        orden = ts.sorted_pending([lejana, proxima], hoy)
        assert [t.id for t in orden] == [2, 1]

    def test_prioridad_ordena_sin_fechas(self):
        hoy = date(2026, 8, 15)
        tareas = [
            _task(id=1, priority="baja"),
            _task(id=2, priority="alta"),
            _task(id=3, priority="media"),
        ]
        assert [t.id for t in ts.sorted_pending(tareas, hoy)] == [2, 3, 1]


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
        tareas = [_task(id=1, title="informe mensual", category="laboral",
                        priority="baja"),
                  _task(id=2, title="vitaminas", category="personal",
                        priority="alta")]
        monkeypatch.setattr(ts, "list_pending", lambda u: tareas)
        out = agent._apply({"action": "task_list", "filter": "personal"},
                           "u@x.com", "America/Mexico_City")
        assert "vitaminas" in out and "#2" in out
        assert "informe mensual" not in out and "#1" not in out


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


class TestFollowup:
    """Fix 1 — continuidad conversacional: cuando Cassiel pregunta, guarda
    followup para que la siguiente respuesta del usuario vuelva a él.

    Fix 2 (loop infinito) — tres cambios deterministas:
    1. El texto guardado nunca reenvuelve un wrapper previo y se trunca a
       1500 chars (el inicio del aviso, donde viven las fechas).
    2. Tope de 2 rondas: a la tercera, Cassiel corta con una salida fija en
       vez de volver a preguntar.
    3. El merge pone la RESPUESTA del usuario primero y el contexto después.
    """

    @pytest.fixture
    def agent(self, monkeypatch):
        from agents.scheduler.agent import CassielAgent
        a = CassielAgent.__new__(CassielAgent)
        a._last_query = "recuérdame lo de la casa"
        a._next_followup_round = 1
        return a

    def _spy(self, agent, monkeypatch):
        llamadas = []

        def fake(user, prev, round_n=1):
            llamadas.append((user, prev, round_n))
            return True
        monkeypatch.setattr(agent, "_set_followup", fake)
        return llamadas

    def test_unclear_guarda_followup(self, agent, monkeypatch):
        llamadas = self._spy(agent, monkeypatch)
        out = agent._apply(
            {"action": "unclear", "clarification": "¿Para cuándo?"},
            "u@x.com", "America/Mexico_City",
        )
        assert out == "¿Para cuándo?"
        assert llamadas == [("u@x.com", "recuérdame lo de la casa", 1)]

    def test_create_incompleto_guarda_followup(self, agent, monkeypatch):
        llamadas = self._spy(agent, monkeypatch)
        out = agent._apply({"action": "create", "title": "x"},
                           "u@x.com", "America/Mexico_City")
        assert "falta" in out.lower()
        assert llamadas

    def test_task_ref_ambiguo_guarda_followup(self, agent, monkeypatch):
        llamadas = self._spy(agent, monkeypatch)
        def ambiguo(user_id, ref):
            raise ValueError("Hay varias pendientes que coinciden con 'banco': …")
        monkeypatch.setattr(ts, "find_task", ambiguo)
        out = agent._apply({"action": "task_done", "task_ref": "banco"},
                           "u@x.com", "America/Mexico_City")
        assert "varias" in out.lower()
        assert llamadas

    def test_task_sin_ref_guarda_followup(self, agent, monkeypatch):
        llamadas = self._spy(agent, monkeypatch)
        out = agent._apply({"action": "task_done"},
                           "u@x.com", "America/Mexico_City")
        assert "cuál" in out.lower()
        assert llamadas

    def test_create_completo_no_guarda_followup(self, agent, monkeypatch):
        llamadas = self._spy(agent, monkeypatch)
        monkeypatch.setattr(ts, "create_task",
                            lambda user_id, title, **kw: _task(id=3, title=title))
        agent._apply({"action": "task_create", "task": {"title": "comprar café",
                                                        "priority": "baja"}},
                     "u@x.com", "America/Mexico_City")
        assert not llamadas

    def test_unclear_ronda_excede_tope_devuelve_escape(self, agent, monkeypatch):
        # round_n=3 (tope=2): no se guarda followup y Cassiel corta el loop
        # con una salida fija en vez de volver a preguntar.
        from agents.scheduler.agent import _FOLLOWUP_ESCAPE_MSG
        agent._next_followup_round = 3
        out = agent._apply(
            {"action": "unclear", "clarification": "¿Para cuándo?"},
            "u@x.com", "America/Mexico_City",
        )
        assert out == _FOLLOWUP_ESCAPE_MSG
        # y en Redis no debería quedar nada (sin mock: sin Redis, no lanza)

    def test_merge_followup_respuesta_primero_contexto_despues(self):
        # Orden: la respuesta corta del usuario va PRIMERO (es la señal),
        # el contexto del followup queda de referencia al final.
        from agents.scheduler.agent import merge_followup
        out = merge_followup("mañana", {"q": "recuérdame lo de la casa", "n": 1})
        assert out.index("mañana") < out.index("recuérdame lo de la casa")
        assert out.endswith("recuérdame lo de la casa]")

    def test_merge_followup_acepta_string_legado(self):
        # Backward-compat: un valor viejo (string crudo, sin ronda) se trata
        # como ronda 1.
        from agents.scheduler.agent import merge_followup
        out = merge_followup("mañana", "recuérdame lo de la casa")
        assert "[[CASSIEL_FOLLOWUP:n=1]]" in out
        assert "recuérdame lo de la casa" in out

    def test_pop_followup_sin_redis_devuelve_none(self, monkeypatch):
        # Sin Redis alcanzable, pop_followup jamás lanza: devuelve None.
        from agents.scheduler.agent import pop_followup
        assert pop_followup("nadie@x.com") is None

    # ── Fix 2: helpers puros (unwrap, tope de rondas) ─────────────────────

    def test_strip_wrapper_anidado_se_queda_con_el_nucleo(self):
        # Forma real que tomaba el texto guardado ANTES del fix: cada ronda
        # reenvolvía la ronda anterior completa (pregunta + wrapper).
        from agents.scheduler.agent import _strip_followup_wrapper
        marker = "[Contexto: el usuario respondía a Cassiel sobre: "
        nested = (f"pregunta2\n\n{marker}pregunta1\n\n{marker}"
                  "aviso escolar del 22 de agosto]]")
        assert _strip_followup_wrapper(nested) == "aviso escolar del 22 de agosto"

    def test_strip_wrapper_sin_wrapper_no_cambia(self):
        from agents.scheduler.agent import _strip_followup_wrapper
        assert _strip_followup_wrapper("texto normal") == "texto normal"

    def test_followup_payload_trunca_1500_desde_el_inicio(self):
        from agents.scheduler.agent import _followup_payload
        texto = "A" * 3000
        payload = _followup_payload(texto, 1)
        assert payload["q"] == "A" * 1500

    def test_followup_payload_ronda_1_y_2_se_guardan(self):
        from agents.scheduler.agent import _followup_payload
        assert _followup_payload("x", 1) == {"q": "x", "n": 1}
        assert _followup_payload("x", 2) == {"q": "x", "n": 2}

    def test_followup_payload_ronda_3_no_se_guarda(self):
        from agents.scheduler.agent import _followup_payload
        assert _followup_payload("x", 3) is None

    def test_followup_payload_desenvuelve_antes_de_truncar(self):
        # No debe truncar contando el wrapper que de todos modos se va a
        # quitar — el núcleo real debe sobrevivir completo si cabe en 1500.
        from agents.scheduler.agent import _followup_payload
        marker = "[Contexto: el usuario respondía a Cassiel sobre: "
        wrapped = f"{marker}núcleo corto]"
        assert _followup_payload(wrapped, 1) == {"q": "núcleo corto", "n": 1}

    async def test_route_with_followup_fuerza_reminder(self, monkeypatch):
        # Hook compartido por /chat y /chat/stream: con followup pendiente,
        # la decisión es reminder sin pasar por el router.
        import agents.scheduler.agent as cassiel_mod
        monkeypatch.setattr(cassiel_mod, "pop_followup",
                            lambda user: {"q": "recuérdame lo de la casa", "n": 1})
        from interfaces.api.routers.chat import _route_with_followup
        q, decision = await _route_with_followup("u@x.com", "mañana")
        assert decision.intent == "reminder"
        assert decision.routing_reason == "cassiel_followup"
        assert "mañana" in q and "recuérdame lo de la casa" in q
        assert q.index("mañana") < q.index("recuérdame lo de la casa")

    async def test_route_with_followup_sin_pendiente_rutea_normal(self, monkeypatch):
        import agents.scheduler.agent as cassiel_mod
        monkeypatch.setattr(cassiel_mod, "pop_followup", lambda user: None)
        from interfaces.api.routers.chat import _route_with_followup
        q, decision = await _route_with_followup("u@x.com", "¿qué pods hay en el cluster?")
        assert q == "¿qué pods hay en el cluster?"
        assert decision.intent == "kubernetes"
        assert decision.routing_reason != "cassiel_followup"


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
        # Fix 3 — conjugaciones de «recordar» + referencia temporal
        "Recuerda de mi mañana de revisar licenciada para el contrato de la casa",
        "recuerda que mañana a las 9 tengo lo del contrato",
        "me recuerdes mañana comprar los útiles",
        "recuérdame hoy a las 5 llamar al banco",
    ])
    async def test_frases_de_tarea_rutean_a_reminder(self, frase):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == "reminder", f"{frase!r} → {decision.intent!r}"

    @pytest.mark.parametrize("frase,intent", [
        ("recuerda lo que te dije del proyecto", "memory"),
        # «la semana pasada» NO es la referencia temporal «la próxima semana»
        ("¿recuerdas qué hablamos la semana pasada?", "memory"),
        ("agenda una reunión con Marco el jueves", "productivity"),
        ("necesito el estado del cluster", "kubernetes"),
    ])
    async def test_no_se_come_otros_intents(self, frase, intent):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == intent

    async def test_texto_largo_pegado_no_dispara_productivity(self, monkeypatch):
        # Fix 4b — un aviso escolar pegado (caso real 17-ago) menciona
        # «agenda» y «fecha» pero no es una petición de calendario/correo.
        from orchestration.agent_router import AgentRouter

        async def sin_llm(self, question):
            return None
        monkeypatch.setattr(AgentRouter, "_route_with_llm", sin_llm)

        aviso = (
            "Estimados padres de familia: les compartimos la siguiente agenda "
            "de actividades del ciclo escolar. La primera fecha importante es "
            "la junta de bienvenida, donde se explicará la dinámica del año. "
            "Después tendremos la semana de evaluaciones diagnósticas, la "
            "entrega de libros y materiales, y el festival de inicio de curso. "
            "Les pedimos puntualidad en la entrada, marcar todos los útiles "
            "con nombre completo y revisar diariamente la libreta de tareas. "
            "Agradecemos su apoyo y quedamos atentos a cualquier duda."
        )
        assert len(aviso) > 400
        decision = await AgentRouter().route(aviso)
        assert decision.intent != "productivity"

    @pytest.mark.parametrize("frase", [
        "agenda una reunión con Marco el jueves",
        "revisa mi correo",
    ])
    async def test_peticiones_cortas_de_productivity_intactas(self, frase):
        from orchestration.agent_router import AgentRouter
        decision = await AgentRouter().route(frase)
        assert decision.intent == "productivity"


class TestRenderToolOutput:
    """Fix 4a — el dict crudo de una herramienta jamás llega a WhatsApp."""

    def test_dict_emails_se_formatea_legible(self):
        from orchestration.agent_dispatcher import _render_tool_output
        out = _render_tool_output({"emails": [
            {"subject": "Factura CFE", "from": "cfe@cfe.mx", "date": "2026-08-15"},
            {"subject": "Aviso escolar", "from": "colegio@x.mx", "date": "2026-08-16"},
        ]})
        assert out.startswith("📧 2 correos:")
        assert "Factura CFE" in out and "cfe@cfe.mx" in out
        assert "{'" not in out

    def test_dict_emails_tope_5(self):
        from orchestration.agent_dispatcher import _render_tool_output
        emails = [{"subject": f"m{i}", "from": "a@b.c", "date": ""} for i in range(9)]
        out = _render_tool_output({"emails": emails})
        assert "📧 9 correos:" in out
        assert out.count("•") == 5

    def test_dict_arbitrario_devuelve_disculpa(self):
        from orchestration.agent_dispatcher import _render_tool_output
        out = _render_tool_output({"events": [1, 2], "ok": True})
        assert "reformular" in out and "{" not in out

    def test_str_repr_de_dict_devuelve_disculpa(self):
        from orchestration.agent_dispatcher import _render_tool_output
        out = _render_tool_output("{'emails': [{'subject': 'x'}]}")
        assert "reformular" in out

    def test_str_normal_intacto(self):
        from orchestration.agent_dispatcher import _render_tool_output
        assert _render_tool_output("Todo en orden ✅") == "Todo en orden ✅"
