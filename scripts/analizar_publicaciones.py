#!/usr/bin/env python3
"""
Análisis inteligente de publicaciones oficiales con LLM (Claude API).
Fase 8A del Sistema de Vigilancia Legislativa Mexicana.

Recibe publicaciones detectadas por vigilancia_dof.py y usa Claude
para determinar qué leyes del catálogo afectan y qué acción tomar.

Uso:
    python3 analizar_publicaciones.py                    # Analiza cola pendiente
    python3 analizar_publicaciones.py --archivo pub.json # Analiza archivo específico
    python3 analizar_publicaciones.py --dry-run          # Solo clasifica, no ejecuta
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date
from html.parser import HTMLParser
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Error: pip install anthropic", file=sys.stderr)
    sys.exit(1)

# ──────────────────────────────────────────────
# Rutas
# ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

COLA_PROCESAMIENTO = LOGS_DIR / "cola_procesamiento.json"
ACCIONES_LOG = LOGS_DIR / "acciones_llm.json"
INDEX_FILE = BASE_DIR / "leyes_index.json"

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "analisis_llm.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Cargar catálogos para dar contexto al LLM
# ──────────────────────────────────────────────
def cargar_catalogos() -> dict[str, list[dict]]:
    """Carga todos los catalogo.json por entidad."""
    catalogos = {}

    # Federal
    federal_cat = BASE_DIR / "federal" / "catalogo.json"
    if federal_cat.exists():
        with open(federal_cat) as f:
            catalogos["federal"] = json.load(f)

    # Estados
    estados_dir = BASE_DIR / "estados"
    if estados_dir.exists():
        for estado_dir in sorted(estados_dir.iterdir()):
            cat_file = estado_dir / "catalogo.json"
            if cat_file.exists():
                with open(cat_file) as f:
                    catalogos[estado_dir.name] = json.load(f)

    return catalogos


def nombres_leyes_entidad(catalogos: dict, entidad: str) -> list[str]:
    """Extrae solo los nombres de leyes de una entidad (para el prompt)."""
    if entidad not in catalogos:
        return []
    return [ley["nombre"] for ley in catalogos[entidad]]


def buscar_en_catalogo(catalogos: dict, entidad: str, id_catalogo: str) -> dict | None:
    """Busca una ley específica en el catálogo por ID."""
    if entidad not in catalogos:
        return None
    for ley in catalogos[entidad]:
        if ley.get("id") == id_catalogo:
            return ley
    return None


# ──────────────────────────────────────────────
# Prompt para el LLM
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """\
Eres un analista jurídico especializado en derecho mexicano. Tu trabajo es clasificar
publicaciones del Diario Oficial de la Federación (DOF) y de los periódicos oficiales
estatales para determinar si afectan leyes existentes en nuestro catálogo legislativo.

REGLAS:
1. Solo clasifica actos que afecten leyes, códigos o reglamentos vigentes.
2. Si el título no es un acto legislativo relevante (nombramientos, convocatorias,
   licitaciones, avisos, etc.), clasifícalo como tipo_acto: "irrelevante".
3. Para reformas y adiciones, identifica la ley afectada con su nombre EXACTO.
4. El id_catalogo debe coincidir con el formato: {entidad}_{nombre_abreviado}.
5. Si no puedes determinar con certeza qué ley afecta, usa confianza baja (<0.5).

TIPOS DE ACTO VÁLIDOS:
- reforma: Se modifican artículos existentes de una ley
- adicion: Se agregan artículos nuevos a una ley existente
- derogacion: Se eliminan artículos específicos de una ley
- abrogacion: Se elimina completamente una ley (sustituida por otra)
- ley_nueva: Se crea una ley completamente nueva
- fe_de_erratas: Corrección de errores de publicación
- irrelevante: No afecta legislación (nombramientos, avisos, etc.)

ACCIONES RECOMENDADAS:
- re-descargar: La ley fue reformada, hay que bajar la versión actualizada
- agregar: Ley nueva, hay que agregarla al catálogo
- marcar-abrogada: La ley fue abrogada, marcar como no vigente
- actualizar-catalogo: Cambio menor que requiere actualizar metadatos
- ignorar: No requiere acción sobre el catálogo

Si un decreto afecta MÚLTIPLES leyes, responde con un JSON ARRAY con un objeto por cada ley afectada.
Si afecta una sola ley, responde con un solo objeto JSON.
Responde SIEMPRE en JSON válido, sin texto adicional."""


# ──────────────────────────────────────────────
# Extracción de contenido de documentos
# ──────────────────────────────────────────────
MAX_CONTENIDO_CHARS = 15000


class _TextExtractor(HTMLParser):
    """Extrae texto plano de HTML."""
    def __init__(self):
        super().__init__()
        self._textos: list[str] = []

    def handle_data(self, data):
        t = data.strip()
        if t:
            self._textos.append(t)

    def get_text(self) -> str:
        return " ".join(self._textos)


def _extraer_texto_pdf(ruta: Path) -> str:
    """Extrae texto de un PDF usando PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.debug("PyMuPDF no instalado, no se puede extraer texto de PDF")
        return ""
    try:
        doc = fitz.open(str(ruta))
        textos = []
        for page in doc:
            textos.append(page.get_text())
        doc.close()
        return " ".join(textos)
    except Exception as e:
        log.warning(f"Error extrayendo texto de PDF {ruta}: {e}")
        return ""


def _extraer_texto_html(ruta: Path) -> str:
    """Extrae texto plano de un archivo HTML."""
    try:
        contenido = ruta.read_text(encoding="utf-8", errors="replace")
        parser = _TextExtractor()
        parser.feed(contenido)
        return parser.get_text()
    except Exception as e:
        log.warning(f"Error extrayendo texto de HTML {ruta}: {e}")
        return ""


def extraer_contenido(publicacion: dict) -> str:
    """Extrae contenido textual del archivo local de una publicación."""
    archivo = publicacion.get("archivo_local")
    if not archivo:
        return ""
    ruta = Path(archivo)
    if not ruta.exists():
        return ""

    sufijo = ruta.suffix.lower()
    if sufijo == ".pdf":
        texto = _extraer_texto_pdf(ruta)
    elif sufijo in (".html", ".htm"):
        texto = _extraer_texto_html(ruta)
    else:
        # DOC/DOCX u otros formatos: no extraer
        return ""

    return texto[:MAX_CONTENIDO_CHARS] if texto else ""


def construir_prompt_usuario(publicacion: dict, nombres_leyes: list[str], contenido_extraido: str = "") -> str:
    """Construye el prompt con la publicación y contexto del catálogo."""
    titulo = publicacion.get("titulo", "")
    entidad = publicacion.get("entidad", "federal")
    fecha = publicacion.get("fecha", "")
    url = publicacion.get("url", "")

    leyes_texto = ""
    if nombres_leyes:
        leyes_texto = "\n".join(f"  - {n}" for n in nombres_leyes)

    contenido_seccion = ""
    if contenido_extraido:
        contenido_seccion = f"\n\nCONTENIDO DEL DOCUMENTO:\n{contenido_extraido}"

    return f"""Analiza esta publicación oficial y clasifícala:

PUBLICACIÓN:
  Título: {titulo}
  Entidad: {entidad}
  Fecha: {fecha}
  URL: {url}{contenido_seccion}

{"LEYES EN NUESTRO CATÁLOGO PARA " + entidad.upper() + ":" + chr(10) + leyes_texto if leyes_texto else "No tenemos catálogo para esta entidad aún."}

Responde con un JSON con esta estructura exacta:
{{
  "tipo_acto": "reforma|adicion|derogacion|abrogacion|ley_nueva|fe_de_erratas|irrelevante",
  "ley_afectada": "Nombre completo de la ley afectada (o null si irrelevante)",
  "id_catalogo": "entidad_nombre_abreviado (o null si no se puede determinar)",
  "entidad": "{entidad}",
  "articulos_afectados": "lista de artículos si se mencionan (o null)",
  "accion_recomendada": "re-descargar|agregar|marcar-abrogada|actualizar-catalogo|ignorar",
  "resumen": "Breve explicación de lo que hace esta publicación",
  "confianza": 0.0
}}"""


# ──────────────────────────────────────────────
# Llamada al LLM
# ──────────────────────────────────────────────
def analizar_con_llm(
    client: anthropic.Anthropic,
    publicacion: dict,
    catalogos: dict,
) -> dict | None:
    """Envía una publicación al LLM y obtiene la clasificación."""
    entidad = publicacion.get("entidad", "federal")
    nombres = nombres_leyes_entidad(catalogos, entidad)
    contenido = extraer_contenido(publicacion)

    prompt = construir_prompt_usuario(publicacion, nombres, contenido)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        texto = response.content[0].text.strip()

        # Extraer JSON de la respuesta (puede venir envuelto en ```json)
        if texto.startswith("```"):
            lines = texto.split("\n")
            texto = "\n".join(lines[1:-1])

        resultados = _parse_json_flexible(texto)
        if not resultados:
            log.error(f"LLM devolvió respuesta no parseable para '{publicacion.get('titulo', '?')}'")
            log.debug(f"Respuesta raw: {texto[:500]}")
            return None

        # Si el LLM devolvió múltiples objetos (decreto que afecta varias leyes),
        # retornamos una lista; si es uno solo, retornamos el objeto directamente
        for r in resultados:
            r["_publicacion_original"] = publicacion
            r["_fecha_analisis"] = datetime.now().isoformat()
            r["_tokens_input"] = response.usage.input_tokens
            r["_tokens_output"] = response.usage.output_tokens

        return resultados if len(resultados) > 1 else resultados[0]

    except anthropic.APIError as e:
        log.error(f"Error API: {e}")
        return None


def _parse_json_flexible(texto: str) -> list[dict]:
    """Parsea JSON que puede ser un objeto, un array, o múltiples objetos concatenados."""
    texto = texto.strip()

    # Caso 1: Array JSON
    if texto.startswith("["):
        try:
            parsed = json.loads(texto)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            pass

    # Caso 2: Objeto JSON único
    try:
        return [json.loads(texto)]
    except json.JSONDecodeError:
        pass

    # Caso 3: Múltiples objetos JSON concatenados (separados por líneas)
    results = []
    depth = 0
    start = None
    for i, c in enumerate(texto):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(texto[start:i+1])
                    results.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None

    return results


# ──────────────────────────────────────────────
# Ejecutar acciones
# ──────────────────────────────────────────────
def ejecutar_acciones(resultados: list[dict], catalogos: dict, dry_run: bool = False):
    """Ejecuta las acciones recomendadas por el LLM."""
    acciones_pendientes = []

    for r in resultados:
        accion = r.get("accion_recomendada", "ignorar")
        confianza = r.get("confianza", 0)
        tipo = r.get("tipo_acto", "irrelevante")

        if accion == "ignorar" or tipo == "irrelevante":
            continue

        if confianza < 0.6:
            log.warning(
                f"  Confianza baja ({confianza}) para: {r.get('ley_afectada', '?')} — "
                f"requiere revisión manual"
            )
            r["_requiere_revision"] = True

        log.info(
            f"  ACCIÓN: {accion} | {r.get('ley_afectada', '?')} | "
            f"tipo={tipo} | confianza={confianza}"
        )
        acciones_pendientes.append(r)

        if dry_run:
            continue

        # Ejecutar acción concreta
        if accion == "re-descargar":
            _accion_redescargar(r, catalogos)
        elif accion == "agregar":
            _accion_agregar(r, catalogos)
        elif accion == "marcar-abrogada":
            _accion_marcar_abrogada(r, catalogos)
        elif accion == "actualizar-catalogo":
            log.info(f"    → Actualización de catálogo pendiente para: {r.get('ley_afectada')}")

    # Guardar log de acciones
    _guardar_acciones(acciones_pendientes)
    return acciones_pendientes


def _accion_redescargar(resultado: dict, catalogos: dict):
    """Marca una ley para re-descarga actualizando su estado en el índice."""
    id_cat = resultado.get("id_catalogo")
    if not id_cat:
        log.warning("    → No se puede re-descargar: falta id_catalogo")
        return

    # Actualizar el leyes_index.json marcando la ley como pendiente
    if not INDEX_FILE.exists():
        log.warning("    → leyes_index.json no existe")
        return

    with open(INDEX_FILE) as f:
        indice = json.load(f)

    pub = resultado.get("_publicacion_original", {})
    encontrada = False
    for ley in indice:
        if ley.get("id") == id_cat:
            ley["estado"] = "pendiente_actualizacion"
            ley["_motivo_actualizacion"] = resultado.get("resumen", "Reforma detectada por LLM")
            ley["_fecha_deteccion"] = datetime.now().isoformat()
            ley["_intentos_actualizacion"] = 0
            ley["_url_dof"] = pub.get("url", "")
            ley["_tipo_acto"] = resultado.get("tipo_acto", "")
            ley["_ley_afectada"] = resultado.get("ley_afectada", "")
            encontrada = True
            log.info(f"    → Marcada para re-descarga: {id_cat}")
            break

    if not encontrada:
        log.warning(f"    → ID no encontrado en índice: {id_cat}")
        return

    with open(INDEX_FILE, "w") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)


def _accion_agregar(resultado: dict, catalogos: dict):
    """Registra una ley nueva para agregar al catálogo."""
    entidad = resultado.get("entidad", "federal")
    nombre = resultado.get("ley_afectada", "")
    pub = resultado.get("_publicacion_original", {})

    nueva_ley = {
        "nombre": nombre,
        "tipo": resultado.get("tipo_acto", "ley_nueva"),
        "entidad": entidad,
        "url_publicacion": pub.get("url", ""),
        "fecha_publicacion": pub.get("fecha", ""),
        "estado": "pendiente_catalogo",
        "_detectada_por": "analizar_publicaciones.py",
        "_fecha_deteccion": datetime.now().isoformat(),
    }

    # Guardar en un archivo de leyes nuevas detectadas
    nuevas_file = LOGS_DIR / "leyes_nuevas_detectadas.json"
    existentes = []
    if nuevas_file.exists():
        with open(nuevas_file) as f:
            existentes = json.load(f)
    existentes.append(nueva_ley)
    with open(nuevas_file, "w") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)
    log.info(f"    → Ley nueva registrada: {nombre}")


def _accion_marcar_abrogada(resultado: dict, catalogos: dict):
    """Marca una ley como abrogada en el índice."""
    id_cat = resultado.get("id_catalogo")
    if not id_cat:
        log.warning("    → No se puede marcar abrogada: falta id_catalogo")
        return

    if not INDEX_FILE.exists():
        return

    with open(INDEX_FILE) as f:
        indice = json.load(f)

    for ley in indice:
        if ley.get("id") == id_cat:
            ley["estado_vigencia"] = "abrogada"
            ley["_abrogacion_detectada"] = datetime.now().isoformat()
            ley["_motivo"] = resultado.get("resumen", "Abrogación detectada por LLM")
            log.info(f"    → Marcada como abrogada: {id_cat}")
            break

    with open(INDEX_FILE, "w") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)


def _guardar_acciones(acciones: list[dict]):
    """Guarda el log de acciones del LLM, deduplicando por url + id_catalogo."""
    existentes = []
    if ACCIONES_LOG.exists():
        with open(ACCIONES_LOG) as f:
            existentes = json.load(f)
    claves_existentes = {
        (a.get("_publicacion_original", {}).get("url", ""), a.get("id_catalogo", ""))
        for a in existentes
    }
    nuevas = [
        a for a in acciones
        if (a.get("_publicacion_original", {}).get("url", ""), a.get("id_catalogo", ""))
        not in claves_existentes
    ]
    existentes.extend(nuevas)
    with open(ACCIONES_LOG, "w") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# Cargar publicaciones pendientes
# ──────────────────────────────────────────────
def cargar_publicaciones(archivo: Path | None = None) -> list[dict]:
    """Carga publicaciones de un archivo o de la cola por defecto."""
    if archivo:
        target = Path(archivo)
    else:
        target = COLA_PROCESAMIENTO

    if not target.exists():
        log.info(f"No hay publicaciones pendientes en {target}")
        return []

    with open(target) as f:
        publicaciones = json.load(f)

    log.info(f"Cargadas {len(publicaciones)} publicaciones de {target}")
    return publicaciones


def marcar_procesadas(publicaciones: list[dict]):
    """Mueve publicaciones procesadas a un archivo de histórico."""
    if not publicaciones:
        return

    historico_file = LOGS_DIR / f"publicaciones_procesadas_{date.today().isoformat()}.json"
    existentes = []
    if historico_file.exists():
        with open(historico_file) as f:
            existentes = json.load(f)
    existentes.extend(publicaciones)
    with open(historico_file, "w") as f:
        json.dump(existentes, f, ensure_ascii=False, indent=2)

    # Limpiar la cola
    if COLA_PROCESAMIENTO.exists():
        with open(COLA_PROCESAMIENTO) as f:
            cola = json.load(f)
        # Remover las procesadas
        titulos_procesados = {p.get("titulo") for p in publicaciones}
        cola = [p for p in cola if p.get("titulo") not in titulos_procesados]
        with open(COLA_PROCESAMIENTO, "w") as f:
            json.dump(cola, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Analiza publicaciones oficiales con LLM para detectar cambios legislativos"
    )
    parser.add_argument(
        "--archivo",
        help="Archivo JSON con publicaciones a analizar (default: cola_procesamiento.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo clasificar, no ejecutar acciones sobre el catálogo",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Máximo de publicaciones a analizar (0 = todas)",
    )
    args = parser.parse_args()

    # Verificar API key
    from dotenv import load_dotenv
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY no encontrada. Configurar en .env o variable de entorno.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Cargar datos
    log.info("=== Análisis de publicaciones con LLM ===")
    publicaciones = cargar_publicaciones(args.archivo)
    if not publicaciones:
        log.info("No hay publicaciones pendientes de análisis.")
        return

    if args.limite > 0:
        publicaciones = publicaciones[:args.limite]
        log.info(f"Limitando a {args.limite} publicaciones")

    catalogos = cargar_catalogos()
    log.info(
        f"Catálogos cargados: {len(catalogos)} entidades, "
        f"{sum(len(v) for v in catalogos.values())} leyes totales"
    )

    # Analizar cada publicación
    resultados = []
    irrelevantes = 0
    errores = 0
    tokens_total = 0

    for i, pub in enumerate(publicaciones, 1):
        titulo = pub.get("titulo", "?")
        log.info(f"[{i}/{len(publicaciones)}] Analizando: {titulo}...")

        raw = analizar_con_llm(client, pub, catalogos)
        if raw is None:
            errores += 1
            continue

        # Normalizar: siempre trabajar con lista
        items = raw if isinstance(raw, list) else [raw]

        for resultado in items:
            tokens_total += resultado.get("_tokens_input", 0) + resultado.get("_tokens_output", 0)

            if resultado.get("tipo_acto") == "irrelevante":
                irrelevantes += 1
                log.info(f"  → Irrelevante: {resultado.get('resumen', '')[:60]}")
            else:
                log.info(
                    f"  → {resultado.get('tipo_acto')}: {resultado.get('ley_afectada', '?')} "
                    f"(confianza: {resultado.get('confianza', 0)})"
                )
                resultados.append(resultado)

    # Ejecutar acciones
    log.info(f"\n{'='*60}")
    log.info(
        f"Análisis completado: {len(resultados)} relevantes, "
        f"{irrelevantes} irrelevantes, {errores} errores"
    )
    log.info(f"Tokens consumidos: {tokens_total:,}")

    if resultados:
        log.info(f"\nEjecutando acciones {'(DRY RUN)' if args.dry_run else ''}:")
        acciones = ejecutar_acciones(resultados, catalogos, dry_run=args.dry_run)
        log.info(f"\n{len(acciones)} acción(es) registradas")

    # Marcar como procesadas
    if not args.dry_run:
        marcar_procesadas(publicaciones)

    log.info("=== Análisis finalizado ===")


if __name__ == "__main__":
    main()
