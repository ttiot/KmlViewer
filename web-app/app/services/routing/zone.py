"""
Zone couverte du constructeur de traces (gpx-route-builder RB-1.3b, admin-space EF-6.20). Module PUR : ni base, ni env.

La zone en vigueur est le polygone `routing.coverage_polygon` s'il n'est pas vide, sinon le rectangle `routing.coverage_bbox`
vu comme un polygone de 4 sommets. Un seul code de contrôle sert aux deux cas.

Géométrie commune : tout se calcule dans la projection de la carte (Web Mercator, en degrés), où la carte trace chaque
arête en ligne droite. Un sommet est un couple `(lon, lat)` en degrés.
"""

import math
from typing import Callable, Optional, Sequence

MAX_VERTICES = 50
MIN_VERTICES = 3
MAX_LAT = 85.0511
MAX_SIDE_M = 2_000_000.0
EDGE_EPSILON = 1e-9             # un point à 10⁻⁹ près (coordonnées projetées) d'une arête est sur le contour : dedans
EARTH_RADIUS_M = 6_371_000.0    # même rayon que `geo.haversine`
DICHOTOMY_STEPS = 12


def project(lon: float, lat: float) -> tuple:
    """Web Mercator en degrés : x = longitude, y = (180/π) · ln(tan(π/4 + lat · π/360))."""
    return float(lon), math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))


# -- Contrôles à l'écriture (EF-6.20) --------------------------------------------------------------------------------------

def _is_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (isinstance(value, float) and not math.isfinite(value))


def validate(value) -> list:
    """
    Liste de sommets `[lon, lat]` normalisée (flottants, sans arrondi) ; `[]` = aucun polygone. Lève `ValueError(message)`.
    Ordre des contrôles : nombre de sommets (avant d'en lire un seul), forme, bornes, emprise, polygone simple.
    """
    if not isinstance(value, (list, tuple)):
        raise ValueError("Zone : de 3 à 50 sommets.")
    if not value:
        return []
    if not MIN_VERTICES <= len(value) <= MAX_VERTICES:
        raise ValueError("Zone : de 3 à 50 sommets.")
    for vertex in value:
        if not isinstance(vertex, (list, tuple)) or len(vertex) != 2 or not all(_is_number(n) for n in vertex):
            raise ValueError("Zone : chaque sommet est [longitude, latitude].")
    for lon, lat in value:                      # bornes sur les nombres tels quels : un entier démesuré n'est jamais converti
        if not (-180 <= lon <= 180 and -MAX_LAT <= lat <= MAX_LAT):
            raise ValueError("Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511.")
    vertices = [[float(lon), float(lat)] for lon, lat in value]
    lons, lats = [v[0] for v in vertices], [v[1] for v in vertices]
    if box_too_large(min(lons), min(lats), max(lons), max(lats)):
        raise ValueError("Zone : emprise de 2 000 km de côté au plus.")
    crossing = first_crossing(vertices)
    if crossing is not None:
        (i, j), (k, l) = crossing
        raise ValueError(f"Zone : le contour se croise (arêtes {i}–{j} et {k}–{l}).")
    return vertices


def box_too_large(lon_min: float, lat_min: float, lon_max: float, lat_max: float) -> bool:
    """Même calcul que le rectangle (P-TR5-1) : largeur le long du parallèle le plus proche de l'équateur, hauteur le long
    d'un méridien, en haversine."""
    def haversine(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
        return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))
    parallel = 0.0 if lat_min <= 0 <= lat_max else min(abs(lat_min), abs(lat_max))
    return max(haversine(parallel, lon_min, parallel, lon_max), haversine(lat_min, lon_min, lat_max, lon_min)) > MAX_SIDE_M


def _orientation(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p) -> bool:
    """`p`, supposé aligné avec [a, b], est dans le segment (bornes comprises)."""
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def _segments_touch(a, b, c, d) -> bool:
    """Les segments [a, b] et [c, d] ont au moins un point commun (sommets et recouvrement compris)."""
    o1, o2, o3, o4 = _orientation(a, b, c), _orientation(a, b, d), _orientation(c, d, a), _orientation(c, d, b)
    if ((o1 > 0 > o2) or (o1 < 0 < o2)) and ((o3 > 0 > o4) or (o3 < 0 < o4)):
        return True
    return ((o1 == 0 and _on_segment(a, b, c)) or (o2 == 0 and _on_segment(a, b, d))
            or (o3 == 0 and _on_segment(c, d, a)) or (o4 == 0 and _on_segment(c, d, b)))


def _adjacent_overlap(a, b, c) -> bool:
    """Arêtes voisines [a, b] et [b, c] : seul `b` doit leur être commun (arête nulle ou retour en arrière = recouvrement)."""
    u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    if u == (0.0, 0.0) or v == (0.0, 0.0):
        return True
    return _orientation(a, b, c) == 0 and u[0] * v[0] + u[1] * v[1] < 0


def first_crossing(vertices: Sequence) -> Optional[tuple]:
    """
    Première paire d'arêtes qui se touchent ou se croisent, en parcourant les arêtes dans l'ordre : `((i, j), (k, l))`,
    numéros de sommets à partir de 1 (la dernière arête relie le sommet n au sommet 1). `None` : polygone simple.
    """
    points = [project(lon, lat) for lon, lat in vertices]
    count = len(points)
    for e1 in range(count):
        for e2 in range(e1 + 1, count):
            a, b = points[e1], points[(e1 + 1) % count]
            c, d = points[e2], points[(e2 + 1) % count]
            if e2 == e1 + 1 or (e1 == 0 and e2 == count - 1):            # voisines : un sommet en commun
                shared = (e1, e2) if e2 == e1 + 1 else (e2, e1)
                first, second = shared
                bad = _adjacent_overlap(points[first], points[(first + 1) % count], points[(second + 1) % count])
            else:
                bad = _segments_touch(a, b, c, d)
            if bad:
                return ((e1 + 1, (e1 + 1) % count + 1), (e2 + 1, (e2 + 1) % count + 1))
    return None


# -- Zone en vigueur -------------------------------------------------------------------------------------------------------

def rectangle_vertices(bbox) -> list:
    """Les 4 angles d'un rectangle `[lon_min, lat_min, lon_max, lat_max]` : sud-ouest, sud-est, nord-est, nord-ouest."""
    lon_min, lat_min, lon_max, lat_max = (float(v) for v in bbox)
    return [[lon_min, lat_min], [lon_max, lat_min], [lon_max, lat_max], [lon_min, lat_max]]


def effective_zone(get: Callable[[str], object]) -> list:
    """Sommets `[lon, lat]` de la zone en vigueur ; `[]` si ni polygone ni rectangle exploitable. `get` : clé -> valeur du registre."""
    try:
        polygon = validate(get('routing.coverage_polygon'))
    except (KeyError, ValueError):
        polygon = []
    if polygon:
        return polygon
    try:
        return rectangle_vertices(get('routing.coverage_bbox'))
    except (KeyError, TypeError, ValueError, IndexError):
        return []


def envelope(vertices: Sequence) -> tuple:
    """Emprise `(lat_min, lon_min, lat_max, lon_max)` : rectangle englobant des sommets."""
    lons, lats = [v[0] for v in vertices], [v[1] for v in vertices]
    return (min(lats), min(lons), max(lats), max(lons))


def envelope_lonlat(vertices: Sequence) -> list:
    """Emprise au format du registre : `[lon_min, lat_min, lon_max, lat_max]`."""
    lat_min, lon_min, lat_max, lon_max = envelope(vertices)
    return [lon_min, lat_min, lon_max, lat_max]


def _is_axis_rectangle(vertices: Sequence) -> bool:
    if len(vertices) != 4:
        return False
    lons, lats = {v[0] for v in vertices}, {v[1] for v in vertices}
    return len(lons) == 2 and len(lats) == 2 and len({(v[0], v[1]) for v in vertices}) == 4


def contains(vertices: Sequence, lat: float, lon: float) -> bool:
    """
    Le point est dans la zone (RB-1.3b). Pré-filtre sur l'emprise ; un rectangle aligné reste exactement le rectangle d'avant
    (bords compris) ; sinon : sur le contour à 10⁻⁹ près (projeté) = dedans, ailleurs règle pair-impair (demi-droite horizontale).
    """
    lat_min, lon_min, lat_max, lon_max = envelope(vertices)
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        return False
    if _is_axis_rectangle(vertices):
        return True
    px, py = project(lon, lat)
    points = [project(v[0], v[1]) for v in vertices]
    inside = False
    count = len(points)
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        if _distance_to_segment(px, py, a, b) <= EDGE_EPSILON:
            return True
        if (a[1] > py) != (b[1] > py) and px < a[0] + (py - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
            inside = not inside
    return inside


def _distance_to_segment(px, py, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / length2))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def pull_inside(vertices: Sequence, origin: tuple, target: tuple) -> Optional[tuple]:
    """
    Point technique ramené dans la zone (RB-1.3b) : dernier point dans la zone du segment `origin` → `target` (`(lat, lon)`),
    par dichotomie en 12 étapes au plus ; `target` lui-même s'il est dans la zone ; `None` si `origin` est hors zone.
    Déterministe.
    """
    if contains(vertices, target[0], target[1]):
        return target
    if not contains(vertices, origin[0], origin[1]):
        return None
    low, high = origin, target
    for _ in range(DICHOTOMY_STEPS):
        middle = ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)
        if contains(vertices, middle[0], middle[1]):
            low = middle
        else:
            high = middle
    return low
