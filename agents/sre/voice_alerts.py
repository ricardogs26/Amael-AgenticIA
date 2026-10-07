"""
Alertas CRITICAL de Raphael en voz (idea 5, 7-oct-2026).

Una alerta CRITICAL que de verdad importa también llega como NOTA DE VOZ, en
inglés (Ricardo practica), para que destaque entre los textos. Datos que
dictaron los filtros (30 días al 7-oct): 80 CRITICAL, todas
DEPLOYMENT_DEGRADED — bridge reiniciando 32, minio 28, backend en deploys 14 —
repartidas las 24 h. Sin filtros serían ~2 audios al día, varios de madrugada.

Filtros, todos en código:
  1. severidad CRITICAL
  2. sigue activa ≥ SRE_VOICE_MIN_PERSIST_S (600) — se sigue ANTES del dedup
     del loop, en cada ciclo de 60 s; si deja de verse 3 min, el episodio acaba
  3. horario silencioso SRE_VOICE_QUIET (23-7 MX): solo texto
  4. no es el bridge (si está caído no puede entregar la nota)
  5. una nota por episodio y SRE_VOICE_MAX_PER_DAY (3) al día
  6. el deployment no se desplegó en los últimos 30 min
La frase se arma en código, sin LLM. La síntesis corre en un hilo: nunca
frena el loop. SRE_VOICE_ALERTS=false lo apaga.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("agents.sre.voice_alerts")

ENABLED         = os.environ.get("SRE_VOICE_ALERTS", "true").lower() == "true"
MIN_PERSIST_S   = int(os.environ.get("SRE_VOICE_MIN_PERSIST_S", "600"))
MAX_PER_DAY     = int(os.environ.get("SRE_VOICE_MAX_PER_DAY", "3"))
QUIET           = os.environ.get("SRE_VOICE_QUIET", "23-7")          # horas MX, "" = sin silencio
EXCLUDED        = {x.strip() for x in os.environ.get(
    "SRE_VOICE_EXCLUDE", "whatsapp-bridge,whatsapp-bridge-deployment").split(",") if x.strip()}
_TZ             = ZoneInfo("America/Mexico_City")
_SEEN_TTL_S     = 180      # sin verla 3 min (3 ciclos), el episodio terminó

_K_FIRST  = "sre:voice:first:{key}"
_K_SPOKEN = "sre:voice:spoken:{key}"
_K_COUNT  = "sre:voice:count:{day}"

_POD_SUFFIX = re.compile(r"(-deployment)?(-[a-z0-9]{8,10})?(-[a-z0-9]{5})?$")

_PHRASES = {
    "DEPLOYMENT_DEGRADED": "{name} has no available replicas",
    "NODE_PRESSURE":       "the node is under resource pressure",
    "NODE_DISK_HIGH":      "the node disk is almost full",
    "NODE_MEMORY_HIGH":    "the node is running out of memory",
    "PVC_CAPACITY_HIGH":   "the volume of {name} is almost full",
    "SLO_BUDGET_BURNING":  "the error budget of {name} is burning fast",
    "CRASH_LOOP":          "{name} is crash looping",
    "OOM_KILLED":          "{name} keeps running out of memory",
}


# ── Puras ─────────────────────────────────────────────────────────────────────

def short_name(name: str) -> str:
    """frontend-next-deployment-7974dddd85-khjg2 → frontend-next."""
    return _POD_SUFFIX.sub("", name or "") or (name or "a resource")


def in_quiet_hours(now: datetime, quiet: str = QUIET) -> bool:
    """`quiet` = "23-7" (cruza medianoche) o "1-5". Hora de México."""
    if not quiet:
        return False
    try:
        start, end = (int(x) for x in quiet.split("-"))
    except ValueError:
        return False
    h = now.astimezone(_TZ).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def sentence(issue_type: str, resource: str, minutes: int) -> str:
    """Frase en inglés, en código. Corta: se escucha en el teléfono."""
    phrase = _PHRASES.get(issue_type, issue_type.lower().replace("_", " ") + " on {name}")
    what = phrase.format(name=short_name(resource))
    return f"Critical alert from Raphael: {what}, for {minutes} minutes. Check WhatsApp for details."


def decide(*, severity: str, resource: str, owner: str, first_seen: float, now_ts: float,
           now: datetime, already_spoken: bool, spoken_today: int) -> tuple[bool, str]:
    """(hablar, motivo). Todo menos «¿se desplegó hace poco?», que pide la API
    de K8s y se consulta solo si lo demás ya pasó."""
    if severity != "CRITICAL":
        return False, "no es CRITICAL"
    if resource in EXCLUDED or owner in EXCLUDED or short_name(resource) in EXCLUDED:
        return False, "excluido (el bridge no puede avisar de sí mismo)"
    if now_ts - first_seen < MIN_PERSIST_S:
        return False, f"activa {int(now_ts - first_seen)}s < {MIN_PERSIST_S}s"
    if already_spoken:
        return False, "ya se avisó por voz en este episodio"
    if in_quiet_hours(now):
        return False, "horario silencioso"
    if spoken_today >= MAX_PER_DAY:
        return False, f"tope diario ({MAX_PER_DAY})"
    return True, "ok"


# ── Con estado (Redis) ────────────────────────────────────────────────────────

def track(anomalies, *, redis=None, speak=None, recently_deployed=None, now=None) -> list[str]:
    """Llamar en CADA ciclo con todas las anomalías detectadas (antes del dedup).
    Devuelve los incident_key que se mandaron a voz en este ciclo."""
    if not ENABLED:
        return []
    now = now or datetime.now(_TZ)
    now_ts = now.timestamp()
    if redis is None:
        from storage.redis.client import get_redis_client
        redis = get_redis_client()
    speak = speak or _speak_async
    recently_deployed = recently_deployed or _recently_deployed
    day = now.astimezone(_TZ).strftime("%Y-%m-%d")
    spoken: list[str] = []

    for a in anomalies:
        if a.severity != "CRITICAL":
            continue
        key = a.incident_key
        k_first = _K_FIRST.format(key=key)
        if redis.set(k_first, str(now_ts), nx=True, ex=_SEEN_TTL_S):
            first_seen = now_ts
        else:
            redis.expire(k_first, _SEEN_TTL_S)
            raw = redis.get(k_first)
            first_seen = float(raw.decode() if isinstance(raw, bytes) else raw) if raw else now_ts
        k_spoken = _K_SPOKEN.format(key=key)
        ok, why = decide(
            severity=a.severity, resource=a.resource_name, owner=a.owner_name or "",
            first_seen=first_seen, now_ts=now_ts, now=now,
            already_spoken=bool(redis.exists(k_spoken)),
            spoken_today=int(redis.get(_K_COUNT.format(day=day)) or 0),
        )
        if not ok:
            logger.debug(f"[voice_alerts] {key}: sin voz ({why})")
            continue
        target = a.owner_name or a.resource_name
        if recently_deployed(target, a.namespace):
            logger.info(f"[voice_alerts] {key}: sin voz (desplegado hace poco)")
            continue
        minutes = max(1, int((now_ts - first_seen) // 60))
        speak(sentence(a.issue_type, target, minutes))
        redis.set(k_spoken, "1", ex=6 * 3600)
        k_count = _K_COUNT.format(day=day)
        if redis.incr(k_count) == 1:
            redis.expire(k_count, 2 * 24 * 3600)
        spoken.append(key)
        logger.info(f"[voice_alerts] {key}: nota de voz enviada ({minutes} min activa)")
    return spoken


def _recently_deployed(name: str, namespace: str) -> bool:
    try:
        from agents.sre.healer import _was_recently_deployed
        return _was_recently_deployed(name, namespace)
    except Exception:
        return False


def _speak_async(text: str) -> None:
    threading.Thread(target=_speak, args=(text,), daemon=True, name="sre-voice").start()


def _speak(text: str) -> None:
    """cosyvoice /tts (voz por defecto = la de Ricardo) → OGG → bridge."""
    import requests

    from tools.cosyvoice.tool import CosyVoiceTool
    cosy = os.environ.get("COSYVOICE_SERVICE_URL", "http://cosyvoice-service:8010")
    bridge = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
    phone = os.environ.get("OWNER_PHONE") or os.environ.get("ADMIN_PHONE", "")
    try:
        r = requests.post(f"{cosy}/tts", json={"text": text, "language": "en"}, timeout=120)
        r.raise_for_status()
        ogg = CosyVoiceTool._wav_to_ogg_opus(r.json()["audio_base64"])
        s = requests.post(f"{bridge}/send-audio", json={
            "phoneNumber": phone, "base64": ogg, "mimetype": "audio/ogg; codecs=opus", "ptt": True,
        }, timeout=60)
        s.raise_for_status()
    except Exception as exc:
        logger.warning(f"[voice_alerts] no se pudo mandar la nota de voz: {exc}")
