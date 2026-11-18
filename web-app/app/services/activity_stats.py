"""
Extraction d'une trace depuis le résultat des parsers GPX/KML et calcul du résumé
d'activité (distance, durées, D+/D-, records sur distances fixes). Fonctions pures.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from app.services import geo

# Seuils d'hystérésis du D+ : l'altitude GPS est bien plus bruitée qu'un MNT
ASCENT_THRESHOLD_GPS_M = 4.0
ASCENT_THRESHOLD_DEM_M = 2.0
MOVING_SPEED_MS = 0.5  # en dessous, le temps n'est pas compté comme « en mouvement »
BEST_EFFORT_DISTANCES_M = (1000, 5000, 10000, 21097, 42195)
MAX_TRACK_POINTS = 500_000


@dataclass
class Track:
    name: str = ''
    activity_type: str = ''
    lat: List[float] = field(default_factory=list)
    lon: List[float] = field(default_factory=list)
    ele: List[float] = field(default_factory=list)
    t: List[Optional[float]] = field(default_factory=list)  # epoch UTC (s) ou None

    def __len__(self) -> int:
        return len(self.lat)

    @property
    def has_time(self) -> bool:
        """Vrai si tous les points sont horodatés et que le temps ne recule jamais."""
        if len(self.t) < 2 or any(v is None for v in self.t):
            return False
        return bool(np.all(np.diff(np.asarray(self.t, dtype=float)) >= 0)) and self.t[-1] > self.t[0]

    @property
    def has_elevation(self) -> bool:
        return any(e != 0 for e in self.ele)


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


def extract_track(parsed: Dict[str, Any]) -> Track:
    """Concatène toutes les traces d'un fichier parsé en une seule Track."""
    track = Track()
    for feature, coords, stamps in iter_lines(parsed.get('features') or []):
        aligned = len(stamps) == len(coords)
        if not track.name and feature.get('name'):
            track.name = str(feature['name'])
        if not track.activity_type:
            track.activity_type = str((feature.get('properties') or {}).get('activity_type') or '')
        for i, coord in enumerate(coords):
            if len(coord) < 2:
                continue
            lat, lon = float(coord[0]), float(coord[1])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            track.lat.append(lat)
            track.lon.append(lon)
            track.ele.append(float(coord[2]) if len(coord) > 2 and coord[2] is not None else 0.0)
            track.t.append(parse_timestamp(stamps[i]) if aligned else None)
        if len(track) > MAX_TRACK_POINTS:
            raise ValueError(f"Trace trop volumineuse (> {MAX_TRACK_POINTS} points)")
    if not track.name:
        track.name = str((parsed.get('metadata') or {}).get('title') or '')
    return track


def best_efforts(dist_cum: np.ndarray, t: np.ndarray) -> Dict[str, float]:
    """Meilleur temps (s) sur chaque distance de référence, par fenêtre glissante."""
    results: Dict[str, float] = {}
    n = len(dist_cum)
    for target in BEST_EFFORT_DISTANCES_M:
        if dist_cum[-1] < target:
            continue
        # Pour chaque départ i, premier point j tel que la distance parcourue atteint la cible
        ends = np.searchsorted(dist_cum, dist_cum + target, side='left')
        valid = ends < n
        if not valid.any():
            continue
        elapsed = t[ends[valid]] - t[:n][valid]
        elapsed = elapsed[elapsed > 0]
        if len(elapsed):
            results[str(target)] = round(float(elapsed.min()), 1)
    return results


def elevation_gain(ele: np.ndarray, source: str) -> tuple:
    threshold = ASCENT_THRESHOLD_DEM_M if source == 'dem' else ASCENT_THRESHOLD_GPS_M
    return geo.ascent_descent(ele, threshold)


def compute_summary(track: Track) -> Dict[str, Any]:
    """Résumé d'une trace à partir de l'altitude GPS. Lève ValueError si inexploitable."""
    if len(track) < 2:
        raise ValueError("Trace vide ou réduite à un seul point")

    lat, lon = np.asarray(track.lat), np.asarray(track.lon)
    steps = geo.step_distances(lat, lon)
    dist_cum = np.concatenate(([0.0], np.cumsum(steps)))

    summary: Dict[str, Any] = {
        'distance_m': round(float(dist_cum[-1]), 1),
        'min_lat': float(lat.min()), 'max_lat': float(lat.max()),
        'min_lon': float(lon.min()), 'max_lon': float(lon.max()),
        'has_time': False, 'start_epoch': None,
        'duration_s': None, 'moving_s': None, 'max_speed_ms': None,
        'ascent_gps_m': 0.0, 'descent_gps_m': 0.0, 'best_efforts': {},
    }

    if track.has_elevation:
        ascent, descent = elevation_gain(np.asarray(track.ele), 'gps')
        summary['ascent_gps_m'], summary['descent_gps_m'] = round(ascent, 1), round(descent, 1)

    if track.has_time:
        t = np.asarray(track.t, dtype=float)
        dt = np.diff(t)
        with np.errstate(divide='ignore', invalid='ignore'):
            speeds = np.where(dt > 0, steps / dt, 0.0)
        summary.update({
            'has_time': True,
            'start_epoch': float(t[0]),
            'duration_s': round(float(t[-1] - t[0]), 1),
            'moving_s': round(float(dt[speeds >= MOVING_SPEED_MS].sum()), 1),
            # Lissage sur 5 intervalles : un saut GPS isolé ne fait pas un record de vitesse
            'max_speed_ms': round(float(geo.moving_average(speeds, 5).max()), 2),
            'best_efforts': best_efforts(dist_cum, t),
        })
    elif track.t and track.t[0] is not None:
        summary['start_epoch'] = float(track.t[0])
    return summary
