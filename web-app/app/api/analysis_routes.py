"""
Routes API pour l'analyse de trajectoires GPS.

Les erreurs inattendues remontent au gestionnaire global (app.errors) : elles sont
journalisées et le client reçoit un message générique.
"""

from flask import jsonify, request

from app.errors import ApiError
from app.services import file_formats
from app.services.timing_tools import track_time
from app.services.trajectory_analyzer import TrajectoryAnalyzer

from . import bp
from .payload import json_body, json_list
from .routes import parse_upload


@bp.route('/analysis/trajectory', methods=['POST'])
@track_time
def analyze_trajectory():
    """Analyse complète d'une trajectoire à partir de ses features."""
    features = json_list(json_body(), 'features', 'Features')
    return jsonify({'success': True, 'analysis': TrajectoryAnalyzer.analyze_trajectory(features)})


@bp.route('/analysis/upload-and-analyze', methods=['POST'])
@track_time
def upload_and_analyze():
    """Parse un fichier et l'analyse en une seule requête."""
    file = request.files.get('file')
    if file is None or not file.filename:
        raise ApiError('Aucun fichier fourni')
    try:
        kml_data = parse_upload(file, request.form.get('display_mode', 'double'))
    except file_formats.FileFormatError as exc:
        raise ApiError(str(exc)) from exc
    if not kml_data.get('success'):
        return jsonify(kml_data), 400
    return jsonify({
        'success': True,
        'kml_data': kml_data,
        'analysis': TrajectoryAnalyzer.analyze_trajectory(kml_data['features']),
    })


@bp.route('/analysis/elevation-profile', methods=['POST'])
@track_time
def get_elevation_profile():
    """Profil d'élévation d'une liste de [lat, lon, alt]."""
    coordinates = json_list(json_body(), 'coordinates', 'Points', 2)
    return jsonify({'success': True,
                    'elevation_profile': TrajectoryAnalyzer.calculate_elevation_profile(coordinates)})


@bp.route('/analysis/speed-analysis', methods=['POST'])
@track_time
def get_speed_analysis():
    """Statistiques de vitesse des points (avec parsed_info)."""
    points = json_list(json_body(), 'points', 'Points')
    return jsonify({'success': True, 'speed_analysis': TrajectoryAnalyzer.calculate_speed_statistics(points)})


@bp.route('/analysis/distance', methods=['POST'])
@track_time
def calculate_distance():
    """Distance totale d'une liste de [lat, lon, alt]."""
    coordinates = json_list(json_body(), 'coordinates', 'Points', 2)
    total = TrajectoryAnalyzer.calculate_total_distance(coordinates)
    return jsonify({'success': True, 'distance': {
        'total_distance_m': round(total, 2), 'total_distance_km': round(total / 1000, 2)}})


@bp.route('/analysis/advanced', methods=['POST'])
@track_time
def get_advanced_analysis():
    """Analyse avancée : arrêts, segments, zones de vitesse, terrain."""
    data = json_body()
    features = data.get('features') or []
    points = data.get('points') or []
    if not isinstance(features, list) or not isinstance(points, list):
        raise ApiError('Features et points doivent être des listes')
    return jsonify({'success': True,
                    'analysis': TrajectoryAnalyzer.advanced_trajectory_analysis(features, points)})


@bp.route('/analysis/stops', methods=['POST'])
@track_time
def detect_stops():
    """Arrêts détectés ; seuils optionnels 'speed_threshold' (km/h) et 'time_threshold' (s)."""
    data = json_body()
    points = json_list(data, 'points', 'Points')
    try:
        speed_threshold = float(data.get('speed_threshold', 2.0))
        time_threshold = float(data.get('time_threshold', 300))
    except (TypeError, ValueError) as exc:
        raise ApiError('Seuils invalides') from exc
    stops = TrajectoryAnalyzer.detect_stops(points, speed_threshold, time_threshold)
    return jsonify({'success': True, 'stops': stops, 'stops_count': len(stops)})


@bp.route('/analysis/segments', methods=['POST'])
@track_time
def segment_trajectory():
    """Découpe la trajectoire en portions homogènes."""
    coordinates = json_list(json_body(), 'coordinates', 'Points', 3)
    segments = TrajectoryAnalyzer.segment_trajectory(coordinates)
    return jsonify({'success': True, 'segments': segments, 'segments_count': len(segments)})


@bp.route('/analysis/speed-zones', methods=['POST'])
@track_time
def analyze_speed_zones():
    """Zones de vitesse des points (avec parsed_info)."""
    points = json_list(json_body(), 'points', 'Points')
    return jsonify({'success': True, 'speed_zones': TrajectoryAnalyzer.analyze_speed_zones(points)})


@bp.route('/analysis/acceleration', methods=['POST'])
@track_time
def analyze_acceleration():
    """Zones d'accélération et de décélération."""
    points = json_list(json_body(), 'points', 'Points', 3)
    zones = TrajectoryAnalyzer.calculate_acceleration_zones(points)
    return jsonify({'success': True, 'acceleration_zones': zones, 'zones_count': len(zones)})


@bp.route('/analysis/terrain', methods=['POST'])
@track_time
def analyze_terrain():
    """Analyse du terrain (pentes) d'une liste de [lat, lon, alt]."""
    coordinates = json_list(json_body(), 'coordinates', 'Points', 3)
    return jsonify({'success': True, 'terrain_analysis': TrajectoryAnalyzer.analyze_terrain(coordinates)})


@bp.route('/analysis/points-of-interest', methods=['POST'])
@track_time
def detect_points_of_interest():
    """Points d'intérêt détectés (virages, changements significatifs)."""
    coordinates = json_list(json_body(), 'coordinates', 'Points', 5)
    points = TrajectoryAnalyzer.detect_points_of_interest(coordinates)
    return jsonify({'success': True, 'points_of_interest': points, 'poi_count': len(points)})
