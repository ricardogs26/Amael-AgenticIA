#!/usr/bin/env bash
# Instala la ingesta del vault en el host: cron cada hora + hooks post-commit y post-merge.
# Idempotente. Los hooks corren en segundo plano para no frenar el commit.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VAULT="${VAULT_DIR:-$HOME/obsidian-vault}"
ESTADO="$HOME/.local/state/vault-ingest"
PY="$(command -v python3)"
mkdir -p "$ESTADO"

cat > "$ESTADO/correr.sh" <<SCRIPT
#!/usr/bin/env bash
# flock: el cron y los hooks nunca corren dos ingestas a la vez.
exec flock -w 900 "$ESTADO/candado" "$PY" "$REPO/knowledge/vault_ingest.py" >> "$ESTADO/ingesta.log" 2>&1
SCRIPT
chmod +x "$ESTADO/correr.sh"

for hook in post-commit post-merge; do
  cat > "$VAULT/.git/hooks/$hook" <<HOOK
#!/bin/sh
# Instalado por Amael-AgenticIA/knowledge/instalar-vault-ingest.sh
nohup "$ESTADO/correr.sh" >/dev/null 2>&1 &
HOOK
  chmod +x "$VAULT/.git/hooks/$hook"
done

LINEA="17 * * * * $ESTADO/correr.sh"
( crontab -l 2>/dev/null | grep -vF "$ESTADO/correr.sh" || true; echo "$LINEA" ) | crontab -
echo "Instalado: cron minuto 17, hooks en $VAULT/.git/hooks, log en $ESTADO/ingesta.log"
