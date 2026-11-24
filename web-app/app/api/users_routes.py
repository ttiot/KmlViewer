"""API d'administration des comptes (admin uniquement, sauf /me)."""

import logging
import shutil
from pathlib import Path

from flask import current_app, g, jsonify, request

from app.api import bp
from app.api.payload import json_body
from app.audit import Actor
from app.auth import admin_required, tokens, users
from app.auth.guard import oidc_enabled
from app.db import get_db, write_transaction
from app.errors import ApiError

logger = logging.getLogger(__name__)


def _unknown_user() -> ApiError:
    return ApiError('Utilisateur inconnu', 404)


@bp.route('/me')
def me():
    return jsonify({'user': users.to_dict(g.user), 'oidc': oidc_enabled(), 'auth_mode': current_app.config['AUTH_MODE']})


@bp.route('/users')
@admin_required
def list_users():
    rows = get_db().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM activities a WHERE a.user_id = u.id AND a.kind = 'activity') AS nb_activities, "
        "(SELECT COUNT(*) FROM activities a WHERE a.user_id = u.id AND a.kind = 'planned') AS nb_planned "
        "FROM users u ORDER BY u.created_at").fetchall()
    counts = ('nb_activities', 'nb_planned')
    return jsonify({'users': [users.to_dict(r) | {c: r[c] for c in counts} for r in rows],
                    'admin_managed_by_idp': bool(current_app.config.get('OIDC_ADMIN_ROLE'))})


@bp.route('/users/<int:user_id>', methods=['PATCH'])
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
    if not updates:
        raise ApiError('Rien à modifier')
    if user_id == g.user['id'] and 0 in updates.values():
        raise ApiError('Impossible de retirer tes propres droits (risque de verrouillage)', 409)
    if 'is_admin' in updates and current_app.config.get('OIDC_ADMIN_ROLE'):
        raise ApiError("Le rôle admin est piloté par l'IdP (OIDC_ADMIN_ROLE)", 409)

    # Tout dans UNE transaction (set_admin la rejoint). Un refus B5 est attrapé DANS la transaction pour que sa ligne d'audit
    # (`user.admin_refused`) soit validée avec elle ; rien d'autre n'est alors appliqué, et l'erreur 409 part après.
    refused = None
    with write_transaction(db):
        if 'is_admin' in updates:
            try:
                users.set_admin(db, user_id, bool(updates.pop('is_admin')), actor=Actor.from_user(g.user),
                                ip=request.remote_addr)
            except users.AdminPromotionRefused as exc:
                refused = exc
        if refused is None and updates:
            assignments = ', '.join(f"{field} = ?" for field in updates)
            db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
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
@admin_required
def delete_user(user_id):
    if user_id == g.user['id']:
        raise ApiError('Impossible de supprimer ton propre compte', 409)
    db = get_db()
    target = users.get_by_id(db, user_id)
    if target is None:
        raise _unknown_user()
    with write_transaction(db):
        # Avant la suppression : la clé étrangère de l'émetteur passerait à NULL et ses invitations ne seraient plus retrouvables
        tokens.revoke_pending_for_creator(db, user_id, actor=Actor.from_user(g.user))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascade : activités, tronçons, efforts
    current_app.extensions['pending'].destroy_for_user(user_id)
    shutil.rmtree(Path(current_app.config['DATA_DIR']) / 'files' / str(int(user_id)), ignore_errors=True)
    logger.info("Utilisateur %s supprimé par %s, données incluses", target['username'], g.user['username'])
    return jsonify({'success': True})
