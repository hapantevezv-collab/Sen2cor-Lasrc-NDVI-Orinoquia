#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gee_ndvi_backend.py v0.7.0

Backend externo para el plugin QGIS "NDVI - Sen2Cor".

Corrección crítica v0.4.1:
  - Evita el error "EEException: User memory limit exceeded" que podía ocurrir
    al usar composite="none" en rangos largos y AOI complejos.
  - Ya no construye un mosaico diario mediante aggregate_array(...).distinct()
    para composite="none". En su lugar, calcula estadísticas por escena y luego
    agrega defensivamente por fecha en Python.
  - Evita getInfo() de conteos de colección antes del cálculo principal.
  - Usa geometría de filtro simplificada/bounds para filterBounds(), y geometría
    de reducción simplificada para reducir presión de memoria en GEE.
  - Añade tileScale configurable en reduceRegion/reduceRegions.

Notas metodológicas:
  - composite="none" devuelve una serie diaria agregada desde escenas Sentinel-2.
    Si hay varias escenas en la misma fecha, el backend agrega esos valores para
    evitar líneas verticales artificiales.
  - Para análisis largos, monthly suele ser más estable computacionalmente.
"""

import argparse
import csv
import json
import math
import statistics
import sys
import traceback
import warnings

warnings.filterwarnings("ignore")

VALID_STATS = ("mean", "median", "min", "max", "stdDev")

# Factores derivados de la investigación A3 (AOI Piedemonte Orinocense).
# La tabla diferencia explícitamente: empírico, interpolación y extrapolación.
# Se incorporan como ayuda operativa trazable, no como operador universal.
TOA_TO_LASRC_TIMELINE = {
    2015: {
        "delta": 0.288, "sigma": 0.054, "estimate_type": "extrapolación",
        "regime": "fuera de rango (-2 años)", "basis": "proyección lineal",
        "applicability": "solo teórica",
    },
    2016: {
        "delta": 0.299, "sigma": 0.053, "estimate_type": "extrapolación",
        "regime": "fuera de rango (-1 año)", "basis": "proyección lineal",
        "applicability": "solo teórica",
    },
    2017: {
        "delta": 0.309, "sigma": 0.054, "estimate_type": "empírico",
        "regime": "ancla observada", "basis": "66 reg. / 22 predios / 7 fechas",
        "applicability": "validada",
    },
    2018: {
        "delta": 0.320, "sigma": 0.052, "estimate_type": "interpolación",
        "regime": "dentro de rango (centroide)", "basis": "proyección lineal",
        "applicability": "defendible",
    },
    2019: {
        "delta": 0.330, "sigma": 0.051, "estimate_type": "empírico",
        "regime": "ancla observada", "basis": "91 reg. / 22 predios / 8 fechas",
        "applicability": "validada",
    },
}
TOA_TO_LASRC_YEAR_FACTOR = {year: meta["delta"] for year, meta in TOA_TO_LASRC_TIMELINE.items()}

TOA_TO_SEN2COR_YEAR_FACTOR = {
    2019: 0.2714,  # delta = NDVI_Sen2Cor - NDVI_L1C, triangulación 2019
}
SEN2COR_TO_LASRC_DEMING_SLOPE = 0.76
SEN2COR_TO_LASRC_DEMING_INTERCEPT = 0.21

VALID_INPUT_COLLECTIONS = ("sen2cor_sr", "toa_l1c")

VALID_CORRECTION_MODES = (
    "none",
    "toa_to_lasrc_timeline_2015_2019",
    "toa_to_lasrc_auto",  # alias retrocompatible de la línea 2015–2019
    "toa_to_sen2cor_2019",
    "sen2cor_to_lasrc_deming_2019",
    "manual_additive",
)

CORRECTION_META_COLUMNS = [
    "radiometric_correction_factor_type",
    "radiometric_correction_sigma",
    "radiometric_correction_regime",
    "radiometric_correction_basis",
    "radiometric_correction_applicability",
]



def _out(obj):
    """Imprime un único objeto JSON a stdout."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def cmd_auth_url(args):
    """
    Autenticación interactiva de Earth Engine.

    Modos disponibles:
    - localhost: recomendado para QGIS/escritorio. Abre el navegador y guarda credenciales locales.
    - notebook: flujo alternativo con código de verificación.
    - gcloud: usa credenciales configuradas con Google Cloud CLI.
    - colab: flujo tipo Google Colab.
    - auto: deja que earthengine-api seleccione el modo.
    """
    try:
        import ee

        auth_mode = getattr(args, "auth_mode", "localhost") or "localhost"
        force = bool(getattr(args, "force", False))

        auth_kwargs = {}
        if auth_mode != "auto":
            auth_kwargs["auth_mode"] = auth_mode
        if force:
            auth_kwargs["force"] = True

        ee.Authenticate(**auth_kwargs)

        result = {
            "ok": True,
            "auth_mode": auth_mode,
            "message": "Token de Earth Engine autenticado/guardado."
        }

        project = getattr(args, "project", None)
        if project:
            ee.Initialize(project=project)
            result["project"] = project
            result["ping"] = ee.Number(1).getInfo()

        _out(result)

    except Exception as exc:
        _out({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": (
                "Si auth_mode=localhost falla, pruebe con --auth-mode notebook "
                "o ejecute earthengine authenticate --force desde una terminal."
            ),
            "trace": traceback.format_exc(),
        })

def cmd_check(args):
    """Verifica que Earth Engine inicialice correctamente con el Project ID."""
    try:
        import ee
        ee.Initialize(project=args.project)
        _out({"ok": True, "project": args.project, "ping": ee.Number(1).getInfo()})
    except Exception as exc:
        _out({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _load_gj(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _as_float_xy(value):
    """Normaliza una coordenada GeoJSON a [lon, lat] 2D."""
    try:
        x = float(value[0])
        y = float(value[1])
    except Exception:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return [x, y]


def _coords_in_wgs84(coords):
    """Valida rangos geográficos para coordenadas lon/lat."""
    stack = [coords]
    while stack:
        item = stack.pop()
        if not isinstance(item, (list, tuple)) or not item:
            return False
        if isinstance(item[0], (int, float)):
            xy = _as_float_xy(item)
            if xy is None:
                return False
            x, y = xy
            if x < -180 or x > 180 or y < -90 or y > 90:
                return False
        else:
            stack.extend(item)
    return True


def _clean_position(pos):
    """Convierte posiciones 3D/M a 2D, descartando coordenadas no finitas."""
    return _as_float_xy(pos)


def _clean_line(coords):
    out = []
    for pos in coords or []:
        xy = _clean_position(pos)
        if xy is None:
            continue
        # Evita puntos consecutivos idénticos, que pueden romper geometrías pequeñas.
        if not out or out[-1] != xy:
            out.append(xy)
    return out


def _clean_ring(coords):
    ring = _clean_line(coords)
    if len(ring) < 3:
        return None
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    # Un anillo válido requiere al menos 4 posiciones incluyendo cierre.
    unique = {tuple(p) for p in ring[:-1]}
    if len(ring) < 4 or len(unique) < 3:
        return None
    return ring


def _clean_polygon(coords):
    rings = []
    for i, ring in enumerate(coords or []):
        clean = _clean_ring(ring)
        if clean is None:
            continue
        # Primer anillo = exterior; los demás = huecos. Huecos inválidos se omiten.
        rings.append(clean)
    if not rings:
        return None
    return rings


def _sanitize_geojson_geometry(geom):
    """
    Convierte la geometría GeoJSON exportada desde QGIS a una forma aceptada por Earth Engine.

    Corrige los casos que originan 'EEException: Invalid GeoJSON geometry':
    - coordenadas Z/M (se reducen a lon/lat 2D),
    - anillos sin cierre explícito,
    - geometrías vacías,
    - GeometryCollection resultante de makeValid(), conservando solo componentes poligonales,
    - coordenadas fuera de EPSG:4326.
    """
    if not isinstance(geom, dict):
        return None, "La geometría no es un objeto GeoJSON."

    gtype = geom.get("type")
    coords = geom.get("coordinates")

    if gtype == "Polygon":
        poly = _clean_polygon(coords)
        if not poly:
            return None, "Polígono vacío o con anillos inválidos."
        clean = {"type": "Polygon", "coordinates": poly}

    elif gtype == "MultiPolygon":
        polys = []
        for poly in coords or []:
            clean_poly = _clean_polygon(poly)
            if clean_poly:
                polys.append(clean_poly)
        if not polys:
            return None, "Multipolígono vacío o sin partes válidas."
        clean = {"type": "MultiPolygon", "coordinates": polys}

    elif gtype == "GeometryCollection":
        polys = []
        for sub in geom.get("geometries") or []:
            sub_clean, _ = _sanitize_geojson_geometry(sub)
            if not sub_clean:
                continue
            if sub_clean.get("type") == "Polygon":
                polys.append(sub_clean["coordinates"])
            elif sub_clean.get("type") == "MultiPolygon":
                polys.extend(sub_clean["coordinates"])
        if not polys:
            return None, "GeometryCollection sin componentes poligonales válidos."
        clean = {"type": "MultiPolygon", "coordinates": polys}

    else:
        return None, f"Tipo de geometría no soportado para AOI: {gtype}. Use polígonos."

    if not _coords_in_wgs84(clean.get("coordinates")):
        return None, (
            "Coordenadas fuera del rango lon/lat EPSG:4326. "
            "Revise el CRS de la capa en QGIS o reproyecte el AOI antes de ejecutar."
        )

    return clean, None


def _normalize_geojson_to_fc(gj):
    """Devuelve siempre un FeatureCollection local, saneado y con _pid estable."""
    if isinstance(gj, dict) and gj.get("type") == "FeatureCollection":
        features = gj.get("features", [])
    elif isinstance(gj, dict) and gj.get("type") == "Feature":
        features = [gj]
    elif isinstance(gj, dict):
        features = [{"type": "Feature", "properties": {}, "geometry": gj}]
    else:
        features = []

    norm_features = []
    errors = []
    for i, ft in enumerate(features):
        props = dict(ft.get("properties") or {})
        try:
            props["_pid"] = int(props.get("_pid", i))
        except (TypeError, ValueError):
            props["_pid"] = i

        geom, err = _sanitize_geojson_geometry(ft.get("geometry"))
        if err:
            errors.append(f"feature {i}: {err}")
            continue

        norm_features.append({
            "type": "Feature",
            "properties": props,
            "geometry": geom,
        })

    if not norm_features:
        detail = "; ".join(errors[:5]) if errors else "sin geometrías válidas"
        raise ValueError(
            "El AOI no contiene geometrías poligonales válidas para Earth Engine: " + detail
        )

    return {"type": "FeatureCollection", "features": norm_features}


def _load_fc(ee, aoi_path):
    """Carga AOI local y devuelve ee.FeatureCollection + GeoJSON normalizado."""
    gj = _normalize_geojson_to_fc(_load_gj(aoi_path))
    feats = []
    for i, ft in enumerate(gj.get("features", [])):
        geom = ft.get("geometry")
        if geom is None:
            continue
        try:
            # geodesic=False evita problemas con polígonos rectangulares locales y AOI pequeños.
            ee_geom = ee.Geometry(geom, None, False)
            feats.append(ee.Feature(ee_geom, dict(ft.get("properties") or {})))
        except Exception as exc:
            raise ValueError(
                f"Earth Engine rechazó la geometría del feature {i}. "
                f"Revise si el AOI está en EPSG:4326, si tiene partes vacías o autointersecciones. "
                f"Detalle: {type(exc).__name__}: {exc}"
            )
    return ee.FeatureCollection(feats), gj


def _filter_local_features(features, field=None, category=None):
    """Filtra features GeoJSON localmente usando comparación string y numérica."""
    if not field or not category or category == "(todas)":
        return list(features)

    cat = str(category)

    def keep(ft):
        props = ft.get("properties") or {}
        val = props.get(field)
        if val is None:
            return False
        if str(val) == cat:
            return True
        try:
            return float(val) == float(cat)
        except (TypeError, ValueError):
            return False

    return [ft for ft in features if keep(ft)]


def cmd_fields(args):
    """Lista campos y, opcionalmente, valores únicos de un campo en el AOI."""
    try:
        gj = _normalize_geojson_to_fc(_load_gj(args.aoi))
        feats = gj.get("features", [])
        fields = sorted({
            k for ft in feats for k in (ft.get("properties") or {}) if k != "_pid"
        })
        values = None
        if args.field:
            values = sorted({
                str((ft.get("properties") or {}).get(args.field))
                for ft in feats
                if (ft.get("properties") or {}).get(args.field) is not None
            })
        _out({"ok": True, "fields": fields, "values": values})
    except Exception as exc:
        _out({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def _parse_stats(stats_text):
    if not stats_text:
        return ["mean"]
    selected = []
    for raw in stats_text.split(","):
        stat = raw.strip()
        if stat in VALID_STATS and stat not in selected:
            selected.append(stat)
    return selected or ["mean"]


def _single_reducer(ee, stat):
    reducers = {
        "mean": ee.Reducer.mean(),
        "median": ee.Reducer.median(),
        "min": ee.Reducer.min(),
        "max": ee.Reducer.max(),
        "stdDev": ee.Reducer.stdDev(),
    }
    return reducers[stat]


def _combined_reducer(ee, stats):
    red = _single_reducer(ee, stats[0])
    for stat in stats[1:]:
        red = red.combine(_single_reducer(ee, stat), sharedInputs=True)
    return red


def _combined_reducer_with_count(ee, stats):
    red = _combined_reducer(ee, stats)
    red = red.combine(ee.Reducer.count(), sharedInputs=True)
    return red


def _read_stat_property(props, stat):
    candidates = [stat, f"NDVI_{stat}"]
    if stat == "mean":
        candidates.extend(["NDVI", "mean"])
    for key in candidates:
        if key in props and props.get(key) is not None:
            return props.get(key)
    return None


def _read_count_property(props):
    for key in ("count", "NDVI_count"):
        if key in props and props.get(key) is not None:
            return props.get(key)
    return None


def _mask_s2_scl(img):
    """Máscara básica de nubes/sombras usando SCL de Sentinel-2 SR/Sen2Cor."""
    import ee
    scl = img.select("SCL")
    mask = (
        scl.neq(3)        # cloud shadow
        .And(scl.neq(8))  # cloud medium probability
        .And(scl.neq(9))  # cloud high probability
        .And(scl.neq(10)) # cirrus
        .And(scl.neq(11)) # snow/ice
    )
    return img.updateMask(mask)


def _mask_s2_toa_qa60(img):
    """Máscara básica de nubes/cirrus para Sentinel-2 L1C/TOA usando QA60."""
    qa = img.select("QA60")
    cloud_bit = 1 << 10
    cirrus_bit = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))
    return img.updateMask(mask)


def _safe_region_from_fc(ee, fc, scale, simplify_meters):
    """
    Construye geometrías de trabajo.

    region_reduce: geometría para reduceRegion. Se simplifica de forma moderada
    para evitar geometrías demasiado complejas en GEE.

    region_filter: bounding box de region_reduce para filterBounds. Esto evita
    que filterBounds evalúe intersecciones contra polígonos muy complejos.
    """
    max_error = max(float(scale), 10.0)
    region_reduce = fc.geometry(max_error)

    simplify = float(simplify_meters or 0)
    if simplify > 0:
        region_reduce = region_reduce.simplify(simplify)

    region_filter = region_reduce.bounds(max_error)
    return region_reduce, region_filter


def _simplify_fc(ee, fc, simplify_meters):
    """Simplifica geometrías de polígonos para reduceRegions, conservando atributos."""
    simplify = float(simplify_meters or 0)
    if simplify <= 0:
        return fc

    def simp(ft):
        return ft.setGeometry(ft.geometry().simplify(simplify))

    return fc.map(simp)


def _collection(ee, filter_geom, start, end, cloud, composite, input_collection="sen2cor_sr"):
    """Construye colección NDVI Sentinel-2 y aplica compuesto temporal.

    input_collection:
      - sen2cor_sr: COPERNICUS/S2_SR_HARMONIZED, con máscara SCL.
      - toa_l1c: COPERNICUS/S2_HARMONIZED, con máscara QA60.
    """
    input_collection = input_collection if input_collection in VALID_INPUT_COLLECTIONS else "sen2cor_sr"

    if input_collection == "toa_l1c":
        raw = (
            ee.ImageCollection("COPERNICUS/S2_HARMONIZED")
            .filterBounds(filter_geom)
            .filterDate(start, end)
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud))
            .map(_mask_s2_toa_qa60)
            .map(lambda im: im.normalizedDifference(["B8", "B4"]).rename("NDVI")
                 .copyProperties(im, ["system:time_start", "CLOUDY_PIXEL_PERCENTAGE", "MGRS_TILE"]))
        )
    else:
        raw = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(filter_geom)
            .filterDate(start, end)
            .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud))
            .map(_mask_s2_scl)
            .map(lambda im: im.normalizedDifference(["B8", "B4"]).rename("NDVI")
                 .copyProperties(im, ["system:time_start", "CLOUDY_PIXEL_PERCENTAGE", "MGRS_TILE"]))
        )

    if composite == "none":
        # Importante: no se mosaica diariamente aquí. El mosaico diario con
        # aggregate_array/distinct fue el origen probable del error de memoria.
        return raw.sort("system:time_start")

    start_date = ee.Date(start)
    end_date = ee.Date(end)
    unit = "month" if composite == "monthly" else "year"
    n_periods = end_date.difference(start_date, unit).ceil()

    def mk(i):
        period_start = start_date.advance(i, unit)
        period_end = period_start.advance(1, unit)
        sub = raw.filterDate(period_start, period_end)
        return (
            sub.median()
            .rename("NDVI")
            .set("system:time_start", period_start.millis())
            .set("date", period_start.format("YYYY-MM-dd"))
            .set("n_images_input", sub.size())
        )

    comp = ee.ImageCollection(ee.List.sequence(0, n_periods.subtract(1)).map(mk))
    return comp.filter(ee.Filter.gt("n_images_input", 0)).sort("system:time_start")


def _filter_fc_by_field(ee, fc, field=None, category=None):
    if not field or not category or category == "(todas)":
        return fc
    try:
        num = float(category)
        return fc.filter(ee.Filter.Or(ee.Filter.eq(field, category), ee.Filter.eq(field, num)))
    except ValueError:
        return fc.filter(ee.Filter.eq(field, category))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)



def _parse_years(text):
    """Convierte '2015-2019' o '2015,2016,2019' en un conjunto de años.

    Cadena vacía = todos los años disponibles para el modo de corrección.
    """
    if not text:
        return set()
    years = set()
    for raw in str(text).replace(";", ",").replace("–", "-").replace("—", "-").split(","):
        raw = raw.strip()
        if not raw:
            continue
        if "-" in raw:
            parts = [p.strip() for p in raw.split("-") if p.strip()]
            if len(parts) == 2:
                try:
                    a, b = int(parts[0]), int(parts[1])
                    lo, hi = min(a, b), max(a, b)
                    for y in range(lo, hi + 1):
                        years.add(y)
                    continue
                except ValueError:
                    pass
        try:
            years.add(int(raw))
        except ValueError:
            continue
    return years


def _single_year_from_dates(start, end):
    """Devuelve un año único si start/end pertenecen al mismo año; si no, None."""
    try:
        sy = int(str(start)[:4])
        ey = int(str(end)[:4])
        return sy if sy == ey else None
    except (TypeError, ValueError):
        return None


def _year_from_row(row):
    """Extrae año desde date='YYYY-MM-dd' o desde una columna year/anio si existe."""
    for key in ("year", "anio"):
        value = row.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    date = row.get("date")
    if date:
        try:
            return int(str(date)[:4])
        except (TypeError, ValueError):
            return None
    return None


def _clip_ndvi(value):
    if value is None or not _is_number(value):
        return value
    return max(-1.0, min(1.0, float(value)))


def _metadata_for_correction(mode, year):
    """Devuelve metadatos trazables del factor aplicado para el año/modo."""
    if mode in ("toa_to_lasrc_auto", "toa_to_lasrc_timeline_2015_2019"):
        return dict(TOA_TO_LASRC_TIMELINE.get(year) or {})

    if mode == "toa_to_sen2cor_2019" and year == 2019:
        return {
            "delta": TOA_TO_SEN2COR_YEAR_FACTOR[2019],
            "sigma": None,
            "estimate_type": "empírico",
            "regime": "triangulación 2019",
            "basis": "NDVI_Sen2Cor - NDVI_L1C, derivado de la triangulación A3",
            "applicability": "validada para 2019",
        }

    if mode == "sen2cor_to_lasrc_deming_2019" and year == 2019:
        return {
            "delta": None,
            "sigma": None,
            "estimate_type": "empírico",
            "regime": "regresión ortogonal Deming 2019",
            "basis": "NDVI_LaSRC ≈ 0.76 × NDVI_Sen2Cor + 0.21",
            "applicability": "validada para 2019",
        }

    if mode == "manual_additive":
        return {
            "delta": None,
            "sigma": None,
            "estimate_type": "manual",
            "regime": "definido por el usuario",
            "basis": "valor manual aditivo",
            "applicability": "requiere justificación metodológica",
        }

    return {}


def _correction_delta_for_value(value, stat, mode, year, manual_value):
    """
    Devuelve (valor_corregido, delta_aplicado). Para stdDev, una corrección aditiva
    no cambia la dispersión; la corrección Deming lineal sí escala la dispersión.
    """
    if value is None or not _is_number(value):
        return value, None

    value = float(value)

    if mode == "none":
        return value, 0.0

    if mode == "manual_additive":
        if stat == "stdDev":
            return value, 0.0
        delta = float(manual_value or 0.0)
        return _clip_ndvi(value + delta), delta

    if mode in ("toa_to_lasrc_auto", "toa_to_lasrc_timeline_2015_2019"):
        if stat == "stdDev":
            return value, 0.0
        meta = TOA_TO_LASRC_TIMELINE.get(year)
        if not meta:
            return value, None
        delta = float(meta["delta"])
        return _clip_ndvi(value + delta), delta

    if mode == "toa_to_sen2cor_2019":
        if stat == "stdDev":
            return value, 0.0
        if year not in TOA_TO_SEN2COR_YEAR_FACTOR:
            return value, None
        delta = float(TOA_TO_SEN2COR_YEAR_FACTOR[year])
        return _clip_ndvi(value + delta), delta

    if mode == "sen2cor_to_lasrc_deming_2019":
        # NDVI_LaSRC ≈ 0.76 × NDVI_Sen2Cor + 0.21, derivado de la triangulación 2019.
        if year is not None and year != 2019:
            return value, None
        if stat == "stdDev":
            corrected = max(0.0, SEN2COR_TO_LASRC_DEMING_SLOPE * value)
            return corrected, corrected - value
        corrected = _clip_ndvi(SEN2COR_TO_LASRC_DEMING_SLOPE * value + SEN2COR_TO_LASRC_DEMING_INTERCEPT)
        return corrected, corrected - value

    return value, None


def _apply_radiometric_correction(rows, stats, mode="none", years_text="", manual_value=0.0, fallback_year=None):
    """
    Aplica corrección solamente a los años indicados. Conserva trazabilidad:
    ndvi_<stat>_raw, ndvi_<stat>_correction y metadatos del factor/año.
    """
    if not rows:
        return rows

    mode = mode if mode in VALID_CORRECTION_MODES else "none"
    if mode == "none":
        return rows

    years = _parse_years(years_text)
    out = []
    for row in rows:
        new = dict(row)
        year = _year_from_row(new)
        if year is None and fallback_year is not None:
            year = fallback_year
        applies_by_year = (not years) or (year in years)
        any_applied = False
        meta = _metadata_for_correction(mode, year) if applies_by_year else {}

        for stat in stats:
            key = f"ndvi_{stat}"
            if key not in new or new.get(key) is None:
                continue
            raw = new.get(key)
            new[f"{key}_raw"] = raw

            if not applies_by_year:
                new[f"{key}_correction"] = 0.0
                continue

            corrected, delta = _correction_delta_for_value(raw, stat, mode, year, manual_value)
            if delta is None:
                new[f"{key}_correction"] = 0.0
                continue

            new[key] = corrected
            new[f"{key}_correction"] = delta
            any_applied = any_applied or abs(float(delta)) > 0

        new["radiometric_correction_mode"] = mode
        new["radiometric_correction_year"] = year
        new["radiometric_correction_applied"] = 1 if any_applied else 0
        new["radiometric_correction_factor_type"] = meta.get("estimate_type")
        new["radiometric_correction_sigma"] = meta.get("sigma")
        new["radiometric_correction_regime"] = meta.get("regime")
        new["radiometric_correction_basis"] = meta.get("basis")
        new["radiometric_correction_applicability"] = meta.get("applicability")
        out.append(new)

    return out


def _correction_note(mode, years_text, manual_value):
    if mode == "none":
        return "Sin corrección radiométrica aplicada."
    years = sorted(_parse_years(years_text))
    years_label = ",".join(str(y) for y in years) if years else "todos los años disponibles del modo"
    if mode in ("toa_to_lasrc_auto", "toa_to_lasrc_timeline_2015_2019"):
        return (
            "Corrección radiométrica TOA→SR/LaSRC por línea temporal aplicada a años "
            f"{years_label}: 2015=+0.288 (extrapolación), 2016=+0.299 (extrapolación), "
            "2017=+0.309 (empírico), 2018=+0.320 (interpolación), 2019=+0.330 (empírico). "
            "2015–2016 son solo teóricos y requieren validación empírica previa."
        )
    if mode == "toa_to_sen2cor_2019":
        return (
            "Corrección radiométrica TOA→SR/Sen2Cor 2019 aplicada solo a años "
            f"{years_label}: +0.2714 NDVI."
        )
    if mode == "sen2cor_to_lasrc_deming_2019":
        return (
            "Corrección Sen2Cor→LaSRC 2019 aplicada solo a años "
            f"{years_label}: NDVI_corr = 0.76 × NDVI_Sen2Cor + 0.21."
        )
    if mode == "manual_additive":
        return f"Corrección radiométrica manual aditiva aplicada a años {years_label}: {float(manual_value):+.4f} NDVI."
    return "Corrección radiométrica solicitada no reconocida."


def _csv_columns_with_correction(base_cols, stats, include_date=True):
    cols = []
    for col in base_cols:
        cols.append(col)
        if col == "date" and include_date:
            continue
    # Esta función queda disponible para extensiones; las columnas se arman explícitamente en cmd_ndvi.
    return cols


def _aggregate_rows_by_date(rows, stats):
    """Agrega salidas por fecha para evitar líneas verticales en el gráfico."""
    buckets = {}
    for row in rows:
        date = row.get("date")
        if not date:
            continue
        buckets.setdefault(date, []).append(row)

    out = []
    for date, items in sorted(buckets.items()):
        new = {"date": date}
        counts = [r.get("n_valid_pixels") for r in items if _is_number(r.get("n_valid_pixels"))]
        total_count = sum(counts) if counts else None

        for stat in stats:
            key = f"ndvi_{stat}"
            vals = [r.get(key) for r in items if _is_number(r.get(key))]
            if not vals:
                new[key] = None
                continue

            if stat == "mean":
                # Promedio ponderado por píxeles válidos cuando sea posible.
                weighted_num = 0.0
                weighted_den = 0.0
                for r in items:
                    value = r.get(key)
                    count = r.get("n_valid_pixels")
                    if _is_number(value) and _is_number(count) and count > 0:
                        weighted_num += float(value) * float(count)
                        weighted_den += float(count)
                new[key] = weighted_num / weighted_den if weighted_den > 0 else statistics.fmean(vals)
            elif stat == "median":
                new[key] = statistics.median(vals)
            elif stat == "min":
                new[key] = min(vals)
            elif stat == "max":
                new[key] = max(vals)
            elif stat == "stdDev":
                # Aproximación defensiva cuando hay varias escenas en la fecha.
                new[key] = statistics.fmean(vals)
            else:
                new[key] = statistics.fmean(vals)

        new["n_valid_pixels"] = total_count
        new["n_images_input"] = sum(
            int(r.get("n_images_input") or 1) for r in items
        )
        new["n_obs_same_date"] = len(items)
        out.append(new)

    return out


def _aoi_series(ee, col, region_reduce, scale, stats, tile_scale):
    """Calcula serie temporal AOI para todos los estadísticos solicitados."""
    reducer = _combined_reducer_with_count(ee, stats)

    def per_image(img):
        date_txt = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        props = {
            "date": date_txt,
            "n_images_input": img.get("n_images_input"),
        }

        rr = img.reduceRegion(
            reducer=reducer,
            geometry=region_reduce,
            scale=scale,
            maxPixels=1e13,
            bestEffort=True,
            tileScale=tile_scale,
        )

        for stat in stats:
            props[f"ndvi_{stat}"] = rr.get(stat, rr.get(f"NDVI_{stat}"))
        props["n_valid_pixels"] = rr.get("count", rr.get("NDVI_count"))

        return ee.Feature(None, props)

    not_null_cols = [f"ndvi_{stat}" for stat in stats]
    ts = col.map(per_image).filter(ee.Filter.notNull(not_null_cols))
    features = ts.getInfo().get("features", [])

    rows = []
    for ft in features:
        props = ft.get("properties", {})
        row = {"date": props.get("date")}
        for stat in stats:
            row[f"ndvi_{stat}"] = props.get(f"ndvi_{stat}")
        row["n_valid_pixels"] = props.get("n_valid_pixels")
        row["n_images_input"] = props.get("n_images_input") or 1
        rows.append(row)

    return _aggregate_rows_by_date(rows, stats)


def _period_by_polygon(ee, col, fc, scale, stats, field=None, tile_scale=4):
    """Estadísticos espaciales por polígono sobre el compuesto mediano del periodo."""
    reducer = _combined_reducer(ee, stats)
    composite_img = col.median().rename("NDVI")
    reduced = composite_img.reduceRegions(
        collection=fc,
        reducer=reducer,
        scale=scale,
        tileScale=tile_scale,
    ).getInfo()

    rows = []
    for ft in reduced.get("features", []):
        props = ft.get("properties", {})
        row = {"_pid": props.get("_pid")}
        if field:
            row[field] = props.get(field)
        for stat in stats:
            row[f"ndvi_{stat}"] = _read_stat_property(props, stat)
        rows.append(row)
    return rows, composite_img


def _polygon_time_series(ee, col, fc, scale, stats, field=None, tile_scale=4):
    """Calcula serie temporal por polígono x fecha respetando stats."""
    reducer = _combined_reducer(ee, stats)

    def per_img_polys(img):
        date_txt = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd")
        n_input = img.get("n_images_input")
        fcr = img.reduceRegions(
            collection=fc,
            reducer=reducer,
            scale=scale,
            tileScale=tile_scale,
        )
        return fcr.map(lambda f: f.set("date", date_txt).set("n_images_input", n_input))

    flat = ee.FeatureCollection(col.map(per_img_polys)).flatten()
    features = flat.getInfo().get("features", [])

    rows = []
    for ft in features:
        props = ft.get("properties", {})
        row = {"_pid": props.get("_pid"), "date": props.get("date")}
        if field:
            row[field] = props.get(field)
        for stat in stats:
            row[f"ndvi_{stat}"] = _read_stat_property(props, stat)
        row["n_images_input"] = props.get("n_images_input") or 1
        if any(row.get(f"ndvi_{stat}") is not None for stat in stats):
            rows.append(row)

    rows.sort(key=lambda r: (r.get("_pid") is None, r.get("_pid"), r.get("date") or ""))
    return rows


def _write_csv(path, rows, columns=None):
    if not path or not rows:
        return
    if columns is None:
        columns = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col) for col in columns})


def _build_output_geojson(filtered_features, by_poly, stats):
    """Cruza resultados por _pid y devuelve GeoJSON final."""
    ndvi_by_pid = {}
    for row in by_poly:
        pid = row.get("_pid")
        if pid is not None:
            try:
                ndvi_by_pid[int(pid)] = row
            except (TypeError, ValueError):
                pass

    out_features = []
    for ft in filtered_features:
        props = dict(ft.get("properties") or {})
        pid = props.get("_pid")
        try:
            pid_int = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid_int = None

        if pid_int in ndvi_by_pid:
            row_src = ndvi_by_pid[pid_int]
            for stat in stats:
                props[f"ndvi_{stat}"] = row_src.get(f"ndvi_{stat}")
                props[f"ndvi_{stat}_raw"] = row_src.get(f"ndvi_{stat}_raw")
                props[f"ndvi_{stat}_correction"] = row_src.get(f"ndvi_{stat}_correction")
            for key in (["radiometric_correction_mode", "radiometric_correction_year", "radiometric_correction_applied"] + CORRECTION_META_COLUMNS):
                if key in row_src:
                    props[key] = row_src.get(key)

        out_features.append({
            "type": "Feature",
            "properties": props,
            "geometry": ft.get("geometry"),
        })

    return {"type": "FeatureCollection", "features": out_features}


def _start_drive_export(ee, image, region, scale, prefix, folder):
    """Lanza una tarea de exportación a Google Drive y devuelve metadatos."""
    task = ee.batch.Export.image.toDrive(
        image=image.clip(region),
        description=prefix,
        folder=folder,
        fileNamePrefix=prefix,
        region=region,
        scale=scale,
        maxPixels=1e13,
    )
    task.start()
    return {"task_id": task.id, "state": task.status().get("state"), "prefix": prefix, "folder": folder}


def cmd_ndvi(args):
    import ee

    try:
        ee.Initialize(project=args.project)
        fc, full_gj = _load_fc(ee, args.aoi)

        all_features = full_gj.get("features", [])
        filtered_gj_features = _filter_local_features(all_features, args.field, args.category)
        if not filtered_gj_features:
            _out({"ok": False, "error": "No hay polígonos tras el filtro."})
            return

        fc = _filter_fc_by_field(ee, fc, args.field, args.category)
        n_polys = len(filtered_gj_features)  # conteo local: evita fc.size().getInfo()

        stats = _parse_stats(args.stats)
        scale = float(args.scale)
        tile_scale = int(args.tile_scale)
        simplify_meters = float(args.simplify_meters)

        fc_work = _simplify_fc(ee, fc, simplify_meters)
        region_reduce, region_filter = _safe_region_from_fc(
            ee, fc_work, scale=scale, simplify_meters=simplify_meters
        )

        input_collection = getattr(args, "input_collection", "sen2cor_sr") or "sen2cor_sr"
        if input_collection not in VALID_INPUT_COLLECTIONS:
            input_collection = "sen2cor_sr"

        col = _collection(
            ee=ee,
            filter_geom=region_filter,
            start=args.start,
            end=args.end,
            cloud=args.cloud,
            composite=args.composite,
            input_collection=input_collection,
        )

        # 1) Serie temporal AOI con stats seleccionados.
        #    Este es el primer cálculo que fuerza evaluación en GEE.
        series = _aoi_series(ee, col, region_reduce, scale, stats, tile_scale)
        if not series:
            _out({"ok": False, "error": "Sin imágenes válidas para AOI+fechas+nubes."})
            return

        n_raw_images_est = sum(int(row.get("n_obs_same_date") or 1) for row in series)
        n_images = len(series)

        # 2) Estadísticos por polígono sobre compuesto mediano del periodo.
        by_poly, composite_img = _period_by_polygon(
            ee=ee,
            col=col,
            fc=fc_work,
            scale=scale,
            stats=stats,
            field=args.field,
            tile_scale=tile_scale,
        )

        # 3) Corrección radiométrica opcional, aplicada únicamente a los años indicados.
        correction_mode = getattr(args, "radiometric_correction_mode", "none") or "none"
        correction_years = getattr(args, "correction_years", "") or ""
        manual_correction = float(getattr(args, "manual_correction", 0.0) or 0.0)

        fallback_year = _single_year_from_dates(args.start, args.end)
        series = _apply_radiometric_correction(
            series, stats, correction_mode, correction_years, manual_correction
        )
        by_poly = _apply_radiometric_correction(
            by_poly, stats, correction_mode, correction_years, manual_correction, fallback_year=fallback_year
        )

        # 4) GeoJSON con cruce estable por _pid.
        out_gj = _build_output_geojson(filtered_gj_features, by_poly, stats)

        result = {
            "ok": True,
            "n_raw_images": n_raw_images_est,
            "n_images": n_images,
            "n_polys": n_polys,
            "composite": args.composite,
            "input_collection": input_collection,
            "stats": stats,
            "field": args.field,
            "category": args.category,
            "series": series,
            "by_poly": by_poly,
            "tile_scale": tile_scale,
            "simplify_meters": simplify_meters,
            "radiometric_correction_mode": correction_mode,
            "radiometric_correction_years": correction_years,
            "radiometric_correction_note": _correction_note(correction_mode, correction_years, manual_correction),
            "method_note": (
                "v0.4.1 memory-safe: composite=none usa escenas Sentinel-2 agregadas por fecha en Python, "
                "sin mosaico diario por aggregate_array/distinct. by_poly contiene estadísticos espaciales "
                "por polígono sobre el compuesto mediano del periodo."
            ),
        }

        # 4) Serie por polígono x fecha opcional.
        if args.out_csv_polyts:
            poly_ts = _polygon_time_series(
                ee=ee,
                col=col,
                fc=fc_work,
                scale=scale,
                stats=stats,
                field=args.field,
                tile_scale=tile_scale,
            )
            if poly_ts:
                poly_ts = _apply_radiometric_correction(
                    poly_ts, stats, correction_mode, correction_years, manual_correction
                )
                cols = ["_pid", "date"]
                if args.field:
                    cols.append(args.field)
                for stat in stats:
                    cols += [f"ndvi_{stat}", f"ndvi_{stat}_raw", f"ndvi_{stat}_correction"]
                cols += ["n_images_input", "radiometric_correction_mode", "radiometric_correction_year", "radiometric_correction_applied"] + CORRECTION_META_COLUMNS
                _write_csv(args.out_csv_polyts, poly_ts, cols)
                result["out_csv_polyts"] = args.out_csv_polyts

        # 5) Escritura de archivos.
        if args.out_csv:
            cols = ["date"]
            for stat in stats:
                cols += [f"ndvi_{stat}", f"ndvi_{stat}_raw", f"ndvi_{stat}_correction"]
            cols += [
                "n_valid_pixels", "n_images_input", "n_obs_same_date",
                "radiometric_correction_mode", "radiometric_correction_year", "radiometric_correction_applied"
            ] + CORRECTION_META_COLUMNS
            _write_csv(args.out_csv, series, cols)
            result["out_csv"] = args.out_csv

        if args.out_csv_poly and by_poly:
            cols = ["_pid"]
            if args.field:
                cols.append(args.field)
            for stat in stats:
                cols += [f"ndvi_{stat}", f"ndvi_{stat}_raw", f"ndvi_{stat}_correction"]
            cols += ["radiometric_correction_mode", "radiometric_correction_year", "radiometric_correction_applied"] + CORRECTION_META_COLUMNS
            _write_csv(args.out_csv_poly, by_poly, cols)
            result["out_csv_poly"] = args.out_csv_poly

        if args.out_geojson and out_gj.get("features"):
            with open(args.out_geojson, "w", encoding="utf-8") as fh:
                json.dump(out_gj, fh, ensure_ascii=False)
            result["out_geojson"] = args.out_geojson

        # 6) Exportación opcional a Google Drive.
        if args.export_drive:
            prefix = args.export_prefix or f"ndvi_composite_{args.start}_{args.end}"
            folder = args.drive_folder or "GEE_NDVI_QGIS"
            export_meta = _start_drive_export(ee, composite_img, region_reduce, scale, prefix, folder)
            result["drive_export"] = export_meta

        _out(result)

    except Exception as exc:
        _out({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc(),
        })


def main():
    parser = argparse.ArgumentParser(description="Backend GEE para NDVI multitemporal QGIS")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("auth-url", help="Autenticación interactiva de Earth Engine")
    pa.add_argument("--project", default=None)
    pa.add_argument("--auth-mode", default="localhost", choices=["localhost", "notebook", "gcloud", "colab", "auto"])
    pa.add_argument("--force", action="store_true")
    pa.set_defaults(func=cmd_auth_url)

    pc = sub.add_parser("check", help="Verificar conexión con Earth Engine")
    pc.add_argument("--project", required=True)
    pc.set_defaults(func=cmd_check)

    pf = sub.add_parser("fields", help="Listar campos/valores de un AOI GeoJSON")
    pf.add_argument("--aoi", required=True)
    pf.add_argument("--field", default=None)
    pf.set_defaults(func=cmd_fields)

    pn = sub.add_parser("ndvi", help="Calcular NDVI multitemporal")
    pn.add_argument("--project", required=True)
    pn.add_argument("--aoi", required=True)
    pn.add_argument("--start", required=True)
    pn.add_argument("--end", required=True)
    pn.add_argument("--cloud", type=float, default=20.0)
    pn.add_argument("--scale", type=float, default=10.0)
    pn.add_argument("--composite", default="none", choices=["none", "monthly", "yearly"])
    pn.add_argument("--input-collection", default="sen2cor_sr", choices=VALID_INPUT_COLLECTIONS)
    pn.add_argument("--stats", default="mean")
    pn.add_argument("--radiometric-correction-mode", default="none", choices=VALID_CORRECTION_MODES)
    pn.add_argument("--correction-years", default="")
    pn.add_argument("--manual-correction", type=float, default=0.0)
    pn.add_argument("--field", default=None)
    pn.add_argument("--category", default=None)
    pn.add_argument("--out-csv", default=None)
    pn.add_argument("--out-csv-poly", default=None)
    pn.add_argument("--out-csv-polyts", default=None)
    pn.add_argument("--out-geojson", default=None)
    pn.add_argument("--export-drive", action="store_true")
    pn.add_argument("--drive-folder", default="GEE_NDVI_QGIS")
    pn.add_argument("--export-prefix", default=None)

    # Parámetros de robustez computacional. No requieren cambios en la interfaz QGIS;
    # tienen valores por defecto pensados para AOI complejos.
    pn.add_argument("--tile-scale", type=int, default=8)
    pn.add_argument("--simplify-meters", type=float, default=30.0)

    pn.set_defaults(func=cmd_ndvi)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
