"""Lógica pura del rotador de llaves y tokens de servicio."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from security import rotacion_tokens as rt

AHORA = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


def _llavero(creada=AHORA - timedelta(days=10)):
    return {
        "activa": "k1",
        "llaves": {"k1": {"secreto": "s" * 64, "creada": creada.isoformat()}},
        "legado_sin_kid": "k1",
        "servicio_sin_exp": True,
    }


def test_rotar_deja_la_anterior_con_gracia():
    nuevo = rt.rotar_llave(_llavero(), AHORA)
    assert nuevo["activa"] == "k202611022000"
    assert len(nuevo["llaves"][nuevo["activa"]]["secreto"]) == 128
    hasta = datetime.fromisoformat(nuevo["llaves"]["k1"]["aceptar_hasta"])
    assert hasta == AHORA + timedelta(days=rt.GRACIA_DIAS)
    assert rt.GRACIA_DIAS > 7  # más que una sesión web


def test_rotar_no_alarga_una_gracia_mas_corta():
    llavero = _llavero()
    corta = (AHORA + timedelta(days=1)).isoformat()
    llavero["llaves"]["k1"]["aceptar_hasta"] = corta
    assert rt.rotar_llave(llavero, AHORA)["llaves"]["k1"]["aceptar_hasta"] == corta


def test_purgar_quita_vencidas_y_el_legado():
    llavero = rt.rotar_llave(_llavero(), AHORA)
    mismo, quitadas = rt.purgar(llavero, AHORA + timedelta(days=1))
    assert quitadas == [] and "k1" in mismo["llaves"]
    limpio, quitadas = rt.purgar(llavero, AHORA + timedelta(days=rt.GRACIA_DIAS, seconds=1))
    assert quitadas == ["k1"]
    assert "legado_sin_kid" not in limpio


def test_llave_vieja_a_los_180_dias():
    assert not rt.llave_vieja(_llavero(), AHORA)
    assert rt.llave_vieja(_llavero(AHORA - timedelta(days=rt.LLAVE_DIAS)), AHORA)


def test_emitir_y_motivos_de_renovacion():
    llavero = _llavero()
    token, claims = rt.emitir("bridge", llavero, AHORA)
    assert jwt.get_unverified_header(token)["kid"] == "k1"
    assert claims["svc"] == "bridge" and claims["sub"] == rt.BOT_USER
    assert claims["exp"] - claims["iat"] == rt.TOKEN_DIAS * 86400
    assert rt.motivo_renovar(token, llavero, AHORA) is None
    tarde = AHORA + timedelta(days=rt.TOKEN_DIAS - rt.RENOVAR_DIAS)
    assert rt.motivo_renovar(token, llavero, tarde).startswith("vence")
    otra = rt.rotar_llave(llavero, AHORA)
    assert rt.motivo_renovar(token, otra, AHORA) == "firmado con otra llave"


def test_motivos_para_tokens_viejos_o_ausentes():
    llavero = _llavero()
    viejo = jwt.encode({"sub": rt.BOT_USER}, "s" * 64, algorithm="HS256")
    assert rt.motivo_renovar(None, llavero, AHORA) == "sin token"
    assert rt.motivo_renovar("basura", llavero, AHORA) == "token ilegible"
    assert rt.motivo_renovar(viejo, llavero, AHORA) == "formato viejo"


def test_token_emitido_lo_acepta_el_backend(tmp_path, monkeypatch):
    import json

    from security import llaves_jwt as lj
    llavero = _llavero(datetime.now(UTC))
    ruta = tmp_path / "llaves.json"
    ruta.write_text(json.dumps(llavero))
    monkeypatch.setattr(lj, "ARCHIVO", ruta)
    monkeypatch.setattr(lj, "jti_vigente", lambda jti: True)
    lj._reiniciar_cache()
    token, _ = rt.emitir("personal", llavero, datetime.now(UTC))
    assert lj.decodificar(token)["svc"] == "personal"
    lj._reiniciar_cache()
