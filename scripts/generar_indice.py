#!/usr/bin/env python3
"""
generar_indice.py — Consolida todos los catalogo.json en leyes_index.json.
No editar leyes_index.json manualmente. Este script es la única fuente.

Uso:
    python3 generar_indice.py              # Regenerar índice completo
    python3 generar_indice.py --resumen    # Solo mostrar estadísticas
"""

import json
import sys
import argparse
from pathlib import Path
from datetime import date

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "leyes_index.json"

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


def cargar_catalogo(entidad: str) -> list[dict]:
    if entidad == "federal":
        ruta = BASE_DIR / "federal" / "catalogo.json"
    else:
        ruta = BASE_DIR / "estados" / entidad / "catalogo.json"

    if not ruta.exists():
        return []

    if ruta.stat().st_size == 0:
        print(f"  ⚠ ADVERTENCIA: {ruta} está vacío, omitiendo {entidad}")
        return []

    try:
        with open(ruta, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ⚠ ADVERTENCIA: {ruta} tiene JSON inválido ({e}), omitiendo {entidad}")
        return []

    # Normalizar campos para el índice
    resultado = []
    for ley in data:
        entrada = {
            "id":              ley.get("id", ""),
            "nombre":          ley.get("nombre", ""),
            "tipo":            ley.get("tipo", "Ley"),
            "entidad":         entidad,
            "url":             ley.get("url_pdf", "") or ley.get("url_word", ""),
            "url_word":        ley.get("url_word", ""),
            "formato":         "pdf" if ley.get("url_pdf") else ("doc" if ley.get("url_word") else "pdf"),
            "ultimo_hash":     ley.get("ultimo_hash", None),
            "ultima_descarga": ley.get("ultima_descarga", None),
            "ultima_reforma":  ley.get("ultima_reforma", ""),
            "estado":          ley.get("estado", "pendiente"),
            "fuente":          ley.get("fuente", ""),
        }
        # Solo incluir si tiene URL de descarga
        if entrada["id"] and entrada["url"]:
            resultado.append(entrada)
        elif entrada["id"] and not entrada["url"]:
            # Incluir igual pero marcado sin URL
            entrada["estado"] = "sin_url"
            resultado.append(entrada)

    return resultado


def main():
    parser = argparse.ArgumentParser(
        description="Genera leyes_index.json consolidado desde todos los catálogos"
    )
    parser.add_argument(
        "--resumen",
        action="store_true",
        help="Mostrar estadísticas sin regenerar el índice",
    )
    args = parser.parse_args()

    print(f"\n{'═'*55}")
    print(f"  LegismEx — Generador de Índice")
    print(f"  {date.today().isoformat()}")
    print(f"{'═'*55}\n")

    indice_total: list[dict] = []
    resumen: list[dict] = []

    for entidad in TODAS_ENTIDADES:
        leyes = cargar_catalogo(entidad)
        con_url = sum(1 for l in leyes if l.get("url"))
        sin_url = len(leyes) - con_url

        estado = "✓" if leyes else "✗ sin catálogo"
        print(
            f"  {entidad:<22} {len(leyes):>4} leyes   "
            f"({con_url} con URL, {sin_url} sin URL)  {estado}"
        )

        resumen.append({
            "entidad": entidad,
            "total": len(leyes),
            "con_url": con_url,
            "sin_url": sin_url,
        })
        indice_total.extend(leyes)

    print(f"\n{'─'*55}")
    print(f"  TOTAL CONSOLIDADO: {len(indice_total)} documentos")
    print(
        f"  Con URL de descarga: "
        f"{sum(1 for l in indice_total if l.get('url'))}"
    )
    print(
        f"  Sin URL (requieren revisión): "
        f"{sum(1 for l in indice_total if not l.get('url'))}"
    )
    print(f"{'═'*55}\n")

    if args.resumen:
        return

    # Preservar hashes y fechas de versión anterior si existe
    indice_anterior = []
    if INDEX_FILE.exists() and INDEX_FILE.stat().st_size > 0:
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                indice_anterior = json.load(f)
        except json.JSONDecodeError:
            print("  ⚠ leyes_index.json anterior corrupto, se regenerará desde cero")
            indice_anterior = []

    if indice_anterior:
        hash_map = {
            l["id"]: {
                "ultimo_hash": l.get("ultimo_hash"),
                "ultima_descarga": l.get("ultima_descarga"),
                "estado": l.get("estado"),
            }
            for l in indice_anterior
        }
        for ley in indice_total:
            prev = hash_map.get(ley["id"])
            if prev:
                ley["ultimo_hash"] = prev.get("ultimo_hash") or ley.get("ultimo_hash")
                ley["ultima_descarga"] = prev.get("ultima_descarga") or ley.get("ultima_descarga")
                # No sobreescribir estado si ya tiene uno significativo
                if prev.get("estado") in ("ok", "critico"):
                    ley["estado"] = prev["estado"]

    # Guard: no sobrescribir con un índice mucho más pequeño (posible catálogo corrupto)
    if indice_anterior and len(indice_total) < len(indice_anterior) * 0.8:
        print(
            f"  ✗ ABORTADO: nuevo índice ({len(indice_total)}) tiene >20% menos "
            f"entradas que el anterior ({len(indice_anterior)}). "
            f"Revisa catálogos vacíos o corruptos."
        )
        sys.exit(1)

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(indice_total, f, ensure_ascii=False, indent=2)

    print(f"✓ leyes_index.json actualizado: {len(indice_total)} entradas")


if __name__ == "__main__":
    main()
