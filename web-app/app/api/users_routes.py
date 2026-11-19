"""API d'administration des comptes (admin uniquement, sauf /me)."""

import logging
import shutil
from pathlib import Path

from flask import current_app, g, jsonify

from app.api import bp
from app.api.payload import json_body
from app.auth import admin_required, users
from app.auth.guard import oidc_enabled
from app.db import get_db
from app.errors import ApiError

logger = logging.getLogger(__name__)


def _unknown_user() -> ApiError:
    return ApiError('Utilisateur inconnu', 404)


@bp.route('/me')
def me():
    return jsonify({'user': users.to_dict(g.user), 'oidc': oidc_enabled()})


@bp.route('/users')
@admin_required
def list_users():
    rows = get_db().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM activities a WHERE a.user_id = u.id) AS nb_activities "
        "FROM users u ORDER BY u.created_at").fetchall()
    return jsonify({'users': [users.to_dict(r) | {'nb_activities': r['nb_activities']} for r in rows],
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

    assignments = ', '.join(f"{field} = ?" for field in updates)
    db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
    db.commit()
    if updates.get('is_active') == 0:
        # Un compte désactivé perd ses sessions : le réactiver ne ressuscite pas un vieux cookie
        users.revoke_sessions(db, user_id)
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
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascade : activités, tronçons, efforts
    db.commit()
    shutil.rmtree(Path(current_app.config['DATA_DIR']) / 'files' / str(int(user_id)), ignore_errors=True)
    logger.info("Utilisateur %s supprimé par %s, données incluses", target['username'], g.user['username'])
    return jsonify({'success': True})
