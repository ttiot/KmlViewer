"""API des tronçons personnels."""

from flask import jsonify, request

from app.api import bp
from app.auth import current_user_id
from app.db import get_db
from app.services import segment_service

MAX_NAME_LENGTH = 100


def _not_found():
    return jsonify({'success': False, 'error': 'Tronçon introuvable'}), 404


@bp.route('/segments')
def list_segments():
    return jsonify({'segments': segment_service.list_for_user(get_db(), current_user_id())})


@bp.route('/segments', methods=['POST'])
def create_segment():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()
    indices = [data.get(k) for k in ('activity_id', 'start_idx', 'end_idx')]
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        return jsonify({'success': False, 'error': 'Nom du tronçon invalide'}), 400
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in indices):
        return jsonify({'success': False, 'error': 'activity_id, start_idx et end_idx doivent être des entiers'}), 400
    db, user_id = get_db(), current_user_id()
    try:
        segment_id = segment_service.create(db, user_id, name, *indices)
    except LookupError:
        return jsonify({'success': False, 'error': 'Activité introuvable'}), 404
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True, 'segment': segment_service.detail(db, user_id, segment_id)}), 201


@bp.route('/segments/<int:segment_id>')
def get_segment(segment_id):
    detail = segment_service.detail(get_db(), current_user_id(), segment_id)
    return jsonify({'segment': detail}) if detail else _not_found()


@bp.route('/segments/<int:segment_id>', methods=['DELETE'])
def delete_segment(segment_id):
    if not segment_service.delete(get_db(), current_user_id(), segment_id):
        return _not_found()
    return jsonify({'success': True})


@bp.route('/segments/<int:segment_id>/rescan', methods=['POST'])
def rescan_segment(segment_id):
    db, user_id = get_db(), current_user_id()
    if segment_service.get(db, user_id, segment_id) is None:
        return _not_found()
    segment_service.match_segment(db, user_id, segment_id)
    return jsonify({'success': True, 'segment': segment_service.detail(db, user_id, segment_id)})
