"""API d'administration des comptes (admin uniquement, sauf /me)."""

import logging
import shutil
from pathlib import Path

from flask import current_app, g, jsonify, request

from app.api import bp
from app.api.payload import json_body
from app import audit
from app.audit import Actor
from app.auth import admin_required, second_factor, tokens, users
from app.auth.guard import oidc_enabled
from app.db import get_db, write_transaction
from app.errors import ApiError

logger = logging.getLogger(__name__)


def ensure_admin_remains(db, user_id, what: str) -> None:
    """
    EF-8.2 : à appeler DANS la transaction d'écriture (BEGIN IMMEDIATE : les écritures sont sérialisées, deux admins qui
    s'éliminent en même temps ne passent pas tous les deux). Lève 409 si `user_id` est un admin ACTIF et qu'aucun autre ne reste.
    """
    row = db.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is not None and row['is_admin'] and row['is_active'] and _last_active_admin(db, user_id):
        raise ApiError(f"Dernier administrateur actif : impossible de {what}.", 409)


def _last_active_admin(db, user_id) -> bool:
    """EF-8.2 : `user_id` est l'unique admin actif (jamais désactivé, rétrogradé ni supprimé par l'API)."""
    others = db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1 AND is_active = 1 AND id != ?", (user_id,)).fetchone()[0]
    return others == 0


def _unknown_user() -> ApiError:
    return ApiError('Utilisateur inconnu', 404)


@bp.route('/me')
def me():
    return jsonify({'user': users.to_dict(g.user), 'oidc': oidc_enabled(), 'auth_mode': current_app.config['AUTH_MODE']})


def _enriched(db, row) -> dict:
    """Ligne d'administration (EF-8.1) : méthode, adresse validée / en attente, facteurs, crédits, émetteur, invitations."""
    return users.to_dict(row) | {
        'pending_email': row['pending_email'], 'email_verified_at': row['email_verified_at'],
        'invite_credits': row['invite_credits'], 'invited_by': row['invited_by'],
        'factors': {'totp': bool(row['totp_enabled_at']), 'keys': _keys(db, row['id']),
                    'recovery_codes': second_factor.remaining_recovery(db, row['id'])},
        'pending_invitations': db.execute(
            "SELECT COUNT(*) FROM auth_tokens WHERE kind = 'invite' AND created_by = ? AND used_at IS NULL "
            "AND revoked_at IS NULL AND expires_at > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')", (row['id'],)).fetchone()[0]}


def _keys(db, user_id) -> int:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'webauthn_credentials'").fetchone():
        return 0
    return db.execute("SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (user_id,)).fetchone()[0]


@bp.route('/users')
@bp.route('/admin/users')
@admin_required
def list_users():
    db = get_db()
    rows = db.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM activities a WHERE a.user_id = u.id AND a.kind = 'activity') AS nb_activities, "
        "(SELECT COUNT(*) FROM activities a WHERE a.user_id = u.id AND a.kind = 'planned') AS nb_planned "
        "FROM users u ORDER BY u.created_at").fetchall()
    counts = ('nb_activities', 'nb_planned')
    return jsonify({'users': [_enriched(db, r) | {c: r[c] for c in counts} for r in rows],
                    'admin_managed_by_idp': bool(current_app.config.get('OIDC_ADMIN_ROLE')),
                    'collisions': identity_collisions(db)})


def identity_collisions(db) -> list:
    """EF-8.5 : comptes qui partagent (casse ignorée) un identifiant ou une adresse validée."""
    found = []
    for column in ('username', 'email'):
        for row in db.execute(f"SELECT lower({column}) AS value, group_concat(id) AS ids FROM users WHERE {column} IS NOT NULL "
                              f"GROUP BY lower({column}) HAVING COUNT(*) > 1"):
            found.append({'field': column, 'user_ids': sorted(int(i) for i in row['ids'].split(','))})
    return found


@bp.route('/users/<int:user_id>', methods=['PATCH'])
@bp.route('/admin/users/<int:user_id>', methods=['PATCH'])
@admin_required
def update_user(user_id):
    db = get_db()
    target = users.get_by_id(db, user_id)
    if target is None:
        raise _unknown_user()
    data = json_body()

    updates = {}
    for field in ('is_admin', 'is_active'):  # liste blanche : seuls ces champs sont modifiables
        if field in data:
            if not isinstance(data[field], bool):
                raise ApiError(f'{field} doit être un booléen')
            updates[field] = int(data[field])
    credits = None
    if 'invite_credits' in data:
        credits = data['invite_credits']
        if isinstance(credits, bool) or not isinstance(credits, int) or not 0 <= credits <= 1000:
            raise ApiError('invite_credits : entier de 0 à 1000')
    if not updates and credits is None:
        raise ApiError('Rien à modifier')
    if user_id == g.user['id'] and 0 in updates.values():
        raise ApiError('Impossible de retirer tes propres droits (risque de verrouillage)', 409)
    if 'is_admin' in updates and current_app.config.get('OIDC_ADMIN_ROLE'):
        raise ApiError("Le rôle admin est piloté par l'IdP (OIDC_ADMIN_ROLE)", 409)

    # Tout dans UNE transaction (set_admin la rejoint). Un refus B5 est attrapé DANS la transaction pour que sa ligne d'audit
    # (`user.admin_refused`) soit validée avec elle ; rien d'autre n'est alors appliqué, et l'erreur 409 part après.
    refused = None
    with write_transaction(db):
        if updates.get('is_active') == 0 or updates.get('is_admin') == 0:
            ensure_admin_remains(db, user_id, 'le désactiver ou le rétrograder')
        if 'is_admin' in updates:
            try:
                users.set_admin(db, user_id, bool(updates.pop('is_admin')), actor=Actor.from_user(g.user),
                                ip=request.remote_addr)
            except users.AdminPromotionRefused as exc:
                refused = exc
        if refused is None and credits is not None:
            # Valeur ABSOLUE, un seul UPDATE ; la valeur de l'admin l'emporte sur une invitation créée au même instant (P-T14-3)
            old = db.execute("SELECT invite_credits FROM users WHERE id = ?", (user_id,)).fetchone()[0]
            db.execute("UPDATE users SET invite_credits = ? WHERE id = ?", (credits, user_id))
            audit.record(db, 'user.credits_set', actor=Actor.from_user(g.user), target_type='user', target_id=user_id,
                         ip=request.remote_addr, details={'from': old, 'to': credits})
        if refused is None and updates:
            assignments = ', '.join(f"{field} = ?" for field in updates)
            db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
            if 'is_active' in updates and updates['is_active'] != target['is_active']:
                # E-4 : activation / désactivation tracées dans la même transaction (avant/après, aucun secret)
                audit.record(db, 'user.activated' if updates['is_active'] else 'user.deactivated', actor=Actor.from_user(g.user),
                             target_type='user', target_id=user_id, ip=request.remote_addr,
                             details={'username': target['username'],
                                      'is_active': {'from': target['is_active'], 'to': updates['is_active']}})
            if updates.get('is_active') == 0:
                # Émetteur désactivé : ses invitations en attente sont révoquées, crédit non rendu (EF-3.7)
                tokens.revoke_pending_for_creator(db, user_id, actor=Actor.from_user(g.user))
    if refused is not None:
        raise ApiError(str(refused), 409)
    if updates.get('is_active') == 0:
        # Un compte désactivé perd ses sessions : le réactiver ne ressuscite pas un vieux cookie
        users.revoke_sessions(db, user_id)
    current_app.extensions['pending'].destroy_for_user(user_id)       # désactivation, droits : plus d'état intermédiaire (T10-1)
    logger.info("Utilisateur %s modifié par %s : %s", target['username'], g.user['username'], updates)
    return jsonify({'success': True, 'user': users.to_dict(users.get_by_id(db, user_id))})


@bp.route('/users/<int:user_id>', methods=['DELETE'])
@bp.route('/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    if user_id == g.user['id']:
        raise ApiError('Impossible de supprimer ton propre compte', 409)
    db = get_db()
    target = users.get_by_id(db, user_id)
    if target is None:
        raise _unknown_user()
    with write_transaction(db):
        ensure_admin_remains(db, user_id, 'le supprimer')
        # Avant la suppression : la clé étrangère de l'émetteur passerait à NULL et ses invitations ne seraient plus retrouvables
        tokens.revoke_pending_for_creator(db, user_id, actor=Actor.from_user(g.user))
        audit.record(db, 'user.deleted', actor=Actor.from_user(g.user), target_type='user', target_id=user_id,
                     ip=request.remote_addr, details={'username': target['username'], 'auth_method': target['auth_method'],
                                                      'is_admin': bool(target['is_admin']),
                                                      'is_active': bool(target['is_active'])})
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascade : activités, tronçons, efforts
    current_app.extensions['pending'].destroy_for_user(user_id)
    shutil.rmtree(Path(current_app.config['DATA_DIR']) / 'files' / str(int(user_id)), ignore_errors=True)
    logger.info("Utilisateur %s supprimé par %s, données incluses", target['username'], g.user['username'])
    return jsonify({'success': True})
