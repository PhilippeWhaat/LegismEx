#!/usr/bin/env python3
"""
Script de gestión de fallos y reintentos.
Fase 5 del Sistema de Vigilancia Legislativa Mexicana (LegismEx).

Procesa la cola_reintentos.json y reintenta las descargas fallidas
cuyo proximo_reintento sea <= ahora. Si una ley acumula 3 fallos,
emite una alerta manual y la elimina de la cola automática.

Uso:
    python3 reintentos.py          # Procesa reintentos pendientes
    python3 reintentos.py --lista  # Muestra el estado de la cola
"""

import json
import logging
import shutil
import sys
import argparse
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ──────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "leyes_index.json"
COLA_FILE = BASE_DIR / "cola_reintentos.json"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

alertas_log = LOGS_DIR / "alertas.log"
fallidas_log = LOGS_DIR / "descargas_fallidas.log"
exitosas_log = LOGS_DIR / "descargas_exitosas.log"

MAX_INTENTOS = 3
INTERVALO_REINTENTO_HORAS = 24

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


def log_alerta(msg: str):
    with open(alertas_log, "a") as f:
        f.write(f"{datetime.now().isoformat()} | REINTENTO FALLIDO | {msg}\n")
    log.warning(f"ALERTA: {msg}")


def log_exitosa(msg: str):
    with open(exitosas_log, "a") as f:
        f.write(f"{datetime.now().isoformat()} | REINTENTO OK | {msg}\n")


def log_fallida(msg: str):
    with open(fallidas_log, "a") as f:
        f.write(f"{datetime.now().isoformat()} | REINTENTO FALLIDO | {msg}\n")


# ──────────────────────────────────────────────
# Cola de reintentos
# ──────────────────────────────────────────────
def cargar_cola() -> list:
    if not COLA_FILE.exists():
        return []
    with open(COLA_FILE) as f:
        return json.load(f)


def guardar_cola(cola: list):
    with open(COLA_FILE, "w") as f:
        json.dump(cola, f, ensure_ascii=False, indent=2)


def cargar_indice() -> list:
    if not INDEX_FILE.exists():
        return []
    with open(INDEX_FILE) as f:
        return json.load(f)


def guardar_indice(indice: list):
    with open(INDEX_FILE, "w") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# Descarga
# ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LegismEx/1.0; "
        "Sistema de Vigilancia Legislativa)"
    )
}


def descargar_archivo(url: str, destino: Path) -> bool:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(destino, "wb") as f:
                shutil.copyfileobj(resp, f)
        return True
    except urllib.error.HTTPError as e:
        log.error(f"HTTP {e.code} al reintentar {url}")
        return False
    except Exception as e:
        log.error(f"Error al reintentar {url}: {e}")
        return False


def calcular_hash_md5(ruta: Path) -> str:
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def directorio_ley(ley: dict) -> Path:
    entidad = ley["entidad"]
    if entidad == "federal":
        return BASE_DIR / "federal" / "leyes"
    return BASE_DIR / "estados" / entidad / "leyes"


# ──────────────────────────────────────────────
# Procesamiento de un reintento
# ──────────────────────────────────────────────
def procesar_reintento(item: dict, indice: list) -> tuple[bool, str]:
    """
    Intenta descargar la URL del item.
    Retorna (éxito, motivo_fallo).
    """
    ley_id = item["id"]
    url = item["url"]

    # Buscar la entrada en el índice para conocer formato y entidad
    ley = next((l for l in indice if l["id"] == ley_id), None)
    if not ley:
        return False, f"ID {ley_id} no encontrado en el índice"

    ext = ley.get("formato", "pdf")
    destino = directorio_ley(ley) / f"{ley_id}.{ext}"
    tmp = destino.with_suffix(".tmp")

    ok = descargar_archivo(url, tmp)
    if not ok:
        if tmp.exists():
            tmp.unlink()
        return False, "Fallo de red o HTTP"

    nuevo_hash = calcular_hash_md5(tmp)
    hash_previo = ley.get("ultimo_hash")

    if hash_previo and nuevo_hash == hash_previo:
        log.info(f"  Reintento sin cambios: {ley_id}")
        tmp.unlink()
    else:
        if destino.exists() and hash_previo:
            changelog = destino.parent.parent / "changelog"
            changelog.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(destino, changelog / f"{ley_id}_{ts}.{ext}")

        shutil.move(str(tmp), str(destino))
        ley["ultimo_hash"] = nuevo_hash
        log.info(f"  Reintento exitoso con actualización: {ley_id}")

    ley["ultima_descarga"] = datetime.now().date().isoformat()
    ley["estado"] = "ok"
    return True, ""


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def listar_cola(cola: list):
    if not cola:
        print("Cola de reintentos vacía.")
        return
    print(f"\n{'ID':<35} {'Intentos':>8} {'Próximo reintento':<22} {'Motivo'}")
    print("-" * 100)
    for item in cola:
        print(
            f"{item['id']:<35} {item.get('intentos', 1):>8} "
            f"{item.get('proximo_reintento', 'N/A'):<22} {item.get('motivo', '')}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Gestión de reintentos de descargas fallidas (LegismEx)"
    )
    parser.add_argument(
        "--lista",
        action="store_true",
        help="Mostrar el estado actual de la cola de reintentos",
    )
    args = parser.parse_args()

    cola = cargar_cola()

    if args.lista:
        listar_cola(cola)
        return

    if not cola:
        log.info("Cola de reintentos vacía. No hay nada que procesar.")
        return

    ahora = datetime.now()
    pendientes = [
        item for item in cola
        if datetime.fromisoformat(item.get("proximo_reintento", ahora.isoformat())) <= ahora
    ]

    log.info(f"Cola total: {len(cola)} | Pendientes ahora: {len(pendientes)}")

    if not pendientes:
        log.info("No hay reintentos pendientes en este momento.")
        return

    indice = cargar_indice()
    cola_actualizada = [i for i in cola if i not in pendientes]

    for item in pendientes:
        ley_id = item["id"]
        intentos = item.get("intentos", 1)
        log.info(f"Reintentando [{intentos}/{MAX_INTENTOS}]: {ley_id}")

        exito, motivo = procesar_reintento(item, indice)

        if exito:
            log_exitosa(f"{ley_id} | Reintento {intentos} exitoso")
            # No volver a agregar a la cola
        else:
            log_fallida(f"{ley_id} | Intento {intentos} | {motivo}")
            nuevo_intento = intentos + 1

            if nuevo_intento > MAX_INTENTOS:
                log_alerta(
                    f"{ley_id} superó {MAX_INTENTOS} fallos consecutivos. "
                    f"Se requiere intervención manual. URL: {item['url']}"
                )
                # Marcar en índice como crítico
                ley = next((l for l in indice if l["id"] == ley_id), None)
                if ley:
                    ley["estado"] = "critico"
            else:
                proximo = (
                    ahora + timedelta(hours=INTERVALO_REINTENTO_HORAS)
                ).replace(minute=0, second=0, microsecond=0)
                item["intentos"] = nuevo_intento
                item["motivo"] = motivo
                item["proximo_reintento"] = proximo.isoformat()
                cola_actualizada.append(item)

    guardar_cola(cola_actualizada)
    guardar_indice(indice)

    log.info(
        f"Reintentos procesados. Cola restante: {len(cola_actualizada)} elemento(s)."
    )


if __name__ == "__main__":
    main()
