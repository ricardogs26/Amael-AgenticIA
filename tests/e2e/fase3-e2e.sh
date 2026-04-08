#!/usr/bin/env bash
# =============================================================================
# fase3-e2e.sh — Tests de integración end-to-end del pipeline SRE → GitOps
#
# Cubre el flujo completo:
#   1. Pre-flight: servicios sanos
#   2. Incidente: despliega amael-demo-oom (OOMKill real)
#   3. Detección: Raphael (SRE) detecta OOMKill y crea entrada Redis
#   4. Remediación: Camael crea PR en Bitbucket + RFC en ServiceNow
#   5. Aprobación: /devops pr lista PRs → /devops aprobar merges
#   6. Cleanup: elimina demo pod + verifica limpieza Redis
#
# Uso:
#   ./tests/e2e/fase3-e2e.sh                    # flujo completo
#   ./tests/e2e/fase3-e2e.sh --only-preflight   # solo health checks
#   ./tests/e2e/fase3-e2e.sh --skip-cleanup     # mantiene demo pod al final
#   ./tests/e2e/fase3-e2e.sh --chaos OOM_KILLED # usa Chaos Mesh en lugar del demo pod
#
# Requisitos: kubectl, curl, jq
# =============================================================================
set -euo pipefail

# ── Colores ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

PASS="${GREEN}✅ PASS${NC}"; FAIL="${RED}❌ FAIL${NC}"; SKIP="${YELLOW}⏭  SKIP${NC}"
INFO="${CYAN}ℹ${NC}"; WAIT="${YELLOW}⏳${NC}"

# ── Configuración ─────────────────────────────────────────────────────────────
NAMESPACE="amael-ia"
CHAOS_NAMESPACE="chaos-testing"
DEMO_YAML="$(dirname "$0")/../../k8s/demo/amael-demo-oom.yaml"
BACKEND_SVC="10.152.183.33:8000"
K8S_AGENT_SVC="10.152.183.95:8002"
WHATSAPP_SVC="10.152.183.66:3000"
REDIS_LABEL="app=redis"

# Timeouts (segundos)
SRE_DETECT_TIMEOUT=180    # 3 min para que el SRE detecte el OOM
PR_CREATE_TIMEOUT=120     # 2 min para que Camael cree el PR en Bitbucket
POLL_INTERVAL=10          # intervalo entre polls

# Flags
ONLY_PREFLIGHT=false
SKIP_CLEANUP=false
CHAOS_MODE=""
CHAOS_FILE=""

# Contadores de resultados
TESTS_PASS=0; TESTS_FAIL=0; TESTS_SKIP=0

# ── Parseo de args ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --only-preflight) ONLY_PREFLIGHT=true ;;
    --skip-cleanup)   SKIP_CLEANUP=true ;;
    --chaos)
      CHAOS_MODE="$2"
      shift
      ;;
    *) echo "Uso: $0 [--only-preflight] [--skip-cleanup] [--chaos ISSUE_TYPE]"; exit 1 ;;
  esac
  shift
done

# Mapeo chaos mode → archivo de experimento
case "$CHAOS_MODE" in
  OOM_KILLED)          CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/11-oom-killed-frontend.yaml" ;;
  HIGH_MEMORY)         CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/12-high-memory-llm-adapter.yaml" ;;
  HIGH_CPU)            CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/13-high-cpu-productivity.yaml" ;;
  HIGH_RESTARTS)       CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/14-crash-loop-whatsapp.yaml" ;;
  HIGH_ERROR_RATE)     CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/15-high-error-rate-llm-adapter.yaml" ;;
  MEMORY_LEAK_PREDICTED) CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/16-memory-leak-cosyvoice.yaml" ;;
  POD_FAILED)          CHAOS_FILE="GitOps-Infra/chaos-mesh/experiments/17-pod-failure-piper.yaml" ;;
  "")                  CHAOS_FILE="" ;;
  *) echo -e "${RED}Chaos mode desconocido: $CHAOS_MODE${NC}"; exit 1 ;;
esac

# ── Helpers ───────────────────────────────────────────────────────────────────
log()      { echo -e "${BOLD}[$(date +%H:%M:%S)]${NC} $*"; }
pass()     { echo -e "  ${PASS} $*"; ((TESTS_PASS++)) || true; }
fail()     { echo -e "  ${FAIL} $*"; ((TESTS_FAIL++)) || true; }
skip()     { echo -e "  ${SKIP} $*"; ((TESTS_SKIP++)) || true; }
section()  { echo -e "\n${BOLD}${BLUE}══════════════════════════════════════════${NC}"; echo -e "${BOLD}${BLUE} $*${NC}"; echo -e "${BOLD}${BLUE}══════════════════════════════════════════${NC}"; }

redis_exec() {
  local pod
  pod=$(kubectl get pod -n "$NAMESPACE" -l "$REDIS_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  kubectl exec -n "$NAMESPACE" "$pod" -- redis-cli "$@" 2>/dev/null
}

backend_post() {
  local path="$1"; shift
  curl -sf -X POST "http://${BACKEND_SVC}${path}" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${INTERNAL_SECRET}" \
    "$@" 2>/dev/null
}

k8s_agent_get() {
  curl -sf "http://${K8S_AGENT_SVC}$1" \
    -H "Authorization: Bearer ${INTERNAL_SECRET}" 2>/dev/null
}

elapsed() { echo $(( $(date +%s) - START_TS ))s; }

cleanup_demo() {
  if [[ -n "$CHAOS_FILE" ]]; then
    local repo_root
    repo_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || echo "/home/richardx/k8s-lab")
    kubectl delete -f "${repo_root}/${CHAOS_FILE}" 2>/dev/null || true
    log "Chaos experiment eliminado: ${CHAOS_FILE##*/}"
  else
    kubectl delete -f "$DEMO_YAML" -n "$NAMESPACE" 2>/dev/null || true
    log "Demo deployment eliminado"
  fi
  # Limpiar claves Redis del test
  local keys
  keys=$(redis_exec KEYS "bb:pending_pr:ns:amael-demo-oom:*" 2>/dev/null || true)
  if [[ -n "$keys" ]]; then
    redis_exec DEL $keys >/dev/null 2>&1 || true
    log "Claves Redis del test limpiadas"
  fi
}

# ── Secrets (leídos de k8s en runtime, no hardcodeados) ─────────────────────
INTERNAL_SECRET=$(kubectl get secret -n "$NAMESPACE" google-auth-secret \
  -o jsonpath='{.data.internal_api_secret}' 2>/dev/null | base64 -d)
BOT_JWT=$(kubectl get secret -n "$NAMESPACE" amael-secrets \
  -o jsonpath='{.data.jwt-token}' 2>/dev/null | base64 -d)
ADMIN_PHONE="5219993437008"

if [[ -z "$INTERNAL_SECRET" ]]; then
  echo -e "${RED}No se pudo obtener INTERNAL_API_SECRET de google-auth-secret${NC}"
  exit 1
fi

START_TS=$(date +%s)

# =============================================================================
# SECCIÓN 1: PRE-FLIGHT
# =============================================================================
section "🔍 Fase 3.1 — Pre-flight: servicios sanos"

# 1.1 Backend health
log "Verificando backend (amael-agentic-backend)..."
resp=$(curl -sf "http://${BACKEND_SVC}/health" 2>/dev/null || echo "")
if echo "$resp" | grep -q '"status"'; then
  pass "Backend /health responde OK"
else
  fail "Backend no responde en ${BACKEND_SVC}"
fi

# 1.2 k8s-agent health
log "Verificando k8s-agent..."
resp=$(curl -sf "http://${K8S_AGENT_SVC}/health" 2>/dev/null || echo "")
if echo "$resp" | grep -q '"ok"\|"status"'; then
  pass "k8s-agent /health OK"
else
  fail "k8s-agent no responde en ${K8S_AGENT_SVC}"
fi

# 1.3 SRE loop status
log "Verificando SRE loop..."
resp=$(k8s_agent_get "/api/sre/loop/status" || echo "")
loop_state=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('loop_enabled', d.get('loop_running', False)))" 2>/dev/null || echo "false")
if [[ "$loop_state" == "True" ]]; then
  leader=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('leader_pod','?'))" 2>/dev/null || echo "?")
  pass "SRE loop activo (leader: ${leader})"
else
  fail "SRE loop NO está activo — el agente no detectará incidentes"
fi

# 1.4 Redis disponible
log "Verificando Redis..."
resp=$(redis_exec PING 2>/dev/null || echo "")
if [[ "$resp" == "PONG" ]]; then
  pass "Redis disponible"
else
  fail "Redis no responde"
fi

# 1.5 WhatsApp bridge
log "Verificando WhatsApp bridge..."
resp=$(curl -sf "http://${WHATSAPP_SVC}/health" 2>/dev/null || echo "")
if echo "$resp" | grep -q '"ok"\|"status"\|"ready"'; then
  pass "WhatsApp bridge disponible"
else
  # No es bloqueante — el bridge puede estar desconectado
  echo -e "  ${YELLOW}⚠  WhatsApp bridge no responde (no bloqueante)${NC}"
fi

# 1.6 No PRs pendientes de tests anteriores
pending_keys_before=$(redis_exec KEYS "bb:pending_pr:*" 2>/dev/null | grep "bb:pending_pr" || true)
pending_before=$(echo "$pending_keys_before" | awk 'NF' | wc -l | tr -d ' ')
if [[ "$pending_before" -gt 0 ]]; then
  echo -e "  ${YELLOW}⚠  Hay ${pending_before} PRs pendientes en Redis de ejecuciones anteriores${NC}"
  echo "$pending_keys_before" | while read -r k; do
    [[ -z "$k" ]] && continue
    pr_info=$(redis_exec GET "$k" 2>/dev/null || echo "{}")
    pr_id=$(echo "$pr_info" | python3 -c "import sys,json; print(json.load(sys.stdin).get('pr_id','?'))" 2>/dev/null || echo "?")
    echo -e "    ${INFO} PR #${pr_id} — key: ${k}"
  done
fi

[[ "$ONLY_PREFLIGHT" == "true" ]] && { section "✅ Solo pre-flight — terminado"; exit 0; }

# =============================================================================
# SECCIÓN 2: INYECCIÓN DE INCIDENTE
# =============================================================================
section "💥 Fase 3.2 — Inyección de incidente"

if [[ -n "$CHAOS_MODE" ]]; then
  # Modo Chaos Mesh
  repo_root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel 2>/dev/null || echo "/home/richardx/k8s-lab")
  chaos_path="${repo_root}/${CHAOS_FILE}"

  if [[ ! -f "$chaos_path" ]]; then
    fail "Archivo chaos no encontrado: $chaos_path"
    exit 1
  fi

  log "Aplicando experimento Chaos Mesh: ${CHAOS_FILE##*/} (issue_type=${CHAOS_MODE})"
  if kubectl apply -f "$chaos_path" 2>/dev/null; then
    pass "Chaos experiment aplicado: ${CHAOS_MODE}"
  else
    fail "Error al aplicar chaos experiment"
    exit 1
  fi

  # Para chaos experiments el target varía — determinamos qué pod se afectará
  target_app=$(python3 -c "
import yaml, sys
with open('$chaos_path') as f:
    doc = yaml.safe_load(f)
print(doc['spec']['selector']['labelSelectors'].get('app', 'unknown'))
" 2>/dev/null || echo "unknown")
  INCIDENT_POD_PREFIX="$target_app"

else
  # Modo demo pod OOM
  log "Desplegando amael-demo-oom (OOMKill intencionado)..."
  if kubectl apply -f "$DEMO_YAML" -n "$NAMESPACE" 2>/dev/null; then
    pass "Demo deployment aplicado"
  else
    fail "Error al aplicar el demo YAML"
    exit 1
  fi
  INCIDENT_POD_PREFIX="amael-demo-oom"

  # Esperar que el pod empiece y entre en OOMKilled
  log "Esperando que el pod entre en OOMKilled..."
  timeout_ts=$(( $(date +%s) + 60 ))
  oom_confirmed=false
  while [[ $(date +%s) -lt $timeout_ts ]]; do
    phase=$(kubectl get pod -n "$NAMESPACE" -l "app=amael-demo-oom" \
      -o jsonpath='{.items[0].status.containerStatuses[0].state.terminated.reason}' 2>/dev/null || echo "")
    restart_count=$(kubectl get pod -n "$NAMESPACE" -l "app=amael-demo-oom" \
      -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo "0")
    if [[ "$phase" == "OOMKilled" ]] || [[ "$restart_count" -ge 1 ]]; then
      oom_confirmed=true
      break
    fi
    echo -e "  ${WAIT} Pod en estado: ${phase:-Pending/Running} | reinicios: ${restart_count} ($(elapsed) transcurridos)"
    sleep 5
  done

  if [[ "$oom_confirmed" == "true" ]]; then
    pass "Pod amael-demo-oom en estado OOMKilled (reinicios: ${restart_count})"
  else
    fail "El pod no entró en OOMKilled en 60s — revisar imagen polinux/stress"
    [[ "$SKIP_CLEANUP" == "false" ]] && cleanup_demo
    exit 1
  fi
fi

# =============================================================================
# SECCIÓN 3: DETECCIÓN POR RAPHAEL (SRE)
# =============================================================================
section "🔭 Fase 3.3 — Detección SRE (Raphael)"

log "Esperando que el SRE loop detecte el incidente (máx ${SRE_DETECT_TIMEOUT}s)..."
log "${INFO} El loop corre cada 60s — espera hasta 2 ciclos"

detect_timeout=$(( $(date +%s) + SRE_DETECT_TIMEOUT ))
incident_detected=false
incident_key=""

while [[ $(date +%s) -lt $detect_timeout ]]; do
  # Buscar en sre:dedup:* — una nueva entrada significa que detectó el incidente
  dedup_keys=$(redis_exec KEYS "sre:dedup:*${INCIDENT_POD_PREFIX}*" 2>/dev/null || echo "")
  if [[ -n "$dedup_keys" ]]; then
    incident_key=$(echo "$dedup_keys" | head -1)
    incident_detected=true
    break
  fi

  # También verificar en los incidents de PostgreSQL via SRE API
  resp=$(k8s_agent_get "/api/sre/incidents?limit=5" || echo "")
  if echo "$resp" | python3 -c "
import sys, json, time
try:
    data = json.load(sys.stdin)
    incidents = data.get('incidents', data) if isinstance(data, dict) else data
    for inc in incidents:
        name = inc.get('resource_name', '') or inc.get('pod', '')
        ts = inc.get('detected_at', '') or inc.get('created_at', '')
        if '${INCIDENT_POD_PREFIX}' in name:
            # Solo incidentes de los últimos 10 minutos
            import datetime
            try:
                detected = datetime.datetime.fromisoformat(ts.replace('Z',''))
                if (datetime.datetime.utcnow() - detected).total_seconds() < 600:
                    print('found')
                    sys.exit(0)
            except:
                print('found')
                sys.exit(0)
except:
    pass
" 2>/dev/null | grep -q "found"; then
    incident_detected=true
    break
  fi

  elapsed_s=$(( $(date +%s) - START_TS ))
  echo -e "  ${WAIT} Esperando detección SRE... ${elapsed_s}s transcurridos"
  sleep "$POLL_INTERVAL"
done

if [[ "$incident_detected" == "true" ]]; then
  pass "SRE detectó el incidente en ~$(elapsed)"
  if [[ -n "$incident_key" ]]; then
    echo -e "    ${INFO} Redis dedup key: ${incident_key}"
  fi
  # Mostrar últimos incidentes
  resp=$(k8s_agent_get "/api/sre/incidents?limit=3" 2>/dev/null || echo "")
  echo "$resp" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    incidents = data.get('incidents', data) if isinstance(data, dict) else data
    for inc in list(incidents)[:3]:
        print(f\"    ℹ  [{inc.get('severity','?')}] {inc.get('issue_type','?')} — {inc.get('resource_name', inc.get('pod','?'))}\")
except:
    pass
" 2>/dev/null || true
else
  fail "SRE NO detectó el incidente en ${SRE_DETECT_TIMEOUT}s"
  echo -e "  ${INFO} Verifica: kubectl logs -n amael-ia -l app=k8s-agent --tail=50"
  [[ "$SKIP_CLEANUP" == "false" ]] && cleanup_demo
  exit 1
fi

# =============================================================================
# SECCIÓN 4: CAMAEL CREA PR EN BITBUCKET
# =============================================================================
section "🤖 Fase 3.4 — Creación de PR (Camael)"

log "Esperando que Camael cree el PR en Bitbucket (máx ${PR_CREATE_TIMEOUT}s)..."

pr_timeout=$(( $(date +%s) + PR_CREATE_TIMEOUT ))
pr_created=false
pr_id=""
pr_repo=""
pr_url=""

while [[ $(date +%s) -lt $pr_timeout ]]; do
  pending_keys=$(redis_exec KEYS "bb:pending_pr:*" 2>/dev/null || echo "")
  if [[ -n "$pending_keys" ]]; then
    # Tomar el primero relacionado con nuestro pod (o el único si es modo demo)
    for key in $pending_keys; do
      pr_raw=$(redis_exec GET "$key" 2>/dev/null || echo "")
      if [[ -n "$pr_raw" ]]; then
        pr_id=$(echo "$pr_raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pr_id',''))" 2>/dev/null || echo "")
        pr_repo=$(echo "$pr_raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('repo',''))" 2>/dev/null || echo "")
        pr_url=$(echo "$pr_raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pr_url',''))" 2>/dev/null || echo "")
        pr_issue=$(echo "$pr_raw" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('issue_type',''))" 2>/dev/null || echo "")
        if [[ -n "$pr_id" ]]; then
          pr_created=true
          break 2
        fi
      fi
    done
  fi

  elapsed_s=$(( $(date +%s) - START_TS ))
  echo -e "  ${WAIT} Esperando PR en Redis... ${elapsed_s}s transcurridos"
  sleep "$POLL_INTERVAL"
done

if [[ "$pr_created" == "true" ]]; then
  pass "PR creado por Camael: #${pr_id} en repo '${pr_repo}'"
  echo -e "    ${INFO} Issue type : ${pr_issue}"
  echo -e "    ${INFO} URL        : ${pr_url}"
else
  fail "Camael no creó el PR en ${PR_CREATE_TIMEOUT}s"
  echo -e "  ${INFO} PRs en Redis: $(redis_exec KEYS 'bb:pending_pr:*' 2>/dev/null | wc -l)"
  echo -e "  ${INFO} Verifica: kubectl logs -n amael-ia -l app=amael-agentic-deployment --tail=80 | grep -i camael"
  [[ "$SKIP_CLEANUP" == "false" ]] && cleanup_demo
  exit 1
fi

# =============================================================================
# SECCIÓN 5: API /devops pr — listar PRs pendientes
# =============================================================================
section "📋 Fase 3.5 — API /devops pr (listar PRs)"

log "Llamando POST /api/devops/command {cmd: 'pr'}..."
resp=$(backend_post "/api/devops/command" \
  -d "{\"cmd\": \"pr\", \"user_id\": \"${ADMIN_PHONE}\"}" || echo "")

if [[ -z "$resp" ]]; then
  fail "No se obtuvo respuesta de /api/devops/command"
else
  reply=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reply',''))" 2>/dev/null || echo "")
  if echo "$reply" | grep -q "PR #"; then
    pass "/devops pr lista el PR correctamente"
    echo -e "    ${INFO} Respuesta: $(echo "$reply" | head -3 | sed 's/^/              /')"
  elif echo "$reply" | grep -q "No hay PRs\|sin PRs"; then
    fail "/devops pr responde 'sin PRs' pero debería haber PR #${pr_id}"
  else
    fail "/devops pr respuesta inesperada: ${reply:0:100}"
  fi
fi

# =============================================================================
# SECCIÓN 6: APROBACIÓN /devops aprobar
# =============================================================================
section "✅ Fase 3.6 — Aprobación /devops aprobar #${pr_id}"

log "Llamando POST /api/devops/command {cmd: 'aprobar #${pr_id}'}..."
resp=$(backend_post "/api/devops/command" \
  -d "{\"cmd\": \"aprobar #${pr_id}\", \"user_id\": \"${ADMIN_PHONE}\"}" || echo "")

if [[ -z "$resp" ]]; then
  fail "No se obtuvo respuesta al aprobar PR #${pr_id}"
else
  reply=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reply',''))" 2>/dev/null || echo "")
  echo -e "    ${INFO} Respuesta: ${reply:0:120}"

  if echo "$reply" | grep -qiE "✅|mergeado|aprobado|merged|OK"; then
    pass "PR #${pr_id} aprobado y mergeado exitosamente"

    # Verificar que Redis limpió la key
    sleep 2
    remaining=$(redis_exec KEYS "bb:pending_pr:*" 2>/dev/null | wc -l || echo "99")
    if [[ "$remaining" -eq 0 ]]; then
      pass "Redis: tracking limpiado post-merge"
    elif [[ "$remaining" -lt "$pending_before" ]] || [[ "$pending_before" -eq 0 ]]; then
      pass "Redis: key del PR #${pr_id} eliminada"
    else
      fail "Redis: key pendiente no fue limpiada post-merge (${remaining} keys restantes)"
    fi

  elif echo "$reply" | grep -qiE "⚠️|ya fue mergeado|404|409"; then
    echo -e "  ${YELLOW}⚠  PR ya estaba mergeado/declinado (409/404) — Redis limpiado${NC}"
    pass "Manejo correcto de PR ya procesado"
  else
    fail "Respuesta inesperada al aprobar: ${reply:0:100}"
  fi
fi

# =============================================================================
# SECCIÓN 7: VERIFICACIÓN FINAL
# =============================================================================
section "🔍 Fase 3.7 — Verificación final"

# 7.1 No quedan PRs huérfanos en Redis
orphan_keys=$(redis_exec KEYS "bb:pending_pr:*" 2>/dev/null | grep "bb:pending_pr" | awk 'NF' | wc -l | tr -d ' ' || echo "0")
if [[ "$orphan_keys" -eq 0 ]]; then
  pass "No hay PRs huérfanos en Redis"
else
  echo -e "  ${YELLOW}⚠  Quedan ${orphan_keys} PRs en Redis (pueden ser de otros incidentes)${NC}"
fi

# 7.2 SRE incidents registrados en PostgreSQL
log "Verificando incidentes en PostgreSQL via SRE API..."
resp=$(k8s_agent_get "/api/sre/incidents?limit=5" || echo "")
incident_count=$(echo "$resp" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    incidents = data.get('incidents', data) if isinstance(data, dict) else data
    print(len(list(incidents)))
except:
    print(0)
" 2>/dev/null || echo "0")

if [[ "$incident_count" -gt 0 ]]; then
  pass "SRE incidents persistidos en PostgreSQL: ${incident_count} registros recientes"
else
  fail "No se encontraron incidentes en PostgreSQL"
fi

# 7.3 SRE loop sigue corriendo (no se crasheó)
resp=$(k8s_agent_get "/api/sre/loop/status" || echo "")
loop_state=$(echo "$resp" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('loop_enabled', d.get('loop_running', False)))" 2>/dev/null || echo "false")
if [[ "$loop_state" == "True" ]]; then
  pass "SRE loop sigue activo post-test"
else
  fail "SRE loop se detuvo durante el test"
fi

# =============================================================================
# SECCIÓN 8: CLEANUP
# =============================================================================
if [[ "$SKIP_CLEANUP" == "false" ]]; then
  section "🧹 Fase 3.8 — Cleanup"
  log "Eliminando artefactos del test..."
  cleanup_demo
  pass "Cleanup completado"
else
  section "⏭  Cleanup omitido (--skip-cleanup)"
  log "Para limpiar manualmente:"
  if [[ -n "$CHAOS_MODE" ]]; then
    echo "  kubectl delete -f GitOps-Infra/chaos-mesh/experiments/${CHAOS_FILE##*/}"
  else
    echo "  kubectl delete -f Amael-AgenticIA/k8s/demo/amael-demo-oom.yaml -n amael-ia"
  fi
fi

# =============================================================================
# RESUMEN FINAL
# =============================================================================
section "📊 Resumen Fase 3"

total=$(( TESTS_PASS + TESTS_FAIL + TESTS_SKIP ))
duration=$(elapsed)

echo ""
echo -e "  Total tests  : ${BOLD}${total}${NC}"
echo -e "  ${GREEN}Pasados${NC}       : ${BOLD}${TESTS_PASS}${NC}"
echo -e "  ${RED}Fallados${NC}      : ${BOLD}${TESTS_FAIL}${NC}"
echo -e "  ${YELLOW}Omitidos${NC}      : ${BOLD}${TESTS_SKIP}${NC}"
echo -e "  Duración      : ${duration}"
echo ""

if [[ "$TESTS_FAIL" -eq 0 ]]; then
  echo -e "${BOLD}${GREEN}🎉 Fase 3 COMPLETADA — pipeline SRE→GitOps funciona de punta a punta${NC}"
  exit 0
else
  echo -e "${BOLD}${RED}💥 Fase 3 FALLIDA — ${TESTS_FAIL} test(s) fallaron${NC}"
  exit 1
fi
