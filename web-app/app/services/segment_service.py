"""
Tronçons personnels : un tronçon est une portion de trace de référence ; on le
retrouve dans les autres traces pour chronométrer chaque passage (« effort »).
"""

import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.services import activity_store, geo

logger = logging.getLogger(__name__)

TOLERANCE_M = 30.0        # écart latéral toléré entre la trace et le tronçon
SPACING_M = 15.0          # pas de rééchantillonnage du tronçon
MAX_OFF_TRACK_M = 150.0   # distance parcourue hors tolérance avant abandon
LOOKAHEAD_M = 60.0        # marge de progression le long du tronçon entre deux points
MIN_SEGMENT_M = 100.0
MAX_SEGMENT_M = 200_000.0
BBOX_MARGIN_DEG = 0.001   # ~100 m
# Points de trace examinés au plus, par point de la trace, pour chercher un tronçon. Une
# trace ordinaire en demande un à deux ; une trace qui oscille près du départ sans jamais
# finir le tronçon relançait un suivi complet à chaque passage (quadratique : 28 s pour
# 4 000 points). Au-delà du budget, la recherche s'arrête avec les efforts déjà trouvés.
FOLLOW_WORK_FACTOR = 4


def _follow(seg: np.ndarray, trk: np.ndarray, steps: np.ndarray, start: int) -> Tuple[Optional[int], int]:
    """
    Suit le tronçon à partir du point `start` de la trace. Retourne (indice du point
    d'arrivée, ou None si la trace quitte le tronçon avant la fin ; nombre de points examinés).
    """
    last = len(seg) - 1
    k = 0            # position atteinte sur le tronçon
    off = 0.0        # distance parcourue hors tolérance depuis le dernier point valide
    since = 0.0      # distance parcourue depuis la dernière progression
    for i in range(start, len(trk)):
        if i > start:
            since += steps[i - 1]
        hi = min(last, k + int(math.ceil((since + LOOKAHEAD_M) / SPACING_M)))
        d = np.hypot(seg[k:hi + 1, 0] - trk[i, 0], seg[k:hi + 1, 1] - trk[i, 1])
        m = int(np.argmin(d))
        if d[m] <= TOLERANCE_M:
            k += m
            off = since = 0.0
        else:
            off += steps[i - 1] if i > start else 0.0
            if off > MAX_OFF_TRACK_M:
                return None, i - start + 1
        if k == last:
            end = i
            dist_end = lambda idx: math.hypot(trk[idx, 0] - seg[last, 0], trk[idx, 1] - seg[last, 1])  # noqa: E731
            while end + 1 < len(trk) and dist_end(end + 1) < dist_end(end):
                end += 1
            return end, end - start + 1
    return None, len(trk) - start


def find_efforts(seg_lat: Sequence[float], seg_lon: Sequence[float],
                 lat: Sequence[float], lon: Sequence[float],
                 t: Sequence[float]) -> List[Tuple[int, int, float]]:
    """Tous les passages de la trace sur le tronçon : liste de (début, fin, durée en s)."""
    lat0, lon0 = seg_lat[0], seg_lon[0]
    seg = geo.resample_polyline(geo.to_local_xy(seg_lat, seg_lon, lat0, lon0), SPACING_M)
    trk = geo.to_local_xy(lat, lon, lat0, lon0)
    if len(seg) < 2 or len(trk) < 2:
        return []
    steps = np.hypot(*np.diff(trk, axis=0).T)
    d_start = np.hypot(trk[:, 0] - seg[0, 0], trk[:, 1] - seg[0, 1])
    near = d_start <= TOLERANCE_M

    efforts: List[Tuple[int, int, float]] = []
    i, n = 0, len(trk)
    budget = FOLLOW_WORK_FACTOR * n
    while i < n:
        if not near[i]:
            i += 1
            continue
        run_end = i
        while run_end + 1 < n and near[run_end + 1]:
            run_end += 1
        start = i + int(np.argmin(d_start[i:run_end + 1]))
        if budget <= 0:
            logger.warning("Recherche de tronçon interrompue : trace de %d points trop coûteuse à suivre "
                           "(%d effort(s) retenus)", n, len(efforts))
            break
        end, examined = _follow(seg, trk, steps, start)
        budget -= examined
        if end is not None and end > start and t[end] > t[start]:
            efforts.append((start, end, float(t[end] - t[start])))
            # Sur une boucle, l'arrivée peut être le départ du tour suivant
            i = end if end > run_end else run_end + 1
        else:
            i = run_end + 1
    return efforts


def _store_efforts(db: sqlite3.Connection, segment: sqlite3.Row, activity: sqlite3.Row) -> int:
    if not activity['has_time']:
        return 0
    pts = activity_store.unpack_points(activity['points'])
    seg = activity_store.unpack_points(segment['points'])
    found = find_efforts(seg['lat'], seg['lon'], pts['lat'], pts['lon'], pts['t'])
    for start, end, elapsed in found:
        db.execute(
            "INSERT OR IGNORE INTO efforts (segment_id, activity_id, start_idx, end_idx, start_time, elapsed_s) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (segment['id'], activity['id'], start, end,
             activity_store.epoch_to_iso(pts['t'][start]), round(elapsed, 1)))
    return len(found)


_BBOX_OVERLAP = ("{a}.min_lat <= {b}.max_lat + {m} AND {a}.max_lat >= {b}.min_lat - {m} AND "
                 "{a}.min_lon <= {b}.max_lon + {m} AND {a}.max_lon >= {b}.min_lon - {m}")


def match_activity(db: sqlite3.Connection, user_id: int, activity_id: int) -> int:
    """Cherche tous les tronçons de l'utilisateur dans une activité. Retourne le nombre d'efforts."""
    activity = activity_store.get(db, user_id, activity_id)
    if activity is None:
        return 0
    overlap = _BBOX_OVERLAP.format(a='s', b='a', m=BBOX_MARGIN_DEG)
    segments = db.execute(
        f"SELECT s.* FROM segments s JOIN activities a ON a.id = ? WHERE s.user_id = ? AND {overlap}",
        (activity_id, user_id)).fetchall()
    count = sum(_store_efforts(db, segment, activity) for segment in segments)
    db.commit()
    return count


def match_segment(db: sqlite3.Connection, user_id: int, segment_id: int) -> int:
    """Cherche un tronçon dans toutes les activités de l'utilisateur."""
    segment = get(db, user_id, segment_id)
    if segment is None:
        return 0
    overlap = _BBOX_OVERLAP.format(a='a', b='s', m=BBOX_MARGIN_DEG)
    ids = [r[0] for r in db.execute(
        f"SELECT a.id FROM activities a JOIN segments s ON s.id = ? "
        f"WHERE a.user_id = ? AND a.has_time = 1 AND {overlap}", (segment_id, user_id)).fetchall()]
    count = 0
    for activity_id in ids:  # une activité à la fois : mémoire bornée
        count += _store_efforts(db, segment, activity_store.get(db, user_id, activity_id))
    db.commit()
    logger.info("Tronçon %s : %d effort(s) dans %d activité(s) candidates", segment_id, count, len(ids))
    return count


def create(db: sqlite3.Connection, user_id: int, name: str, activity_id: int,
           start_idx: int, end_idx: int) -> int:
    """Crée un tronçon à partir d'une portion d'activité, puis le cherche partout. Lève ValueError."""
    activity = activity_store.get(db, user_id, activity_id)
    if activity is None:
        raise LookupError("Activité introuvable")
    pts = activity_store.unpack_points(activity['points'])
    n = len(pts['lat'])
    if not (0 <= start_idx < end_idx < n):
        raise ValueError("Indices de début et de fin invalides")
    lat, lon = pts['lat'][start_idx:end_idx + 1], pts['lon'][start_idx:end_idx + 1]
    distance = float(geo.cumulative_distance(np.asarray(lat), np.asarray(lon))[-1])
    if not MIN_SEGMENT_M <= distance <= MAX_SEGMENT_M:
        raise ValueError(f"Un tronçon doit mesurer entre {MIN_SEGMENT_M:.0f} m et {MAX_SEGMENT_M / 1000:.0f} km")

    cur = db.execute(
        "INSERT INTO segments (user_id, name, distance_m, min_lat, max_lat, min_lon, max_lon, points) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, round(distance, 1), min(lat), max(lat), min(lon), max(lon),
         activity_store.pack_points(lat, lon, [0.0] * len(lat), [None] * len(lat))))
    db.commit()
    match_segment(db, user_id, cur.lastrowid)
    return cur.lastrowid


def get(db: sqlite3.Connection, user_id: int, segment_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM segments WHERE id = ? AND user_id = ?",
                      (segment_id, user_id)).fetchone()


def delete(db: sqlite3.Connection, user_id: int, segment_id: int) -> bool:
    cur = db.execute("DELETE FROM segments WHERE id = ? AND user_id = ?", (segment_id, user_id))
    db.commit()
    return cur.rowcount > 0


def list_for_user(db: sqlite3.Connection, user_id: int) -> List[Dict[str, Any]]:
    rows = db.execute(
        "SELECT s.id, s.name, s.distance_m, s.created_at, COUNT(e.id) AS nb_efforts, "
        "MIN(e.elapsed_s) AS best_s, MAX(e.start_time) AS last_effort "
        "FROM segments s LEFT JOIN efforts e ON e.segment_id = s.id "
        "WHERE s.user_id = ? GROUP BY s.id ORDER BY s.name COLLATE NOCASE", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def detail(db: sqlite3.Connection, user_id: int, segment_id: int) -> Optional[Dict[str, Any]]:
    """Tronçon, son tracé et ses efforts classés du plus rapide au plus lent."""
    segment = get(db, user_id, segment_id)
    if segment is None:
        return None
    pts = activity_store.unpack_points(segment['points'])
    efforts = db.execute(
        "SELECT e.id, e.activity_id, e.start_time, e.elapsed_s, a.name AS activity_name "
        "FROM efforts e JOIN activities a ON a.id = e.activity_id "
        "WHERE e.segment_id = ? AND a.user_id = ? ORDER BY e.elapsed_s", (segment_id, user_id)).fetchall()
    return {
        'id': segment['id'], 'name': segment['name'], 'distance_m': segment['distance_m'],
        'created_at': segment['created_at'],
        'coordinates': list(zip(pts['lat'], pts['lon'])),
        'efforts': [dict(e) | {'rank': i + 1} for i, e in enumerate(efforts)],
    }
