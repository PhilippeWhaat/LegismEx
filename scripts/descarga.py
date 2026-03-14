#!/usr/bin/env python3
"""
Script de descarga y verificación de cambios legislativos.
Fase 3 del Sistema de Vigilancia Legislativa Mexicana (LegismEx).

Uso:
    python3 descarga.py                  # Procesa todas las leyes del índice
    python3 descarga.py --id federal_constitucion  # Procesa una ley específica
    python3 descarga.py --entidad cdmx   # Procesa todas las leyes de una entidad
"""

import hashlib
import json
import logging
import shutil
import sys
import argparse
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

# ──────────────────────────────────────────────
# Configuración de rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "leyes_index.json"
LOGS_DIR = BASE_DIR / "logs"
COLA_REINTENTOS = BASE_DIR / "cola_reintentos.json"

LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "actualizaciones.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

exitosas_log = LOGS_DIR / "descargas_exitosas.log"
fallidas_log = LOGS_DIR / "descargas_fallidas.log"


def log_exitosa(mensaje: str):
    with open(exitosas_log, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {mensaje}\n")


def log_fallida(mensaje: str):
    with open(fallidas_log, "a") as f:
        f.write(f"{datetime.now().isoformat()} | {mensaje}\n")


# ──────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────
def calcular_hash_md5(ruta: Path) -> str:
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def directorio_entidad(entidad: str) -> Path:
    if entidad == "federal":
        return BASE_DIR / "federal"
    return BASE_DIR / "estados" / entidad


def ruta_ley(ley: dict) -> Path:
    ext = ley.get("formato", "pdf")
    return directorio_entidad(ley["entidad"]) / "leyes" / f"{ley['id']}.{ext}"


def ruta_changelog(ley: dict) -> Path:
    return directorio_entidad(ley["entidad"]) / "changelog"


# ──────────────────────────────────────────────
# Descarga
# ──────────────────────────────────────────────
def descargar_archivo(url: str, destino: Path) -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; LegismEx/1.0; "
            "Sistema de Vigilancia Legislativa)"
        )
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            destino.parent.mkdir(parents=True, exist_ok=True)
            with open(destino, "wb") as f:
                shutil.copyfileobj(resp, f)
        return True
    except urllib.error.HTTPError as e:
        log.error(f"HTTP {e.code} al descargar {url}")
        return False
    except urllib.error.URLError as e:
        log.error(f"Error de red al descargar {url}: {e.reason}")
        return False
    except Exception as e:
        log.error(f"Error inesperado al descargar {url}: {e}")
        return False


# ──────────────────────────────────────────────
# Cola de reintentos
# ──────────────────────────────────────────────
def cargar_cola() -> list:
    if COLA_REINTENTOS.exists():
        with open(COLA_REINTENTOS) as f:
            return json.load(f)
    return []


def guardar_cola(cola: list):
    with open(COLA_REINTENTOS, "w") as f:
        json.dump(cola, f, ensure_ascii=False, indent=2)


def agregar_a_cola(ley_id: str, url: str, motivo: str):
    cola = cargar_cola()
    existente = next((x for x in cola if x["id"] == ley_id), None)
    if existente:
        existente["intentos"] = existente.get("intentos", 0) + 1
        existente["motivo"] = motivo
        existente["proximo_reintento"] = (
            datetime.now().replace(hour=8, minute=0, second=0).isoformat()
        )
    else:
        cola.append(
            {
                "id": ley_id,
                "url": url,
                "intentos": 1,
                "proximo_reintento": (
                    datetime.now().replace(hour=8, minute=0, second=0).isoformat()
                ),
                "motivo": motivo,
            }
        )
    guardar_cola(cola)


def remover_de_cola(ley_id: str):
    cola = cargar_cola()
    cola = [x for x in cola if x["id"] != ley_id]
    guardar_cola(cola)


def verificar_alerta_manual(cola: list):
    criticos = [x for x in cola if x.get("intentos", 0) >= 3]
    if criticos:
        log.warning(
            f"ALERTA MANUAL REQUERIDA: {len(criticos)} ley(es) con 3+ fallos consecutivos:"
        )
        for item in criticos:
            log.warning(f"  - {item['id']}: {item['motivo']}")


# ──────────────────────────────────────────────
# Procesamiento de una ley
# ──────────────────────────────────────────────
def procesar_ley(ley: dict, indice: list) -> dict:
    ley_id = ley["id"]
    url = ley["url"]
    hash_previo = ley.get("ultimo_hash")
    ruta = ruta_ley(ley)
    tmp = ruta.with_suffix(".tmp")

    log.info(f"Procesando: {ley_id} — {ley['nombre']}")

    ok = descargar_archivo(url, tmp)
    if not ok:
        motivo = "Fallo de descarga"
        ley["estado"] = "fallido"
        log_fallida(f"{ley_id} | {url} | {motivo}")
        agregar_a_cola(ley_id, url, motivo)
        if tmp.exists():
            tmp.unlink()
        return ley

    nuevo_hash = calcular_hash_md5(tmp)

    if hash_previo and nuevo_hash == hash_previo:
        log.info(f"  Sin cambios: {ley_id}")
        tmp.unlink()
        ley["estado"] = "ok"
        ley["ultima_descarga"] = datetime.now().date().isoformat()
        log_exitosa(f"{ley_id} | sin cambios")
        remover_de_cola(ley_id)
        return ley

    # Hay cambio (o primera descarga)
    if ruta.exists() and hash_previo:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        changelog_dir = ruta_changelog(ley)
        changelog_dir.mkdir(parents=True, exist_ok=True)
        archivo_historico = changelog_dir / f"{ley_id}_{ts}.{ley.get('formato', 'pdf')}"
        shutil.copy2(ruta, archivo_historico)
        log.info(f"  Versión anterior guardada: {archivo_historico.name}")

    shutil.move(str(tmp), str(ruta))
    ley["ultimo_hash"] = nuevo_hash
    ley["ultima_descarga"] = datetime.now().date().isoformat()
    ley["estado"] = "ok"

    accion = "Primera descarga" if not hash_previo else "ACTUALIZACIÓN DETECTADA"
    log.info(f"  {accion}: {ley_id} (hash: {nuevo_hash[:8]}...)")
    log_exitosa(f"{ley_id} | {accion} | hash: {nuevo_hash}")
    remover_de_cola(ley_id)
    return ley


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def cargar_indice() -> list:
    with open(INDEX_FILE) as f:
        return json.load(f)


def guardar_indice(indice: list):
    with open(INDEX_FILE, "w") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Descarga y verifica cambios en leyes del índice LegismEx"
    )
    parser.add_argument("--id", help="ID de una ley específica a procesar")
    parser.add_argument("--entidad", help="Entidad federativa a procesar (ej: cdmx, federal)")
    args = parser.parse_args()

    indice = cargar_indice()

    if args.id:
        leyes = [ley for ley in indice if ley["id"] == args.id]
        if not leyes:
            log.error(f"ID no encontrado en el índice: {args.id}")
            sys.exit(1)
    elif args.entidad:
        leyes = [ley for ley in indice if ley["entidad"] == args.entidad]
        if not leyes:
            log.error(f"No hay leyes indexadas para la entidad: {args.entidad}")
            sys.exit(1)
    else:
        leyes = indice

    log.info(f"Iniciando descarga de {len(leyes)} ley(es)...")

    actualizadas = 0
    fallidas = 0

    for i, ley in enumerate(indice):
        if ley in leyes:
            resultado = procesar_ley(ley, indice)
            indice[i] = resultado
            if resultado["estado"] == "ok":
                actualizadas += 1
            else:
                fallidas += 1

    guardar_indice(indice)

    cola = cargar_cola()
    verificar_alerta_manual(cola)

    log.info(
        f"Descarga completada: {actualizadas} exitosas, {fallidas} fallidas. "
        f"Cola de reintentos: {len(cola)} elemento(s)."
    )


if __name__ == "__main__":
    main()
