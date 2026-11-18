"""API de la bibliothèque d'activités (toujours restreinte à l'utilisateur courant)."""

import logging

from flask import current_app, jsonify, request

from app.api import bp
from app.auth import admin_required, current_user_id
from app.db import get_db
from app.services import activity_store, dem_service, inbox_watcher, ingest_service

logger = logging.getLogger(__name__)

MAX_FILES_PER_UPLOAD = 50
DEFAULT_TRACK_POINTS = 5000
MAX_TRACK_POINTS = 50000


def _not_found():
    return jsonify({'success': False, 'error': 'Activité introuvable'}), 404


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


@bp.route('/activities')
def list_activities():
    db, user_id = get_db(), current_user_id()
    return jsonify({
        'activities': activity_store.list_for_user(
            db, user_id, request.args.get('type') or None,
            _int_arg('limit', 100), _int_arg('offset', 0)),
        'types': activity_store.list_types(db, user_id),
        'dem_enabled': dem_service.is_enabled(current_app.config),
    })


@bp.route('/activities', methods=['POST'])
def import_activities():
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400
    if len(files) > MAX_FILES_PER_UPLOAD:
        return jsonify({'success': False, 'error': f'{MAX_FILES_PER_UPLOAD} fichiers maximum par envoi'}), 400

    db, user_id = get_db(), current_user_id()
    results = []
    for file in files:
        # Altitude corrigée en tâche de fond : la requête ne dépend pas du MNT
        result = ingest_service.ingest_bytes(db, current_app.config, user_id, file.read(),
                                             file.filename, 'upload', defer_elevation=True)
        results.append({'filename': file.filename, 'status': result.status,
                        'activity_id': result.activity_id, 'message': result.message})
    if any(r['status'] == 'created' for r in results):
        inbox_watcher.wake()
    return jsonify({'success': any(r['status'] != 'error' for r in results), 'results': results})


@bp.route('/activities/<int:activity_id>')
def get_activity(activity_id):
    row = activity_store.get(get_db(), current_user_id(), activity_id)
    return jsonify({'activity': activity_store.to_dict(row)}) if row else _not_found()


@bp.route('/activities/<int:activity_id>/track')
def get_activity_track(activity_id):
    """
    Tracé allégé (pour choisir un tronçon sur la carte). Au-delà de max_points, un point
    sur N est renvoyé ; 'indices' donne la position de chacun dans la trace complète.
    """
    row = activity_store.get(get_db(), current_user_id(), activity_id)
    if row is None:
        return _not_found()
    pts = activity_store.unpack_points(row['points'])
    total = len(pts['lat'])
    max_points = max(2, min(_int_arg('max_points', DEFAULT_TRACK_POINTS), MAX_TRACK_POINTS))
    step = max(1, -(-total // max_points))  # division entière arrondie au supérieur
    indices = list(range(0, total, step))
    if indices and indices[-1] != total - 1:
        indices.append(total - 1)  # la trace allégée finit là où finit la vraie
    return jsonify({'id': row['id'], 'name': row['name'], 'has_time': bool(row['has_time']),
                    'total_points': total, 'indices': indices,
                    'coordinates': [[pts['lat'][i], pts['lon'][i]] for i in indices]})


@bp.route('/activities/<int:activity_id>/parsed')
def get_activity_parsed(activity_id):
    """Fichier d'origine re-parsé, au même format que /api/upload, pour le visualiseur."""
    user_id = current_user_id()
    row = activity_store.get(get_db(), user_id, activity_id)
    if row is None:
        return _not_found()
    ext = ingest_service.file_extension(row['filename'])
    try:
        path = activity_store.raw_file_path(current_app.config['DATA_DIR'], user_id, row['sha256'], ext)
        parsed = ingest_service.parse_raw(path.read_bytes(), row['filename'])
    except (OSError, ValueError) as exc:
        logger.error("Fichier d'origine illisible pour l'activité %s : %s", activity_id, exc)
        return jsonify({'success': False, 'error': "Fichier d'origine indisponible"}), 500
    return jsonify({**parsed, 'filename': row['name']})


@bp.route('/activities/<int:activity_id>', methods=['PATCH'])
def rename_activity(activity_id):
    name = str((request.get_json(silent=True) or {}).get('name') or '').strip()
    if not 1 <= len(name) <= ingest_service.MAX_NAME_LENGTH:
        return jsonify({'success': False, 'error': 'Nom invalide'}), 400
    if not activity_store.rename(get_db(), current_user_id(), activity_id, name):
        return _not_found()
    return jsonify({'success': True})


@bp.route('/activities/<int:activity_id>', methods=['DELETE'])
def delete_activity(activity_id):
    if not activity_store.delete(get_db(), current_user_id(), activity_id, current_app.config['DATA_DIR']):
        return _not_found()
    return jsonify({'success': True})


@bp.route('/activities/<int:activity_id>/elevation', methods=['POST'])
def correct_elevation(activity_id):
    try:
        result = ingest_service.recompute_elevation(get_db(), current_app.config, current_user_id(), activity_id)
    except LookupError:
        return _not_found()
    except dem_service.DemError as exc:
        logger.warning("Correction d'altitude en échec (activité %s) : %s", activity_id, exc)
        return jsonify({'success': False, 'error': "Service d'altitude indisponible"}), 502
    return jsonify({'success': True, **result})


@bp.route('/inbox')
@admin_required
def inbox_status():
    """Derniers fichiers vus par le dossier surveillé (diagnostic, admin uniquement)."""
    rows = get_db().execute(
        "SELECT path, size, status, error, activity_id, processed_at FROM inbox_files "
        "ORDER BY processed_at DESC LIMIT 100").fetchall()
    return jsonify({'enabled': current_app.config['INBOX_ENABLED'], 'files': [dict(r) for r in rows]})
