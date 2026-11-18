"""
Routes API du visualiseur : chargement de fichiers, exemples, santé.
"""

from flask import current_app, jsonify, request

from app.api import bp
from app.auth import admin_required
from app.errors import ApiError
from app.services import file_formats, timing_tools
from app.services.cache_service import parse_gpx_cached, parse_kml_cached
from app.services.file_service import FileService
from app.services.timing_tools import track_time


def parse_upload(file, display_mode: str = 'double') -> dict:
    """Parse un fichier envoyé (GPX, KML ou KMZ). Lève FileFormatError si le fichier est refusé."""
    content, fmt = file_formats.decode(file.read(), file.filename)
    return parse_gpx_cached(content) if fmt == 'gpx' else parse_kml_cached(content, display_mode)


@bp.route('/upload', methods=['POST'])
@track_time
def upload_file():
    """Parse un fichier et renvoie ses données pour le visualiseur."""
    file = request.files.get('file')
    if file is None or not file.filename:
        raise ApiError('Aucun fichier sélectionné')
    try:
        result = parse_upload(file, request.form.get('display_mode', 'double'))
    except file_formats.FileFormatError as exc:
        raise ApiError(str(exc)) from exc
    return jsonify(result), 200 if result.get('success') else 400


@bp.route('/multi-upload', methods=['POST'])
@track_time
def multi_upload():
    """Parse plusieurs fichiers en une requête ; un fichier refusé n'empêche pas les autres."""
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        raise ApiError('Aucun fichier fourni')
    display_mode = request.form.get('display_mode', 'double')
    results = []
    for file in files:
        try:
            # Copie superficielle : le résultat sort d'un cache partagé, on n'y écrit pas
            result = {**parse_upload(file, display_mode), 'filename': file.filename}
        except file_formats.FileFormatError as exc:
            result = {'filename': file.filename, 'success': False, 'error': str(exc)}
        results.append(result)
    return jsonify({'files': results})


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
    if fmt == 'gpx':
        return jsonify(parse_gpx_cached(content))
    return jsonify(parse_kml_cached(content, request.args.get('display_mode', 'double')))


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
