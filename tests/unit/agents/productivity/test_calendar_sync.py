"""Pruebas de la escritura del brief diario al calendario.

Contexto (12-ago-2026): el brief de las 7:00 creó 11 eventos que taparon el día
de 08:00 a 17:00 sin un hueco, y uno de ellos era una COPIA de un evento
recurrente que el usuario ya tenía («Checkpoint lectura Rewired»). El prompt le
pide al LLM «respetar los eventos ya agendados» y el modelo los incluye en el
plan; como todo lo que sale en el plan se escribe, se duplican.
"""
from unittest.mock import MagicMock, patch

from agents.productivity import calendar_manager


def _plan(tasks):
    return {"date": "2026-08-12", "tasks": tasks}


def test_no_recrea_un_evento_que_ya_existe():
    existentes = [{"summary": "Checkpoint lectura Rewired", "start": "2026-08-12T13:00:00-06:00"}]
    plan = _plan([
        {"title": "Checkpoint lectura Rewired", "start": "13:00", "end": "13:15"},
        {"title": "Bloque de concentración",    "start": "08:00", "end": "09:30"},
    ])

    with patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}) as crear:
        creados = calendar_manager.sync_plan_to_calendar(MagicMock(), plan, existentes=existentes)

    titulos = [c.kwargs["summary"] for c in crear.call_args_list]
    assert "Checkpoint lectura Rewired" not in titulos
    assert titulos == ["Bloque de concentración"]
    assert creados == 1


def test_el_duplicado_se_detecta_sin_importar_may_minusculas_ni_espacios():
    existentes = [{"summary": "  checkpoint LECTURA rewired ", "start": "2026-08-12T13:00:00-06:00"}]
    plan = _plan([{"title": "Checkpoint lectura Rewired", "start": "13:00", "end": "13:15"}])

    with patch.object(calendar_manager, "create_calendar_event") as crear:
        creados = calendar_manager.sync_plan_to_calendar(MagicMock(), plan, existentes=existentes)

    crear.assert_not_called()
    assert creados == 0


def test_mismo_titulo_a_otra_hora_si_se_crea():
    """Un bloque que se repite a lo largo del día es legítimo: solo cuenta como
    duplicado si coincide título Y hora de inicio."""
    existentes = [{"summary": "Bloque de concentración", "start": "2026-08-12T08:00:00-06:00"}]
    plan = _plan([{"title": "Bloque de concentración", "start": "15:15", "end": "16:45"}])

    with patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}) as crear:
        creados = calendar_manager.sync_plan_to_calendar(MagicMock(), plan, existentes=existentes)

    crear.assert_called_once()
    assert creados == 1


def test_sin_lista_de_existentes_se_comporta_como_antes():
    """Compatibilidad: el parámetro es opcional."""
    plan = _plan([{"title": "Bloque", "start": "08:00", "end": "09:30"}])

    with patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}) as crear:
        assert calendar_manager.sync_plan_to_calendar(MagicMock(), plan) == 1
    crear.assert_called_once()


def test_un_evento_existente_de_dia_completo_no_rompe_la_comparacion():
    """Los eventos all-day traen `date` en vez de `dateTime`; get_todays_events
    los normaliza al mismo campo `start`, así que llega '2026-08-12' pelado."""
    existentes = [{"summary": "Vacaciones", "start": "2026-08-12"}]
    plan = _plan([{"title": "Bloque", "start": "08:00", "end": "09:30"}])

    with patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}) as crear:
        assert calendar_manager.sync_plan_to_calendar(MagicMock(), plan, existentes=existentes) == 1
    crear.assert_called_once()
