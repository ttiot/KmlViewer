"""
Analyse avancée enregistrée d'une activité de la bibliothèque.

Le résultat est calculé à partir du fichier d'origine stocké (jamais de données envoyées
par le navigateur), puis conservé pour ne pas être recalculé à chaque ouverture. Comme
dans activity_store, chaque fonction prend un user_id : une analyse n'est lisible,
remplaçable ou supprimable que par le propriétaire de l'activité.
"""

import json
import logging
import sqlite3
import time
import zlib
from typing import Any, Dict, Mapping, Optional

from app.services import activity_store, advanced_analysis, file_formats, ingest_service

logger = logging.getLogger(__name__)

_OWNED = "EXISTS (SELECT 1 FROM activities a WHERE a.id = ? AND a.user_id = ?)"


def _payload(analysis: Dict[str, Any], computed_at: str) -> Dict[str, Any]:
    return {'analysis': analysis, 'computed_at': computed_at}


def load(db: sqlite3.Connection, user_id: int, activity_id: int) -> Optional[Dict[str, Any]]:
    """
    Analyse enregistrée, ou None s'il n'y en a pas ou si elle a été produite par une autre
    version de l'algorithme (elle sera alors remplacée au prochain calcul).
    """
    row = db.execute(
        f"SELECT version, computed_at, result FROM activity_analyses WHERE activity_id = ? AND {_OWNED}",
        (activity_id, activity_id, user_id)).fetchone()
    if row is None or row['version'] != advanced_analysis.ANALYSIS_VERSION:
        return None
    try:
        analysis = json.loads(zlib.decompress(row['result']).decode('utf-8'))
    except (zlib.error, ValueError) as exc:
        logger.warning("Analyse enregistrée illisible pour l'activité %s, ignorée : %s", activity_id, exc)
        return None
    return _payload(analysis, row['computed_at'])


def compute(db: sqlite3.Connection, cfg: Mapping, user_id: int, activity_id: int) -> Dict[str, Any]:
    """
    Calcule l'analyse à partir du fichier d'origine et l'enregistre (remplace l'ancienne).
    Lève LookupError si l'activité n'existe pas pour cet utilisateur, OSError ou ValueError
    si le fichier d'origine est illisible ou la trace trop volumineuse.
    """
    row = activity_store.get(db, user_id, activity_id)
    if row is None:
        raise LookupError("Activité introuvable")
    ext = file_formats.extension(row['filename'])
    path = activity_store.raw_file_path(cfg['DATA_DIR'], user_id, row['sha256'], ext)
    parsed = ingest_service.parse_raw(path.read_bytes(), row['filename'])
    if not parsed.get('success'):
        raise ValueError(str(parsed.get('error') or "Fichier d'origine illisible"))
    # Altitude corrigée par le MNT : l'analyse mesure alors le même dénivelé que la bibliothèque
    # (ignorée par l'analyse si elle n'est pas alignée point pour point sur les traces)
    elevation = activity_store.unpack_points(row['points']).get('ele_dem') if row['elev_source'] == 'dem' else None
    analysis = advanced_analysis.analyze(parsed.get('features') or [], parsed.get('points') or [],
                                         elevation=elevation)

    computed_at = activity_store.epoch_to_iso(time.time())
    blob = zlib.compress(json.dumps(analysis, separators=(',', ':')).encode('utf-8'), 6)
    # Garde de course (EA-5) : le résultat n'est enregistré que si l'altitude de l'activité est celle lue au départ (même
    # source, même statut, mêmes points). Une correction MNT survenue pendant le calcul l'a rendu périmé : il est rendu à
    # l'appelant sans être stocké, et la demande suivante recalcule. Le test et l'écriture sont une seule instruction.
    stored = db.execute(
        "INSERT INTO activity_analyses (activity_id, version, computed_at, result) "
        "SELECT ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM activities a WHERE a.id = ? AND a.user_id = ? "
        "AND a.elev_source = ? AND a.dem_status IS ? AND a.points = ?) "
        "ON CONFLICT(activity_id) DO UPDATE SET version = excluded.version, "
        "computed_at = excluded.computed_at, result = excluded.result",
        (activity_id, advanced_analysis.ANALYSIS_VERSION, computed_at, blob, activity_id, user_id,
         row['elev_source'], row['dem_status'], row['points'])).rowcount
    db.commit()
    if stored:
        logger.info("Analyse avancée enregistrée pour l'activité %s (%d octets)", activity_id, len(blob))
    else:
        logger.info("Analyse avancée de l'activité %s non enregistrée : l'altitude a changé pendant le calcul", activity_id)
    return _payload(analysis, computed_at)


def delete(db: sqlite3.Connection, user_id: int, activity_id: int) -> bool:
    """Supprime l'analyse enregistrée. Retourne False s'il n'y avait rien à supprimer."""
    cur = db.execute(f"DELETE FROM activity_analyses WHERE activity_id = ? AND {_OWNED}",
                     (activity_id, activity_id, user_id))
    db.commit()
    return cur.rowcount > 0

