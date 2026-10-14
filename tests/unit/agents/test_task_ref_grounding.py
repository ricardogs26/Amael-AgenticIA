"""Caso real 14-oct-2026: Ricardo citó el brief y escribió «Correos de
optionwright no son importantes, descartarlos»; Cassiel preguntó «¿Cuál
pendiente?», él contestó «Deadline fallido» y el LLM devolvió task_done con
task_ref «#2» — tomado del brief citado, no de sus palabras — y se cerró
«Inscribir divorcio en registro civil». Regla: cerrar/cancelar/posponer
exige que la referencia esté en las PALABRAS DEL USUARIO (id o palabra del
título), nunca solo en el contexto."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

from agents.scheduler import agent as ag
from agents.scheduler import tasks_storage as ts


def _t(i, title, **kw):
    base = dict(id=i, user_id="u@x.com", title=title, description="", category="personal",
                priority="media", estimated_minutes=None, due_date=None, status="pending",
                needs_scheduling=False, calendar_event_id=None, last_nudge_at=None,
                created_at=datetime(2026, 10, 1, tzinfo=UTC), completed_at=None)
    base.update(kw)
    return ts.Task(**base)


DIVORCIO = _t(2, "Inscribir divorcio en registro civil", priority="alta")
PS5 = _t(1, "Recomendación PS5 Regis")
CONTEXTO = ("Deadline fallido\n\n[Contexto: el usuario respondía a Cassiel sobre: "
            "[Contexto: el usuario responde citando el brief del día: 📝 *Pendientes* "
            "• #2 Inscribir divorcio en registro civil • #1 Recomendación PS5 Regis] "
            "Correos de optionwright no son importantes, descartarlos]")


class TestPalabrasDelUsuario:
    def test_quita_los_bloques_de_contexto(self):
        assert ag._user_words(CONTEXTO) == "Deadline fallido"

    def test_sin_contexto_devuelve_todo(self):
        assert ag._user_words("ya lo hice, el #2") == "ya lo hice, el #2"


class TestGrounding:
    def test_id_en_las_palabras_del_usuario(self):
        assert ag._ref_grounded("ya lo hice, el #2", DIVORCIO)
        assert ag._ref_grounded("cierra la 2", DIVORCIO)

    def test_palabra_del_titulo_en_las_palabras_del_usuario(self):
        assert ag._ref_grounded("ya inscribí el divorcio", DIVORCIO)
        assert ag._ref_grounded("Ya compré la recomendación de Regis", PS5)

    def test_solo_en_el_contexto_no_cuenta(self):
        assert not ag._ref_grounded(CONTEXTO, DIVORCIO)

    def test_palabras_vacias_no_cuentan(self):
        # «en», «de», «el» aparecen en cualquier frase.
        assert not ag._ref_grounded("ya lo hice en la tarde", DIVORCIO)


class _Cassiel:
    def __init__(self, last_query):
        self._apply_task = ag.CassielAgent._apply_task.__get__(self)
        self._set_followup = lambda *a, **k: True
        self._last_query = last_query
        self._next_followup_round = 2


def test_el_caso_real_no_cierra_nada_y_lista_los_pendientes():
    agente = _Cassiel(CONTEXTO)
    with patch.object(ts, "find_task", return_value=DIVORCIO), \
         patch.object(ts, "list_pending", return_value=[DIVORCIO, PS5]), \
         patch.object(ts, "set_status") as st:
        out = agente._apply_task("task_done", {"task_ref": "2"}, "u@x.com", "America/Mexico_City")
    st.assert_not_called()
    assert "Deadline fallido" in out and "#2" in out and "#1" in out


def test_con_la_referencia_en_sus_palabras_si_cierra():
    agente = _Cassiel("ya inscribí el divorcio")
    with patch.object(ts, "find_task", return_value=DIVORCIO), \
         patch.object(ts, "set_status", return_value=True) as st:
        out = agente._apply_task("task_done", {"task_ref": "divorcio"}, "u@x.com", "America/Mexico_City")
    st.assert_called_once()
    assert "Cerrada #2" in out


def test_posponer_tambien_exige_grounding():
    agente = _Cassiel(CONTEXTO)
    with patch.object(ts, "find_task", return_value=DIVORCIO), \
         patch.object(ts, "list_pending", return_value=[DIVORCIO]), \
         patch.object(ts, "postpone_task") as pp:
        agente._apply_task("task_postpone", {"task_ref": "2", "new_due": "2026-10-20"},
                           "u@x.com", "America/Mexico_City")
    pp.assert_not_called()
