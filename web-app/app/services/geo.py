"""Primitives géographiques vectorisées (numpy), sans dépendance à Flask ni à la base."""

from typing import List, Tuple

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


def median_filter(values: np.ndarray, window: int, mode: str = 'edge') -> np.ndarray:
    """
    Médiane glissante centrée : écarte une mesure isolée aberrante. `mode` est celui de
    np.pad aux extrémités : 'edge' répète la valeur du bord, 'reflect' reprend les voisines
    (une valeur aberrante au tout premier point ne se valide alors pas elle-même).
    """
    values = np.asarray(values, dtype=float)
    if len(values) < window:
        return values
    padded = np.pad(values, window // 2, mode=mode)
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, window), axis=1)


def true_runs(mask: np.ndarray) -> List[Tuple[int, int]]:
    """Plages [début, fin] (incluses) des valeurs vraies consécutives."""
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1).tolist(), (np.flatnonzero(edges == -1) - 1).tolist()))


def unwrap_longitudes(lon: np.ndarray) -> np.ndarray:
    """
    Longitudes continues (degrés) : au passage de l'antiméridien la série continue au-delà
    de ±180 au lieu de sauter de 360°. Inchangée quand la trace ne le franchit pas.
    """
    lon = np.asarray(lon, dtype=float)
    return np.unwrap(lon, period=360.0) if len(lon) > 1 else lon


def spread_duplicate_times(t: np.ndarray) -> np.ndarray:
    """
    Répartit dans le temps les points de même horodatage (enregistreur à plusieurs hertz
    daté à la seconde) : k points à l'instant t sont étalés régulièrement jusqu'à l'instant
    suivant, sans dépasser l'intervalle typique de la trace (une pause n'est pas étalée).
    `t` doit être croissant ; il est renvoyé tel quel s'il n'a pas de doublon.
    """
    t = np.asarray(t, dtype=float)
    if len(t) < 2 or not (np.diff(t) == 0).any():
        return t
    values, first, counts = np.unique(t, return_index=True, return_counts=True)
    gaps = np.diff(values)
    if not len(gaps):
        return t  # un seul instant : aucun intervalle à répartir
    typical = float(np.median(gaps))
    room = np.minimum(np.concatenate((gaps, [typical])), typical)
    rank = np.arange(len(t)) - np.repeat(first, counts)
    return t + rank * np.repeat(room / counts, counts)


def despike_steps(lat: np.ndarray, lon: np.ndarray, steps: np.ndarray, dt: np.ndarray, window: int = 11,
                  factor: float = 3.0, margin_ms: float = 3.0, max_run: int = 3,
                  round_trip: float = 0.5) -> Tuple[np.ndarray, int]:
    """
    Neutralise les sauts GPS. Un saut est un aller-retour : un pas bien plus rapide que
    l'allure locale, suivi `max_run` pas plus loin au maximum d'un autre qui ramène près du
    point de départ (déplacement net faible devant le chemin parcouru). Les pas concernés
    sont ramenés au déplacement net. Une pointe de vitesse qui fait réellement avancer
    (déplacement net ≈ chemin) n'est jamais touchée, même brève : la vitesse seule ne suffit
    pas à qualifier un point d'aberrant.

    Cas particulier des extrémités : le premier ou le dernier point n'a pas de retour à
    observer ; un pas aberrant isolé en bout de trace est ramené à l'allure locale.

    Retourne (pas corrigés, nombre de sauts).
    """
    steps, dt = np.array(steps, dtype=float), np.asarray(dt, dtype=float)
    count = len(steps)
    if count == 0:
        return steps, 0
    with np.errstate(divide='ignore', invalid='ignore'):
        speed = np.where(dt > 0, steps / dt, 0.0)
    local = median_filter(speed, window, mode='reflect')
    fast = np.flatnonzero(speed > factor * local + margin_ms).tolist()
    jumps, done = 0, -1  # done : dernier pas déjà corrigé
    for position, a in enumerate(fast):
        if a <= done:
            continue
        for b in fast[position + 1:position + max_run]:  # au plus max_run - 1 candidats : coût linéaire
            if b - a >= max_run:
                break
            path = float(steps[a:b + 1].sum())
            net = float(haversine(lat[a], lon[a], lat[b + 1], lon[b + 1]))
            if net <= round_trip * path:
                span = float(dt[a:b + 1].sum())
                steps[a:b + 1] = net * dt[a:b + 1] / span if span > 0 else net / (b - a + 1)
                jumps, done = jumps + 1, b
                break
        else:
            alone = (position == 0 or fast[position - 1] != a - 1) and (position + 1 == len(fast) or fast[position + 1] != a + 1)
            if alone and a in (0, count - 1):
                steps[a] = local[a] * dt[a]
                jumps, done = jumps + 1, a
    return steps, jumps
