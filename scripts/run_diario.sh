#!/bin/bash
# ══════════════════════════════════════════════════════
# run_diario.sh — Ejecución diaria del sistema LegismEx
# ══════════════════════════════════════════════════════
# Instalar en cron con:
#   crontab -e
#   0 7 * * 1-5 /ruta/completa/a/legismex/scripts/run_diario.sh
#
# Descripción de la secuencia:
#   1. Vigilancia del DOF y periódicos estatales (Fase 4) — máxima prioridad
#   2. Reintentos de descargas fallidas (Fase 5)
#   3. Descarga de leyes del índice (Fase 3)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$BASE_DIR/logs/run_diario.log"
PYTHON="${PYTHON:-python3}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "══════════════════════════════════════════"
log "Iniciando ciclo diario LegismEx"
log "══════════════════════════════════════════"

# ── FASE 4: Vigilancia DOF + estados principales ──
log "[FASE 4] Vigilancia del DOF (federal)"
$PYTHON "$SCRIPT_DIR/vigilancia_dof.py" --entidad federal 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en vigilancia DOF — continuando"
}

log "[FASE 4] Vigilancia estados configurados"
for ENTIDAD in cdmx edomex jalisco hidalgo nuevoleon puebla veracruz guanajuato sonora tamaulipas; do
    log "  Vigilando: $ENTIDAD"
    $PYTHON "$SCRIPT_DIR/vigilancia_dof.py" --entidad "$ENTIDAD" 2>&1 | tee -a "$LOG_FILE" || {
        log "  ERROR vigilando $ENTIDAD — continuando"
    }
done

# ── FASE 5: Reintentos ──
log "[FASE 5] Procesando cola de reintentos"
$PYTHON "$SCRIPT_DIR/reintentos.py" 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en reintentos — continuando"
}

# ── FASE 3: Descarga del índice ──
log "[FASE 3] Descargando leyes del índice"
$PYTHON "$SCRIPT_DIR/descarga.py" 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en descarga de índice — continuando"
}

log "Ciclo diario completado"
log "══════════════════════════════════════════"
