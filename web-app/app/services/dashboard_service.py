"""Agrégats du tableau de bord : volumes par période et records personnels."""

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services import activity_types

logger = logging.getLogger(__name__)

PERIODS = ('week', 'month', 'year')
WINDOW = {'week': 12, 'month': 12}  # nombre de périodes glissantes ; 'year' : les 12 mois de l'année affichée
MIN_YEAR, MAX_YEAR = 1970, 2100
BEST_EFFORT_LABELS = {'1000': '1 km', '5000': '5 km', '10000': '10 km',
                      '21097': 'Semi-marathon', '42195': 'Marathon'}


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _number(value: Any) -> float:
    """
    Valeur numérique d'une colonne, 0 si elle est absente ou non finie. L'ingestion refuse
    les valeurs non finies, mais une ligne plus ancienne peut en porter : une seule suffisait
    à faire échouer tout le tableau de bord (round(inf)) ou à produire un JSON invalide (Infinity).
    """
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0


def _local_day(start_time: Any, tz) -> Optional[date]:
    """Jour local d'une activité ; None si sa date stockée est illisible (la ligne est alors ignorée)."""
    try:
        return datetime.fromisoformat(str(start_time).replace('Z', '+00:00')).astimezone(tz).date()
    except (ValueError, OverflowError, OSError):
        return None


def _bucket(day: date, period: str) -> str:
    if period == 'week':
        iso = day.isocalendar()
        return f"{iso[0]}-S{iso[1]:02d}"
    if period == 'month':
        return f"{day.year}-{day.month:02d}"
    return str(day.year)


def _recent_buckets(today: date, period: str, shown_year: int) -> List[str]:
    """Liste ordonnée des périodes à afficher, y compris celles sans activité."""
    if period == 'year':
        return [f"{shown_year}-{month:02d}" for month in range(1, 13)]
    keys: List[str] = []
    cursor = today
    while len(keys) < WINDOW[period]:
        key = _bucket(cursor, period)
        if not keys or keys[-1] != key:
            keys.append(key)
        cursor -= timedelta(days=7 if period == 'week' else cursor.day)  # mois : veille du 1er
    return list(reversed(keys))


def _rows(db: sqlite3.Connection, user_id: int, activity_type: Optional[str], columns: str):
    clause, type_params = activity_types.sql_filter(activity_type)
    return db.execute(f"SELECT {columns} FROM activities WHERE user_id = ?{clause}",
                      [user_id, *type_params]).fetchall()


def period_stats(db: sqlite3.Connection, user_id: int, period: str, tz_name: str,
                 activity_type: Optional[str] = None, today: Optional[date] = None,
                 year: Optional[int] = None) -> Dict[str, Any]:
    """
    Volumes par période et totaux de l'année affichée.

    Année affichée : `year` si elle est demandée ; sinon l'année en cours, ou, quand elle
    est vide, la dernière année qui porte des données (un tableau de bord vide en janvier
    n'apprend rien). 'calendar_year' et 'latest_year' permettent à l'interface de le dire.
    Série : 12 semaines ou 12 mois glissants ; en 'year', les 12 mois de l'année affichée.
    """
    if period not in PERIODS:
        raise ValueError("Période invalide (week, month ou year)")
    tz = _timezone(tz_name)
    today = today or datetime.now(tz).date()
    rows = _rows(db, user_id, activity_type, "start_time, distance_m, ascent_m, moving_s, duration_s")

    dated = []
    for row in rows:
        day = _local_day(row['start_time'], tz)
        if day is None:
            logger.warning("Tableau de bord : date illisible %r, activité ignorée", row['start_time'])
            continue
        dated.append((day, row))

    years = {day.year for day, _ in dated}
    latest_year = max(years) if years else None
    if year is not None:
        shown_year = year
    else:
        shown_year = today.year if today.year in years or latest_year is None else latest_year

    empty = lambda: {'count': 0, 'distance_km': 0.0, 'ascent_m': 0.0, 'time_s': 0.0}  # noqa: E731
    totals, year_totals = empty(), empty()
    buckets: Dict[str, Dict[str, float]] = {}

    for day, row in dated:
        targets = [totals]
        if day.year == shown_year:
            targets.append(year_totals)
        if period != 'year' or day.year == shown_year:
            targets.append(buckets.setdefault(_bucket(day, 'month' if period == 'year' else period), empty()))
        for target in targets:
            target['count'] += 1
            target['distance_km'] += _number(row['distance_m']) / 1000
            target['ascent_m'] += _number(row['ascent_m'])
            target['time_s'] += _number(row['moving_s']) or _number(row['duration_s'])

    def rounded(values):
        return {'count': values['count'], 'distance_km': round(values['distance_km'], 1),
                'ascent_m': round(values['ascent_m']), 'time_s': round(values['time_s'])}

    series = [{'period': key, **rounded(buckets.get(key, empty()))}
              for key in _recent_buckets(today, period, shown_year)]
    return {'period': period, 'series': series, 'totals': rounded(totals),
            'current_year': {'year': shown_year, **rounded(year_totals)},
            'calendar_year': today.year, 'latest_year': latest_year}


def records(db: sqlite3.Connection, user_id: int, activity_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Records personnels : distance, D+, durée, vitesse de pointe, meilleurs temps de course à pied."""
    rows = _rows(db, user_id, activity_type,
                 "id, name, activity_type, start_time, distance_m, ascent_m, moving_s, duration_s, "
                 "max_speed_ms, best_efforts")
    if not rows:
        return []

    def entry(label, kind, value, row):
        return {'label': label, 'kind': kind, 'value': value, 'activity_id': row['id'],
                'activity_name': row['name'], 'date': row['start_time']}

    def elapsed(row) -> float:
        return _number(row['moving_s']) or _number(row['duration_s'])

    result = []
    longest = max(rows, key=lambda r: _number(r['distance_m']))
    result.append(entry('Plus longue distance', 'distance', _number(longest['distance_m']), longest))
    climb = max(rows, key=lambda r: _number(r['ascent_m']))
    if _number(climb['ascent_m']):
        result.append(entry('Plus gros dénivelé', 'ascent', _number(climb['ascent_m']), climb))
    timed = [r for r in rows if elapsed(r)]
    if timed:
        duration = max(timed, key=elapsed)
        result.append(entry('Plus longue durée', 'time', elapsed(duration), duration))
    fastest = max(rows, key=lambda r: _number(r['max_speed_ms']))
    if _number(fastest['max_speed_ms']):
        result.append(entry('Vitesse de pointe', 'speed', _number(fastest['max_speed_ms']), fastest))

    # Meilleurs temps sur distance : seulement pour la course à pied (un trajet en voiture
    # n'a pas de « meilleur temps sur 10 km »). Décodés une fois par activité.
    rows = [row for row in rows if row['activity_type'] in activity_types.RUNNING]
    efforts: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            decoded = json.loads(row['best_efforts'] or '{}')
        except ValueError:
            decoded = {}
        efforts[row['id']] = decoded if isinstance(decoded, dict) else {}

    for key, label in BEST_EFFORT_LABELS.items():
        best = None
        for row in rows:
            value = _number(efforts[row['id']].get(key))
            if value > 0 and (best is None or value < best[0]):
                best = (value, row)
        if best:
            result.append(entry(f'Meilleur temps {label}', 'best_effort', best[0], best[1]))
    return result
