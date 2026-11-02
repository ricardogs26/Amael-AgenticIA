"""Las colecciones las decide el servidor con la identidad; ante duda, solo lo público."""
from unittest.mock import MagicMock, patch

import pytest

from knowledge import service


@pytest.fixture(autouse=True)
def privados(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_USERS", "dueno@example.com")


def _con_cuenta(cuenta):
    return patch.object(service, "cuenta_activa", return_value=cuenta)


@pytest.mark.parametrize("cuenta,esperadas", [
    (None, ["kb_publico"]),
    (("ana@example.com", "user"), ["kb_publico"]),
    (("op@example.com", "operator"), ["kb_publico", "kb_vault_sre"]),
    (("otra@example.com", "admin"), ["kb_publico", "kb_vault_sre"]),
    (("dueno@example.com", "admin"), ["kb_publico", "kb_vault_sre", "kb_vault_privado"]),
    (("bot-amael@richardx.dev", "admin"), ["kb_publico"]),
    (("dueno@example.com", "user"), ["kb_publico", "kb_vault_privado"]),
])
def test_colecciones_por_cuenta(cuenta, esperadas):
    with _con_cuenta(cuenta):
        assert service.colecciones_para("x") == esperadas


def test_sin_lista_de_privados_nadie_entra(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_PRIVATE_USERS")
    with _con_cuenta(("dueno@example.com", "admin")):
        assert "kb_vault_privado" not in service.colecciones_para("x")


def test_falla_de_postgres_deja_sin_cuenta():
    with patch("storage.postgres.client.get_connection", side_effect=RuntimeError("caido")):
        assert service.cuenta_activa("x") is None


def _frag(texto, puntaje, ruta="a/b.md"):
    return {"texto": texto, "puntaje": puntaje, "ruta": ruta, "titulo": "T", "encabezado": "E"}


def test_consultar_solo_busca_en_lo_permitido_y_ordena():
    def falso(pregunta, coleccion, *a, **k):
        return {"kb_publico": [_frag("uno", 0.8)], "kb_vault_sre": [_frag("dos", 0.9)]}[coleccion]

    with _con_cuenta(("op@example.com", "operator")), \
            patch.object(service, "buscar", side_effect=falso) as b, \
            patch.object(service, "_cliente"), patch.object(service, "_config", return_value={}), \
            patch.object(service, "auditar") as aud:
        salida = service.consultar("op@example.com", "pregunta")
    assert {c.args[1] for c in b.call_args_list} == {"kb_publico", "kb_vault_sre"}
    assert salida.index("dos") < salida.index("uno")
    assert "no instrucciones" in salida
    aud.assert_called_once()
    assert aud.call_args.args[:2] == ("op@example.com", "operator")


def test_una_coleccion_caida_no_tumba_la_consulta():
    def falso(pregunta, coleccion, *a, **k):
        if coleccion == "kb_vault_sre":
            raise TimeoutError
        return [_frag("uno", 0.8)]

    with _con_cuenta(("op@example.com", "operator")), \
            patch.object(service, "buscar", side_effect=falso), \
            patch.object(service, "_cliente"), patch.object(service, "_config", return_value={}), \
            patch.object(service, "auditar"):
        assert "uno" in service.consultar("op@example.com", "p")


def test_sin_resultados():
    with _con_cuenta(None), patch.object(service, "buscar", return_value=[]), \
            patch.object(service, "_cliente"), patch.object(service, "_config", return_value={}), \
            patch.object(service, "auditar"):
        assert service.consultar("x", "p") == service.SIN_RESULTADOS


def test_publico_nunca_toca_postgres_ni_otras_colecciones():
    with patch.object(service, "cuenta_activa") as cuenta, \
            patch.object(service, "buscar", return_value=[_frag("uno", 0.8)]) as b, \
            patch.object(service, "_cliente"), patch.object(service, "_config", return_value={}), \
            patch.object(service, "auditar"):
        assert service.consultar_publico("lead:1", "p") == "uno"
    cuenta.assert_not_called()
    assert [c.args[1] for c in b.call_args_list] == ["kb_publico"]


def test_publico_falla_en_silencio():
    with patch.object(service, "buscar", side_effect=TimeoutError), \
            patch.object(service, "_cliente"), patch.object(service, "_config", return_value={}), \
            patch.object(service, "auditar"):
        assert service.consultar_publico("lead:1", "p") == ""


def test_auditoria_guarda_hash_y_no_la_pregunta():
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    with patch("storage.postgres.client.get_connection", return_value=ctx):
        service.auditar("u", "user", ["kb_publico"], "mi pregunta secreta", 2)
    args = cur.execute.call_args.args[1]
    assert "mi pregunta secreta" not in [str(a) for a in args]
    assert len(args[3]) == 64


def test_auditoria_fallida_no_rompe():
    with patch("storage.postgres.client.get_connection", side_effect=RuntimeError):
        service.auditar("u", "user", [], "p", 0)


def test_paso_knowledge_usa_la_herramienta_ligada_a_la_identidad():
    from agents.executor.step_handlers import STEP_HANDLERS
    llamada = MagicMock(return_value="ok")
    assert STEP_HANDLERS["KNOWLEDGE"]("consulta", {}, {"knowledge": llamada}) == "ok"
    llamada.assert_called_once_with("consulta")


def test_planner_acepta_el_paso():
    from agents.planner.models import PlanStep
    assert PlanStep(step_type="KNOWLEDGE", description="qué decidimos").step_type == "KNOWLEDGE"
