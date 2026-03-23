# Runbook: Dashboards de Grafana sin datos (amael-ia)

## Síntoma

Uno o más dashboards de Grafana muestran paneles vacíos o "No data" después de un
despliegue o reinicio de `backend-ia`. Los dashboards afectados típicamente son:

| # | Dashboard | Métricas afectadas |
|---|---|---|
| 1 | Amael - LLM & HTTP | `amael_planner_latency_seconds_bucket`, `amael_supervisor_latency_seconds_bucket`, `amael_executor_estimated_prompt_tokens_bucket`, `amael_tool_calls_total` |
| 2 | Amael - Pipeline de Agente | `amael_agent_execution_latency_bucket`, `amael_executor_step_latency_seconds_bucket`, `amael_executor_steps_total`, `amael_planner_step_types_total` |
| 5 | Amael - Supervisor & Calidad | `amael_supervisor_decisions_total`, `amael_supervisor_quality_score_bucket` |
| 6 | Amael - Seguridad & Rate Limiting | `amael_security_rate_limited_total`, `amael_security_input_blocked_total` |
| 7 | Amael - Service Map & Herramientas | `amael_tool_calls_total`, `amael_executor_step_latency_seconds_bucket` |

**Los dashboards 3 (RAG), 4 (Infra) y 8 (SRE) no se ven afectados** porque usan métricas de
fondo (GPU, CPU, RAM, loop SRE) que no dependen del tráfico de usuario.

---

## Causa raíz

Todas las métricas afectadas (`amael_planner_*`, `amael_executor_*`, `amael_supervisor_*`,
`amael_security_*`, `amael_tool_calls_total`) son **Counters e Histogramas de Python
prometheus_client** definidos en `backend-ia/agents/metrics.py` y `backend-ia/main.py`.

**Comportamiento clave:**
- Al reiniciar el pod de `backend-ia`, el registro Prometheus en memoria se resetea a cero.
- Estos contadores solo se incrementan durante conversaciones activas (`POST /api/chat`).
- Las queries de Grafana usan ventanas `rate([5m])` o `increase([1h])` — si no hubo
  tráfico reciente, retornan 0 o "No data".
- Esto ocurre frecuentemente tras despliegues, ya que cualquier cambio en `k8s/` puede
  causar un rollout de `backend-ia`.

**Causa secundaria posible:** el Prometheus ServiceMonitor o la anotación de scraping
se perdió, o el pod de backend-ia no está `Running`.

---

## Diagnóstico paso a paso

### Paso 1 — Verificar que backend-ia esté corriendo

```promql
# Herramienta: Listar_Pods / namespace: amael-ia
# Buscar backend-ia-* con status Running y 0 reinicios recientes
```

Usar `Listar_Pods` con namespace `amael-ia`. Si el pod está en CrashLoopBackOff o
reiniciándose, resolver primero ese problema (ver runbooks crash_loop.md / oom_killed.md).

### Paso 2 — Verificar que Prometheus esté scrapeando backend-ia

```promql
up{namespace="amael-ia", pod=~"backend-ia.*"}
```

- Resultado `1` → Prometheus alcanza el pod correctamente.
- Resultado `0` o sin datos → problema de scraping (ver Paso 5).

### Paso 3 — Verificar si las métricas existen en Prometheus

```promql
# Verificar métricas de planner
amael_planner_latency_seconds_count

# Verificar métricas de executor
amael_executor_steps_total

# Verificar métricas de supervisor
amael_supervisor_decisions_total

# Verificar métricas de seguridad
amael_security_rate_limited_total

# Verificar herramientas
amael_tool_calls_total
```

- **Si retornan valores > 0** → Las métricas existen. El dashboard solo necesita tráfico
  reciente para mostrar datos en las ventanas `rate([5m])`.
- **Si retornan 0 o vacío** → El pod fue reiniciado recientemente y no ha habido
  conversaciones. Continuar al Paso 4.

### Paso 4 — Generar tráfico de prueba para poblar las métricas

La solución más rápida es enviar al menos una conversación real al backend. Esto activa
todos los agentes (Planner → Executor → Supervisor) y genera observaciones en todos los
Counters e Histogramas afectados.

```bash
# Desde dentro del clúster o vía ingress:
curl -X POST https://amael-ia.richardx.dev/api/chat \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"message": "estado del cluster", "conversation_id": "sre-test"}'
```

Después de ~15 segundos (intervalo de scrape de Prometheus), los dashboards deben
mostrar datos.

### Paso 5 — Si Prometheus no scrapeea el pod (up=0)

Verificar que el pod tenga la anotación correcta:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "8000"
  prometheus.io/path: "/metrics"
```

Si falta la anotación, está en `k8s/04.-backend-deployment.yaml`. Reaplicar el manifiesto:

```bash
kubectl apply -f k8s/04.-backend-deployment.yaml -n amael-ia
```

### Paso 6 — Si el dashboard sigue sin datos tras tráfico

Verificar la integridad del ConfigMap de dashboards:

```bash
kubectl get configmap amael-custom-dashboards -n observability
kubectl describe configmap amael-custom-dashboards -n observability | grep "Data\|Name"
```

Si el ConfigMap está corrupto o falta, regenerarlo:

```bash
cd GitOps-Infra/observability
python3 - << 'EOF'
import json, os
files = {
    "llm-latency-dashboard.json":     "llm-dashboard.json",
    "agent-pipeline-dashboard.json":  "agent-dashboard.json",
    "rag-dashboard.json":             "rag-dashboard.json",
    "infra-dashboard.json":           "infra-dashboard.json",
    "supervisor-dashboard.json":      "supervisor-dashboard.json",
    "security-dashboard.json":        "security-dashboard.json",
    "service-map-dashboard.json":     "service-map-dashboard.json",
    "sre-agent-dashboard.json":       "sre-agent-dashboard.json",
}
lines = ["apiVersion: v1","kind: ConfigMap","metadata:","  name: amael-custom-dashboards",
         "  namespace: observability","  labels:",'    grafana_dashboard: "1"',"data:"]
for src, key in files.items():
    lines.append(f"  {key}: |")
    for line in open(f"dashboards/{src}").read().rstrip().splitlines():
        lines.append(f"    {line}")
open("k8s/04-custom-dashboards-cm.yaml","w").write("\n".join(lines)+"\n")
EOF
kubectl apply -f k8s/04-custom-dashboards-cm.yaml
```

El sidecar de Grafana recarga automáticamente en ~30 segundos (no se necesita
reiniciar Grafana).

### Paso 7 — Si el sidecar de Grafana no recargó los dashboards

```bash
# Verificar que el sidecar esté corriendo
kubectl get pods -n observability -l app.kubernetes.io/name=grafana

# Ver logs del sidecar
kubectl logs -n observability deployment/kube-prometheus-stack-grafana \
  -c grafana-sc-dashboard --tail=50
```

Si el sidecar muestra errores, reiniciar el pod de Grafana:
```bash
kubectl rollout restart deployment/kube-prometheus-stack-grafana -n observability
```

---

## Árbol de decisión rápido

```
¿backend-ia está Running?
├── NO → Resolver CrashLoop/OOM primero (ver crash_loop.md / oom_killed.md)
└── SÍ
    ↓
    ¿up{pod=~"backend-ia.*"} = 1?
    ├── NO → Verificar anotaciones prometheus.io/scrape en el deployment
    └── SÍ
        ↓
        ¿amael_planner_latency_seconds_count > 0?
        ├── SÍ → Solo falta tráfico reciente. Enviar un mensaje al chat.
        └── NO → Pod reiniciado recientemente + sin tráfico.
                  → Enviar un mensaje al chat para generar métricas.
                  → Esperar 15s y refrescar Grafana.
```

---

## Prevención

- Los dashboards 1, 2, 5, 6, 7 son **activity-driven**: solo muestran datos cuando hay
  conversaciones activas. Es comportamiento normal tras un reinicio sin tráfico.
- Si se desea monitoreo continuo sin tráfico, considerar un **synthetic heartbeat**:
  un CronJob que envíe un mensaje de prueba cada 5 minutos al endpoint `/api/chat`.
- Los dashboards 3, 4 y 8 siempre muestran datos porque sus métricas son de fondo.

---

## Métricas de diagnóstico clave (resumen)

| Query de diagnóstico | Qué verifica |
|---|---|
| `up{namespace="amael-ia", pod=~"backend-ia.*"}` | Prometheus alcanza el pod |
| `amael_planner_latency_seconds_count` | Planner ha procesado requests |
| `amael_executor_steps_total` | Executor ha procesado pasos |
| `amael_supervisor_decisions_total` | Supervisor ha evaluado respuestas |
| `amael_tool_calls_total` | Herramientas han sido invocadas |
| `amael_security_rate_limited_total` | Seguridad activa (puede ser 0 si no hay bloqueos) |
| `rate(http_requests_total{namespace="amael-ia",handler="/api/chat"}[5m])` | Tráfico activo al chat |
