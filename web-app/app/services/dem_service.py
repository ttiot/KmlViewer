"""
Correction d'altitude par modèle numérique de terrain, via une API compatible
OpenTopoData (instance auto-hébergée servant par ex. Copernicus GLO-30).

L'altitude GPS d'un smartphone est bruitée et gonfle le D+ ; on la remplace par
l'altitude du terrain sous la trace.
"""

import logging
import time
from typing import List, Optional, Sequence

import numpy as np
import requests

from app.services import geo

logger = logging.getLogger(__name__)

# Inutile d'interroger plus fin que la résolution du MNT (30 m pour GLO-30)
MIN_SPACING_M = 20.0


class DemError(Exception):
    """Le service MNT est injoignable ou a renvoyé une réponse inexploitable."""


class DemCoverageError(DemError):
    """Le service répond, mais n'a pas de dalle pour cette zone : réessayer ne sert à rien."""


def is_enabled(cfg) -> bool:
    return bool(cfg.get('DEM_URL'))


def fetch_elevations(lat: Sequence[float], lon: Sequence[float], cfg) -> List[Optional[float]]:
    """Altitude du terrain pour chaque point (None hors couverture). Lève DemError."""
    url = cfg['DEM_URL']
    if not url.startswith(('http://', 'https://')):
        raise DemError("DEM_URL doit être une URL http(s)")
    batch = max(1, int(cfg.get('DEM_BATCH_SIZE', 100)))
    results: List[Optional[float]] = []
    with requests.Session() as http:
        for start in range(0, len(lat), batch):
            locations = '|'.join(f"{la:.6f},{lo:.6f}"
                                 for la, lo in zip(lat[start:start + batch], lon[start:start + batch]))
            try:
                resp = http.get(url, params={'locations': locations, 'interpolation': 'bilinear'},
                                timeout=cfg.get('DEM_TIMEOUT', 15), allow_redirects=False)
                resp.raise_for_status()
                body = resp.json()
            except (requests.RequestException, ValueError) as exc:
                raise DemError(f"Appel MNT en échec : {exc}") from exc
            chunk = body.get('results') if isinstance(body, dict) else None
            expected = min(batch, len(lat) - start)
            if body.get('status') != 'OK' or not isinstance(chunk, list) or len(chunk) != expected:
                raise DemError(f"Réponse MNT inattendue (status={body.get('status')!r})")
            for item in chunk:
                value = item.get('elevation') if isinstance(item, dict) else None
                results.append(float(value) if isinstance(value, (int, float)) else None)
            if cfg.get('DEM_REQUEST_DELAY') and start + batch < len(lat):
                time.sleep(cfg['DEM_REQUEST_DELAY'])
    return results


def corrected_profile(lat: Sequence[float], lon: Sequence[float], cfg) -> np.ndarray:
    """
    Altitude MNT pour chaque point de la trace. Seul un point tous les ~20 m est
    interrogé, le reste est interpolé le long de la distance parcourue.
    """
    lat_a, lon_a = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    dist = geo.cumulative_distance(lat_a, lon_a)

    keep = [0]
    for i in range(1, len(dist) - 1):
        if dist[i] - dist[keep[-1]] >= MIN_SPACING_M:
            keep.append(i)
    if len(dist) > 1:
        keep.append(len(dist) - 1)

    values = fetch_elevations(lat_a[keep].tolist(), lon_a[keep].tolist(), cfg)
    known = [(dist[idx], v) for idx, v in zip(keep, values) if v is not None]
    if len(known) < max(2, len(keep) // 2):
        raise DemCoverageError("Trace hors couverture du MNT (trop de points sans altitude)")
    xs, ys = zip(*known)
    logger.info("MNT : %d points interrogés pour une trace de %d points", len(keep), len(dist))
    return np.interp(dist, xs, ys)
