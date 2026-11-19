"""API des tronçons personnels."""

import logging

from flask import jsonify

from app.api import bp
from app.api.payload import json_body, json_int, json_name
from app.auth import current_user_id
from app.db import get_db
from app.errors import ApiError
from app.services import segment_service

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 100


def _not_found() -> ApiError:
    return ApiError('Tronçon introuvable', 404)


@bp.route('/segments')
def list_segments():
    return jsonify({'segments': segment_service.list_for_user(get_db(), current_user_id())})


@bp.route('/segments', methods=['POST'])
def create_segment():
    data = json_body()
    name = json_name(data, MAX_NAME_LENGTH, 'Nom du tronçon invalide')
    activity_id, start_idx, end_idx = (json_int(data, key) for key in ('activity_id', 'start_idx', 'end_idx'))
    db, user_id = get_db(), current_user_id()
    try:
        segment_id = segment_service.create(db, user_id, name, activity_id, start_idx, end_idx)
    except LookupError as exc:
        raise ApiError('Activité introuvable', 404) from exc
    except ValueError as exc:
        logger.info("Tronçon refusé (activité %s, points %s à %s) : %s", activity_id, start_idx, end_idx, exc)
        raise ApiError(
            f'Tronçon invalide : le début doit précéder la fin, et sa longueur tenir entre '
            f'{segment_service.MIN_SEGMENT_M:.0f} m et {segment_service.MAX_SEGMENT_M / 1000:.0f} km') from exc
    return jsonify({'success': True, 'segment': segment_service.detail(db, user_id, segment_id)}), 201


@bp.route('/segments/<int:segment_id>')
def get_segment(segment_id):
    detail = segment_service.detail(get_db(), current_user_id(), segment_id)
    if detail is None:
        raise _not_found()
    return jsonify({'segment': detail})


@bp.route('/segments/<int:segment_id>', methods=['DELETE'])
def delete_segment(segment_id):
    if not segment_service.delete(get_db(), current_user_id(), segment_id):
        raise _not_found()
    return jsonify({'success': True})


@bp.route('/segments/<int:segment_id>/rescan', methods=['POST'])
def rescan_segment(segment_id):
    db, user_id = get_db(), current_user_id()
    if segment_service.get(db, user_id, segment_id) is None:
        raise _not_found()
    segment_service.match_segment(db, user_id, segment_id)
    return jsonify({'success': True, 'segment': segment_service.detail(db, user_id, segment_id)})
