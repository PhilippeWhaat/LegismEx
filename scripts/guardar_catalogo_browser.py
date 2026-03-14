#!/usr/bin/env python3
"""
guardar_catalogo_browser.py — Recibe JSON extraído del browser y lo procesa
como catálogo oficial de una entidad.

Uso:
    # Pegar el JSON en un archivo temporal, luego:
    python3 scripts/guardar_catalogo_browser.py --entidad nuevoleon --archivo /tmp/nl_raw.json

    # O directamente desde stdin:
    echo '[...]' | python3 scripts/guardar_catalogo_browser.py --entidad nuevoleon
"""

import json
import re
import sys
import argparse
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def limpiar_url_pdf(url: str) -> str:
    """Quitar el query string de cache-busting (?fecha) de las URLs de PDF."""
    return url.split('?')[0] if url else ''


def generar_id(entidad: str, nombre: str) -> str:
    s = nombre.lower()
    for src, dst in [('á','a'),('à','a'),('ä','a'),('é','e'),('è','e'),('ë','e'),
                     ('í','i'),('ì','i'),('ï','i'),('ó','o'),('ò','o'),('ö','o'),
                     ('ú','u'),('ù','u'),('ü','u'),('ñ','n')]:
        s = s.replace(src, dst)
    s = re.sub(r'[^a-z0-9\s]', '', s)
    partes = [p for p in s.split() if p not in {
        'del','de','la','las','el','los','para','y','en','al','con',
        'un','una','por','que','se','o','sus','estado','municipios','nuevo','leon'
    }][:6]
    return f"{entidad}_{'_'.join(partes)}"


def inferir_tipo(nombre: str) -> str:
    n = nombre.upper()
    if n.startswith('CÓDIGO') or n.startswith('CODIGO'):
        return 'Código'
    if n.startswith('REGLAMENTO'):
        return 'Reglamento'
    if n.startswith('DECRETO'):
        return 'Decreto'
    if n.startswith('ACUERDO'):
        return 'Acuerdo'
    if n.startswith('NORMA'):
        return 'Norma'
    return 'Ley'


def procesar(entidad: str, raw: list[dict]) -> list[dict]:
    ids_vistos = set()
    resultado = []
    for item in raw:
        nombre = (item.get('nombre') or '').strip()
        if not nombre or len(nombre) < 5:
            continue
        ley_id = generar_id(entidad, nombre)
        # Desduplicar
        if ley_id in ids_vistos:
            continue
        ids_vistos.add(ley_id)

        categoria = item.get('categoria', '')
        # Inferir tipo desde categoría o nombre
        if categoria == 'codigos':
            tipo = 'Código'
        elif categoria == 'reglamentos':
            tipo = 'Reglamento'
        elif categoria == 'decretos':
            tipo = 'Decreto'
        elif categoria == 'acuerdos':
            tipo = 'Acuerdo'
        else:
            tipo = inferir_tipo(nombre)

        resultado.append({
            'id':              ley_id,
            'nombre':          nombre,
            'tipo':            tipo,
            'entidad':         entidad,
            'url_pdf':         limpiar_url_pdf(item.get('url_pdf', '')),
            'url_word':        item.get('url_word', ''),
            'ultima_reforma':  item.get('ultima_reforma', ''),
            'estado_vigencia': 'vigente',
            'fuente':          item.get('fuente', ''),
            'categoria':       categoria,
        })
    return resultado


def guardar(entidad: str, leyes: list[dict]):
    if entidad == 'federal':
        dir_entidad = BASE_DIR / 'federal'
    else:
        dir_entidad = BASE_DIR / 'estados' / entidad
    dir_entidad.mkdir(parents=True, exist_ok=True)

    json_path = dir_entidad / 'catalogo.json'
    # Si ya existe, fusionar preservando entradas no duplicadas
    existente = []
    if json_path.exists():
        with open(json_path, encoding='utf-8') as f:
            existente = json.load(f)
        ids_existentes = {l['id'] for l in existente}
        nuevas = [l for l in leyes if l['id'] not in ids_existentes]
        leyes = existente + nuevas
        print(f"  Fusionado con catálogo existente ({len(existente)} prev + {len(nuevas)} nuevas)")

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(leyes, f, ensure_ascii=False, indent=2)
    print(f"  ✓ {json_path}  ({len(leyes)} entradas)")

    # Markdown
    md_path = dir_entidad / 'catalogo.md'
    nombre_entidad = entidad.upper() if entidad != 'federal' else 'FEDERAL'
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f'# Catálogo de Leyes Vigentes — {nombre_entidad}\n\n')
        f.write(f'_Generado: {date.today().isoformat()} | Total: {len(leyes)} documentos_\n\n')
        f.write('| # | Nombre | Tipo | Última reforma | PDF |\n')
        f.write('|---|--------|------|----------------|-----|\n')
        for i, ley in enumerate(leyes, 1):
            url = ley.get('url_pdf', '')
            enlace = f"[PDF]({url})" if url else '—'
            reforma = ley.get('ultima_reforma', '—') or '—'
            f.write(f"| {i} | {ley['nombre']} | {ley['tipo']} | {reforma} | {enlace} |\n")
    print(f"  ✓ {md_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--entidad', required=True)
    parser.add_argument('--archivo', help='Ruta a archivo JSON. Si no se da, lee stdin.')
    args = parser.parse_args()

    if args.archivo:
        with open(args.archivo, encoding='utf-8') as f:
            raw = json.load(f)
    else:
        raw = json.load(sys.stdin)

    if not isinstance(raw, list):
        print('ERROR: El JSON debe ser una lista de objetos', file=sys.stderr)
        sys.exit(1)

    leyes = procesar(args.entidad, raw)
    print(f"\nProcesando {args.entidad}: {len(raw)} items raw → {len(leyes)} únicos")
    guardar(args.entidad, leyes)


if __name__ == '__main__':
    main()
