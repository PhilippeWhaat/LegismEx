# DerIAMex — Derecho Positivo Mexicano

Base de datos estructurada de la legislación vigente en México: federal + 32 estados.
Catálogos completos de leyes, códigos y reglamentos con URLs de descarga directa al PDF oficial, más vigilancia automatizada de cambios.

---

## Cobertura actual

| Entidad | Leyes | Con PDF | Fuente |
|---------|:-----:|:-------:|--------|
| Estado de México | 426 | 426 | legislacion.edomex.gob.mx |
| Yucatán | 384 | 371 | congresoyucatan.gob.mx |
| Oaxaca | 215 | 215 | congresooaxaca.gob.mx |
| Tlaxcala | 203 | 203 | congresodetlaxcala.gob.mx |
| Chihuahua | 176 | 5 | congresochihuahua.gob.mx |
| Nuevo León | 174 | 173 | hcnl.gob.mx |
| Hidalgo | 168 | 168 | congresohidalgo.gob.mx |
| Baja California | 163 | 162 | congresobc.gob.mx |
| Durango | 157 | 157 | congresodurango.gob.mx |
| Tamaulipas | 129 | 129 | congresotamaulipas.gob.mx |
| Guanajuato | 118 | 117 | congresogto.gob.mx |
| Tabasco | 49 | 49 | congresotabasco.gob.mx |
| Quintana Roo | 27 | 27 | congresoqroo.gob.mx |
| **Total** | **2389** | **2202** | |

19 entidades restantes (federal + 18 estados) en proceso.

---

## Estructura

```
dermex/
├── federal/
│   ├── fuentes.md          ← URLs del DOF y Cámara de Diputados
│   ├── catalogo.json       ← Catálogo de leyes federales
│   └── catalogo.md         ← Versión legible
├── estados/
│   └── {estado}/           ← 32 carpetas (cdmx, edomex, jalisco, ...)
│       ├── fuentes.md      ← URLs del periódico oficial y congreso local
│       ├── catalogo.json   ← Catálogo de leyes del estado
│       └── catalogo.md     ← Versión legible
├── scripts/
│   ├── scraper_catalogo.py         ← Extrae catálogos de portales oficiales
│   ├── guardar_catalogo_browser.py ← Procesa JSON extraído desde el browser
│   ├── generar_indice.py           ← Consolida catálogos → leyes_index.json
│   ├── descarga.py                 ← Descarga PDFs con verificación por hash
│   ├── vigilancia_dof.py           ← Vigilancia diaria del DOF y periódicos estatales
│   ├── reintentos.py               ← Gestión de fallos y reintentos
│   └── run_diario.sh               ← Orquestador diario (cron)
└── leyes_index.json        ← Índice consolidado (generado, no editar)
```

---

## Formato del catálogo

Cada `catalogo.json` es una lista de objetos:

```json
[
  {
    "id": "guanajuato_ley_bebidas_alcoholicas",
    "nombre": "Ley de Bebidas Alcohólicas para el Estado de Guanajuato y sus Municipios",
    "tipo": "Ley",
    "entidad": "guanajuato",
    "url_pdf": "https://congreso-gto.s3.amazonaws.com/uploads/reforma/pdf/3694/LBAEGM.pdf",
    "url_word": "https://congreso-gto.s3.amazonaws.com/uploads/reforma/word/3694/LBAEGM.doc",
    "ultima_reforma": "2025-12-31",
    "estado_vigencia": "vigente",
    "fuente": "congresogto.gob.mx"
  }
]
```

Tipos válidos: `Ley`, `Código`, `Reglamento`, `Decreto`, `Acuerdo`, `Norma`

---

## Uso

### Extraer catálogo de un portal oficial

```bash
python3 scripts/scraper_catalogo.py --entidad guanajuato
python3 scripts/scraper_catalogo.py --entidad edomex
python3 scripts/scraper_catalogo.py --entidad nuevoleon
python3 scripts/scraper_catalogo.py --entidad tamaulipas
python3 scripts/scraper_catalogo.py --entidad chihuahua
python3 scripts/scraper_catalogo.py --entidad durango
python3 scripts/scraper_catalogo.py --entidad tabasco
python3 scripts/scraper_catalogo.py --entidad oaxaca
python3 scripts/scraper_catalogo.py --entidad bajacalifornia
python3 scripts/scraper_catalogo.py --entidad yucatan
python3 scripts/scraper_catalogo.py --entidad quintanaroo
python3 scripts/scraper_catalogo.py --entidad tlaxcala
python3 scripts/scraper_catalogo.py --entidad hidalgo
python3 scripts/scraper_catalogo.py --todas       # todas las entidades con scraper
```

### Guardar catálogo extraído desde el browser

```bash
# Después de extraer JSON desde la consola del browser:
python3 scripts/guardar_catalogo_browser.py --entidad nuevoleon --archivo /tmp/nl_raw.json
```

### Regenerar el índice consolidado

```bash
python3 scripts/generar_indice.py           # regenerar leyes_index.json
python3 scripts/generar_indice.py --resumen # solo estadísticas
```

### Descargar leyes

```bash
python3 scripts/descarga.py --entidad nuevoleon
python3 scripts/descarga.py --id nuevoleon_constitucion_politica
python3 scripts/descarga.py  # todas las leyes del índice
```

### Vigilar el DOF de hoy

```bash
python3 scripts/vigilancia_dof.py
python3 scripts/vigilancia_dof.py --fecha 2026-02-19
python3 scripts/vigilancia_dof.py --entidad jalisco
python3 scripts/vigilancia_dof.py --todas
```

### Ciclo diario completo

```bash
bash scripts/run_diario.sh
```

### Automatizar con cron (lunes a viernes, 7:00 AM)

```
0 7 * * 1-5 /ruta/completa/a/dermex/scripts/run_diario.sh
```

---

## Lógica de fallos en descarga

| Intentos | Acción |
|----------|--------|
| 1 fallo | Agregar a `cola_reintentos.json`, reintentar en 24h |
| 2 fallos | Reintentar en 24h adicionales |
| 3 fallos | Alerta en `logs/alertas.log`, marcar `estado: critico` |

---

## Requisitos

- Python 3.10+
- Sin dependencias externas (solo stdlib)
