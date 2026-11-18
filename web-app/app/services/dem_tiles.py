"""
Téléchargement des dalles Copernicus GLO-30 (bucket public AWS Open Data) pour une
emprise donnée, au nommage attendu par OpenTopoData (N45E005.tif = coin sud-ouest).

Autonome, sans Flask ni base :
    python -m app.services.dem_tiles            (emprise lue dans DEM_BBOX)
    DEM_BBOX="lat_min,lon_min,lat_max,lon_max"  ex. France métropolitaine : 41,-6,52,10
"""

import logging
import math
import os
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://copernicus-dem-30m.s3.amazonaws.com'
MAX_TILES = 600           # garde-fou : ~45 Mo par dalle terrestre, soit ~25 Go au maximum
TIMEOUT = (10, 120)
CHUNK = 1024 * 1024


def parse_bbox(value: str) -> Tuple[float, float, float, float]:
    """« lat_min,lon_min,lat_max,lon_max » -> tuple validé. Lève ValueError."""
    try:
        lat_min, lon_min, lat_max, lon_max = (float(v) for v in value.split(','))
    except ValueError as exc:
        raise ValueError("DEM_BBOX attendu : lat_min,lon_min,lat_max,lon_max") from exc
    if not (-90 <= lat_min <= lat_max <= 90 and -180 <= lon_min <= lon_max <= 180):
        raise ValueError("DEM_BBOX hors limites ou bornes inversées")
    return lat_min, lon_min, lat_max, lon_max


def tiles_for_bbox(bbox: Tuple[float, float, float, float]) -> List[Tuple[int, int]]:
    """Coins sud-ouest (lat, lon) entiers des dalles 1°x1° couvrant l'emprise."""
    lat_min, lon_min, lat_max, lon_max = bbox
    lats = range(math.floor(lat_min), min(math.floor(lat_max), 89) + 1)
    lons = range(math.floor(lon_min), min(math.floor(lon_max), 179) + 1)
    return [(lat, lon) for lat in lats for lon in lons]


def tile_name(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tile_url(lat: int, lon: int) -> str:
    ns, ew = ('N' if lat >= 0 else 'S'), ('E' if lon >= 0 else 'W')
    stem = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
    return f"{BASE_URL}/{stem}/{stem}.tif"


def _download(http: requests.Session, url: str, target: Path) -> str:
    """Retourne 'present', 'downloaded' ou 'absent' (dalle inexistante : mer). Lève en cas d'échec."""
    if target.exists():
        # L'écriture est atomique : une dalle présente est complète. Aucun appel réseau,
        # donc un redémarrage de la pile ne dépend pas d'Internet.
        return 'present'
    with http.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False) as resp:
        if resp.status_code == 404:
            return 'absent'
        resp.raise_for_status()
        expected = int(resp.headers.get('Content-Length') or 0)
        partial = target.with_suffix('.part')
        written = 0
        with open(partial, 'wb') as handle:
            for chunk in resp.iter_content(CHUNK):
                handle.write(chunk)
                written += len(chunk)
        if expected and written != expected:
            partial.unlink(missing_ok=True)
            raise IOError(f"téléchargement tronqué ({written}/{expected} octets)")
        os.replace(partial, target)  # atomique : OpenTopoData ne voit jamais de dalle partielle
        return 'downloaded'


def fetch(bbox: Tuple[float, float, float, float], dest: Path) -> Iterator[Tuple[str, str]]:
    """Télécharge les dalles manquantes ; produit (nom, statut) pour chacune."""
    tiles = tiles_for_bbox(bbox)
    if len(tiles) > MAX_TILES:
        raise ValueError(f"Emprise trop grande : {len(tiles)} dalles (maximum {MAX_TILES})")
    dest.mkdir(parents=True, exist_ok=True)
    with requests.Session() as http:
        for lat, lon in tiles:
            name = tile_name(lat, lon)
            yield name, _download(http, tile_url(lat, lon), dest / f"{name}.tif")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    dest = Path(os.environ.get('DEM_TILES_DIR') or '/dem/copernicus30')
    raw = os.environ.get('DEM_BBOX', '').strip()
    if not raw:
        logger.error("DEM_BBOX non défini : rien à télécharger (ex. DEM_BBOX=41,-6,52,10)")
        return 2
    try:
        bbox = parse_bbox(raw)
        counts = {'present': 0, 'downloaded': 0, 'absent': 0}
        for name, status in fetch(bbox, dest):
            counts[status] += 1
            if status == 'downloaded':
                logger.info("Dalle %s téléchargée", name)
    except (ValueError, OSError, requests.RequestException) as exc:
        logger.error("Téléchargement des dalles en échec : %s", exc)
        return 1
    logger.info("Dalles GLO-30 dans %s : %d déjà présentes, %d téléchargées, %d inexistantes (mer)",
                dest, counts['present'], counts['downloaded'], counts['absent'])
    if counts['present'] + counts['downloaded'] == 0:
        logger.error("Aucune dalle terrestre dans cette emprise")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
