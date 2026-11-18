"""
Ingestion d'un fichier GPX/KML dans la bibliothèque d'un utilisateur :
parsing (code existant) -> résumé -> correction d'altitude -> SQLite -> tronçons.
Indépendant de Flask : `cfg` est un simple mapping de configuration.
"""

import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Dict, Mapping, Optional

import numpy as np

from app.services import activity_stats, activity_store, dem_service, segment_service
from app.services.gpx_parser import GPXParser
from app.services.kml_parser import KMLParser

logger = logging.getLogger(__name__)

INGEST_EXTENSIONS = ('gpx', 'kml')
MAX_NAME_LENGTH = 200


@dataclass
class IngestResult:
    status: str                      # 'created' | 'duplicate' | 'error'
    activity_id: Optional[int] = None
    message: str = ''


def file_extension(filename: str) -> str:
    return PurePath(filename).suffix.lower().lstrip('.')


def parse_content(content: str, ext: str) -> Dict[str, Any]:
    if ext == 'gpx':
        return GPXParser.parse_gpx_coordinates(content)
    return KMLParser.parse_kml_coordinates(content, 'double')


def _elevation_fields(track, cfg: Mapping, summary: Dict[str, Any]) -> Dict[str, Any]:
    """D+/D- retenus : MNT si disponible, sinon GPS. Une panne du MNT ne bloque pas l'ingestion."""
    fields = {'ascent_m': summary['ascent_gps_m'], 'descent_m': summary['descent_gps_m'],
              'elev_source': 'gps', 'ele_dem': None, 'dem_status': None}
    if not dem_service.is_enabled(cfg):
        return fields
    try:
        ele_dem = dem_service.corrected_profile(track.lat, track.lon, cfg)
    except dem_service.DemCoverageError as exc:
        logger.info("Pas de dalle MNT pour cette trace, altitude GPS conservée : %s", exc)
        return {**fields, 'dem_status': 'no_coverage'}
    except dem_service.DemError as exc:
        # dem_status reste NULL : le rattrapage en tâche de fond réessaiera
        logger.warning("Correction d'altitude indisponible, altitude GPS conservée : %s", exc)
        return fields
    ascent, descent = activity_stats.elevation_gain(ele_dem, 'dem')
    return {'ascent_m': round(ascent, 1), 'descent_m': round(descent, 1),
            'elev_source': 'dem', 'ele_dem': ele_dem, 'dem_status': 'ok'}


def ingest_bytes(db: sqlite3.Connection, cfg: Mapping, user_id: int, raw: bytes, filename: str,
                 source: str, fallback_epoch: Optional[float] = None) -> IngestResult:
    """Ingère un fichier. Ne lève pas : toute erreur est renvoyée dans IngestResult."""
    filename = PurePath(filename).name[:MAX_NAME_LENGTH] or 'fichier'
    ext = file_extension(filename)
    if ext not in INGEST_EXTENSIONS:
        return IngestResult('error', message="Seuls les fichiers .gpx et .kml sont pris en charge")
    if not raw:
        return IngestResult('error', message="Fichier vide")
    if len(raw) > cfg.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024):
        return IngestResult('error', message="Fichier trop volumineux")

    sha256 = hashlib.sha256(raw).hexdigest()
    existing = activity_store.find_by_hash(db, user_id, sha256)
    if existing is not None:
        return IngestResult('duplicate', existing['id'], "Fichier déjà importé")

    try:
        content = raw.decode('utf-8-sig')
        parsed = parse_content(content, ext)
        if not parsed.get('success'):
            return IngestResult('error', message=str(parsed.get('error') or 'Fichier illisible'))
        track = activity_stats.extract_track(parsed)
        summary = activity_stats.compute_summary(track)
    except UnicodeDecodeError:
        return IngestResult('error', message="Encodage invalide (UTF-8 attendu)")
    except ValueError as exc:
        return IngestResult('error', message=str(exc))

    elevation = _elevation_fields(track, cfg, summary)
    start_epoch = summary['start_epoch'] or fallback_epoch or time.time()
    name = (track.name or PurePath(filename).stem)[:MAX_NAME_LENGTH]

    try:
        activity_store.write_raw_file(
            activity_store.raw_file_path(cfg['DATA_DIR'], user_id, sha256, ext), raw)
        activity_id = activity_store.insert(db, user_id, {
            'name': name, 'filename': filename, 'sha256': sha256, 'source': source,
            'activity_type': track.activity_type[:50] or None,
            'start_time': activity_store.epoch_to_iso(start_epoch),
            'has_time': int(summary['has_time']),
            'duration_s': summary['duration_s'], 'moving_s': summary['moving_s'],
            'distance_m': summary['distance_m'],
            'ascent_m': elevation['ascent_m'], 'descent_m': elevation['descent_m'],
            'ascent_gps_m': summary['ascent_gps_m'], 'descent_gps_m': summary['descent_gps_m'],
            'elev_source': elevation['elev_source'], 'dem_status': elevation['dem_status'],
            'max_speed_ms': summary['max_speed_ms'],
            'min_lat': summary['min_lat'], 'max_lat': summary['max_lat'],
            'min_lon': summary['min_lon'], 'max_lon': summary['max_lon'],
            'best_efforts': summary['best_efforts'],
            'points': activity_store.pack_points(track.lat, track.lon, track.ele, track.t,
                                                 elevation['ele_dem']),
        })
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()  # course entre deux ingestions du même fichier
        existing = activity_store.find_by_hash(db, user_id, sha256)
        return IngestResult('duplicate', existing['id'] if existing else None, "Fichier déjà importé")
    except (OSError, sqlite3.Error) as exc:
        db.rollback()
        logger.exception("Échec d'enregistrement de %s", filename)
        return IngestResult('error', message=f"Erreur de stockage : {exc.__class__.__name__}")

    efforts = segment_service.match_activity(db, user_id, activity_id)
    logger.info("Activité %s ingérée (user=%s, source=%s, %.1f km, D+ %s m [%s], %d effort(s))",
                activity_id, user_id, source, summary['distance_m'] / 1000,
                elevation['ascent_m'], elevation['elev_source'], efforts)
    return IngestResult('created', activity_id)


def recompute_elevation(db: sqlite3.Connection, cfg: Mapping, user_id: int, activity_id: int) -> Dict[str, Any]:
    """(Re)corrige l'altitude d'une activité via le MNT. Lève LookupError / DemError."""
    row = activity_store.get(db, user_id, activity_id)
    if row is None:
        raise LookupError("Activité introuvable")
    if not dem_service.is_enabled(cfg):
        raise dem_service.DemError("Correction d'altitude non configurée (DEM_URL)")
    pts = activity_store.unpack_points(row['points'])
    ele_dem = dem_service.corrected_profile(pts['lat'], pts['lon'], cfg)
    ascent, descent = activity_stats.elevation_gain(np.asarray(ele_dem), 'dem')
    activity_store.update_elevation(
        db, user_id, activity_id,
        points=activity_store.pack_points(pts['lat'], pts['lon'], pts['ele'], pts['t'], ele_dem),
        ascent_m=round(ascent, 1), descent_m=round(descent, 1), elev_source='dem')
    return {'ascent_m': round(ascent, 1), 'descent_m': round(descent, 1),
            'ascent_gps_m': row['ascent_gps_m'], 'elev_source': 'dem'}


def backfill_elevation(db: sqlite3.Connection, cfg: Mapping, limit: int = 5) -> int:
    """
    Rattrapage : corrige les activités ingérées pendant que le MNT était indisponible
    (ou avant son activation). Retourne le nombre d'activités corrigées. S'arrête à la
    première panne : le prochain cycle réessaiera.
    """
    if not dem_service.is_enabled(cfg):
        return 0
    corrected = 0
    for row in activity_store.pending_dem(db, limit):
        try:
            recompute_elevation(db, cfg, row['user_id'], row['id'])
            corrected += 1
        except dem_service.DemCoverageError:
            activity_store.set_dem_status(db, row['user_id'], row['id'], 'no_coverage')
        except dem_service.DemError as exc:
            logger.debug("Rattrapage MNT suspendu : %s", exc)
            break
        except LookupError:
            continue  # supprimée entre-temps
    if corrected:
        logger.info("Rattrapage MNT : %d activité(s) corrigée(s)", corrected)
    return corrected
