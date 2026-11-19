"""
Extraction d'une trace depuis le résultat des parsers GPX/KML et calcul du résumé
d'activité (distance, durées, D+/D-, records sur distances fixes). Fonctions pures.

Les mesures viennent de app.services.track_series, comme celles du visualiseur : une même
trace donne les mêmes distance, durée, temps en mouvement, vitesse de pointe et dénivelé
dans la bibliothèque et dans l'analyse.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from app.services import geo, track_series
# Réexportés : ces noms sont importés d'ici par les parsers, l'éditeur et l'ingestion
from app.services.track_series import (  # noqa: F401
    ASCENT_THRESHOLD_DEM_M, ASCENT_THRESHOLD_GPS_M, MAX_TRACK_POINTS, iter_lines, parse_timestamp,
)

BEST_EFFORT_DISTANCES_M = (1000, 5000, 10000, 21097, 42195)


@dataclass
class Track:
    name: str = ''
    activity_type: str = ''
    lat: List[float] = field(default_factory=list)
    lon: List[float] = field(default_factory=list)
    ele: List[float] = field(default_factory=list)
    t: List[Optional[float]] = field(default_factory=list)  # epoch UTC (s) ou None
    # Indice du premier point de chaque trace du fichier : rien n'est mesuré d'une trace à l'autre
    breaks: List[int] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.lat)


def extract_track(parsed: Dict[str, Any]) -> Track:
    """Concatène toutes les traces d'un fichier parsé en une seule Track."""
    track = Track()
    for feature, coords, stamps in iter_lines(parsed.get('features') or []):
        aligned = len(stamps) == len(coords)
        if not track.name and feature.get('name'):
            track.name = str(feature['name'])
        if not track.activity_type:
            track.activity_type = str((feature.get('properties') or {}).get('activity_type') or '')
        track.breaks.append(len(track))
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


def file_epoch(parsed: Dict[str, Any]) -> Optional[float]:
    """Date portée par le fichier en dehors de la trace : métadonnées, sinon premier repère daté."""
    metadata = parsed.get('metadata') or {}
    candidates = [metadata.get('time'), *(metadata.get('timestamps') or [])[:1]]
    for feature in parsed.get('features') or []:
        time_info = feature.get('time_info') if isinstance(feature, dict) else None
        if isinstance(time_info, dict) and (time_info.get('timestamp') or time_info.get('begin')):
            candidates.append(time_info.get('timestamp') or time_info.get('begin'))
            break
    return next((epoch for epoch in map(parse_timestamp, candidates) if epoch is not None), None)


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


def elevation_gain(ele: Sequence[float], source: str, lat: Optional[Sequence[float]] = None,
                   lon: Optional[Sequence[float]] = None, breaks: Sequence[int] = ()) -> tuple:
    """
    Dénivelés positif et négatif (m) d'une altitude par point, `source` valant 'gps' ou 'dem'.

    Avec `lat` et `lon`, le calcul est celui du résumé et de l'analyse : profil
    rééchantillonné en distance, trace par trace. Sans position (ancien appel), l'hystérésis
    s'applique point par point : acceptable pour un MNT, qui ne bouge pas à l'arrêt, mais
    sensible au bruit d'une altitude GPS.
    """
    if lat is None or lon is None:
        return geo.ascent_descent(np.asarray(ele, dtype=float), track_series.ascent_threshold(source))
    is_dem = source == 'dem'
    series = track_series.series_from_arrays(lat, lon, [0.0] * len(lat) if is_dem else ele, [None] * len(lat),
                                             breaks, elevation=ele if is_dem else None)
    measures = track_series.measure(series, track_series.build_profiles(series), [])
    return measures.ascent_m or 0.0, measures.descent_m or 0.0


def compute_summary(track: Track) -> Dict[str, Any]:
    """Résumé d'une trace à partir de l'altitude GPS. Lève ValueError si inexploitable."""
    if len(track) < 2:
        raise ValueError("Trace vide ou réduite à un seul point")

    series = track_series.series_from_arrays(track.lat, track.lon, track.ele, track.t, track.breaks)
    stops = track_series.detect_stops(series)
    measures = track_series.measure(series, track_series.build_profiles(series), stops)

    summary: Dict[str, Any] = {
        'distance_m': round(measures.distance_m, 1),
        'min_lat': float(series.lat.min()), 'max_lat': float(series.lat.max()),
        'min_lon': float(series.lon.min()), 'max_lon': float(series.lon.max()),
        'has_time': False, 'start_epoch': None,
        'duration_s': None, 'moving_s': None, 'max_speed_ms': None,
        'ascent_gps_m': round(measures.ascent_m or 0.0, 1), 'descent_gps_m': round(measures.descent_m or 0.0, 1),
        'best_efforts': {},
    }
    if series.t is not None:
        summary.update({
            'has_time': True,
            'start_epoch': float(series.epoch.min()),
            'duration_s': round(measures.duration_s, 1),
            'moving_s': round(measures.moving_s, 1),
            'max_speed_ms': None if measures.max_speed_ms is None else round(measures.max_speed_ms, 2),
            'best_efforts': best_efforts(series.cum, series.t),
        })
    else:
        known = [value for value in track.t if value is not None]
        if known:
            summary['start_epoch'] = float(known[0])
    return summary
