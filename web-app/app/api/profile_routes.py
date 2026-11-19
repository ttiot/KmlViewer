"""API du profil : chacun ne lit et ne modifie que le sien (aucun identifiant dans l'URL)."""

import logging

from flask import jsonify

from app.api import bp
from app.api.payload import json_body
from app.auth import current_user_id
from app.db import get_db
from app.errors import ApiError
from app.services import profile_service

logger = logging.getLogger(__name__)


@bp.route('/me/profile')
def get_profile():
    return jsonify({'profile': profile_service.get(get_db(), current_user_id())})


@bp.route('/me/profile', methods=['PATCH'])
def update_profile():
    user_id = current_user_id()
    try:
        profile = profile_service.update(get_db(), user_id, json_body())
    except profile_service.ProfileError as exc:
        raise ApiError(str(exc)) from exc
    # Les valeurs (mesures corporelles) ne sont pas journalisées
    logger.info("Profil de l'utilisateur %s modifié", user_id)
    return jsonify({'success': True, 'profile': profile})
