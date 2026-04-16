"""
Bug Library — mapeo de anomalías SRE a fixes de YAML en Bitbucket.

Arquitectura de dos capas:
  1. APP_MANIFEST_MAP  — dónde vive el YAML de cada aplicación (repo + file_path).
                         Clave: nombre del deployment/pod tal como lo reporta K8s.
  2. BUG_LIBRARY       — qué parche aplicar según el issue_type (genérico, reutilizable).

get_fix(issue_type, resource_name) combina ambas:
  - Busca la plantilla de fix por issue_type
  - Busca repo+file_path por resource_name (o usa el default si no está en el mapa)
  - Retorna un BugFix con repo y file_path correctos para esa app

Agregar soporte para una nueva app:
    APP_MANIFEST_MAP["nombre-deployment"] = AppManifest(
        repo="nombre-repo-bitbucket",
        file_path="ruta/al/deployment.yaml",
    )

Agregar soporte para un nuevo tipo de anomalía:
    BUG_LIBRARY["NUEVO_TYPE"] = BugFixTemplate(...)
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class AppManifest:
    """Localización del YAML de una aplicación en Bitbucket."""
    repo:      str
    file_path: str


@dataclass
class BugFixTemplate:
    """Plantilla de fix genérica por issue_type — sin repo ni file_path."""
    issue_type:    str
    description:   str
    patch_fn:      Callable[[str], str]
    branch_prefix: str
    pr_title:      str
    pr_body_tpl:   str


@dataclass
class BugFix:
    """Fix completo = template + localización resuelta para una app específica."""
    issue_type:    str
    description:   str
    repo:          str
    file_path:     str
    patch_fn:      Callable[[str], str]
    branch_prefix: str
    pr_title:      str
    pr_body_tpl:   str


# ── Mapa de aplicaciones → repo + file_path ──────────────────────────────────
# Clave: nombre del deployment tal como aparece en K8s (metadata.name del pod/deploy)
# Agregar una entrada por cada app que Camael puede parchear.

APP_MANIFEST_MAP: dict[str, AppManifest] = {
    # Backend principal (amael-agentic-backend)
    "amael-agentic-deployment":  AppManifest("amael-agentic-backend", "k8s/agents/05-backend-deployment.yaml"),
    "amael-agentic-backend":     AppManifest("amael-agentic-backend", "k8s/agents/05-backend-deployment.yaml"),

    # k8s-agent (SRE/infra agent)
    "k8s-agent-deployment":      AppManifest("amael-agentic-backend", "k8s/19.-k8s-agent-deployment.yaml"),
    "k8s-agent":                 AppManifest("amael-agentic-backend", "k8s/19.-k8s-agent-deployment.yaml"),

    # productivity-service
    "productivity-deployment":   AppManifest("amael-agentic-backend", "k8s/15.-productivity-deployment.yaml"),
    "productivity-service":      AppManifest("amael-agentic-backend", "k8s/15.-productivity-deployment.yaml"),

    # frontend-next
    "frontend-next-deployment":  AppManifest("amael-agentic-backend", "k8s/27.-frontend-next-deployment.yaml"),
    "frontend-next":             AppManifest("amael-agentic-backend", "k8s/27.-frontend-next-deployment.yaml"),

    # whatsapp-bridge
    "whatsapp-deployment":       AppManifest("amael-agentic-backend", "k8s/08.-whatsapp-deployment.yaml"),
    "whatsapp-bridge":           AppManifest("amael-agentic-backend", "k8s/08.-whatsapp-deployment.yaml"),

    # llm-adapter
    "llm-adapter-deployment":    AppManifest("amael-agentic-backend", "k8s/17.-k8s-llm-adapter.yaml"),
    "llm-adapter":               AppManifest("amael-agentic-backend", "k8s/17.-k8s-llm-adapter.yaml"),

    # podinfo (demo app)
    "podinfo":                   AppManifest("amael-agentic-backend", "k8s/podinfo-deployment.yaml"),

    # amael-demo-oom (POC demo Escenario A — OOM stress test)
    "amael-demo-oom":            AppManifest("amael-agentic-backend", "k8s/agents/06-demo-oom.yaml"),

    # amael-demo-crashloop (POC demo Escenario B — CrashLoopBackOff)
    "amael-demo-crashloop":      AppManifest("amael-agentic-backend", "k8s/agents/07-demo-crashloop.yaml"),

    # amael-demo-highmem (POC demo Escenario C — HIGH_MEMORY preventivo)
    "amael-demo-highmem":        AppManifest("amael-agentic-backend", "k8s/agents/08-demo-high-memory.yaml"),

    # amael-demo-degraded (POC demo Escenario D — DEPLOYMENT_DEGRADED)
    "amael-demo-degraded":       AppManifest("amael-agentic-backend", "k8s/agents/09-demo-degraded.yaml"),
}

# App por defecto si el resource_name no está en el mapa
_DEFAULT_APP = AppManifest("amael-agentic-backend", "k8s/agents/05-backend-deployment.yaml")


def is_known_resource(resource_name: str) -> bool:
    """
    Retorna True si resource_name está en APP_MANIFEST_MAP (exacto o por prefijo).
    Usado por Camael para decidir si necesita discovery dinámico en Bitbucket.
    """
    if not resource_name:
        return False
    if resource_name in APP_MANIFEST_MAP:
        return True
    # Fuzzy: pod name con sufijo hash (e.g. "k8s-agent-7d9fab12-xk2vp")
    return any(resource_name.startswith(k) for k in APP_MANIFEST_MAP)


# ── Funciones de parchado YAML ────────────────────────────────────────────────

def _parse_memory_mi(value: str) -> int:
    """Convierte '256Mi', '1Gi', '512Mi' a MiB como int."""
    value = value.strip().strip('"').strip("'")
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024
    if value.endswith("Mi"):
        return int(value[:-2])
    if value.endswith("G"):
        return int(value[:-1]) * 1024
    if value.endswith("M"):
        return int(value[:-1])
    return int(value)


def _format_memory(mi: int) -> str:
    """Formatea MiB a string legible: '1536Mi' → '1536Mi', >=1024 → 'XGi'."""
    if mi % 1024 == 0:
        return f"{mi // 1024}Gi"
    return f"{mi}Mi"


def _patch_memory_limit(yaml_text: str, multiplier: float = 3.0) -> str:
    """
    Aumenta resources.limits.memory en el YAML por el multiplicador dado.
    Opera en texto plano para preservar comentarios y formato.
    Busca el primer 'memory:' dentro de la sección 'limits:'.
    """
    in_limits = False
    lines_out = []

    for line in yaml_text.splitlines():
        stripped = line.strip()

        if stripped == "limits:":
            in_limits = True
            lines_out.append(line)
            continue

        if in_limits and re.match(r"memory:\s*", stripped):
            # Extraer el valor actual
            m = re.search(r'memory:\s*["\']?(\d+(?:\.\d+)?(?:Mi|Gi|G|M))["\']?', line)
            if m:
                current_mi = _parse_memory_mi(m.group(1))
                new_mi = int(current_mi * multiplier)
                new_val = _format_memory(new_mi)
                # Preservar indentación y comillas si las había
                if '"' in line:
                    line = re.sub(
                        r'(memory:\s+")[^"]+(")',
                        lambda x: f'{x.group(1)}{new_val}{x.group(2)}',
                        line,
                    )
                else:
                    line = re.sub(
                        r'(memory:\s+)\S+',
                        lambda x: f'{x.group(1)}{new_val}',
                        line,
                    )
            in_limits = False  # solo el primer memory en limits
            lines_out.append(line)
            continue

        # Si encontramos otra sección de nivel similar, salimos de limits
        if in_limits and stripped and not stripped.startswith("#") and ":" in stripped:
            key = stripped.split(":")[0]
            if not key.startswith(" ") and key != "memory" and key != "cpu":
                in_limits = False

        lines_out.append(line)

    return "\n".join(lines_out)


def _patch_cpu_limit(yaml_text: str, multiplier: float = 2.0) -> str:
    """Aumenta resources.limits.cpu en el YAML."""
    in_limits = False
    lines_out = []

    for line in yaml_text.splitlines():
        stripped = line.strip()

        if stripped == "limits:":
            in_limits = True
            lines_out.append(line)
            continue

        if in_limits and re.match(r"cpu:\s*", stripped):
            m = re.search(r'cpu:\s*["\']?(\d+)(m?)["\']?', line)
            if m:
                val = int(m.group(1))
                unit = m.group(2)  # "m" o ""
                new_val = int(val * multiplier)
                new_str = f"{new_val}{unit}"
                if '"' in line:
                    line = re.sub(
                        r'(cpu:\s+")[^"]+(")',
                        lambda x, nv=new_str: f'{x.group(1)}{nv}{x.group(2)}',
                        line,
                    )
                else:
                    line = re.sub(
                        r'(cpu:\s+)\S+',
                        lambda x, nv=new_str: f'{x.group(1)}{nv}',
                        line,
                    )
            in_limits = False
            lines_out.append(line)
            continue

        lines_out.append(line)

    return "\n".join(lines_out)


def _patch_liveness_delay(yaml_text: str, extra_seconds: int = 30) -> str:
    """Aumenta livenessProbe.initialDelaySeconds para reducir reinicios por probe."""
    lines_out = []
    in_liveness = False

    for line in yaml_text.splitlines():
        stripped = line.strip()

        if stripped == "livenessProbe:":
            in_liveness = True
            lines_out.append(line)
            continue

        if in_liveness and stripped.startswith("initialDelaySeconds:"):
            m = re.search(r"initialDelaySeconds:\s*(\d+)", line)
            if m:
                current = int(m.group(1))
                new_val = current + extra_seconds
                line = re.sub(
                    r"(initialDelaySeconds:\s*)\d+",
                    lambda x, nv=new_val: f"{x.group(1)}{nv}",
                    line,
                )
            in_liveness = False
            lines_out.append(line)
            continue

        # Si encontramos otra probe section, salimos
        if in_liveness and stripped in {"readinessProbe:", "startupProbe:", "resources:"}:
            in_liveness = False

        lines_out.append(line)

    return "\n".join(lines_out)


def _patch_memory_and_liveness(yaml_text: str) -> str:
    """Fix para HIGH_RESTARTS: aumenta memoria + ajusta probe delay."""
    yaml_text = _patch_memory_limit(yaml_text, multiplier=1.5)
    yaml_text = _patch_liveness_delay(yaml_text, extra_seconds=30)
    return yaml_text


# ── Bug Library (plantillas genéricas por issue_type) ─────────────────────────

BUG_LIBRARY: dict[str, BugFixTemplate] = {
    "OOM_KILLED": BugFixTemplate(
        issue_type="OOM_KILLED",
        description=(
            "El contenedor fue terminado por el kernel (OOM Killer) porque "
            "superó el límite de memoria definido en el deployment YAML. "
            "Fix: incrementar resources.limits.memory ×3."
        ),
        patch_fn=lambda t: _patch_memory_limit(t, multiplier=3.0),
        branch_prefix="fix/oom-memory-limit",
        pr_title="fix: increase memory limit to prevent OOM kills",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael (SRE Agent) detectó un evento **OOM_KILLED** en "
            "`{namespace}/{resource}` — el pod fue terminado por el kernel "
            "porque superó el límite de memoria configurado.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.memory` incrementado ×3 para dar margen "
            "suficiente con el tráfico actual.\n\n"
            "## Verificación\n\n"
            "- [ ] Pod no vuelve a ser OOM-killed en las próximas 24h\n"
            "- [ ] Uso de memoria se mantiene por debajo del 80% del nuevo límite\n\n"
            "🤖 *Generado automáticamente por Camael (DevOps Agent) — "
            "Incidente {incident_key}*"
        ),
    ),

    "HIGH_MEMORY": BugFixTemplate(
        issue_type="HIGH_MEMORY",
        description=(
            "Uso de memoria supera el umbral de alerta (85%). "
            "Fix preventivo: incrementar resources.limits.memory ×2."
        ),
        patch_fn=lambda t: _patch_memory_limit(t, multiplier=2.0),
        branch_prefix="fix/high-memory-limit",
        pr_title="fix: increase memory limit — HIGH_MEMORY alert",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael detectó uso elevado de memoria en `{namespace}/{resource}` "
            "(>85% del límite). Fix preventivo antes de un posible OOM.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.memory` incrementado ×2.\n\n"
            "🤖 *Generado por Camael — Incidente {incident_key}*"
        ),
    ),

    "HIGH_CPU": BugFixTemplate(
        issue_type="HIGH_CPU",
        description=(
            "Uso de CPU supera el 90% del límite configurado. "
            "Fix: incrementar resources.limits.cpu ×2."
        ),
        patch_fn=lambda t: _patch_cpu_limit(t, multiplier=2.0),
        branch_prefix="fix/high-cpu-limit",
        pr_title="fix: increase CPU limit — HIGH_CPU alert",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael detectó CPU throttling en `{namespace}/{resource}` (>90%).\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.cpu` incrementado ×2.\n\n"
            "🤖 *Generado por Camael — Incidente {incident_key}*"
        ),
    ),

    "HIGH_RESTARTS": BugFixTemplate(
        issue_type="HIGH_RESTARTS",
        description=(
            "El pod acumula reinicios excesivos. Probable causa: liveness probe "
            "muy agresiva o memoria insuficiente. "
            "Fix: aumentar initialDelaySeconds +30s y memoria ×1.5."
        ),
        patch_fn=_patch_memory_and_liveness,
        branch_prefix="fix/high-restarts-probe-memory",
        pr_title="fix: adjust probe delay and memory — HIGH_RESTARTS",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael detectó reinicios excesivos en `{namespace}/{resource}`.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambios\n\n"
            "- `livenessProbe.initialDelaySeconds` +30s para dar más tiempo al startup.\n"
            "- `resources.limits.memory` ×1.5 para prevenir OOM en el proceso de inicio.\n\n"
            "🤖 *Generado por Camael — Incidente {incident_key}*"
        ),
    ),

    "DEPLOYMENT_DEGRADED": BugFixTemplate(
        issue_type="DEPLOYMENT_DEGRADED",
        description=(
            "El deployment no tiene réplicas disponibles. Causa más probable: "
            "pods terminados por OOM Kill. Fix: incrementar resources.limits.memory ×3."
        ),
        patch_fn=lambda t: _patch_memory_limit(t, multiplier=3.0),
        branch_prefix="fix/degraded-memory-limit",
        pr_title="fix: increase memory limit to restore deployment availability",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael (SRE Agent) detectó **DEPLOYMENT_DEGRADED** en "
            "`{namespace}/{resource}` — sin réplicas disponibles. "
            "Causa raíz: pods terminados repetidamente por OOM Kill.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.memory` incrementado ×3 para resolver el OOM subyacente.\n\n"
            "## Verificación\n\n"
            "- [ ] Deployment vuelve a tener réplicas disponibles\n"
            "- [ ] No se repiten los OOM kills en 24h\n\n"
            "🤖 *Generado automáticamente por Camael (DevOps Agent) — "
            "Incidente {incident_key}*"
        ),
    ),

    "CRASH_LOOP": BugFixTemplate(
        issue_type="CRASH_LOOP",
        description=(
            "El pod está en CrashLoopBackOff. Causa más común: límite de memoria "
            "insuficiente → OOM Kill en cada inicio. "
            "Fix: incrementar resources.limits.memory ×3."
        ),
        patch_fn=lambda t: _patch_memory_limit(t, multiplier=3.0),
        branch_prefix="fix/crashloop-memory-limit",
        pr_title="fix: increase memory limit to resolve CrashLoopBackOff",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael (SRE Agent) detectó **CrashLoopBackOff** en "
            "`{namespace}/{resource}`. Causa raíz más probable: límite de memoria "
            "insuficiente → el kernel OOM-killa el contenedor en cada arranque.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.memory` incrementado ×3 para dar margen al proceso.\n\n"
            "## Verificación\n\n"
            "- [ ] Pod arranca correctamente sin crashear\n"
            "- [ ] No se repite el CrashLoopBackOff en 24h\n\n"
            "🤖 *Generado automáticamente por Camael (DevOps Agent) — "
            "Incidente {incident_key}*"
        ),
    ),

    "MEMORY_LEAK_PREDICTED": BugFixTemplate(
        issue_type="MEMORY_LEAK_PREDICTED",
        description=(
            "Trend de memoria con pendiente positiva sostenida. "
            "Fix preventivo: aumentar límite ×2 mientras se investiga el leak."
        ),
        patch_fn=lambda t: _patch_memory_limit(t, multiplier=2.0),
        branch_prefix="fix/memory-leak-limit",
        pr_title="fix: increase memory limit — predicted memory leak",
        pr_body_tpl=(
            "## Contexto\n\n"
            "Raphael detectó una tendencia de crecimiento lineal de memoria "
            "en `{namespace}/{resource}` que sugiere un posible memory leak.\n\n"
            "**Incidente**: {incident_key}\n"
            "**Detalles**: {details}\n\n"
            "## Cambio\n\n"
            "- `resources.limits.memory` incrementado ×2 como medida preventiva "
            "mientras se investiga la causa del leak.\n\n"
            "⚠️ *Este fix es temporal. Se recomienda revisar el código en busca "
            "de recursos no liberados.*\n\n"
            "🤖 *Generado por Camael — Incidente {incident_key}*"
        ),
    ),
}


def get_fix(issue_type: str, resource_name: str = "") -> BugFix | None:
    """
    Retorna un BugFix completo para (issue_type, resource_name), o None si no hay fix.

    Prioridad de resolución del manifest:
      1. ArgoCD discovery (fuente de verdad dinámica — lee CRDs argoproj.io)
         - Si el repo es GitHub → retorna None (Camael solo soporta Bitbucket; el
           caller debe escalar a NOTIFY_HUMAN)
         - Si el repo es Bitbucket → usa repo+path descubierto
      2. APP_MANIFEST_MAP estático (fallback para entradas conocidas sin ArgoCD)
      3. _DEFAULT_APP (último recurso — loggea advertencia)

    Args:
        issue_type:    Tipo de anomalía (e.g. "OOM_KILLED", "CRASH_LOOP").
        resource_name: Nombre del deployment/pod reportado por K8s.

    Returns:
        BugFix con repo+file_path resueltos, o None si no hay template o el repo
        es GitHub (no soportado por Camael).
    """
    import logging
    _log = logging.getLogger("agents.sre.bug_library")

    template = BUG_LIBRARY.get(issue_type)
    if not template:
        return None

    manifest: AppManifest | None = None

    # ── 1. APP_MANIFEST_MAP estático (fuente de verdad para recursos conocidos) ──
    # Tiene prioridad sobre ArgoCD porque contiene file_path exacto.
    # ArgoCD solo conoce el directorio (e.g. "k8s/agents"), no el archivo específico.
    manifest = APP_MANIFEST_MAP.get(resource_name)
    if manifest is None and resource_name:
        for key in sorted(APP_MANIFEST_MAP, key=len, reverse=True):
            if resource_name.startswith(key):
                manifest = APP_MANIFEST_MAP[key]
                break
    if manifest is not None:
        _log.debug(
            f"[bug_library] APP_MANIFEST_MAP: '{resource_name}' → "
            f"{manifest.repo}/{manifest.file_path}"
        )

    # ── 2. ArgoCD discovery (solo para recursos NO conocidos en APP_MANIFEST_MAP) ─
    # ArgoCD retorna el directorio del repo, no el archivo específico.
    # Solo se usa como fallback cuando el recurso no está en APP_MANIFEST_MAP.
    if manifest is None:
        try:
            from agents.sre.argocd_discovery import discover_manifest
            discovered = discover_manifest(resource_name)
            if discovered is not None:
                if not discovered.is_bitbucket:
                    # GitHub u otro SCM no soportado — Camael no puede crear el PR
                    _log.info(
                        f"[bug_library] '{resource_name}' está en repo GitHub "
                        f"({discovered.repo_url}) — Camael no soporta GitHub. "
                        f"Retornando None para escalar a NOTIFY_HUMAN."
                    )
                    return None
                manifest = AppManifest(
                    repo=discovered.bb_repo_name,
                    file_path=discovered.path,  # directorio; Camael hará discovery del archivo
                )
                _log.debug(
                    f"[bug_library] ArgoCD: '{resource_name}' → "
                    f"bb={discovered.bb_repo_name}, path={discovered.path}"
                )
        except Exception as exc:
            _log.debug(f"[bug_library] ArgoCD discovery falló (no crítico): {exc}")

    # ── 3. Fallback default ──────────────────────────────────────────────────
    if manifest is None:
        _log.warning(
            f"[bug_library] '{resource_name}' no encontrado en ArgoCD ni en "
            f"APP_MANIFEST_MAP — usando default ({_DEFAULT_APP.repo}/{_DEFAULT_APP.file_path})."
        )
        manifest = _DEFAULT_APP

    return BugFix(
        issue_type=template.issue_type,
        description=template.description,
        repo=manifest.repo,
        file_path=manifest.file_path,
        patch_fn=template.patch_fn,
        branch_prefix=template.branch_prefix,
        pr_title=template.pr_title,
        pr_body_tpl=template.pr_body_tpl,
    )
