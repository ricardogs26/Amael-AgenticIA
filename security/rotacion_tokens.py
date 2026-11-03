"""
Rotación automática de la llave de firma JWT y de los tokens de servicio.

Corre como CronJob diario (`k8s/agents/19-rotacion-tokens-cronjob.yaml`):

  1. Si la llave activa tiene ≥ LLAVE_DIAS (o `--rotar-llave`): crea una nueva, la
     anterior se sigue aceptando GRACIA_DIAS (más que una sesión web de 7 días).
     Espera a que cada pod del backend acepte el `kid` nuevo antes de usarlo.
  2. Quita del llavero las llaves cuya gracia ya venció.
  3. Renueva cada token de servicio que falte, sea viejo de formato, esté firmado con
     otra llave o venza en ≤ RENOVAR_DIAS. Lo registra en `service_tokens` y lo
     escribe en su Secret; el servicio lo lee del archivo montado, sin reiniciar.
     El token anterior deja de valer GRACIA_TOKEN_MIN después.
  4. Escribe el estado en el ConfigMap `rotacion-tokens-estado` (lo lee el watchdog).

Otros usos:
  python -m security.rotacion_tokens --revocar bridge   # token nuevo y el viejo muere ya
  python -m security.rotacion_tokens --cerrar-legado    # el token compartido sin exp deja de valer
  python -m security.rotacion_tokens --simular          # dice qué haría, no escribe nada

No importa `config.settings`: necesita solo POSTGRES_* y acceso a la API de Kubernetes.
Nunca imprime secretos ni tokens.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt

logger = logging.getLogger("rotacion_tokens")

NS = os.environ.get("ROTACION_NAMESPACE", "amael-ia")
SECRET_LLAVES = "amael-jwt-keys"
ARCHIVO_LLAVES = "llaves.json"
CM_ESTADO = "rotacion-tokens-estado"
SERVICIO_BACKEND = os.environ.get("ROTACION_BACKEND_SERVICE", "amael-agentic-service")
PUERTO_BACKEND = int(os.environ.get("ROTACION_BACKEND_PORT", "8000"))
BOT_USER = "bot-amael@richardx.dev"

# servicio → Secret con la llave `token`
SERVICIOS: dict[str, str] = {
    "bridge": "amael-svc-token-bridge",
    "personal": "amael-svc-token-personal",
    "morning-audio": "amael-svc-token-morning-audio",
}

TOKEN_DIAS = int(os.environ.get("ROTACION_TOKEN_DIAS", "90"))
RENOVAR_DIAS = int(os.environ.get("ROTACION_RENOVAR_DIAS", "14"))
LLAVE_DIAS = int(os.environ.get("ROTACION_LLAVE_DIAS", "180"))
GRACIA_DIAS = int(os.environ.get("ROTACION_GRACIA_DIAS", "8"))
GRACIA_TOKEN_MIN = int(os.environ.get("ROTACION_GRACIA_TOKEN_MIN", "15"))
ESPERA_KID_S = int(os.environ.get("ROTACION_ESPERA_KID_S", "300"))


# ── Lógica pura (probada sin cluster) ────────────────────────────────────────

def nuevo_kid(ahora: datetime) -> str:
    return "k" + ahora.strftime("%Y%m%d%H%M")


def rotar_llave(llavero: dict, ahora: datetime) -> dict:
    """Llavero con una llave activa nueva; la anterior se acepta GRACIA_DIAS más."""
    kid = nuevo_kid(ahora)
    if kid in llavero["llaves"]:
        raise RuntimeError(f"ya existe la llave {kid}")
    llaves = {k: dict(v) for k, v in llavero["llaves"].items()}
    anterior = llavero["activa"]
    hasta = (ahora + timedelta(days=GRACIA_DIAS)).isoformat()
    actual = llaves[anterior].get("aceptar_hasta")
    if not actual or actual > hasta:
        llaves[anterior]["aceptar_hasta"] = hasta
    llaves[kid] = {"secreto": secrets.token_hex(64), "creada": ahora.isoformat()}
    return {**llavero, "activa": kid, "llaves": llaves}


def purgar(llavero: dict, ahora: datetime) -> tuple[dict, list[str]]:
    """Quita las llaves con la gracia vencida (nunca la activa)."""
    quitadas = [
        k for k, v in llavero["llaves"].items()
        if k != llavero["activa"] and v.get("aceptar_hasta")
        and datetime.fromisoformat(v["aceptar_hasta"]) <= ahora
    ]
    if not quitadas:
        return llavero, []
    llaves = {k: v for k, v in llavero["llaves"].items() if k not in quitadas}
    nuevo = {**llavero, "llaves": llaves}
    if nuevo.get("legado_sin_kid") in quitadas:
        nuevo.pop("legado_sin_kid")
    return nuevo, quitadas


def llave_vieja(llavero: dict, ahora: datetime) -> bool:
    creada = llavero["llaves"][llavero["activa"]].get("creada")
    if not creada:
        return True
    return ahora - datetime.fromisoformat(creada) >= timedelta(days=LLAVE_DIAS)


def motivo_renovar(token: str | None, llavero: dict, ahora: datetime) -> str | None:
    """Por qué hay que renovar este token, o None si sigue bien. No valida firma."""
    if not token:
        return "sin token"
    try:
        cabecera = jwt.get_unverified_header(token)
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return "token ilegible"
    if not claims.get("svc") or not claims.get("jti") or "exp" not in claims:
        return "formato viejo"
    if cabecera.get("kid") != llavero["activa"]:
        return "firmado con otra llave"
    vence = datetime.fromtimestamp(claims["exp"], UTC)
    if vence - ahora <= timedelta(days=RENOVAR_DIAS):
        return f"vence {vence.date().isoformat()}"
    return None


def emitir(servicio: str, llavero: dict, ahora: datetime) -> tuple[str, dict]:
    kid = llavero["activa"]
    claims = {
        "sub": BOT_USER,
        "svc": servicio,
        "jti": uuid.uuid4().hex,
        "iat": int(ahora.timestamp()),
        "exp": int((ahora + timedelta(days=TOKEN_DIAS)).timestamp()),
    }
    token = jwt.encode(claims, llavero["llaves"][kid]["secreto"], algorithm="HS256",
                       headers={"kid": kid})
    return token, claims


def vencimiento(token: str | None) -> str | None:
    if not token:
        return None
    try:
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError:
        return None
    return datetime.fromtimestamp(exp, UTC).isoformat() if exp else None


# ── Kubernetes ───────────────────────────────────────────────────────────────

def _core():
    from kubernetes import client, config
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api()


def _leer_secret(core, nombre: str, llave: str) -> str | None:
    s = core.read_namespaced_secret(nombre, NS)
    crudo = (s.data or {}).get(llave)
    return base64.b64decode(crudo).decode() if crudo else None


def _escribir_secret(core, nombre: str, llave: str, valor: str) -> None:
    cuerpo = {"data": {llave: base64.b64encode(valor.encode()).decode()}}
    core.patch_namespaced_secret(nombre, NS, cuerpo)


def _pods_backend(core) -> list[str]:
    ep = core.read_namespaced_endpoints(SERVICIO_BACKEND, NS)
    return [a.ip for s in (ep.subsets or []) for a in (s.addresses or [])]


def esperar_kid(core, kid: str) -> None:
    """Espera a que todos los pods del backend acepten `kid` (Kubernetes tarda ~1 min)."""
    import requests
    limite = time.monotonic() + ESPERA_KID_S
    while True:
        ips = _pods_backend(core)
        faltan = []
        for ip in ips:
            try:
                r = requests.get(f"http://{ip}:{PUERTO_BACKEND}/api/auth/llaves", timeout=5)
                if kid not in r.json().get("aceptadas", []):
                    faltan.append(ip)
            except Exception:
                faltan.append(ip)
        if ips and not faltan:
            logger.info(f"los {len(ips)} pods del backend aceptan {kid}")
            return
        if time.monotonic() > limite:
            raise RuntimeError(f"pods sin la llave {kid} tras {ESPERA_KID_S}s: {faltan or 'sin pods'}")
        time.sleep(10)


def escribir_estado(core, estado: dict) -> None:
    core.patch_namespaced_config_map(CM_ESTADO, NS, {"data": {"estado.json": json.dumps(estado)}})


# ── Postgres ─────────────────────────────────────────────────────────────────

def _pg():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "postgres-service"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=10,
    )


def registrar(conn, servicio: str, claims: dict, kid: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO service_tokens (jti, servicio, kid, emitido, vence) "
            "VALUES (%s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
            (claims["jti"], servicio, kid, claims["iat"], claims["exp"]),
        )
    conn.commit()


def retirar(conn, jti: str | None, revocar_ya: bool) -> None:
    """El token anterior deja de valer: ya, o tras la gracia para que el archivo se actualice."""
    if not jti:
        return
    espera = "0 minutes" if revocar_ya else f"{GRACIA_TOKEN_MIN} minutes"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE service_tokens SET revocar_desde = NOW() + %s::interval, motivo = %s "
            "WHERE jti = %s AND revocar_desde IS NULL",
            (espera, "revocado" if revocar_ya else "reemplazado", jti),
        )
    conn.commit()


def _jti(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("jti")
    except jwt.PyJWTError:
        return None


# ── Corrida ──────────────────────────────────────────────────────────────────

def correr(args) -> int:
    ahora = datetime.now(UTC)
    core = _core()
    crudo = _leer_secret(core, SECRET_LLAVES, ARCHIVO_LLAVES)
    if not crudo:
        raise RuntimeError(f"el Secret {SECRET_LLAVES} no tiene {ARCHIVO_LLAVES}")
    llavero = json.loads(crudo)
    original = json.dumps(llavero, sort_keys=True)

    rotada = False
    if args.rotar_llave or llave_vieja(llavero, ahora):
        llavero = rotar_llave(llavero, ahora)
        rotada = True
        logger.info(f"llave nueva {llavero['activa']}")
    llavero, quitadas = purgar(llavero, ahora)
    for k in quitadas:
        logger.info(f"llave {k} retirada (gracia vencida)")
    if args.cerrar_legado and llavero.get("servicio_sin_exp"):
        llavero["servicio_sin_exp"] = False
        logger.info("token de servicio compartido sin caducidad: cerrado")

    tokens = {svc: _leer_secret(core, sec, "token") for svc, sec in SERVICIOS.items()}
    por_renovar = {}
    for svc, tok in tokens.items():
        motivo = "revocado" if svc in args.revocar else motivo_renovar(tok, llavero, ahora)
        if motivo:
            por_renovar[svc] = motivo
            logger.info(f"{svc}: renovar ({motivo})")
        else:
            logger.info(f"{svc}: vigente hasta {vencimiento(tok)}")

    if args.simular:
        logger.info("simulación: no se escribió nada")
        return 0

    if json.dumps(llavero, sort_keys=True) != original:
        _escribir_secret(core, SECRET_LLAVES, ARCHIVO_LLAVES, json.dumps(llavero, indent=1))
    if rotada or (por_renovar and not args.sin_esperar):
        esperar_kid(core, llavero["activa"])

    if por_renovar:
        conn = _pg()
        try:
            for svc, motivo in por_renovar.items():
                token, claims = emitir(svc, llavero, ahora)
                # Orden: registrar, publicar, retirar. Un fallo a medias deja vigente
                # al menos uno de los dos tokens.
                registrar(conn, svc, claims, llavero["activa"])
                _escribir_secret(core, SERVICIOS[svc], "token", token)
                retirar(conn, _jti(tokens[svc]), revocar_ya=svc in args.revocar)
                tokens[svc] = token
                logger.info(f"{svc}: token nuevo hasta {vencimiento(token)} ({motivo})")
        finally:
            conn.close()

    activa = llavero["llaves"][llavero["activa"]]
    escribir_estado(core, {
        "ultimo_ok": datetime.now(UTC).isoformat(),
        "llave_activa": llavero["activa"],
        "llave_creada": activa.get("creada"),
        "servicio_sin_exp": bool(llavero.get("servicio_sin_exp")),
        "tokens": {svc: vencimiento(t) for svc, t in tokens.items()},
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--rotar-llave", action="store_true", help="llave nueva aunque no toque")
    p.add_argument("--revocar", action="append", default=[], choices=sorted(SERVICIOS),
                   help="token nuevo y el anterior deja de valer ya")
    p.add_argument("--cerrar-legado", action="store_true",
                   help="el token compartido sin caducidad deja de valer")
    p.add_argument("--sin-esperar", action="store_true",
                   help="no esperar al backend si no se rotó la llave")
    p.add_argument("--simular", action="store_true", help="no escribe nada")
    args = p.parse_args(argv)
    try:
        return correr(args)
    except Exception as exc:
        logger.error(f"rotación fallida: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
