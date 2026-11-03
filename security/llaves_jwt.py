"""
Llaves de firma JWT con versión (`kid`) y tokens de servicio con caducidad.

Las llaves viven en el Secret `amael-jwt-keys`, montado como archivo. Kubernetes
actualiza el archivo cuando el rotador cambia el Secret, y este módulo lo relee
sin reiniciar el pod. Formato:

    {
      "activa": "k202611021930",
      "llaves": {
        "k202611021930": {"secreto": "...", "creada": "2026-11-02T19:30:00+00:00"},
        "k0":            {"secreto": "...", "creada": "...", "aceptar_hasta": "2026-11-10T..."}
      },
      "legado_sin_kid": "k0",        # tokens sin `kid` (emitidos antes de esto) se validan con esta
      "servicio_sin_exp": false      # true solo mientras viva el token de servicio compartido viejo
    }

Sin archivo se comporta como antes: firma y valida con `JWT_SECRET_KEY`, sin `kid`.

Tokens de servicio: `sub` = bot, `svc` = servicio, `jti` registrado en la tabla
`service_tokens`. Un `jti` desconocido o con `revocar_desde` vencido se rechaza.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jwt

logger = logging.getLogger("security.llaves_jwt")

ARCHIVO = Path(os.environ.get("JWT_KEYS_FILE", "/etc/amael/jwt/llaves.json"))
BOT_USER = "bot-amael@richardx.dev"
_RELEER_S = 15
_CACHE_JTI_S = 60


class TokenRechazado(Exception):
    """El token no es válido. El mensaje es apto para el cliente."""


@dataclass(frozen=True)
class Llavero:
    activa: str | None
    llaves: dict[str, dict]
    legado_sin_kid: str | None
    servicio_sin_exp: bool

    def secreto(self, kid: str | None, ahora: datetime | None = None) -> str | None:
        """Secreto aceptado para ese `kid`, o None si no existe o ya venció su gracia."""
        if kid is None:
            kid = self.legado_sin_kid
        if kid is None or kid not in self.llaves:
            return None
        llave = self.llaves[kid]
        hasta = llave.get("aceptar_hasta")
        if hasta and datetime.fromisoformat(hasta) <= (ahora or datetime.now(UTC)):
            return None
        return llave["secreto"]


def llavero_desde_dict(datos: dict) -> Llavero:
    activa = datos.get("activa")
    llaves = datos.get("llaves") or {}
    if activa not in llaves:
        raise ValueError("la llave activa no está en el llavero")
    return Llavero(
        activa=activa,
        llaves=llaves,
        legado_sin_kid=datos.get("legado_sin_kid"),
        servicio_sin_exp=bool(datos.get("servicio_sin_exp", False)),
    )


def _llavero_legado() -> Llavero:
    from config.settings import settings
    return Llavero(
        activa=None,
        llaves={"_env": {"secreto": settings.jwt_secret_key}},
        legado_sin_kid="_env",
        servicio_sin_exp=True,
    )


_lock = threading.Lock()
_cache: tuple[float, str | None, Llavero] | None = None


def llavero() -> Llavero:
    """Llavero vigente; relee el archivo cada pocos segundos. Sin archivo, el de `JWT_SECRET_KEY`."""
    global _cache
    ahora = time.monotonic()
    with _lock:
        if _cache and ahora - _cache[0] < _RELEER_S:
            return _cache[2]
        try:
            crudo = ARCHIVO.read_text()
        except FileNotFoundError:
            crudo = None
        if crudo is None:
            # Sin archivo: siempre el JWT_SECRET_KEY vigente, sin cache.
            _cache = None
            return _llavero_legado()
        if _cache and crudo == _cache[1]:
            _cache = (ahora, crudo, _cache[2])
            return _cache[2]
        try:
            nuevo = llavero_desde_dict(json.loads(crudo))
        except Exception as exc:
            # Un archivo roto no debe dejar entrar a nadie con una llave vieja:
            # se conserva el último llavero bueno y se avisa en el log.
            logger.error(f"[llaves] archivo de llaves inválido: {exc}")
            if _cache:
                return _cache[2]
            raise
        if not _cache or _cache[2].activa != nuevo.activa:
            logger.info(f"[llaves] llave activa: {nuevo.activa}")
        _cache = (ahora, crudo, nuevo)
        return nuevo


def _reiniciar_cache() -> None:
    """Para pruebas."""
    global _cache
    with _lock:
        _cache = None
        _jti_cache.clear()


def firmar(claims: dict, juego: Llavero | None = None) -> str:
    """Firma con la llave activa (con `kid`), o con `JWT_SECRET_KEY` si no hay llavero."""
    from config.settings import settings
    juego = juego or llavero()
    if juego.activa is None:
        return jwt.encode(claims, juego.llaves["_env"]["secreto"], algorithm=settings.jwt_algorithm)
    return jwt.encode(
        claims,
        juego.llaves[juego.activa]["secreto"],
        algorithm=settings.jwt_algorithm,
        headers={"kid": juego.activa},
    )


def decodificar(token: str) -> dict:
    """Valida firma, `exp` y reglas de servicio. Devuelve el payload o lanza TokenRechazado."""
    from config.settings import settings
    juego = llavero()
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise TokenRechazado("Token inválido o expirado") from exc
    secreto = juego.secreto(kid)
    if secreto is None:
        raise TokenRechazado("Token inválido o expirado")
    try:
        payload = jwt.decode(token, secreto, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenRechazado("Token inválido o expirado") from exc

    usuario = payload.get("sub") or payload.get("email")
    if not usuario:
        raise TokenRechazado("Token inválido: falta 'sub'")

    if usuario == BOT_USER:
        if payload.get("svc"):
            if "exp" not in payload or not payload.get("jti"):
                raise TokenRechazado("Token de servicio incompleto")
            if not jti_vigente(payload["jti"]):
                raise TokenRechazado("Token de servicio revocado")
        elif "exp" not in payload and not juego.servicio_sin_exp:
            # El token compartido viejo (sin exp ni svc) vale solo hasta cerrar el legado.
            raise TokenRechazado("Token de servicio sin caducidad")
    elif "exp" not in payload:
        raise TokenRechazado("Sesión sin caducidad: vuelve a iniciar sesión")
    return payload


_jti_cache: dict[str, tuple[float, bool]] = {}


def jti_vigente(jti: str) -> bool:
    """True si el jti está registrado y no revocado. Ante error de Postgres, True (y log)."""
    ahora = time.monotonic()
    hit = _jti_cache.get(jti)
    if hit and ahora - hit[0] < _CACHE_JTI_S:
        return hit[1]
    try:
        from storage.postgres.client import get_connection
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT revocar_desde IS NULL OR revocar_desde > NOW() "
                "FROM service_tokens WHERE jti = %s",
                (jti,),
            )
            fila = cur.fetchone()
        vigente = bool(fila and fila[0])
    except Exception as exc:
        # Sin Postgres el backend casi no sirve; tumbar además WhatsApp no ayuda.
        logger.warning(f"[llaves] no se pudo consultar service_tokens: {exc}")
        return True
    _jti_cache[jti] = (ahora, vigente)
    return vigente


def kids_aceptados() -> dict:
    """Lo que ve este pod: llave activa y `kid` aceptados. Sin secretos."""
    juego = llavero()
    ahora = datetime.now(UTC)
    return {
        "activa": juego.activa,
        "aceptadas": sorted(k for k in juego.llaves if juego.secreto(k, ahora) and k != "_env"),
        "servicio_sin_exp": juego.servicio_sin_exp,
    }
