"""
Invitations, crédits et création des comptes à mot de passe (admin-space §5.1, §5.3, §5.4 ; B2, R2, r7, r8).

  - Peut inviter : tout admin (sans limite) ; tout utilisateur ACTIF dont l'adresse est validée (`users.email` ne
    contient que des adresses validées, EF-4.4) et qui a au moins 1 crédit. Un non-admin consomme un crédit à la
    création (décrément atomique) ; le crédit n'est JAMAIS rendu (révocation, expiration, échec d'envoi).
  - Une invitation de l'UI ne donne jamais l'admin ; la note et les crédits initiaux sont réservés aux admins.
  - Adresse déjà portée ou visée : un admin reçoit un 409 explicite ; un non-admin reçoit la même réponse qu'une
    adresse libre (l'unicité est imposée à la VALIDATION de l'adresse, sans rien révéler à l'émetteur, EF-3.10).
  - Consommation : crée le compte (`auth_method = 'password'`, `pwd:<uuid4>`), adresse EN ATTENTE (jamais validée
    d'office, B2), actif, sans session (r8). Identifiant : format et réservés (EF-1.2, EF-1.2b), unicité parmi TOUS les
    comptes ; mot de passe : politique (EF-1.4). Le hachage a lieu AVANT la transaction (il est long et sous sémaphore) ;
    la consommation du jeton et la création du compte sont une seule transaction : un identifiant pris ne consomme pas
    le lien.
"""

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional

from app import audit
from app.auth import emails, factors, identifiers, passwords, tokens
from app.auth.users import _new_session_token
from app.db import write_transaction

logger = logging.getLogger(__name__)

MAX_CREDITS = tokens.MAX_CREDITS


class InvitationError(Exception):
    """Règle d'invitation violée. `status` : code HTTP conseillé ; le message est affichable."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message, self.status = message, status


class AccountError(Exception):
    """Création ou mise à jour de compte refusée. `code` stable, `status` HTTP conseillé."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def can_invite(user) -> bool:
    """Admin, ou compte actif à adresse validée avec au moins un crédit (EF-3.2)."""
    if not user['is_active']:
        return False
    return bool(user['is_admin']) or (bool(user['email']) and user['invite_credits'] >= 1)


def _holders_of(conn, wanted: str) -> bool:
    """Adresse (forme canonique) déjà portée, en attente sur un compte, ou visée par une invitation encore valable."""
    for row in conn.execute("SELECT email, pending_email FROM users WHERE email IS NOT NULL OR pending_email IS NOT NULL"):
        for stored in (row['email'], row['pending_email']):
            if stored and stored == wanted:
                return True
    return tokens.pending_for_email(conn, wanted)


def create(conn, issuer, email, *, note: Optional[str] = None, initial_credits: int = 0, ttl: Optional[timedelta] = None,
           ip: Optional[str] = None, now: Optional[datetime] = None) -> tokens.NewToken:
    """Crée une invitation (jeton `invite`). Retourne le jeton EN CLAIR : à montrer une fois à l'émetteur."""
    is_admin = bool(issuer['is_admin'])
    if not issuer['is_active'] or (not is_admin and not issuer['email']):
        raise InvitationError("Tu ne peux pas inviter (compte désactivé ou adresse non validée).", 403)
    if not is_admin and issuer['invite_credits'] < 1:
        raise InvitationError("Plus aucun crédit d'invitation.", 409)          # CA-2 : 409 pour 0 crédit, 403 pour non autorisé
    if not is_admin and (note or initial_credits):
        raise InvitationError("Seul un administrateur peut joindre une note ou des crédits.", 400)
    try:
        wanted = emails.validate_email(email)
    except ValueError:
        raise InvitationError("Adresse e-mail invalide.", 400) from None
    actor = audit.Actor.from_user(issuer)
    with write_transaction(conn):
        if is_admin and _holders_of(conn, wanted):
            raise InvitationError("Cette adresse est déjà portée par un compte ou visée par une invitation en attente.", 409)
        if not is_admin:
            # Décrément ATOMIQUE : deux créations concurrentes avec un seul crédit ne passent pas toutes les deux
            if not conn.execute("UPDATE users SET invite_credits = invite_credits - 1 WHERE id = ? AND invite_credits > 0",
                                (issuer['id'],)).rowcount:
                raise InvitationError("Plus aucun crédit d'invitation.", 409)
        try:
            return tokens.create(conn, 'invite', actor=actor, created_by=issuer['id'], email=wanted,
                                 note=note or None, initial_credits=initial_credits if is_admin else 0, ttl=ttl, ip=ip, now=now)
        except ValueError as exc:
            raise InvitationError(str(exc), 400) from None


def _status(row, now: str) -> str:
    if row['used_at']:
        return 'used'
    if row['revoked_at']:
        return 'revoked'
    return 'expired' if row['expires_at'] <= now else 'pending'


def list_for(conn, issuer, *, everyone: bool = False, now: Optional[datetime] = None) -> list:
    """Invitations de l'émetteur (ou toutes pour un admin). Ne contient jamais le jeton ni son hash."""
    moment = tokens._stamp(now)
    if everyone and not issuer['is_admin']:
        raise InvitationError("Réservé aux administrateurs.", 403)
    rows = conn.execute(
        "SELECT id, email, note, initial_credits, created_by, created_at, expires_at, used_at, used_by, revoked_at "
        "FROM auth_tokens WHERE kind = 'invite'" + ("" if everyone else " AND created_by = ?") + " ORDER BY id DESC LIMIT 500",
        () if everyone else (issuer['id'],)).fetchall()
    return [{**{k: r[k] for k in r.keys()}, 'status': _status(r, moment)} for r in rows]


def revoke(conn, issuer, token_id, *, ip: Optional[str] = None, now: Optional[datetime] = None) -> bool:
    """Révocation par l'émetteur (les siennes) ou un admin (toutes). Crédit NON rendu. False : rien à révoquer."""
    row = conn.execute("SELECT created_by FROM auth_tokens WHERE id = ? AND kind = 'invite'", (token_id,)).fetchone() \
        if isinstance(token_id, int) and not isinstance(token_id, bool) and 0 < token_id < 2 ** 63 else None
    if row is None or not (issuer['is_admin'] or row['created_by'] == issuer['id']):
        return False                       # inconnue ou d'un autre : même réponse
    return tokens.revoke(conn, token_id, actor=audit.Actor.from_user(issuer), ip=ip, now=now)


def accept(conn, token, username, password, *, ip: Optional[str] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """
    Consomme un jeton `invite` ou `bootstrap` et crée le compte. Retourne `{'id', 'username'}`, ou None si le jeton n'est pas
    valable (même réponse que partout). Lève AccountError (format ou identifiant indisponible : le jeton n'est alors PAS
    consommé), passwords.PolicyError, passwords.HashBusy.
    """
    found = tokens.peek(conn, token, now)
    if found is None or found[0] not in ('invite', 'bootstrap'):
        return None
    kind, target = found
    if target is not None:
        # Reprise d'amorçage d'un compte EXISTANT : autre formulaire (`confirm_password`), autre fonction (confirm_bootstrap)
        raise AccountError('wrong_form', "Ce lien demande le mot de passe actuel du compte.", 400)
    try:
        identifier = identifiers.check_identifier(username)
    except identifiers.IdentifierError as exc:
        raise AccountError('unavailable' if exc.unavailable else 'invalid_identifier', exc.message,
                           409 if exc.unavailable else 400) from None
    passwords.check_policy(password, identities=[identifier])          # PolicyError : le jeton n'est pas consommé
    hashed = passwords.hash_password(password)                           # HashBusy : idem ; hors transaction
    moment = tokens._stamp(now)
    try:
        with write_transaction(conn):
            info = tokens.consume(conn, token, kind=kind, now=now)
            if info is None:
                return None
            if info.email:
                passwords.check_policy(password, identities=[identifier, info.email])
            if conn.execute("SELECT 1 FROM users WHERE lower(username) = ?", (identifier,)).fetchone():
                raise AccountError('unavailable', identifiers.UNAVAILABLE_MESSAGE, 409)
            cursor = conn.execute(
                "INSERT INTO users (sub, username, display_name, email, pending_email, is_admin, is_active, auth_method, "
                "password_hash, password_changed_at, session_token, invite_credits, invited_by) "
                "VALUES (?, ?, ?, NULL, ?, 0, 1, 'password', ?, ?, ?, ?, ?)",
                (f"pwd:{uuid.uuid4()}", identifier, identifier, info.email, hashed, moment, _new_session_token(),
                 info.initial_credits, info.created_by))
            user_id = cursor.lastrowid
            conn.execute("UPDATE auth_tokens SET used_by = ? WHERE id = ?", (user_id, info.id))
            audit.record(conn, 'user.create', actor=audit.Actor.user(user_id, identifier), target_type='user',
                         target_id=user_id, ip=ip, details={'method': 'password', 'via': kind, 'invited_by': info.created_by})
    except sqlite3.IntegrityError:
        # Course sur l'identifiant (index unique partiel) : même réponse qu'un identifiant pris, jeton intact
        raise AccountError('unavailable', identifiers.UNAVAILABLE_MESSAGE, 409) from None
    account = {'id': user_id, 'username': identifier}
    if info.is_admin_grant:
        account['grant'] = True                 # amorçage : l'octroi d'admin part avec l'état d'enrôlement (EF-0.3)
    return account


def confirm_bootstrap(conn, token, password, *, verify, ip: Optional[str] = None,
                      now: Optional[datetime] = None) -> Optional[dict]:
    """
    Reprise d'amorçage d'un compte existant (EF-0.3b) : consomme le jeton si `verify(user)` confirme le mot de passe actuel
    (le limiteur et le sémaphore de hachage sont appliqués par l'appelant, avant). Le mot de passe déjà posé est CONSERVÉ.
    Retourne `{'id', 'username', 'grant': True}`, ou None : jeton non valable, compte désactivé / converti / devenu doté d'un
    facteur depuis l'émission (même réponse neutre que partout), ou mot de passe refusé (jeton non consommé).
    """
    found = tokens.peek(conn, token, now)
    if found is None or found[0] != 'bootstrap' or found[1] is None:
        return None
    target = found[1]
    user = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if (user is None or not user['is_active'] or user['auth_method'] != 'password'
            or factors.has_second_factor(conn, target) or not verify(user)):
        return None
    with write_transaction(conn):
        info = tokens.consume(conn, token, kind='bootstrap', target_user_id=target, used_by=target, now=now)
        if info is None:
            return None
        # REVÉRIFICATION sous le verrou d'écriture (TOCTOU) : ni état, ni mot de passe changés, ni facteur gagné
        fresh = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
        if (fresh is None or not fresh['is_active'] or fresh['auth_method'] != 'password'
                or fresh['password_hash'] != user['password_hash'] or factors.has_second_factor(conn, target)):
            conn.rollback()
            return None
        audit.record(conn, 'user.bootstrap_resumed', actor=audit.Actor.from_user(user), target_type='user', target_id=target,
                     ip=ip)
    return {'id': target, 'username': user['username'], 'grant': bool(info.is_admin_grant)}


def convert_account(conn, token, username, password, *, ip: Optional[str] = None,
                    now: Optional[datetime] = None) -> Optional[dict]:
    """
    Conversion SSO -> mot de passe (EF-0.5, M5), mode `password` : consomme un jeton `set_password`. UNE transaction : `sub`
    réécrit (l'IdP ne retrouve plus le compte), méthode `password`, identifiant CHOISI (EF-1.2, EF-1.2b), mot de passe, jeton
    de session renouvelé, `is_admin` remis à 0 (B5 : repromu après enrôlement d'un facteur). L'adresse du compte reste
    validée, sauf si un compte `password` la porte déjà : elle passe en attente (`email_pending`, tracé). Retourne le compte
    converti, ou None (jeton non valable, compte plus SSO ou désactivé : réponse neutre).
    """
    found = tokens.peek(conn, token, now)
    if found is None or found[0] != 'set_password' or found[1] is None:
        return None
    target = found[1]
    try:
        identifier = identifiers.check_identifier(username)
    except identifiers.IdentifierError as exc:
        raise AccountError('unavailable' if exc.unavailable else 'invalid_identifier', exc.message,
                           409 if exc.unavailable else 400) from None
    row = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if row is None:
        return None
    passwords.check_policy(password, identities=[identifier, row['email']])
    hashed = passwords.hash_password(password)
    moment = tokens._stamp(now)
    try:
        with write_transaction(conn):
            info = tokens.consume(conn, token, kind='set_password', target_user_id=target, used_by=target, now=now)
            row = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
            if info is None or row is None or row['auth_method'] != 'oidc' or not row['is_active']:
                return None
            if conn.execute("SELECT 1 FROM users WHERE lower(username) = ? AND id != ?", (identifier, target)).fetchone():
                raise AccountError('unavailable', identifiers.UNAVAILABLE_MESSAGE, 409)
            email, pending = row['email'], row['pending_email']
            moved = bool(email) and conn.execute(
                "SELECT 1 FROM users WHERE email = ? AND auth_method = 'password' AND id != ?",
                (email, target)).fetchone() is not None
            if moved:
                email, pending = None, row['email']
            conn.execute(
                "UPDATE users SET sub = ?, username = ?, auth_method = 'password', password_hash = ?, password_changed_at = ?, "
                "session_token = ?, is_admin = 0, email = ?, pending_email = ? WHERE id = ?",
                (f"pwd:{uuid.uuid4()}", identifier, hashed, moment, _new_session_token(), email, pending, target))
            audit.record(conn, 'user.convert', actor=audit.Actor.from_user(row), target_type='user', target_id=target, ip=ip,
                         details={'from': 'oidc', 'to': 'password', 'was_admin': bool(row['is_admin']), 'email_pending': moved})
    except sqlite3.IntegrityError:
        raise AccountError('unavailable', identifiers.UNAVAILABLE_MESSAGE, 409) from None
    return {'id': target, 'username': identifier, 'email_pending': moved}


def reset_bootstrap(conn, token, password, *, ip: Optional[str] = None, now: Optional[datetime] = None) -> Optional[dict]:
    """
    Reprise d'amorçage AVEC remplacement du mot de passe oublié (`user-invite --admin --account <login> --reset-password`,
    EF-0.3b, CA-54) : politique EF-1.4, mot de passe remplacé, sessions du compte révoquées, jeton consommé ; l'enrôlement suit
    (l'appelant crée l'état). Même réponse neutre que partout pour un jeton ou un compte non valable.
    """
    found = tokens.peek(conn, token, now)
    if found is None or found[0] != 'bootstrap' or found[1] is None or not tokens.is_reset_password(conn, token, now):
        return None
    target = found[1]
    user = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
    if user is None or not user['is_active'] or user['auth_method'] != 'password' or factors.has_second_factor(conn, target):
        return None
    passwords.check_policy(password, identities=[user['username'], user['email'], user['pending_email']])
    hashed = passwords.hash_password(password)
    with write_transaction(conn):
        info = tokens.consume(conn, token, kind='bootstrap', target_user_id=target, used_by=target, now=now)
        if info is None:
            return None
        fresh = conn.execute("SELECT * FROM users WHERE id = ?", (target,)).fetchone()
        if (fresh is None or not fresh['is_active'] or fresh['auth_method'] != 'password'
                or factors.has_second_factor(conn, target)):
            conn.rollback()
            return None
        conn.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, session_token = ? WHERE id = ?",
                     (hashed, tokens._stamp(now), _new_session_token(), target))
        audit.record(conn, 'user.bootstrap_password_reset', actor=audit.Actor.from_user(user), target_type='user',
                     target_id=target, ip=ip)
    return {'id': target, 'username': user['username'], 'grant': bool(info.is_admin_grant)}


def verify_email(conn, user, token, *, ip: Optional[str] = None, now: Optional[datetime] = None) -> str:
    """
    Valide l'adresse en attente de `user` (EF-4.3, EF-4.3b). Le jeton est consommé SEULEMENT s'il vise ce compte
    (`target_user_id`, dans l'UPDATE conditionnel) : sans la session du compte cible, rien n'est consommé. Retourne
    `ok`, `invalid` (jeton non valable, ou adresse en attente différente) ou `refused` (adresse déjà validée sur un
    autre compte : `pending_email` effacée, audit, message neutre côté appelant). Contrôle et écriture : une transaction.
    """
    moment = tokens._stamp(now)
    with write_transaction(conn):
        info = tokens.consume(conn, token, kind='verify_email', target_user_id=user['id'], used_by=user['id'], now=now)
        if info is None:
            return 'invalid'
        row = conn.execute("SELECT pending_email FROM users WHERE id = ?", (user['id'],)).fetchone()
        if row is None or not row['pending_email'] or row['pending_email'] != info.email:
            return 'invalid'                 # adresse changée depuis l'émission : ce lien ne vaut plus rien
        pending = row['pending_email']
        if conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (pending, user['id'])).fetchone():
            conn.execute("UPDATE users SET pending_email = NULL WHERE id = ?", (user['id'],))
            audit.record(conn, 'identity.verify_refused', actor=audit.Actor.from_user(user), target_type='user',
                         target_id=user['id'], ip=ip, details={'reason': 'adresse déjà validée sur un autre compte'})
            return 'refused'
        try:
            conn.execute("UPDATE users SET email = ?, pending_email = NULL, email_verified_at = ? WHERE id = ?",
                         (pending, moment, user['id']))
        except sqlite3.IntegrityError:        # course avec une autre validation de la même adresse (r7)
            conn.execute("UPDATE users SET pending_email = NULL WHERE id = ?", (user['id'],))
            return 'refused'
        audit.record(conn, 'user.email_verified', actor=audit.Actor.from_user(user), target_type='user',
                     target_id=user['id'], ip=ip)
    return 'ok'


def reset_password(conn, token, password, *, ip: Optional[str] = None, now: Optional[datetime] = None) -> Optional[int]:
    """
    Consomme un jeton `reset` et définit le mot de passe du compte visé (EF-1.8) ; ne retire PAS le second facteur ;
    ferme toutes les sessions du compte ; n'ouvre aucune session (EF-1.12). Retourne l'identifiant du compte, ou None.
    """
    found = tokens.peek(conn, token, now)
    if found is None or found[0] != 'reset' or found[1] is None:
        return None
    target = found[1]
    user = conn.execute("SELECT * FROM users WHERE id = ? AND auth_method = 'password'", (target,)).fetchone()
    if user is None:
        return None
    passwords.check_policy(password, identities=[user['username'], user['email'], user['pending_email']])
    hashed = passwords.hash_password(password)
    with write_transaction(conn):
        if tokens.consume(conn, token, kind='reset', target_user_id=target, used_by=target, now=now) is None:
            return None
        conn.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, session_token = ? WHERE id = ?",
                     (hashed, tokens._stamp(now), _new_session_token(), target))
        audit.record(conn, 'user.password_reset', actor=audit.Actor.from_user(user), target_type='user', target_id=target, ip=ip)
    return target
