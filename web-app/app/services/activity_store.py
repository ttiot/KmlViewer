"""
Persistance des activités. Toutes les fonctions prennent un user_id et filtrent dessus :
c'est ici que se fait le cloisonnement entre utilisateurs.
"""

import json
import logging
import math
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from app.services import activity_types, file_formats

KINDS = ('activity', 'planned')   # réalisée / planifiée (RB-5.4) ; `kind` n'est jamais modifiable (EDITABLE_COLUMNS)
MAX_PENDING_LATE = 500  # borne la liste passée à pending_dem (nombre de variables SQL)

logger = logging.getLogger(__name__)

SUMMARY_COLUMNS = (
    "id, name, filename, source, activity_type, start_time, start_time_known, has_time, duration_s, moving_s, "
    "distance_m, ascent_m, descent_m, ascent_gps_m, descent_gps_m, elev_source, dem_status, max_speed_ms, "
    "best_efforts, created_at, kind"
)


def pack_points(lat, lon, ele, t, ele_dem=None, breaks=None) -> bytes:
    """
    Points d'une activité, compressés. `breaks` : indice du premier point de chaque trace du
    fichier, pour que rien ne soit mesuré d'une trace à l'autre lors d'un recalcul (absent
    des activités enregistrées avant son ajout : elles sont alors lues comme une seule trace).
    """
    payload = {
        'lat': [round(float(v), 6) for v in lat],
        'lon': [round(float(v), 6) for v in lon],
        'ele': [round(float(v), 1) for v in ele],
        't': [None if v is None else round(float(v), 3) for v in t],
        'ele_dem': None if ele_dem is None else [round(float(v), 1) for v in ele_dem],
        'breaks': [int(v) for v in breaks or []],
    }
    return zlib.compress(json.dumps(payload, separators=(',', ':')).encode('utf-8'), 6)


def unpack_points(blob: bytes) -> Dict[str, Any]:
    return json.loads(zlib.decompress(blob).decode('utf-8'))


def epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def raw_file_path(data_dir: str, user_id: int, sha256: str, ext: str) -> Path:
    """Chemin du fichier d'origine, construit uniquement à partir de valeurs maîtrisées."""
    if ext not in file_formats.SUPPORTED_EXTENSIONS or len(sha256) != 64 \
            or not all(c in '0123456789abcdef' for c in sha256):
        raise ValueError("Référence de fichier invalide")
    return Path(data_dir) / 'files' / str(int(user_id)) / f"{sha256}.{ext}"


def user_storage_bytes(data_dir: str, user_id: int) -> int:
    """Volume des fichiers d'origine conservés pour un utilisateur."""
    directory = Path(data_dir) / 'files' / str(int(user_id))
    try:
        with os.scandir(directory) as entries:
            return sum(entry.stat(follow_symlinks=False).st_size for entry in entries
                       if entry.is_file(follow_symlinks=False))
    except FileNotFoundError:
        return 0


def write_raw_file(path: Path, raw: bytes) -> None:
    """
    Écriture atomique : le fichier n'apparaît sous son nom qu'une fois complet. Un lecteur
    concurrent (ou un arrêt en cours d'écriture) ne voit jamais un fichier tronqué.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        os.replace(partial, path)
    except OSError:
        partial.unlink(missing_ok=True)
        raise


def remove_raw_file(path: Path) -> None:
    """Supprime un fichier d'origine ; son absence n'est pas une erreur."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Fichier d'origine %s non supprimé : %s", path.name, exc)


def find_by_hash(db: sqlite3.Connection, user_id: int, sha256: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT id FROM activities WHERE user_id = ? AND sha256 = ?",
                      (user_id, sha256)).fetchone()


def insert(db: sqlite3.Connection, user_id: int, values: Dict[str, Any]) -> int:
    values = {**values, 'user_id': user_id, 'best_efforts': json.dumps(values.get('best_efforts') or {})}
    columns = ', '.join(values)
    marks = ', '.join('?' for _ in values)
    cur = db.execute(f"INSERT INTO activities ({columns}) VALUES ({marks})", tuple(values.values()))
    return cur.lastrowid


def _finite(value: Any) -> Any:
    """None à la place d'un réel non fini : « Infinity » n'est pas du JSON, le navigateur le refuserait."""
    return None if isinstance(value, float) and not math.isfinite(value) else value


def to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = {k: _finite(row[k]) for k in row.keys() if k not in ('points', 'best_efforts', 'sha256', 'user_id')}
    data['has_time'] = bool(row['has_time'])
    if 'start_time_known' in data:
        data['start_time_known'] = bool(data['start_time_known'])
    try:
        efforts = json.loads(row['best_efforts'] or '{}')
    except ValueError:
        efforts = {}
    data['best_efforts'] = ({key: value for key, value in efforts.items() if _finite(value) is not None}
                            if isinstance(efforts, dict) else {})
    return data


def list_for_user(db: sqlite3.Connection, user_id: int, activity_type: Optional[str] = None,
                  limit: int = 100, offset: int = 0, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Bibliothèque : réalisées et planifiées (`kind` renvoyé) ; `kind` filtre sur l'une des deux (RB-5.5, défaut : toutes)."""
    if kind is not None and kind not in KINDS:
        raise ValueError("Nature de trace inconnue")
    clause, type_params = activity_types.sql_filter(activity_type)
    kind_clause = " AND kind = ?" if kind else ""
    sql = f"SELECT {SUMMARY_COLUMNS} FROM activities WHERE user_id = ?{clause}{kind_clause}"
    params: list = [user_id, *type_params, *([kind] if kind else [])]
    sql += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
    params += [max(1, min(int(limit), 500)), max(0, int(offset))]
    return [to_dict(r) for r in db.execute(sql, params).fetchall()]


def list_types(db: sqlite3.Connection, user_id: int) -> List[str]:
    # Types présents : activités réalisées seulement (RB-5.5), une trace planifiée n'en ajoute pas au filtre
    rows = db.execute("SELECT DISTINCT activity_type FROM activities WHERE user_id = ? AND kind = 'activity' "
                      "AND activity_type IS NOT NULL AND activity_type != '' ORDER BY 1", (user_id,))
    return [r[0] for r in rows.fetchall()]


def get(db: sqlite3.Connection, user_id: int, activity_id: int) -> Optional[sqlite3.Row]:
    """Ligne complète (points inclus) ou None si absente ou appartenant à un autre utilisateur."""
    return db.execute("SELECT * FROM activities WHERE id = ? AND user_id = ?",
                      (activity_id, user_id)).fetchone()


def count_for_user(db: sqlite3.Connection, user_id: int) -> int:
    """Activités réalisées ; les traces planifiées se comptent à part (count_planned_for_user)."""
    return db.execute("SELECT COUNT(*) FROM activities WHERE user_id = ? AND kind = 'activity'", (user_id,)).fetchone()[0]


def count_planned_for_user(db: sqlite3.Connection, user_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM activities WHERE user_id = ? AND kind = 'planned'", (user_id,)).fetchone()[0]


EDITABLE_COLUMNS = frozenset({'name', 'activity_type'})


def update_fields(db: sqlite3.Connection, user_id: int, activity_id: int, values: Dict[str, Any]) -> bool:
    """Modifie le nom et/ou le type. Les valeurs sont validées par l'appelant ; les colonnes, ici."""
    if not values or not set(values) <= EDITABLE_COLUMNS:
        raise ValueError("Colonnes non modifiables")
    assignments = ', '.join(f"{column} = ?" for column in values)
    cur = db.execute(f"UPDATE activities SET {assignments} WHERE id = ? AND user_id = ?",
                     (*values.values(), activity_id, user_id))
    db.commit()
    return cur.rowcount > 0


def delete(db: sqlite3.Connection, user_id: int, activity_id: int, data_dir: str) -> bool:
    row = db.execute("SELECT filename, sha256 FROM activities WHERE id = ? AND user_id = ?",
                     (activity_id, user_id)).fetchone()
    if row is None:
        return False
    try:
        # Le DELETE prend le verrou d'écriture, gardé jusqu'au commit : le fichier est supprimé
        # sous ce verrou. Un ré-import du même fichier écrit le sien sous le même verrou
        # (ingest_service) ; les deux ne peuvent donc pas se croiser et laisser une activité
        # sans fichier d'origine.
        cur = db.execute("DELETE FROM activities WHERE id = ? AND user_id = ?", (activity_id, user_id))
        if cur.rowcount == 0:
            db.rollback()
            return False  # supprimée entre-temps par une autre requête
        try:
            remove_raw_file(raw_file_path(data_dir, user_id, row['sha256'], file_formats.extension(row['filename'])))
        except ValueError:
            logger.warning("Référence de fichier invalide pour l'activité %s : fichier laissé en place", activity_id)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return True


def update_elevation(db: sqlite3.Connection, user_id: int, activity_id: int, *, points: bytes,
                     ascent_m: float, descent_m: float, elev_source: str) -> None:
    db.execute("UPDATE activities SET points = ?, ascent_m = ?, descent_m = ?, elev_source = ?, "
               "dem_status = 'ok' WHERE id = ? AND user_id = ?",
               (points, ascent_m, descent_m, elev_source, activity_id, user_id))
    # L'analyse enregistrée repose sur l'ancienne altitude : elle est supprimée dans la même transaction (« pas encore
    # lancée »), la prochaine demande la recalcule avec l'altitude MNT
    db.execute("DELETE FROM activity_analyses WHERE activity_id = ? AND EXISTS "
               "(SELECT 1 FROM activities WHERE id = ? AND user_id = ?)", (activity_id, activity_id, user_id))
    db.commit()


def update_summary(db: sqlite3.Connection, user_id: int, activity_id: int, values: Dict[str, Any]) -> None:
    """Met à jour les colonnes de résumé recalculées (noms de colonnes fournis par le code)."""
    values = {**values, 'best_efforts': json.dumps(values.get('best_efforts') or {})}
    assignments = ', '.join(f"{column} = ?" for column in values)
    db.execute(f"UPDATE activities SET {assignments} WHERE id = ? AND user_id = ?",
               (*values.values(), activity_id, user_id))


def set_dem_status(db: sqlite3.Connection, user_id: int, activity_id: int, status: str) -> None:
    db.execute("UPDATE activities SET dem_status = ? WHERE id = ? AND user_id = ?",
               (status, activity_id, user_id))
    db.commit()


def pending_dem(db: sqlite3.Connection, limit: int, last: Sequence[int] = ()) -> List[sqlite3.Row]:
    """
    Activités jamais passées au MNT, tous utilisateurs confondus (tâche de fond uniquement).
    `last` : activités à servir après les autres (échec récent), pour qu'elles ne tiennent
    pas la tête de la file.
    """
    late = [int(activity_id) for activity_id in last][:MAX_PENDING_LATE]
    order = f"CASE WHEN id IN ({','.join('?' * len(late))}) THEN 1 ELSE 0 END, id" if late else "id"
    return db.execute(f"SELECT id, user_id FROM activities WHERE dem_status IS NULL ORDER BY {order} LIMIT ?",
                      (*late, limit)).fetchall()
