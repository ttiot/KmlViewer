"""Persistance des comptes utilisateurs."""

import logging
import sqlite3
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

LOCAL_SUB = 'local'
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def get_by_id(db: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_or_create_local_user(db: sqlite3.Connection) -> sqlite3.Row:
    """Compte unique du mode sans OIDC."""
    row = db.execute("SELECT * FROM users WHERE sub = ?", (LOCAL_SUB,)).fetchone()
    if row is None:
        db.execute(
            "INSERT OR IGNORE INTO users (sub, username, display_name, is_admin) "
            "VALUES (?, 'local', 'Local', 1)", (LOCAL_SUB,))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE sub = ?", (LOCAL_SUB,)).fetchone()
    return row


def find_by_login(db: sqlite3.Connection, login: str) -> Optional[sqlite3.Row]:
    """Recherche par username ou email (insensible à la casse), comptes actifs uniquement."""
    return db.execute(
        "SELECT * FROM users WHERE is_active = 1 AND (lower(username) = lower(?) OR lower(email) = lower(?)) "
        "ORDER BY id LIMIT 1", (login, login)).fetchone()


def _claim_roles(claims: Mapping) -> set:
    roles = claims.get('roles') or []
    if isinstance(roles, str):
        roles = roles.split()
    return {str(r) for r in roles}


def upsert_from_claims(db: sqlite3.Connection, claims: Mapping, *,
                       admin_role: str = '', auto_activate: bool = True) -> sqlite3.Row:
    """
    Crée ou met à jour un compte à partir des claims OIDC.

    - admin_role défini : le flag admin suit ce rôle (claim "roles") à chaque connexion.
    - sinon : le tout premier compte OIDC devient admin, les suivants sont gérés dans l'appli.
    """
    sub = claims.get('sub')
    if not sub or sub == LOCAL_SUB:
        raise ValueError("Claim 'sub' absent ou invalide dans le jeton OIDC")

    email = claims.get('email')
    username = claims.get('preferred_username') or email or sub
    display_name = (claims.get('name')
                    or ' '.join(filter(None, [claims.get('given_name'), claims.get('family_name')]))
                    or username)
    role_admin = (admin_role in _claim_roles(claims)) if admin_role else None

    row = db.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
    if row is None:
        nb_users = db.execute("SELECT COUNT(*) FROM users WHERE sub != ?", (LOCAL_SUB,)).fetchone()[0]
        is_admin = role_admin if role_admin is not None else nb_users == 0
        is_active = auto_activate or is_admin
        db.execute(
            "INSERT INTO users (sub, email, username, display_name, is_admin, is_active, last_login) "
            f"VALUES (?, ?, ?, ?, ?, ?, {_NOW})",
            (sub, email, username, display_name, int(is_admin), int(is_active)))
        logger.info("Nouvel utilisateur OIDC : %s (admin=%s, actif=%s)", username, is_admin, is_active)
    else:
        is_admin = role_admin if role_admin is not None else bool(row['is_admin'])
        db.execute(
            f"UPDATE users SET email = ?, username = ?, display_name = ?, is_admin = ?, last_login = {_NOW} "
            "WHERE id = ?", (email, username, display_name, int(is_admin), row['id']))
    db.commit()
    return db.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()


def to_dict(row: sqlite3.Row) -> dict:
    return {
        'id': row['id'], 'email': row['email'], 'username': row['username'],
        'display_name': row['display_name'], 'created_at': row['created_at'],
        'last_login': row['last_login'],
        'is_admin': bool(row['is_admin']), 'is_active': bool(row['is_active']),
    }
