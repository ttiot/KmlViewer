"""
Ingestion d'un fichier GPX/KML dans la bibliothèque d'un utilisateur :
parsing (code existant) -> résumé -> correction d'altitude -> SQLite -> tronçons.
Indépendant de Flask : `cfg` est un simple mapping de configuration.
"""

import hashlib
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Dict, Mapping, Optional, Set

import numpy as np

from app.settings import effective

from app.services import (
    activity_stats, activity_store, activity_types, dem_service, file_formats, profile_service, segment_service,
)
from app.services.gpx_parser import GPXParser
from app.services.kml_parser import KMLParser

logger = logging.getLogger(__name__)

INGEST_EXTENSIONS = file_formats.SUPPORTED_EXTENSIONS
MAX_NAME_LENGTH = file_formats.MAX_NAME_LENGTH
# Date de début plausible : après le lancement du GPS, et pas dans le futur (marge d'un jour
# pour les fuseaux). En dehors, l'horodatage du fichier est faux : l'activité est rangée à
# la date d'import, comme un fichier sans date.
MIN_START_EPOCH = 315_964_800.0  # 1980-01-06
MAX_FUTURE_S = 86_400.0
# Colonnes numériques du résumé : un réel non fini y rendrait la liste et le tableau de bord inutilisables
_NUMERIC_FIELDS = ('duration_s', 'moving_s', 'distance_m', 'ascent_gps_m', 'descent_gps_m', 'max_speed_ms',
                   'min_lat', 'max_lat', 'min_lon', 'max_lon')


@dataclass
class IngestResult:
    status: str                      # 'created' | 'duplicate' | 'error'
    activity_id: Optional[int] = None
    message: str = ''
    # Erreur qui ne tient pas au fichier (quota, disque, base) : le réessayer plus tard a un sens
    retryable: bool = False


# Alias conservé pour les appelants (routes) : l'extension se calcule dans file_formats
file_extension = file_formats.extension


def parse_raw(raw: bytes, filename: str) -> Dict[str, Any]:
    """Parse un fichier brut (GPX, KML ou KMZ). Lève FileFormatError s'il est refusé."""
    content, fmt = file_formats.decode(raw, filename)
    if fmt == 'gpx':
        return GPXParser.parse_gpx_coordinates(content)
    return KMLParser.parse_kml_coordinates(content)


def _plausible_epoch(epoch: Optional[float], now: float) -> Optional[float]:
    if epoch is None or not math.isfinite(epoch) or not MIN_START_EPOCH <= epoch <= now + MAX_FUTURE_S:
        return None
    return epoch


def _all_finite(values: Mapping[str, Any], keys) -> bool:
    return all(values.get(key) is None or math.isfinite(values[key]) for key in keys)


def _elevation_fields(track, cfg: Mapping, summary: Dict[str, Any], defer: bool) -> Dict[str, Any]:
    """
    D+/D- retenus : MNT si disponible, sinon GPS. Une panne du MNT ne bloque pas l'ingestion.
    Avec defer, le MNT n'est pas interrogé : dem_status reste NULL et la tâche de fond s'en charge.
    """
    fields = {'ascent_m': summary['ascent_gps_m'], 'descent_m': summary['descent_gps_m'],
              'elev_source': 'gps', 'ele_dem': None, 'dem_status': None}
    if defer or not dem_service.is_enabled(cfg):
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
    ascent, descent = activity_stats.elevation_gain(ele_dem, 'dem', track.lat, track.lon, track.breaks)
    return {'ascent_m': round(ascent, 1), 'descent_m': round(descent, 1),
            'elev_source': 'dem', 'ele_dem': ele_dem, 'dem_status': 'ok'}


def ingest_bytes(db: sqlite3.Connection, cfg: Mapping, user_id: int, raw: bytes, filename: str,
                 source: str, fallback_epoch: Optional[float] = None,
                 defer_elevation: bool = False) -> IngestResult:
    """
    Ingère un fichier. Ne lève pas : toute erreur est renvoyée dans IngestResult.
    defer_elevation : laisse la correction d'altitude à la tâche de fond (import interactif,
    où interroger le MNT fichier par fichier ferait expirer la requête HTTP).
    """
    try:
        return _ingest(db, cfg, user_id, raw, filename, source, fallback_epoch, defer_elevation)
    except Exception as exc:  # noqa: BLE001 - contrat : un fichier, quel qu'il soit, ne fait pas tomber le lot
        logger.exception("Ingestion de %r : erreur inattendue", str(filename)[:MAX_NAME_LENGTH])
        try:
            db.rollback()
        except sqlite3.Error:
            pass
        return IngestResult('error', message=f"Erreur interne : {exc.__class__.__name__}")


def _ingest(db: sqlite3.Connection, cfg: Mapping, user_id: int, raw: bytes, filename: str,
            source: str, fallback_epoch: Optional[float], defer_elevation: bool) -> IngestResult:
    # L'extension est lue sur le nom réduit, dont seul le radical est tronqué
    filename = file_formats.safe_name(filename, MAX_NAME_LENGTH)
    ext = file_formats.extension(filename)
    if ext not in INGEST_EXTENSIONS:
        return IngestResult('error', message="Seuls les fichiers .gpx, .kml et .kmz sont pris en charge")
    if not raw:
        return IngestResult('error', message="Fichier vide")
    if len(raw) > cfg.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024):
        return IngestResult('error', message="Fichier trop volumineux")

    sha256 = hashlib.sha256(raw).hexdigest()
    try:
        existing = activity_store.find_by_hash(db, user_id, sha256)
    except sqlite3.Error as exc:
        logger.error("Base illisible pendant l'ingestion de %s : %s", filename, exc)
        return IngestResult('error', message=f"Erreur de stockage : {exc.__class__.__name__}", retryable=True)
    if existing is not None:
        return IngestResult('duplicate', existing['id'], "Fichier déjà importé")

    quota = int(effective(cfg, 'USER_QUOTA_MB', db)) * 1024 * 1024
    if quota:
        try:
            used = activity_store.user_storage_bytes(cfg['DATA_DIR'], user_id)
        except OSError as exc:
            logger.error("Quota illisible pour l'utilisateur %s : %s", user_id, exc)
            return IngestResult('error', message="Erreur de stockage", retryable=True)
        if used + len(raw) > quota:
            return IngestResult('error', message="Quota de stockage atteint : supprime des activités", retryable=True)

    try:
        parsed = parse_raw(raw, filename)
        if not parsed.get('success'):
            return IngestResult('error', message=str(parsed.get('error') or 'Fichier illisible'))
        track = activity_stats.extract_track(parsed)
        summary = activity_stats.compute_summary(track)
    except (ValueError, ArithmeticError) as exc:  # FileFormatError comprise ; dépassement numérique
        return IngestResult('error', message=str(exc) if isinstance(exc, ValueError) else "Valeurs numériques invalides")

    elevation = _elevation_fields(track, cfg, summary, defer_elevation)
    if not _all_finite(summary, _NUMERIC_FIELDS) or not _all_finite(elevation, ('ascent_m', 'descent_m')):
        return IngestResult('error', message="Valeurs numériques invalides")
    # Sans date (plausible) dans le fichier, l'activité est rangée à la date du fichier ou de
    # l'import, et marquée comme telle : ce n'est pas la date de la sortie.
    now = time.time()
    known_epoch = _plausible_epoch(summary['start_epoch'], now) or _plausible_epoch(activity_stats.file_epoch(parsed), now)
    start_epoch = known_epoch or _plausible_epoch(fallback_epoch, now) or now
    name = (track.name or PurePath(filename).stem)[:MAX_NAME_LENGTH]
    values = {
        'name': name, 'filename': filename, 'sha256': sha256, 'source': source,
        # Type du fichier ramené au catalogue ; sans type, celui que l'utilisateur a choisi par défaut
        'activity_type': (activity_types.from_raw(track.activity_type)
                          or profile_service.default_activity_type(db, user_id)),
        'start_time': activity_store.epoch_to_iso(start_epoch),
        'start_time_known': int(known_epoch is not None),
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
        'points': activity_store.pack_points(track.lat, track.lon, track.ele, track.t, elevation['ele_dem'],
                                              track.breaks),
    }

    stored = _store(db, cfg, user_id, values, raw, ext)
    if stored.status != 'created':
        return stored

    try:
        efforts = segment_service.match_activity(db, user_id, stored.activity_id)
    except Exception:  # noqa: BLE001 - l'activité est enregistrée : la recherche de tronçons est rejouable (rescan)
        logger.exception("Recherche de tronçons en échec pour l'activité %s", stored.activity_id)
        db.rollback()
        efforts = 0
    logger.info("Activité %s ingérée (user=%s, source=%s, %.1f km, D+ %s m [%s], %d effort(s))",
                stored.activity_id, user_id, source, summary['distance_m'] / 1000,
                elevation['ascent_m'], elevation['elev_source'], efforts)
    return stored


def _store(db: sqlite3.Connection, cfg: Mapping, user_id: int, values: Dict[str, Any],
           raw: bytes, ext: str) -> IngestResult:
    """
    Enregistre la ligne et le fichier d'origine, ensemble ou pas du tout.

    L'INSERT passe en premier : il prend le verrou d'écriture, sous lequel le fichier est
    écrit puis la transaction validée. Une suppression concurrente de la même activité
    (activity_store.delete, qui retire le fichier sous ce même verrou) ne peut donc pas
    s'intercaler, et un échec ne laisse ni ligne sans fichier ni fichier sans ligne.
    """
    sha256, filename = values['sha256'], values['filename']
    path = activity_store.raw_file_path(cfg['DATA_DIR'], user_id, sha256, ext)
    written = False
    try:
        activity_id = activity_store.insert(db, user_id, values)
        activity_store.write_raw_file(path, raw)
        written = True
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        existing = activity_store.find_by_hash(db, user_id, sha256)
        if existing is not None:  # course entre deux ingestions du même fichier
            return IngestResult('duplicate', existing['id'], "Fichier déjà importé")
        # Autre contrainte (valeur manquante…) : ce n'est pas un doublon, ne pas le prétendre
        logger.error("Enregistrement de %s refusé par la base : %s", filename, exc)
        return IngestResult('error', message="Fichier inexploitable")
    except (OSError, sqlite3.Error) as exc:
        db.rollback()
        logger.exception("Échec d'enregistrement de %s", filename)
        if written:
            # Ligne annulée : le fichier écrit pour elle ne doit pas rester (quota, données orphelines)
            activity_store.remove_raw_file(path)
        return IngestResult('error', message=f"Erreur de stockage : {exc.__class__.__name__}", retryable=True)
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
    breaks = pts.get('breaks') or []
    # Même calcul que le résumé et l'analyse : profil en distance, trace par trace
    ascent, descent = activity_stats.elevation_gain(np.asarray(ele_dem), 'dem', pts['lat'], pts['lon'], breaks)
    activity_store.update_elevation(
        db, user_id, activity_id,
        points=activity_store.pack_points(pts['lat'], pts['lon'], pts['ele'], pts['t'], ele_dem, breaks),
        ascent_m=round(ascent, 1), descent_m=round(descent, 1), elev_source='dem')
    return {'ascent_m': round(ascent, 1), 'descent_m': round(descent, 1),
            'ascent_gps_m': row['ascent_gps_m'], 'elev_source': 'dem'}


def recompute_summaries(db: sqlite3.Connection, user_id: Optional[int] = None) -> int:
    """
    Recalcule distance, durées, vitesse max, D+ GPS et records des activités déjà stockées,
    à partir de leurs points (utile après une correction du calcul). L'altitude MNT est
    conservée. Retourne le nombre d'activités mises à jour.
    """
    sql, params = "SELECT id, user_id FROM activities", ()
    if user_id is not None:
        sql, params = sql + " WHERE user_id = ?", (user_id,)
    updated = 0
    for ref in db.execute(sql + " ORDER BY id", params).fetchall():
        row = activity_store.get(db, ref['user_id'], ref['id'])
        pts = activity_store.unpack_points(row['points'])
        track = activity_stats.Track(lat=pts['lat'], lon=pts['lon'], ele=pts['ele'], t=pts['t'],
                                     breaks=pts.get('breaks') or [])
        try:
            summary = activity_stats.compute_summary(track)
        except ValueError:
            continue
        values = {key: summary[key] for key in ('distance_m', 'duration_s', 'moving_s', 'max_speed_ms',
                                                'ascent_gps_m', 'descent_gps_m', 'best_efforts')}
        values['has_time'] = int(summary['has_time'])
        if row['elev_source'] != 'dem':
            values['ascent_m'], values['descent_m'] = summary['ascent_gps_m'], summary['descent_gps_m']
        activity_store.update_summary(db, ref['user_id'], ref['id'], values)
        updated += 1
    db.commit()
    return updated


DEM_MAX_FAILURES_PER_PASS = 3   # au-delà, le passage s'arrête : service probablement indisponible
# Activités dont la dernière correction a échoué, servies après les autres. En mémoire : un
# seul processus fait tourner la tâche de fond, et l'ordre de la file n'a pas à survivre à un
# redémarrage.
_dem_failed: Set[int] = set()


def backfill_elevation(db: sqlite3.Connection, cfg: Mapping, limit: Optional[int] = None) -> int:
    """
    Rattrapage : corrige les activités ingérées pendant que le MNT était indisponible
    (ou avant son activation). Retourne le nombre d'activités corrigées.

    Une activité en échec repasse en fin de file au lieu d'être retentée en tête à chaque
    cycle : une trace qui échoue à coup sûr (trop longue pour le budget de temps) ne bloque
    plus les suivantes, et tout est retenté dès que le service revient. Après quelques échecs
    dans le même passage, on s'arrête : le service est probablement indisponible.
    """
    if not dem_service.is_enabled(cfg):
        return 0
    limit = limit if limit is not None else int(cfg.get('DEM_BACKFILL_BATCH', 25))
    corrected = failures = 0
    for row in activity_store.pending_dem(db, limit, last=sorted(_dem_failed)):
        _dem_failed.discard(row['id'])
        try:
            recompute_elevation(db, cfg, row['user_id'], row['id'])
            corrected += 1
        except dem_service.DemCoverageError:
            activity_store.set_dem_status(db, row['user_id'], row['id'], 'no_coverage')
        except dem_service.DemError as exc:
            _dem_failed.add(row['id'])
            failures += 1
            logger.warning("Rattrapage MNT en échec pour l'activité %s, replacée en fin de file : %s", row['id'], exc)
            if failures >= DEM_MAX_FAILURES_PER_PASS:
                logger.warning("Rattrapage MNT suspendu jusqu'au prochain cycle (%d échecs)", failures)
                break
        except LookupError:
            continue  # supprimée entre-temps
    if corrected:
        logger.info("Rattrapage MNT : %d activité(s) corrigée(s)", corrected)
    return corrected
