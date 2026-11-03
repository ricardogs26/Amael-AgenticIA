"""Llavero JWT con kid y tokens de servicio (rotación automática, nov-2026)."""
from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

os.environ.setdefault("INTERNAL_API_SECRET", "test-internal-secret-" + "x" * 32)
os.environ.setdefault("JWT_SECRET_KEY",      "test-jwt-secret-" + "x" * 32)
os.environ.setdefault("SESSION_SECRET_KEY",  "test-session-secret-" + "x" * 32)
os.environ.setdefault("POSTGRES_PASSWORD",   "test")
os.environ.setdefault("MINIO_ACCESS_KEY",    "test-minio-access-key")
os.environ.setdefault("MINIO_SECRET_KEY",    "test-minio-secret-key")

import jwt  # noqa: E402
import pytest  # noqa: E402

from security import llaves_jwt as lj  # noqa: E402

BOT = lj.BOT_USER
VIEJA = "v" * 64
NUEVA = "n" * 64


def _exp(dias=1):
    return int((datetime.now(UTC) + timedelta(days=dias)).timestamp())


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "llaves.json"
    monkeypatch.setattr(lj, "ARCHIVO", ruta)
    monkeypatch.setattr(lj, "jti_vigente", lambda jti: jti != "revocado")
    lj._reiniciar_cache()
    yield ruta
    lj._reiniciar_cache()


def _escribir(ruta, **extra):
    datos = {
        "activa": "k2",
        "llaves": {
            "k2": {"secreto": NUEVA, "creada": datetime.now(UTC).isoformat()},
            "k0": {"secreto": VIEJA,
                   "aceptar_hasta": (datetime.now(UTC) + timedelta(days=8)).isoformat()},
        },
        "legado_sin_kid": "k0",
        "servicio_sin_exp": False,
    }
    datos.update(extra)
    ruta.write_text(json.dumps(datos))
    lj._reiniciar_cache()


def test_sin_archivo_firma_y_valida_con_env(archivo):
    token = lj.firmar({"sub": "a@b.c", "exp": _exp()})
    assert "kid" not in jwt.get_unverified_header(token)
    assert lj.decodificar(token)["sub"] == "a@b.c"


def test_sin_archivo_el_bot_viejo_sin_exp_sigue_valiendo(archivo):
    from config.settings import settings
    token = jwt.encode({"sub": BOT}, settings.jwt_secret_key, algorithm="HS256")
    assert lj.decodificar(token)["sub"] == BOT


def test_firma_con_la_activa_y_kid(archivo):
    _escribir(archivo)
    token = lj.firmar({"sub": "a@b.c", "exp": _exp()})
    assert jwt.get_unverified_header(token)["kid"] == "k2"
    assert lj.decodificar(token)["sub"] == "a@b.c"


def test_sesion_vieja_sin_kid_vale_durante_la_gracia(archivo):
    _escribir(archivo)
    token = jwt.encode({"sub": "a@b.c", "exp": _exp()}, VIEJA, algorithm="HS256")
    assert lj.decodificar(token)["sub"] == "a@b.c"


def test_gracia_vencida_rechaza_la_llave_vieja(archivo):
    _escribir(archivo, llaves={
        "k2": {"secreto": NUEVA},
        "k0": {"secreto": VIEJA,
               "aceptar_hasta": (datetime.now(UTC) - timedelta(minutes=1)).isoformat()},
    })
    token = jwt.encode({"sub": "a@b.c", "exp": _exp()}, VIEJA, algorithm="HS256")
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(token)


def test_kid_desconocido_se_rechaza(archivo):
    _escribir(archivo)
    token = jwt.encode({"sub": "a@b.c", "exp": _exp()}, NUEVA, algorithm="HS256",
                       headers={"kid": "k9"})
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(token)


def test_kid_cambiado_a_otra_llave_falla_la_firma(archivo):
    _escribir(archivo)
    token = jwt.encode({"sub": "a@b.c", "exp": _exp()}, VIEJA, algorithm="HS256",
                       headers={"kid": "k2"})
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(token)


def test_bot_compartido_sin_exp_muere_al_cerrar_legado(archivo):
    _escribir(archivo, servicio_sin_exp=True)
    token = jwt.encode({"sub": BOT}, VIEJA, algorithm="HS256")
    assert lj.decodificar(token)["sub"] == BOT
    _escribir(archivo, servicio_sin_exp=False)
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(token)


def _svc(**claims):
    base = {"sub": BOT, "svc": "bridge", "jti": "abc", "exp": _exp(90)}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, NUEVA,
                      algorithm="HS256", headers={"kid": "k2"})


def test_token_de_servicio_valido(archivo):
    _escribir(archivo)
    assert lj.decodificar(_svc())["svc"] == "bridge"


@pytest.mark.parametrize("cambio", [{"jti": None}, {"exp": None}, {"jti": "revocado"}],
                         ids=["sin-jti", "sin-exp", "revocado"])
def test_token_de_servicio_incompleto_o_revocado(archivo, cambio):
    _escribir(archivo)
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(_svc(**cambio))


def test_usuario_sin_exp_se_rechaza(archivo):
    _escribir(archivo)
    token = jwt.encode({"sub": "a@b.c"}, NUEVA, algorithm="HS256", headers={"kid": "k2"})
    with pytest.raises(lj.TokenRechazado):
        lj.decodificar(token)


def test_archivo_roto_conserva_el_ultimo_bueno(archivo, monkeypatch):
    _escribir(archivo)
    assert lj.llavero().activa == "k2"
    archivo.write_text("{no json")
    monkeypatch.setattr(lj, "_RELEER_S", 0)
    time.sleep(0.01)
    assert lj.llavero().activa == "k2"


def test_relee_el_archivo_al_cambiar(archivo, monkeypatch):
    monkeypatch.setattr(lj, "_RELEER_S", 0)
    _escribir(archivo)
    datos = json.loads(archivo.read_text())
    datos["llaves"]["k3"] = {"secreto": "t" * 64}
    datos["activa"] = "k3"
    archivo.write_text(json.dumps(datos))
    assert lj.llavero().activa == "k3"


def test_kids_aceptados_no_expone_secretos(archivo):
    _escribir(archivo)
    info = lj.kids_aceptados()
    assert info == {"activa": "k2", "aceptadas": ["k0", "k2"], "servicio_sin_exp": False}
    assert VIEJA not in json.dumps(info) and NUEVA not in json.dumps(info)
