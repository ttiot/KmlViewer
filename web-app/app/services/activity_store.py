"""
Persistance des activités. Toutes les fonctions prennent un user_id et filtrent dessus :
c'est ici que se fait le cloisonnement entre utilisateurs.
"""

import json
import os
import sqlite3
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SUMMARY_COLUMNS = (
    "id, name, filename, source, activity_type, start_time, has_time, duration_s, moving_s, "
    "distance_m, ascent_m, descent_m, ascent_gps_m, descent_gps_m, elev_source, dem_status, max_speed_ms, "
    "best_efforts, created_at"
)


def pack_points(lat, lon, ele, t, ele_dem=None) -> bytes:
    payload = {
        'lat': [round(float(v), 6) for v in lat],
        'lon': [round(float(v), 6) for v in lon],
        'ele': [round(float(v), 1) for v in ele],
        't': [None if v is None else round(float(v), 3) for v in t],
        'ele_dem': None if ele_dem is None else [round(float(v), 1) for v in ele_dem],
    }
    return zlib.compress(json.dumps(payload, separators=(',', ':')).encode('utf-8'), 6)


def unpack_points(blob: bytes) -> Dict[str, Any]:
    return json.loads(zlib.decompress(blob).decode('utf-8'))


def epoch_to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def raw_file_path(data_dir: str, user_id: int, sha256: str, ext: str) -> Path:
    """Chemin du fichier d'origine, construit uniquement à partir de valeurs maîtrisées."""
    if ext not in ('gpx', 'kml') or not all(c in '0123456789abcdef' for c in sha256):
        raise ValueError("Référence de fichier invalide")
    return Path(data_dir) / 'files' / str(int(user_id)) / f"{sha256}.{ext}"


def write_raw_file(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)


def find_by_hash(db: sqlite3.Connection, user_id: int, sha256: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT id FROM activities WHERE user_id = ? AND sha256 = ?",
                      (user_id, sha256)).fetchone()


def insert(db: sqlite3.Connection, user_id: int, values: Dict[str, Any]) -> int:
    values = {**values, 'user_id': user_id, 'best_efforts': json.dumps(values.get('best_efforts') or {})}
    columns = ', '.join(values)
    marks = ', '.join('?' for _ in values)
    cur = db.execute(f"INSERT INTO activities ({columns}) VALUES ({marks})", tuple(values.values()))
    return cur.lastrowid


def to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k not in ('points', 'best_efforts', 'sha256', 'user_id')}
    data['has_time'] = bool(row['has_time'])
    data['best_efforts'] = json.loads(row['best_efforts'] or '{}')
    return data


def list_for_user(db: sqlite3.Connection, user_id: int, activity_type: Optional[str] = None,
                  limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    sql = f"SELECT {SUMMARY_COLUMNS} FROM activities WHERE user_id = ?"
    params: list = [user_id]
    if activity_type:
        sql += " AND activity_type = ?"
        params.append(activity_type)
    sql += " ORDER BY start_time DESC LIMIT ? OFFSET ?"
    params += [max(1, min(int(limit), 500)), max(0, int(offset))]
    return [to_dict(r) for r in db.execute(sql, params).fetchall()]


def list_types(db: sqlite3.Connection, user_id: int) -> List[str]:
    rows = db.execute("SELECT DISTINCT activity_type FROM activities WHERE user_id = ? "
                      "AND activity_type IS NOT NULL AND activity_type != '' ORDER BY 1", (user_id,))
    return [r[0] for r in rows.fetchall()]


def get(db: sqlite3.Connection, user_id: int, activity_id: int) -> Optional[sqlite3.Row]:
    """Ligne complète (points inclus) ou None si absente ou appartenant à un autre utilisateur."""
    return db.execute("SELECT * FROM activities WHERE id = ? AND user_id = ?",
                      (activity_id, user_id)).fetchone()


def rename(db: sqlite3.Connection, user_id: int, activity_id: int, name: str) -> bool:
    cur = db.execute("UPDATE activities SET name = ? WHERE id = ? AND user_id = ?",
                     (name, activity_id, user_id))
    db.commit()
    return cur.rowcount > 0


def delete(db: sqlite3.Connection, user_id: int, activity_id: int, data_dir: str) -> bool:
    row = get(db, user_id, activity_id)
    if row is None:
        return False
    db.execute("DELETE FROM activities WHERE id = ? AND user_id = ?", (activity_id, user_id))
    db.commit()
    ext = row['filename'].rsplit('.', 1)[-1].lower()
    try:
        raw_file_path(data_dir, user_id, row['sha256'], ext).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass  # fichier orphelin sans conséquence, la ligne est supprimée
    return True


def update_elevation(db: sqlite3.Connection, user_id: int, activity_id: int, *, points: bytes,
                     ascent_m: float, descent_m: float, elev_source: str) -> None:
    db.execute("UPDATE activities SET points = ?, ascent_m = ?, descent_m = ?, elev_source = ?, "
               "dem_status = 'ok' WHERE id = ? AND user_id = ?",
               (points, ascent_m, descent_m, elev_source, activity_id, user_id))
    db.commit()


def set_dem_status(db: sqlite3.Connection, user_id: int, activity_id: int, status: str) -> None:
    db.execute("UPDATE activities SET dem_status = ? WHERE id = ? AND user_id = ?",
               (status, activity_id, user_id))
    db.commit()


def pending_dem(db: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    """Activités jamais passées au MNT, tous utilisateurs confondus (tâche de fond uniquement)."""
    return db.execute("SELECT id, user_id FROM activities WHERE dem_status IS NULL "
                      "ORDER BY id LIMIT ?", (limit,)).fetchall()
