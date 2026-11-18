"""
Analyse avancée d'une trace : arrêts, montées et descentes, zones de vitesse,
accélérations, terrain et points remarquables. Fonctions pures (numpy).

Principes de fiabilité :
- rien n'est inventé : sans horodatage ni vitesse, une analyse est déclarée indisponible
  (voir `summary.unavailable`) au lieu d'être estimée ;
- les entrées sont assainies (coordonnées invalides, altitudes absentes ou aberrantes) ;
- l'altitude est rééchantillonnée à pas de distance constant puis lissée : le bruit du
  capteur à l'arrêt ou point à point ne fabrique ni pente ni dénivelé ;
- un arrêt se mesure au déplacement net sur une fenêtre de temps, insensible au
  tremblement du GPS ;
- la réponse reste compacte (aucune liste par point) et ne contient jamais de NaN.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.services import geo
from app.services.activity_stats import ASCENT_THRESHOLD_GPS_M, MAX_TRACK_POINTS, iter_lines, parse_timestamp

STOP_SPEED_KMH = 2.0        # en dessous : à l'arrêt
STOP_MIN_DURATION_S = 60.0  # durée minimale d'un arrêt
STOP_WINDOW_S = 30.0        # fenêtre du déplacement net
STOP_MERGE_GAP_S = 30.0     # deux arrêts proches dans le temps et l'espace n'en font qu'un
STOP_MERGE_RADIUS_M = 50.0
STOP_MIN_POINTS_NO_TIME = 3
SPEED_WINDOW_S = 10.0       # lissage de la vitesse calculée
JUMP_WINDOW = 11            # pas voisins servant de référence pour repérer un saut GPS
JUMP_FACTOR = 3.0           # saut : plus de 3 fois l'allure locale…
JUMP_MARGIN_MS = 3.0        # … plus une marge (m/s)

GRID_M = 20.0               # pas du profil rééchantillonné
MAX_GRID_CELLS = 20000
SMOOTH_M = 100.0            # lissage de l'altitude
GRADE_BASE_M = 100.0        # base de mesure d'une pente
CLIMB_MIN_GAIN_M = 10.0     # dénivelé minimal d'une montée ou d'une descente
CLIMB_MIN_GRADE = 1.5       # % ; en dessous le tronçon est « plat »
SECTION_MIN_M = 200.0       # longueur minimale d'un tronçon de pente homogène
SUMMIT_PROMINENCE_M = 30.0
TURN_BASE_M = 30.0
TURN_MIN_DEG = 60.0
TURN_SHARP_DEG = 120.0
ACCEL_THRESHOLD = 0.5       # m/s²
MAX_ITEMS = 200             # plafond de chaque liste renvoyée
PATH_POINTS = 60            # points du tracé simplifié d'un segment

SLOPE_CLASSES = (('flat', 5.0), ('gentle', 15.0), ('moderate', 30.0), ('steep', math.inf))
ZONES = (('slow', 'Lente', '#3498db'), ('medium', 'Moyenne', '#f39c12'), ('fast', 'Rapide', '#e74c3c'))

NO_SPEED = "La trace ne contient ni horodatage ni vitesse"
NO_ELEVATION = "La trace ne contient pas d'altitude exploitable"
NO_TIME = "La trace n'est pas horodatée"
NO_TRACK = "Aucune trace exploitable"


@dataclass
class Series:
    """Trace assainie : tableaux parallèles, un élément par point."""
    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray                   # sans trou ; zéros si has_elevation est faux
    t: Optional[np.ndarray]           # epoch UTC (s), croissant ; None si inexploitable
    device_speed: Optional[np.ndarray]  # m/s lus dans le fichier (NaN possibles)
    new_line: np.ndarray              # vrai au premier point de chaque trace
    has_elevation: bool
    source: str                       # 'lines' ou 'points'
    notes: List[str] = field(default_factory=list)
    # Dérivés
    steps: np.ndarray = None          # distance (m) entre points consécutifs, 0 entre deux traces
    cum: np.ndarray = None            # distance cumulée (m)
    speed: Optional[np.ndarray] = None  # m/s par point, lissée
    speed_source: Optional[str] = None  # 'time', 'device' ou None

    def __len__(self) -> int:
        return len(self.lat)


# ----------------------------------------------------------------- Assainissement

def _num(value: Any) -> float:
    """Nombre fini ou NaN ; ni booléen ni chaîne."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return math.nan
    return float(value) if math.isfinite(value) else math.nan


def _valid_position(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0  # faux pour NaN


def _r(value: Any, digits: int = 1) -> Optional[float]:
    """Arrondi sérialisable : None pour une valeur absente ou non finie."""
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _raw_from_lines(features: List[Any]) -> Dict[str, list]:
    raw = {'lat': [], 'lon': [], 'ele': [], 't': [], 'new_line': []}
    for _feature, coords, stamps in iter_lines(features):
        if not isinstance(coords, list):
            continue
        aligned = isinstance(stamps, list) and len(stamps) == len(coords)
        first = True
        for i, coord in enumerate(coords):
            if not isinstance(coord, (list, tuple)) or len(coord) < 2:
                continue
            lat, lon = _num(coord[0]), _num(coord[1])
            if not _valid_position(lat, lon):
                continue
            stamp = parse_timestamp(stamps[i]) if aligned else None
            raw['lat'].append(lat)
            raw['lon'].append(lon)
            raw['ele'].append(_num(coord[2]) if len(coord) > 2 else math.nan)
            raw['t'].append(math.nan if stamp is None else stamp)
            raw['new_line'].append(first)
            first = False
        if len(raw['lat']) > MAX_TRACK_POINTS:
            raise ValueError(f"Trace trop volumineuse (> {MAX_TRACK_POINTS} points)")
    return raw


def _raw_from_markers(points: List[Any]) -> Dict[str, list]:
    raw = {'lat': [], 'lon': [], 'ele': [], 't': [], 'speed': [], 'hidden': []}
    for point in points:
        if not isinstance(point, dict) or point.get('type', 'marker') != 'marker':
            continue
        coord = point.get('coordinates')
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        lat, lon = _num(coord[0]), _num(coord[1])
        if not _valid_position(lat, lon):
            continue
        info = point.get('parsed_info') if isinstance(point.get('parsed_info'), dict) else {}
        time_info = point.get('time_info') if isinstance(point.get('time_info'), dict) else {}
        speed = _num(info.get('speed_kmh'))
        stamp = parse_timestamp(time_info.get('timestamp'))
        ele = _num(point.get('altitude'))
        if math.isnan(ele) and len(coord) > 2:
            ele = _num(coord[2])
        raw['lat'].append(lat)
        raw['lon'].append(lon)
        raw['ele'].append(ele)
        raw['t'].append(math.nan if stamp is None else stamp)
        raw['speed'].append(speed / 3.6 if speed >= 0 else math.nan)
        raw['hidden'].append(str(point.get('visibility')) == '0')
        if len(raw['lat']) > MAX_TRACK_POINTS:
            raise ValueError(f"Trace trop volumineuse (> {MAX_TRACK_POINTS} points)")
    return raw


def _usable_time(t: np.ndarray, notes: List[str]) -> Optional[np.ndarray]:
    if len(t) < 2 or np.isnan(t).all():
        return None
    if np.isnan(t).any():
        notes.append("Horodatage incomplet : les analyses temporelles sont désactivées")
        return None
    if (np.diff(t) < 0).any():
        notes.append("Horodatages désordonnés : les analyses temporelles sont désactivées")
        return None
    return t if t[-1] > t[0] else None


def _clean_elevation(ele: np.ndarray, notes: List[str]) -> Tuple[np.ndarray, bool]:
    ele = np.where((ele > -500.0) & (ele < 9000.0), ele, np.nan)
    nonzero = ele[np.isfinite(ele) & (ele != 0.0)]
    if len(nonzero) < 2:
        return np.zeros(len(ele)), False
    # Les parsers écrivent 0 quand l'altitude manque : un 0 exact au milieu d'une trace
    # en altitude est un trou, pas un passage au niveau de la mer.
    zeros = ele == 0.0
    if zeros.any() and np.percentile(np.abs(nonzero), 5) > 30.0:
        ele = np.where(zeros, np.nan, ele)
    missing = ~np.isfinite(ele)
    if missing.any():
        notes.append(f"{int(missing.sum())} altitude(s) absente(s) ou aberrante(s) interpolée(s)")
        index = np.arange(len(ele))
        ele = np.interp(index, index[~missing], ele[~missing])
    return ele, True


def _aligned_markers(markers: Dict[str, list], lat: np.ndarray, lon: np.ndarray) -> Optional[np.ndarray]:
    """Indices des marqueurs superposés point pour point à la trace, sinon None."""
    m_lat, m_lon = np.asarray(markers['lat']), np.asarray(markers['lon'])
    hidden = np.asarray(markers['hidden'], dtype=bool)
    for candidate in (np.arange(len(m_lat)), np.flatnonzero(hidden)):
        if len(candidate) != len(lat):
            continue
        if np.abs(m_lat[candidate] - lat).max() < 1e-5 and np.abs(m_lon[candidate] - lon).max() < 1e-5:
            return candidate
    return None


def build_series(features: List[Any], points: List[Any]) -> Optional[Series]:
    """
    Trace d'analyse à partir des lignes du fichier ; à défaut (ou si seules les mesures
    ponctuelles portent une vitesse), à partir des points. None si rien d'exploitable.
    """
    notes: List[str] = []
    lines = _raw_from_lines(features or [])
    markers = _raw_from_markers(points or [])
    marker_speed = np.asarray(markers['speed'], dtype=float)
    marker_time = np.asarray(markers['t'], dtype=float)

    series = None
    if len(lines['lat']) >= 2:
        lat, lon = np.asarray(lines['lat']), np.asarray(lines['lon'])
        t = _usable_time(np.asarray(lines['t'], dtype=float), notes)
        device = None
        if t is None and len(markers['lat']):
            match = _aligned_markers(markers, lat, lon)
            if match is not None:
                if np.isfinite(marker_speed[match]).any():
                    device = marker_speed[match]
                t = _usable_time(marker_time[match], [])
            elif np.isfinite(marker_speed).sum() >= 2 or np.isfinite(marker_time).sum() >= 2:
                lines = None  # seuls les points portent vitesse ou temps : on analyse les points
        if lines is not None:
            ele, has_elevation = _clean_elevation(np.asarray(lines['ele'], dtype=float), notes)
            series = Series(lat, lon, ele, t, device, np.asarray(lines['new_line'], dtype=bool),
                            has_elevation, 'lines', notes)
    if series is None:
        if len(markers['lat']) < 2:
            return None
        keep = np.arange(len(markers['lat']))
        if lines is None:  # trace présente mais non alignée : ne garder que les mesures
            keep = np.flatnonzero(np.isfinite(marker_speed) | np.isfinite(marker_time))
        if len(keep) < 2:
            return None
        notes.append("Analyse fondée sur les points du fichier (la trace ne porte ni temps ni vitesse)"
                     if lines is None else "Analyse fondée sur les points du fichier (aucune trace)")
        ele, has_elevation = _clean_elevation(np.asarray(markers['ele'], dtype=float)[keep], notes)
        device = marker_speed[keep]
        new_line = np.zeros(len(keep), dtype=bool)
        new_line[0] = True
        series = Series(np.asarray(markers['lat'])[keep], np.asarray(markers['lon'])[keep], ele,
                        _usable_time(marker_time[keep], notes),
                        device if np.isfinite(device).any() else None, new_line, has_elevation, 'points', notes)

    _derive(series)
    return series


def _derive(series: Series) -> None:
    """Distances (sans les sauts d'une trace à la suivante) et vitesse lissée par point."""
    steps = geo.step_distances(series.lat, series.lon)
    steps[series.new_line[1:]] = 0.0
    series.steps = steps
    series.cum = np.concatenate(([0.0], np.cumsum(steps)))

    if series.t is not None:
        series.speed = _window_speed(series, SPEED_WINDOW_S, net=False, cum=_cum_without_jumps(series))
        series.speed_source = 'time'
    elif series.device_speed is not None:
        speed = series.device_speed
        index = np.arange(len(speed))
        known = np.isfinite(speed)
        speed = np.interp(index, index[known], speed[known])
        series.speed = _median_filter(speed, 5)
        series.speed_source = 'device'


def _cum_without_jumps(series: Series) -> np.ndarray:
    """
    Distance cumulée pour le calcul des vitesses, sauts GPS neutralisés : un pas bien plus
    rapide que ses voisins (aller-retour d'un point aberrant) est ramené à l'allure locale.
    Une vitesse élevée mais soutenue n'est pas touchée.
    """
    dt = np.diff(series.t)
    with np.errstate(divide='ignore', invalid='ignore'):
        step_speed = np.where(dt > 0, series.steps / dt, 0.0)
    local = _median_filter(step_speed, JUMP_WINDOW)
    jumps = step_speed > JUMP_FACTOR * local + JUMP_MARGIN_MS
    if not jumps.any():
        return series.cum
    series.notes.append(f"{int(jumps.sum())} saut(s) GPS ignoré(s) dans le calcul des vitesses")
    return np.concatenate(([0.0], np.cumsum(np.where(jumps, local * dt, series.steps))))


def _median_filter(values: np.ndarray, window: int) -> np.ndarray:
    """Médiane glissante : écarte une mesure isolée aberrante."""
    if len(values) < window:
        return values
    half = window // 2
    padded = np.pad(values, half, mode='edge')
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def _window_speed(series: Series, window_s: float, net: bool, cum: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Vitesse (m/s) en chaque point sur une fenêtre de temps centrée : distance parcourue
    (net=False) ou déplacement à vol d'oiseau (net=True) divisé par la durée.
    """
    t, n = series.t, len(series)
    lo = np.searchsorted(t, t - window_s / 2, side='left')
    hi = np.searchsorted(t, t + window_s / 2, side='right') - 1
    single = lo >= hi  # échantillonnage plus lâche que la fenêtre : voisins immédiats
    lo = np.where(single, np.maximum(np.arange(n) - 1, 0), lo)
    hi = np.where(single, np.minimum(np.arange(n) + 1, n - 1), hi)
    span = t[hi] - t[lo]
    if net:
        dist = geo.haversine(series.lat[lo], series.lon[lo], series.lat[hi], series.lon[hi])
    else:
        cum = series.cum if cum is None else cum
        dist = cum[hi] - cum[lo]
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(span > 0, dist / span, 0.0)


def _runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Plages [début, fin] (incluses) des valeurs vraies consécutives."""
    if not mask.any():
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), (np.flatnonzero(edges == -1) - 1).tolist()))


def _span(series: Series, start: int, end: int, path: bool = False) -> Dict[str, Any]:
    """Repères d'une portion de trace pour l'affichage (indices, emprise, tracé simplifié)."""
    lat, lon = series.lat[start:end + 1], series.lon[start:end + 1]
    result = {
        'start_index': int(start), 'end_index': int(end),
        'bounds': [[float(lat.min()), float(lon.min())], [float(lat.max()), float(lon.max())]],
    }
    if path:
        pick = np.unique(np.linspace(0, len(lat) - 1, min(len(lat), PATH_POINTS)).astype(int))
        result['path'] = [[float(lat[i]), float(lon[i])] for i in pick]
    return result


# ------------------------------------------------------------------------ Arrêts

def detect_stops(series: Series, speed_kmh: float = STOP_SPEED_KMH,
                 min_duration_s: float = STOP_MIN_DURATION_S) -> List[Dict[str, Any]]:
    """Arrêts : immobilité (déplacement net sous le seuil) pendant au moins `min_duration_s`."""
    threshold = max(float(speed_kmh), 0.0) / 3.6
    if series.t is None:
        if series.speed is None:
            return []
        # Sans horodatage la durée est inconnue : seule la vitesse du fichier fait foi
        still = np.isfinite(series.device_speed) & (series.device_speed <= threshold)
        spans = [(a, b) for a, b in _runs(still) if b - a + 1 >= STOP_MIN_POINTS_NO_TIME]
        return [_stop(series, a, b) for a, b in spans][:MAX_ITEMS]

    t = series.t
    dt = np.diff(t)
    still_point = _window_speed(series, STOP_WINDOW_S, net=True) < threshold
    with np.errstate(divide='ignore', invalid='ignore'):
        slow_gap = (dt >= STOP_WINDOW_S) & (series.steps < threshold * dt)  # pause d'enregistrement
    still_step = ((still_point[:-1] & still_point[1:]) | slow_gap) & ~series.new_line[1:]

    spans = [(a, b + 1) for a, b in _runs(still_step)]  # pas a..b -> points a..b+1
    merged: List[Tuple[int, int]] = []
    for a, b in spans:
        if merged:
            pa, pb = merged[-1]
            close = geo.haversine(series.lat[pb], series.lon[pb], series.lat[a], series.lon[a]) <= STOP_MERGE_RADIUS_M
            if t[a] - t[pb] <= STOP_MERGE_GAP_S and close and not series.new_line[pb + 1:a + 1].any():
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    stops = [_stop(series, a, b) for a, b in merged if t[b] - t[a] >= min_duration_s]
    return stops[:MAX_ITEMS]


def _stop(series: Series, start: int, end: int) -> Dict[str, Any]:
    lat = float(np.median(series.lat[start:end + 1]))
    lon = float(np.median(series.lon[start:end + 1]))
    duration = float(series.t[end] - series.t[start]) if series.t is not None else None
    return {
        **_span(series, start, end),
        'lat': lat, 'lng': lon, 'coordinates': [lat, lon],
        'points_count': int(end - start + 1),
        'distance_km': _r(series.cum[start] / 1000, 2),
        'duration_s': _r(duration, 0),
        'duration_minutes': _r(duration / 60, 1) if duration is not None else None,
        'start_time': _iso(series.t[start]) if series.t is not None else None,
        'end_time': _iso(series.t[end]) if series.t is not None else None,
    }


# ---------------------------------------------------------------- Profil et pentes

@dataclass
class Profile:
    """Altitude rééchantillonnée à pas de distance constant, lissée, et pentes associées."""
    grid: np.ndarray     # distances (m)
    step: float
    raw: np.ndarray      # altitude interpolée
    smooth: np.ndarray   # altitude lissée
    grade: np.ndarray    # pente (%) de chaque intervalle, taille len(grid) - 1


def build_profile(series: Series) -> Optional[Profile]:
    total = float(series.cum[-1])
    if not series.has_elevation or total < 2 * GRID_M:
        return None
    step = max(GRID_M, total / MAX_GRID_CELLS)
    grid = np.linspace(0.0, total, int(math.ceil(total / step)) + 1)
    step = float(grid[1] - grid[0])
    raw = np.interp(grid, series.cum, series.ele)
    window = max(1, int(round(SMOOTH_M / step))) | 1  # impair
    smooth = geo.moving_average(raw, window)
    half = max(1, int(round(GRADE_BASE_M / 2 / step)))
    index = np.arange(len(grid))
    lo, hi = np.maximum(index - half, 0), np.minimum(index + half, len(grid) - 1)
    point_grade = (smooth[hi] - smooth[lo]) / (grid[hi] - grid[lo]) * 100.0
    return Profile(grid, step, raw, smooth, (point_grade[:-1] + point_grade[1:]) / 2)


def _turning_points(values: np.ndarray, threshold: float) -> List[int]:
    """
    Extrema alternés séparés d'au moins `threshold` (zigzag à hystérésis), extrémités
    comprises : une ondulation plus petite que le seuil ne crée pas de point de rebroussement.
    """
    n = len(values)
    pivots, candidate, direction = [0], 0, 0
    for i in range(1, n):
        value = values[i]
        if direction == 0:
            if abs(value - values[0]) >= threshold:
                direction = 1 if value > values[0] else -1
                candidate = i
        elif (value - values[candidate]) * direction > 0:
            candidate = i
        elif (values[candidate] - value) * direction >= threshold:
            pivots.append(candidate)
            direction, candidate = -direction, i
    if direction != 0 and candidate not in (pivots[-1], n - 1):
        pivots.append(candidate)
    if pivots[-1] != n - 1:
        pivots.append(n - 1)
    return pivots


def _point_index(series: Series, distance: float) -> int:
    return int(min(np.searchsorted(series.cum, distance, side='left'), len(series) - 1))


def _leg_stats(series: Series, profile: Profile, a: int, b: int) -> Dict[str, Any]:
    """Mesures communes à une portion [a, b] du profil (indices de grille)."""
    start, end = _point_index(series, profile.grid[a]), _point_index(series, profile.grid[b])
    length = float(profile.grid[b] - profile.grid[a])
    diffs = np.diff(profile.smooth[a:b + 1])
    stats = {
        'start_km': _r(profile.grid[a] / 1000, 2), 'end_km': _r(profile.grid[b] / 1000, 2),
        'distance_m': _r(length, 0), 'distance_km': _r(length / 1000, 2),
        'elevation_change': _r(profile.smooth[b] - profile.smooth[a], 1),
        'elevation_gain': _r(diffs[diffs > 0].sum(), 1), 'elevation_loss': _r(-diffs[diffs < 0].sum(), 1),
        'start_elevation': _r(profile.smooth[a], 0), 'end_elevation': _r(profile.smooth[b], 0),
        'avg_slope': _r((profile.smooth[b] - profile.smooth[a]) / length * 100 if length > 0 else 0.0, 1),
        'duration_s': None, 'avg_speed_kmh': None,
    }
    if end > start:
        if series.t is not None:
            duration = float(series.t[end] - series.t[start])
            stats['duration_s'] = _r(duration, 0)
            if duration > 0:
                stats['avg_speed_kmh'] = _r((series.cum[end] - series.cum[start]) / duration * 3.6, 1)
        elif series.speed is not None:
            stats['avg_speed_kmh'] = _r(series.speed[start:end + 1].mean() * 3.6, 1)
    return {**stats, **_span(series, start, max(end, start), path=True)}


def segment_climbs(series: Series, profile: Optional[Profile]) -> List[Dict[str, Any]]:
    """Découpe la trace en montées, descentes et portions plates."""
    if profile is None:
        return []
    pivots = _turning_points(profile.smooth, CLIMB_MIN_GAIN_M)
    legs: List[List[Any]] = []  # [type, a, b]
    for a, b in zip(pivots[:-1], pivots[1:]):
        delta = profile.smooth[b] - profile.smooth[a]
        grade = delta / (profile.grid[b] - profile.grid[a]) * 100
        kind = 'flat'
        if abs(delta) >= CLIMB_MIN_GAIN_M and abs(grade) >= CLIMB_MIN_GRADE:
            kind = 'ascent' if delta > 0 else 'descent'
        if legs and legs[-1][0] == kind:
            legs[-1][2] = b
        else:
            legs.append([kind, a, b])
    segments = []
    for kind, a, b in legs:
        grades = profile.grade[a:b]
        segments.append({
            'type': kind, **_leg_stats(series, profile, a, b),
            'max_slope': _r(grades.max() if kind != 'descent' else grades.min(), 1),
        })
    return segments[:MAX_ITEMS]


def _slope_class(grade: float) -> str:
    return next(name for name, limit in SLOPE_CLASSES if abs(grade) <= limit)


def analyze_terrain(series: Series, profile: Optional[Profile]) -> Dict[str, Any]:
    """Répartition des pentes en distance et tronçons de pente homogène."""
    if profile is None:
        return {}
    grade, step = profile.grade, profile.step
    magnitude = np.abs(grade)
    total = step * len(grade)
    distribution, previous = {}, -1.0
    for name, limit in SLOPE_CLASSES:
        length = float(((magnitude > previous) & (magnitude <= limit)).sum() * step)
        distribution[name] = {'distance_km': _r(length / 1000, 2), 'percent': _r(length / total * 100, 1)}
        previous = limit

    # Tronçons : classe et sens de pente constants, les trop courts sont absorbés
    def key(value: float) -> Tuple[str, int]:
        name = _slope_class(value)
        return name, 0 if name == 'flat' else (1 if value > 0 else -1)

    keys = [key(g) for g in grade]
    bounds: List[List[int]] = []  # [a, b] intervalles de grille, b exclu
    for i in range(len(grade)):
        if bounds and keys[i] == keys[i - 1]:
            bounds[-1][1] = i + 1
        else:
            bounds.append([i, i + 1])
    min_cells = max(1, int(round(max(SECTION_MIN_M, total / MAX_ITEMS) / step)))
    merged: List[List[int]] = []
    for a, b in bounds:
        if merged and (b - a < min_cells or merged[-1][1] - merged[-1][0] < min_cells):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    sections: List[Dict[str, Any]] = []
    for a, b in merged:
        name, sign = key(float(grade[a:b].mean()))
        if sections and (sections[-1]['type'], sections[-1]['_sign']) == (name, sign):
            a = sections.pop()['_a']
        sections.append({'type': name, '_sign': sign, '_a': a, '_b': b})
    slope_segments = []
    for section in sections:
        a, b, sign = section['_a'], section['_b'], section['_sign']
        stats = _leg_stats(series, profile, a, b)
        stats.pop('path')
        slope_segments.append({'type': section['type'], 'direction': {1: 'up', -1: 'down', 0: 'flat'}[sign], **stats})

    return {
        'avg_slope': _r(grade.mean(), 2), 'max_slope': _r(grade.max(), 2), 'min_slope': _r(grade.min(), 2),
        'slope_segments': slope_segments[:MAX_ITEMS],
        'terrain_analysis': {
            'total_distance_km': _r(total / 1000, 2),
            'terrain_distribution': distribution,
            'dominant_terrain': max(distribution, key=lambda name: distribution[name]['percent']),
        },
    }


# ---------------------------------------------------------------------- Vitesses

def _moving_steps(series: Series, stops: List[Dict[str, Any]]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pas en mouvement : vitesse (m/s), poids (secondes, ou 1 sans horodatage), masque."""
    speed = (series.speed[:-1] + series.speed[1:]) / 2
    valid = ~series.new_line[1:]
    if series.t is not None:
        weight = np.diff(series.t)
        valid &= weight > 0
    else:
        weight = np.ones(len(speed))
    for stop in stops:
        valid[stop['start_index']:stop['end_index']] = False
    valid &= speed >= STOP_SPEED_KMH / 3.6
    return speed, weight, valid


def analyze_speed_zones(series: Series, stops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Temps et distance passés dans trois zones (tiers de la vitesse de pointe robuste),
    arrêts exclus.
    """
    if series.speed is None or len(series) < 2:
        return {}
    speed, weight, valid = _moving_steps(series, stops)
    if not valid.any():
        return {'zones': [], 'speed_distribution': {}, 'total_points': int(len(series))}
    moving = speed[valid]
    top = float(np.percentile(moving, 98))  # un saut GPS isolé ne fixe pas l'échelle
    limits = [0.0, top / 3, 2 * top / 3, math.inf]
    total_weight = float(weight[valid].sum())
    zones, counts = [], {}
    for index, (name, label, color) in enumerate(ZONES):
        inside = valid & (speed > limits[index]) & (speed <= limits[index + 1])
        zone_weight = float(weight[inside].sum())
        counts[name] = int(inside.sum())
        zones.append({
            'type': name, 'label': label, 'color': color,
            'min_speed': _r(limits[index] * 3.6, 1),
            'max_speed': _r((limits[index + 1] if index < 2 else top) * 3.6, 1),
            'avg_speed': _r(np.average(speed[inside], weights=weight[inside]) * 3.6, 1) if zone_weight else None,
            'time_s': _r(zone_weight, 0) if series.t is not None else None,
            'distance_km': _r(series.steps[inside].sum() / 1000, 2),
            'percent': _r(zone_weight / total_weight * 100, 1),
        })
    return {
        'zones': zones,
        'basis': 'time' if series.t is not None else 'points',
        'speed_distribution': {
            'slow_count': counts['slow'], 'medium_count': counts['medium'], 'fast_count': counts['fast'],
            'slow_threshold': _r(limits[1] * 3.6, 1), 'medium_threshold': _r(limits[2] * 3.6, 1),
        },
        'total_points': int(len(series)),
    }


def acceleration_zones(series: Series) -> List[Dict[str, Any]]:
    """Phases d'accélération ou de freinage soutenues (au moins deux pas consécutifs)."""
    if series.t is None or len(series) < 3:
        return []
    dt = np.diff(series.t)
    with np.errstate(divide='ignore', invalid='ignore'):
        accel = np.where((dt > 0) & ~series.new_line[1:], np.diff(series.speed) / dt, 0.0)
    zones = []
    for sign, kind in ((1, 'acceleration'), (-1, 'deceleration')):
        for a, b in _runs(accel * sign > ACCEL_THRESHOLD):
            if b == a:
                continue
            values = accel[a:b + 1]
            zones.append({
                'type': kind, 'start_index': int(a), 'end_index': int(b + 1),
                'coordinates': [float(series.lat[a]), float(series.lon[a])],
                'avg_acceleration': _r(values.mean(), 2), 'max_acceleration': _r(np.abs(values).max(), 2),
                'duration_s': _r(series.t[b + 1] - series.t[a], 0),
                'speed_change_kmh': _r((series.speed[b + 1] - series.speed[a]) * 3.6, 1),
            })
    zones.sort(key=lambda zone: -zone['max_acceleration'])
    return sorted(zones[:MAX_ITEMS], key=lambda zone: zone['start_index'])


# ----------------------------------------------------------- Points remarquables

def _poi(series: Series, kind: str, distance: float, description: str, **extra: Any) -> Dict[str, Any]:
    index = _point_index(series, distance)
    return {'type': kind, 'index': index, 'distance_km': _r(distance / 1000, 2),
            'coordinates': [float(series.lat[index]), float(series.lon[index])],
            'description': description, **extra}


def _turns(series: Series, stops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Virages serrés : changement de cap mesuré sur une base de distance fixe."""
    total = float(series.cum[-1])
    step = max(GRID_M / 2, total / MAX_GRID_CELLS)
    half = max(1, int(round(TURN_BASE_M / step)))
    if total < 4 * half * step:
        return []
    grid = np.linspace(0.0, total, int(math.ceil(total / step)) + 1)
    lat, lon = np.interp(grid, series.cum, series.lat), np.interp(grid, series.cum, series.lon)
    xy = geo.to_local_xy(lat, lon, float(lat.mean()), float(lon.mean()))
    centre = np.arange(half, len(grid) - half)
    before, after = xy[centre] - xy[centre - half], xy[centre + half] - xy[centre]
    norm = np.hypot(*before.T) * np.hypot(*after.T)
    with np.errstate(divide='ignore', invalid='ignore'):
        cosine = np.where(norm > 0, (before * after).sum(axis=1) / norm, 1.0)
    angle = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))

    # À l'arrêt le GPS tremble, et entre deux traces la position saute : pas des virages
    position = grid[centre]
    margin = half * step
    excluded = [(series.cum[s['start_index']], series.cum[s['end_index']]) for s in stops]
    excluded += [(series.cum[i], series.cum[i]) for i in np.flatnonzero(series.new_line)[1:]]
    for lo, hi in excluded:
        angle[(position >= lo - margin) & (position <= hi + margin)] = 0.0

    turns = []
    order = np.argsort(-angle)
    taken = np.zeros(len(angle), dtype=bool)
    for i in order:
        if angle[i] < TURN_MIN_DEG or len(turns) >= 30:
            break
        if taken[i]:
            continue
        taken[max(0, i - 2 * half):i + 2 * half + 1] = True  # un seul point par virage
        value = float(angle[i])
        sharp = value >= TURN_SHARP_DEG
        turns.append(_poi(series, 'turn', float(position[i]),
                          f"{'Épingle' if sharp else 'Virage serré'} ({round(value)}°)",
                          angle_degrees=_r(value, 1), severity='sharp' if sharp else 'moderate'))
    return turns


def points_of_interest(series: Series, profile: Optional[Profile], stops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Point haut, point bas, sommets et creux marqués, virages serrés ; triés par distance."""
    pois: List[Dict[str, Any]] = []
    if profile is not None:
        smooth = profile.smooth
        top, bottom = int(smooth.argmax()), int(smooth.argmin())
        pois.append(_poi(series, 'highest', float(profile.grid[top]),
                         f"Point haut ({round(float(smooth[top]))} m)", elevation=_r(smooth[top], 0)))
        pois.append(_poi(series, 'lowest', float(profile.grid[bottom]),
                         f"Point bas ({round(float(smooth[bottom]))} m)", elevation=_r(smooth[bottom], 0)))
        for i in _turning_points(smooth, SUMMIT_PROMINENCE_M)[1:-1]:
            if i in (top, bottom):
                continue
            summit = smooth[i] >= smooth[max(i - 1, 0)] and smooth[i] >= smooth[min(i + 1, len(smooth) - 1)]
            label = 'Sommet' if summit else 'Creux'
            pois.append(_poi(series, 'summit' if summit else 'valley', float(profile.grid[i]),
                             f"{label} ({round(float(smooth[i]))} m)", elevation=_r(smooth[i], 0)))
    pois.extend(_turns(series, stops))
    pois.sort(key=lambda poi: poi['distance_km'])
    return pois[:MAX_ITEMS]


# ----------------------------------------------------------------------- Synthèse

def _summary(series: Series, profile: Optional[Profile], stops: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        'points': len(series), 'source': series.source,
        'has_time': series.t is not None, 'has_elevation': series.has_elevation,
        'speed_source': series.speed_source,
        'distance_km': _r(series.cum[-1] / 1000, 2),
        'duration_s': None, 'moving_s': None, 'stopped_s': None,
        'avg_moving_speed_kmh': None, 'max_speed_kmh': None,
        'ascent_m': None, 'descent_m': None,
        'notes': series.notes, 'unavailable': {},
    }
    if profile is not None:
        ascent, descent = geo.ascent_descent(profile.raw, ASCENT_THRESHOLD_GPS_M)
        summary['ascent_m'], summary['descent_m'] = _r(ascent, 0), _r(descent, 0)
    if series.t is not None:
        dt = np.where(series.new_line[1:], 0.0, np.diff(series.t))
        duration = float(dt.sum())
        stopped = float(sum(stop['duration_s'] or 0 for stop in stops))
        moving = max(duration - stopped, 0.0)
        summary.update({'duration_s': _r(duration, 0), 'stopped_s': _r(stopped, 0), 'moving_s': _r(moving, 0)})
        if moving > 0:
            stopped_m = sum(float(series.cum[s['end_index']] - series.cum[s['start_index']]) for s in stops)
            summary['avg_moving_speed_kmh'] = _r((float(series.cum[-1]) - stopped_m) / moving * 3.6, 1)
    if series.speed is not None and len(series) >= 2:
        speed, weight, valid = _moving_steps(series, stops)
        if valid.any():
            summary['max_speed_kmh'] = _r(np.percentile(speed[valid], 99) * 3.6, 1)
            if summary['avg_moving_speed_kmh'] is None:
                summary['avg_moving_speed_kmh'] = _r(np.average(speed[valid], weights=weight[valid]) * 3.6, 1)

    unavailable = summary['unavailable']
    if series.speed is None:
        unavailable['stops'] = unavailable['speed_zones'] = NO_SPEED
    if series.t is None:
        unavailable['acceleration_zones'] = NO_TIME
    if profile is None:
        unavailable['segments'] = unavailable['terrain'] = NO_ELEVATION
    return summary


def empty_analysis(reason: str = NO_TRACK) -> Dict[str, Any]:
    sections = ('stops', 'segments', 'speed_zones', 'acceleration_zones', 'terrain', 'points_of_interest')
    return {
        'stops': [], 'segments': [], 'speed_zones': {}, 'acceleration_zones': [], 'terrain': {},
        'points_of_interest': [],
        'summary': {'points': 0, 'source': None, 'has_time': False, 'has_elevation': False,
                    'speed_source': None, 'distance_km': 0.0, 'notes': [reason],
                    'unavailable': {name: reason for name in sections}},
    }


def analyze(features: List[Any], points: List[Any], stop_speed_kmh: float = STOP_SPEED_KMH,
            stop_min_duration_s: float = STOP_MIN_DURATION_S) -> Dict[str, Any]:
    """Analyse avancée complète. Lève ValueError si la trace est trop volumineuse."""
    series = build_series(features, points)
    if series is None or float(series.cum[-1]) <= 0:
        return empty_analysis()
    profile = build_profile(series)
    stops = detect_stops(series, stop_speed_kmh, stop_min_duration_s)
    return {
        'stops': stops,
        'segments': segment_climbs(series, profile),
        'speed_zones': analyze_speed_zones(series, stops),
        'acceleration_zones': acceleration_zones(series),
        'terrain': analyze_terrain(series, profile),
        'points_of_interest': points_of_interest(series, profile, stops),
        'summary': _summary(series, profile, stops),
    }
