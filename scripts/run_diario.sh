#!/bin/bash
# ══════════════════════════════════════════════════════
# run_diario.sh — Ejecución diaria del sistema LegismEx
# ══════════════════════════════════════════════════════
# Instalar en cron con:
#   crontab -e
#   0 7 * * 1-5 /ruta/completa/a/DerIAMex/scripts/run_diario.sh
#
# Secuencia:
#   1. Re-scraping de catálogos (semanal, solo lunes)
#   2. Regenerar índice consolidado
#   3. Descarga de PDFs nuevos o actualizados
#   4. Vigilancia del DOF y periódicos estatales
#   5. Reintentos de descargas fallidas
#
# Para ejecución completa inicial (todo desde cero):
#   bash scripts/run_diario.sh --full

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="$BASE_DIR/logs/run_diario.log"
PYTHON="${PYTHON:-python3}"
DAY_OF_WEEK="$(date +%u)"  # 1=lunes

mkdir -p "$BASE_DIR/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "══════════════════════════════════════════"
log "Iniciando ciclo LegismEx"
log "══════════════════════════════════════════"

# ── PASO 1: Re-scraping de catálogos (lunes o --full) ──
if [[ "$DAY_OF_WEEK" == "1" ]] || [[ "${1:-}" == "--full" ]]; then
    log "[PASO 1] Re-scraping de catálogos (33 entidades)"
    $PYTHON "$SCRIPT_DIR/scraper_catalogo.py" --todas 2>&1 | tee -a "$LOG_FILE" || {
        log "ERROR en scraping de catálogos — continuando"
    }
else
    log "[PASO 1] Saltar re-scraping (solo se ejecuta los lunes o con --full)"
fi

# ── PASO 2: Regenerar índice consolidado ──
log "[PASO 2] Regenerando leyes_index.json"
$PYTHON "$SCRIPT_DIR/generar_indice.py" 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR regenerando índice — continuando"
}

# ── PASO 3: Descargar PDFs ──
log "[PASO 3] Descargando PDFs pendientes"
$PYTHON "$SCRIPT_DIR/descarga.py" 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en descarga — continuando"
}

# ── PASO 4: Vigilancia DOF + periódicos estatales ──
log "[PASO 4] Vigilancia del DOF (federal)"
$PYTHON "$SCRIPT_DIR/vigilancia_dof.py" --entidad federal 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en vigilancia DOF — continuando"
}

log "[PASO 4] Vigilancia periódicos estatales"
for ENTIDAD in cdmx edomex jalisco hidalgo nuevoleon puebla veracruz guanajuato sonora tamaulipas; do
    $PYTHON "$SCRIPT_DIR/vigilancia_dof.py" --entidad "$ENTIDAD" 2>&1 | tee -a "$LOG_FILE" || {
        log "  ERROR vigilando $ENTIDAD — continuando"
    }
done

# ── PASO 5: Reintentos ──
log "[PASO 5] Procesando cola de reintentos"
$PYTHON "$SCRIPT_DIR/reintentos.py" 2>&1 | tee -a "$LOG_FILE" || {
    log "ERROR en reintentos — continuando"
}

# ── Resumen ──
log "Ciclo completado"
$PYTHON "$SCRIPT_DIR/generar_indice.py" --resumen 2>&1 | tail -5 | tee -a "$LOG_FILE"
log "══════════════════════════════════════════"
