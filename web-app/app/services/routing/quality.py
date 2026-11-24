"""
Contrôles de qualité d'une trace routée (gpx-route-builder RB-4.7) : longueur, fermeture, passage ordonné, recouvrement,
accroche des points au réseau. Module PUR (numpy seulement) : aucune connaissance du moteur ni de la base.
"""

import math
from typing import Optional, Sequence

import numpy as np

from app.services import geo

CELL_M = 25.0
NEAR_M = 15.0
MIN_ARC_GAP_M = 120.0      # deux passages « au même endroit » doivent être séparés d'au moins cela le long de la trace
STEP_M = 20.0
MAX_SAMPLES = 20_000       # plafond du rééchantillonnage (une trace de 1000 km à 20 m = 50 000 : on espace davantage)


def _array(points) -> np.ndarray:
    return np.asarray(points, dtype=float).reshape(-1, 2)


def length_m(points) -> float:
    pts = _array(points)
    return float(geo.cumulative_distance(pts[:, 0], pts[:, 1])[-1]) if len(pts) > 1 else 0.0


def closure_m(points) -> float:
    pts = _array(points)
    return float(geo.haversine(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])) if len(pts) else 0.0


def _resample(points, step_m: float = STEP_M):
    pts = _array(points)
    cum = geo.cumulative_distance(pts[:, 0], pts[:, 1])
    total = float(cum[-1]) if len(cum) else 0.0
    if total <= 0:
        return pts[:1], np.zeros(min(1, len(pts)))
    step = max(step_m, total / MAX_SAMPLES)
    marks = np.arange(0, total, step)
    return np.column_stack([np.interp(marks, cum, pts[:, 0]), np.interp(marks, cum, pts[:, 1])]), marks


def overlap_rate(points) -> float:
    """
    Part de la distance « repassée » (0..1) : un point échantillonné est répété s'il existe un point antérieur à moins de
    NEAR_M dont l'abscisse curviligne diffère d'au moins MIN_ARC_GAP_M. Aller-retour pur => ~0,5 ; boucle propre => ~0.
    """
    samples, arc = _resample(points)
    if len(samples) < 3:
        return 0.0
    kx = 111_320.0 * max(math.cos(math.radians(float(np.mean(samples[:, 0])))), 1e-6)
    xy = np.column_stack([samples[:, 1] * kx, samples[:, 0] * 110_540.0])
    grid: dict = {}
    repeated = 0
    for i, (x, y) in enumerate(xy):
        cx, cy = int(x // CELL_M), int(y // CELL_M)
        hit = any(arc[i] - arc[j] >= MIN_ARC_GAP_M and math.hypot(x - xy[j][0], y - xy[j][1]) <= NEAR_M
                  for dx in (-1, 0, 1) for dy in (-1, 0, 1) for j in grid.get((cx + dx, cy + dy), ()))
        repeated += hit
        grid.setdefault((cx, cy), []).append(i)
    return repeated / len(xy)


def snap_to_trace(trace, waypoints: Sequence) -> tuple:
    """
    Position de chaque point de l'utilisateur SUR la trace de base (le moteur accroche un point hors voie à la voie la plus
    proche) et écart d'accroche en m. Retourne (points accrochés, écarts).
    """
    arr = _array(trace)
    snapped, gaps = [], []
    for lat, lon in waypoints:
        d = geo.haversine(lat, lon, arr[:, 0], arr[:, 1])
        i = int(np.argmin(d))
        snapped.append((float(arr[i][0]), float(arr[i][1])))
        gaps.append(float(d[i]))
    return snapped, gaps


def ordered_pass(trace, waypoints: Sequence, tolerance_m: float) -> bool:
    """Chaque point est à ≤ tolérance de la trace, à des positions STRICTEMENT croissantes le long de la trace (RB-4.7)."""
    arr = _array(trace)
    last = -1
    for lat, lon in waypoints:
        d = geo.haversine(lat, lon, arr[last + 1:, 0], arr[last + 1:, 1])
        close = np.nonzero(d <= tolerance_m)[0]
        if len(close) == 0:
            return False
        last = last + 1 + int(close[0])
    return True


def target_gap(distance_m: float, target_m: Optional[float]) -> Optional[float]:
    """Écart relatif signé à la cible, None sans cible."""
    return None if not target_m else (distance_m - target_m) / target_m
