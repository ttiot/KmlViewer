"""
Routes API pour l'édition et l'export de fichiers KML.
Phase 4 : Fonctionnalités d'édition et export multi-formats.
"""

from flask import request, jsonify, make_response
from app.services.kml_editor import KMLEditor
from app.services.timing_tools import track_time
from . import bp


# ===== ROUTES D'ÉDITION =====

@bp.route('/editor/add-point', methods=['POST'])
@track_time
def add_point():
    """
    Ajoute un nouveau point à la trajectoire.
    
    Expects:
        JSON avec 'features', 'coordinates', 'name', 'description', 'is_annotation'
        
    Returns:
        JSON avec le résultat de l'opération
    """
    return jsonify({
        'success': False,
        'error': 'Not implemented yet'
    }), 400

    try:
        data = request.get_json()
        
        if not data or 'features' not in data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes. Veuillez fournir features et coordinates.'
            }), 400
        
        features = data['features']
        coordinates = data['coordinates']
        name = data.get('name', 'Nouveau point')
        description = data.get('description', '')
        is_annotation = data.get('is_annotation', False)
        
        # Ajouter le point
        result = KMLEditor.add_point(features, coordinates, name, description, is_annotation)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message'],
                'point': result['point'],
                'total_points': result['total_points'],
                'updated_features': features
            })
        else:
            return jsonify(result), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'ajout du point: {str(e)}'
        }), 500


@bp.route('/editor/update-point', methods=['POST'])
@track_time
def update_point():
    """
    Met à jour un point existant.
    
    Expects:
        JSON avec 'features', 'point_index', 'updates'
        
    Returns:
        JSON avec le résultat de l'opération
    """
    return jsonify({
        'success': False,
        'error': 'Not implemented yet'
    }), 400
    try:
        data = request.get_json()
        
        if not data or 'features' not in data or 'point_index' not in data or 'updates' not in data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes. Veuillez fournir features, point_index et updates.'
            }), 400
        
        features = data['features']
        point_index = data['point_index']
        updates = data['updates']
        
        # Mettre à jour le point
        result = KMLEditor.update_point(features, point_index, updates)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message'],
                'point': result['point'],
                'updated_features': features
            })
        else:
            return jsonify(result), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la modification du point: {str(e)}'
        }), 500


@bp.route('/editor/delete-point', methods=['POST'])
@track_time
def delete_point():
    """
    Supprime un point de la trajectoire.
    
    Expects:
        JSON avec 'features', 'point_index'
        
    Returns:
        JSON avec le résultat de l'opération
    """
    return jsonify({
        'success': False,
        'error': 'Not implemented yet'
    }), 400
    try:
        data = request.get_json()
        
        if not data or 'features' not in data or 'point_index' not in data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes. Veuillez fournir features et point_index.'
            }), 400
        
        features = data['features']
        point_index = data['point_index']
        
        # Supprimer le point
        result = KMLEditor.delete_point(features, point_index)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message'],
                'deleted_point': result['deleted_point'],
                'total_points': result['total_points'],
                'updated_features': features
            })
        else:
            return jsonify(result), 400
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la suppression du point: {str(e)}'
        }), 500


@bp.route('/editor/simplify-trace', methods=['POST'])
@track_time
def simplify_trace():
    """
    Simplifie une trace en réduisant le nombre de points.
    
    Expects:
        JSON avec 'coordinates' et optionnellement 'tolerance'
        
    Returns:
        JSON avec les coordonnées simplifiées
    """
    return jsonify({
        'success': False,
        'error': 'Not implemented yet'
    }), 400
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        tolerance = data.get('tolerance', 0.0001)
        
        if len(coordinates) < 3:
            return jsonify({
                'success': False,
                'error': 'Au moins 3 points sont nécessaires pour simplifier une trace'
            }), 400
        
        # Simplifier la trace
        simplified_coordinates = KMLEditor.simplify_trace(coordinates, tolerance)
        
        return jsonify({
            'success': True,
            'original_points': len(coordinates),
            'simplified_points': len(simplified_coordinates),
            'reduction_percentage': round((1 - len(simplified_coordinates) / len(coordinates)) * 100, 1),
            'simplified_coordinates': simplified_coordinates
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la simplification: {str(e)}'
        }), 500


# ===== ROUTES D'EXPORT =====

@bp.route('/export/gpx', methods=['POST'])
@track_time
def export_gpx():
    """
    Exporte les données au format GPX.
    
    Expects:
        JSON avec 'features' et optionnellement 'metadata'
        
    Returns:
        Fichier GPX en téléchargement
    """
    try:
        data = request.get_json()
        
        if not data or 'features' not in data:
            return jsonify({
                'success': False,
                'error': 'Features manquantes'
            }), 400
        
        features = data['features']
        metadata = data.get('metadata', {})
        
        # Générer le contenu GPX
        gpx_content = KMLEditor.export_to_gpx(features, metadata)
        
        # Créer la réponse avec le fichier
        response = make_response(gpx_content)
        response.headers['Content-Type'] = 'application/gpx+xml'
        response.headers['Content-Disposition'] = 'attachment; filename=trajectory.gpx'
        
        return response
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'export GPX: {str(e)}'
        }), 500


@bp.route('/export/csv', methods=['POST'])
@track_time
def export_csv():
    """
    Exporte les données au format CSV.
    
    Expects:
        JSON avec 'features' et optionnellement 'include_traces'
        
    Returns:
        Fichier CSV en téléchargement
    """
    try:
        data = request.get_json()
        
        if not data or 'features' not in data:
            return jsonify({
                'success': False,
                'error': 'Features manquantes'
            }), 400
        
        features = data['features']
        include_traces = data.get('include_traces', True)
        
        # Générer le contenu CSV
        csv_content = KMLEditor.export_to_csv(features, include_traces)
        
        # Créer la réponse avec le fichier
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=trajectory.csv'
        
        return response
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'export CSV: {str(e)}'
        }), 500


@bp.route('/export/geojson', methods=['POST'])
@track_time
def export_geojson():
    """
    Exporte les données au format GeoJSON.
    
    Expects:
        JSON avec 'features'
        
    Returns:
        Fichier GeoJSON en téléchargement
    """
    try:
        data = request.get_json()
        
        if not data or 'features' not in data:
            return jsonify({
                'success': False,
                'error': 'Features manquantes'
            }), 400
        
        features = data['features']
        
        # Générer le contenu GeoJSON
        geojson_content = KMLEditor.export_to_geojson(features)
        
        # Créer la réponse avec le fichier
        response = make_response(geojson_content)
        response.headers['Content-Type'] = 'application/geo+json'
        response.headers['Content-Disposition'] = 'attachment; filename=trajectory.geojson'
        
        return response
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'export GeoJSON: {str(e)}'
        }), 500


@bp.route('/export/kml', methods=['POST'])
@track_time
def export_kml():
    """
    Exporte les données au format KML.
    
    Expects:
        JSON avec 'features' et optionnellement 'metadata'
        
    Returns:
        Fichier KML en téléchargement
    """
    try:
        data = request.get_json()
        
        if not data or 'features' not in data:
            return jsonify({
                'success': False,
                'error': 'Features manquantes'
            }), 400
        
        features = data['features']
        metadata = data.get('metadata', {})
        
        # Générer le contenu KML
        kml_content = KMLEditor.export_to_kml(features, metadata)
        
        # Créer la réponse avec le fichier
        response = make_response(kml_content)
        response.headers['Content-Type'] = 'application/vnd.google-earth.kml+xml'
        response.headers['Content-Disposition'] = 'attachment; filename=trajectory.kml'
        
        return response
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'export KML: {str(e)}'
        }), 500


@bp.route('/export/formats', methods=['GET'])
@track_time
def get_export_formats():
    """
    Retourne la liste des formats d'export disponibles.
    
    Returns:
        JSON avec les formats supportés
    """
    formats = {
        'gpx': {
            'name': 'GPX',
            'description': 'Format GPS Exchange pour les appareils GPS',
            'extension': '.gpx',
            'mime_type': 'application/gpx+xml'
        },
        'csv': {
            'name': 'CSV',
            'description': 'Comma Separated Values pour tableurs',
            'extension': '.csv',
            'mime_type': 'text/csv'
        },
        'geojson': {
            'name': 'GeoJSON',
            'description': 'Format JSON géospatial pour applications web',
            'extension': '.geojson',
            'mime_type': 'application/geo+json'
        },
        'kml': {
            'name': 'KML',
            'description': 'Keyhole Markup Language pour Google Earth',
            'extension': '.kml',
            'mime_type': 'application/vnd.google-earth.kml+xml'
        }
    }
    
    return jsonify({
        'success': True,
        'formats': formats
    })