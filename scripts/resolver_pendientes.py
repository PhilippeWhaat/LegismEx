#!/usr/bin/env python3
"""
Resolución inteligente de leyes pendientes de actualización.
Fase 8A+ del Sistema de Vigilancia Legislativa Mexicana.

Cuando el LLM detecta una reforma en el DOF, marca la ley como
pendiente_actualizacion. descarga.py reintenta desde la URL original.
Si después de 7 días el hash no ha cambiado (el congreso no actualizó
el PDF), este script escala con estrategias más sofisticadas:

  Nivel 1 (días 1-7):  descarga.py reintenta URL del catálogo
  Nivel 2 (día 8+):    Este script:
    a) Re-scrape del portal del congreso buscando URL nueva
    b) Extraer texto de la nota del DOF (nota_detalle)
    c) Web search: "{nombre ley} texto vigente pdf"
    d) Si nada funciona: mantener como pendiente, nunca descartar

Uso:
    python3 resolver_pendientes.py              # Procesar pendientes >7 días
    python3 resolver_pendientes.py --todos      # Procesar todos los pendientes
    python3 resolver_pendientes.py --dry-run    # Solo reportar, no actuar
"""

import argparse
import json
import logging
import os
import re
import ssl
import sys
import urllib.request
import urllib.error
import hashlib
import shutil
from datetime import datetime, date
from pathlib import Path
from html.parser import HTMLParser

try:
    import anthropic
except ImportError:
    anthropic = None

# ──────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "leyes_index.json"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

RESOLVER_LOG = LOGS_DIR / "resolver_pendientes.json"

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
}

DIAS_ANTES_DE_ESCALAR = 7

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "resolver.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────
def fetch_html(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.debug(f"Fetch falló: {url} — {e}")
        return None


def descargar_archivo(url: str, destino: Path) -> bool:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            with open(destino, "wb") as f:
                shutil.copyfileobj(resp, f)
        if destino.stat().st_size < 500:
            destino.unlink()
            return False
        return True
    except Exception:
        if destino.exists():
            destino.unlink()
        return False


def calcular_hash(ruta: Path) -> str:
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def directorio_ley(ley: dict) -> Path:
    if ley["entidad"] == "federal":
        return BASE_DIR / "federal" / "leyes"
    return BASE_DIR / "estados" / ley["entidad"] / "leyes"


class LinkExtractor(HTMLParser):
    """Extrae todos los links con su texto de una página HTML."""
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href", "")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            texto = " ".join(t for t in self._text if t)
            if texto and self._href:
                self.links.append({"texto": texto, "url": self._href})
            self._href = None


# ──────────────────────────────────────────────
# Estrategia A: Re-scrape del portal del congreso
# ──────────────────────────────────────────────
def estrategia_rescrape_portal(ley: dict) -> str | None:
    """
    Visita la fuente original del catálogo y busca un link
    actualizado para la ley.
    """
    url_actual = ley.get("url", "")
    fuente = ley.get("fuente", "")
    nombre = ley.get("nombre", "")

    if not url_actual:
        return None

    # Derivar la página de listado del portal a partir de la URL
    # Ej: https://congresogto.gob.mx/leyes → buscar ahí
    from urllib.parse import urlparse
    parsed = urlparse(url_actual)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # Intentar la página principal del portal legislativo
    catalogo_file = None
    entidad = ley.get("entidad", "")
    if entidad == "federal":
        catalogo_file = BASE_DIR / "federal" / "catalogo.json"
    else:
        catalogo_file = BASE_DIR / "estados" / entidad / "catalogo.json"

    # Verificar si la URL original devuelve algo nuevo
    log.info(f"    [A] Verificando URL original: {url_actual[:80]}")
    tmp = directorio_ley(ley) / f"{ley['id']}.rescrape.tmp"
    if descargar_archivo(url_actual, tmp):
        nuevo_hash = calcular_hash(tmp)
        if nuevo_hash != ley.get("ultimo_hash"):
            log.info(f"    [A] ¡URL original tiene contenido nuevo! hash={nuevo_hash[:8]}")
            tmp.unlink()
            return url_actual
        tmp.unlink()

    # Intentar variaciones de la URL (versión, fecha en nombre)
    variaciones = _generar_variaciones_url(url_actual)
    for var_url in variaciones:
        log.debug(f"    [A] Probando variación: {var_url[:80]}")
        if descargar_archivo(var_url, tmp):
            nuevo_hash = calcular_hash(tmp)
            if nuevo_hash != ley.get("ultimo_hash"):
                log.info(f"    [A] ¡Variación encontrada! {var_url[:80]}")
                tmp.unlink()
                return var_url
            tmp.unlink()

    return None


def _generar_variaciones_url(url: str) -> list[str]:
    """Genera variaciones de una URL que podrían contener la versión actualizada."""
    variaciones = []

    # Reemplazar año en la URL
    year = str(date.today().year)
    prev_year = str(date.today().year - 1)
    if prev_year in url:
        variaciones.append(url.replace(prev_year, year))

    # Cambiar extensión (pdf → doc, doc → pdf)
    if url.endswith(".pdf"):
        variaciones.append(url.rsplit(".pdf", 1)[0] + ".doc")
        variaciones.append(url.rsplit(".pdf", 1)[0] + ".docx")
    elif url.endswith(".doc"):
        variaciones.append(url.rsplit(".doc", 1)[0] + ".pdf")

    return variaciones


# ──────────────────────────────────────────────
# Estrategia B: Extraer contenido del DOF
# ──────────────────────────────────────────────
def estrategia_extraer_dof(ley: dict) -> str | None:
    """
    Si tenemos la URL de la nota del DOF, intenta extraer el
    texto completo de la publicación directamente.
    """
    url_dof = ley.get("_url_dof", "")
    if not url_dof:
        return None

    log.info(f"    [B] Intentando extraer de DOF: {url_dof[:80]}")

    html = fetch_html(url_dof)
    if not html:
        return None

    # Las notas del DOF tienen el texto dentro de un div específico
    # Guardar como HTML para referencia
    entidad = ley.get("entidad", "federal")
    if entidad == "federal":
        dest_dir = BASE_DIR / "federal" / "diario_oficial"
    else:
        dest_dir = BASE_DIR / "estados" / entidad / "periodico_oficial"

    dest_dir.mkdir(parents=True, exist_ok=True)
    nombre_seguro = re.sub(r"[^\w-]", "_", ley["id"])[:80]
    ts = datetime.now().strftime("%Y%m%d")
    dest_file = dest_dir / f"{ts}_dof_{nombre_seguro}.html"

    with open(dest_file, "w", encoding="utf-8") as f:
        f.write(html)

    log.info(f"    [B] Contenido DOF guardado: {dest_file.name}")
    return f"dof:{dest_file}"


# ──────────────────────────────────────────────
# Estrategia C: Búsqueda inteligente con LLM
# ──────────────────────────────────────────────
def estrategia_busqueda_llm(ley: dict, client) -> str | None:
    """
    Usa el LLM para analizar la página del portal legislativo
    y encontrar la URL correcta del documento actualizado.
    """
    if not client:
        return None

    url_actual = ley.get("url", "")
    nombre = ley.get("nombre", "")
    entidad = ley.get("entidad", "")
    fuente = ley.get("fuente", "")

    # Intentar cargar la página del portal y pedirle al LLM que encuentre la URL
    from urllib.parse import urlparse
    parsed = urlparse(url_actual)
    portal_url = f"{parsed.scheme}://{parsed.netloc}"

    # Buscar en la fuente del catálogo
    fuentes_md = None
    if entidad == "federal":
        fuentes_path = BASE_DIR / "federal" / "fuentes.md"
    else:
        fuentes_path = BASE_DIR / "estados" / entidad / "fuentes.md"

    if fuentes_path.exists():
        fuentes_md = fuentes_path.read_text()[:2000]

    # Construir un prompt para que el LLM sugiera dónde buscar
    prompt = f"""Necesito encontrar la versión actualizada de esta ley mexicana:

Nombre: {nombre}
Entidad: {entidad}
URL anterior: {url_actual}
Fuente original: {fuente}

La URL anterior ya no tiene la versión actualizada (el hash no ha cambiado tras una reforma publicada en el DOF).

{f"Información del portal:{chr(10)}{fuentes_md}" if fuentes_md else ""}

Sugiere hasta 5 URLs alternativas donde podría estar el PDF actualizado.
Responde SOLO con un JSON array de strings con las URLs, sin texto adicional.
Ejemplo: ["https://...", "https://..."]"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        texto = response.content[0].text.strip()
        if texto.startswith("```"):
            texto = "\n".join(texto.split("\n")[1:-1])

        urls_sugeridas = json.loads(texto)
        if not isinstance(urls_sugeridas, list):
            return None

        log.info(f"    [C] LLM sugirió {len(urls_sugeridas)} URLs alternativas")

        tmp = directorio_ley(ley) / f"{ley['id']}.llmsearch.tmp"
        for url_alt in urls_sugeridas:
            if not isinstance(url_alt, str) or not url_alt.startswith("http"):
                continue
            log.info(f"    [C] Probando: {url_alt[:80]}")
            if descargar_archivo(url_alt, tmp):
                nuevo_hash = calcular_hash(tmp)
                if nuevo_hash != ley.get("ultimo_hash"):
                    log.info(f"    [C] ¡URL alternativa funciona! hash={nuevo_hash[:8]}")
                    tmp.unlink()
                    return url_alt
                tmp.unlink()

    except Exception as e:
        log.debug(f"    [C] Error en búsqueda LLM: {e}")

    return None


# ──────────────────────────────────────────────
# Aplicar actualización exitosa
# ──────────────────────────────────────────────
def aplicar_actualizacion(ley: dict, nueva_url: str, indice: list) -> bool:
    """Descarga la ley desde la nueva URL y actualiza el índice."""
    if nueva_url.startswith("dof:"):
        # El contenido se guardó como HTML del DOF, no es un PDF descargable
        ley["_contenido_dof"] = nueva_url.replace("dof:", "")
        ley["_ultimo_intento_resolver"] = datetime.now().isoformat()
        log.info(f"    Contenido DOF guardado como referencia (no reemplaza el PDF)")
        return True

    ext = ley.get("formato", "pdf")
    destino = directorio_ley(ley) / f"{ley['id']}.{ext}"
    tmp = destino.with_suffix(".resolver.tmp")

    if not descargar_archivo(nueva_url, tmp):
        return False

    nuevo_hash = calcular_hash(tmp)

    # Versionar archivo anterior si existe
    if destino.exists() and ley.get("ultimo_hash"):
        changelog = destino.parent.parent / "changelog"
        changelog.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(destino, changelog / f"{ley['id']}_{ts}.{ext}")

    shutil.move(str(tmp), str(destino))

    # Actualizar índice
    ley["ultimo_hash"] = nuevo_hash
    ley["ultima_descarga"] = date.today().isoformat()
    ley["estado"] = "ok"
    if nueva_url != ley.get("url"):
        ley["_url_anterior"] = ley.get("url")
        ley["url"] = nueva_url
    # Limpiar campos temporales
    for k in ["_motivo_actualizacion", "_fecha_deteccion", "_intentos_actualizacion",
              "_url_dof", "_tipo_acto", "_ley_afectada", "_ultimo_intento_resolver"]:
        ley.pop(k, None)

    log.info(f"    ✓ Actualizada: {ley['id']} (hash={nuevo_hash[:8]})")
    return True


# ──────────────────────────────────────────────
# Asegurar referencia al DOF/diario como evidencia
# ──────────────────────────────────────────────
def _asegurar_referencia_dof(ley: dict):
    """
    Siempre guarda la referencia a la publicación del DOF/diario que
    originó esta actualización. Esto permite revisión manual: ver qué
    se detectó, verificar si fue falso positivo, o buscar manualmente.
    """
    url_dof = ley.get("_url_dof", "")
    if not url_dof:
        return

    # Guardar en un registro centralizado de evidencias
    evidencias_file = LOGS_DIR / "evidencias_reformas.json"
    evidencias = []
    if evidencias_file.exists():
        with open(evidencias_file) as f:
            evidencias = json.load(f)

    # Evitar duplicados
    ya_existe = any(e.get("id") == ley["id"] and e.get("url_dof") == url_dof for e in evidencias)
    if ya_existe:
        return

    evidencias.append({
        "id": ley["id"],
        "nombre": ley.get("nombre", ""),
        "entidad": ley.get("entidad", ""),
        "url_catalogo": ley.get("url", ""),
        "url_dof": url_dof,
        "tipo_acto": ley.get("_tipo_acto", ""),
        "motivo": ley.get("_motivo_actualizacion", ""),
        "fecha_deteccion": ley.get("_fecha_deteccion", ""),
        "dias_pendiente": (datetime.now() - datetime.fromisoformat(
            ley.get("_fecha_deteccion", datetime.now().isoformat())
        )).days if ley.get("_fecha_deteccion") else 0,
        "intentos": ley.get("_intentos_actualizacion", 0),
        "resuelto": ley.get("estado") == "ok",
    })

    with open(evidencias_file, "w") as f:
        json.dump(evidencias, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# Guardar reporte de resolución
# ──────────────────────────────────────────────
def guardar_reporte(resultados: list[dict]):
    # JSON estructurado para consumo programático
    existentes = []
    if RESOLVER_LOG.exists():
        with open(RESOLVER_LOG) as f:
            existentes = json.load(f)
    existentes.extend(resultados)
    with open(RESOLVER_LOG, "w") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)

    # Reporte legible para revisión manual
    pendientes = [r for r in resultados if not r.get("resuelto")]
    if pendientes:
        revision_file = LOGS_DIR / "revision_manual_pendientes.md"
        with open(revision_file, "w") as f:
            f.write(f"# Leyes pendientes de actualización\n")
            f.write(f"Generado: {datetime.now().isoformat()}\n\n")
            f.write(f"Estas leyes fueron detectadas como reformadas en el DOF/diario oficial\n")
            f.write(f"pero el PDF actualizado aún no está disponible en el portal del congreso.\n\n")
            f.write(f"| ID | Nombre | Días | Intentos | URL DOF | URL Catálogo |\n")
            f.write(f"|---|---|---|---|---|---|\n")
            for r in sorted(pendientes, key=lambda x: -x.get("dias", 0)):
                f.write(
                    f"| {r['id']} | {r['nombre'][:50]} | {r.get('dias', '?')} | "
                    f"{r.get('intentos', '?')} | "
                    f"[DOF]({r.get('url_dof', '')}) | "
                    f"[Catálogo]({r.get('url_catalogo', '')}) |\n"
                )
            f.write(f"\n**Posibles causas:**\n")
            f.write(f"- El congreso aún no actualiza el PDF (normal, pueden tardar semanas)\n")
            f.write(f"- Falso positivo del LLM (la publicación no afecta realmente esta ley)\n")
            f.write(f"- La URL del catálogo cambió y necesita actualizarse\n")
        log.info(f"  Reporte de revisión: {revision_file.name}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Resolución inteligente de leyes pendientes de actualización"
    )
    parser.add_argument("--todos", action="store_true",
                        help="Procesar todos los pendientes (no solo >7 días)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo reportar, no ejecutar acciones")
    args = parser.parse_args()

    # Cargar .env
    from dotenv import load_dotenv
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # Cliente LLM (opcional pero preferible)
    client = None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key and anthropic:
        client = anthropic.Anthropic(api_key=api_key)
        log.info("Cliente LLM disponible para estrategia C")
    else:
        log.info("Sin cliente LLM — estrategia C no disponible")

    # Cargar índice
    if not INDEX_FILE.exists():
        log.error("leyes_index.json no existe")
        sys.exit(1)

    with open(INDEX_FILE) as f:
        indice = json.load(f)

    # Filtrar leyes pendientes de actualización
    ahora = datetime.now()
    pendientes = []
    for ley in indice:
        if ley.get("estado") != "pendiente_actualizacion":
            continue

        fecha_det = ley.get("_fecha_deteccion", "")
        if fecha_det:
            try:
                dias = (ahora - datetime.fromisoformat(fecha_det)).days
            except ValueError:
                dias = 0
        else:
            dias = 0

        if args.todos or dias >= DIAS_ANTES_DE_ESCALAR:
            pendientes.append((ley, dias))

    if not pendientes:
        log.info("No hay leyes pendientes que requieran resolución inteligente.")
        return

    log.info(f"=== Resolución inteligente: {len(pendientes)} ley(es) pendiente(s) ===")
    reportes = []

    for ley, dias in pendientes:
        ley_id = ley["id"]
        nombre = ley.get("nombre", "?")
        intentos = ley.get("_intentos_actualizacion", 0) + 1
        ley["_intentos_actualizacion"] = intentos
        ley["_ultimo_intento_resolver"] = ahora.isoformat()

        log.info(f"\n[{ley_id}] {nombre[:60]}")
        log.info(f"  Días pendiente: {dias} | Intento resolución: {intentos}")

        if args.dry_run:
            reportes.append({
                "id": ley_id, "nombre": nombre, "dias": dias,
                "intentos": intentos, "accion": "dry-run"
            })
            continue

        nueva_url = None

        # Estrategia A: Re-scrape del portal
        log.info(f"  Estrategia A: Re-scrape del portal...")
        nueva_url = estrategia_rescrape_portal(ley)

        # Estrategia B: Extraer del DOF
        if not nueva_url:
            log.info(f"  Estrategia B: Extraer contenido del DOF...")
            nueva_url = estrategia_extraer_dof(ley)

        # Estrategia C: Búsqueda con LLM
        if not nueva_url and client:
            log.info(f"  Estrategia C: Búsqueda inteligente con LLM...")
            nueva_url = estrategia_busqueda_llm(ley, client)

        # Siempre guardar referencia al DOF/diario como evidencia para revisión
        _asegurar_referencia_dof(ley)

        # Resultado
        if nueva_url:
            ok = aplicar_actualizacion(ley, nueva_url, indice)
            estrategia = "B-dof" if str(nueva_url).startswith("dof:") else \
                         "C-llm" if client and not estrategia_rescrape_portal(ley) else "A-rescrape"
            reportes.append({
                "id": ley_id, "nombre": nombre, "dias": dias,
                "intentos": intentos, "resuelto": ok,
                "estrategia": estrategia,
                "url_nueva": nueva_url,
                "url_catalogo": ley.get("url", ""),
                "url_dof": ley.get("_url_dof", ""),
                "motivo_original": ley.get("_motivo_actualizacion", ""),
                "fecha": ahora.isoformat(),
            })
        else:
            # "Aún no disponible" no es un error — es normal que los congresos
            # tarden semanas en actualizar. Se reintenta indefinidamente.
            log.info(
                f"  → Aún no disponible — se reintentará mañana "
                f"(día {dias}, intento {intentos})"
            )
            reportes.append({
                "id": ley_id, "nombre": nombre, "dias": dias,
                "intentos": intentos, "resuelto": False,
                "url_catalogo": ley.get("url", ""),
                "url_dof": ley.get("_url_dof", ""),
                "motivo_original": ley.get("_motivo_actualizacion", ""),
                "fecha": ahora.isoformat(),
            })

    # Guardar índice y reportes
    if not args.dry_run:
        with open(INDEX_FILE, "w") as f:
            json.dump(indice, f, ensure_ascii=False, indent=2)

    guardar_reporte(reportes)

    resueltos = sum(1 for r in reportes if r.get("resuelto"))
    log.info(f"\n=== Resolución: {resueltos}/{len(pendientes)} resueltas ===")


if __name__ == "__main__":
    main()
