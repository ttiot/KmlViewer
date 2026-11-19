"""
Correction d'altitude par modèle numérique de terrain, via une API compatible
OpenTopoData (instance auto-hébergée servant par ex. Copernicus GLO-30).

L'altitude GPS d'un smartphone est bruitée et gonfle le D+ ; on la remplace par
l'altitude du terrain sous la trace.
"""

import logging
import math
import time
from typing import Any, List, Optional, Sequence

import numpy as np
import requests

from app.services import geo

logger = logging.getLogger(__name__)

# Inutile d'interroger plus fin que la résolution du MNT (30 m pour GLO-30)
MIN_SPACING_M = 20.0
# Altitudes de terrain plausibles (mer Morte, Everest) : hors de là, la valeur est un défaut du service
ELEVATION_MIN_M, ELEVATION_MAX_M = -500.0, 9000.0

# Valeurs par défaut, surchargées par la configuration (clés entre parenthèses)
MAX_GAP_M = 1000.0        # plus long trou sans altitude comblé par interpolation (DEM_MAX_GAP_M)
TOTAL_TIMEOUT_S = 90.0    # budget de temps d'une trace entière, pauses comprises (DEM_TOTAL_TIMEOUT)
RETRIES = 2               # nouvelles tentatives par lot sur une panne passagère (DEM_RETRIES)
RETRY_BACKOFF_S = 0.25    # attente avant la première nouvelle tentative, doublée ensuite (DEM_RETRY_BACKOFF)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class DemError(Exception):
    """Le service MNT est injoignable ou a renvoyé une réponse inexploitable."""


class DemCoverageError(DemError):
    """Le service répond, mais n'a pas de dalle pour cette zone : réessayer ne sert à rien."""


def is_enabled(cfg) -> bool:
    return bool(cfg.get('DEM_URL'))


def _elevation(item: Any) -> Optional[float]:
    """Altitude d'un résultat, ou None si elle est absente, non numérique, non finie ou invraisemblable."""
    value = item.get('elevation') if isinstance(item, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and ELEVATION_MIN_M <= value <= ELEVATION_MAX_M else None


def _get_batch(http: requests.Session, url: str, locations: str, cfg, deadline: float) -> Any:
    """
    Un lot, avec nouvelles tentatives bornées sur une panne passagère (connexion, délai,
    429 ou 5xx) et sans jamais dépasser l'échéance globale. Lève DemError.
    Les messages ne reprennent pas l'URL appelée : elle contient les positions de la trace.
    """
    attempts = 1 + max(0, int(cfg.get('DEM_RETRIES', RETRIES)))
    backoff = max(0.0, float(cfg.get('DEM_RETRY_BACKOFF', RETRY_BACKOFF_S)))
    failure = 'aucune tentative'
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DemError(f"Budget de temps MNT dépassé (dernier échec : {failure})")
        status = None
        try:
            resp = http.get(url, params={'locations': locations, 'interpolation': 'bilinear'},
                            timeout=max(0.1, min(float(cfg.get('DEM_TIMEOUT', 15)), remaining)), allow_redirects=False)
            status = getattr(resp, 'status_code', None)
            if status not in RETRY_STATUSES:
                resp.raise_for_status()
                return resp.json()
            failure = f"HTTP {status}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            failure = type(exc).__name__
        except requests.HTTPError as exc:
            raise DemError(f"Appel MNT refusé (HTTP {status or 'inconnu'})") from exc
        except (requests.RequestException, ValueError) as exc:
            raise DemError(f"Appel MNT en échec : {type(exc).__name__}") from exc
        if attempt + 1 < attempts:
            logger.warning("MNT : tentative %d/%d en échec (%s), nouvel essai", attempt + 1, attempts, failure)
            time.sleep(min(backoff * 2 ** attempt, max(0.0, deadline - time.monotonic())))
    raise DemError(f"Appel MNT en échec après {attempts} tentative(s) : {failure}")


def fetch_elevations(lat: Sequence[float], lon: Sequence[float], cfg,
                     deadline: Optional[float] = None) -> List[Optional[float]]:
    """
    Altitude du terrain pour chaque point (None hors couverture ou valeur inexploitable).
    `deadline` : échéance time.monotonic() ; par défaut le budget DEM_TOTAL_TIMEOUT à partir
    de maintenant. Lève DemError.
    """
    url = cfg['DEM_URL']
    if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
        raise DemError("DEM_URL doit être une URL http(s)")
    if deadline is None:
        deadline = time.monotonic() + max(1.0, float(cfg.get('DEM_TOTAL_TIMEOUT', TOTAL_TIMEOUT_S)))
    batch = max(1, int(cfg.get('DEM_BATCH_SIZE', 100)))
    delay = max(0.0, float(cfg.get('DEM_REQUEST_DELAY') or 0))
    results: List[Optional[float]] = []
    with requests.Session() as http:
        for start in range(0, len(lat), batch):
            locations = '|'.join(f"{la:.6f},{lo:.6f}"
                                 for la, lo in zip(lat[start:start + batch], lon[start:start + batch]))
            body = _get_batch(http, url, locations, cfg, deadline)
            if not isinstance(body, dict):
                raise DemError(f"Réponse MNT inattendue (type {type(body).__name__})")
            chunk = body.get('results')
            expected = min(batch, len(lat) - start)
            if body.get('status') != 'OK' or not isinstance(chunk, list) or len(chunk) != expected:
                raise DemError(f"Réponse MNT inattendue (status={body.get('status')!r})")
            results.extend(_elevation(item) for item in chunk)
            if delay and start + batch < len(lat):
                time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
    return results


def _longest_gap(dist: np.ndarray, keep: List[int], values: List[Optional[float]]) -> float:
    """Plus longue distance (m) que l'interpolation devrait combler entre deux altitudes connues (ou jusqu'à un bout)."""
    longest, previous, pending = 0.0, float(dist[0]), False
    for index, value in zip(keep, values):
        if value is None:
            pending = True
            continue
        if pending:
            longest = max(longest, float(dist[index]) - previous)
        previous, pending = float(dist[index]), False
    return max(longest, float(dist[-1]) - previous) if pending else longest


def corrected_profile(lat: Sequence[float], lon: Sequence[float], cfg) -> np.ndarray:
    """
    Altitude MNT pour chaque point de la trace. Seul un point tous les ~20 m est
    interrogé, le reste est interpolé le long de la distance parcourue.

    Lève DemCoverageError si le MNT ne couvre pas la trace : trop de points sans altitude,
    ou un trou plus long que DEM_MAX_GAP_M (une altitude interpolée sur des kilomètres
    serait inventée). Lève DemError pour une trace vide ou une panne du service.
    """
    lat_a, lon_a = np.asarray(lat, dtype=float), np.asarray(lon, dtype=float)
    if len(lat_a) == 0 or len(lat_a) != len(lon_a):
        raise DemError("Trace vide ou coordonnées de tailles différentes")
    dist = geo.cumulative_distance(lat_a, lon_a)

    keep = [0]
    for i in range(1, len(dist) - 1):
        if dist[i] - dist[keep[-1]] >= MIN_SPACING_M:
            keep.append(i)
    if len(dist) > 1:
        keep.append(len(dist) - 1)

    values = fetch_elevations(lat_a[keep].tolist(), lon_a[keep].tolist(), cfg)
    known = [(dist[idx], v) for idx, v in zip(keep, values) if v is not None]
    if len(known) < max(min(2, len(keep)), len(keep) // 2):
        raise DemCoverageError("Trace hors couverture du MNT (trop de points sans altitude)")
    gap = _longest_gap(dist, keep, values)
    max_gap = float(cfg.get('DEM_MAX_GAP_M', MAX_GAP_M))
    if gap > max_gap:
        raise DemCoverageError(f"Couverture MNT partielle : {gap:.0f} m sans altitude (maximum {max_gap:.0f} m)")
    xs, ys = zip(*known)
    logger.info("MNT : %d points interrogés pour une trace de %d points (%d sans altitude)",
                len(keep), len(dist), len(keep) - len(known))
    return np.interp(dist, xs, ys)
