"""API de la bibliothèque d'activités (toujours restreinte à l'utilisateur courant)."""

import logging

from flask import current_app, jsonify, request

from app.api import bp
from app.api.payload import int_arg, json_body, json_name
from app.api.uploads import public_error, uploaded_files
from app.api.workload import heavy_slot
from app.auth import admin_required, current_user_id
from app.db import get_db
from app.errors import ApiError
from app.services import activity_store, analysis_store, dem_service, inbox_watcher, ingest_service

logger = logging.getLogger(__name__)

DEFAULT_TRACK_POINTS = 5000
MAX_TRACK_POINTS = 50000


def _not_found() -> ApiError:
    return ApiError('Activité introuvable', 404)


def _source_unavailable() -> ApiError:
    return ApiError("Fichier d'origine indisponible", 500)


@bp.route('/activities')
def list_activities():
    db, user_id = get_db(), current_user_id()
    return jsonify({
        'activities': activity_store.list_for_user(
            db, user_id, request.args.get('type') or None,
            int_arg('limit', 100), int_arg('offset', 0)),
        'types': activity_store.list_types(db, user_id),
        'dem_enabled': dem_service.is_enabled(current_app.config),
    })


@bp.route('/activities', methods=['POST'])
def import_activities():
    files = uploaded_files()
    db, user_id = get_db(), current_user_id()
    results = []
    busy = None  # message du refus une fois les traitements saturés : les fichiers restants ne sont pas tentés
    for file in files:
        if busy is None:
            try:
                # Une place par fichier : un envoi de cinquante fichiers ne monopolise pas les traitements
                with heavy_slot():
                    # Altitude corrigée en tâche de fond : la requête ne dépend pas du MNT
                    result = ingest_service.ingest_bytes(db, current_app.config, user_id, file.read(),
                                                         file.filename, 'upload', defer_elevation=True)
            except ApiError as exc:
                busy = exc.message
        if busy is not None:
            # Ce qui est déjà importé le reste : la réponse dit fichier par fichier ce qu'il en est
            results.append({'filename': file.filename, 'status': 'error', 'activity_id': None, 'message': busy})
            continue
        message = public_error(result.message, file.filename) if result.status == 'error' else result.message
        results.append({'filename': file.filename, 'status': result.status,
                        'activity_id': result.activity_id, 'message': message})
    if any(r['status'] == 'created' for r in results):
        inbox_watcher.wake()
    return jsonify({'success': any(r['status'] != 'error' for r in results), 'results': results})


@bp.route('/activities/<int:activity_id>')
def get_activity(activity_id):
    row = activity_store.get(get_db(), current_user_id(), activity_id)
    if row is None:
        raise _not_found()
    return jsonify({'activity': activity_store.to_dict(row)})


@bp.route('/activities/<int:activity_id>/track')
def get_activity_track(activity_id):
    """
    Tracé allégé (pour choisir un tronçon sur la carte). Au-delà de max_points, un point
    sur N est renvoyé ; 'indices' donne la position de chacun dans la trace complète.
    """
    row = activity_store.get(get_db(), current_user_id(), activity_id)
    if row is None:
        raise _not_found()
    pts = activity_store.unpack_points(row['points'])
    total = len(pts['lat'])
    max_points = max(2, int_arg('max_points', DEFAULT_TRACK_POINTS, MAX_TRACK_POINTS))
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
        raise _not_found()
    ext = ingest_service.file_extension(row['filename'])
    with heavy_slot():
        try:
            path = activity_store.raw_file_path(current_app.config['DATA_DIR'], user_id, row['sha256'], ext)
            parsed = ingest_service.parse_raw(path.read_bytes(), row['filename'])
        except (OSError, ValueError) as exc:
            logger.error("Fichier d'origine illisible pour l'activité %s : %s", activity_id, exc)
            raise _source_unavailable() from exc
        if not parsed.get('success'):
            # Le fichier a été accepté à l'import : s'il ne se relit plus, ce n'est pas la faute du client
            logger.error("Fichier d'origine de l'activité %s refusé par le parser : %s", activity_id, parsed.get('error'))
            raise _source_unavailable()
        return jsonify({**parsed, 'filename': row['name'], 'activity_id': row['id']})


@bp.route('/activities/<int:activity_id>', methods=['PATCH'])
def rename_activity(activity_id):
    name = json_name(json_body(), ingest_service.MAX_NAME_LENGTH, 'Nom invalide')
    if not activity_store.rename(get_db(), current_user_id(), activity_id, name):
        raise _not_found()
    return jsonify({'success': True})


@bp.route('/activities/<int:activity_id>', methods=['DELETE'])
def delete_activity(activity_id):
    if not activity_store.delete(get_db(), current_user_id(), activity_id, current_app.config['DATA_DIR']):
        raise _not_found()
    return jsonify({'success': True})


@bp.route('/activities/<int:activity_id>/elevation', methods=['POST'])
def correct_elevation(activity_id):
    try:
        result = ingest_service.recompute_elevation(get_db(), current_app.config, current_user_id(), activity_id)
    except LookupError as exc:
        raise _not_found() from exc
    except dem_service.DemError as exc:
        logger.warning("Correction d'altitude en échec (activité %s) : %s", activity_id, exc)
        raise ApiError("Service d'altitude indisponible", 502) from exc
    return jsonify({'success': True, **result})


@bp.route('/activities/<int:activity_id>/analysis')
def get_activity_analysis(activity_id):
    """Analyse avancée enregistrée ; 'analysis' vaut null tant qu'elle n'a pas été lancée."""
    db, user_id = get_db(), current_user_id()
    if activity_store.get(db, user_id, activity_id) is None:
        raise _not_found()
    stored = analysis_store.load(db, user_id, activity_id) or {'analysis': None, 'computed_at': None}
    return jsonify({'success': True, **stored})


@bp.route('/activities/<int:activity_id>/analysis', methods=['POST'])
def run_activity_analysis(activity_id):
    """(Re)calcule l'analyse avancée depuis le fichier d'origine et l'enregistre."""
    with heavy_slot():
        try:
            stored = analysis_store.compute(get_db(), current_app.config, current_user_id(), activity_id)
        except LookupError as exc:
            raise _not_found() from exc
        except ValueError as exc:  # fichier refusé (FileFormatError) ou trace trop volumineuse
            logger.warning("Analyse de l'activité %s refusée : %s", activity_id, exc)
            raise ApiError("Analyse impossible : trace trop volumineuse ou fichier d'origine illisible") from exc
        except OSError as exc:
            logger.error("Fichier d'origine illisible pour l'activité %s : %s", activity_id, exc)
            raise _source_unavailable() from exc
        return jsonify({'success': True, **stored})


@bp.route('/activities/<int:activity_id>/analysis', methods=['DELETE'])
def delete_activity_analysis(activity_id):
    db, user_id = get_db(), current_user_id()
    if activity_store.get(db, user_id, activity_id) is None:
        raise _not_found()
    analysis_store.delete(db, user_id, activity_id)
    return jsonify({'success': True})


@bp.route('/inbox')
@admin_required
def inbox_status():
    """Derniers fichiers vus par le dossier surveillé (diagnostic, admin uniquement)."""
    rows = get_db().execute(
        "SELECT path, size, status, error, activity_id, processed_at FROM inbox_files "
        "ORDER BY processed_at DESC LIMIT 100").fetchall()
    return jsonify({'enabled': current_app.config['INBOX_ENABLED'], 'files': [dict(r) for r in rows]})
