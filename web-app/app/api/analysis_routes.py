"""
Routes API pour l'analyse de trajectoires GPS.
Phase 3 : Analyses de trajet - Fonctionnalités de base.
"""

from flask import request, jsonify
from app.services.kml_parser import KMLParser
from app.services.trajectory_analyzer import TrajectoryAnalyzer
from app.services.timing_tools import track_time
from . import bp


@bp.route('/analysis/trajectory', methods=['POST'])
@track_time
def analyze_trajectory():
    """
    Analyse une trajectoire GPS à partir des données KML.
    
    Expects:
        JSON avec 'features' contenant les données KML parsées
        
    Returns:
        JSON avec l'analyse complète de la trajectoire
    """
    try:
        data = request.get_json()
        
        if not data or 'features' not in data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes. Veuillez fournir les features KML.'
            }), 400
        
        features = data['features']
        
        # Effectuer l'analyse complète
        analysis = TrajectoryAnalyzer.analyze_trajectory(features)
        
        return jsonify({
            'success': True,
            'analysis': analysis
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse: {str(e)}'
        }), 500


@bp.route('/analysis/upload-and-analyze', methods=['POST'])
@track_time
def upload_and_analyze():
    """
    Upload un fichier KML et effectue l'analyse en une seule requête.
    
    Returns:
        JSON avec les données KML parsées et l'analyse
    """
    try:
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'Aucun fichier fourni'
            }), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'Aucun fichier sélectionné'
            }), 400
        
        # Récupérer le mode d'affichage
        display_mode = request.form.get('display_mode', 'double')
        
        # Lire et parser le fichier KML
        content = file.read().decode('utf-8')
        kml_data = KMLParser.parse_kml_coordinates(content, display_mode)
        
        if not kml_data['success']:
            return jsonify(kml_data), 400
        
        # Effectuer l'analyse
        analysis = TrajectoryAnalyzer.analyze_trajectory(kml_data['features'])
        
        # Combiner les données KML et l'analyse
        result = {
            'success': True,
            'kml_data': kml_data,
            'analysis': analysis
        }
        
        return jsonify(result)
        
    except UnicodeDecodeError:
        return jsonify({
            'success': False,
            'error': 'Erreur d\'encodage du fichier. Assurez-vous qu\'il s\'agit d\'un fichier KML valide.'
        }), 400
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du traitement: {str(e)}'
        }), 500


@bp.route('/analysis/elevation-profile', methods=['POST'])
@track_time
def get_elevation_profile():
    """
    Calcule le profil d'élévation pour une trace donnée.
    
    Expects:
        JSON avec 'coordinates' : liste de [lat, lon, alt]
        
    Returns:
        JSON avec le profil d'élévation détaillé
    """
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        
        if not coordinates or len(coordinates) < 2:
            return jsonify({
                'success': False,
                'error': 'Au moins 2 points sont nécessaires pour calculer un profil d\'élévation'
            }), 400
        
        # Calculer le profil d'élévation
        elevation_data = TrajectoryAnalyzer.calculate_elevation_profile(coordinates)
        
        return jsonify({
            'success': True,
            'elevation_profile': elevation_data
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du calcul du profil d\'élévation: {str(e)}'
        }), 500


@bp.route('/analysis/speed-analysis', methods=['POST'])
@track_time
def get_speed_analysis():
    """
    Analyse les données de vitesse des points GPS.
    
    Expects:
        JSON avec 'points' : liste des points avec parsed_info
        
    Returns:
        JSON avec l'analyse de vitesse
    """
    try:
        data = request.get_json()
        
        if not data or 'points' not in data:
            return jsonify({
                'success': False,
                'error': 'Points manquants'
            }), 400
        
        points = data['points']
        
        # Calculer les statistiques de vitesse
        speed_stats = TrajectoryAnalyzer.calculate_speed_statistics(points)
        
        return jsonify({
            'success': True,
            'speed_analysis': speed_stats
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse de vitesse: {str(e)}'
        }), 500


@bp.route('/analysis/distance', methods=['POST'])
@track_time
def calculate_distance():
    """
    Calcule la distance totale d'une trace.
    
    Expects:
        JSON avec 'coordinates' : liste de [lat, lon, alt]
        
    Returns:
        JSON avec la distance calculée
    """
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        
        if not coordinates or len(coordinates) < 2:
            return jsonify({
                'success': False,
                'error': 'Au moins 2 points sont nécessaires pour calculer une distance'
            }), 400
        
        # Calculer la distance totale
        total_distance = TrajectoryAnalyzer.calculate_total_distance(coordinates)
        
        return jsonify({
            'success': True,
            'distance': {
                'total_distance_m': round(total_distance, 2),
                'total_distance_km': round(total_distance / 1000, 2)
            }
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du calcul de distance: {str(e)}'
        }), 500