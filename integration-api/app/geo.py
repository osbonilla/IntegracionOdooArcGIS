"""
Utilidades geométricas y de "huellas" (fingerprints) para el merge.

Todo trabaja en WGS84 (grados). Las distancias se calculan con una
proyección equirectangular local alrededor del punto consultado: para
tramos de decenas o cientos de metros el error es despreciable y no hace
falta ninguna dependencia externa (shapely, pyproj, arcpy).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

EARTH_RADIUS_M = 6_371_008.8
# Tolerancia para considerar "el mismo punto": ~0,2 m. Absorbe el ruido de
# ida y vuelta WGS84 -> Web Mercator (almacenamiento de la capa) -> WGS84.
POINT_TOLERANCE_DEG = 2e-6


# ---------------------------------------------------------------- puntos ---

def valid_latlon(lat: Any, lon: Any) -> bool:
    """
    Coordenadas utilizables. Odoo guarda 0.0 cuando el contacto no está
    geolocalizado, y datos viejos pueden traer valores fuera de rango (p. ej.
    metros Web Mercator guardados como grados): ambos casos cuentan como
    "sin coordenadas".
    """
    if lat is None or lon is None or lat is False or lon is False:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if lat == 0.0 and lon == 0.0:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def same_point(a: tuple[float, float] | None, b: tuple[float, float] | None) -> bool:
    if a is None or b is None:
        return a is b
    return (abs(a[0] - b[0]) <= POINT_TOLERANCE_DEG
            and abs(a[1] - b[1]) <= POINT_TOLERANCE_DEG)


def point_fp(lat: float, lon: float) -> str:
    """Huella legible de un punto: 'lat,lon' con 6 decimales (~0,1 m)."""
    return f"{float(lat):.6f},{float(lon):.6f}"


def parse_point_fp(value: Any) -> tuple[float, float] | None:
    if not value or not isinstance(value, str) or "," not in value:
        return None
    try:
        lat, lon = value.split(",", 1)
        return float(lat), float(lon)
    except ValueError:
        return None


# ---------------------------------------------------------------- líneas ---

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def polyline_paths(geometry: dict | None) -> list[list[list[float]]]:
    """Caminos de una polilínea Esri JSON ({"paths": [[[x, y], ...], ...]})."""
    if not geometry:
        return []
    return [p for p in geometry.get("paths") or [] if len(p) >= 2]


def polyline_length_m(geometry: dict | None) -> float:
    total = 0.0
    for path in polyline_paths(geometry):
        for (x1, y1, *_), (x2, y2, *_) in zip(path, path[1:]):
            total += haversine_m(y1, x1, y2, x2)
    return total


def point_to_polyline_m(lat: float, lon: float, geometry: dict | None) -> float:
    """
    Distancia mínima (m) de un punto a una polilínea. Proyecta todo a un
    plano local centrado en el punto (x = Δlon·cos(lat), y = Δlat) y mide
    la distancia punto-segmento en ese plano.
    """
    best = math.inf
    k = math.cos(math.radians(lat))
    m_per_deg = math.pi * EARTH_RADIUS_M / 180.0

    def local(x: float, y: float) -> tuple[float, float]:
        return (x - lon) * k * m_per_deg, (y - lat) * m_per_deg

    for path in polyline_paths(geometry):
        pts = [local(v[0], v[1]) for v in path]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
            px, py = ax + t * dx, ay + t * dy
            best = min(best, math.hypot(px, py))
    return best


def polyline_midpoint(geometry: dict | None) -> tuple[float, float] | None:
    """Punto aproximado a mitad del recorrido (lat, lon), para enlaces al mapa."""
    paths = polyline_paths(geometry)
    if not paths:
        return None
    half = polyline_length_m(geometry) / 2
    walked = 0.0
    for path in paths:
        for (x1, y1, *_), (x2, y2, *_) in zip(path, path[1:]):
            seg = haversine_m(y1, x1, y2, x2)
            if walked + seg >= half and seg > 0:
                t = (half - walked) / seg
                return y1 + t * (y2 - y1), x1 + t * (x2 - x1)
            walked += seg
    x, y = paths[0][0][0], paths[0][0][1]
    return y, x


def rounded_paths(geometry: dict | None, ndigits: int = 6) -> list:
    return [[[round(v[0], ndigits), round(v[1], ndigits)] for v in path]
            for path in polyline_paths(geometry)]


# ---------------------------------------------------------------- huellas ---

def content_fp(content: Any) -> str:
    """Huella estable (sha1 truncado) de cualquier estructura JSON-serializable."""
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def text_fp(text: str | None) -> str:
    """Huella de un texto libre; texto vacío -> '' (no genera escrituras)."""
    text = (text or "").strip()
    return content_fp(text) if text else ""
