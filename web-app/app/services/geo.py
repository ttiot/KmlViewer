"""Primitives géographiques vectorisées (numpy), sans dépendance à Flask ni à la base."""

from typing import Tuple

import numpy as np

EARTH_RADIUS_M = 6371000.0


def haversine(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Distance haversine (m) entre deux séries de points (degrés), élément par élément."""
    lat1_r, lat2_r = np.radians(lat1), np.radians(lat2)
    dlat, dlon = lat2_r - lat1_r, np.radians(lon2) - np.radians(lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def step_distances(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Distance haversine (m) entre points consécutifs ; tableau de taille n-1."""
    if len(lat) < 2:
        return np.zeros(0)
    lat, lon = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    return haversine(lat[:-1], lon[:-1], lat[1:], lon[1:])


def cumulative_distance(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Distance cumulée (m) en chaque point ; tableau de taille n commençant à 0."""
    return np.concatenate(([0.0], np.cumsum(step_distances(lat, lon))))


def to_local_xy(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    """Projection équirectangulaire locale en mètres autour de (lat0, lon0) ; tableau (n, 2)."""
    x = np.radians(np.asarray(lon) - lon0) * EARTH_RADIUS_M * np.cos(np.radians(lat0))
    y = np.radians(np.asarray(lat) - lat0) * EARTH_RADIUS_M
    return np.column_stack((x, y))


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    """Moyenne glissante centrée, sans effet de bord (fenêtre réduite aux extrémités)."""
    values = np.asarray(values, dtype=float)
    if window <= 1 or len(values) < window:
        return values
    kernel = np.ones(window)
    return np.convolve(values, kernel, mode='same') / np.convolve(np.ones(len(values)), kernel, mode='same')


def ascent_descent(elevations: np.ndarray, threshold_m: float, smooth_window: int = 5) -> Tuple[float, float]:
    """
    Dénivelés positif et négatif cumulés (m).

    Lissage puis hystérésis : une variation n'est comptée qu'une fois le seuil franchi,
    ce qui évite d'additionner le bruit du capteur point après point.
    """
    ele = moving_average(np.asarray(elevations, dtype=float), smooth_window)
    if len(ele) < 2:
        return 0.0, 0.0
    ascent = descent = 0.0
    ref = ele[0]
    for value in ele[1:]:
        delta = value - ref
        if delta >= threshold_m:
            ascent += delta
            ref = value
        elif delta <= -threshold_m:
            descent -= delta
            ref = value
    return float(ascent), float(descent)


def resample_polyline(xy: np.ndarray, spacing_m: float) -> np.ndarray:
    """Rééchantillonne une polyligne (n, 2) à pas constant, extrémités conservées."""
    if len(xy) < 2:
        return xy
    seg = np.hypot(*np.diff(xy, axis=0).T)
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    total = cum[-1]
    if total <= 0:
        return xy[:1]
    targets = np.linspace(0.0, total, max(2, int(np.ceil(total / spacing_m)) + 1))
    return np.column_stack((np.interp(targets, cum, xy[:, 0]), np.interp(targets, cum, xy[:, 1])))
