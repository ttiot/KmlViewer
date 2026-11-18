"""API d'administration des comptes (admin uniquement, sauf /me)."""

import logging
import shutil
from pathlib import Path

from flask import current_app, g, jsonify, request

from app.api import bp
from app.auth import admin_required, users
from app.auth.guard import oidc_enabled
from app.db import get_db

logger = logging.getLogger(__name__)


def _error(message: str, status: int):
    return jsonify({'success': False, 'error': message}), status


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
        return _error('Utilisateur inconnu', 404)
    data = request.get_json(silent=True) or {}

    updates = {}
    for field in ('is_admin', 'is_active'):  # liste blanche : seuls ces champs sont modifiables
        if field in data:
            if not isinstance(data[field], bool):
                return _error(f'{field} doit être un booléen', 400)
            updates[field] = int(data[field])
    if not updates:
        return _error('Rien à modifier', 400)
    if user_id == g.user['id'] and 0 in updates.values():
        return _error('Impossible de retirer tes propres droits (risque de verrouillage)', 409)
    if 'is_admin' in updates and current_app.config.get('OIDC_ADMIN_ROLE'):
        return _error("Le rôle admin est piloté par l'IdP (OIDC_ADMIN_ROLE)", 409)

    assignments = ', '.join(f"{field} = ?" for field in updates)
    db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
    db.commit()
    logger.info("Utilisateur %s modifié par %s : %s", target['username'], g.user['username'], updates)
    return jsonify({'success': True, 'user': users.to_dict(users.get_by_id(db, user_id))})


@bp.route('/users/<int:user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id):
    if user_id == g.user['id']:
        return _error('Impossible de supprimer ton propre compte', 409)
    db = get_db()
    target = users.get_by_id(db, user_id)
    if target is None:
        return _error('Utilisateur inconnu', 404)
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascade : activités, tronçons, efforts
    db.commit()
    shutil.rmtree(Path(current_app.config['DATA_DIR']) / 'files' / str(int(user_id)), ignore_errors=True)
    logger.info("Utilisateur %s supprimé par %s, données incluses", target['username'], g.user['username'])
    return jsonify({'success': True})
