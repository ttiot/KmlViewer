"""Agrégats du tableau de bord : volumes par période et records personnels."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PERIODS = ('week', 'month', 'year')
WINDOW = {'week': 12, 'month': 12}  # nombre de périodes affichées ; 'year' : tout l'historique
BEST_EFFORT_LABELS = {'1000': '1 km', '5000': '5 km', '10000': '10 km',
                      '21097': 'Semi-marathon', '42195': 'Marathon'}


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _bucket(day: date, period: str) -> str:
    if period == 'week':
        iso = day.isocalendar()
        return f"{iso[0]}-S{iso[1]:02d}"
    if period == 'month':
        return f"{day.year}-{day.month:02d}"
    return str(day.year)


def _recent_buckets(today: date, period: str, first: Optional[date]) -> List[str]:
    """Liste ordonnée des périodes à afficher, y compris celles sans activité."""
    if period == 'year':
        start = first.year if first else today.year
        return [str(y) for y in range(start, today.year + 1)]
    keys: List[str] = []
    cursor = today
    while len(keys) < WINDOW[period]:
        key = _bucket(cursor, period)
        if not keys or keys[-1] != key:
            keys.append(key)
        cursor -= timedelta(days=7 if period == 'week' else cursor.day)  # mois : veille du 1er
    return list(reversed(keys))


def _rows(db: sqlite3.Connection, user_id: int, activity_type: Optional[str], columns: str):
    sql = f"SELECT {columns} FROM activities WHERE user_id = ?"
    params: list = [user_id]
    if activity_type:
        sql += " AND activity_type = ?"
        params.append(activity_type)
    return db.execute(sql, params).fetchall()


def period_stats(db: sqlite3.Connection, user_id: int, period: str, tz_name: str,
                 activity_type: Optional[str] = None, today: Optional[date] = None) -> Dict[str, Any]:
    if period not in PERIODS:
        raise ValueError("Période invalide (week, month ou year)")
    tz = _timezone(tz_name)
    today = today or datetime.now(tz).date()
    rows = _rows(db, user_id, activity_type, "start_time, distance_m, ascent_m, moving_s, duration_s")

    empty = lambda: {'count': 0, 'distance_km': 0.0, 'ascent_m': 0.0, 'time_s': 0.0}  # noqa: E731
    totals, year_totals = empty(), empty()
    buckets: Dict[str, Dict[str, float]] = {}
    first_day: Optional[date] = None

    for row in rows:
        day = datetime.fromisoformat(row['start_time'].replace('Z', '+00:00')).astimezone(tz).date()
        first_day = day if first_day is None or day < first_day else first_day
        targets = [totals, buckets.setdefault(_bucket(day, period), empty())]
        if day.year == today.year:
            targets.append(year_totals)
        for target in targets:
            target['count'] += 1
            target['distance_km'] += row['distance_m'] / 1000
            target['ascent_m'] += row['ascent_m'] or 0
            target['time_s'] += row['moving_s'] or row['duration_s'] or 0

    def rounded(values):
        return {'count': values['count'], 'distance_km': round(values['distance_km'], 1),
                'ascent_m': round(values['ascent_m']), 'time_s': round(values['time_s'])}

    series = [{'period': key, **rounded(buckets.get(key, empty()))}
              for key in _recent_buckets(today, period, first_day)]
    return {'period': period, 'series': series, 'totals': rounded(totals),
            'current_year': {'year': today.year, **rounded(year_totals)}}


def records(db: sqlite3.Connection, user_id: int, activity_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Records personnels : plus longue sortie, plus gros D+, plus longue durée, meilleurs temps."""
    rows = _rows(db, user_id, activity_type,
                 "id, name, start_time, distance_m, ascent_m, moving_s, duration_s, best_efforts")
    if not rows:
        return []

    def entry(label, kind, value, row):
        return {'label': label, 'kind': kind, 'value': value, 'activity_id': row['id'],
                'activity_name': row['name'], 'date': row['start_time']}

    result = []
    longest = max(rows, key=lambda r: r['distance_m'])
    result.append(entry('Plus longue distance', 'distance', longest['distance_m'], longest))
    climb = max(rows, key=lambda r: r['ascent_m'] or 0)
    if climb['ascent_m']:
        result.append(entry('Plus gros dénivelé', 'ascent', climb['ascent_m'], climb))
    timed = [r for r in rows if r['moving_s'] or r['duration_s']]
    if timed:
        duration = max(timed, key=lambda r: r['moving_s'] or r['duration_s'])
        result.append(entry('Plus longue durée', 'time', duration['moving_s'] or duration['duration_s'], duration))

    for key, label in BEST_EFFORT_LABELS.items():
        best = None
        for row in rows:
            value = json.loads(row['best_efforts'] or '{}').get(key)
            if value and (best is None or value < best[0]):
                best = (value, row)
        if best:
            result.append(entry(f'Meilleur temps {label}', 'time', best[0], best[1]))
    return result
