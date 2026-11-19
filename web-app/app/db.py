"""
Accès SQLite : une connexion par requête Flask (g), et une connexion
autonome pour les traitements hors requête (watcher, CLI).
"""

import logging
import os
import sqlite3

from flask import current_app, g

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    sub          TEXT NOT NULL UNIQUE,
    email        TEXT,
    username     TEXT,
    display_name TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login   TEXT
);

CREATE TABLE IF NOT EXISTS activities (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    source        TEXT NOT NULL,
    activity_type TEXT,
    start_time    TEXT NOT NULL,
    start_time_known INTEGER NOT NULL DEFAULT 1,  -- 0 : fichier sans date, start_time = date d'import
    has_time      INTEGER NOT NULL DEFAULT 0,
    duration_s    REAL,
    moving_s      REAL,
    distance_m    REAL NOT NULL DEFAULT 0,
    ascent_m      REAL NOT NULL DEFAULT 0,
    descent_m     REAL NOT NULL DEFAULT 0,
    ascent_gps_m  REAL NOT NULL DEFAULT 0,
    descent_gps_m REAL NOT NULL DEFAULT 0,
    elev_source   TEXT NOT NULL DEFAULT 'gps',
    dem_status    TEXT,  -- NULL : jamais tenté ; 'ok' ; 'no_coverage' : hors dalles disponibles
    max_speed_ms  REAL,
    min_lat REAL, max_lat REAL, min_lon REAL, max_lon REAL,
    best_efforts  TEXT NOT NULL DEFAULT '{}',
    points        BLOB NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (user_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_activities_user_start ON activities (user_id, start_time);

CREATE TABLE IF NOT EXISTS inbox_files (
    path         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    status       TEXT NOT NULL,
    error        TEXT,
    activity_id  INTEGER REFERENCES activities(id) ON DELETE SET NULL,
    processed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS segments (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    distance_m REAL NOT NULL,
    min_lat REAL, max_lat REAL, min_lon REAL, max_lon REAL,
    points     BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS efforts (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    start_idx   INTEGER NOT NULL,
    end_idx     INTEGER NOT NULL,
    start_time  TEXT NOT NULL,
    elapsed_s   REAL NOT NULL,
    UNIQUE (segment_id, activity_id, start_idx)
);
CREATE INDEX IF NOT EXISTS idx_efforts_segment ON efforts (segment_id, elapsed_s);

-- Résultat enregistré de l'analyse avancée d'une activité (un seul par activité)
CREATE TABLE IF NOT EXISTS activity_analyses (
    activity_id INTEGER PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    result      BLOB NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Ouvre une connexion SQLite configurée (WAL, clés étrangères)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """ALTER TABLE ADD COLUMN idempotent (les noms viennent du code, jamais d'une entrée)."""
    existing = {row['name'] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Migrations appliquées dans l'ordre aux bases existantes ; PRAGMA user_version retient la
# dernière passée. SCHEMA décrit toujours l'état final : une base neuve le reçoit directement,
# puis chaque migration est un no-op. Règle : on ajoute en fin de liste, on ne modifie jamais
# une migration déjà publiée.
MIGRATIONS = [
    # 1 - état de la correction d'altitude par MNT
    lambda conn: _add_column(conn, 'activities', 'dem_status', 'TEXT'),
    # 2 - distingue une vraie date de début d'une date d'import (fichier sans horodatage)
    lambda conn: _add_column(conn, 'activities', 'start_time_known', 'INTEGER NOT NULL DEFAULT 1'),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Applique les migrations manquantes. Retourne la version de schéma atteinte."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, migration in enumerate(MIGRATIONS, start=1):
        if number > version:
            migration(conn)
            conn.execute(f"PRAGMA user_version = {int(number)}")
            conn.commit()
            logger.info("Migration de schéma %d appliquée", number)
    return max(version, len(MIGRATIONS))


def init_db(db_path: str) -> None:
    """Crée le fichier et le schéma si nécessaire, puis migre une base existante (idempotent)."""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        version = migrate(conn)
    finally:
        conn.close()
    logger.info("Base SQLite prête : %s (schéma v%d)", db_path, version)


def get_db() -> sqlite3.Connection:
    """Connexion liée à la requête Flask courante."""
    if 'db' not in g:
        g.db = connect(current_app.config['DB_PATH'])
    return g.db


def close_db(_exc=None) -> None:
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()
