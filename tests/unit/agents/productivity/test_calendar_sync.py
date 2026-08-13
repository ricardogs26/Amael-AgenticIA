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


# ── Marca de autoría ────────────────────────────────────────────────────────
# Sin marca, el plan de cada corrida se suma al anterior: el dedup por
# título+hora solo cubre repeticiones idénticas, y el LLM cambia los títulos
# cada vez. Marcar lo que escribe el brief permite reemplazar SU plan sin tocar
# lo que el usuario puso a mano.

def test_los_eventos_del_brief_se_marcan_como_suyos():
    servicio = MagicMock()
    with patch.object(calendar_manager, "_build_calendar_service", return_value=servicio):
        calendar_manager.create_calendar_event(
            MagicMock(), summary="Bloque", start_iso="2026-08-12T08:00:00",
            end_iso="2026-08-12T09:30:00",
        )

    body = servicio.events.return_value.insert.call_args.kwargs["body"]
    assert body["extendedProperties"]["private"]["amael_planner"] == "1"


def test_borrar_el_plan_previo_solo_toca_lo_marcado():
    servicio = MagicMock()
    servicio.events.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "a"}, {"id": "b"}]
    }
    with patch.object(calendar_manager, "_build_calendar_service", return_value=servicio):
        borrados = calendar_manager.delete_planner_events(MagicMock(), "2026-08-12")

    # El filtro se lo pide a Google, no se decide en cliente: traer todo y
    # elegir aquí sería una forma de borrar por error algo del usuario.
    kwargs = servicio.events.return_value.list.call_args.kwargs
    assert kwargs["privateExtendedProperty"] == "amael_planner=1"
    assert borrados == 2
    assert servicio.events.return_value.delete.call_count == 2


def test_sync_reemplaza_el_plan_anterior_cuando_se_le_pide():
    plan = _plan([{"title": "Bloque", "start": "08:00", "end": "09:30"}])

    with patch.object(calendar_manager, "delete_planner_events", return_value=3) as borrar, \
         patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}):
        calendar_manager.sync_plan_to_calendar(MagicMock(), plan, reemplazar=True)

    borrar.assert_called_once()
    assert borrar.call_args.args[1] == "2026-08-12"


def test_sync_no_borra_nada_por_default():
    """Borrar es destructivo: tiene que pedirse explícitamente."""
    plan = _plan([{"title": "Bloque", "start": "08:00", "end": "09:30"}])

    with patch.object(calendar_manager, "delete_planner_events") as borrar, \
         patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}):
        calendar_manager.sync_plan_to_calendar(MagicMock(), plan)

    borrar.assert_not_called()


def test_si_falla_el_borrado_igual_se_escribe_el_plan():
    """Un error limpiando no puede dejar al usuario sin plan del día."""
    plan = _plan([{"title": "Bloque", "start": "08:00", "end": "09:30"}])

    with patch.object(calendar_manager, "delete_planner_events", side_effect=RuntimeError("api caída")), \
         patch.object(calendar_manager, "create_calendar_event", return_value={"id": "x"}) as crear:
        creados = calendar_manager.sync_plan_to_calendar(MagicMock(), plan, reemplazar=True)

    crear.assert_called_once()
    assert creados == 1
