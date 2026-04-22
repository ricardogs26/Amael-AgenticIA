"""
clients.raphael_client — Abstracción de agents/sre/ detrás de feature flag.

Expone la misma superficie de funciones que consumen hoy los routers
(`interfaces/api/routers/sre.py`). En modo `inprocess` delega a `agents.sre.*`.
En modo `remote` hace HTTP a raphael-service según OpenAPI spec.

API pública (coincide 1:1 con `agents.sre.__init__.__all__`):
    get_loop_state()                      → dict
    get_recent_incidents(limit)           → list[dict]
    get_recent_postmortems(limit)         → list[dict]
    get_historical_success_rate(days)     → list[dict]
    get_slo_burn_rates()                  → list[dict]
    activate_maintenance(minutes)         → None
    deactivate_maintenance()              → None
    load_slo_targets()                    → list[dict]

Fase 1: skeleton — AGENTS_MODE=inprocess por default ⇒ comportamiento sin cambios.
Fase 2: los routers migran sus imports a `clients.raphael_client` y se flipa el flag.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

from config.settings import settings

logger = logging.getLogger("clients.raphael")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_inprocess() -> bool:
    return settings.agents_mode == "inprocess"


def _get(path: str, **params: Any) -> Any:
    from clients._http import get_raphael_client
    client = get_raphael_client()
    resp = client.get(path, params={k: v for k, v in params.items() if v is not None} or None)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, json: dict | None = None) -> Any:
    from clients._http import get_raphael_client
    client = get_raphael_client()
    resp = client.post(path, json=json)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> Any:
    from clients._http import get_raphael_client
    client = get_raphael_client()
    resp = client.delete(path)
    resp.raise_for_status()
    return resp.json()


def _to_dict(obj: Any) -> Any:
    """Convierte dataclass → dict si aplica; devuelve el objeto intacto si ya es dict."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


# ── API pública ────────────────────────────────────────────────────────────────

def get_loop_state() -> dict[str, Any]:
    """Estado del loop (CB, maintenance, last run, leader election)."""
    if _is_inprocess():
        from agents.sre import get_loop_state as _local
        return _to_dict(_local())
    return _get("/api/sre/loop/status")


def get_recent_incidents(limit: int = 5) -> list[dict[str, Any]]:
    """Últimos N incidentes desde PostgreSQL."""
    if _is_inprocess():
        from agents.sre import get_recent_incidents as _local
        return _local(limit=limit)
    return _get("/api/sre/incidents", limit=limit)


def get_recent_postmortems(limit: int = 3) -> list[dict[str, Any]]:
    """Últimos N postmortems LLM."""
    if _is_inprocess():
        from agents.sre import get_recent_postmortems as _local
        return _local(limit=limit)
    return _get("/api/sre/postmortems", limit=limit)


def get_historical_success_rate(days: int = 7) -> list[dict[str, Any]]:
    """Tasa de éxito por (issue_type, action) en ventana N días."""
    if _is_inprocess():
        from agents.sre import get_historical_success_rate as _local
        return _local(days=days)
    # El endpoint devuelve {window_days, rows[]}; normalizamos a rows.
    data = _get("/api/sre/learning/stats", window_days=days)
    return data.get("rows", []) if isinstance(data, dict) else data


def get_slo_burn_rates() -> list[dict[str, Any]]:
    """SLO targets con burn rates actuales desde Prometheus."""
    if _is_inprocess():
        from agents.sre.scheduler import get_slo_burn_rates as _local
        return _local()
    return _get("/api/sre/slo/status")


def activate_maintenance(minutes: int = 60, reason: str | None = None) -> None:
    """Activa ventana de mantenimiento por N minutos (pausa el loop)."""
    if _is_inprocess():
        from agents.sre import activate_maintenance as _local
        return _local(minutes=minutes)
    _post("/api/sre/maintenance", json={"duration_minutes": minutes, "reason": reason})


def deactivate_maintenance() -> None:
    """Desactiva la ventana de mantenimiento y reanuda el loop."""
    if _is_inprocess():
        from agents.sre import deactivate_maintenance as _local
        return _local()
    _delete("/api/sre/maintenance")


def load_slo_targets() -> list[dict[str, Any]]:
    """Lista de SLO targets configurados (ConfigMap `sre-agent-slo`)."""
    if _is_inprocess():
        from agents.sre import load_slo_targets as _local
        return _local()
    # En modo remote usamos el mismo endpoint de slo/status (es superset)
    return _get("/api/sre/slo/status")
