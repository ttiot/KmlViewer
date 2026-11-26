"""
Accès SQLite : une connexion par requête Flask (g), et une connexion
autonome pour les traitements hors requête (watcher, CLI).
"""

import logging
import os
import re
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager

from flask import current_app, g

logger = logging.getLogger(__name__)

# AUTOINCREMENT : l'identifiant d'un compte supprimé n'est jamais réattribué. Sans lui, SQLite
# redonne au compte créé ensuite l'id du dernier supprimé, et tout ce qui désigne encore
# l'ancien compte (cookie de session, dossier de fichiers) désignerait le nouveau.
# session_token : jeton que doit présenter une session pour être acceptée (auth/guard.py) ;
# le changer ferme d'un coup toutes les sessions du compte.
_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sub           TEXT NOT NULL UNIQUE,
    email         TEXT,
    username      TEXT,
    display_name  TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login    TEXT,
    session_token TEXT NOT NULL DEFAULT '',
    -- Profil (app/services/profile_service.py) : tout est facultatif
    birth_date    TEXT,
    sex           TEXT,
    height_cm     INTEGER,
    weight_kg     REAL,
    hr_max        INTEGER,
    hr_rest       INTEGER,
    default_activity_type TEXT
);
"""
# Colonnes reprises telles quelles lors de la reconstruction de la table (migration 3)
_USERS_LEGACY_COLUMNS = "id, sub, email, username, display_name, is_admin, is_active, created_at, last_login"

SCHEMA = _USERS_TABLE.format(name='users') + """

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
    # Mode transactionnel figé : les transactions explicites du registre de migrations
    # (BEGIN IMMEDIATE) et le commit() des migrations positionnelles supposent le contrôle
    # « legacy » de sqlite3 ; avec autocommit=False (PEP 249) une transaction est toujours
    # ouverte et BEGIN échouerait. On ne dépend donc pas du défaut de la version de Python.
    conn = sqlite3.connect(db_path, timeout=30, autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
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


def _rebuild_users(conn: sqlite3.Connection) -> None:
    """
    Recrée la table users avec AUTOINCREMENT et session_token (SQLite ne sait pas modifier une
    clé primaire en place). Les lignes gardent leur id, donc les activités et tronçons restent
    rattachés. Les clés étrangères sont suspendues le temps de l'échange, sans quoi supprimer
    l'ancienne table emporterait par cascade les données de tous les comptes ; elles sont
    revérifiées avant de valider.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone()
    if row is None or 'AUTOINCREMENT' in row['sql'].upper():
        return
    conn.commit()  # PRAGMA foreign_keys est sans effet dans une transaction ouverte
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS users_new")
        conn.execute(_USERS_TABLE.format(name='users_new'))
        conn.execute(f"INSERT INTO users_new ({_USERS_LEGACY_COLUMNS}) SELECT {_USERS_LEGACY_COLUMNS} FROM users")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        orphans = conn.execute("PRAGMA foreign_key_check").fetchall()
        if orphans:
            raise sqlite3.IntegrityError(f"{len(orphans)} référence(s) orpheline(s) après reconstruction de users")
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Reconstruction de la table users annulée, base inchangée")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


_PROFILE_COLUMNS = (
    ('birth_date', 'TEXT'), ('sex', 'TEXT'), ('height_cm', 'INTEGER'), ('weight_kg', 'REAL'),
    ('hr_max', 'INTEGER'), ('hr_rest', 'INTEGER'), ('default_activity_type', 'TEXT'),
)


def _add_profile_columns(conn: sqlite3.Connection) -> None:
    for column, definition in _PROFILE_COLUMNS:
        _add_column(conn, 'users', column, definition)


def _normalize_activity_types(conn: sqlite3.Connection) -> None:
    """
    Ramène les types lus tels quels dans les fichiers (« running », « Cycling »…) aux clés du
    catalogue. Import local : le catalogue est un module pur, sans dépendance, et db.py ne
    doit rien importer des services au chargement.
    """
    from app.services.activity_types import from_raw
    rows = conn.execute("SELECT DISTINCT activity_type FROM activities WHERE activity_type IS NOT NULL").fetchall()
    for row in rows:
        normalized = from_raw(row['activity_type'])
        if normalized != row['activity_type']:
            conn.execute("UPDATE activities SET activity_type = ? WHERE activity_type = ?",
                         (normalized, row['activity_type']))


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """
    Transaction d'écriture. Si l'appelant en a déjà ouvert une (ex. le registre de migrations,
    ou un changement de réglage qui efface aussi des secrets), on y participe sans rien valider :
    c'est lui qui valide. Sinon BEGIN IMMEDIATE (verrou d'écriture pris d'emblée, pas de
    promotion de verrou en cours de route) puis commit, ou rollback si le corps échoue.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _audit_log_v2(conn: sqlite3.Connection) -> None:
    """
    0006 v2 - journal d'audit (admin-space EF-9, CA-24b). `actor_type` structurel : l'origine d'une action
    ne se lit jamais sur le libellé, et la base refuse une ligne `cli`/`system` qui porterait un compte.
    Remplace `0006_audit_log` (modifiée avant fusion : renommée, ADR-0012). Une base qui l'aurait déjà
    appliquée (image de branche) est reconstruite : la table n'est référencée par rien.
    """
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'").fetchone()
    old_shape = exists and 'actor_type' not in {r['name'] for r in conn.execute("PRAGMA table_info(audit_log)")}
    if old_shape:
        conn.execute("DROP INDEX IF EXISTS idx_audit_log_at")
        conn.execute("DROP INDEX IF EXISTS idx_audit_log_action")
        conn.execute("ALTER TABLE audit_log RENAME TO audit_log_v1")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            at           TEXT NOT NULL,
            actor_type   TEXT NOT NULL CHECK (actor_type IN ('user', 'cli', 'system')),
            actor_id     INTEGER,
            actor_label  TEXT NOT NULL DEFAULT '',
            action       TEXT NOT NULL,
            target_type  TEXT,
            target_id    TEXT,
            details      TEXT,
            ip           TEXT,
            CHECK ((actor_type = 'user') = (actor_id IS NOT NULL))
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_at ON audit_log (at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action, at)")
    if old_shape:
        # Anciennes lignes : un identifiant de compte => user ; « cli » sans compte => cli ; le reste => system
        conn.execute("""
            INSERT INTO audit_log (id, at, actor_type, actor_id, actor_label, action, target_type, target_id, details, ip)
            SELECT id, at,
                   CASE WHEN actor_id IS NOT NULL THEN 'user' WHEN actor_label = 'cli' THEN 'cli' ELSE 'system' END,
                   actor_id,
                   CASE WHEN actor_id IS NOT NULL THEN actor_label ELSE '' END,
                   action, target_type, target_id, details, ip
            FROM audit_log_v1""")
        conn.execute("DROP TABLE audit_log_v1")


def _settings_tables(conn: sqlite3.Connection) -> None:
    """0007 - réglages administrables et leur version (admin-space EF-6.4, M10)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )""")
    conn.execute("CREATE TABLE IF NOT EXISTS settings_meta (id INTEGER PRIMARY KEY CHECK (id = 1), "
                 "version INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO settings_meta (id, version) VALUES (1, 1)")


def _secrets_table(conn: sqlite3.Connection) -> None:
    """0008 - secrets des services externes, chiffrés (admin-space EF-5, ADR-0003). `bound_to` : destination liée."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS secrets (
            name       TEXT PRIMARY KEY,
            ciphertext BLOB NOT NULL,
            last4      TEXT,
            bound_to   TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )""")


def _activities_kind(conn: sqlite3.Connection) -> None:
    """
    0011 - nature d'une trace (gpx-route-builder RB-5.4, ADR-0009) : `activity` (réalisée) ou `planned`
    (générée par le planificateur). Toute ligne existante devient `activity` : statistiques inchangées.
    La colonne et son index vivent dans la même migration (deux migrations dépendantes, même tâche).
    """
    _add_column(conn, 'activities', 'kind', "TEXT NOT NULL DEFAULT 'activity' CHECK (kind IN ('activity', 'planned'))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_user_kind_start ON activities (user_id, kind, start_time)")


def _users_auth_method(conn: sqlite3.Connection) -> None:
    """
    0009 - méthode d'authentification d'un compte (admin-space EF-1.3, EF-0.6) : `oidc` | `password` | `none`.
    Les comptes existants sont SSO (`oidc`), sauf le compte du mode local (`sub = 'local'`) : `none`.
    """
    _add_column(conn, 'users', 'auth_method', "TEXT NOT NULL DEFAULT 'oidc' CHECK (auth_method IN ('oidc', 'password', 'none'))")
    conn.execute("UPDATE users SET auth_method = 'none' WHERE sub = 'local' AND auth_method = 'oidc'")


def _feature_tables(conn: sqlite3.Connection) -> None:
    """0010 - fonctions bêta (admin-space EF-7) : état par fonction et liste d'utilisateurs, conservée entre états."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            key        TEXT PRIMARY KEY,
            state      TEXT NOT NULL CHECK (state IN ('off', 'users', 'all')),
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_users (
            feature_key TEXT NOT NULL,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (feature_key, user_id)
        )""")


def _auth_tokens(conn: sqlite3.Connection) -> None:
    """
    0012 - jetons à usage unique (admin-space §7, EF-3, EF-0.3) : invitation, amorçage, réinitialisation, validation
    d'adresse, définition de mot de passe. Seul le SHA-256 du jeton est stocké : le jeton en clair n'existe que
    dans le lien montré une fois.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token_hash      TEXT NOT NULL UNIQUE,
            kind            TEXT NOT NULL CHECK (kind IN ('invite', 'bootstrap', 'reset', 'verify_email', 'set_password')),
            created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_user_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,
            email           TEXT,
            is_admin_grant  INTEGER NOT NULL DEFAULT 0 CHECK (is_admin_grant IN (0, 1)),
            initial_credits INTEGER NOT NULL DEFAULT 0 CHECK (initial_credits BETWEEN 0 AND 1000),
            note            TEXT,
            sent_by_mail    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            expires_at      TEXT NOT NULL,
            used_at         TEXT,
            used_by         INTEGER REFERENCES users(id) ON DELETE SET NULL,
            revoked_at      TEXT,
            CHECK (is_admin_grant = 0 OR kind = 'bootstrap')
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_target ON auth_tokens (target_user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_creator ON auth_tokens (created_by)")


def _users_password_accounts(conn: sqlite3.Connection) -> None:
    """
    0013 - comptes à mot de passe et invitations (admin-space §7, EF-1.2, EF-3.1, EF-4.3) : hash, adresse en attente
    et validée, crédits d'invitation, émetteur, et unicité PARTIELLE (comptes `password` seulement : un compte SSO
    ne peut pas être refusé à la connexion). Colonnes et index dépendants dans la même migration.
    """
    for column, definition in (
            ('password_hash', 'TEXT'), ('password_changed_at', 'TEXT'), ('pending_email', 'TEXT'),
            ('email_verified_at', 'TEXT'),
            ('invite_credits', 'INTEGER NOT NULL DEFAULT 0 CHECK (invite_credits BETWEEN 0 AND 1000)'),
            ('invited_by', 'INTEGER REFERENCES users(id) ON DELETE SET NULL')):
        _add_column(conn, 'users', column, definition)
    # Identifiant : ASCII [a-z0-9._-] (EF-1.2), lower() suffit. Adresse : forme canonique stockée (EF-4.10), comparée
    # telle quelle.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_password_username ON users (lower(username)) "
                 "WHERE auth_method = 'password'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_password_email ON users (email) "
                 "WHERE auth_method = 'password' AND email IS NOT NULL")


def _second_factors(conn: sqlite3.Connection) -> None:
    """
    0015 - second facteur TOTP et codes de secours (admin-space ADR-0004, T11) : `totp_secret` (Fernet, jamais en clair),
    `totp_last_step` (anti-rejeu atomique), `totp_enabled_at` (un secret non confirmé n'est PAS un facteur) ; codes de secours
    stockés par empreinte SHA-256, usage unique. Les clés WebAuthn arrivent avec T12 (migration propre).
    """
    for column, definition in (('totp_secret', 'BLOB'), ('totp_last_step', 'INTEGER'), ('totp_enabled_at', 'TEXT')):
        _add_column(conn, 'users', column, definition)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recovery_codes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash  TEXT NOT NULL,
            used_at    TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, code_hash)
        )""")


def _webauthn_credentials(conn: sqlite3.Connection) -> None:
    """
    0016 - clés de sécurité WebAuthn (admin-space ADR-0004, EF-2.1, EF-2.4 ; T12) : identifiant et clé publique de la clé,
    compteur de signatures (la régression d'un compteur non nul est refusée), transports, nom donné par l'utilisateur.
    Supprimées avec le compte (CASCADE). Aucune donnée secrète : la clé privée ne quitte jamais l'authentificateur.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS webauthn_credentials (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            credential_id BLOB NOT NULL UNIQUE,
            public_key    BLOB NOT NULL,
            sign_count    INTEGER NOT NULL DEFAULT 0,
            transports    TEXT,
            name          TEXT,
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_used_at  TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_webauthn_user ON webauthn_credentials (user_id)")


def _emails_canonical(conn: sqlite3.Connection) -> None:
    """
    0014 - adresses sous forme canonique (EF-4.10, §13 n°32) : `email` et `pending_email` de TOUS les comptes (SSO compris)
    deviennent `NFKC(strip).casefold()`. Idempotente. Si deux comptes se retrouvent avec la même adresse (ou qu'un index
    unique partiel refuse la réécriture), la migration N'ÉCHOUE PAS : la ligne reste telle quelle, WARNING et audit
    `identity.collision`.
    """
    from app.auth.emails import canonical_or_none
    collisions = set()
    for column in ('email', 'pending_email'):
        for row in conn.execute(f"SELECT id, {column} AS value FROM users WHERE {column} IS NOT NULL").fetchall():
            wanted = canonical_or_none(row['value'])
            if wanted is None or wanted == row['value']:
                continue
            try:
                conn.execute(f"UPDATE users SET {column} = ? WHERE id = ?", (wanted, row['id']))
            except sqlite3.IntegrityError:
                collisions.add(row['id'])           # un compte password porte déjà cette adresse : on ne réécrit pas
    for column in ('email', 'pending_email'):
        for row in conn.execute(f"SELECT {column} AS value, group_concat(id) AS ids FROM users WHERE {column} IS NOT NULL "
                                f"GROUP BY {column} HAVING COUNT(*) > 1").fetchall():
            collisions.update(int(i) for i in row['ids'].split(','))
    if collisions:
        logger.warning("Migration des adresses : %d compte(s) en collision d'adresse (identity.collision) : %s", len(collisions),
                       ', '.join(f"#{i}" for i in sorted(collisions)[:20]))
        from app import audit
        audit.record(conn, 'identity.collision', actor=audit.Actor.system(), target_type='user', target_id='migration',
                     details={'kind': 'adresse', 'accounts': sorted(collisions)[:50]})


def _coverage_polygon_only(conn: sqlite3.Connection) -> None:
    """
    0017 - un seul réglage de zone (lot Z1, admin-space EF-6.20, CA-87) : reprise de `routing.coverage_bbox` dans
    `routing.coverage_polygon`, puis suppression de la ligne du rectangle. Données seulement, aucun changement de schéma.
    Principe : la zone en vigueur avant est la zone en vigueur après (polygone valide non vide, sinon rectangle valide,
    sinon défaut). Idempotente : rejouée, elle ne trouve plus de rectangle. Aucune coordonnée dans les journaux ;
    valeurs avant / après dans l'audit, acteur `system`.
    """
    import json
    import math
    from datetime import datetime, timezone

    from app import audit

    polygon_key, bbox_key = 'routing.coverage_polygon', 'routing.coverage_bbox'
    rows = {r['key']: r['value'] for r in conn.execute(
        "SELECT key, value FROM settings WHERE key IN (?, ?)", (polygon_key, bbox_key))}
    if not rows:
        return

    def loads(text):
        try:
            return json.loads(text)
        except (ValueError, RecursionError):
            return text[:2048]                      # illisible : l'audit garde le texte brut tronqué à 2 Kio, jamais None

    def finite(number):
        try:
            return not isinstance(number, bool) and isinstance(number, (int, float)) and math.isfinite(number)
        except OverflowError:                       # entier démesuré : jamais converti en flottant
            return False

    # Copie FIGÉE des contrôles (EF-6.20 contrôles 1 à 5, et ancien type `rectangle`) : une migration publiée ne change plus
    # (ADR-0012). Elle n'importe ni le registre des réglages ni `zone`, dont l'évolution ne doit pas la modifier.
    def side_too_large(lon_min, lat_min, lon_max, lat_max):
        def haversine(lat1, lon1, lat2, lon2):
            p1, p2 = math.radians(lat1), math.radians(lat2)
            a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
            return 2 * 6_371_000.0 * math.asin(min(1.0, math.sqrt(a)))
        parallel = 0.0 if lat_min <= 0 <= lat_max else min(abs(lat_min), abs(lat_max))
        return max(haversine(parallel, lon_min, parallel, lon_max), haversine(lat_min, lon_min, lat_max, lon_min)) > 2_000_000.0

    def mercator(lon, lat):
        return float(lon), math.degrees(math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)))

    def orientation(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def in_segment(a, b, q):
        return min(a[0], b[0]) <= q[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= q[1] <= max(a[1], b[1])

    def segments_touch(a, b, c, d):
        o1, o2, o3, o4 = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
        if ((o1 > 0 > o2) or (o1 < 0 < o2)) and ((o3 > 0 > o4) or (o3 < 0 < o4)):
            return True
        return ((o1 == 0 and in_segment(a, b, c)) or (o2 == 0 and in_segment(a, b, d))
                or (o3 == 0 and in_segment(c, d, a)) or (o4 == 0 and in_segment(c, d, b)))

    def adjacent_overlap(a, b, c):
        u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        if u == (0.0, 0.0) or v == (0.0, 0.0):
            return True
        return orientation(a, b, c) == 0 and u[0] * v[0] + u[1] * v[1] < 0

    def is_simple(vertices):
        points = [mercator(lon, lat) for lon, lat in vertices]
        count = len(points)
        for e1 in range(count):
            for e2 in range(e1 + 1, count):
                if e2 == e1 + 1 or (e1 == 0 and e2 == count - 1):
                    first, second = (e1, e2) if e2 == e1 + 1 else (e2, e1)
                    bad = adjacent_overlap(points[first], points[(first + 1) % count], points[(second + 1) % count])
                else:
                    bad = segments_touch(points[e1], points[(e1 + 1) % count], points[e2], points[(e2 + 1) % count])
                if bad:
                    return False
        return True

    def valid_polygon(value):
        """Les sommets `[lon, lat]` en flottants si la valeur passe les contrôles 1 à 5, sinon None."""
        if not isinstance(value, list) or not 3 <= len(value) <= 50:
            return None
        if not all(isinstance(v, list) and len(v) == 2 and all(finite(n) for n in v) for v in value):
            return None
        if not all(-180 <= lon <= 180 and -85.0511 <= lat <= 85.0511 for lon, lat in value):
            return None
        vertices = [[float(lon), float(lat)] for lon, lat in value]
        lons, lats = [v[0] for v in vertices], [v[1] for v in vertices]
        if side_too_large(min(lons), min(lats), max(lons), max(lats)) or not is_simple(vertices):
            return None
        return vertices

    def valid_rectangle(value):                     # anciennes règles du type `rectangle` (RB-1.3, P-TR5-1)
        if not isinstance(value, list) or len(value) != 4 or not all(finite(n) for n in value):
            return None
        lon_min, lat_min, lon_max, lat_max = value
        if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
            return None
        lon_min, lat_min, lon_max, lat_max = (float(n) for n in value)
        return None if side_too_large(lon_min, lat_min, lon_max, lat_max) else [lon_min, lat_min, lon_max, lat_max]

    old_polygon = loads(rows[polygon_key]) if polygon_key in rows else None
    old_bbox = loads(rows[bbox_key]) if bbox_key in rows else None
    new_polygon = None                              # valeur à écrire ; None : rien à écrire
    delete_polygon = polygon_key in rows
    if valid_polygon(old_polygon) is not None:      # 1. polygone valide : gardé tel quel
        delete_polygon = False
    else:
        rectangle = valid_rectangle(old_bbox)
        if rectangle is not None and rectangle != [1.35, 48.83, 6.42, 51.55]:   # 2. rectangle personnalisé valide
            vertices = [[rectangle[0], rectangle[1]], [rectangle[2], rectangle[1]],
                        [rectangle[2], rectangle[3]], [rectangle[0], rectangle[3]]]
            if valid_polygon(vertices) is not None:
                new_polygon, delete_polygon = vertices, False
            else:
                logger.warning("routing.coverage_bbox hors de la projection de la carte : zone remise au défaut")
        # 3. sinon : la ligne du polygone est supprimée, la zone est le défaut

    changed = False
    migration = '0017_coverage_polygon_only'
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    actor = audit.Actor.system()
    if new_polygon is not None:
        conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, 'system') "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                     "updated_by = excluded.updated_by", (polygon_key, json.dumps(new_polygon), now))
        audit.record(conn, 'setting.update', actor=actor, target_type='setting', target_id=polygon_key,
                     details={'key': polygon_key, 'old': old_polygon, 'new': new_polygon, 'migration': migration})
        changed = True
    for key, old, drop in ((polygon_key, old_polygon, delete_polygon), (bbox_key, old_bbox, bbox_key in rows)):
        if drop:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            audit.record(conn, 'setting.reset', actor=actor, target_type='setting', target_id=key,
                         details={'keys': [key], 'old': {key: old}, 'migration': migration})
            changed = True
    if changed:
        conn.execute("INSERT INTO settings_meta (id, version) VALUES (1, 1) "
                     "ON CONFLICT(id) DO UPDATE SET version = version + 1")


def _routing_coverage_state(conn: sqlite3.Connection) -> None:
    """
    0018 - couverture pilotée par la zone (lot Z2, routing-zone-donnees ZD-2, ADR-0013) : table d'état à UNE ligne
    (zone active, régions, graphe, dalles de la tâche, statut unique, demande en cours, dernier échec, réveil du MNT,
    budget du jour) et table du catalogue Geofabrik (une ligne, remplacée en une transaction). Aucune donnée existante
    n'est touchée ; la ligne d'état naît « sources historiques » (`active_polygon` NULL), statut `idle`. Idempotente.

    `request_id` est le compteur des demandes : il ne revient jamais en arrière, la demande en cours est la dernière.
    `request_polygon` est la zone demandée au moment de la demande (JSON). Demande prise : `claim_id`, `claimed_at` et
    `claim_payload` (JSON : zone, emprise d'extraction, régions, dalles, marge de CETTE demande, reprise par `done`).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_coverage_state (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            active_polygon      TEXT,
            active_extract_bbox TEXT,
            active_regions      TEXT NOT NULL DEFAULT '[]',
            active_graph        TEXT,
            data_date           TEXT,
            task_tiles          TEXT NOT NULL DEFAULT '[]',
            status              TEXT NOT NULL DEFAULT 'idle'
                                CHECK (status IN ('idle', 'pending', 'building', 'starting', 'failed')),
            reason              TEXT CHECK (reason IS NULL OR reason IN ('zone', 'mensuel', 'activation')),
            status_at           TEXT,
            request_id          INTEGER NOT NULL DEFAULT 0,
            request_polygon     TEXT,
            request_reason      TEXT CHECK (request_reason IS NULL OR request_reason IN ('zone', 'mensuel', 'activation')),
            requested_at        TEXT,
            claimed_at          TEXT,
            claim_id            INTEGER,
            claim_payload       TEXT,
            last_failure_code   TEXT,
            catalog_at          TEXT,
            dem_wakeup          INTEGER NOT NULL DEFAULT 0 CHECK (dem_wakeup IN (0, 1)),
            budget_day          TEXT,
            budget_builds       INTEGER NOT NULL DEFAULT 0,
            budget_bytes        INTEGER NOT NULL DEFAULT 0,
            last_report_hash    TEXT,
            invalid_report_hash TEXT
        )""")
    conn.execute("INSERT OR IGNORE INTO routing_coverage_state (id) VALUES (1)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS routing_catalog (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            content   TEXT NOT NULL,
            sha256    TEXT NOT NULL,
            loaded_at TEXT NOT NULL
        )""")


# Migrations appliquées dans l'ordre aux bases existantes ; PRAGMA user_version retient la
# dernière passée. SCHEMA décrit l'état final de ces 5 migrations positionnelles : une base neuve le reçoit
# directement, puis chacune est un no-op. Les tables ajoutées depuis (ADR-0012) ne sont créées que par
# leur migration NOMMÉE, appliquée aussi aux bases neuves. Règle : on ajoute en fin de liste, on ne modifie jamais
# une migration déjà publiée.
MIGRATIONS = [
    # 1 - état de la correction d'altitude par MNT
    lambda conn: _add_column(conn, 'activities', 'dem_status', 'TEXT'),
    # 2 - distingue une vraie date de début d'une date d'import (fichier sans horodatage)
    lambda conn: _add_column(conn, 'activities', 'start_time_known', 'INTEGER NOT NULL DEFAULT 1'),
    # 3 - identifiants de compte jamais réattribués, jeton de session par compte
    _rebuild_users,
    # 4 - profil utilisateur (mesures facultatives, type par défaut à l'import)
    _add_profile_columns,
    # 5 - types d'activité ramenés à la liste fermée du catalogue
    _normalize_activity_types,
]


# Migrations nommées (ADR-0012), appliquées après les 5 positionnelles. Chaque entrée est
# (nom, fonction) ; la fonction reçoit la connexion DANS une transaction ouverte par le
# registre : elle ne fait ni commit ni rollback, et ne touche pas à PRAGMA foreign_keys.
# Règles (admin-space §7) :
#   - le nom est unique et STABLE (interface) ; le préfixe numérique n'est qu'un ordre de lecture ;
#   - « publiée » = fusionnée sur la branche principale : on ne la modifie plus, on en ajoute une ;
#   - modifiée AVANT fusion (donc peut-être déjà dans une image unstable-<branche>) : elle change
#     de nom (suffixe _v2…), pour être appliquée dans sa nouvelle version partout ;
#   - idempotente (_add_column, CREATE … IF NOT EXISTS) : une base « en avance » ou une reprise
#     après interruption ne doit pas échouer ;
#   - deux migrations dépendantes l'une de l'autre vivent dans la même tâche ;
#   - elle ne reconstruit pas de table : cela exige de suspendre les clés étrangères, donc de
#     sortir de la transaction (modèle _rebuild_users), ce que ce registre n'offre pas. Ajouter
#     une colonne ou un index suffit ; une reconstruction demande une décision à part ;
#   - elle reste courte : un second processus qui démarre attend le verrou d'écriture au plus
#     busy_timeout (30 s) puis échoue en « database is locked ». Une migration longue (index sur
#     une grosse table activities) demanderait de relancer ce démarrage ou d'allonger l'attente.
# L'ordre de cette liste est l'ordre d'application des migrations manquantes.
NAMED_MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection], None]]] = [
    ('0006_audit_log_v2', _audit_log_v2),
    ('0007_settings', _settings_tables),
    ('0008_secrets', _secrets_table),
    ('0009_users_auth_method', _users_auth_method),
    ('0010_feature_flags', _feature_tables),
    ('0011_activities_kind', _activities_kind),
    ('0012_auth_tokens', _auth_tokens),
    ('0013_users_password_accounts', _users_password_accounts),
    ('0014_emails_canonical', _emails_canonical),
    ('0015_second_factors', _second_factors),
    ('0016_webauthn_credentials', _webauthn_credentials),
    ('0017_coverage_polygon_only', _coverage_polygon_only),
    ('0018_routing_coverage_state', _routing_coverage_state),
]

_MIGRATION_NAME = re.compile(r'[a-z0-9][a-z0-9_]{0,63}')

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


def _check_registry(named) -> None:
    """Un registre mal formé (nom dupliqué ou invalide, fonction absente) est une erreur de code."""
    seen = set()
    for entry in named:
        name, func = entry
        if not isinstance(name, str) or not _MIGRATION_NAME.fullmatch(name):
            raise ValueError(f"Nom de migration invalide : {name!r}")
        if name in seen:
            raise ValueError(f"Nom de migration en double : {name!r}")
        if not callable(func):
            raise ValueError(f"Migration {name!r} : fonction manquante")
        seen.add(name)


def _apply_named(conn: sqlite3.Connection, name: str, func) -> bool:
    """
    Applique une migration nommée et l'enregistre dans la même transaction. BEGIN IMMEDIATE
    prend le verrou d'écriture avant de relire la table : deux processus qui démarrent
    ensemble ne l'appliquent pas deux fois. Retourne False si un autre l'a passée entre-temps.
    """
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone():
            conn.rollback()
            return False
        func(conn)
        if not conn.in_transaction:
            # Elle a validé (ou annulé) elle-même : l'atomicité avec l'enregistrement est perdue.
            # On n'enregistre rien ; la prochaine exécution la rejouera, d'où l'idempotence exigée.
            raise RuntimeError(f"La migration {name!r} a terminé sa transaction elle-même (commit/rollback interdits)")
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (name,))
        conn.commit()
    except BaseException:
        conn.rollback()
        logger.error("Migration %r annulée et non enregistrée ; si elle avait validé elle-même, "
                     "la base peut être partiellement modifiée", name)
        raise
    return True


def migrate(conn: sqlite3.Connection, named=None) -> int:
    """
    Applique les migrations manquantes : les positionnelles (PRAGMA user_version, 5 au plus),
    puis chaque migration nommée absente de schema_migrations, dans l'ordre de la liste, chacune
    dans sa transaction. Un nom enregistré mais inconnu du code (base passée par une image de
    branche) est signalé en WARNING sans rien annuler. `named` n'est injecté que par les tests.
    Retourne la version positionnelle atteinte.
    """
    named = NAMED_MIGRATIONS if named is None else named
    _check_registry(named)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        # Base passée par une image de branche antérieure au registre nommé (numérotation
        # positionnelle à la fusion) : on ne la modifie pas, mais on le dit
        logger.warning("Version de schéma positionnelle %d supérieure à celle du code (%d) : base "
                       "passée par une autre version de l'application ?", version, len(MIGRATIONS))
    for number, migration in enumerate(MIGRATIONS, start=1):
        if number > version:
            migration(conn)
            conn.execute(f"PRAGMA user_version = {int(number)}")
            conn.commit()
            logger.info("Migration de schéma %d appliquée", number)

    conn.execute(_MIGRATIONS_TABLE)
    conn.commit()
    applied = {row['name'] for row in conn.execute("SELECT name FROM schema_migrations")}
    unknown = sorted(applied - {name for name, _ in named})
    if unknown:
        logger.warning("Migrations enregistrées en base mais inconnues de cette version du code "
                       "(image de branche ? migration renommée ?) : %s", ', '.join(unknown))
    for name, func in named:
        if name not in applied and _apply_named(conn, name, func):
            logger.info("Migration nommée %r appliquée", name)
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
