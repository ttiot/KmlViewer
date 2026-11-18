"""
Routes API de simplification et d'export.

L'ajout, la modification et la suppression de points se font dans le navigateur, sur les
données déjà chargées : le serveur n'intervient que pour le calcul (simplification) et la
sérialisation (export).
"""

import re

from flask import jsonify, make_response

from app.errors import ApiError
from app.services.kml_editor import KMLEditor
from app.services.timing_tools import track_time

from . import bp
from .payload import json_body, json_list

EXPORT_FORMATS = {
    'gpx': {'name': 'GPX', 'description': 'Format GPS Exchange pour les appareils GPS',
            'extension': '.gpx', 'mime_type': 'application/gpx+xml'},
    'csv': {'name': 'CSV', 'description': 'Comma Separated Values pour tableurs',
            'extension': '.csv', 'mime_type': 'text/csv'},
    'geojson': {'name': 'GeoJSON', 'description': 'Format JSON géospatial pour applications web',
                'extension': '.geojson', 'mime_type': 'application/geo+json'},
    'kml': {'name': 'KML', 'description': 'Keyhole Markup Language pour Google Earth',
            'extension': '.kml', 'mime_type': 'application/vnd.google-earth.kml+xml'},
}
MAX_TOLERANCE = 1.0  # degrés


def _safe_filename(value) -> str:
    """Nom de fichier réduit à des caractères sans risque pour l'en-tête Content-Disposition."""
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '')).strip('._')[:80]
    return name or 'trajectory'


@bp.route('/editor/simplify-trace', methods=['POST'])
@track_time
def simplify_trace():
    """Simplifie une trace : JSON avec 'coordinates' et, en option, 'tolerance' (degrés)."""
    data = json_body()
    coordinates = json_list(data, 'coordinates', 'Points', 3)
    try:
        tolerance = float(data.get('tolerance', 0.0001))
        valid = all(len(c) >= 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in c[:2])
                    for c in coordinates)
    except TypeError as exc:
        raise ApiError('Coordonnées ou tolérance invalides') from exc
    if not valid or not 0 < tolerance <= MAX_TOLERANCE:
        raise ApiError('Coordonnées ou tolérance invalides')

    kept = KMLEditor.simplify_indices(coordinates, tolerance)
    return jsonify({
        'success': True,
        'original_points': len(coordinates),
        'simplified_points': len(kept),
        'reduction_percentage': round((1 - len(kept) / len(coordinates)) * 100, 1),
        'simplified_coordinates': [coordinates[i] for i in kept],
        # Positions conservées : permet au client d'aligner les données parallèles (horodatages)
        'kept_indices': kept,
    })


@bp.route('/export/<fmt>', methods=['POST'])
@track_time
def export_features(fmt):
    """Exporte les features fournies (JSON 'features', 'metadata', 'include_traces', 'filename')."""
    if fmt not in EXPORT_FORMATS:
        raise ApiError("Format d'export inconnu", 404)
    data = json_body()
    features = json_list(data, 'features', 'Features')
    if not all(isinstance(f, dict) for f in features):
        raise ApiError('Features invalides')
    metadata = data.get('metadata') if isinstance(data.get('metadata'), dict) else {}

    try:
        if fmt == 'gpx':
            content = KMLEditor.export_to_gpx(features, metadata)
        elif fmt == 'csv':
            content = KMLEditor.export_to_csv(features, bool(data.get('include_traces', True)))
        elif fmt == 'geojson':
            content = KMLEditor.export_to_geojson(features)
        else:
            content = KMLEditor.export_to_kml(features, metadata)
    except (TypeError, ValueError, KeyError, IndexError, AttributeError) as exc:
        # Features mal formées (coordonnées absentes ou non numériques)
        raise ApiError('Features invalides') from exc

    response = make_response(content)
    response.headers['Content-Type'] = EXPORT_FORMATS[fmt]['mime_type']
    filename = _safe_filename(data.get('filename')) + EXPORT_FORMATS[fmt]['extension']
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@bp.route('/export/formats', methods=['GET'])
def get_export_formats():
    """Formats d'export disponibles."""
    return jsonify({'success': True, 'formats': EXPORT_FORMATS})
