"""Persistance des comptes utilisateurs."""

import hmac
import logging
import secrets
import sqlite3
from typing import List, Mapping, Optional

from app.auth import emails

logger = logging.getLogger(__name__)

LOCAL_SUB = 'local'
PASSWORD_SUB_PREFIX = 'pwd:'      # espace des `sub` des comptes à mot de passe (EF-1.3)
_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


def get_by_id(db: sqlite3.Connection, user_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _new_session_token() -> str:
    return secrets.token_urlsafe(24)


def session_token(db: sqlite3.Connection, user_id: int) -> str:
    """
    Jeton à déposer dans la session à la connexion. Il n'est pas renouvelé à chaque login
    (se connecter sur un second appareil ne déconnecte pas le premier), seulement créé s'il
    manque : compte antérieur à la colonne.
    """
    row = db.execute("SELECT session_token FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError("Compte introuvable")
    if row['session_token']:
        return row['session_token']
    return revoke_sessions(db, user_id)


def revoke_sessions(db: sqlite3.Connection, user_id: int) -> str:
    """Change le jeton du compte : toutes ses sessions ouvertes sont refusées dès la requête suivante."""
    token = _new_session_token()
    db.execute("UPDATE users SET session_token = ? WHERE id = ?", (token, user_id))
    db.commit()
    return token


def owns_session(user: sqlite3.Row, token) -> bool:
    """Vrai si le jeton porté par la session est celui du compte (jamais vrai pour un jeton vide)."""
    expected = user['session_token']
    return bool(expected) and isinstance(token, str) and hmac.compare_digest(expected, token)


ADMIN_NEEDS_FACTOR = ("Ce compte à mot de passe n'a pas de second facteur actif : il doit d'abord en enrôler un "
                      "(promotion refusée).")


class AdminPromotionRefused(ValueError):
    """Promotion refusée par l'invariant B5 : un mot de passe seul ne donne jamais l'admin."""


def set_admin(db: sqlite3.Connection, user_id: int, make_admin: bool, *, actor, activate: bool = False, ip=None) -> sqlite3.Row:
    """
    SEUL chemin d'écriture de `is_admin` pour la CLI et l'API (B5, CA-13) : un compte à mot de passe sans second
    facteur ACTIF ne devient jamais admin. Le contrôle est fait sous le verrou d'écriture, dans la transaction de
    la promotion : un retrait de facteur concurrent ne peut pas passer entre les deux. Tracé (`user.admin_grant` /
    `user.admin_revoke`). Lève LookupError (compte absent) ou AdminPromotionRefused. `activate` : active aussi le
    compte (désignation du premier admin).
    """
    from app import audit
    from app.auth import factors
    from app.db import write_transaction
    refused = False
    with write_transaction(db):
        target = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise LookupError("Compte introuvable")
        refused = make_admin and target['auth_method'] == 'password' and not factors.has_second_factor(db, user_id)
        if refused:
            # La trace du refus est validée (la transaction se termine normalement) ; l'erreur part après
            audit.record(db, 'user.admin_refused', actor=actor, target_type='user', target_id=user_id, ip=ip,
                         details={'reason': 'second facteur absent'})
        elif make_admin:
            db.execute("UPDATE users SET is_admin = 1" + (", is_active = 1" if activate else "") + " WHERE id = ?", (user_id,))
        else:
            db.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
        if not refused:
            audit.record(db, 'user.admin_grant' if make_admin else 'user.admin_revoke', actor=actor, target_type='user',
                         target_id=user_id, ip=ip)
    if refused:
        raise AdminPromotionRefused(ADMIN_NEEDS_FACTOR)
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_or_create_local_user(db: sqlite3.Connection) -> sqlite3.Row:
    """Compte unique du mode sans OIDC."""
    row = db.execute("SELECT * FROM users WHERE sub = ?", (LOCAL_SUB,)).fetchone()
    if row is None:
        db.execute(
            "INSERT OR IGNORE INTO users (sub, username, display_name, is_admin, auth_method) "
            "VALUES (?, 'local', 'Local', 1, 'none')", (LOCAL_SUB,))
        db.commit()
        row = db.execute("SELECT * FROM users WHERE sub = ?", (LOCAL_SUB,)).fetchone()
    return row


def match_login(db: sqlite3.Connection, login: str, active_only: bool = False) -> List[sqlite3.Row]:
    """
    Comptes désignés par un login (insensible à la casse). Un login contenant « @ » est
    comparé aux emails, sinon aux usernames, jamais aux deux : un compte dont le username
    serait l'email d'un autre ne peut pas se faire passer pour lui.
    """
    if '@' in login:
        # Adresse : forme canonique (NFKC + casefold) comparée à la valeur stockée telle quelle, sans lower() SQL (EF-4.10)
        wanted = emails.canonical_or_none(login)
        if wanted is None:
            return []
        clause, value = "email = ?", wanted
    else:
        clause, value = "lower(username) = lower(?)", login
    sql = f"SELECT * FROM users WHERE sub != ? AND {clause}"
    if active_only:
        sql += " AND is_active = 1"
    return db.execute(sql + " ORDER BY id", (LOCAL_SUB, value)).fetchall()


def find_by_login(db: sqlite3.Connection, login: str) -> Optional[sqlite3.Row]:
    """Compte actif désigné par un username ou un email ; None s'il est absent ou ambigu."""
    rows = match_login(db, login, active_only=True)
    if len(rows) > 1:
        logger.warning("Login %r ambigu (%d comptes) : aucun n'est retenu", login, len(rows))
        return None
    return rows[0] if rows else None


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
    - sinon : aucun compte ne devient admin par sa seule connexion. Le droit se donne dans
      l'appli (/admin/users) ou, pour le tout premier, par `flask user-admin <login>` :
      « le premier arrivé est admin » offrait l'instance à quiconque se connectait avant
      son propriétaire.
    """
    sub = claims.get('sub')
    if not sub or not isinstance(sub, str) or sub == LOCAL_SUB or sub.startswith(PASSWORD_SUB_PREFIX):
        # `pwd:` est l'espace des comptes à mot de passe : un IdP ne peut pas désigner l'un d'eux (EF-0.5)
        logger.warning("Connexion OIDC refusée : claim 'sub' absent ou réservé")
        raise ValueError("Claim 'sub' absent ou invalide dans le jeton OIDC")

    # Seul un email que l'IdP déclare vérifié identifie quelqu'un (il désigne un dossier de
    # l'inbox) : sans claim email_verified à true, il n'est pas retenu
    # Forme canonique (EF-4.10) : un compte SSO ne peut pas contourner l'unicité des adresses par la casse
    email = emails.canonical_or_none(claims.get('email')) if claims.get('email_verified') is True else None
    username = claims.get('preferred_username') or email or sub
    display_name = (claims.get('name')
                    or ' '.join(filter(None, [claims.get('given_name'), claims.get('family_name')]))
                    or username)
    role_admin = (admin_role in _claim_roles(claims)) if admin_role else None

    row = db.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
    if row is not None and row['auth_method'] != 'oidc':
        logger.warning("Connexion OIDC refusée : le compte #%s n'est pas un compte SSO", row['id'])
        raise ValueError("Ce compte n'est pas un compte SSO")
    if row is None:
        is_admin = bool(role_admin)
        is_active = auto_activate or is_admin
        db.execute(
            "INSERT INTO users (sub, email, username, display_name, is_admin, is_active, last_login, session_token) "
            f"VALUES (?, ?, ?, ?, ?, ?, {_NOW}, ?)",
            (sub, email, username, display_name, int(is_admin), int(is_active), _new_session_token()))
        logger.info("Nouvel utilisateur OIDC : %s (admin=%s, actif=%s)", username, is_admin, is_active)
    else:
        is_admin = role_admin if role_admin is not None else bool(row['is_admin'])
        db.execute(
            f"UPDATE users SET email = ?, username = ?, display_name = ?, is_admin = ?, last_login = {_NOW} "
            "WHERE id = ?", (email, username, display_name, int(is_admin), row['id']))
    db.commit()
    result = db.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
    if email and (row is None or row['email'] != email):
        _flag_email_collision(db, result)
    return result


def _flag_email_collision(db, user) -> None:
    """
    Un compte SSO prend une adresse déjà portée (ou en attente) ailleurs : tolérée côté SSO (EF-1.2, l'IdP ne se refuse pas),
    mais signalée : WARNING et audit `identity.collision` (une fois, quand l'adresse change ou à la création).
    """
    others = [r['id'] for r in db.execute(
        "SELECT id FROM users WHERE id != ? AND (email = ? OR pending_email = ?)", (user['id'], user['email'], user['email']))]
    if not others:
        return
    from app import audit
    logger.warning("Collision d'identité : le compte #%s partage son adresse avec %d autre(s) compte(s)", user['id'], len(others))
    audit.record(db, 'identity.collision', actor=audit.Actor.system(), target_type='user', target_id=user['id'],
                 details={'kind': 'adresse', 'accounts': [user['id'], *others][:20]})


def to_dict(row: sqlite3.Row) -> dict:
    return {
        'id': row['id'], 'email': row['email'], 'username': row['username'],
        'display_name': row['display_name'], 'created_at': row['created_at'],
        'last_login': row['last_login'],
        'is_admin': bool(row['is_admin']), 'is_active': bool(row['is_active']),
        'auth_method': row['auth_method'],
    }
