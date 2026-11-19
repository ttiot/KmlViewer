"""
Trace assainie et mesures de base : la source unique des grandeurs d'une trace (distance,
durées, arrêts, vitesses, dénivelé), partagée par le résumé d'activité de la bibliothèque
(app.services.activity_stats) et par l'analyse du visualiseur (app.services.advanced_analysis).
Fonctions pures (numpy), sans Flask ni base.

Principes de fiabilité :
- rien n'est inventé : sans horodatage ni vitesse, une mesure reste absente ;
- les entrées sont assainies (coordonnées invalides, altitudes absentes ou aberrantes) ;
- chaque trace d'un fichier est mesurée séparément : ni distance, ni durée, ni dénivelé
  entre la fin d'une trace et le début de la suivante ;
- l'altitude est rééchantillonnée à pas de distance constant puis lissée : le bruit du
  capteur à l'arrêt ou point à point ne fabrique ni pente ni dénivelé ;
- un arrêt se mesure au déplacement net sur plusieurs fenêtres de temps, insensible au
  tremblement du GPS.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.services import geo

logger = logging.getLogger(__name__)

MAX_TRACK_POINTS = 500_000

# Seuils d'hystérésis du D+ : l'altitude GPS est bien plus bruitée qu'un MNT
ASCENT_THRESHOLD_GPS_M = 4.0
ASCENT_THRESHOLD_DEM_M = 2.0

# Altitudes plausibles : de la mer Morte au plafond d'un planeur ou d'un avion de ligne
ELEVATION_MIN_M = -500.0
ELEVATION_MAX_M = 20000.0

STOP_SPEED_KMH = 2.0        # en dessous : à l'arrêt
STOP_MIN_DURATION_S = 60.0  # durée minimale d'un arrêt
STOP_WINDOW_S = 30.0        # fenêtre principale du déplacement net
# Fenêtres plus courtes : une boucle (orbite, lacet) ramène au point de départ sur une
# fenêtre égale à sa période, pas sur ses fractions
STOP_SUB_WINDOWS_S = (15.0, 10.0)
STOP_MERGE_GAP_S = 30.0     # deux arrêts proches dans le temps et l'espace n'en font qu'un
STOP_MERGE_RADIUS_M = 50.0
STOP_MIN_POINTS_NO_TIME = 3
# Progression lente mais réelle : entre le début et la fin d'un arrêt, la position ne doit
# pas avoir dérivé plus vite que cette fraction du seuil de vitesse (ni de moins de 10 m)
STOP_DRIFT_RATIO = 0.5
STOP_DRIFT_MIN_M = 10.0
SPEED_WINDOW_S = 10.0       # lissage de la vitesse calculée
# Écart minimal de l'horloge de calcul entre deux traces : aucune fenêtre ne les chevauche
CLOCK_GAP_S = 30.0

GRID_M = 20.0               # pas du profil rééchantillonné
MAX_GRID_CELLS = 20000
SMOOTH_M = 100.0            # lissage de l'altitude
GRADE_BASE_M = 100.0        # base de mesure d'une pente


# ------------------------------------------------------------- Lecture des entrées

def parse_timestamp(value: Any) -> Optional[float]:
    """ISO 8601 -> epoch UTC. None si absent ou illisible."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iter_lines(features: List[Dict[str, Any]]):
    """Aplatit polylines, gx:Track et gx:MultiTrack en (feature, coordonnées, horodatages)."""
    for feature in features:
        if not isinstance(feature, dict):
            continue
        kind = feature.get('type')
        if kind == 'multitrack':
            yield from iter_lines(feature.get('tracks') or [])
        elif kind in ('polyline', 'track'):
            coords = feature.get('coordinates') or []
            time_info = feature.get('time_info')
            stamps = (time_info.get('timestamps') if isinstance(time_info, dict) else None) or feature.get('timestamps') or []
            yield feature, coords, stamps


def finite_number(value: Any) -> float:
    """Nombre fini ou NaN ; ni booléen ni chaîne."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return math.nan
    return float(value) if math.isfinite(value) else math.nan


def valid_position(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0  # faux pour NaN


def _check_size(count: int, max_points: Optional[int]) -> None:
    if max_points is not None and count > max_points:
        raise ValueError(f"Trace trop volumineuse (> {max_points} points)")


def raw_from_lines(features: List[Any], max_points: Optional[int] = MAX_TRACK_POINTS) -> Dict[str, list]:
    """Points valides de toutes les lignes, dans l'ordre du fichier. Lève ValueError au-delà de `max_points`."""
    raw = {'lat': [], 'lon': [], 'ele': [], 't': [], 'new_line': []}
    for _feature, coords, stamps in iter_lines(features):
        if not isinstance(coords, list):
            continue
        aligned = isinstance(stamps, list) and len(stamps) == len(coords)
        first = True
        for i, coord in enumerate(coords):
            if not isinstance(coord, (list, tuple)) or len(coord) < 2:
                continue
            lat, lon = finite_number(coord[0]), finite_number(coord[1])
            if not valid_position(lat, lon):
                continue
            stamp = parse_timestamp(stamps[i]) if aligned else None
            raw['lat'].append(lat)
            raw['lon'].append(lon)
            raw['ele'].append(finite_number(coord[2]) if len(coord) > 2 else math.nan)
            raw['t'].append(math.nan if stamp is None else stamp)
            raw['new_line'].append(first)
            first = False
        _check_size(len(raw['lat']), max_points)
    return raw


def _raw_from_markers(points: List[Any], max_points: Optional[int]) -> Dict[str, list]:
    raw = {'lat': [], 'lon': [], 'ele': [], 't': [], 'speed': [], 'hidden': []}
    for point in points:
        if not isinstance(point, dict) or point.get('type', 'marker') != 'marker':
            continue
        coord = point.get('coordinates')
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        lat, lon = finite_number(coord[0]), finite_number(coord[1])
        if not valid_position(lat, lon):
            continue
        info = point.get('parsed_info') if isinstance(point.get('parsed_info'), dict) else {}
        time_info = point.get('time_info') if isinstance(point.get('time_info'), dict) else {}
        speed = finite_number(info.get('speed_kmh'))
        stamp = parse_timestamp(time_info.get('timestamp'))
        ele = finite_number(point.get('altitude'))
        if math.isnan(ele) and len(coord) > 2:
            ele = finite_number(coord[2])
        raw['lat'].append(lat)
        raw['lon'].append(lon)
        raw['ele'].append(ele)
        raw['t'].append(math.nan if stamp is None else stamp)
        raw['speed'].append(speed / 3.6 if speed >= 0 else math.nan)
        raw['hidden'].append(str(point.get('visibility')) == '0')
        _check_size(len(raw['lat']), max_points)
    return raw


# --------------------------------------------------------------------------- Série

@dataclass
class Series:
    """Trace assainie : tableaux parallèles, un élément par point."""
    lat: np.ndarray
    lon: np.ndarray
    ele: np.ndarray                   # sans trou ; zéros si has_elevation est faux
    epoch: Optional[np.ndarray]       # epoch UTC (s) lu dans le fichier, pour l'affichage
    t: Optional[np.ndarray]           # horloge de calcul (s), croissante ; None si inexploitable
    device_speed: Optional[np.ndarray]  # m/s lus dans le fichier (NaN possibles)
    new_line: np.ndarray              # vrai au premier point de chaque trace
    has_elevation: bool
    source: str                       # 'lines' ou 'points'
    notes: List[str] = field(default_factory=list)
    elevation_source: str = 'gps'     # 'gps' (fichier) ou 'dem' (modèle de terrain)
    # Dérivés
    first: np.ndarray = None          # indice du premier point de la trace de chaque point
    last: np.ndarray = None           # indice du dernier point de la trace de chaque point
    lon_unwrapped: np.ndarray = None  # longitudes continues au passage de l'antiméridien
    steps: np.ndarray = None          # distance (m) entre points consécutifs, 0 entre deux traces
    cum: np.ndarray = None            # distance cumulée (m)
    speed: Optional[np.ndarray] = None  # m/s par point, lissée
    speed_source: Optional[str] = None  # 'time', 'device' ou None

    def __len__(self) -> int:
        return len(self.lat)

    def tracks(self) -> List[Tuple[int, int]]:
        """Plages de points [début, fin] (incluses) de chaque trace."""
        starts = np.flatnonzero(self.new_line)
        return list(zip(starts.tolist(), (np.append(starts[1:], len(self)) - 1).tolist()))


def _clock(epoch: np.ndarray, new_line: np.ndarray, notes: List[str]) -> Optional[np.ndarray]:
    """
    Horloge de calcul d'une série horodatée, ou None si le temps est inexploitable.

    Le temps doit être complet et ne jamais reculer à l'intérieur d'une trace. D'une trace
    à la suivante il peut reculer (traces rangées hors ordre chronologique) : l'horloge
    avance alors d'un écart fixe, puisque rien n'est mesuré entre deux traces. Les points
    de même horodatage sont répartis dans le temps (enregistreur à plusieurs hertz).
    """
    if len(epoch) < 2 or np.isnan(epoch).all():
        return None
    if np.isnan(epoch).any():
        notes.append("Horodatage incomplet : les analyses temporelles sont désactivées")
        return None
    dt = np.diff(epoch)
    between = new_line[1:]
    if (dt[~between] < 0).any():
        notes.append("Horodatages désordonnés : les analyses temporelles sont désactivées")
        return None
    if dt[~between].sum() <= 0:
        return None
    clock = epoch
    if (dt[between] < CLOCK_GAP_S).any():
        if (dt[between] < 0).any():
            notes.append("Traces hors ordre chronologique : chacune est mesurée séparément")
        gaps = np.where(between, np.maximum(dt, CLOCK_GAP_S), dt)
        clock = epoch[0] + np.concatenate(([0.0], np.cumsum(gaps)))
    spread = geo.spread_duplicate_times(clock)
    if spread is not clock:
        notes.append("Plusieurs points par horodatage : le temps est réparti entre eux")
    return spread


def _clean_elevation(ele: np.ndarray, notes: List[str], zero_is_missing: bool = True) -> Tuple[np.ndarray, bool]:
    ele = np.where((ele > ELEVATION_MIN_M) & (ele < ELEVATION_MAX_M), ele, np.nan)
    nonzero = ele[np.isfinite(ele) & (ele != 0.0)]
    if len(nonzero) < 2:
        return np.zeros(len(ele)), False
    # Les parsers écrivent 0 quand l'altitude manque : un 0 exact au milieu d'une trace
    # en altitude est un trou, pas un passage au niveau de la mer.
    zeros = ele == 0.0
    if zero_is_missing and zeros.any() and np.percentile(np.abs(nonzero), 5) > 30.0:
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


def build_series(features: List[Any], points: List[Any], max_points: Optional[int] = MAX_TRACK_POINTS,
                 elevation: Optional[Sequence[float]] = None) -> Optional[Series]:
    """
    Trace d'analyse à partir des lignes du fichier ; à défaut (ou si seules les mesures
    ponctuelles portent une vitesse), à partir des points. None si rien d'exploitable.

    `elevation` : altitude du terrain (MNT) alignée point pour point sur les lignes ; elle
    remplace alors l'altitude du fichier. Ignorée si elle ne correspond pas à la trace.
    Lève ValueError si la trace dépasse `max_points`.
    """
    notes: List[str] = []
    lines = raw_from_lines(features or [], max_points)
    markers = _raw_from_markers(points or [], max_points)
    marker_speed = np.asarray(markers['speed'], dtype=float)
    marker_time = np.asarray(markers['t'], dtype=float)

    series = None
    if len(lines['lat']) >= 2:
        lat, lon = np.asarray(lines['lat']), np.asarray(lines['lon'])
        new_line = np.asarray(lines['new_line'], dtype=bool)
        epoch = np.asarray(lines['t'], dtype=float)
        clock = _clock(epoch, new_line, notes)
        device = None
        if clock is None and len(markers['lat']):
            match = _aligned_markers(markers, lat, lon)
            if match is not None:
                if np.isfinite(marker_speed[match]).any():
                    device = marker_speed[match]
                epoch = marker_time[match]
                clock = _clock(epoch, new_line, [])
            elif np.isfinite(marker_speed).sum() >= 2 or np.isfinite(marker_time).sum() >= 2:
                lines = None  # seuls les points portent vitesse ou temps : on analyse les points
        if lines is not None:
            ele, has_elevation = _clean_elevation(np.asarray(lines['ele'], dtype=float), notes)
            series = Series(lat, lon, ele, epoch if clock is not None else None, clock, device, new_line,
                            has_elevation, 'lines', notes)
            _apply_terrain_elevation(series, elevation)
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
        epoch = marker_time[keep]
        clock = _clock(epoch, new_line, notes)
        series = Series(np.asarray(markers['lat'])[keep], np.asarray(markers['lon'])[keep], ele,
                        epoch if clock is not None else None, clock,
                        device if np.isfinite(device).any() else None, new_line, has_elevation, 'points', notes)

    _derive(series)
    return series


def series_from_arrays(lat: Sequence[float], lon: Sequence[float], ele: Sequence[float],
                       t: Sequence[Optional[float]], breaks: Sequence[int] = (),
                       elevation: Optional[Sequence[float]] = None) -> Series:
    """
    Série à partir de tableaux déjà validés (points stockés d'une activité). `t` : epoch UTC
    ou None par point ; `breaks` : indice du premier point de chaque trace ; `elevation` :
    altitude MNT alignée, qui remplace alors celle du fichier. Lève ValueError sous 2 points.
    """
    count = len(lat)
    if count < 2 or not (len(lon) == len(ele) == len(t) == count):
        raise ValueError("Trace vide, réduite à un seul point ou tableaux de tailles différentes")
    notes: List[str] = []
    new_line = np.zeros(count, dtype=bool)
    new_line[[i for i in breaks if 0 < i < count]] = True
    new_line[0] = True
    epoch = np.asarray([math.nan if v is None else v for v in t], dtype=float)
    clock = _clock(epoch, new_line, notes)
    clean, has_elevation = _clean_elevation(np.asarray(ele, dtype=float), notes)
    series = Series(np.asarray(lat, dtype=float), np.asarray(lon, dtype=float), clean,
                    epoch if clock is not None else None, clock, None, new_line, has_elevation, 'lines', notes)
    _apply_terrain_elevation(series, elevation)
    _derive(series)
    return series


def _apply_terrain_elevation(series: Series, elevation: Optional[Sequence[float]]) -> None:
    """Remplace l'altitude du fichier par celle du MNT quand elle est alignée et exploitable."""
    if elevation is None:
        return
    try:
        values = np.asarray(elevation, dtype=float)
    except (TypeError, ValueError):
        values = np.zeros(0)
    if values.shape != (len(series),):
        logger.warning("Altitude MNT ignorée : %s valeur(s) pour %d points", values.shape, len(series))
        series.notes.append("Altitude du terrain non alignée sur la trace : altitude du fichier conservée")
        return
    clean, usable = _clean_elevation(values, series.notes, zero_is_missing=False)
    if usable:
        series.ele, series.has_elevation, series.elevation_source = clean, True, 'dem'


def _derive(series: Series) -> None:
    """Bornes de trace, distances (sans les sauts d'une trace à la suivante) et vitesse lissée par point."""
    index = np.arange(len(series))
    starts = np.flatnonzero(series.new_line)
    series.first = np.maximum.accumulate(np.where(series.new_line, index, 0))
    series.last = np.repeat(np.append(starts[1:], len(series)) - 1, np.diff(np.append(starts, len(series))))
    series.lon_unwrapped = geo.unwrap_longitudes(series.lon)

    steps = geo.step_distances(series.lat, series.lon)
    steps[series.new_line[1:]] = 0.0
    if series.t is not None:
        # Un point aberrant ne compte ni dans la distance ni dans les vitesses
        steps, jumps = geo.despike_steps(series.lat, series.lon, steps, np.diff(series.t))
        if jumps:
            series.notes.append(f"{jumps} saut(s) GPS ignoré(s) dans les distances et les vitesses")
    series.steps = steps
    series.cum = np.concatenate(([0.0], np.cumsum(steps)))

    if series.t is not None:
        series.speed = window_speed(series, SPEED_WINDOW_S, net=False)
        series.speed_source = 'time'
    elif series.device_speed is not None:
        speed = series.device_speed
        known = np.isfinite(speed)
        speed = np.interp(index, index[known], speed[known])
        series.speed = geo.median_filter(speed, 5)
        series.speed_source = 'device'


def window_motion(series: Series, window_s: float, net: bool) -> Tuple[np.ndarray, np.ndarray]:
    """
    Distance (m) et durée (s) en chaque point sur une fenêtre de temps centrée, sans jamais
    sortir de la trace du point : distance parcourue (net=False) ou déplacement à vol
    d'oiseau (net=True).
    """
    t, index = series.t, np.arange(len(series))
    lo = np.maximum(np.searchsorted(t, t - window_s / 2, side='left'), series.first)
    hi = np.minimum(np.searchsorted(t, t + window_s / 2, side='right') - 1, series.last)
    single = lo >= hi  # échantillonnage plus lâche que la fenêtre : voisins immédiats
    lo = np.where(single, np.maximum(index - 1, series.first), lo)
    hi = np.where(single, np.minimum(index + 1, series.last), hi)
    if net:
        dist = geo.haversine(series.lat[lo], series.lon[lo], series.lat[hi], series.lon[hi])
    else:
        dist = series.cum[hi] - series.cum[lo]
    return dist, t[hi] - t[lo]


def window_speed(series: Series, window_s: float, net: bool) -> np.ndarray:
    """Vitesse (m/s) en chaque point sur une fenêtre de temps centrée (voir window_motion)."""
    dist, span = window_motion(series, window_s, net)
    with np.errstate(divide='ignore', invalid='ignore'):
        return np.where(span > 0, dist / span, 0.0)


# ------------------------------------------------------------------------ Arrêts

def _still_points(series: Series, threshold: float) -> np.ndarray:
    """
    Points immobiles : déplacement net sous le seuil sur la fenêtre principale, et pas plus
    grand sur les fenêtres plus courtes (une orbite de 30 s revient à son point de départ,
    mais s'en éloigne d'un diamètre en 15 s).
    """
    still = window_speed(series, STOP_WINDOW_S, net=True) < threshold
    for window_s in STOP_SUB_WINDOWS_S:
        dist, span = window_motion(series, window_s, net=True)
        still &= dist < threshold * np.maximum(span, STOP_WINDOW_S)
    return still


def _still_steps(series: Series, threshold: float) -> Tuple[np.ndarray, np.ndarray]:
    """Pas immobiles et, parmi eux, pauses d'enregistrement (long intervalle sans déplacement)."""
    dt = np.diff(series.t)
    still_point = _still_points(series, threshold)
    slow_gap = (dt >= STOP_WINDOW_S) & (series.steps < threshold * dt) & ~series.new_line[1:]
    return ((still_point[:-1] & still_point[1:]) & ~series.new_line[1:]) | slow_gap, slow_gap


def _drifts(series: Series, start: int, end: int, threshold: float, slow_gap: np.ndarray) -> bool:
    """
    Vrai si la position dérive du début à la fin de la plage : progression lente mais réelle
    (pente raide, escalade), pas un arrêt. Une pause d'enregistrement n'est pas concernée :
    rien n'a été mesuré pendant ce temps.
    """
    t = series.t
    duration = float(t[end] - t[start])
    if duration <= 0 or float(np.diff(t[start:end + 1])[slow_gap[start:end]].sum()) >= duration / 2:
        return False
    quarter = max(1, (end - start + 1) // 4)
    head, tail = slice(start, start + quarter), slice(end - quarter + 1, end + 1)
    drift = float(geo.haversine(np.median(series.lat[head]), np.median(series.lon_unwrapped[head]),
                                np.median(series.lat[tail]), np.median(series.lon_unwrapped[tail])))
    apart = float(np.median(t[tail]) - np.median(t[head]))
    return drift > max(STOP_DRIFT_MIN_M, STOP_DRIFT_RATIO * threshold * apart)


def detect_stops(series: Series, speed_kmh: float = STOP_SPEED_KMH,
                 min_duration_s: float = STOP_MIN_DURATION_S) -> List[Tuple[int, int]]:
    """
    Arrêts, en plages de points [début, fin] (incluses) : immobilité pendant au moins
    `min_duration_s`. Sans horodatage la durée est inconnue : seule la vitesse du fichier
    fait foi. Liste complète, jamais tronquée (les synthèses en dépendent).
    """
    threshold = max(float(speed_kmh), 0.0) / 3.6
    if series.t is None:
        if series.speed is None or series.device_speed is None:
            return []
        still = np.isfinite(series.device_speed) & (series.device_speed <= threshold)
        return [(a, b) for a, b in geo.true_runs(still) if b - a + 1 >= STOP_MIN_POINTS_NO_TIME]

    t = series.t
    still_step, slow_gap = _still_steps(series, threshold)
    merged: List[Tuple[int, int]] = []
    for a, b in ((a, b + 1) for a, b in geo.true_runs(still_step)):  # pas a..b -> points a..b+1
        if merged:
            pa, pb = merged[-1]
            close = geo.haversine(series.lat[pb], series.lon[pb], series.lat[a], series.lon[a]) <= STOP_MERGE_RADIUS_M
            if t[a] - t[pb] <= STOP_MERGE_GAP_S and close and not series.new_line[pb + 1:a + 1].any():
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))

    stops: List[Tuple[int, int]] = []
    strict_step = None
    for a, b in merged:
        if not _drifts(series, a, b, threshold, slow_gap):
            stops.append((a, b))
            continue
        # La plage avance lentement : n'en garder que les vraies haltes, à seuil resserré
        if strict_step is None:
            strict_step, _ = _still_steps(series, threshold * STOP_DRIFT_RATIO)
        for sa, sb in geo.true_runs(strict_step[a:b]):
            if not _drifts(series, a + sa, a + sb + 1, threshold, slow_gap):
                stops.append((a + sa, a + sb + 1))
    return [(a, b) for a, b in stops if t[b] - t[a] >= min_duration_s]


# ---------------------------------------------------------------- Profil et pentes

@dataclass
class Profile:
    """Altitude d'une trace rééchantillonnée à pas de distance constant, lissée, et pentes associées."""
    start: int           # premier et dernier point de la trace dans la série
    end: int
    grid: np.ndarray     # distances cumulées (m), dans le repère de la série
    step: float
    raw: np.ndarray      # altitude interpolée
    smooth: np.ndarray   # altitude lissée
    grade: np.ndarray    # pente (%) de chaque intervalle, taille len(grid) - 1

    def point_index(self, series: Series, distance: float) -> int:
        """Indice du point de la série (dans cette trace) à la distance donnée."""
        offset = np.searchsorted(series.cum[self.start:self.end + 1], distance, side='left')
        return int(min(self.start + offset, self.end))


def build_profiles(series: Series) -> List[Profile]:
    """
    Un profil par trace d'au moins deux pas de grille : l'altitude n'est jamais interpolée
    d'une trace à la suivante (pas de marche ni de pente fantôme à la jonction).
    """
    total = float(series.cum[-1])
    if not series.has_elevation or total < 2 * GRID_M:
        return []
    target = max(GRID_M, total / MAX_GRID_CELLS)
    profiles = []
    for start, end in series.tracks():
        length = float(series.cum[end] - series.cum[start])
        if length < 2 * GRID_M:
            continue
        grid = np.linspace(series.cum[start], series.cum[end], int(math.ceil(length / target)) + 1)
        step = float(grid[1] - grid[0])
        raw = np.interp(grid, series.cum[start:end + 1], series.ele[start:end + 1])
        window = max(1, int(round(SMOOTH_M / step))) | 1  # impair
        smooth = geo.moving_average(raw, window)
        half = max(1, int(round(GRADE_BASE_M / 2 / step)))
        index = np.arange(len(grid))
        lo, hi = np.maximum(index - half, 0), np.minimum(index + half, len(grid) - 1)
        point_grade = (smooth[hi] - smooth[lo]) / (grid[hi] - grid[lo]) * 100.0
        profiles.append(Profile(start, end, grid, step, raw, smooth, (point_grade[:-1] + point_grade[1:]) / 2))
    return profiles


# ----------------------------------------------------------------------- Mesures

@dataclass
class Measures:
    """Grandeurs d'une trace ; None quand la donnée nécessaire manque (jamais d'estimation)."""
    distance_m: float
    duration_s: Optional[float] = None       # temps écoulé dans les traces (pas entre elles)
    stopped_s: Optional[float] = None
    moving_s: Optional[float] = None
    avg_moving_speed_ms: Optional[float] = None
    max_speed_ms: Optional[float] = None     # vitesse de pointe robuste (99e centile en mouvement)
    ascent_m: Optional[float] = None
    descent_m: Optional[float] = None


def moving_steps(series: Series, stops: List[Tuple[int, int]]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pas en mouvement : vitesse (m/s), poids (secondes, ou 1 sans horodatage), masque."""
    speed = (series.speed[:-1] + series.speed[1:]) / 2
    valid = ~series.new_line[1:]
    if series.t is not None:
        weight = np.diff(series.t)
        valid &= weight > 0
    else:
        weight = np.ones(len(speed))
    for start, end in stops:
        valid[start:end] = False
    valid &= speed >= STOP_SPEED_KMH / 3.6
    return speed, weight, valid


def ascent_threshold(source: str) -> float:
    return ASCENT_THRESHOLD_DEM_M if source == 'dem' else ASCENT_THRESHOLD_GPS_M


def measure(series: Series, profiles: List[Profile], stops: List[Tuple[int, int]]) -> Measures:
    """Distance, durées, vitesses et dénivelé de la série, arrêts exclus du temps en mouvement."""
    total = float(series.cum[-1])
    measures = Measures(distance_m=total)
    if profiles:
        gains = [geo.ascent_descent(profile.raw, ascent_threshold(series.elevation_source)) for profile in profiles]
        measures.ascent_m = float(sum(gain[0] for gain in gains))
        measures.descent_m = float(sum(gain[1] for gain in gains))
    if series.t is not None:
        duration = float(np.where(series.new_line[1:], 0.0, np.diff(series.t)).sum())
        stopped = float(sum(series.t[end] - series.t[start] for start, end in stops))
        moving = max(duration - stopped, 0.0)
        measures.duration_s, measures.stopped_s, measures.moving_s = duration, stopped, moving
        if moving > 0:
            stopped_m = float(sum(series.cum[end] - series.cum[start] for start, end in stops))
            measures.avg_moving_speed_ms = (total - stopped_m) / moving
    if series.speed is not None and len(series) >= 2:
        speed, weight, valid = moving_steps(series, stops)
        if valid.any():
            measures.max_speed_ms = float(np.percentile(speed[valid], 99))
            if measures.avg_moving_speed_ms is None:
                measures.avg_moving_speed_ms = float(np.average(speed[valid], weights=weight[valid]))
    return measures
