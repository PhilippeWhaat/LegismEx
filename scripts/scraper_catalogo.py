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

    # La página principal /LeyesBiblio/ da 403 (anti-bot),
    # pero /LeyesBiblio/index.htm funciona y lista todas las leyes
    # con links a ref/*.htm (cada uno tiene link al PDF).
    url = f"{base_url}/LeyesBiblio/index.htm"
    log.info(f"  Federal: {url}")

    html = fetch(url)
    if not html:
        log.warning("  Federal: no se pudo obtener index.htm")
        return []

    # Extraer links ref/*.htm con el nombre de la ley como texto
    entries = re.findall(
        r'<a[^>]+href=["\'](?:ref/([^"\']+\.htm))["\'][^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE
    )

    # Deduplicar y filtrar (excluir _crono, _art, _per variantes)
    leyes: list[dict] = []
    ids_vistos: set[str] = set()
    refs_vistos: set[str] = set()

    for ref_page, text in entries:
        # Skip variant pages
        if any(s in ref_page for s in ["_crono", "_art.", "_per.", "_fe.", "_eo."]):
            continue
        if ref_page in refs_vistos:
            continue
        refs_vistos.add(ref_page)

        nombre = re.sub(r'<[^>]+>', '', text).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 5:
            continue
        # Skip "Más de la Constitución" or navigation text
        if nombre.startswith("Más de") or nombre.startswith("Artículo"):
            continue

        # Construct PDF URL: ref/xxx.htm → pdf/XXX.pdf
        # The PDF name is usually the ref name in uppercase without .htm
        ref_base = ref_page.replace(".htm", "")
        url_pdf = f"{base_url}/LeyesBiblio/pdf/{ref_base.upper()}.pdf"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "diputados.gob.mx",
        })

    log.info(f"  Federal total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# TAMAULIPAS — congresotamaulipas.gob.mx
# Lista numerada con PDF/Word links. 129 leyes en una sola página.
# URL: /LegislacionEstatal/LegislacionVigente/Vigente.asp?idtipoArchivo=1
# ──────────────────────────────────────────────

class TamaulipasParser(HTMLParser):
    """Extrae leyes de la página de legislación vigente de Tamaulipas."""

    def __init__(self):
        super().__init__()
        self.leyes: list[dict] = []
        self._current_text: list[str] = []
        self._current_links: list[dict] = []
        self._in_bold = False
        self._bold_text: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "b" or tag == "strong":
            self._in_bold = True
            self._bold_text = []
        if tag == "a" and "href" in attrs_d:
            href = attrs_d["href"]
            text = ""
            if "VerLey" in href or ".pdf" in href.lower():
                self._current_links.append({"href": href, "type": "pdf"})
            elif ".doc" in href.lower():
                self._current_links.append({"href": href, "type": "word"})

    def handle_data(self, data):
        stripped = data.strip()
        if stripped:
            self._current_text.append(stripped)
            if self._in_bold:
                self._bold_text.append(stripped)

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self._in_bold = False
        if tag == "hr" or tag == "img":
            # Each law entry is separated by hr/img dividers
            self._flush_entry()

    def _flush_entry(self):
        text = " ".join(self._current_text)
        if not text or len(text) < 10:
            self._current_text = []
            self._current_links = []
            return

        # Extract law name and reform date from text
        # Format: "NNN. Name, Ley de... Última Reforma: DD de Month de YYYY"
        nombre_match = re.search(
            r'\d{3}\.\s*(.+?)(?:\s*Última\s+Reforma|\s*Publicación|\s*$)',
            text, re.IGNORECASE | re.DOTALL
        )
        nombre = nombre_match.group(1).strip() if nombre_match else ""
        if not nombre:
            # Try bold text as fallback
            nombre = " ".join(self._bold_text).strip()

        reforma_match = re.search(
            r'(?:Última\s+Reforma|Publicación)[:\s]+(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})',
            text, re.IGNORECASE
        )
        ultima_reforma = ""
        if reforma_match:
            dia, mes_str, anio = reforma_match.groups()
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
            }
            mes = meses.get(mes_str.lower(), "01")
            ultima_reforma = f"{anio}-{mes}-{int(dia):02d}"

        pdf_link = ""
        word_link = ""
        for link in self._current_links:
            if link["type"] == "pdf" and not pdf_link:
                pdf_link = link["href"]
            elif link["type"] == "word" and not word_link:
                word_link = link["href"]

        if nombre and len(nombre) > 5:
            self.leyes.append({
                "nombre": limpiar_texto(nombre),
                "url_pdf": pdf_link,
                "url_word": word_link,
                "ultima_reforma": ultima_reforma,
            })

        self._current_text = []
        self._current_links = []
        self._bold_text = []


def scrape_tamaulipas() -> list[dict]:
    entidad = "tamaulipas"
    base_url = "https://www.congresotamaulipas.gob.mx"
    url = f"{base_url}/LegislacionEstatal/LegislacionVigente/Vigente.asp?idtipoArchivo=1"

    log.info(f"  Tamaulipas: {url}")
    # Fetch with latin-1 encoding (ASP page)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            html = resp.read().decode("latin-1", errors="replace")
    except Exception as e:
        log.warning(f"  Tamaulipas: no se pudo acceder ({e})")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    # Split by VerLey links to isolate each entry
    # Pattern: number + bold name + reform date + VerLey link
    # Each block: <b>NNN</b> ... <b>Name</b> ... reforma ... VerLey.asp?IdLey=NNN
    blocks = re.split(r'(?=<td[^>]*>\s*<span[^>]*>\s*<b>\s*\d{3}\s*</b>)', html)

    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    }

    for block in blocks:
        # Extract VerLey ID
        id_match = re.search(r'VerLey\.asp\?IdLey=(\d+)', block)
        if not id_match:
            continue
        id_ley = id_match.group(1)

        # Extract law name from second <b> tag (first is the number)
        bold_texts = re.findall(r'<b>([^<]+)</b>', block)
        nombre = ""
        for bt in bold_texts:
            bt = bt.strip()
            if bt and not bt.isdigit() and len(bt) > 5:
                nombre = limpiar_texto(bt)
                break

        if not nombre:
            continue

        # Extract reform date
        reform_match = re.search(
            r'(?:ltima\s+reforma|Publicaci).*?(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})',
            block, re.IGNORECASE
        )
        ultima_reforma = ""
        if reform_match:
            dia, mes_str, anio = reform_match.groups()
            mes = meses.get(mes_str.lower(), "01")
            ultima_reforma = f"{anio}-{mes}-{int(dia):02d}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        url_pdf = f"{base_url}/LegislacionEstatal/LegislacionVigente/VerLey.asp?IdLey={id_ley}"

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresotamaulipas.gob.mx",
        })

    log.info(f"  Tamaulipas total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# CHIHUAHUA — congresochihuahua.gob.mx
# Biblioteca legislativa paginada (5 leyes/página).
# CSV export disponible en generarCSV.php.
# PDF: congresochihuahua2.gob.mx/biblioteca/leyes/archivosLeyes/{id}.pdf
# ──────────────────────────────────────────────

def scrape_chihuahua() -> list[dict]:
    entidad = "chihuahua"
    csv_url = "https://www.congresochihuahua.gob.mx/biblioteca/leyes/generarCSV.php"
    page_url = "https://www.congresochihuahua.gob.mx/biblioteca/leyes/"

    # Step 1: Get full list from CSV export (names + publication dates)
    log.info(f"  Chihuahua CSV: {csv_url}")
    csv_text = fetch(csv_url)
    if not csv_text:
        log.warning("  Chihuahua: no se pudo obtener CSV")
        return []

    import csv as csv_mod
    import io
    reader = csv_mod.reader(io.StringIO(csv_text))
    header = next(reader, None)
    csv_rows = list(reader)
    log.info(f"  Chihuahua CSV: {len(csv_rows)} leyes en CSV")

    # Step 2: Scrape paginated pages to map law names to PDF IDs
    # The page uses GET with ?pagina=N parameter
    # Max pages = ceil(CSV rows / 5) + buffer (server wraps around infinitely)
    max_pages = (len(csv_rows) // 5) + 5
    name_to_pdf: dict[str, dict] = {}
    pagina = 1
    while pagina <= max_pages:
        url = f"{page_url}?pagina={pagina}"
        html = fetch(url)
        if not html:
            break

        # Each law card: name in text, then archivosLeyes/{id}.pdf
        # Split by "table-bordered" class which wraps each law
        cards = re.split(r'class="table\s+table-bordered"', html)
        found = 0

        for card in cards[1:]:
            name_match = re.search(
                r'>\s*((?:LEY|CÓDIGO|CODIGO|REGLAMENTO|CONSTITUCIÓN|CONSTITUCION|'
                r'DECRETO)[^<]{10,})',
                card, re.IGNORECASE
            )
            if not name_match:
                # Try any substantial uppercase text
                name_match = re.search(
                    r'>\s*([A-ZÁÉÍÓÚÑÜ][A-ZÁÉÍÓÚÑÜ\s,.\-()]{15,}[A-ZÁÉÍÓÚÑÜ)])\s*<',
                    card
                )
            if not name_match:
                continue

            nombre = limpiar_texto(name_match.group(1))
            pdf_match = re.search(r'(https?://[^"\'>\s]*archivosLeyes/\d+\.pdf)', card)
            word_match = re.search(r'(https?://[^"\'>\s]*archivosLeyesWord/\d+\.doc)', card)

            name_to_pdf[nombre.upper()] = {
                "url_pdf": pdf_match.group(1) if pdf_match else "",
                "url_word": word_match.group(1) if word_match else "",
            }
            found += 1

        log.info(f"  Chihuahua página {pagina}: {found} leyes con PDF")
        if found == 0:
            break
        pagina += 1
        time.sleep(0.3)

    log.info(f"  Chihuahua: {len(name_to_pdf)} leyes mapeadas a PDF")

    # Step 3: Build catalog from CSV, enriched with PDF URLs
    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for row in csv_rows:
        if not row or not row[0].strip():
            continue
        nombre = limpiar_texto(row[0])
        fecha_pub = row[1].strip() if len(row) > 1 else ""

        # Look up PDF URL
        urls = name_to_pdf.get(nombre.upper(), {})

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": urls.get("url_pdf", ""),
            "url_word": urls.get("url_word", ""),
            "ultima_reforma": fecha_pub,
            "estado_vigencia": "vigente",
            "fuente": "congresochihuahua.gob.mx",
        })

    log.info(f"  Chihuahua total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# DURANGO — congresodurango.gob.mx/Archivos/legislacion/
# Directory listing Apache con archivos PDF directos.
# Nombres de ley extraídos del nombre de archivo.
# ──────────────────────────────────────────────

def scrape_durango() -> list[dict]:
    entidad = "durango"
    base_url = "https://congresodurango.gob.mx/Archivos/legislacion"
    url = f"{base_url}/"

    log.info(f"  Durango: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Durango: no se pudo acceder al directorio")
        return []

    # Parse Apache directory listing in table format
    # Format: <a href="FILE.pdf">...</a></td><td>2026-02-12 09:10</td><td>2.8M</td>
    entries = re.findall(
        r'<a\s+href="([^"]+\.pdf)">.*?</a>\s*</td>\s*<td[^>]*>\s*'
        r'(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s*</td>\s*<td[^>]*>\s*'
        r'([0-9.]+[KMG]?)',
        html, re.DOTALL
    )

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for filename, fecha, size in entries:
        # Skip duplicates and copies
        if "copy" in filename.lower() or "copia" in filename.lower():
            continue
        if "(ABROGADA)" in filename.upper() or "(ANTERIOR)" in filename.upper():
            continue

        # Decode URL-encoded filename to get law name
        nombre_raw = urllib.parse.unquote(filename).replace(".pdf", "")
        # Clean up: remove (NUEVA), (N), trailing spaces
        nombre = re.sub(r'\s*\(NUEV[AO]\)\s*', ' ', nombre_raw).strip()
        nombre = re.sub(r'\s*\(N\)\s*', ' ', nombre).strip()
        nombre = re.sub(r'\s+', ' ', nombre)

        # Title case
        nombre_title = nombre.title()
        # Fix common title case issues
        for word in ["Del", "De", "La", "Las", "Los", "El", "Para", "En",
                      "Al", "Con", "Por", "Y", "O", "Sus", "Que"]:
            nombre_title = nombre_title.replace(f" {word} ", f" {word.lower()} ")

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        url_pdf = f"{base_url}/{urllib.parse.quote(filename)}"

        # Check for matching .docx
        docx_name = filename.replace(".pdf", ".docx")
        url_word = ""
        if docx_name in html:
            url_word = f"{base_url}/{urllib.parse.quote(docx_name)}"

        leyes.append({
            "id": ley_id,
            "nombre": nombre_title,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "url_word": url_word,
            "ultima_reforma": fecha,
            "estado_vigencia": "vigente",
            "fuente": "congresodurango.gob.mx",
        })

    log.info(f"  Durango total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# TABASCO — congresotabasco.gob.mx/leyes/
# WordPress con links PDF directos en wp-content/uploads.
# ──────────────────────────────────────────────

def scrape_tabasco() -> list[dict]:
    entidad = "tabasco"
    base_url = "https://congresotabasco.gob.mx"
    url = f"{base_url}/leyes/"

    log.info(f"  Tabasco: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Tabasco: no se pudo acceder al portal")
        return []

    # Extract all PDF links from wp-content/uploads
    pdf_links = re.findall(
        r'href="(https://congresotabasco\.gob\.mx/wp-content/uploads/[^"]+\.pdf)"',
        html
    )

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for url_pdf in pdf_links:
        # Extract law name from URL
        # Pattern: .../2026/02/Ley-de-Something-del-Estado-de-Tabasco.pdf
        filename = url_pdf.split("/")[-1]
        nombre_raw = filename.replace(".pdf", "").replace("-", " ")

        # Skip non-law files (Leyes de Ingresos municipales, etc.)
        if re.search(r'Ley de Ingresos de (?!Tabasco)', nombre_raw, re.IGNORECASE):
            continue

        nombre = limpiar_texto(nombre_raw)
        if len(nombre) < 10:
            continue

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        # Try to extract date from URL path (e.g., /2026/02/)
        date_match = re.search(r'/(\d{4})/(\d{2})/', url_pdf)
        ultima_reforma = ""
        if date_match:
            ultima_reforma = f"{date_match.group(1)}-{date_match.group(2)}"

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresotabasco.gob.mx",
        })

    log.info(f"  Tabasco total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# OAXACA — congresooaxaca.gob.mx
# Tabla HTML simple en una sola página. 225 leyes.
# Columnas: No, Nombre, Publicación, Última Reforma, Descarga (PDF)
# ──────────────────────────────────────────────

def scrape_oaxaca() -> list[dict]:
    entidad = "oaxaca"
    url = "https://www.congresooaxaca.gob.mx/legislaciones/legislacion_estatal.html"

    log.info(f"  Oaxaca: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Oaxaca: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    # Parse table rows
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows[1:]:  # Skip header
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 8:
            continue

        # Cell 3: nombre, Cell 5: publicación, Cell 7: última reforma
        # Cell 9 (or last): PDF link
        nombre = re.sub(r'<[^>]+>', '', cells[3]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue

        # Clean trailing reform description from name
        nombre = re.sub(r'\s*\(Reformad[ao].*$', '', nombre).strip()
        nombre = re.sub(r'\s*\(Abrogad[ao].*$', '', nombre).strip()

        fecha_pub = re.sub(r'<[^>]+>', '', cells[5]).strip() if len(cells) > 5 else ""
        ultima_reforma = re.sub(r'<[^>]+>', '', cells[7]).strip() if len(cells) > 7 else ""

        # Find PDF link in last cells
        url_pdf = ""
        for cell in cells[8:]:
            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', cell, re.IGNORECASE)
            if pdf_match:
                url_pdf = pdf_match.group(1)
                break

        # Normalize PDF URL
        if url_pdf and not url_pdf.startswith("http"):
            # Relative path like ../../docs66.congresooaxaca.gob.mx/...
            if "congresooaxaca" in url_pdf:
                # Extract the domain-relative part
                match = re.search(r'(docs\d*\.congresooaxaca\.gob\.mx/.*)', url_pdf)
                if match:
                    url_pdf = f"https://{match.group(1)}"
                else:
                    url_pdf = f"https://www.congresooaxaca.gob.mx/{url_pdf.lstrip('./')}"
            else:
                url_pdf = f"https://www.congresooaxaca.gob.mx/{url_pdf.lstrip('./')}"

        # Normalize dates from DD-MM-YYYY to YYYY-MM-DD
        for date_str in [ultima_reforma, fecha_pub]:
            m = re.match(r'(\d{2})-(\d{2})-(\d{4})', date_str)
            if m:
                if date_str == ultima_reforma:
                    ultima_reforma = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
                else:
                    fecha_pub = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresooaxaca.gob.mx",
        })

    log.info(f"  Oaxaca total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# BAJA CALIFORNIA — congresobc.gob.mx
# DataTables HTML con PDF+WORD links. ~165 leyes vigentes.
# Columnas: Nombre, PDF, WORD, Fecha, Estado, Tomo, Historial
# ──────────────────────────────────────────────

def scrape_bajacalifornia() -> list[dict]:
    entidad = "bajacalifornia"
    base_url = "https://www.congresobc.gob.mx"
    url = f"{base_url}/TrabajoLegislativo/Leyes"

    log.info(f"  Baja California: {url}")
    html = fetch(url)
    if not html:
        log.warning("  BC: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 5:
            continue

        # Cell 0: nombre, Cell 1: PDF link, Cell 2: WORD link, Cell 3: fecha, Cell 4: estado
        nombre = re.sub(r'<[^>]+>', '', cells[0]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue

        # PDF link
        pdf_match = re.search(r'href=["\']([^"\']+\.PDF)["\']', cells[1], re.IGNORECASE)
        url_pdf = pdf_match.group(1) if pdf_match else ""
        if url_pdf and not url_pdf.startswith("http"):
            url_pdf = f"{base_url}/TrabajoLegislativo/{url_pdf.lstrip('./')}"

        # Word link
        word_match = re.search(r'href=["\']([^"\']+\.DOC)["\']', cells[2], re.IGNORECASE)
        url_word = word_match.group(1) if word_match else ""
        if url_word and not url_word.startswith("http"):
            url_word = f"{base_url}/TrabajoLegislativo/{url_word.lstrip('./')}"

        # Date (YYYY/MM/DD format)
        fecha = re.sub(r'<[^>]+>', '', cells[3]).strip()
        ultima_reforma = fecha.replace("/", "-") if fecha else ""

        # Vigencia
        estado = re.sub(r'<[^>]+>', '', cells[4]).strip().lower()

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "url_word": url_word,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente" if "vigente" in estado else estado,
            "fuente": "congresobc.gob.mx",
        })

    log.info(f"  Baja California total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# YUCATÁN — congresoyucatan.gob.mx
# Tabla HTML con 1091 leyes (vigentes + abrogadas).
# Incluye leyes, códigos, reglamentos en múltiples categorías.
# URLs: /legislacion/leyes, /legislacion/codigos, /legislacion/reglamentos
# ──────────────────────────────────────────────

def scrape_yucatan() -> list[dict]:
    entidad = "yucatan"
    base_url = "https://www.congresoyucatan.gob.mx"

    categorias = {
        "leyes":       f"{base_url}/legislacion/leyes",
        "codigos":     f"{base_url}/legislacion/codigos",
        "reglamentos": f"{base_url}/legislacion/reglamentos",
    }

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for cat, url in categorias.items():
        log.info(f"  Yucatán/{cat}: {url}")
        html = fetch(url)
        if not html:
            continue

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) < 5:
                continue

            nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
            nombre = limpiar_texto(nombre)
            if not nombre or len(nombre) < 8:
                continue

            fecha_pub = re.sub(r'<[^>]+>', '', cells[2]).strip()
            ultima_reforma = ""
            if len(cells) > 3:
                ultima_reforma = re.sub(r'<[^>]+>', '', cells[3]).strip()
                if ultima_reforma == "0000-00-00":
                    ultima_reforma = ""

            estado = re.sub(r'<[^>]+>', '', cells[4]).strip().lower() if len(cells) > 4 else ""

            # Skip abrogated laws
            if "abrogada" in estado:
                continue

            # PDF and Word links (in last cell)
            url_pdf = ""
            url_word = ""
            link_cell = cells[-1] if len(cells) > 5 else ""
            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', link_cell, re.IGNORECASE)
            if pdf_match:
                url_pdf = pdf_match.group(1)
                if not url_pdf.startswith("http"):
                    url_pdf = f"{base_url}{url_pdf}"
            word_match = re.search(r'href=["\']([^"\']+\.(?:doc|docx))["\']', link_cell, re.IGNORECASE)
            if word_match:
                url_word = word_match.group(1)
                if not url_word.startswith("http"):
                    url_word = f"{base_url}{url_word}"

            tipo = inferir_tipo(nombre)
            if cat == "codigos":
                tipo = "Código"
            elif cat == "reglamentos":
                tipo = "Reglamento"

            ley_id = generar_id(entidad, nombre)
            if ley_id in ids_vistos:
                continue
            ids_vistos.add(ley_id)

            leyes.append({
                "id": ley_id,
                "nombre": nombre,
                "tipo": tipo,
                "entidad": entidad,
                "url_pdf": url_pdf,
                "url_word": url_word,
                "ultima_reforma": ultima_reforma if ultima_reforma else fecha_pub,
                "estado_vigencia": "vigente",
                "fuente": "congresoyucatan.gob.mx",
            })
        time.sleep(0.5)

    log.info(f"  Yucatán total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# QUINTANA ROO — congresoqroo.gob.mx/leyes/
# WordPress con PDF links directos en documentos.congresoqroo.gob.mx.
# ──────────────────────────────────────────────

def scrape_quintanaroo() -> list[dict]:
    entidad = "quintanaroo"
    url = "https://www.congresoqroo.gob.mx/leyes/"

    log.info(f"  Quintana Roo: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Quintana Roo: no se pudo acceder al portal")
        return []

    # Extract PDF links with their anchor text (law names)
    entries = re.findall(
        r'<a[^>]+href=["\']'
        r'(https://documentos\.congresoqroo\.gob\.mx/leyes/[^"\']+\.pdf)'
        r'["\'][^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE
    )

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    for url_pdf, text in entries:
        nombre = re.sub(r'<[^>]+>', '', text).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue
        # Remove trailing period
        nombre = nombre.rstrip(".")

        # Skip "Tablas de Valores" (not laws)
        if "Tablas de Valores" in nombre:
            continue

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "congresoqroo.gob.mx",
        })

    log.info(f"  Quintana Roo total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# TLAXCALA — congresodetlaxcala.gob.mx/legislacion/
# Tabla HTML con 206 leyes. Nombre, PDF, DOC, última reforma.
# PDFs en /archivo/leyes2020/pdf/
# ──────────────────────────────────────────────

def scrape_tlaxcala() -> list[dict]:
    entidad = "tlaxcala"
    url = "https://congresodetlaxcala.gob.mx/legislacion/"

    log.info(f"  Tlaxcala: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Tlaxcala: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    meses = {
        "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
        "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
        "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
    }

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue

        # Cell 0: nombre, Cell 1: PDF link, Cell 2: DOC link, Cell 3: última reforma
        nombre = re.sub(r'<[^>]+>', '', cells[0]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or "Ultima Reforma" in nombre:
            continue

        # PDF link
        pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', cells[1] if len(cells) > 1 else "", re.IGNORECASE)
        url_pdf = pdf_match.group(1) if pdf_match else ""
        if url_pdf and not url_pdf.startswith("http"):
            url_pdf = f"https://congresodetlaxcala.gob.mx/archivo/leyes2020/pdf/{url_pdf}"

        # DOC link
        doc_match = re.search(r'href=["\']([^"\']+\.doc[x]?)["\']', cells[2] if len(cells) > 2 else "", re.IGNORECASE)
        url_word = doc_match.group(1) if doc_match else ""
        if url_word and not url_word.startswith("http"):
            url_word = f"https://congresodetlaxcala.gob.mx/archivo/leyes2020/doc/{url_word}"

        # Última reforma (format: DD/Mes/YYYY)
        ultima_reforma = ""
        if len(cells) > 3:
            fecha_raw = re.sub(r'<[^>]+>', '', cells[3]).strip()
            fecha_match = re.match(r'(\d{1,2})/(\w+)/(\d{4})', fecha_raw)
            if fecha_match:
                dia, mes_str, anio = fecha_match.groups()
                mes = meses.get(mes_str.lower(), "01")
                ultima_reforma = f"{anio}-{mes}-{int(dia):02d}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "url_word": url_word,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresodetlaxcala.gob.mx",
        })

    log.info(f"  Tlaxcala total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# MORELOS — marcojuridico.morelos.gob.mx/leyes.jsp
# Tabla HTML con ~140 leyes. Nombre, PDF, última reforma.
# ──────────────────────────────────────────────

def scrape_morelos() -> list[dict]:
    entidad = "morelos"
    base_url = "http://marcojuridico.morelos.gob.mx"
    url = f"{base_url}/leyes.jsp"

    log.info(f"  Morelos: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Morelos: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 3:
            continue

        # Cell 0: número, Cell 1: nombre + PDF link, Cell 2: última reforma
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or nombre.isdigit():
            continue

        # PDF URL from cell 1 (href may contain newlines)
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf)\s*["\']', cells[1], re.IGNORECASE | re.DOTALL)
        url_pdf = ""
        if pdf_match:
            url_pdf = pdf_match.group(1).strip()
            if not url_pdf.startswith("http"):
                url_pdf = f"{base_url}/{url_pdf.lstrip('./')}"

        # Última reforma from cell 2 (DD-MM-YYYY format)
        fecha_raw = re.sub(r'<[^>]+>', '', cells[2]).strip()
        ultima_reforma = ""
        fecha_match = re.match(r'(\d{2})-(\d{2})-(\d{4})', fecha_raw)
        if fecha_match:
            ultima_reforma = f"{fecha_match.group(3)}-{fecha_match.group(2)}-{fecha_match.group(1)}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "marcojuridico.morelos.gob.mx",
        })

    log.info(f"  Morelos total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# AGUASCALIENTES — congresoags.gob.mx/agenda_legislativa/leyes/
# Tabla HTML con ~154 leyes. PDF via /descargarPdf/ID.
# ──────────────────────────────────────────────

def scrape_aguascalientes() -> list[dict]:
    entidad = "aguascalientes"
    base_url = "https://congresoags.gob.mx"
    url = f"{base_url}/agenda_legislativa/leyes/"

    log.info(f"  Aguascalientes: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Aguascalientes: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 4:
            continue

        # Cell 0: número, Cell 1: nombre, Cell 2: pub, Cell 3: reforma, Cell 4: PDF link
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or nombre.isdigit():
            continue

        # Última reforma (DD/MM/YYYY)
        fecha_raw = re.sub(r'<[^>]+>', '', cells[3]).strip() if len(cells) > 3 else ""
        ultima_reforma = ""
        fecha_match = re.match(r'(\d{2})/(\d{2})/(\d{4})', fecha_raw)
        if fecha_match:
            ultima_reforma = f"{fecha_match.group(3)}-{fecha_match.group(2)}-{fecha_match.group(1)}"

        # PDF link (usually in cell 4)
        url_pdf = ""
        if len(cells) > 4:
            pdf_match = re.search(r'href=["\']([^"\']+descargarPdf[^"\']*)["\']', cells[4])
            if pdf_match:
                url_pdf = pdf_match.group(1)
                if not url_pdf.startswith("http"):
                    url_pdf = f"{base_url}{url_pdf}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresoags.gob.mx",
        })

    log.info(f"  Aguascalientes total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# SINALOA — iip.congresosinaloa.gob.mx/estatales.html
# Página con links PDF directos. ~168 leyes.
# PDFs en gaceta.congresosinaloa.gob.mx:3001/pdfs/leyes/
# ──────────────────────────────────────────────

def scrape_sinaloa() -> list[dict]:
    entidad = "sinaloa"
    url = "https://iip.congresosinaloa.gob.mx/estatales.html"

    log.info(f"  Sinaloa: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Sinaloa: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    # Extract all PDF links with their anchor text
    entries = re.findall(
        r'<a[^>]+href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>',
        html, re.DOTALL | re.IGNORECASE
    )

    for url_pdf, text in entries:
        nombre = re.sub(r'<[^>]+>', '', text).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            # Try to extract name from filename
            filename = urllib.parse.unquote(url_pdf.split("/")[-1]).replace(".pdf", "")
            nombre = filename.replace("_", " ").replace("-", " ").strip()
            nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "iip.congresosinaloa.gob.mx",
        })

    log.info(f"  Sinaloa total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# VERACRUZ — legisver.gob.mx/Inicio.php?p=le
# Tabla HTML con ~206 leyes. Nombre + PDF directo + fecha.
# ──────────────────────────────────────────────

def scrape_veracruz() -> list[dict]:
    entidad = "veracruz"
    url = "https://www.legisver.gob.mx/Inicio.php?p=le"

    log.info(f"  Veracruz: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Veracruz: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue

        # Cell 0: nombre + PDF link, Cell 1: fecha
        nombre = re.sub(r'<[^>]+>', '', cells[0]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue

        # PDF link (may contain newlines)
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf)\s*["\']', cells[0], re.IGNORECASE | re.DOTALL)
        url_pdf = pdf_match.group(1).strip() if pdf_match else ""

        # Fecha
        fecha_raw = re.sub(r'<[^>]+>', '', cells[1]).strip() if len(cells) > 1 else ""
        ultima_reforma = ""
        fecha_match = re.match(r'(\d{2})/(\d{2})/(\d{4})', fecha_raw)
        if fecha_match:
            ultima_reforma = f"{fecha_match.group(3)}-{fecha_match.group(2)}-{fecha_match.group(1)}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "legisver.gob.mx",
        })

    log.info(f"  Veracruz total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# CHIAPAS — web.congresochiapas.gob.mx/trabajo-legislativo/legislacion-vigente
# Tabla con ~148 leyes. Nombre + fecha. Sin PDF directo.
# ──────────────────────────────────────────────

def scrape_chiapas() -> list[dict]:
    entidad = "chiapas"
    url = "https://web.congresochiapas.gob.mx/trabajo-legislativo/legislacion-vigente"

    log.info(f"  Chiapas: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Chiapas: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 5:
            continue

        # Cell 0: num, Cell 1: nombre, Cell 4: fecha
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or nombre.isdigit():
            continue

        ultima_reforma = re.sub(r'<[^>]+>', '', cells[4]).strip() if len(cells) > 4 else ""

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": "",
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "web.congresochiapas.gob.mx",
        })

    log.info(f"  Chiapas total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# BAJA CALIFORNIA SUR — cbcs.gob.mx/index.php/trabajos-legislativos/leyes
# Tabla con ~330 filas. Nombre + link DOC. Sin PDF directo.
# ──────────────────────────────────────────────

def scrape_bajacaliforniasur() -> list[dict]:
    entidad = "bajacaliforniasur"
    base_url = "https://www.cbcs.gob.mx"
    url = f"{base_url}/index.php/trabajos-legislativos/leyes"

    log.info(f"  BCS: {url}")
    html = fetch(url)
    if not html:
        log.warning("  BCS: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue

        # Cell 0: número, Cell 1: nombre + link, Cell 2: DOC link
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or nombre.isdigit():
            continue

        # DOC/Word link (cell 2)
        url_word = ""
        if len(cells) > 2:
            doc_match = re.search(r'href=["\']([^"\']+\.doc[x]?)["\']', cells[2], re.IGNORECASE)
            if doc_match:
                url_word = doc_match.group(1)
                if not url_word.startswith("http"):
                    url_word = f"{base_url}{url_word}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": "",
            "url_word": url_word,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "cbcs.gob.mx",
        })

    log.info(f"  BCS total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# MICHOACÁN — congresomich.gob.mx/leyes/ (HTTP)
# Headings h2-h5 con nombres de leyes + PDF links debajo.
# ~100 leyes. Usar HTTP (no HTTPS).
# ──────────────────────────────────────────────

def scrape_michoacan() -> list[dict]:
    entidad = "michoacan"
    base_url = "http://congresomich.gob.mx"
    url = f"{base_url}/leyes/"

    log.info(f"  Michoacán: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Michoacán: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    # Split by headings to associate each law name with its PDF links
    # Pattern: <h2-h5>LAW NAME</h2-h5> ... <a href="...pdf">PDF</a>
    blocks = re.split(r'(?=<h[2-5][^>]*>)', html)

    for block in blocks:
        # Extract heading (law name)
        heading_match = re.search(r'<h[2-5][^>]*>(.*?)</h[2-5]>', block, re.DOTALL)
        if not heading_match:
            continue

        nombre = re.sub(r'<[^>]+>', '', heading_match.group(1)).strip()
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10:
            continue
        # Skip navigation headings
        if any(skip in nombre.lower() for skip in ["menú", "menu", "buscar", "compartir",
                "redes", "contacto", "dirección", "teléfono"]):
            continue

        # Find first PDF link in this block (the main law PDF, not reforms)
        pdf_match = re.search(
            r'<a[^>]+href=["\']([^"\']+\.pdf)["\'][^>]*>\s*PDF',
            block, re.IGNORECASE
        )
        url_pdf = ""
        if pdf_match:
            url_pdf = pdf_match.group(1)
            if not url_pdf.startswith("http"):
                url_pdf = f"{base_url}{url_pdf}" if url_pdf.startswith("/") else f"{base_url}/{url_pdf}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "congresomich.gob.mx",
        })

    log.info(f"  Michoacán total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# NAYARIT — congresonayarit.gob.mx (WP API)
# PDFs extraídos via WordPress REST API.
# ~119 leyes en wp-content/uploads/QUE_HACEMOS/LEGISLACION_ESTATAL/leyes/
# ──────────────────────────────────────────────

def scrape_nayarit() -> list[dict]:
    entidad = "nayarit"
    api_url = "https://congresonayarit.gob.mx/wp-json/wp/v2/pages"

    log.info(f"  Nayarit: {api_url} (buscando páginas con PDFs de leyes)")

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    # Search WP API for pages containing legislation PDFs
    for page_num in range(1, 5):
        url = f"{api_url}?per_page=100&page={page_num}&search=legislacion"
        req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
                import json as json_mod
                pages = json_mod.load(resp)
        except Exception as e:
            log.error(f"  Nayarit API página {page_num}: {e}")
            break

        if not pages:
            break

        for page in pages:
            content = page.get("content", {}).get("rendered", "")
            # Extract PDF links from /leyes/ directory
            pdfs = re.findall(
                r'href=["\']([^"\']*leyes/[^"\']+\.pdf)["\']',
                content, re.IGNORECASE
            )
            for url_pdf in pdfs:
                # Clean escaped slashes from JSON
                url_pdf = url_pdf.replace("\\/", "/")

                # Extract name from filename
                filename = urllib.parse.unquote(url_pdf.split("/")[-1])
                nombre = filename.replace(".pdf", "").replace("_", " ")
                nombre = limpiar_texto(nombre)
                if not nombre or len(nombre) < 10:
                    continue

                tipo = inferir_tipo(nombre)
                ley_id = generar_id(entidad, nombre)
                if ley_id in ids_vistos:
                    continue
                ids_vistos.add(ley_id)

                leyes.append({
                    "id": ley_id,
                    "nombre": nombre,
                    "tipo": tipo,
                    "entidad": entidad,
                    "url_pdf": url_pdf,
                    "ultima_reforma": "",
                    "estado_vigencia": "vigente",
                    "fuente": "congresonayarit.gob.mx",
                })

    log.info(f"  Nayarit total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# PUEBLA — ojp.puebla.gob.mx (Orden Jurídico Poblano)
# K2/Joomla paginado. PDFs en /media/k2/attachments/.
# Paginación: ?catid=19&start=0,20,40,...
# ──────────────────────────────────────────────

def scrape_puebla() -> list[dict]:
    entidad = "puebla"
    base_url = "https://ojp.puebla.gob.mx"

    leyes: list[dict] = []
    ids_vistos: set[str] = set()
    start = 0

    while True:
        url = f"{base_url}/legislaciondelestado?catid=19&start={start}"
        log.info(f"  Puebla: {url}")
        html = fetch(url)
        if not html:
            break

        # Extract PDF links from /media/k2/attachments/
        pdfs = re.findall(
            r'href=["\']([^"\']*media/k2/attachments/[^"\']+\.pdf)["\']',
            html, re.IGNORECASE
        )

        if not pdfs:
            break

        for pdf_path in pdfs:
            url_pdf = pdf_path
            if not url_pdf.startswith("http"):
                url_pdf = f"{base_url}{url_pdf}"

            # Extract name from filename
            filename = urllib.parse.unquote(pdf_path.split("/")[-1])
            nombre = filename.replace(".pdf", "").replace("_", " ")
            # Remove trailing reform dates like "T7 03102024"
            nombre = re.sub(r'\s+T\d+\s+\d{8}$', '', nombre)
            nombre = re.sub(r'\s+\d{8}$', '', nombre)
            nombre = limpiar_texto(nombre)
            if not nombre or len(nombre) < 10:
                continue

            tipo = inferir_tipo(nombre)
            ley_id = generar_id(entidad, nombre)
            if ley_id in ids_vistos:
                continue
            ids_vistos.add(ley_id)

            leyes.append({
                "id": ley_id,
                "nombre": nombre,
                "tipo": tipo,
                "entidad": entidad,
                "url_pdf": url_pdf,
                "ultima_reforma": "",
                "estado_vigencia": "vigente",
                "fuente": "ojp.puebla.gob.mx",
            })

        # Check for next page
        if f"start={start + 20}" not in html:
            break
        start += 20
        time.sleep(0.5)

    log.info(f"  Puebla total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# QUERÉTARO — legislaturaqueretaro.gob.mx/leyes/
# Tabla HTML con ~118 leyes. PDFs en site.legislaturaqueretaro.gob.mx.
# ──────────────────────────────────────────────

def scrape_queretaro() -> list[dict]:
    entidad = "queretaro"
    url = "http://legislaturaqueretaro.gob.mx/leyes/"

    log.info(f"  Querétaro: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Querétaro: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 2:
            continue

        # Cell 0: num, Cell 1: nombre, Cell 2: PDF link
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip() if len(cells) > 1 else ""
        nombre = limpiar_texto(nombre)
        if not nombre or len(nombre) < 10 or nombre.isdigit():
            continue

        # PDF link
        url_pdf = ""
        for cell in cells:
            pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', cell, re.IGNORECASE)
            if pdf_match:
                url_pdf = pdf_match.group(1)
                break

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": "",
            "estado_vigencia": "vigente",
            "fuente": "legislaturaqueretaro.gob.mx",
        })

    log.info(f"  Querétaro total: {len(leyes)} documentos")
    return leyes


# ──────────────────────────────────────────────
# HIDALGO — congresohidalgo.gob.mx/acervo_legislativo/leyes/
# Tabla HTML con ~173 leyes. Nombre, publicación, última reforma, PDF.
# ──────────────────────────────────────────────

def scrape_hidalgo() -> list[dict]:
    entidad = "hidalgo"
    base_url = "https://congresohidalgo.gob.mx"
    url = f"{base_url}/acervo_legislativo/leyes/"

    log.info(f"  Hidalgo: {url}")
    html = fetch(url)
    if not html:
        log.warning("  Hidalgo: no se pudo acceder al portal")
        return []

    leyes: list[dict] = []
    ids_vistos: set[str] = set()

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)

    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        if len(cells) < 5:
            continue

        # Cell 0: número, Cell 1: nombre, Cell 2: publicación, Cell 3: última reforma, Cell 4: PDF
        nombre = re.sub(r'<[^>]+>', '', cells[1]).strip()
        nombre = limpiar_texto(nombre).rstrip(".")
        if not nombre or len(nombre) < 10:
            continue

        # Última reforma
        reforma_raw = re.sub(r'<[^>]+>', ' ', cells[3]).strip()
        reforma_raw = re.sub(r'\s+', ' ', reforma_raw)
        ultima_reforma = ""
        # Try to extract date (various formats)
        fecha_match = re.search(r'(\d{1,2})\s+(?:de\s+)?(\w+)\s+(\d{4})', reforma_raw)
        if fecha_match:
            dia, mes_str, anio = fecha_match.groups()
            meses = {
                "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
                "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
                "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
            }
            mes = meses.get(mes_str.lower(), "")
            if mes:
                ultima_reforma = f"{anio}-{mes}-{int(dia):02d}"

        # PDF URL
        pdf_match = re.search(r'href=["\']([^"\']+\.pdf)["\']', cells[4], re.IGNORECASE)
        url_pdf = ""
        if pdf_match:
            url_pdf = pdf_match.group(1)
            if not url_pdf.startswith("http"):
                url_pdf = f"{base_url}/acervo_legislativo/leyes/{url_pdf}"

        tipo = inferir_tipo(nombre)
        ley_id = generar_id(entidad, nombre)
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        leyes.append({
            "id": ley_id,
            "nombre": nombre,
            "tipo": tipo,
            "entidad": entidad,
            "url_pdf": url_pdf,
            "ultima_reforma": ultima_reforma,
            "estado_vigencia": "vigente",
            "fuente": "congresohidalgo.gob.mx",
        })

    log.info(f"  Hidalgo total: {len(leyes)} documentos")
    return leyes


# ══════════════════════════════════════════════
# REGISTRO DE SCRAPERS DISPONIBLES
# ══════════════════════════════════════════════

SCRAPERS: dict[str, callable] = {
    "guanajuato":     scrape_guanajuato,
    "nuevoleon":      scrape_nuevoleon,
    "edomex":         scrape_edomex,
    "federal":        scrape_federal,
    "tamaulipas":     scrape_tamaulipas,
    "chihuahua":      scrape_chihuahua,
    "durango":        scrape_durango,
    "tabasco":        scrape_tabasco,
    "oaxaca":         scrape_oaxaca,
    "bajacalifornia": scrape_bajacalifornia,
    "yucatan":        scrape_yucatan,
    "quintanaroo":    scrape_quintanaroo,
    "tlaxcala":       scrape_tlaxcala,
    "hidalgo":        scrape_hidalgo,
    "morelos":        scrape_morelos,
    "aguascalientes": scrape_aguascalientes,
    "sinaloa":        scrape_sinaloa,
    "veracruz":       scrape_veracruz,
    "chiapas":        scrape_chiapas,
    "bajacaliforniasur": scrape_bajacaliforniasur,
    "queretaro":         scrape_queretaro,
    "puebla":            scrape_puebla,
    "nayarit":           scrape_nayarit,
    "michoacan":         scrape_michoacan,
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
