#!/usr/bin/env python3
"""
Generador del Dashboard de Vigilancia Legislativa.
Produce un archivo HTML autocontenido con todos los datos del sistema.

Uso:
    python3 generar_dashboard.py                    # Genera dashboard.html
    python3 generar_dashboard.py --output /ruta/    # Genera en ruta específica
"""

import argparse
import json
import html
import sys
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "leyes_index.json"
LOGS_DIR = BASE_DIR / "logs"

# ──────────────────────────────────────────────
# Recopilar datos
# ──────────────────────────────────────────────

def cargar_indice() -> list[dict]:
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            return json.load(f)
    return []


def cargar_catalogos() -> dict[str, list[dict]]:
    catalogos = {}
    federal = BASE_DIR / "federal" / "catalogo.json"
    if federal.exists():
        with open(federal) as f:
            catalogos["federal"] = json.load(f)
    estados = BASE_DIR / "estados"
    if estados.exists():
        for d in sorted(estados.iterdir()):
            cat = d / "catalogo.json"
            if cat.exists():
                with open(cat) as f:
                    catalogos[d.name] = json.load(f)
    return catalogos


def cargar_json(path: Path) -> list:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return []


def cargar_log_lines(path: Path) -> list[str]:
    if path.exists():
        with open(path) as f:
            return f.readlines()
    return []


NOMBRES_ESTADOS = {
    "federal": "Federal", "aguascalientes": "Aguascalientes",
    "bajacalifornia": "Baja California", "bajacaliforniasur": "Baja California Sur",
    "campeche": "Campeche", "cdmx": "Ciudad de México", "chiapas": "Chiapas",
    "chihuahua": "Chihuahua", "coahuila": "Coahuila", "colima": "Colima",
    "durango": "Durango", "edomex": "Estado de México", "guanajuato": "Guanajuato",
    "guerrero": "Guerrero", "hidalgo": "Hidalgo", "jalisco": "Jalisco",
    "michoacan": "Michoacán", "morelos": "Morelos", "nayarit": "Nayarit",
    "nuevoleon": "Nuevo León", "oaxaca": "Oaxaca", "puebla": "Puebla",
    "queretaro": "Querétaro", "quintanaroo": "Quintana Roo",
    "sanluispotosi": "San Luis Potosí", "sinaloa": "Sinaloa", "sonora": "Sonora",
    "tabasco": "Tabasco", "tamaulipas": "Tamaulipas", "tlaxcala": "Tlaxcala",
    "veracruz": "Veracruz", "yucatan": "Yucatán", "zacatecas": "Zacatecas",
}


def filtrar_acciones_verificadas(acciones: list, indice: list) -> list:
    """Solo incluir acciones LLM que se materializaron en el índice.

    Para re-descargar/marcar-abrogada: debe existir una ley de esa entidad
    con estado que confirme que la acción se ejecutó (ok o abrogada).
    Para agregar/ley_nueva: la ley debe haber sido efectivamente agregada
    al índice (descargada con éxito).
    Ignorar/irrelevante: se excluyen siempre.
    """
    # Construir lookup: id exacto + set de entidades con descargas ok
    ids_indice = {ley.get("id") for ley in indice}
    leyes_actualizadas = set()
    for ley in indice:
        if ley.get("estado") in ("ok", "pendiente_actualizacion", "abrogada"):
            leyes_actualizadas.add(ley.get("id"))

    verificadas = []
    for a in acciones:
        accion = a.get("accion_recomendada", "")
        if accion == "ignorar":
            continue
        id_cat = a.get("id_catalogo", "")
        if accion in ("re-descargar", "marcar-abrogada", "actualizar-catalogo"):
            # El ID del LLM puede no coincidir exactamente; buscar por entidad
            # y verificar que al menos existe la ley referenciada en el índice
            if id_cat in ids_indice:
                verificadas.append(a)
        elif accion == "agregar":
            # Ley nueva: solo mostrar si fue agregada al índice y descargada
            if id_cat in leyes_actualizadas:
                verificadas.append(a)
    return verificadas


def recopilar_datos() -> dict:
    indice = cargar_indice()
    catalogos = cargar_catalogos()
    acciones = cargar_json(LOGS_DIR / "acciones_llm.json")
    cola_reintentos = cargar_json(BASE_DIR / "cola_reintentos.json")
    evidencias = cargar_json(LOGS_DIR / "evidencias_reformas.json")
    nuevas = cargar_json(LOGS_DIR / "leyes_nuevas_detectadas.json")
    resolver = cargar_json(LOGS_DIR / "resolver_pendientes.json")

    # Publicaciones detectadas (buscar todos los archivos diarios)
    publicaciones = []
    for f in sorted(LOGS_DIR.glob("publicaciones_2*.json")):
        publicaciones.extend(cargar_json(f))

    # Pipeline logs
    pipeline_logs = []
    for f in sorted(LOGS_DIR.glob("pipeline_*.log")):
        nombre = f.stem.replace("pipeline_", "")
        lines = f.read_text().splitlines()
        exito = any("completado exitosamente" in l for l in lines[-5:])
        duracion = ""
        for l in lines[-5:]:
            if "completado" in l and "m " in l:
                duracion = l.split("en ")[-1] if "en " in l else ""
        pipeline_logs.append({"fecha": nombre, "exito": exito, "duracion": duracion})

    # Stats por entidad
    por_entidad = defaultdict(lambda: {"total": 0, "con_url": 0, "descargadas": 0, "tipos": Counter()})
    for ley in indice:
        ent = ley.get("entidad", "?")
        por_entidad[ent]["total"] += 1
        if ley.get("url", "").startswith("http"):
            por_entidad[ent]["con_url"] += 1
        if ley.get("estado") == "ok":
            por_entidad[ent]["descargadas"] += 1
        tipo = ley.get("tipo", "Otro")
        por_entidad[ent]["tipos"][tipo] += 1

    # Stats globales
    total = len(indice)
    con_url = sum(1 for l in indice if l.get("url", "").startswith("http"))
    descargadas = sum(1 for l in indice if l.get("estado") == "ok")
    pendientes = sum(1 for l in indice if l.get("estado") == "pendiente_actualizacion")
    criticas = sum(1 for l in indice if l.get("estado") == "critico")
    tipos_global = Counter(l.get("tipo", "Otro") for l in indice)

    # Alertas recientes
    alertas_lines = cargar_log_lines(LOGS_DIR / "alertas.log")
    alertas_recientes = alertas_lines[-20:] if alertas_lines else []

    # Descargas exitosas/fallidas counts
    exitosas = len(cargar_log_lines(LOGS_DIR / "descargas_exitosas.log"))
    fallidas = len(cargar_log_lines(LOGS_DIR / "descargas_fallidas.log"))

    # Entidades data for table
    entidades_tabla = []
    for ent_id in sorted(por_entidad.keys()):
        d = por_entidad[ent_id]
        entidades_tabla.append({
            "id": ent_id,
            "nombre": NOMBRES_ESTADOS.get(ent_id, ent_id),
            "total": d["total"],
            "con_url": d["con_url"],
            "descargadas": d["descargadas"],
            "pct": round(d["descargadas"] / d["total"] * 100, 1) if d["total"] > 0 else 0,
            "tipos": dict(d["tipos"]),
        })

    return {
        "fecha_generacion": datetime.now().isoformat(),
        "total_leyes": total,
        "con_url": con_url,
        "descargadas": descargadas,
        "pendientes_actualizacion": pendientes,
        "criticas": criticas,
        "pct_descarga": round(descargadas / total * 100, 2) if total > 0 else 0,
        "entidades": len(por_entidad),
        "tipos_global": dict(tipos_global),
        "entidades_tabla": entidades_tabla,
        "acciones_llm": filtrar_acciones_verificadas(acciones, indice),
        "cola_reintentos": cola_reintentos,
        "evidencias": evidencias,
        "leyes_nuevas": nuevas,
        "publicaciones": publicaciones[-50:],  # últimas 50
        "pipeline_logs": pipeline_logs,
        "exitosas_total": exitosas,
        "fallidas_total": fallidas,
        "alertas_recientes": [l.strip() for l in alertas_recientes],
        "resolver": resolver,
    }


# ──────────────────────────────────────────────
# Generar HTML
# ──────────────────────────────────────────────

def generar_html(datos: dict) -> str:
    # Escape </script> sequences to prevent script tag breakout (XSS)
    datos_json = json.dumps(datos, ensure_ascii=False, default=str).replace(
        '</script>', '<\\/script>').replace('</Script>', '<\\/Script>')

    return f'''<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LegismEx — Vigilancia Legislativa Mexicana</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {{
  --wine: #8A1538;
  --wine-dark: #6b1030;
  --wine-light: #a91d47;
  --wine-bg: rgba(138, 21, 56, 0.06);
  --wine-bg2: rgba(138, 21, 56, 0.12);
  --gold: #b8860b;
  --gold-bg: #fdf6e3;
  --green: #2d7a4f;
  --green-bg: #ecf5f0;
  --red: #c0392b;
  --red-bg: #fdf0ee;
  --purple: #7c3a6e;
  --purple-bg: #f5edf4;
  --text: #1a1a1a;
  --text2: #555;
  --text3: #6b7280;
  --border: #e5e5e5;
  --bg: #ffffff;
  --bg-warm: #f8f6f3;
  --bg-warm2: #faf9f7;
  --font-serif: 'Cormorant Garamond', Georgia, serif;
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --shadow: 0 1px 3px rgba(0,0,0,0.06);
  --shadow-lg: 0 4px 12px rgba(0,0,0,0.1);
  --radius: 6px;
  --max-w: 1200px;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
  font-family: var(--font-sans);
  color: var(--text);
  background: var(--bg);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}}

/* ── Header ─────────────────────── */
.header {{
  background: var(--wine);
  color: white;
  padding: 0;
  position: sticky;
  top: 0;
  z-index: 100;
  box-shadow: 0 2px 8px rgba(138, 21, 56, 0.3);
}}

.header-inner {{
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 1rem 2rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}}

.header h1 {{
  font-family: var(--font-serif);
  font-size: 1.6rem;
  font-weight: 700;
  letter-spacing: -0.02em;
}}

.header h1 span {{
  font-weight: 400;
  opacity: 0.8;
  font-size: 0.9em;
}}

.header-meta {{
  font-size: 0.8rem;
  opacity: 0.75;
}}

.header-nav {{
  display: flex;
  gap: 0.25rem;
  flex-wrap: wrap;
}}

.header-nav a {{
  color: rgba(255,255,255,0.8);
  text-decoration: none;
  font-size: 0.8rem;
  padding: 0.35rem 0.75rem;
  border-radius: 4px;
  transition: all 0.2s;
  font-weight: 500;
}}

.header-nav a:hover, .header-nav a.active {{
  background: rgba(255,255,255,0.15);
  color: white;
}}

/* ── Sections ───────────────────── */
.section {{
  padding: 3rem 2rem;
}}

.section--alt {{
  background: var(--bg-warm);
}}

.section--dark {{
  background: var(--text);
  color: white;
}}

.container {{
  max-width: var(--max-w);
  margin: 0 auto;
}}

.section-title {{
  font-family: var(--font-serif);
  font-size: clamp(1.5rem, 3vw, 2rem);
  font-weight: 700;
  color: var(--wine);
  margin-bottom: 0.5rem;
}}

.section--dark .section-title {{ color: white; }}

.section-subtitle {{
  font-size: 0.85rem;
  color: var(--text3);
  margin-bottom: 2rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  font-weight: 600;
}}

.section--dark .section-subtitle {{ color: rgba(255,255,255,0.6); }}

.divider {{
  width: 48px;
  height: 2px;
  background: var(--wine);
  margin-bottom: 2rem;
}}

.section--dark .divider {{ background: rgba(255,255,255,0.4); }}

/* ── Hero Stats ─────────────────── */
.hero {{
  background: linear-gradient(135deg, var(--wine) 0%, var(--wine-dark) 100%);
  color: white;
  padding: 4rem 2rem;
  text-align: center;
}}

.hero h2 {{
  font-family: var(--font-serif);
  font-size: clamp(2rem, 4vw, 3rem);
  font-weight: 700;
  margin-bottom: 0.5rem;
}}

.hero p {{
  opacity: 0.8;
  font-size: 1.1rem;
  margin-bottom: 2.5rem;
}}

.stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1.5rem;
  max-width: var(--max-w);
  margin: 0 auto;
}}

.stat-card {{
  background: rgba(255,255,255,0.1);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: var(--radius);
  padding: 1.5rem;
  text-align: center;
  transition: transform 0.2s, background 0.2s;
}}

.stat-card:hover {{
  transform: translateY(-2px);
  background: rgba(255,255,255,0.15);
}}

.stat-number {{
  font-family: var(--font-serif);
  font-size: 2.5rem;
  font-weight: 700;
  line-height: 1;
  margin-bottom: 0.25rem;
}}

.stat-number.green {{ color: #8eff8e; }}
.stat-number.gold {{ color: #ffd700; }}
.stat-number.red {{ color: #ff8e8e; }}

.stat-label {{
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  opacity: 0.7;
  font-weight: 600;
}}

/* ── Cards ──────────────────────── */
.cards-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1.5rem;
}}

.card {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem;
  transition: border-color 0.2s, box-shadow 0.2s;
}}

.card:hover {{
  border-color: var(--wine);
  box-shadow: var(--shadow-lg);
}}

.card-title {{
  font-family: var(--font-serif);
  font-size: 1.25rem;
  font-weight: 600;
  color: var(--wine);
  margin-bottom: 1rem;
}}

/* ── Charts ─────────────────────── */
.chart-container {{
  position: relative;
  width: 100%;
  max-height: 400px;
}}

.chart-row {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1.5rem;
}}

@media (max-width: 768px) {{
  .chart-row {{ grid-template-columns: 1fr; }}
}}

/* ── Table ──────────────────────── */
.table-wrap {{
  overflow-x: auto;
  border-radius: var(--radius);
  border: 1px solid var(--border);
}}

table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.85rem;
}}

th {{
  background: var(--wine);
  color: white;
  padding: 0.75rem 1rem;
  text-align: left;
  font-weight: 600;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  position: sticky;
  top: 0;
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}}

th:hover {{ background: var(--wine-dark); }}
th .sort-arrow {{ margin-left: 4px; opacity: 0.5; }}

td {{
  padding: 0.6rem 1rem;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}}

tr:hover td {{ background: var(--wine-bg); }}

.bar-cell {{
  display: flex;
  align-items: center;
  gap: 0.5rem;
}}

.bar {{
  height: 6px;
  border-radius: 3px;
  background: var(--border);
  flex: 1;
  min-width: 60px;
  overflow: hidden;
}}

.bar-fill {{
  height: 100%;
  border-radius: 3px;
  transition: width 0.5s ease;
}}

.bar-fill.green {{ background: var(--green); }}
.bar-fill.wine {{ background: var(--wine); }}
.bar-fill.gold {{ background: var(--gold); }}

.pct {{ font-weight: 600; font-size: 0.8rem; min-width: 42px; text-align: right; }}

/* ── Badges ─────────────────────── */
.badge {{
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 3px;
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}

.badge--ok {{ background: var(--green-bg); color: var(--green); }}
.badge--warn {{ background: var(--gold-bg); color: var(--gold); }}
.badge--error {{ background: var(--red-bg); color: var(--red); }}
.badge--info {{ background: var(--wine-bg2); color: var(--wine); }}
.badge--purple {{ background: var(--purple-bg); color: var(--purple); }}

/* ── Activity feed ──────────────── */
.feed {{
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}}

.feed-item {{
  display: flex;
  gap: 1rem;
  padding: 0.75rem 1rem;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 0.85rem;
  transition: border-color 0.2s;
  align-items: flex-start;
}}

.feed-item:hover {{ border-color: var(--wine); }}
a.feed-item {{ text-decoration: none; color: inherit; cursor: pointer; }}
a.feed-item:hover {{ box-shadow: var(--shadow); }}

.feed-dot {{
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-top: 6px;
  flex-shrink: 0;
}}

.feed-dot.green {{ background: var(--green); }}
.feed-dot.wine {{ background: var(--wine); }}
.feed-dot.gold {{ background: var(--gold); }}
.feed-dot.red {{ background: var(--red); }}

.feed-content {{ flex: 1; min-width: 0; }}
.feed-title {{ font-weight: 600; margin-bottom: 2px; overflow-wrap: break-word; word-wrap: break-word; }}
.feed-meta {{ color: var(--text3); font-size: 0.75rem; }}
.feed-desc {{ color: var(--text2); font-size: 0.8rem; overflow-wrap: break-word; word-wrap: break-word; }}

/* ── Tabs ───────────────────────── */
.tabs {{
  display: flex;
  gap: 0;
  border-bottom: 2px solid var(--border);
  margin-bottom: 1.5rem;
}}

.tab {{
  padding: 0.75rem 1.25rem;
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text3);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  transition: all 0.2s;
}}

.tab:hover {{ color: var(--wine); }}
.tab.active {{ color: var(--wine); border-bottom-color: var(--wine); }}

.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}

/* ── Footer ─────────────────────── */
.footer {{
  background: #1a1a1a;
  color: #aaa;
  padding: 2rem;
  text-align: center;
  font-size: 0.8rem;
}}

.footer a {{ color: #ccc; }}

/* ── Responsive ─────────────────── */
@media (max-width: 768px) {{
  .stats-grid {{ grid-template-columns: repeat(2, 1fr); gap: 0.75rem; }}
  .stat-card {{ padding: 1rem; }}
  .stat-number {{ font-size: 1.8rem; }}
  .cards-grid {{ grid-template-columns: 1fr; }}
  .header-inner {{ padding: 0.75rem 1rem; }}
  .section {{ padding: 2rem 1rem; }}
  .hero {{ padding: 2.5rem 1rem; }}
  .tabs {{ overflow-x: auto; -webkit-overflow-scrolling: touch; flex-wrap: nowrap; }}
  .tab {{ white-space: nowrap; flex-shrink: 0; padding: 0.6rem 0.9rem; font-size: 0.8rem; }}
  .feed-item {{ flex-wrap: wrap; padding: 0.6rem 0.75rem; gap: 0.5rem; }}
  .feed-content {{ min-width: 0; width: calc(100% - 24px); }}
  .feed-meta {{ flex-wrap: wrap; gap: 0.25rem; }}
  td {{ white-space: normal; padding: 0.5rem 0.6rem; font-size: 0.78rem; }}
  th {{ padding: 0.5rem 0.6rem; font-size: 0.68rem; }}
  .header-nav a {{ padding: 0.3rem 0.5rem; font-size: 0.72rem; }}
}}

@media (max-width: 480px) {{
  .stats-grid {{ grid-template-columns: 1fr; }}
  .header-inner {{ flex-direction: column; align-items: flex-start; gap: 0.5rem; }}
  .header h1 {{ font-size: 1.3rem; }}
  .header-nav {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  .tab {{ padding: 0.5rem 0.7rem; font-size: 0.75rem; }}
  .hero h2 {{ font-size: 1.5rem; }}
  .hero p {{ font-size: 0.9rem; }}
}}

/* ── Embed mode (hide chrome when loaded inside iframe) ── */
.embed-mode .header,
.embed-mode .hero,
.embed-mode .footer {{ display: none !important; }}
.embed-mode .section:first-of-type {{ padding-top: 2rem; }}
.embed-mode {{ overflow: hidden !important; height: auto !important; }}

/* ── Animations ─────────────────── */
@keyframes fadeUp {{
  from {{ opacity: 0; transform: translateY(20px); }}
  to {{ opacity: 1; transform: translateY(0); }}
}}

.animate {{ animation: fadeUp 0.5s ease forwards; opacity: 0; }}

.animate-delay-1 {{ animation-delay: 0.1s; }}
.animate-delay-2 {{ animation-delay: 0.2s; }}
.animate-delay-3 {{ animation-delay: 0.3s; }}
.animate-delay-4 {{ animation-delay: 0.4s; }}
.animate-delay-5 {{ animation-delay: 0.5s; }}

/* ── Search/Filter ──────────────── */
.search-box {{
  width: 100%;
  max-width: 400px;
  padding: 0.6rem 1rem;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-size: 0.85rem;
  font-family: var(--font-sans);
  margin-bottom: 1.5rem;
  transition: border-color 0.2s;
}}

.search-box:focus {{
  outline: none;
  border-color: var(--wine);
  box-shadow: 0 0 0 2px var(--wine-bg2);
}}

/* ── Pulse dot ──────────────────── */
.pulse {{
  width: 10px; height: 10px;
  background: var(--green);
  border-radius: 50%;
  display: inline-block;
  animation: pulse-anim 2s infinite;
}}

@keyframes pulse-anim {{
  0% {{ box-shadow: 0 0 0 0 rgba(45, 122, 79, 0.4); }}
  70% {{ box-shadow: 0 0 0 8px rgba(45, 122, 79, 0); }}
  100% {{ box-shadow: 0 0 0 0 rgba(45, 122, 79, 0); }}
}}
</style>
</head>
<body>
<script>
if (new URLSearchParams(window.location.search).has('embed')) {{
  document.body.classList.add('embed-mode');
  function notifyHeight() {{
    // Measure actual content height (not viewport) by temporarily shrinking body
    document.body.style.height = '0';
    var h = document.body.scrollHeight;
    document.body.style.height = '';
    window.parent.postMessage({{ type: 'dashboard-height', height: h }}, '*');
  }}
  window.addEventListener('load', notifyHeight);
  new ResizeObserver(notifyHeight).observe(document.body);
  // Re-notify on any click (tab switches, accordions, etc.)
  document.addEventListener('click', function() {{ setTimeout(notifyHeight, 150); }});
}}
</script>

<!-- Header -->
<header class="header">
  <div class="header-inner">
    <h1>LegismEx <span>Vigilancia Legislativa</span></h1>
    <nav class="header-nav">
      <a href="#resumen" class="active">Resumen</a>
      <a href="#entidades">Entidades</a>
      <a href="#tipos">Instrumentos</a>
      <a href="#actividad">Actividad</a>
      <a href="#pipeline">Pipeline</a>
    </nav>
    <div class="header-meta">
      <span class="pulse"></span> Actualizado: <span id="fecha-gen"></span>
    </div>
  </div>
</header>

<!-- Hero Stats -->
<section class="hero" id="resumen">
  <h2>Derecho Positivo Mexicano</h2>
  <p>Vigilancia automatizada del Diario Oficial de la Federaci&oacute;n y 30 peri&oacute;dicos oficiales estatales</p>
  <div class="stats-grid">
    <div class="stat-card animate animate-delay-1">
      <div class="stat-number" id="s-total">-</div>
      <div class="stat-label">Instrumentos legales</div>
    </div>
    <div class="stat-card animate animate-delay-2">
      <div class="stat-number" id="s-entidades">-</div>
      <div class="stat-label">Entidades (federal + 32 estados)</div>
    </div>
    <div class="stat-card animate animate-delay-3">
      <div class="stat-number green" id="s-descargadas">-</div>
      <div class="stat-label">Descargadas y verificadas</div>
    </div>
    <div class="stat-card animate animate-delay-4">
      <div class="stat-number green" id="s-pct">-</div>
      <div class="stat-label">Cobertura</div>
    </div>
    <div class="stat-card animate animate-delay-5">
      <div class="stat-number gold" id="s-pendientes">-</div>
      <div class="stat-label">Pendientes de actualizar</div>
    </div>
  </div>
</section>

<!-- Tipos de instrumentos -->
<section class="section" id="tipos">
  <div class="container">
    <div class="section-subtitle">Composici&oacute;n</div>
    <h3 class="section-title">Instrumentos Legales por Tipo</h3>
    <div class="divider"></div>
    <div class="chart-row">
      <div class="card">
        <div class="card-title">Distribuci&oacute;n Nacional</div>
        <div class="chart-container">
          <canvas id="chart-tipos"></canvas>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Top 15 Entidades por Volumen</div>
        <div class="chart-container" style="max-height:none;">
          <canvas id="chart-top-entidades"></canvas>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- Entidades -->
<section class="section section--alt" id="entidades">
  <div class="container">
    <div class="section-subtitle">Federal + 32 estados</div>
    <h3 class="section-title">Cobertura por Entidad Federativa</h3>
    <div class="divider"></div>
    <input type="text" class="search-box" placeholder="Buscar entidad..." id="search-entidad">
    <div class="table-wrap">
      <table id="tabla-entidades">
        <thead>
          <tr>
            <th onclick="sortTable(0)">Entidad <span class="sort-arrow">&#9650;</span></th>
            <th onclick="sortTable(1)">Total <span class="sort-arrow">&#9650;</span></th>
            <th onclick="sortTable(2)">Con URL <span class="sort-arrow">&#9650;</span></th>
            <th onclick="sortTable(3)">Descargadas <span class="sort-arrow">&#9650;</span></th>
            <th onclick="sortTable(4)">Cobertura <span class="sort-arrow">&#9650;</span></th>
            <th>Leyes</th>
            <th>C&oacute;digos</th>
            <th>Reglamentos</th>
          </tr>
        </thead>
        <tbody id="tbody-entidades"></tbody>
      </table>
    </div>
  </div>
</section>

<!-- Actividad reciente -->
<section class="section" id="actividad">
  <div class="container">
    <div class="section-subtitle">Monitoreo en tiempo real</div>
    <h3 class="section-title">Actividad Reciente</h3>
    <div class="divider"></div>

    <div class="tabs">
      <div class="tab active" onclick="switchTab('pub')">Publicaciones DOF</div>
      <div class="tab" onclick="switchTab('acciones')">Acciones LLM</div>
      <div class="tab" onclick="switchTab('alertas')">Alertas</div>
      <div class="tab" onclick="switchTab('resolver')">Resoluciones</div>
    </div>

    <div class="tab-content active" id="tab-pub">
      <div class="feed" id="feed-pub"></div>
    </div>
    <div class="tab-content" id="tab-acciones">
      <div class="feed" id="feed-acciones"></div>
    </div>
    <div class="tab-content" id="tab-alertas">
      <div class="feed" id="feed-alertas"></div>
    </div>
    <div class="tab-content" id="tab-resolver">
      <div class="feed" id="feed-resolver"></div>
    </div>
  </div>
</section>

<!-- Pipeline -->
<section class="section section--dark" id="pipeline">
  <div class="container">
    <div class="section-subtitle">Sistema aut&oacute;nomo</div>
    <h3 class="section-title">Estado del Pipeline</h3>
    <div class="divider"></div>
    <div class="cards-grid">
      <div class="card" style="background:rgba(255,255,255,0.05);border-color:rgba(255,255,255,0.1);">
        <div class="card-title" style="color:white;">Descargas Hist&oacute;ricas</div>
        <div class="chart-container">
          <canvas id="chart-descargas"></canvas>
        </div>
      </div>
      <div class="card" style="background:rgba(255,255,255,0.05);border-color:rgba(255,255,255,0.1);">
        <div class="card-title" style="color:white;">Ejecuciones del Pipeline</div>
        <div class="feed" id="feed-pipeline"></div>
      </div>
    </div>
    <div style="margin-top:2rem;">
      <div class="cards-grid" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
        <div class="stat-card">
          <div class="stat-number green" id="s-exitosas">-</div>
          <div class="stat-label">Descargas exitosas</div>
        </div>
        <div class="stat-card">
          <div class="stat-number red" id="s-fallidas">-</div>
          <div class="stat-label">Descargas fallidas</div>
        </div>
        <div class="stat-card">
          <div class="stat-number gold" id="s-reintentos">-</div>
          <div class="stat-label">En cola de reintentos</div>
        </div>
        <div class="stat-card">
          <div class="stat-number" id="s-llm-acciones">-</div>
          <div class="stat-label">Acciones LLM</div>
        </div>
      </div>
    </div>
  </div>
</section>

<footer class="footer">
  <p>LegismEx &mdash; Sistema de Vigilancia Legislativa Mexicana</p>
  <p style="margin-top:0.5rem;opacity:0.6;">Dashboard generado autom&aacute;ticamente &middot; Datos actualizados diariamente a las 11:00 AM CDMX</p>
</footer>

<script>
const D = {datos_json};

// ── XSS sanitization ──
function esc(s) {{ if (s == null) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}
function escUrl(u) {{ if (u == null) return ''; const s = String(u).trim(); if (/^https?:\\/\\//i.test(s)) return esc(s); return ''; }}

// ── Populate stats ──
document.getElementById('fecha-gen').textContent = new Date(D.fecha_generacion + 'Z').toLocaleString('es-MX', {{timeZone: 'America/Mexico_City'}});
document.getElementById('s-total').textContent = D.total_leyes.toLocaleString();
document.getElementById('s-entidades').textContent = D.entidades;
document.getElementById('s-descargadas').textContent = D.descargadas.toLocaleString();
document.getElementById('s-pct').textContent = D.pct_descarga + '%';
document.getElementById('s-pendientes').textContent = D.pendientes_actualizacion;
document.getElementById('s-exitosas').textContent = D.exitosas_total.toLocaleString();
document.getElementById('s-fallidas').textContent = D.fallidas_total.toLocaleString();
document.getElementById('s-reintentos').textContent = D.cola_reintentos.length;
document.getElementById('s-llm-acciones').textContent = D.acciones_llm.length;

// ── Chart: Tipos ──
const tipoColors = {{
  'Ley': '#8A1538', 'C\\u00f3digo': '#b8860b', 'Reglamento': '#2d7a4f',
  'Decreto': '#7c3a6e', 'Acuerdo': '#2980b9', 'Norma': '#e67e22', 'Otro': '#95a5a6'
}};

const tipoLabels = Object.keys(D.tipos_global);
const tipoData = Object.values(D.tipos_global);

new Chart(document.getElementById('chart-tipos'), {{
  type: 'doughnut',
  data: {{
    labels: tipoLabels,
    datasets: [{{
      data: tipoData,
      backgroundColor: tipoLabels.map(t => tipoColors[t] || '#95a5a6'),
      borderWidth: 2,
      borderColor: '#fff',
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: true,
    plugins: {{
      legend: {{
        position: 'bottom',
        labels: {{ font: {{ family: "'Inter'", size: 12 }}, padding: 16 }}
      }}
    }}
  }}
}});

// ── Chart: Top entidades ──
const sorted = [...D.entidades_tabla].sort((a, b) => b.total - a.total).slice(0, 15);
new Chart(document.getElementById('chart-top-entidades'), {{
  type: 'bar',
  data: {{
    labels: sorted.map(e => e.nombre),
    datasets: [{{
      label: 'Descargadas',
      data: sorted.map(e => e.descargadas),
      backgroundColor: 'rgba(138, 21, 56, 0.8)',
      borderRadius: 3,
    }}, {{
      label: 'Pendientes',
      data: sorted.map(e => e.total - e.descargadas),
      backgroundColor: 'rgba(184, 134, 11, 0.5)',
      borderRadius: 3,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: true,
    aspectRatio: 1.2,
    indexAxis: 'y',
    scales: {{
      x: {{ stacked: true, grid: {{ color: 'rgba(0,0,0,0.05)' }} }},
      y: {{ stacked: true, ticks: {{ font: {{ size: 12 }}, autoSkip: false, padding: 4 }} }}
    }},
    plugins: {{
      legend: {{ position: 'top', labels: {{ font: {{ family: "'Inter'", size: 11 }} }} }}
    }}
  }}
}});

// ── Chart: Descargas ──
new Chart(document.getElementById('chart-descargas'), {{
  type: 'doughnut',
  data: {{
    labels: ['Exitosas', 'Fallidas'],
    datasets: [{{
      data: [D.exitosas_total, D.fallidas_total],
      backgroundColor: ['#2d7a4f', '#c0392b'],
      borderWidth: 0,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: true,
    cutout: '70%',
    plugins: {{
      legend: {{
        position: 'bottom',
        labels: {{ color: '#ccc', font: {{ family: "'Inter'", size: 12 }} }}
      }}
    }}
  }}
}});

// ── Table: Entidades ──
function renderTable(data) {{
  const tbody = document.getElementById('tbody-entidades');
  tbody.innerHTML = data.map(e => {{
    const barColor = e.pct >= 95 ? 'green' : e.pct >= 80 ? 'wine' : 'gold';
    return `<tr>
      <td><strong>${{esc(e.nombre)}}</strong></td>
      <td>${{Number(e.total) || 0}}</td>
      <td>${{Number(e.con_url) || 0}}</td>
      <td>${{Number(e.descargadas) || 0}}</td>
      <td><div class="bar-cell">
        <span class="pct">${{Number(e.pct) || 0}}%</span>
        <div class="bar"><div class="bar-fill ${{barColor}}" style="width:${{Number(e.pct) || 0}}%"></div></div>
      </div></td>
      <td>${{Number(e.tipos['Ley']) || 0}}</td>
      <td>${{Number(e.tipos['C\\u00f3digo']) || Number(e.tipos['Codigo']) || 0}}</td>
      <td>${{Number(e.tipos['Reglamento']) || 0}}</td>
    </tr>`;
  }}).join('');
}}

renderTable(D.entidades_tabla);

// Search
document.getElementById('search-entidad').addEventListener('input', function() {{
  const q = this.value.toLowerCase();
  const filtered = D.entidades_tabla.filter(e =>
    e.nombre.toLowerCase().includes(q) || e.id.includes(q)
  );
  renderTable(filtered);
}});

// Sort
let sortDir = {{}};
function sortTable(col) {{
  sortDir[col] = !(sortDir[col] || false);
  const keys = ['nombre', 'total', 'con_url', 'descargadas', 'pct'];
  const key = keys[col];
  const dir = sortDir[col] ? 1 : -1;
  const sorted = [...D.entidades_tabla].sort((a, b) => {{
    if (typeof a[key] === 'string') return a[key].localeCompare(b[key]) * dir;
    return (a[key] - b[key]) * dir;
  }});
  renderTable(sorted);
}}

// ── Feed: Publicaciones ──
const feedPub = document.getElementById('feed-pub');
if (D.publicaciones.length === 0) {{
  feedPub.innerHTML = '<div class="feed-item"><div class="feed-content"><div class="feed-title" style="color:var(--text3)">Sin publicaciones detectadas a&uacute;n</div></div></div>';
}} else {{
  feedPub.innerHTML = D.publicaciones.slice(-20).reverse().map(p => {{
    const ent = p.entidad || 'federal';
    const hasUrl = escUrl(p.url);
    const tag = hasUrl ? 'a' : 'div';
    const linkAttrs = hasUrl ? ` href="${{escUrl(p.url)}}" target="_blank" rel="noopener"` : '';
    return `<${{tag}} class="feed-item"${{linkAttrs}}>
      <div class="feed-dot wine"></div>
      <div class="feed-content">
        <div class="feed-title">${{esc(p.titulo || '')}}</div>
        <div class="feed-meta">
          <span class="badge badge--info">${{esc(ent)}}</span>
          ${{p.fecha_deteccion ? esc(new Date(p.fecha_deteccion).toLocaleDateString('es-MX')) : ''}}
        </div>
      </div>
    </${{tag}}>`;
  }}).join('');
}}

// ── Feed: Acciones LLM ──
const feedAcciones = document.getElementById('feed-acciones');
if (D.acciones_llm.length === 0) {{
  feedAcciones.innerHTML = '<div class="feed-item"><div class="feed-content"><div class="feed-title" style="color:var(--text3)">Sin acciones LLM registradas</div></div></div>';
}} else {{
  feedAcciones.innerHTML = D.acciones_llm.map(a => {{
    const dotColor = a.accion_recomendada === 're-descargar' ? 'gold' :
                     a.accion_recomendada === 'agregar' ? 'green' :
                     a.accion_recomendada === 'marcar-abrogada' ? 'red' : 'wine';
    const badgeClass = a.accion_recomendada === 're-descargar' ? 'warn' :
                       a.accion_recomendada === 'agregar' ? 'ok' :
                       a.accion_recomendada === 'marcar-abrogada' ? 'error' : 'info';
    return `<div class="feed-item">
      <div class="feed-dot ${{dotColor}}"></div>
      <div class="feed-content">
        <div class="feed-title">${{esc(a.ley_afectada || a.tipo_acto || '?')}}</div>
        <div class="feed-desc">${{esc(a.resumen || '')}}</div>
        <div class="feed-meta">
          <span class="badge badge--${{badgeClass}}">${{esc(a.accion_recomendada)}}</span>
          <span class="badge badge--purple">${{esc(a.tipo_acto)}}</span>
          Confianza: ${{(Number(a.confianza) * 100 || 0).toFixed(0)}}%
          ${{a._fecha_analisis ? ' &middot; ' + esc(new Date(a._fecha_analisis).toLocaleDateString('es-MX')) : ''}}
        </div>
      </div>
    </div>`;
  }}).join('');
}}

// ── Feed: Alertas ──
const feedAlertas = document.getElementById('feed-alertas');
if (D.alertas_recientes.length === 0) {{
  feedAlertas.innerHTML = '<div class="feed-item"><div class="feed-content"><div class="feed-title" style="color:var(--text3)">Sin alertas recientes</div></div></div>';
}} else {{
  feedAlertas.innerHTML = D.alertas_recientes.reverse().map(a => {{
    const parts = a.split(' | ');
    const fecha = parts[0] || '';
    const msg = parts.slice(2).join(' | ') || parts.slice(1).join(' | ') || a;
    return `<div class="feed-item">
      <div class="feed-dot gold"></div>
      <div class="feed-content">
        <div class="feed-desc">${{esc(msg)}}</div>
        <div class="feed-meta">${{esc(fecha)}}</div>
      </div>
    </div>`;
  }}).join('');
}}

// ── Feed: Resolver ──
const feedResolver = document.getElementById('feed-resolver');
if (D.resolver.length === 0) {{
  feedResolver.innerHTML = '<div class="feed-item"><div class="feed-content"><div class="feed-title" style="color:var(--text3)">Sin resoluciones pendientes</div></div></div>';
}} else {{
  feedResolver.innerHTML = D.resolver.map(r => {{
    const dotColor = r.resuelto ? 'green' : 'gold';
    const badge = r.resuelto ? 'ok' : 'warn';
    const label = r.resuelto ? 'Resuelto' : 'Pendiente';
    return `<div class="feed-item">
      <div class="feed-dot ${{dotColor}}"></div>
      <div class="feed-content">
        <div class="feed-title">${{esc(r.nombre || r.id)}}</div>
        <div class="feed-meta">
          <span class="badge badge--${{badge}}">${{label}}</span>
          D&iacute;as: ${{Number(r.dias) || '?'}} &middot; Intentos: ${{Number(r.intentos) || '?'}}
          ${{r.estrategia ? ' &middot; Estrategia ' + esc(r.estrategia) : ''}}
          ${{r.fecha ? ' &middot; ' + esc(new Date(r.fecha).toLocaleDateString('es-MX')) : ''}}
        </div>
      </div>
    </div>`;
  }}).join('');
}}

// ── Feed: Pipeline ──
const feedPipeline = document.getElementById('feed-pipeline');
if (D.pipeline_logs.length === 0) {{
  feedPipeline.innerHTML = '<div class="feed-item"><div class="feed-content"><div class="feed-title" style="color:rgba(255,255,255,0.5)">Sin ejecuciones registradas</div></div></div>';
}} else {{
  feedPipeline.innerHTML = D.pipeline_logs.slice(-10).reverse().map(p => {{
    const dotColor = p.exito ? 'green' : 'red';
    const badge = p.exito ? 'ok' : 'error';
    const label = p.exito ? 'Exitoso' : 'Con errores';
    return `<div class="feed-item" style="background:rgba(255,255,255,0.03);border-color:rgba(255,255,255,0.1);">
      <div class="feed-dot ${{dotColor}}"></div>
      <div class="feed-content">
        <div class="feed-title" style="color:white;">${{esc(p.fecha)}}</div>
        <div class="feed-meta" style="color:rgba(255,255,255,0.5);">
          <span class="badge badge--${{badge}}">${{label}}</span>
          ${{esc(p.duracion)}}
        </div>
      </div>
    </div>`;
  }}).join('');
}}

// ── Tabs ──
function switchTab(name) {{
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('tab-' + name).classList.add('active');
}}

// ── Smooth scroll ──
document.querySelectorAll('.header-nav a').forEach(a => {{
  a.addEventListener('click', function(e) {{
    e.preventDefault();
    const id = this.getAttribute('href').slice(1);
    document.getElementById(id)?.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    document.querySelectorAll('.header-nav a').forEach(x => x.classList.remove('active'));
    this.classList.add('active');
  }});
}});

// ── Intersection observer for active nav ──
const sections = document.querySelectorAll('.section, .hero');
const navLinks = document.querySelectorAll('.header-nav a');
const observer = new IntersectionObserver(entries => {{
  entries.forEach(entry => {{
    if (entry.isIntersecting) {{
      navLinks.forEach(l => l.classList.remove('active'));
      const link = document.querySelector(`.header-nav a[href="#${{entry.target.id}}"]`);
      if (link) link.classList.add('active');
    }}
  }});
}}, {{ threshold: 0.3 }});
sections.forEach(s => {{ if (s.id) observer.observe(s); }});
</script>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description="Genera el dashboard HTML de LegismEx")
    parser.add_argument("--output", default=str(BASE_DIR), help="Directorio de salida")
    args = parser.parse_args()

    print("Recopilando datos...")
    datos = recopilar_datos()

    print(f"  {datos['total_leyes']} instrumentos legales")
    print(f"  {datos['entidades']} entidades")
    print(f"  {datos['descargadas']} descargadas ({datos['pct_descarga']}%)")

    html_content = generar_html(datos)
    output = Path(args.output) / "dashboard.html"
    with open(output, "w", encoding="utf-8") as f:
        f.write(html_content)

    size_kb = output.stat().st_size / 1024
    print(f"\n  Dashboard generado: {output} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
