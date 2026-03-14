#!/usr/bin/env python3
"""
scraper_catalogo.py — Extrae el catálogo completo de leyes de portales legislativos.
Fase 2 del Sistema LegismEx.

Uso:
    python3 scraper_catalogo.py --entidad guanajuato
    python3 scraper_catalogo.py --entidad nuevoleon
    python3 scraper_catalogo.py --entidad edomex
    python3 scraper_catalogo.py --todas          # Scraping de todas las entidades configuradas

Salida:
    estados/{entidad}/catalogo.json
    estados/{entidad}/catalogo.md
"""

import json
import logging
import re
import ssl
import sys
import argparse
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, date
from html.parser import HTMLParser
from pathlib import Path

# Contexto SSL que no verifica certificados (necesario en macOS con Python sin certifi)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ──────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
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

# ──────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-MX,es;q=0.9,en;q=0.5",
}


def fetch(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.error(f"Error fetching {url}: {e}")
        return None


# ──────────────────────────────────────────────
# Utilidades
# ──────────────────────────────────────────────
def limpiar_texto(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip()


def generar_id(entidad: str, nombre: str) -> str:
    nombre_limpio = nombre.lower()
    nombre_limpio = re.sub(r"[áàä]", "a", nombre_limpio)
    nombre_limpio = re.sub(r"[éèë]", "e", nombre_limpio)
    nombre_limpio = re.sub(r"[íìï]", "i", nombre_limpio)
    nombre_limpio = re.sub(r"[óòö]", "o", nombre_limpio)
    nombre_limpio = re.sub(r"[úùü]", "u", nombre_limpio)
    nombre_limpio = re.sub(r"ñ", "n", nombre_limpio)
    nombre_limpio = re.sub(r"[^a-z0-9\s]", "", nombre_limpio)
    nombre_limpio = re.sub(r"\s+", "_", nombre_limpio.strip())
    # Tomar primeras palabras significativas para mantener ID corto
    partes = [p for p in nombre_limpio.split("_") if p not in {
        "del", "de", "la", "las", "el", "los", "para", "y", "en", "al", "con",
        "un", "una", "por", "que", "se", "o", "sus", "estado", "municipios"
    }][:6]
    return f"{entidad}_{'_'.join(partes)}"


def inferir_tipo(nombre: str) -> str:
    n = nombre.upper()
    if n.startswith("CÓDIGO") or n.startswith("CODIGO"):
        return "Código"
    if n.startswith("REGLAMENTO"):
        return "Reglamento"
    if n.startswith("DECRETO"):
        return "Decreto"
    if n.startswith("ACUERDO"):
        return "Acuerdo"
    if n.startswith("NORMA"):
        return "Norma"
    return "Ley"


def guardar_catalogo(entidad: str, leyes: list[dict]):
    """Guarda catalogo.json y catalogo.md para la entidad."""
    if entidad == "federal":
        dir_entidad = BASE_DIR / "federal"
    else:
        dir_entidad = BASE_DIR / "estados" / entidad
    dir_entidad.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = dir_entidad / "catalogo.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(leyes, f, ensure_ascii=False, indent=2)
    log.info(f"  → {json_path} ({len(leyes)} leyes)")

    # Markdown
    md_path = dir_entidad / "catalogo.md"
    with open(md_path, "w", encoding="utf-8") as f:
        nombre_entidad = entidad.upper() if entidad != "federal" else "FEDERAL"
        f.write(f"# Catálogo de Leyes Vigentes — {nombre_entidad}\n\n")
        f.write(f"_Generado: {date.today().isoformat()} | Total: {len(leyes)} documentos_\n\n")
        f.write("| # | Nombre | Tipo | Última reforma | PDF |\n")
        f.write("|---|--------|------|---------------|-----|\n")
        for i, ley in enumerate(leyes, 1):
            nombre = ley.get("nombre", "")
            tipo = ley.get("tipo", "")
            reforma = ley.get("ultima_reforma", "—")
            url = ley.get("url_pdf", "")
            enlace = f"[PDF]({url})" if url else "—"
            f.write(f"| {i} | {nombre} | {tipo} | {reforma} | {enlace} |\n")
    log.info(f"  → {md_path}")


# ══════════════════════════════════════════════
# SCRAPERS POR ENTIDAD
# ══════════════════════════════════════════════

# ──────────────────────────────────────────────
# GUANAJUATO — congresogto.gob.mx/leyes
# Tabla paginada con S3 links. Categorías: LEYES, CÓDIGOS, REGLAMENTOS, etc.
# ──────────────────────────────────────────────

class GtoTableParser(HTMLParser):
    """Extrae filas de la tabla del Congreso de Guanajuato."""

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self._in_table = False
        self._in_row = False
        self._current_row: dict = {}
        self._col_index = 0
        self._cell_text: list[str] = []
        self._cell_links: list[str] = []
        self._depth_td = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "tbody":
            self._in_table = True
        if self._in_table and tag == "tr":
            self._in_row = True
            self._current_row = {}
            self._col_index = 0
        if self._in_row and tag in ("td", "th"):
            self._cell_text = []
            self._cell_links = []
            self._depth_td += 1
        if self._in_row and tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href and not href.startswith("#"):
                self._cell_links.append(href)

    def handle_data(self, data):
        if self._depth_td > 0:
            self._cell_text.append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._depth_td > 0:
            self._depth_td -= 1
            if self._depth_td == 0:
                text = limpiar_texto(" ".join(t for t in self._cell_text if t))
                links = self._cell_links[:]
                col = self._col_index
                if col == 0:
                    self._current_row["nombre"] = text
                elif col == 1:
                    self._current_row["tipo_raw"] = text
                elif col == 2 and links:
                    self._current_row["url_pdf"] = links[0]
                elif col == 3 and links:
                    self._current_row["url_word"] = links[0]
                elif col == 4:
                    self._current_row["ultima_reforma"] = text
                self._col_index += 1
                self._cell_text = []
                self._cell_links = []
        if tag == "tr" and self._in_row:
            if self._current_row.get("nombre"):
                self.rows.append(dict(self._current_row))
            self._in_row = False
            self._current_row = {}
        if tag == "tbody":
            self._in_table = False


def scrape_guanajuato() -> list[dict]:
    entidad = "guanajuato"
    base_url = "https://www.congresogto.gob.mx"
    categorias = {
        "leyes":       f"{base_url}/leyes",
        "codigos":     f"{base_url}/codigos",
        "reglamentos": f"{base_url}/reglamentos",
        "acuerdos":    f"{base_url}/acuerdos",
        "decretos":    f"{base_url}/decretos",
    }

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for cat, url_base in categorias.items():
        log.info(f"  Guanajuato/{cat}: {url_base}")
        pagina = 1
        while True:
            url = f"{url_base}?page={pagina}" if pagina > 1 else url_base
            html = fetch(url)
            if not html:
                break

            parser = GtoTableParser()
            parser.feed(html)

            if not parser.rows:
                break

            for row in parser.rows:
                nombre = row.get("nombre", "").strip()
                if not nombre or len(nombre) < 5:
                    continue
                tipo = inferir_tipo(nombre)
                ley_id = generar_id(entidad, nombre)
                if ley_id in ids_vistos:
                    continue
                ids_vistos.add(ley_id)

                # Normalizar URL PDF
                url_pdf = row.get("url_pdf", "")
                if url_pdf and not url_pdf.startswith("http"):
                    url_pdf = base_url + url_pdf

                leyes.append({
                    "id": ley_id,
                    "nombre": nombre,
                    "tipo": tipo,
                    "entidad": entidad,
                    "url_pdf": url_pdf,
                    "url_word": row.get("url_word", ""),
                    "ultima_reforma": row.get("ultima_reforma", ""),
                    "estado_vigencia": "vigente",
                    "fuente": "congresogto.gob.mx",
                    "categoria": cat,
                })

            # ¿Hay página siguiente? Buscar "Next" o número de página
            if f"page={pagina + 1}" not in html and f"page%3D{pagina + 1}" not in html:
                # Verificar si hay enlace de siguiente página
                has_next = bool(
                    re.search(r'href=["\'][^"\']*page=' + str(pagina + 1), html)
                )
                if not has_next:
                    break
            pagina += 1
            time.sleep(0.5)

    log.info(f"  Guanajuato total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# NUEVO LEÓN — hcnl.gob.mx/trabajo_legislativo/leyes/
# Tabla paginada. URLs .pdf directas en servidor propio.
# ──────────────────────────────────────────────

class NLTableParser(HTMLParser):
    """Extrae filas de la tabla del Congreso de Nuevo León."""

    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self._in_tbody = False
        self._in_tr = False
        self._current_row: dict = {}
        self._col = 0
        self._cell_text: list[str] = []
        self._cell_links: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "tbody":
            self._in_tbody = True
        if self._in_tbody and tag == "tr":
            self._in_tr = True
            self._current_row = {}
            self._col = 0
        if self._in_tr and tag == "td":
            self._cell_text = []
            self._cell_links = []
            self._depth += 1
        if self._in_tr and tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href:
                self._cell_links.append(href)

    def handle_data(self, data):
        if self._depth > 0:
            stripped = data.strip()
            if stripped:
                self._cell_text.append(stripped)

    def handle_endtag(self, tag):
        if tag == "td" and self._depth > 0:
            self._depth -= 1
            if self._depth == 0:
                text = limpiar_texto(" ".join(self._cell_text))
                links = self._cell_links[:]
                if self._col == 0:
                    self._current_row["nombre"] = text
                    # Buscar link del nombre
                    if links:
                        self._current_row["url_detalle"] = links[0]
                elif self._col == 1:
                    self._current_row["ultima_reforma"] = text
                elif self._col == 2:
                    # Columna de archivos: PDF y DOC
                    pdf_links = [l for l in links if ".pdf" in l.lower()]
                    doc_links = [l for l in links if ".doc" in l.lower()]
                    if pdf_links:
                        self._current_row["url_pdf"] = pdf_links[0]
                    if doc_links:
                        self._current_row["url_word"] = doc_links[0]
                self._col += 1
                self._cell_text = []
                self._cell_links = []
        if tag == "tr" and self._in_tr:
            if self._current_row.get("nombre"):
                self.rows.append(dict(self._current_row))
            self._in_tr = False
            self._current_row = {}
        if tag == "tbody":
            self._in_tbody = False


def scrape_nuevoleon() -> list[dict]:
    entidad = "nuevoleon"
    base_url = "https://www.hcnl.gob.mx"
    tabs = {
        "leyes":          f"{base_url}/trabajo_legislativo/leyes/",
        "codigos":        f"{base_url}/trabajo_legislativo/codigos/",
        "reglamentos":    f"{base_url}/trabajo_legislativo/reglamentos/",
        "decretos":       f"{base_url}/trabajo_legislativo/decretos/",
        "acuerdos":       f"{base_url}/trabajo_legislativo/acuerdos/",
    }

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for cat, url_base in tabs.items():
        log.info(f"  Nuevo León/{cat}: {url_base}")

        # El portal usa DataTables con parámetro de longitud
        # Intentar cargar con length=200 para reducir paginación
        params = ["", "?length=200", "?per_page=200"]

        for param in params:
            url = url_base + param
            html = fetch(url)
            if not html:
                continue

            parser = NLTableParser()
            parser.feed(html)
            if not parser.rows:
                continue

            for row in parser.rows:
                nombre = row.get("nombre", "").strip()
                if not nombre or len(nombre) < 5:
                    continue

                tipo = inferir_tipo(nombre)
                ley_id = generar_id(entidad, nombre)
                if ley_id in ids_vistos:
                    continue
                ids_vistos.add(ley_id)

                url_pdf = row.get("url_pdf", "")
                if url_pdf and not url_pdf.startswith("http"):
                    url_pdf = base_url + url_pdf

                # Si no hay URL de PDF directa, intentar construirla desde el nombre
                if not url_pdf and nombre:
                    nombre_url = urllib.parse.quote(nombre.upper()) if hasattr(urllib, 'parse') else nombre.upper()
                    url_pdf = f"{base_url}/trabajo_legislativo/leyes/pdf/{nombre_url}.pdf"

                leyes.append({
                    "id": ley_id,
                    "nombre": nombre,
                    "tipo": tipo,
                    "entidad": entidad,
                    "url_pdf": url_pdf,
                    "url_word": row.get("url_word", ""),
                    "ultima_reforma": row.get("ultima_reforma", ""),
                    "estado_vigencia": "vigente",
                    "fuente": "hcnl.gob.mx",
                    "categoria": cat,
                })
            break  # Si encontramos filas, no probar otros params
        time.sleep(0.5)

    log.info(f"  Nuevo León total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# ESTADO DE MÉXICO — legislacion.edomex.gob.mx
# Acordeón por categorías. Links PDF y WORD visibles.
# ──────────────────────────────────────────────

class EdoMexParser(HTMLParser):
    """Extrae links de leyes del acordeón de EdoMex."""

    def __init__(self):
        super().__init__()
        self.leyes: list[dict] = []
        self._current_nombre: str = ""
        self._capture_next = False
        self._links_buffer: list[dict] = []
        self._in_list_item = False
        self._item_text: list[str] = []
        self._item_links: list[str] = []
        self._depth_li = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "li":
            self._depth_li += 1
            if self._depth_li == 1:
                self._item_text = []
                self._item_links = []
        if tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href and (".pdf" in href.lower() or ".doc" in href.lower() or ".docx" in href.lower()):
                if self._depth_li > 0:
                    self._item_links.append(href)

    def handle_data(self, data):
        if self._depth_li > 0:
            stripped = data.strip()
            if stripped and len(stripped) > 3:
                self._item_text.append(stripped)

    def handle_endtag(self, tag):
        if tag == "li" and self._depth_li > 0:
            self._depth_li -= 1
            if self._depth_li == 0:
                text = limpiar_texto(" ".join(self._item_text))
                links = self._item_links[:]
                if text and len(text) > 10 and links:
                    pdf_links = [l for l in links if ".pdf" in l.lower()]
                    doc_links = [l for l in links if ".doc" in l.lower()]
                    self.leyes.append({
                        "nombre": text,
                        "url_pdf": pdf_links[0] if pdf_links else "",
                        "url_word": doc_links[0] if doc_links else "",
                    })
                self._item_text = []
                self._item_links = []


def scrape_edomex() -> list[dict]:
    entidad = "edomex"
    base_url = "https://legislacion.edomex.gob.mx"

    urls_categorias = [
        f"{base_url}/leyes_vigentes",
        f"{base_url}/codigos",
        f"{base_url}/reglamentos",
        f"{base_url}/",  # Página principal también tiene listado
    ]

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for url in urls_categorias:
        log.info(f"  EdoMex: {url}")
        html = fetch(url)
        if not html:
            continue

        parser = EdoMexParser()
        parser.feed(html)

        for item in parser.leyes:
            nombre = item["nombre"]
            if not nombre or len(nombre) < 8:
                continue

            tipo = inferir_tipo(nombre)
            ley_id = generar_id(entidad, nombre)
            if ley_id in ids_vistos:
                continue
            ids_vistos.add(ley_id)

            url_pdf = item.get("url_pdf", "")
            if url_pdf and not url_pdf.startswith("http"):
                url_pdf = base_url + url_pdf

            url_word = item.get("url_word", "")
            if url_word and not url_word.startswith("http"):
                url_word = base_url + url_word

            leyes.append({
                "id": ley_id,
                "nombre": nombre,
                "tipo": tipo,
                "entidad": entidad,
                "url_pdf": url_pdf,
                "url_word": url_word,
                "ultima_reforma": "",
                "estado_vigencia": "vigente",
                "fuente": "legislacion.edomex.gob.mx",
            })
        time.sleep(0.5)

    log.info(f"  EdoMex total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# FEDERAL — diputados.gob.mx/LeyesBiblio/
# Tiene anti-bot 403. Alternativa: parsear el HTML de la tabla.
# ──────────────────────────────────────────────

class FederalParser(HTMLParser):
    """Extrae leyes de la tabla de la Cámara de Diputados."""

    def __init__(self):
        super().__init__()
        self.leyes: list[dict] = []
        self._in_tabla = False
        self._in_tr = False
        self._col = 0
        self._depth_td = 0
        self._cell_text: list[str] = []
        self._cell_links: list[str] = []
        self._current_row: dict = {}

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        cls = attrs_d.get("class", "")
        if tag == "table" and ("leyes" in cls or "tabla" in cls.lower()):
            self._in_tabla = True
        if self._in_tabla and tag == "tr":
            self._in_tr = True
            self._col = 0
            self._current_row = {}
        if self._in_tr and tag == "td":
            self._cell_text = []
            self._cell_links = []
            self._depth_td += 1
        if self._in_tr and tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            if href:
                self._cell_links.append(href)

    def handle_data(self, data):
        if self._depth_td > 0:
            stripped = data.strip()
            if stripped:
                self._cell_text.append(stripped)

    def handle_endtag(self, tag):
        if tag == "td" and self._depth_td > 0:
            self._depth_td -= 1
            if self._depth_td == 0:
                text = limpiar_texto(" ".join(self._cell_text))
                links = self._cell_links[:]
                if self._col == 0:
                    self._current_row["nombre"] = text
                elif links:
                    pdf = [l for l in links if ".pdf" in l.lower()]
                    if pdf:
                        self._current_row["url_pdf"] = pdf[0]
                self._col += 1
                self._cell_text = []
                self._cell_links = []
        if tag == "tr" and self._in_tr:
            if self._current_row.get("nombre"):
                self.leyes.append(dict(self._current_row))
            self._in_tr = False
            self._current_row = {}
        if tag == "table":
            self._in_tabla = False


def scrape_federal() -> list[dict]:
    entidad = "federal"
    base_url = "https://www.diputados.gob.mx"
    url = f"{base_url}/LeyesBiblio/"

    # Intentar con headers más completos para evitar el 403
    headers_extra = {
        **HEADERS,
        "Referer": "https://www.google.com/",
        "Accept-Encoding": "gzip, deflate, br",
    }
    req = urllib.request.Request(url, headers=headers_extra)
    html = None
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            html = resp.read().decode(charset, errors="replace")
    except Exception as e:
        log.warning(f"  Federal: acceso directo falló ({e}). Intentando SCJN como alternativa...")

    leyes: list[dict] = []

    if html:
        # Extraer todos los links .pdf y .htm de la página
        pattern_pdf = re.compile(
            r'href=["\']([^"\']*\.pdf)["\']',
            re.IGNORECASE
        )
        pattern_nombre = re.compile(
            r'<(?:td|li|p)[^>]*>\s*<a[^>]+href=["\'][^"\']*\.pdf["\'][^>]*>([^<]+)</a>',
            re.IGNORECASE
        )

        pdfs = pattern_pdf.findall(html)
        nombres_raw = pattern_nombre.findall(html)

        # Método alternativo: parsear toda la tabla
        parser = FederalParser()
        parser.feed(html)

        ids_vistos: set[str] = set()
        for item in parser.leyes:
            nombre = item.get("nombre", "").strip()
            if not nombre or len(nombre) < 5:
                continue
            tipo = inferir_tipo(nombre)
            ley_id = generar_id(entidad, nombre)
            if ley_id in ids_vistos:
                continue
            ids_vistos.add(ley_id)

            url_pdf = item.get("url_pdf", "")
            if url_pdf and not url_pdf.startswith("http"):
                url_pdf = base_url + url_pdf

            leyes.append({
                "id": ley_id,
                "nombre": nombre,
                "tipo": tipo,
                "entidad": entidad,
                "url_pdf": url_pdf,
                "url_word": "",
                "ultima_reforma": "",
                "estado_vigencia": "vigente",
                "fuente": "diputados.gob.mx",
            })
    else:
        log.warning("  Federal: no se pudo obtener el catálogo. Requiere revisión manual.")
        log.warning("  Sugerencia: ejecutar con un navegador real o usar SCJN como fuente.")

    log.info(f"  Federal total: {len(leyes)} documentos")
    return leyes


# ══════════════════════════════════════════════
# REGISTRO DE SCRAPERS DISPONIBLES
# ══════════════════════════════════════════════

SCRAPERS: dict[str, callable] = {
    "guanajuato": scrape_guanajuato,
    "nuevoleon":  scrape_nuevoleon,
    "edomex":     scrape_edomex,
    "federal":    scrape_federal,
}


def scraper_no_implementado(entidad: str):
    """Placeholder para entidades sin scraper aún."""
    def _scraper():
        log.warning(
            f"  [{entidad}] No hay scraper implementado. "
            f"Ver estados/{entidad}/fuentes.md para la URL de inicio."
        )
        return []
    return _scraper


# ══════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════

TODAS_ENTIDADES = [
    "federal",
    "aguascalientes", "bajacalifornia", "bajacaliforniasur", "campeche",
    "cdmx", "chiapas", "chihuahua", "coahuila", "colima", "durango",
    "edomex", "guanajuato", "guerrero", "hidalgo", "jalisco",
    "michoacan", "morelos", "nayarit", "nuevoleon", "oaxaca",
    "puebla", "queretaro", "quintanaroo", "sanluispotosi", "sinaloa",
    "sonora", "tabasco", "tamaulipas", "tlaxcala", "veracruz",
    "yucatan", "zacatecas",
]


def main():
    parser = argparse.ArgumentParser(
        description="Extrae catálogos de leyes de portales legislativos mexicanos"
    )
    parser.add_argument(
        "--entidad",
        help="Entidad a procesar (ej: guanajuato, nuevoleon, federal)",
    )
    parser.add_argument(
        "--todas",
        action="store_true",
        help="Procesar todas las entidades con scraper implementado",
    )
    args = parser.parse_args()

    if not args.entidad and not args.todas:
        parser.print_help()
        print("\nEntidades con scraper implementado:")
        for e in sorted(SCRAPERS.keys()):
            print(f"  {e}")
        sys.exit(0)

    entidades = TODAS_ENTIDADES if args.todas else [args.entidad]

    # Import tardío ya está al inicio del módulo

    for entidad in entidades:
        log.info(f"\n{'═'*50}")
        log.info(f"Procesando: {entidad.upper()}")
        log.info(f"{'═'*50}")

        fn = SCRAPERS.get(entidad, scraper_no_implementado(entidad))
        try:
            leyes = fn()
            if leyes:
                guardar_catalogo(entidad, leyes)
                log.info(f"✓ {entidad}: {len(leyes)} leyes guardadas en catálogo")
            else:
                log.warning(f"✗ {entidad}: catálogo vacío")
        except Exception as e:
            log.error(f"Error procesando {entidad}: {e}", exc_info=True)

    log.info("\nScraping completado.")


if __name__ == "__main__":
    main()
