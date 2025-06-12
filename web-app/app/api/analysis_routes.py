"""
Routes API pour l'analyse de trajectoires GPS.
Phase 3 : Analyses de trajet - Fonctionnalités de base.
"""
import logging
from flask import request, jsonify
from app.services.kml_parser import KMLParser
from app.services.trajectory_analyzer import TrajectoryAnalyzer
from app.services.timing_tools import track_time
from app.services.cache_service import parse_kml_cached, parse_gpx_cached
from . import bp

# Configuration du logger
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
        
        # Lire et parser le fichier en fonction de son extension
        content = file.read().decode('utf-8')
        ext = file.filename.rsplit('.', 1)[1].lower()
        if ext == 'gpx':
            kml_data = parse_gpx_cached(content)
        else:
            kml_data = parse_kml_cached(content, display_mode)
        
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


# ===== PHASE 4: ROUTES POUR ANALYSES AVANCÉES =====

@bp.route('/analysis/advanced', methods=['POST'])
@track_time
def get_advanced_analysis():
    """
    Effectue une analyse avancée complète d'une trajectoire (Phase 4).
    
    Expects:
        JSON avec 'features' contenant les données KML parsées
        
    Returns:
        JSON avec l'analyse avancée complète
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'error': 'Données manquantes. Veuillez fournir les features KML.'
            }), 400
        
        # features = data['features']
        all_features = data.get('features', [])
        all_points = data.get('points', [])

        # Effectuer l'analyse avancée complète
        advanced_analysis = TrajectoryAnalyzer.advanced_trajectory_analysis(all_features,all_points)

        return jsonify({
            'success': True,
            'analysis': advanced_analysis
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse avancée: {str(e)}'
        }), 500


@bp.route('/analysis/stops', methods=['POST'])
@track_time
def detect_stops():
    """
    Détecte les arrêts dans une trajectoire GPS.
    
    Expects:
        JSON avec 'points' et optionnellement 'speed_threshold' et 'time_threshold'
        
    Returns:
        JSON avec les arrêts détectés
    """
    try:
        data = request.get_json()
        
        if not data or 'points' not in data:
            return jsonify({
                'success': False,
                'error': 'Points manquants'
            }), 400
        
        points = data['points']
        speed_threshold = data.get('speed_threshold', 2.0)
        time_threshold = data.get('time_threshold', 300)
        
        # Détecter les arrêts
        stops = TrajectoryAnalyzer.detect_stops(points, speed_threshold, time_threshold)
        
        return jsonify({
            'success': True,
            'stops': stops,
            'stops_count': len(stops)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la détection des arrêts: {str(e)}'
        }), 500


@bp.route('/analysis/segments', methods=['POST'])
@track_time
def segment_trajectory():
    """
    Segmente automatiquement une trajectoire en portions homogènes.
    
    Expects:
        JSON avec 'coordinates' : liste de [lat, lon, alt]
        
    Returns:
        JSON avec les segments de trajectoire
    """
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        
        if not coordinates or len(coordinates) < 3:
            return jsonify({
                'success': False,
                'error': 'Au moins 3 points sont nécessaires pour segmenter une trajectoire'
            }), 400
        
        # Segmenter la trajectoire
        segments = TrajectoryAnalyzer.segment_trajectory(coordinates)
        
        return jsonify({
            'success': True,
            'segments': segments,
            'segments_count': len(segments)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la segmentation: {str(e)}'
        }), 500


@bp.route('/analysis/speed-zones', methods=['POST'])
@track_time
def analyze_speed_zones():
    """
    Analyse les zones de vitesse et colore le trajet selon la vitesse.
    
    Expects:
        JSON avec 'points' : liste des points avec parsed_info
        
    Returns:
        JSON avec l'analyse des zones de vitesse
    """
    try:
        data = request.get_json()
        
        if not data or 'points' not in data:
            return jsonify({
                'success': False,
                'error': 'Points manquants'
            }), 400
        
        points = data['points']
        
        # Analyser les zones de vitesse
        speed_zones = TrajectoryAnalyzer.analyze_speed_zones(points)
        
        return jsonify({
            'success': True,
            'speed_zones': speed_zones
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse des zones de vitesse: {str(e)}'
        }), 500


@bp.route('/analysis/acceleration', methods=['POST'])
@track_time
def analyze_acceleration():
    """
    Calcule les zones d'accélération et de décélération.
    
    Expects:
        JSON avec 'points' : liste des points avec parsed_info
        
    Returns:
        JSON avec les zones d'accélération/décélération
    """
    try:
        data = request.get_json()
        
        if not data or 'points' not in data:
            return jsonify({
                'success': False,
                'error': 'Points manquants'
            }), 400
        
        points = data['points']
        
        if len(points) < 3:
            return jsonify({
                'success': False,
                'error': 'Au moins 3 points sont nécessaires pour calculer l\'accélération'
            }), 400
        
        # Analyser l'accélération
        acceleration_zones = TrajectoryAnalyzer.calculate_acceleration_zones(points)
        
        return jsonify({
            'success': True,
            'acceleration_zones': acceleration_zones,
            'zones_count': len(acceleration_zones)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse d\'accélération: {str(e)}'
        }), 500


@bp.route('/analysis/terrain', methods=['POST'])
@track_time
def analyze_terrain():
    """
    Analyse le terrain (pente, exposition, type de terrain).
    
    Expects:
        JSON avec 'coordinates' : liste de [lat, lon, alt]
        
    Returns:
        JSON avec l'analyse du terrain
    """
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        
        if not coordinates or len(coordinates) < 3:
            return jsonify({
                'success': False,
                'error': 'Au moins 3 points sont nécessaires pour analyser le terrain'
            }), 400
        
        # Analyser le terrain
        terrain_analysis = TrajectoryAnalyzer.analyze_terrain(coordinates)
        
        return jsonify({
            'success': True,
            'terrain_analysis': terrain_analysis
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de l\'analyse du terrain: {str(e)}'
        }), 500


@bp.route('/analysis/points-of-interest', methods=['POST'])
@track_time
def detect_points_of_interest():
    """
    Détecte automatiquement les points d'intérêt (virages, changements significatifs).
    
    Expects:
        JSON avec 'coordinates' : liste de [lat, lon, alt]
        
    Returns:
        JSON avec les points d'intérêt détectés
    """
    try:
        data = request.get_json()
        
        if not data or 'coordinates' not in data:
            return jsonify({
                'success': False,
                'error': 'Coordonnées manquantes'
            }), 400
        
        coordinates = data['coordinates']
        
        if not coordinates or len(coordinates) < 5:
            return jsonify({
                'success': False,
                'error': 'Au moins 5 points sont nécessaires pour détecter les points d\'intérêt'
            }), 400
        
        # Détecter les points d'intérêt
        points_of_interest = TrajectoryAnalyzer.detect_points_of_interest(coordinates)
        
        return jsonify({
            'success': True,
            'points_of_interest': points_of_interest,
            'poi_count': len(points_of_interest)
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors de la détection des points d\'intérêt: {str(e)}'
        }), 500