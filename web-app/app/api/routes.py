"""
Routes API du visualiseur : chargement de fichiers, exemples, santé.
"""

from flask import current_app, jsonify

from app.api import bp
from app.api.uploads import parse_upload, parse_uploads, public_error, uploaded_file, uploaded_files
from app.api.workload import heavy_slot
from app.auth import admin_required
from app.errors import ApiError
from app.services import file_formats, timing_tools
from app.services.cache_service import parse_gpx_cached, parse_kml_cached
from app.services.file_service import FileService
from app.services.timing_tools import track_time


@bp.route('/upload', methods=['POST'])
@track_time
def upload_file():
    """Parse un fichier et renvoie ses données pour le visualiseur."""
    file = uploaded_file('Aucun fichier sélectionné')
    with heavy_slot():
        return jsonify(parse_upload(file))


@bp.route('/multi-upload', methods=['POST'])
@track_time
def multi_upload():
    """Parse plusieurs fichiers en une requête ; un fichier refusé n'empêche pas les autres."""
    files = uploaded_files()
    with heavy_slot():
        return jsonify({'files': parse_uploads(files)})


@bp.route('/sample-files')
def list_sample_files():
    """Liste les fichiers d'exemple disponibles."""
    return jsonify({'files': FileService.get_sample_files()})


@bp.route('/load-sample/<filename>')
def load_sample_file(filename):
    """Charge un fichier d'exemple."""
    try:
        raw = FileService.load_sample_file(filename)
        content, fmt = file_formats.decode(raw, filename)
    except FileNotFoundError as exc:
        raise ApiError('Fichier non trouvé ou type invalide', 404) from exc
    except file_formats.FileFormatError as exc:
        raise ApiError(str(exc)) from exc
    with heavy_slot():
        parsed = parse_gpx_cached(content) if fmt == 'gpx' else parse_kml_cached(content)
        if not parsed.get('success'):
            raise ApiError(public_error(parsed.get('error'), filename))
        return jsonify(parsed)


@bp.route('/health')
def health_check():
    """Endpoint de vérification de santé de l'API."""
    return jsonify({
        'status': 'healthy',
        'version': current_app.config['APP_VERSION'],
        'api': 'KML/GPX Viewer API'
    })


@bp.route('/stats')
@admin_required
def stats():
    """Temps d'exécution cumulés par fonction (diagnostic, admin uniquement)."""
    return jsonify({
        name: {
            'call_count': entry['count'],
            'total_time': round(entry['total_time'], 2),
            'average_time': round(entry['total_time'] / entry['count'], 4) if entry['count'] else 0,
        }
        for name, entry in timing_tools.snapshot().items()
    })
