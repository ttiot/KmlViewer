"""
Couverture pilotée par la zone (routing-zone-donnees ZD-5, ZD-6, ZD-7). Module PUR : ni base, ni env, ni réseau.

- `extract_bbox` : emprise d'extraction (zone + marge), pour l'extraction OSM et les dalles MNT ;
- `dem_tiles` : dalles 1° × 1° qui coupent cette emprise ;
- `resolve_regions` : régions du catalogue retenues pour un POLYGONE (pas pour son emprise), calcul exact par `shapely`
  dans la projection de la carte (Web Mercator, règle d'EF-6.20). Résultat déterministe, trié par identifiant.
"""

import math
from dataclasses import dataclass
from typing import Sequence

import shapely
from shapely.geometry import Polygon

from app.services.routing import zone

KM_PER_DEGREE = 111.1949             # 2π · 6 371 km / 360 (ZD-5)
MAX_LAT = zone.MAX_LAT
MIN_PART_RATIO = 1e-4                # 0,01 % de l'aire de la zone : en dessous, c'est un liseré de contours voisins (ZD-6)


def _outward(value: float, up: bool) -> float:
    """Arrondi vers l'extérieur à 4 décimales (au-dessus si `up`), sans bruit de flottant."""
    scaled = round(value * 1e4, 6)
    return (math.ceil(scaled) if up else math.floor(scaled)) / 1e4


def extract_bbox(vertices: Sequence, margin_km: int) -> list:
    """
    Emprise d'extraction `[lon_min, lat_min, lon_max, lat_max]` (ZD-5) : emprise de la zone, élargie de `margin_km`.
    Δlat = m / 111,1949 ; Δlon = m / (111,1949 · cos φ), φ = plus grande latitude absolue de l'emprise de la zone ;
    latitudes bornées à ±85,0511, longitudes à ±180 ; arrondi vers l'extérieur à 4 décimales.
    """
    lons, lats = [v[0] for v in vertices], [v[1] for v in vertices]
    lon_min, lon_max, lat_min, lat_max = min(lons), max(lons), min(lats), max(lats)
    d_lat = margin_km / KM_PER_DEGREE
    phi = max(abs(lat_min), abs(lat_max))
    d_lon = margin_km / (KM_PER_DEGREE * math.cos(math.radians(phi)))
    return [_outward(max(-180.0, lon_min - d_lon), False), _outward(max(-MAX_LAT, lat_min - d_lat), False),
            _outward(min(180.0, lon_max + d_lon), True), _outward(min(MAX_LAT, lat_max + d_lat), True)]


def dem_tiles(bbox: Sequence) -> list:
    """Dalles 1° × 1° dont le carré COUPE l'emprise (un contact de bord ne compte pas) : `[(lat, lon)]`, coin sud-ouest, trié."""
    lon_min, lat_min, lon_max, lat_max = bbox
    return [(lat, lon) for lat in range(math.floor(lat_min), math.ceil(lat_max))
            for lon in range(math.floor(lon_min), math.ceil(lon_max))]


@dataclass(frozen=True)
class Region:
    """Une région candidate : contour (Web Mercator) préparé une fois à l'import du catalogue (ZD-6)."""

    id: str
    url: str
    size: int
    geometry: object                      # shapely Polygon / MultiPolygon, valide, en Web Mercator

    @property
    def area(self) -> float:
        return self.geometry.area


@dataclass(frozen=True)
class Resolution:
    regions: tuple                        # Region retenues, triées par identifiant
    uncovered_pct: float                  # part de la zone hors des données, en %

    @property
    def ids(self) -> list:
        return [r.id for r in self.regions]

    @property
    def total_bytes(self) -> int:
        return sum(r.size for r in self.regions)


def zone_polygon(vertices: Sequence) -> Polygon:
    """Polygone de la zone en Web Mercator (valide : EF-6.20 garantit un polygone simple)."""
    return Polygon([zone.project(lon, lat) for lon, lat in vertices])


def resolve_regions(vertices: Sequence, candidates: Sequence) -> Resolution:
    """
    Résolution des régions sur le polygone (ZD-6) : les candidates dont l'emprise coupe celle de la zone sont prises par aire
    croissante (égalité : identifiant). *reste* = la zone ; pour chaque candidate, *part* = *reste* ∩ contour ; si l'aire de
    *part* atteint 0,01 % de l'aire de la zone, la région est retenue et *reste* perd son contour ; sinon *reste* est inchangé.
    La marge n'intervient pas.
    """
    target = zone_polygon(vertices)
    total = target.area
    if total <= 0:
        raise ValueError("zone sans surface")
    minx, miny, maxx, maxy = target.bounds
    near = [c for c in candidates
            if not (c.geometry.bounds[2] < minx or c.geometry.bounds[0] > maxx
                    or c.geometry.bounds[3] < miny or c.geometry.bounds[1] > maxy)]
    remaining, kept = target, []
    for candidate in sorted(near, key=lambda c: (c.area, c.id)):
        if remaining.is_empty:
            break
        if not remaining.intersects(candidate.geometry):
            continue
        if remaining.intersection(candidate.geometry).area >= MIN_PART_RATIO * total:
            kept.append(candidate)
            remaining = remaining.difference(candidate.geometry)
    return Resolution(tuple(sorted(kept, key=lambda c: c.id)), 100.0 * remaining.area / total)


def version() -> tuple:
    """(version de shapely, version de GEOS) : figées par un test (ADR-0013, SHP-1)."""
    return shapely.__version__, shapely.geos_version
