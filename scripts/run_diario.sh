#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
# run_diario.sh — Orquestador diario del Sistema de Vigilancia
#                  Legislativa Mexicana (LegismEx)
#
# Pipeline completo:
#   1. Re-scraping de catálogos legislativos (semanal, solo lunes)
#   2. Regenerar índice consolidado (leyes_index.json)
#   3. Vigilancia del DOF y periódicos oficiales estatales
#   4. Análisis con LLM (Claude) de publicaciones detectadas
#   5. Re-descarga de leyes marcadas como actualizadas
#   6. Resolución inteligente de pendientes >7 días (LLM + scraping)
#   7. Reintentos de descargas fallidas previas
#   8. Regenerar dashboard HTML
#
# Uso:
#   ./run_diario.sh                # Pipeline diario estándar
#   ./run_diario.sh --full         # Forzar re-scraping + descarga completa
#   ./run_diario.sh --solo-dof     # Solo vigilancia DOF federal
#   ./run_diario.sh --dry-run      # Análisis LLM sin ejecutar acciones
#   ./run_diario.sh --sin-llm      # Vigilancia + descarga, sin análisis LLM
#
# Cron recomendado (diario a las 7:00 AM hora CDMX, lun-vie):
#   0 7 * * 1-5 /ruta/a/LegismEx/scripts/run_diario.sh >> /ruta/a/LegismEx/logs/cron.log 2>&1
#
# Variables de entorno requeridas:
#   ANTHROPIC_API_KEY  — en .env o exportada (solo si se usa análisis LLM)
# ═══════════════════════════════════════════════════════════════════

set -uo pipefail

# ── Rutas ─────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"
LOGS_DIR="$BASE_DIR/logs"
PYTHON="${PYTHON:-python3}"
DAY_OF_WEEK="$(date +%u)"  # 1=lunes ... 7=domingo
HOY=$(date +%Y-%m-%d)

mkdir -p "$LOGS_DIR"
LOG_FILE="$LOGS_DIR/pipeline_${HOY}.log"

# ── Argumentos ────────────────────────────────────────────────────
FULL=false
SOLO_DOF=false
DRY_RUN=false
SIN_LLM=false

for arg in "$@"; do
    case "$arg" in
        --full)     FULL=true ;;
        --solo-dof) SOLO_DOF=true ;;
        --dry-run)  DRY_RUN=true ;;
        --sin-llm)  SIN_LLM=true ;;
        --help|-h)
            head -28 "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "Argumento desconocido: $arg" >&2
            exit 1
            ;;
    esac
done

# ── Logging ───────────────────────────────────────────────────────
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$LOG_FILE" >&2
}

# ── Cargar .env ───────────────────────────────────────────────────
if [ -f "$BASE_DIR/.env" ]; then
    set -a
    # shellcheck source=/dev/null
    source "$BASE_DIR/.env"
    set +a
fi

# ══════════════════════════════════════════════════════════════════
log "════════════════════════════════════════════════════════"
log "Pipeline LegismEx — $HOY"
log "════════════════════════════════════════════════════════"

ERRORES=0
INICIO=$(date +%s)

# ── PASO 1: Re-scraping de catálogos (lunes o --full) ────────────
if [[ "$DAY_OF_WEEK" == "1" ]] || [[ "$FULL" == true ]]; then
    log ""
    log "── PASO 1: Re-scraping de catálogos (33 entidades) ──"
    $PYTHON "$SCRIPT_DIR/scraper_catalogo.py" --todas 2>&1 | tee -a "$LOG_FILE" || {
        log_error "Scraping de catálogos falló — continuando"
        ERRORES=$((ERRORES + 1))
    }
else
    log ""
    log "── PASO 1: Re-scraping saltado (solo lunes o con --full) ──"
fi

# ── PASO 2: Regenerar índice consolidado ──────────────────────────
log ""
log "── PASO 2: Regenerando leyes_index.json ──"
$PYTHON "$SCRIPT_DIR/generar_indice.py" 2>&1 | tee -a "$LOG_FILE" || {
    log_error "Regeneración de índice falló — continuando"
    ERRORES=$((ERRORES + 1))
}

# ── PASO 3: Vigilancia del DOF y periódicos oficiales ─────────────
log ""
log "── PASO 3: Vigilancia de publicaciones oficiales ──"

if [ "$SOLO_DOF" = true ]; then
    log "Consultando solo DOF federal..."
    $PYTHON "$SCRIPT_DIR/vigilancia_dof.py" 2>&1 | tee -a "$LOG_FILE" || {
        log_error "Vigilancia DOF falló"
        ERRORES=$((ERRORES + 1))
    }
else
    log "Consultando DOF + todos los estados configurados..."
    $PYTHON "$SCRIPT_DIR/vigilancia_dof.py" --todas 2>&1 | tee -a "$LOG_FILE" || {
        log_error "Vigilancia completa falló"
        ERRORES=$((ERRORES + 1))
    }
fi

# ── PASO 4: Análisis LLM de publicaciones ────────────────────────
COLA="$LOGS_DIR/cola_procesamiento.json"
TIENE_PUBLICACIONES=false

if [ -f "$COLA" ]; then
    N_PUB=$($PYTHON -c "import json; print(len(json.load(open('$COLA'))))" 2>/dev/null || echo 0)
    if [ "$N_PUB" -gt 0 ]; then
        TIENE_PUBLICACIONES=true
    fi
fi

if [ "$SIN_LLM" = true ]; then
    log ""
    log "── PASO 4: Análisis LLM saltado (--sin-llm) ──"
elif [ "$TIENE_PUBLICACIONES" = false ]; then
    log ""
    log "── PASO 4: Sin publicaciones pendientes de análisis ──"
else
    log ""
    log "── PASO 4: Análisis con LLM — $N_PUB publicación(es) ──"

    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        log_error "ANTHROPIC_API_KEY no configurada — saltando análisis LLM"
        ERRORES=$((ERRORES + 1))
    else
        LLM_ARGS=""
        if [ "$DRY_RUN" = true ]; then
            LLM_ARGS="--dry-run"
            log "Modo dry-run: solo clasificar, sin ejecutar acciones."
        fi

        $PYTHON "$SCRIPT_DIR/analizar_publicaciones.py" $LLM_ARGS 2>&1 | tee -a "$LOG_FILE" || {
            log_error "Análisis LLM falló"
            ERRORES=$((ERRORES + 1))
        }
    fi
fi

# ── PASO 5: Re-descarga de leyes actualizadas ────────────────────
if [ "$DRY_RUN" = false ]; then
    log ""
    log "── PASO 5: Re-descarga de leyes actualizadas ──"

    INDEX="$BASE_DIR/leyes_index.json"
    if [ -f "$INDEX" ]; then
        PENDIENTES=$($PYTHON -c "
import json
with open('$INDEX') as f:
    idx = json.load(f)
pend = [l['id'] for l in idx if l.get('estado') == 'pendiente_actualizacion']
for p in pend:
    print(p)
" 2>/dev/null || true)

        if [ -n "$PENDIENTES" ]; then
            N_PEND=$(echo "$PENDIENTES" | wc -l)
            log "$N_PEND ley(es) marcadas para re-descarga."
            echo "$PENDIENTES" | while read -r LEY_ID; do
                if [ -n "$LEY_ID" ]; then
                    log "  Descargando: $LEY_ID"
                    $PYTHON "$SCRIPT_DIR/descarga.py" --id "$LEY_ID" 2>&1 | tee -a "$LOG_FILE" || {
                        log_error "Descarga falló para $LEY_ID"
                    }
                fi
            done
        else
            log "No hay leyes pendientes de re-descarga."
        fi
    fi
else
    log ""
    log "── PASO 5: Re-descarga saltada (--dry-run) ──"
fi

# ── PASO 6: Resolución inteligente de pendientes >7 días ──────────
if [ "$DRY_RUN" = false ] && [ "$SIN_LLM" = false ]; then
    log ""
    log "── PASO 6: Resolución inteligente de pendientes antiguos ──"

    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        log "Sin API key — resolver_pendientes ejecutará solo estrategias A y B"
    fi

    $PYTHON "$SCRIPT_DIR/resolver_pendientes.py" 2>&1 | tee -a "$LOG_FILE" || {
        log_error "Resolución de pendientes falló"
        ERRORES=$((ERRORES + 1))
    }
else
    log ""
    log "── PASO 6: Resolución inteligente saltada ──"
fi

# ── PASO 7: Reintentos de descargas fallidas ──────────────────────
if [ "$DRY_RUN" = false ]; then
    log ""
    log "── PASO 6: Procesando cola de reintentos ──"

    COLA_REINTENTOS="$BASE_DIR/cola_reintentos.json"
    if [ -f "$COLA_REINTENTOS" ]; then
        N_REINTENTOS=$($PYTHON -c "import json; print(len(json.load(open('$COLA_REINTENTOS'))))" 2>/dev/null || echo 0)
        if [ "$N_REINTENTOS" -gt 0 ]; then
            log "$N_REINTENTOS elemento(s) en cola de reintentos."
            $PYTHON "$SCRIPT_DIR/reintentos.py" 2>&1 | tee -a "$LOG_FILE" || {
                log_error "Reintentos fallaron"
                ERRORES=$((ERRORES + 1))
            }
        else
            log "Cola de reintentos vacía."
        fi
    else
        log "No hay cola de reintentos."
    fi
fi

# ── PASO 8: Regenerar dashboard y publicar ────────────────────────
log ""
log "── PASO 8: Generando y publicando dashboard ──"
$PYTHON "$SCRIPT_DIR/generar_dashboard.py" 2>&1 | tee -a "$LOG_FILE" || {
    log_error "Generación de dashboard falló"
}

# Publicar a gh-pages (GitHub Pages)
if [ -f "$BASE_DIR/dashboard.html" ]; then
    DASH_TMP=$(mktemp)
    cp "$BASE_DIR/dashboard.html" "$DASH_TMP"
    CURRENT_BRANCH=$(git -C "$BASE_DIR" branch --show-current)
    git -C "$BASE_DIR" stash --quiet 2>/dev/null
    git -C "$BASE_DIR" checkout gh-pages --quiet 2>/dev/null && {
        cp "$DASH_TMP" "$BASE_DIR/dashboard.html"
        cp "$DASH_TMP" "$BASE_DIR/index.html"
        git -C "$BASE_DIR" add index.html dashboard.html 2>/dev/null
        git -C "$BASE_DIR" diff --cached --quiet 2>/dev/null || {
            git -C "$BASE_DIR" commit -m "Dashboard actualizado: $HOY" --quiet
            git -C "$BASE_DIR" push --quiet 2>/dev/null && {
                log "Dashboard publicado en GitHub Pages"
            } || log_error "Push a gh-pages falló"
        }
        git -C "$BASE_DIR" checkout "$CURRENT_BRANCH" --quiet 2>/dev/null
    } || log_error "No se pudo cambiar a gh-pages"
    git -C "$BASE_DIR" stash pop --quiet 2>/dev/null
    rm -f "$DASH_TMP"
fi


# ── Resumen final ─────────────────────────────────────────────────
FIN=$(date +%s)
DURACION=$(( FIN - INICIO ))
MINUTOS=$(( DURACION / 60 ))
SEGUNDOS=$(( DURACION % 60 ))

log ""
log "════════════════════════════════════════════════════════"
if [ "$ERRORES" -gt 0 ]; then
    log "Pipeline completado con $ERRORES error(es) en ${MINUTOS}m ${SEGUNDOS}s"
    log "Revisar: $LOG_FILE"
    exit 1
else
    log "Pipeline completado exitosamente en ${MINUTOS}m ${SEGUNDOS}s"
    exit 0
fi
