"""
ArgoCD Discovery — encuentra el repo+path que gestiona cada Deployment via CRDs.

En vez de mantener APP_MANIFEST_MAP a mano, consultamos las Applications de ArgoCD
que ya saben exactamente qué repo/path/branch corresponde a cada recurso K8s.

Flujo:
  1. `discover_manifest(deployment_name)` es llamado por get_fix() en bug_library.
  2. Se leen los CRDs argoproj.io/v1alpha1/Application del namespace argocd.
  3. Se busca qué Application tiene ese deployment en status.resources[].
  4. Se retorna DiscoveredManifest con repo_url, path, branch e indicador is_bitbucket.

Cache:
  El resultado se cachea en memoria por _CACHE_TTL_S segundos para no llamar
  a la K8s API en cada iteración del SRE loop (60s).

Soporte de repos:
  - Bitbucket: Camael puede crear PR directamente.
  - GitHub:    Camael no soporta GitHub → el caller debe usar NOTIFY_HUMAN.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("agents.sre.argocd_discovery")

_ARGOCD_NAMESPACE = "argocd"
_CACHE_TTL_S = 300  # refrescar cada 5 minutos

# Cache: deployment_name → {"repo_url": str, "path": str, "branch": str}
_cache: dict[str, dict] = {}
_cache_loaded_at: float = 0.0


@dataclass
class DiscoveredManifest:
    """Localización de un manifest descubierta desde ArgoCD."""
    repo_url:    str   # URL completa: https://bitbucket.org/... o https://github.com/...
    path:        str   # Directorio en el repo: k8s/agents/
    branch:      str   # Rama objetivo: main
    is_bitbucket: bool
    bb_repo_name: str = field(default="")  # Solo cuando is_bitbucket=True


def _is_bitbucket(url: str) -> bool:
    return "bitbucket.org" in url.lower()


def _extract_bb_repo_name(url: str) -> str:
    """https://bitbucket.org/workspace/repo → 'repo'"""
    parts = url.rstrip("/").split("/")
    return parts[-1] if len(parts) >= 2 else ""


def _load_cache() -> None:
    """Lee todos los ArgoCD Application CRDs y construye el mapa deployment→source."""
    global _cache, _cache_loaded_at
    try:
        from kubernetes import client, config
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()

        custom = client.CustomObjectsApi()
        apps = custom.list_namespaced_custom_object(
            group="argoproj.io",
            version="v1alpha1",
            namespace=_ARGOCD_NAMESPACE,
            plural="applications",
        )

        new_cache: dict[str, dict] = {}
        items = apps.get("items", [])
        for app in items:
            src = app.get("spec", {}).get("source", {})
            repo_url = src.get("repoURL", "")
            path = src.get("path", "").rstrip("/")
            branch = src.get("targetRevision", "main")

            resources = app.get("status", {}).get("resources", [])
            for r in resources:
                if r.get("kind") == "Deployment":
                    dep_name = r.get("name", "")
                    if dep_name:
                        new_cache[dep_name] = {
                            "repo_url": repo_url,
                            "path": path,
                            "branch": branch,
                        }

        _cache = new_cache
        _cache_loaded_at = time.monotonic()
        logger.info(
            f"[argocd_discovery] Cache cargado: {len(_cache)} deployments "
            f"desde {len(items)} Applications"
        )

    except Exception as exc:
        # Si ArgoCD no está disponible, no bloquear el SRE loop
        _cache_loaded_at = time.monotonic()  # Evitar retry inmediato
        logger.warning(f"[argocd_discovery] No se pudo cargar cache de ArgoCD: {exc}")


def _ensure_cache() -> None:
    """Carga o refresca el cache si ha expirado."""
    if time.monotonic() - _cache_loaded_at > _CACHE_TTL_S:
        _load_cache()


def discover_manifest(deployment_name: str) -> DiscoveredManifest | None:
    """
    Retorna la fuente ArgoCD (repo+path+branch) para un deployment.

    Args:
        deployment_name: Nombre del Deployment K8s (puede tener sufijo hash de pod).

    Returns:
        DiscoveredManifest si se encontró en ArgoCD, None si no.
    """
    _ensure_cache()

    # Búsqueda exacta primero
    entry = _cache.get(deployment_name)

    # Búsqueda fuzzy: pod con sufijo hash "whatsapp-personal-deployment-649b6f9786-95bzm"
    if entry is None and deployment_name:
        for key in sorted(_cache, key=len, reverse=True):
            if deployment_name == key or deployment_name.startswith(key + "-"):
                entry = _cache[key]
                logger.debug(
                    f"[argocd_discovery] Fuzzy match: '{deployment_name}' → '{key}'"
                )
                break

    if entry is None:
        logger.debug(
            f"[argocd_discovery] '{deployment_name}' no encontrado en ninguna ArgoCD Application"
        )
        return None

    repo_url = entry["repo_url"]
    bb = _is_bitbucket(repo_url)

    return DiscoveredManifest(
        repo_url=repo_url,
        path=entry["path"],
        branch=entry["branch"],
        is_bitbucket=bb,
        bb_repo_name=_extract_bb_repo_name(repo_url) if bb else "",
    )


def refresh_cache() -> None:
    """Fuerza refresco del cache (útil después de desplegar nuevas ArgoCD Applications)."""
    global _cache_loaded_at
    _cache_loaded_at = 0.0
    _load_cache()
