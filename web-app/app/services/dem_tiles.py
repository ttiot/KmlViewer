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
import time
from pathlib import Path
from typing import Iterator, List, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://copernicus-dem-30m.s3.amazonaws.com'
MAX_TILES = 600           # garde-fou : ~45 Mo par dalle terrestre, soit ~25 Go au maximum
TIMEOUT = (10, 120)
CHUNK = 1024 * 1024
ATTEMPTS = 3              # par dalle, sur une panne passagère
RETRY_BACKOFF_S = 2.0     # attente avant la deuxième tentative, doublée ensuite
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Une dalle GLO-30 pèse de quelques centaines de ko (côte) à ~45 Mo : hors de ces bornes,
# ou sans signature TIFF, ce qui a été reçu n'est pas une dalle (page d'erreur d'un proxy…)
MIN_TILE_BYTES = 1024
MAX_TILE_BYTES = 512 * 1024 * 1024
TIFF_SIGNATURES = (b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+')  # TIFF et BigTIFF, petit et grand boutiste


class TileError(IOError):
    """Le contenu reçu ou présent sur disque n'est pas une dalle exploitable."""


def parse_bbox(value: str) -> Tuple[float, float, float, float]:
    """« lat_min,lon_min,lat_max,lon_max » -> tuple validé. Lève ValueError."""
    try:
        lat_min, lon_min, lat_max, lon_max = (float(v) for v in value.split(','))
    except ValueError as exc:
        raise ValueError("DEM_BBOX attendu : lat_min,lon_min,lat_max,lon_max") from exc
    if not (-90 <= lat_min <= lat_max <= 90 and -180 <= lon_min <= lon_max <= 180):
        raise ValueError("DEM_BBOX hors limites ou bornes inversées")
    return lat_min, lon_min, lat_max, lon_max


def _tile_range(low: float, high: float, last: int) -> range:
    """Dalles d'un axe : une borne haute entière appartient à la dalle d'en dessous, pas à la suivante."""
    first = math.floor(low)
    return range(first, min(max(first, math.ceil(high) - 1), last) + 1)


def tiles_for_bbox(bbox: Tuple[float, float, float, float]) -> List[Tuple[int, int]]:
    """Coins sud-ouest (lat, lon) entiers des dalles 1°x1° couvrant l'emprise, sans rangée ni colonne en trop."""
    lat_min, lon_min, lat_max, lon_max = bbox
    return [(lat, lon) for lat in _tile_range(lat_min, lat_max, 89) for lon in _tile_range(lon_min, lon_max, 179)]


def tile_name(lat: int, lon: int) -> str:
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def tile_url(lat: int, lon: int) -> str:
    ns, ew = ('N' if lat >= 0 else 'S'), ('E' if lon >= 0 else 'W')
    stem = f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"
    return f"{BASE_URL}/{stem}/{stem}.tif"


def _is_tile(path: Path) -> bool:
    """Vrai si le fichier a la taille et la signature d'une dalle GeoTIFF."""
    try:
        if not MIN_TILE_BYTES <= path.stat().st_size <= MAX_TILE_BYTES:
            return False
        with open(path, 'rb') as handle:
            return handle.read(4) in TIFF_SIGNATURES
    except OSError:
        return False


def _fsync_dir(directory: Path) -> None:
    """Rend le renommage durable ; sans effet là où un dossier ne s'ouvre pas (Windows)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _download_once(http: requests.Session, url: str, target: Path) -> str:
    """Une tentative : 'downloaded' ou 'absent'. Lève TileError (contenu invalide) ou une erreur réseau."""
    partial = target.with_suffix('.part')
    with http.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False) as resp:
        if resp.status_code == 404:
            return 'absent'
        resp.raise_for_status()
        if resp.status_code != 200:
            raise TileError(f"réponse HTTP {resp.status_code} inattendue")
        expected = int(resp.headers.get('Content-Length') or 0)
        written = 0
        try:
            with open(partial, 'wb') as handle:
                for chunk in resp.iter_content(CHUNK):
                    written += len(chunk)
                    if written > MAX_TILE_BYTES:
                        raise TileError(f"dalle trop volumineuse (> {MAX_TILE_BYTES} octets)")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if expected and written != expected:
                raise TileError(f"téléchargement tronqué ({written}/{expected} octets)")
            if not _is_tile(partial):
                raise TileError(f"contenu reçu invalide ({written} octets, pas un GeoTIFF)")
            os.replace(partial, target)  # atomique : OpenTopoData ne voit jamais de dalle partielle
        finally:
            partial.unlink(missing_ok=True)
    _fsync_dir(target.parent)
    return 'downloaded'


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        return getattr(exc.response, 'status_code', None) in RETRY_STATUSES
    return isinstance(exc, (TileError, requests.ConnectionError, requests.Timeout))


def _download(http: requests.Session, url: str, target: Path) -> str:
    """Retourne 'present', 'downloaded' ou 'absent' (dalle inexistante : mer). Lève en cas d'échec."""
    if target.exists():
        if _is_tile(target):
            # L'écriture est atomique et vérifiée : une dalle présente est complète. Aucun appel
            # réseau, donc un redémarrage de la pile ne dépend pas d'Internet.
            return 'present'
        logger.warning("Dalle %s présente mais invalide (taille ou signature) : nouveau téléchargement", target.name)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _download_once(http, url, target)
        except (OSError, requests.RequestException) as exc:
            if attempt == ATTEMPTS or not _retryable(exc):
                raise
            logger.warning("Dalle %s : tentative %d/%d en échec (%s), nouvel essai",
                           target.name, attempt, ATTEMPTS, exc)
            time.sleep(RETRY_BACKOFF_S * 2 ** (attempt - 1))
    raise TileError(f"dalle {target.name} : aucune tentative effectuée")  # ATTEMPTS < 1


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
