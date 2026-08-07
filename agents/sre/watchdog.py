"""
agents.sre.watchdog — vigilante externo de deployments críticos.

Motivación (incidente ollama, 31-jul-2026): el nodo se reinició, dejó 5 pods de
`ollama-deployment` en ContainerStatusUnknown reteniendo la GPU, y el clúster
estuvo ~15 h sin LLM. Raphael sí lo detectó, pero su remediación fallaba y su
notificación estaba mal configurada, así que nadie se enteró.

Este módulo es deliberadamente **independiente** de Raphael:
  - No usa el SRE loop, ni el LLM, ni Qdrant, ni el registry de agentes.
  - Corre como CronJob aparte, en su propio pod y con su propia ServiceAccount.
  - Si Raphael está caído, en CrashLoop o mudo, esto sigue avisando.

Comprueba dos cosas y manda un WhatsApp si fallan: que los deployments críticos
tengan al menos una réplica disponible, y que Vault no esté sellado. Nada más —
cada función extra es una forma nueva de que el vigilante falle en silencio.

Lo de Vault entró el 7-ago-2026. Un pod de Vault que reinicia arranca SELLADO
siempre: el proceso corre y el Deployment se ve sano, pero no entrega secretos.
Esa mañana el brief de las 7:00 avisó «no se encontraron credenciales de Google
Calendar, autoriza el acceso» — la autorización estaba intacta; Vault llevaba
cuatro horas sellado tras un reciclado del cluster. Un fallo que solo se nota
por un mensaje engañoso a otra hora es exactamente lo que este módulo existe
para evitar. NO lo abre: solo avisa (las llaves Shamir no viven en el cluster).

Uso:
    python -m agents.sre.watchdog

Env vars:
    WATCHDOG_DEPLOYMENTS   CSV `namespace/nombre` (default: los de amael-ia)
    WATCHDOG_REALERT_MIN   minutos entre avisos repetidos del mismo recurso (60)
    WATCHDOG_VAULT_ADDR    URL de Vault ("" desactiva el chequeo)
    OWNER_PHONE            teléfono destino
    WHATSAPP_BRIDGE_URL    bridge (default http://whatsapp-bridge-service:3000)

Salida: exit 0 si todo sano, exit 1 si hubo algún deployment caído (para que el
Job quede marcado como fallido y sea visible en `kubectl get jobs`).
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [watchdog] %(message)s",
)
logger = logging.getLogger("agents.sre.watchdog")

_DEFAULT_TARGETS = (
    "amael-ia/ollama-deployment,"
    "amael-ia/postgres,"
    "amael-ia/redis,"
    "amael-ia/qdrant,"
    "amael-ia/amael-agentic-deployment,"
    "amael-ia/raphael-service"
)

_TARGETS      = os.environ.get("WATCHDOG_DEPLOYMENTS", _DEFAULT_TARGETS)
_REALERT_MIN  = int(os.environ.get("WATCHDOG_REALERT_MIN", "60"))
_VAULT_ADDR   = os.environ.get("WATCHDOG_VAULT_ADDR",
                               "http://vault.vault.svc.cluster.local:8200")
_BRIDGE_URL   = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
_PHONE        = (
    os.environ.get("OWNER_PHONE")
    or os.environ.get("SRE_ALERT_PHONE")
    or os.environ.get("ADMIN_PHONE")
    or ""
)


def _parse_targets(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        ns, _, name = item.partition("/")
        if name:
            out.append((ns, name))
        else:
            logger.warning(f"Target mal formado (falta namespace/): {item!r}")
    return out


def check_deployment(apps_v1, namespace: str, name: str) -> str | None:
    """Devuelve un mensaje de problema, o None si el deployment está sano."""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            return f"`{namespace}/{name}` NO EXISTE en el clúster."
        # Un error de API no es prueba de que el deployment esté caído; no
        # inventamos una caída, pero tampoco la ocultamos.
        return f"`{namespace}/{name}` no se pudo consultar: {exc}"

    desired   = dep.spec.replicas or 0
    available = dep.status.available_replicas or 0

    if desired == 0:
        return None  # escalado a cero a propósito (demos en standby)
    if available < 1:
        return (
            f"`{namespace}/{name}` SIN RÉPLICAS DISPONIBLES "
            f"({available}/{desired})."
        )
    if available < desired:
        return (
            f"`{namespace}/{name}` degradado: {available}/{desired} réplicas."
        )
    return None


def check_vault() -> str | None:
    """
    ¿Vault está sellado? Devuelve el problema, o None si entrega secretos.

    Un pod de Vault recién arrancado corre y responde, así que el Deployment se
    ve sano y `check_deployment` no ve nada — pero sellado no entrega un solo
    secreto. `/v1/sys/seal-status` es público (no lleva token) y es la única
    forma honesta de saberlo desde fuera.

    Devuelve el aviso con el recurso entre backticks porque `main()` saca de ahí
    la clave de dedup.
    """
    if not _VAULT_ADDR:
        return None
    try:
        import requests

        r = requests.get(f"{_VAULT_ADDR}/v1/sys/seal-status", timeout=10)
        data = r.json()
    except Exception as exc:
        # No poder preguntarle a Vault no prueba que esté sellado, pero un
        # gestor de secretos incomunicado tampoco es un estado sano.
        return f"`vault/seal-status` no se pudo consultar: {exc}"

    if not data.get("initialized", True):
        return "`vault/vault-0` NO INICIALIZADO."
    if data.get("sealed"):
        progreso = data.get("progress", 0)
        umbral = data.get("t", "?")
        return (
            f"`vault/vault-0` SELLADO ({progreso}/{umbral} llaves). "
            "Los secretos de Google no se pueden leer: el brief diario y "
            "productivity-service van a fallar."
        )
    return None


def _should_alert(key: str) -> bool:
    """Dedup por Redis. Si Redis no responde, se alerta igual (fail-loud)."""
    try:
        import redis

        r = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis-service"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            decode_responses=True,
            socket_timeout=5,
        )
        return bool(r.set(f"watchdog:alerted:{key}", "1", nx=True, ex=_REALERT_MIN * 60))
    except Exception as exc:
        logger.warning(f"Redis no disponible ({exc}) — se alerta sin dedup.")
        return True


def send_alert(problems: list[str]) -> bool:
    if not _PHONE:
        logger.error("OWNER_PHONE/SRE_ALERT_PHONE sin definir — no se puede alertar.")
        return False

    hay_vault = any("vault/" in p for p in problems)
    text = (
        "🚨 *WATCHDOG — servicio crítico caído*\n\n"
        + "\n".join(f"• {p}" for p in problems)
        + "\n\nRevisa `kubectl get pods -n amael-ia`. "
        "Si hay pods en Unknown tras un reinicio del nodo, están reteniendo "
        "recursos (GPU/PVC) y hay que borrarlos con --force."
    )
    if hay_vault:
        # Vault no se arregla mirando pods: necesita las llaves, que a
        # propósito no viven en el cluster.
        text += (
            "\n\nPara Vault: `kubectl exec -n vault vault-0 -- vault operator "
            "unseal <LLAVE>` tres veces, con las llaves de `vault.root`."
        )
    try:
        import requests

        resp = requests.post(
            f"{_BRIDGE_URL}/send",
            json={"phoneNumber": _PHONE, "text": text},
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Alerta WhatsApp enviada.")
            return True
        logger.error(f"Bridge respondió {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"Fallo enviando alerta: {exc}")
    return False


def main() -> int:
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    apps_v1 = client.AppsV1Api()

    targets = _parse_targets(_TARGETS)
    logger.info(f"Vigilando {len(targets)} deployments críticos.")

    problems: list[str] = []
    for ns, name in targets:
        problem = check_deployment(apps_v1, ns, name)
        if problem:
            logger.error(problem)
            problems.append(problem)
        else:
            logger.info(f"OK — {ns}/{name}")

    vault_problem = check_vault()
    if vault_problem:
        logger.error(vault_problem)
        problems.append(vault_problem)
    elif _VAULT_ADDR:
        logger.info("OK — vault (abierto)")

    if not problems:
        logger.info("Todos los deployments críticos están sanos.")
        return 0

    nuevos = [p for p in problems if _should_alert(p.split("`")[1])]
    if nuevos:
        send_alert(nuevos)
    else:
        logger.info(f"{len(problems)} problema(s) ya notificados — dedup activo.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
