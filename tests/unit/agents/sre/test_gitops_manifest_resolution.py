"""
Tests de resolución de manifest para el handoff GitOps (P2).

Raphael veía 'trader-service no encontrado en ArgoCD ni en APP_MANIFEST_MAP —
usando default (05-backend-deployment.yaml)'. Si el handoff llegaba a ejecutarse,
Camael habría abierto un PR modificando el manifest del backend por una anomalía
del trader.
"""
from __future__ import annotations

from agents.sre.bug_library import APP_MANIFEST_MAP, get_fix, is_known_resource

# ── Servicios standalone registrados ─────────────────────────────────────────

def test_agents_split_services_are_mapped():
    """raphael/camael/trader corren como servicios propios desde Phase 3."""
    for name, expected in [
        ("raphael-service", "k8s/agents/13-raphael-deployment.yaml"),
        ("camael-service",  "k8s/agents/15-camael-deployment.yaml"),
        ("trader-service",  "k8s/agents/16-trader-deployment.yaml"),
    ]:
        assert name in APP_MANIFEST_MAP, f"{name} sin mapear"
        assert APP_MANIFEST_MAP[name].file_path == expected


def test_trader_resolves_to_own_manifest_not_backend():
    """El caso concreto que motivó el fix."""
    fix = get_fix("DEPLOYMENT_DEGRADED", "trader-service")
    assert fix is not None
    assert fix.file_path == "k8s/agents/16-trader-deployment.yaml"
    assert "05-backend-deployment" not in fix.file_path


def test_trader_pod_name_with_hash_resolves_too():
    """El observer suele reportar el pod, no el deployment."""
    fix = get_fix("DEPLOYMENT_DEGRADED", "trader-service-84c5fbf49-hnltc")
    assert fix is not None
    assert fix.file_path == "k8s/agents/16-trader-deployment.yaml"


def test_known_resource_recognizes_new_services():
    assert is_known_resource("trader-service") is True
    assert is_known_resource("raphael-service") is True


# ── allow_default ────────────────────────────────────────────────────────────

def test_unknown_resource_returns_none_without_default():
    """Sin manifest resuelto, mejor no actuar que escribir en otro archivo."""
    assert get_fix("OOM_KILLED", "servicio-inexistente-xyz", allow_default=False) is None


def test_unknown_resource_still_falls_back_when_allowed():
    """Camael conserva el default: hace discovery en Bitbucket y corrige el path."""
    fix = get_fix("OOM_KILLED", "servicio-inexistente-xyz", allow_default=True)
    assert fix is not None
    assert fix.file_path == "k8s/agents/05-backend-deployment.yaml"


def test_default_is_allowed_by_default():
    """No romper llamadas existentes que no pasan el flag."""
    assert get_fix("OOM_KILLED", "servicio-inexistente-xyz") is not None


def test_known_resource_unaffected_by_flag():
    a = get_fix("OOM_KILLED", "trader-service", allow_default=False)
    b = get_fix("OOM_KILLED", "trader-service", allow_default=True)
    assert a is not None and b is not None
    assert a.file_path == b.file_path


def test_no_template_returns_none_regardless():
    assert get_fix("TIPO_QUE_NO_EXISTE", "trader-service", allow_default=True) is None
