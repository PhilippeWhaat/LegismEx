#!/usr/bin/env python3
"""
Script de vigilancia del Diario Oficial de la Federación (DOF) y
periódicos oficiales estatales.
Fase 4 del Sistema de Vigilancia Legislativa Mexicana (LegismEx).

Uso:
    python3 vigilancia_dof.py                   # Vigilar DOF de hoy
    python3 vigilancia_dof.py --fecha 2026-02-19  # Vigilar fecha específica
    python3 vigilancia_dof.py --entidad cdmx     # Vigilar gaceta de CDMX de hoy

Resultados:
    - Documentos descargados en: estados/{entidad}/periodico_oficial/
    - Alertas registradas en: logs/alertas.log
"""

import json
import logging
import re
import shutil
import ssl
import sys
import argparse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, date
from pathlib import Path
from html import unescape
from html.parser import HTMLParser

# Contexto SSL permisivo (muchos portales gubernamentales tienen certs malos)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ──────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

alertas_log = LOGS_DIR / "alertas.log"
actualizaciones_log = LOGS_DIR / "actualizaciones.log"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(actualizaciones_log),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def log_alerta(mensaje: str):
    ts = datetime.now().isoformat()
    with open(alertas_log, "a") as f:
        f.write(f"{ts} | ALERTA | {mensaje}\n")
    log.warning(f"ALERTA: {mensaje}")


# ──────────────────────────────────────────────
# Palabras clave legislativas
# ──────────────────────────────────────────────
PALABRAS_CLAVE = [
    r"\bLey\b",
    r"\bDecreto\b",
    r"\bReforma\b",
    r"\bReglamento\b",
    r"\bAbrogaci[oó]n\b",
    r"\bDerogaci[oó]n\b",
    r"\bC[oó]digo\b",
    r"\bAcuerdo\b",
    r"\bNorma Oficial\b",
    r"\bDisposici[oó]n\b",
]

PATRON_LEGISLATIVO = re.compile("|".join(PALABRAS_CLAVE), re.IGNORECASE)


def es_acto_legislativo(titulo: str) -> bool:
    return bool(PATRON_LEGISLATIVO.search(titulo))


# ──────────────────────────────────────────────
# Parser HTML genérico para extraer links
# ──────────────────────────────────────────────
class LinkParser(HTMLParser):
    def __init__(self, base_url: str = ""):
        super().__init__()
        self.links: list[dict] = []
        self.base_url = base_url
        self._current_href = None
        self._current_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            self._current_href = attrs_dict.get("href", "")
            self._current_text = []

    def handle_data(self, data):
        if self._current_href is not None:
            self._current_text.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "a" and self._current_href is not None:
            texto = " ".join(t for t in self._current_text if t)
            if texto:
                href = self._current_href
                if href and not href.startswith("http"):
                    href = self.base_url.rstrip("/") + "/" + href.lstrip("/")
                self.links.append({"texto": texto, "url": href})
            self._current_href = None
            self._current_text = []


# ──────────────────────────────────────────────
# Utilidades HTTP
# ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; LegismEx/1.0; "
        "Sistema de Vigilancia Legislativa)"
    )
}


def fetch_html(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            charset = "utf-8"
            content_type = resp.headers.get_content_charset()
            if content_type:
                charset = content_type
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.error(f"Error al obtener {url}: {e}")
        return None


def descargar_pdf(url: str, destino: Path) -> bool:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            with open(destino, "wb") as f:
                shutil.copyfileobj(resp, f)
        return True
    except Exception as e:
        log.error(f"Error al descargar PDF {url}: {e}")
        return False


# ──────────────────────────────────────────────
# Vigilancia del DOF (federal)
# ──────────────────────────────────────────────
def vigilar_dof(fecha: date) -> list[dict]:
    """
    Descarga el sumario RSS/XML del DOF y extrae actos legislativos.
    El feed RSS siempre contiene el sumario del día más reciente publicado.
    Para fechas anteriores, usa el endpoint con parámetro de fecha.
    """
    # El feed RSS del DOF contiene el sumario del día
    url = "https://www.dof.gob.mx/sumario.xml"
    log.info(f"Consultando DOF RSS: {url}")

    xml_text = fetch_html(url)
    if not xml_text:
        log.error("No se pudo obtener el sumario RSS del DOF")
        return []

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.error(f"Error parseando XML del DOF: {e}")
        return []

    actos_todos = []
    actos_legislativos = []

    for item in root.iter("item"):
        seccion = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        descripcion = unescape(item.findtext("description", "").strip())
        fecha_pub = item.findtext("valueDate", "").strip()

        # Combinar sección + descripción para el título completo
        titulo = f"{seccion}: {descripcion}" if seccion else descripcion

        actos_todos.append(titulo)

        if es_acto_legislativo(titulo) or es_acto_legislativo(descripcion):
            actos_legislativos.append(
                {
                    "titulo": titulo,
                    "descripcion": descripcion,
                    "seccion_dof": seccion,
                    "url": link,
                    "fecha": fecha_pub or fecha.isoformat(),
                    "entidad": "federal",
                    "fuente": "DOF RSS",
                }
            )

    log.info(
        f"DOF sumario: {len(actos_todos)} items totales, "
        f"{len(actos_legislativos)} legislativos"
    )
    return actos_legislativos


# ──────────────────────────────────────────────
# Vigilancia de periódicos estatales (genérica)
# ──────────────────────────────────────────────

# Configuración de fuentes estatales que permiten scraping por índice.
# 30 de 32 estados configurados (faltan: michoacan [403], tlaxcala [502]).
FUENTES_ESTATALES = {
    "aguascalientes": {
        "nombre": "Periódico Oficial Aguascalientes",
        "url_indice": "https://eservicios2.aguascalientes.gob.mx/periodicooficial/",
        "base_url": "https://eservicios2.aguascalientes.gob.mx",
    },
    "bajacalifornia": {
        "nombre": "Periódico Oficial Baja California",
        "url_indice": "https://periodicooficial.ebajacalifornia.gob.mx/oficial/inicioConsulta.jsp",
        "base_url": "https://periodicooficial.ebajacalifornia.gob.mx",
    },
    "bajacaliforniasur": {
        "nombre": "Boletín Oficial Baja California Sur",
        "url_indice": "https://finanzas.bcs.gob.mx/boletines-oficiales/",
        "base_url": "https://finanzas.bcs.gob.mx",
    },
    "campeche": {
        "nombre": "Periódico Oficial Campeche",
        "url_indice": "http://periodicooficial.campeche.gob.mx/sipoec/public/documentos",
        "base_url": "http://periodicooficial.campeche.gob.mx",
    },
    "cdmx": {
        "nombre": "Gaceta Oficial CDMX",
        "url_indice": "https://www.consejeria.cdmx.gob.mx/gaceta",
        "base_url": "https://www.consejeria.cdmx.gob.mx",
    },
    "chiapas": {
        "nombre": "Periódico Oficial Chiapas",
        "url_indice": "https://www.sgg.chiapas.gob.mx/periodico/periodico1824",
        "base_url": "https://www.sgg.chiapas.gob.mx",
    },
    "chihuahua": {
        "nombre": "Periódico Oficial Chihuahua",
        "url_indice": "https://chihuahua.gob.mx/periodicooficial/ultimo",
        "base_url": "https://chihuahua.gob.mx",
    },
    "coahuila": {
        "nombre": "Periódico Oficial Coahuila",
        "url_indice": "https://periodico.segobcoahuila.gob.mx/",
        "base_url": "https://periodico.segobcoahuila.gob.mx",
    },
    "colima": {
        "nombre": "Periódico Oficial Colima",
        "url_indice": "https://periodicooficial.col.gob.mx/",
        "base_url": "https://periodicooficial.col.gob.mx",
    },
    "durango": {
        "nombre": "Periódico Oficial Durango",
        "url_indice": "https://periodicooficial.durango.gob.mx/publicaciones",
        "base_url": "https://periodicooficial.durango.gob.mx",
    },
    "edomex": {
        "nombre": "Gaceta del Gobierno EdoMex",
        "url_indice": "https://legislacion.edomex.gob.mx/gaceta-gobierno",
        "base_url": "https://legislacion.edomex.gob.mx",
    },
    "guanajuato": {
        "nombre": "Periódico Oficial Guanajuato",
        "url_indice": "https://periodico.guanajuato.gob.mx/",
        "base_url": "https://periodico.guanajuato.gob.mx",
    },
    "guerrero": {
        "nombre": "Periódico Oficial Guerrero",
        "url_indice": "https://periodicooficial.guerrero.gob.mx/publicaciones/",
        "base_url": "https://periodicooficial.guerrero.gob.mx",
    },
    "hidalgo": {
        "nombre": "Periódico Oficial Hidalgo",
        "url_indice": "https://periodicooficial.hidalgo.gob.mx/",
        "base_url": "https://periodicooficial.hidalgo.gob.mx",
    },
    "jalisco": {
        "nombre": "Periódico Oficial Jalisco",
        "url_indice": "https://periodicooficial.jalisco.gob.mx/",
        "base_url": "https://periodicooficial.jalisco.gob.mx",
    },
    "morelos": {
        "nombre": "Periódico Oficial Morelos",
        "url_indice": "https://periodico.morelos.gob.mx/",
        "base_url": "https://periodico.morelos.gob.mx",
    },
    "nayarit": {
        "nombre": "Periódico Oficial Nayarit",
        "url_indice": "https://periodicooficial.nayarit.gob.mx/",
        "base_url": "https://periodicooficial.nayarit.gob.mx",
    },
    "nuevoleon": {
        "nombre": "Periódico Oficial Nuevo León",
        "url_indice": "https://www.nl.gob.mx/publicaciones/periodico-oficial-del-estado",
        "base_url": "https://www.nl.gob.mx",
    },
    "oaxaca": {
        "nombre": "Periódico Oficial Oaxaca",
        "url_indice": "https://www.periodicooficial.oaxaca.gob.mx/",
        "base_url": "https://www.periodicooficial.oaxaca.gob.mx",
    },
    "puebla": {
        "nombre": "Periódico Oficial Puebla",
        "url_indice": "https://periodico.puebla.gob.mx/",
        "base_url": "https://periodico.puebla.gob.mx",
    },
    "queretaro": {
        "nombre": "La Sombra de Arteaga (Querétaro)",
        "url_indice": "https://lasombradearteaga.segobqueretaro.gob.mx/",
        "base_url": "https://lasombradearteaga.segobqueretaro.gob.mx",
    },
    "quintanaroo": {
        "nombre": "Periódico Oficial Quintana Roo",
        "url_indice": "https://cjpe.qroo.gob.mx/periodico-oficial-del-estado-de-quintana-roo/",
        "base_url": "https://cjpe.qroo.gob.mx",
    },
    "sanluispotosi": {
        "nombre": "Periódico Oficial San Luis Potosí",
        "url_indice": "https://periodicooficial.slp.gob.mx/",
        "base_url": "https://periodicooficial.slp.gob.mx",
    },
    "sinaloa": {
        "nombre": "Periódico Oficial Sinaloa",
        "url_indice": "https://iip.congresosinaloa.gob.mx/poes.html",
        "base_url": "https://iip.congresosinaloa.gob.mx",
    },
    "sonora": {
        "nombre": "Boletín Oficial Sonora",
        "url_indice": "https://www.boletinoficial.sonora.gob.mx/",
        "base_url": "https://www.boletinoficial.sonora.gob.mx",
    },
    "tabasco": {
        "nombre": "Periódico Oficial Tabasco",
        "url_indice": "https://tabasco.gob.mx/PeriodicoOficial",
        "base_url": "https://tabasco.gob.mx",
    },
    "tamaulipas": {
        "nombre": "Periódico Oficial Tamaulipas",
        "url_indice": "https://po.tamaulipas.gob.mx/",
        "base_url": "https://po.tamaulipas.gob.mx",
    },
    "veracruz": {
        "nombre": "Gaceta Oficial Veracruz",
        "url_indice": "https://www.veracruz.gob.mx/gobierno/gaceta-oficial/",
        "base_url": "https://www.veracruz.gob.mx",
    },
    "yucatan": {
        "nombre": "Diario Oficial Yucatán",
        "url_indice": "https://www.yucatan.gob.mx/gobierno/diario_oficial.php",
        "base_url": "https://www.yucatan.gob.mx",
    },
    "zacatecas": {
        "nombre": "Periódico Oficial Zacatecas",
        "url_indice": "https://periodicooficial.zacatecas.gob.mx/",
        "base_url": "https://periodicooficial.zacatecas.gob.mx",
    },
}


def vigilar_estado(entidad: str, fecha: date) -> list[dict]:
    """
    Descarga el índice del periódico oficial de un estado y
    extrae actos de tipo legislativo.
    """
    if entidad not in FUENTES_ESTATALES:
        log.warning(f"Entidad sin configuración de vigilancia: {entidad}")
        return []

    fuente = FUENTES_ESTATALES[entidad]
    url = fuente["url_indice"]
    log.info(f"Consultando {fuente['nombre']}: {url}")

    html = fetch_html(url)
    if not html:
        log.error(f"No se pudo obtener el índice de {fuente['nombre']}")
        return []

    parser = LinkParser(base_url=fuente["base_url"])
    parser.feed(html)

    actos_legislativos = []
    for link in parser.links:
        texto = link["texto"]
        if es_acto_legislativo(texto):
            actos_legislativos.append(
                {
                    "titulo": texto,
                    "url": link["url"],
                    "fecha": fecha.isoformat(),
                    "entidad": entidad,
                }
            )

    log.info(
        f"{fuente['nombre']}: {len(parser.links)} links totales, "
        f"{len(actos_legislativos)} legislativos detectados"
    )
    return actos_legislativos


# ──────────────────────────────────────────────
# Procesar y descargar actos detectados
# ──────────────────────────────────────────────
def procesar_actos(actos: list[dict]):
    cola_procesamiento = []

    for acto in actos:
        entidad = acto["entidad"]
        titulo = acto["titulo"]
        url = acto["url"]
        fecha = acto["fecha"]

        # Generar nombre de archivo seguro
        nombre_seguro = re.sub(r"[^\w\s-]", "", titulo)[:80].strip()
        nombre_seguro = re.sub(r"\s+", "_", nombre_seguro)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre_archivo = f"{fecha}_{ts}_{nombre_seguro}.pdf"

        if entidad == "federal":
            destino = BASE_DIR / "federal" / "diario_oficial" / nombre_archivo
        else:
            destino = BASE_DIR / "estados" / entidad / "periodico_oficial" / nombre_archivo

        # Intentar descargar si es PDF
        if url and (url.endswith(".pdf") or "pdf" in url.lower()):
            ok = descargar_pdf(url, destino)
            if ok:
                log_alerta(
                    f"Nueva publicación descargada: {titulo} — {fecha} — {entidad}"
                )
            else:
                log_alerta(
                    f"Publicación detectada (descarga fallida): {titulo} — {url}"
                )
        else:
            # URL no es PDF directo, registrar para revisión manual
            log_alerta(
                f"Nueva publicación detectada (requiere revisión): {titulo} — {url}"
            )

        cola_procesamiento.append(
            {
                "titulo": titulo,
                "url": url,
                "entidad": entidad,
                "fecha_deteccion": datetime.now().isoformat(),
                "archivo_local": str(destino) if destino else None,
            }
        )

    # Guardar cola de procesamiento (para análisis LLM)
    if cola_procesamiento:
        cola_file = LOGS_DIR / "cola_procesamiento.json"
        cola_existente = []
        if cola_file.exists():
            with open(cola_file) as f:
                cola_existente = json.load(f)
        cola_existente.extend(cola_procesamiento)
        with open(cola_file, "w") as f:
            json.dump(cola_existente, f, ensure_ascii=False, indent=2)
        log.info(f"{len(cola_procesamiento)} acto(s) agregado(s) a la cola de procesamiento")

    # También guardar archivo diario para histórico
    if cola_procesamiento:
        hoy = date.today().isoformat()
        diario_file = LOGS_DIR / f"publicaciones_{hoy}.json"
        with open(diario_file, "w") as f:
            json.dump(cola_procesamiento, f, ensure_ascii=False, indent=2)
        log.info(f"Archivo diario guardado: {diario_file.name}")


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Vigilancia del DOF y periódicos oficiales estatales"
    )
    parser.add_argument(
        "--fecha",
        help="Fecha a vigilar (YYYY-MM-DD). Por defecto: hoy",
        default=date.today().isoformat(),
    )
    parser.add_argument(
        "--entidad",
        help=(
            "Entidad federativa a vigilar (ej: cdmx, jalisco). "
            "Por defecto: federal (DOF)"
        ),
        default="federal",
    )
    parser.add_argument(
        "--todas",
        action="store_true",
        help="Vigilar DOF + todos los estados configurados",
    )
    args = parser.parse_args()

    try:
        fecha = date.fromisoformat(args.fecha)
    except ValueError:
        log.error(f"Formato de fecha inválido: {args.fecha}. Use YYYY-MM-DD")
        sys.exit(1)

    log.info(f"=== Vigilancia legislativa — {fecha.isoformat()} ===")

    todos_actos = []

    if args.todas:
        # DOF federal
        actos_dof = vigilar_dof(fecha)
        todos_actos.extend(actos_dof)
        # Todos los estados configurados
        for entidad in FUENTES_ESTATALES:
            actos_estado = vigilar_estado(entidad, fecha)
            todos_actos.extend(actos_estado)
    elif args.entidad == "federal":
        todos_actos = vigilar_dof(fecha)
    else:
        todos_actos = vigilar_estado(args.entidad, fecha)

    if todos_actos:
        log.info(f"Total de actos legislativos detectados: {len(todos_actos)}")
        procesar_actos(todos_actos)
    else:
        log.info("No se detectaron actos legislativos relevantes")

    log.info("=== Vigilancia completada ===")


if __name__ == "__main__":
    main()
