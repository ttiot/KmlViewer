"""
Analyse d'une trace pour le visualiseur : arrêts, montées et descentes, zones de vitesse,
accélérations, terrain et points remarquables, plus l'analyse de base (statistiques et
courbes). Fonctions pures (numpy).

Les mesures elles-mêmes (trace assainie, distances, arrêts, profil d'altitude, synthèse)
sont dans app.services.track_series, partagées avec la bibliothèque. Ce module les met en
forme :
- rien n'est inventé : sans horodatage ni vitesse, une analyse est déclarée indisponible
  (voir `summary.unavailable`) au lieu d'être estimée ;
- les listes ne sont plafonnées qu'à la sérialisation : les synthèses portent sur tout ;
- la réponse reste compacte (aucune liste par point) et ne contient jamais de NaN.
"""

import math
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from app.services import geo, track_series
from app.services.track_series import (  # noqa: F401 - réexportés (routes, tests)
    GRID_M, MAX_GRID_CELLS, MAX_TRACK_POINTS, STOP_MIN_DURATION_S, STOP_SPEED_KMH, Profile, Series,
)

# À incrémenter dès qu'un changement (seuil, algorithme, format) modifie le résultat : les
# analyses enregistrées avec une autre version sont ignorées et recalculées à la demande.
ANALYSIS_VERSION = 2

CLIMB_MIN_GAIN_M = 10.0     # dénivelé minimal d'une montée ou d'une descente
# Pente (%) à partir de laquelle un tronçon monte ou descend. Deux notions distinctes s'en
# servent : un segment est une montée dès ce seuil ; un tronçon de terrain de classe « plat »
# (jusqu'à 5 %) est orienté « up » ou « down » dès ce seuil (faux plat), « flat » en dessous.
CLIMB_MIN_GRADE = 1.5
SECTION_MIN_M = 200.0       # longueur minimale d'un tronçon de pente homogène
SUMMIT_PROMINENCE_M = 30.0
TURN_BASE_M = 30.0
TURN_MIN_DEG = 60.0
TURN_SHARP_DEG = 120.0
ACCEL_THRESHOLD = 0.5       # m/s²
MAX_ITEMS = 200             # plafond de chaque liste renvoyée
PATH_POINTS = 60            # points du tracé simplifié d'un segment
CHART_POINTS = 1500         # points des courbes d'altitude et de vitesse

SLOPE_CLASSES = (('flat', 5.0), ('gentle', 15.0), ('moderate', 30.0), ('steep', math.inf))
ZONES = (('slow', 'Lente', '#3498db'), ('medium', 'Moyenne', '#f39c12'), ('fast', 'Rapide', '#e74c3c'))
SECTIONS = ('stops', 'segments', 'speed_zones', 'acceleration_zones', 'terrain', 'points_of_interest', 'summary')

NO_SPEED = "La trace ne contient ni horodatage ni vitesse"
NO_ELEVATION = "La trace ne contient pas d'altitude exploitable"
NO_TIME = "La trace n'est pas horodatée"
NO_TRACK = "Aucune trace exploitable"

Span = Tuple[int, int]  # plage de points [début, fin] (incluses)


def _r(value: Any, digits: int = 1) -> Optional[float]:
    """Arrondi sérialisable : None pour une valeur absente ou non finie."""
    if value is None:
        return None
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _kmh(value: Optional[float]) -> Optional[float]:
    return None if value is None else _r(value * 3.6, 1)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _span(series: Series, start: int, end: int, path: bool = False) -> Dict[str, Any]:
    """
    Repères d'une portion de trace pour l'affichage (indices, emprise, tracé simplifié).
    Longitudes continues : une portion à cheval sur l'antiméridien garde une emprise étroite
    (au-delà de ±180) au lieu de couvrir toute la planète.
    """
    lat, lon = series.lat[start:end + 1], series.lon_unwrapped[start:end + 1]
    result = {
        'start_index': int(start), 'end_index': int(end),
        'bounds': [[float(lat.min()), float(lon.min())], [float(lat.max()), float(lon.max())]],
    }
    if path:
        pick = np.unique(np.linspace(0, len(lat) - 1, min(len(lat), PATH_POINTS)).astype(int))
        result['path'] = [[float(lat[i]), float(lon[i])] for i in pick]
    return result


# ------------------------------------------------------------------------ Arrêts

def _stop(series: Series, start: int, end: int) -> Dict[str, Any]:
    """Arrêt mis en forme ; heures lues dans le fichier, durée mesurée sur l'horloge de calcul."""
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
        'start_time': _iso(series.epoch[start]) if series.epoch is not None else None,
        'end_time': _iso(series.epoch[end]) if series.epoch is not None else None,
    }


# ----------------------------------------------------------- Montées et terrain

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


def _leg_stats(series: Series, profile: Profile, a: int, b: int) -> Dict[str, Any]:
    """Mesures communes à une portion [a, b] du profil (indices de grille)."""
    start, end = profile.point_index(series, profile.grid[a]), profile.point_index(series, profile.grid[b])
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


def segment_climbs(series: Series, profiles: List[Profile]) -> List[Dict[str, Any]]:
    """Découpe chaque trace en montées, descentes et portions sans montée marquée (« flat »)."""
    segments = []
    for profile in profiles:
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
        for kind, a, b in legs:
            grades = profile.grade[a:b]
            segments.append({
                'type': kind, **_leg_stats(series, profile, a, b),
                'max_slope': _r(grades.max() if kind != 'descent' else grades.min(), 1),
            })
    return segments


def _slope_class(grade: float) -> str:
    return next(name for name, limit in SLOPE_CLASSES if abs(grade) <= limit)


def _slope_key(grade: float) -> Tuple[str, int]:
    """Classe et sens de pente d'un intervalle ; le « plat » n'a pas de sens (bruit autour de zéro)."""
    name = _slope_class(grade)
    return name, 0 if name == 'flat' else (1 if grade > 0 else -1)


def _slope_sections(profile: Profile, min_length_m: float) -> List[Tuple[str, int, int, int]]:
    """Tronçons (classe, sens, a, b) de pente homogène d'un profil ; les trop courts sont absorbés."""
    grade = profile.grade
    keys = [_slope_key(g) for g in grade]
    bounds: List[List[int]] = []  # [a, b] intervalles de grille, b exclu
    for i in range(len(grade)):
        if bounds and keys[i] == keys[i - 1]:
            bounds[-1][1] = i + 1
        else:
            bounds.append([i, i + 1])
    min_cells = max(1, int(round(min_length_m / profile.step)))
    merged: List[List[int]] = []
    for a, b in bounds:
        if merged and (b - a < min_cells or merged[-1][1] - merged[-1][0] < min_cells):
            merged[-1][1] = b
        else:
            merged.append([a, b])
    sections: List[Tuple[str, int, int, int]] = []
    for a, b in merged:
        name, sign = _slope_key(float(grade[a:b].mean()))
        if sections and sections[-1][:2] == (name, sign):
            a = sections.pop()[2]
        sections.append((name, sign, a, b))
    return sections


def _direction(name: str, sign: int, avg_slope: Optional[float]) -> str:
    """Sens affiché d'un tronçon. Un tronçon « plat » qui monte ou descend d'au moins CLIMB_MIN_GRADE est un faux plat."""
    if name == 'flat' and avg_slope is not None and abs(avg_slope) >= CLIMB_MIN_GRADE:
        sign = 1 if avg_slope > 0 else -1
    return {1: 'up', -1: 'down', 0: 'flat'}[sign]


def analyze_terrain(series: Series, profiles: List[Profile]) -> Dict[str, Any]:
    """Répartition des pentes en distance et tronçons de pente homogène, toutes traces confondues."""
    if not profiles:
        return {}
    grade = np.concatenate([profile.grade for profile in profiles])
    length = np.concatenate([np.full(len(profile.grade), profile.step) for profile in profiles])
    magnitude = np.abs(grade)
    total = float(length.sum())
    distribution, previous = {}, -1.0
    for name, limit in SLOPE_CLASSES:
        inside = float(length[(magnitude > previous) & (magnitude <= limit)].sum())
        distribution[name] = {'distance_km': _r(inside / 1000, 2), 'percent': _r(inside / total * 100, 1)}
        previous = limit

    slope_segments = []
    min_length = max(SECTION_MIN_M, total / MAX_ITEMS)
    for profile in profiles:
        for name, sign, a, b in _slope_sections(profile, min_length):
            stats = _leg_stats(series, profile, a, b)
            stats.pop('path')
            slope_segments.append({'type': name, 'direction': _direction(name, sign, stats['avg_slope']), **stats})

    return {
        'avg_slope': _r(np.average(grade, weights=length), 2), 'max_slope': _r(grade.max(), 2),
        'min_slope': _r(grade.min(), 2),
        'slope_segments': slope_segments,
        'terrain_analysis': {
            'total_distance_km': _r(total / 1000, 2),
            'terrain_distribution': distribution,
            'dominant_terrain': max(distribution, key=lambda name: distribution[name]['percent']),
        },
    }


# ---------------------------------------------------------------------- Vitesses

def analyze_speed_zones(series: Series, stops: List[Span]) -> Dict[str, Any]:
    """
    Temps et distance passés dans trois zones (tiers de la vitesse de pointe robuste),
    arrêts exclus.
    """
    if series.speed is None or len(series) < 2:
        return {}
    speed, weight, valid = track_series.moving_steps(series, stops)
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
    """Phases d'accélération ou de freinage soutenues (au moins deux pas consécutifs), les plus fortes d'abord."""
    if series.t is None or len(series) < 3:
        return []
    dt = np.diff(series.t)
    with np.errstate(divide='ignore', invalid='ignore'):
        accel = np.where((dt > 0) & ~series.new_line[1:], np.diff(series.speed) / dt, 0.0)
    zones = []
    for sign, kind in ((1, 'acceleration'), (-1, 'deceleration')):
        for a, b in geo.true_runs(accel * sign > ACCEL_THRESHOLD):
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
    return zones


# ----------------------------------------------------------- Points remarquables

def _poi(series: Series, kind: str, index: int, distance: float, description: str, **extra: Any) -> Dict[str, Any]:
    return {'type': kind, 'index': int(index), 'distance_km': _r(distance / 1000, 2),
            'coordinates': [float(series.lat[index]), float(series.lon[index])],
            'description': description, **extra}


def _turns(series: Series, stops: List[Span]) -> List[Dict[str, Any]]:
    """Virages serrés : changement de cap mesuré sur une base de distance fixe."""
    total = float(series.cum[-1])
    step = max(GRID_M / 2, total / MAX_GRID_CELLS)
    half = max(1, int(round(TURN_BASE_M / step)))
    if total < 4 * half * step:
        return []
    grid = np.linspace(0.0, total, int(math.ceil(total / step)) + 1)
    # Longitudes continues : sans cela le passage de l'antiméridien ressemble à un demi-tour
    lat, lon = np.interp(grid, series.cum, series.lat), np.interp(grid, series.cum, series.lon_unwrapped)
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
    excluded = [(series.cum[start], series.cum[end]) for start, end in stops]
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
        index = int(min(np.searchsorted(series.cum, position[i], side='left'), len(series) - 1))
        turns.append(_poi(series, 'turn', index, float(position[i]),
                          f"{'Épingle' if sharp else 'Virage serré'} ({round(value)}°)",
                          angle_degrees=_r(value, 1), severity='sharp' if sharp else 'moderate'))
    return turns


def points_of_interest(series: Series, profiles: List[Profile], stops: List[Span]) -> List[Dict[str, Any]]:
    """Point haut, point bas, sommets et creux marqués, virages serrés ; triés par distance."""
    pois: List[Dict[str, Any]] = []

    def elevation_poi(profile: Profile, i: int, kind: str, label: str) -> Dict[str, Any]:
        distance, value = float(profile.grid[i]), float(profile.smooth[i])
        return _poi(series, kind, profile.point_index(series, distance), distance,
                    f"{label} ({round(value)} m)", elevation=_r(value, 0))

    if profiles:
        highest = max(profiles, key=lambda profile: float(profile.smooth.max()))
        lowest = min(profiles, key=lambda profile: float(profile.smooth.min()))
        extremes = {(id(highest), int(highest.smooth.argmax())), (id(lowest), int(lowest.smooth.argmin()))}
        pois.append(elevation_poi(highest, int(highest.smooth.argmax()), 'highest', 'Point haut'))
        pois.append(elevation_poi(lowest, int(lowest.smooth.argmin()), 'lowest', 'Point bas'))
        for profile in profiles:
            smooth = profile.smooth
            for i in _turning_points(smooth, SUMMIT_PROMINENCE_M)[1:-1]:
                if (id(profile), i) in extremes:
                    continue
                summit = smooth[i] >= smooth[max(i - 1, 0)] and smooth[i] >= smooth[min(i + 1, len(smooth) - 1)]
                pois.append(elevation_poi(profile, i, 'summit' if summit else 'valley', 'Sommet' if summit else 'Creux'))
    pois.extend(_turns(series, stops))
    pois.sort(key=lambda poi: poi['distance_km'])
    return pois


# ----------------------------------------------------------------------- Synthèse

def _summary(series: Series, profiles: List[Profile], stops: List[Span]) -> Dict[str, Any]:
    """Synthèse sur toute la trace (jamais sur des listes plafonnées)."""
    measures = track_series.measure(series, profiles, stops)
    summary: Dict[str, Any] = {
        'points': len(series), 'source': series.source,
        'has_time': series.t is not None, 'has_elevation': series.has_elevation,
        'elevation_source': series.elevation_source if series.has_elevation else None,
        'speed_source': series.speed_source,
        'distance_km': _r(measures.distance_m / 1000, 2),
        'duration_s': _r(measures.duration_s, 0), 'moving_s': _r(measures.moving_s, 0),
        'stopped_s': _r(measures.stopped_s, 0), 'stops_count': len(stops),
        'avg_moving_speed_kmh': _kmh(measures.avg_moving_speed_ms), 'max_speed_kmh': _kmh(measures.max_speed_ms),
        'ascent_m': _r(measures.ascent_m, 0), 'descent_m': _r(measures.descent_m, 0),
        'notes': list(series.notes), 'unavailable': {},
    }
    unavailable = summary['unavailable']
    if series.speed is None:
        unavailable['stops'] = unavailable['speed_zones'] = NO_SPEED
    if series.t is None:
        unavailable['acceleration_zones'] = NO_TIME
    if not profiles:
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


def _prepare(features: List[Any], points: List[Any], stop_speed_kmh: float, stop_min_duration_s: float,
             elevation: Optional[Sequence[float]]) -> Optional[Tuple[Series, List[Profile], List[Span]]]:
    """Trace assainie, profils et arrêts, communs à l'analyse de base et à l'analyse avancée."""
    series = track_series.build_series(features, points, MAX_TRACK_POINTS, elevation)
    if series is None or float(series.cum[-1]) <= 0:
        return None
    return series, track_series.build_profiles(series), track_series.detect_stops(series, stop_speed_kmh, stop_min_duration_s)


def _capped(items: list, label: str, notes: List[str]) -> list:
    """Plafonne une liste à la sérialisation, en le disant : le reste de l'analyse porte sur tout."""
    if len(items) > MAX_ITEMS:
        notes.append(f"{len(items)} {label} : seuls les {MAX_ITEMS} premiers sont listés")
    return items[:MAX_ITEMS]


def analyze(features: List[Any], points: List[Any], stop_speed_kmh: float = STOP_SPEED_KMH,
            stop_min_duration_s: float = STOP_MIN_DURATION_S, only: Optional[Iterable[str]] = None,
            elevation: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """
    Analyse avancée. `only` : sections à calculer (voir SECTIONS), toutes par défaut ; seules
    celles-là figurent dans le résultat. `elevation` : altitude MNT alignée sur les points des
    traces, qui remplace alors celle du fichier. Lève ValueError si la trace est trop
    volumineuse ou si une section demandée n'existe pas.
    """
    wanted = SECTIONS if only is None else tuple(only)
    unknown = set(wanted) - set(SECTIONS)
    if unknown:
        raise ValueError(f"Section d'analyse inconnue : {', '.join(sorted(unknown))}")
    prepared = _prepare(features, points, stop_speed_kmh, stop_min_duration_s, elevation)
    if prepared is None:
        empty = empty_analysis()
        return {name: empty[name] for name in SECTIONS if name in wanted}
    series, profiles, stops = prepared
    notes: List[str] = []  # plafonnements, ajoutés à la synthèse
    builders: Dict[str, Callable[[], Any]] = {
        'stops': lambda: [_stop(series, a, b) for a, b in _capped(stops, 'arrêts', notes)],
        'segments': lambda: _capped(segment_climbs(series, profiles), 'segments', notes),
        'speed_zones': lambda: analyze_speed_zones(series, stops),
        'acceleration_zones': lambda: sorted(acceleration_zones(series)[:MAX_ITEMS], key=lambda zone: zone['start_index']),
        'terrain': lambda: _terrain(series, profiles, notes),
        'points_of_interest': lambda: _capped(points_of_interest(series, profiles, stops), 'points remarquables', notes),
    }
    analysis = {name: builders[name]() for name in SECTIONS if name in wanted and name != 'summary'}
    if 'summary' in wanted:
        analysis['summary'] = _summary(series, profiles, stops)
        analysis['summary']['notes'] += notes
    return analysis


def _terrain(series: Series, profiles: List[Profile], notes: List[str]) -> Dict[str, Any]:
    terrain = analyze_terrain(series, profiles)
    if terrain:
        terrain['slope_segments'] = _capped(terrain['slope_segments'], 'tronçons de pente', notes)
    return terrain


# ------------------------------------------------------------- Analyse de base

def _decimate(count: int, limit: int = CHART_POINTS) -> np.ndarray:
    return np.unique(np.linspace(0, count - 1, min(count, limit)).astype(int))


def basic(features: List[Any], points: Optional[List[Any]] = None,
          elevation: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """
    Statistiques et courbes de l'onglet principal, sur les mêmes mesures que l'analyse
    avancée : durée et vitesses réelles (jamais estimées), dénivelé à hystérésis sur un
    profil lissé. Une section sans donnée exploitable reste vide.
    """
    features = features or []
    if points is None:
        points = [f for f in features if isinstance(f, dict) and f.get('type') == 'marker']
    lines = sum(1 for _ in track_series.iter_lines(features))
    analysis: Dict[str, Any] = {
        'basic_stats': {'total_points': 0, 'total_traces': lines, 'has_elevation_data': False,
                        'has_speed_data': False, 'has_time_data': False},
        'distance': {'total_distance_m': 0.0, 'total_distance_km': 0.0},
        'elevation': {}, 'speed': {}, 'duration': {}, 'notes': [],
    }
    prepared = _prepare(features, points, STOP_SPEED_KMH, STOP_MIN_DURATION_S, elevation)
    if prepared is None:
        return analysis
    series, profiles, stops = prepared
    summary = _summary(series, profiles, stops)
    total = float(series.cum[-1])
    analysis['notes'] = summary['notes']
    analysis['basic_stats'].update({
        'total_points': len(series), 'has_elevation_data': series.has_elevation,
        'has_speed_data': series.speed is not None, 'has_time_data': series.t is not None,
    })
    analysis['distance'] = {'total_distance_m': _r(total, 1), 'total_distance_km': _r(total / 1000, 2)}

    if series.has_elevation:
        grid = np.concatenate([profile.grid for profile in profiles]) if profiles else np.zeros(0)
        raw = np.concatenate([profile.raw for profile in profiles]) if profiles else np.zeros(0)
        analysis['elevation'] = {
            'source': series.elevation_source,
            'total_ascent': summary['ascent_m'] or 0.0, 'total_descent': summary['descent_m'] or 0.0,
            'min_elevation': _r(series.ele.min(), 1), 'max_elevation': _r(series.ele.max(), 1),
            'elevation_gain': _r(series.ele.max() - series.ele.min(), 1),
            'elevation_profile': [{'distance': _r(grid[i], 1), 'elevation': _r(raw[i], 1), 'index': int(i)}
                                  for i in (_decimate(len(grid)) if len(grid) else [])],
        }

    if series.speed is not None:
        speed_kmh = series.speed * 3.6
        average, top = summary['avg_moving_speed_kmh'], summary['max_speed_kmh']
        lowest = _r(speed_kmh.min(), 1)
        analysis['speed'] = {
            'source': series.speed_source,
            'avg_speed_kmh': average, 'max_speed_kmh': top, 'min_speed_kmh': lowest,
            'avg_speed_kts': _r(average / 1.852, 1) if average is not None else None,
            'max_speed_kts': _r(top / 1.852, 1) if top is not None else None,
            'min_speed_kts': _r(lowest / 1.852, 1) if lowest is not None else None,
            'speed_profile': [
                {'index': int(i), 'distance': _r(series.cum[i], 1), 'speed_kmh': _r(speed_kmh[i], 1),
                 'speed_kts': _r(speed_kmh[i] / 1.852, 1),
                 'coordinates': [float(series.lat[i]), float(series.lon[i])]}
                for i in _decimate(len(series))],
        }

    if series.t is not None:
        analysis['duration'] = {
            'total_s': summary['duration_s'], 'moving_s': summary['moving_s'], 'stopped_s': summary['stopped_s'],
            'stops_count': len(stops),
            'start_time': _iso(series.epoch.min()), 'end_time': _iso(series.epoch.max()),
        }
    return analysis
