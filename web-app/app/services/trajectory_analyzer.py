"""
Service d'analyse de trajectoires GPS.
Implémente les fonctionnalités d'analyse de base de la Phase 3.
"""

import math
import logging
from typing import List, Dict, Any
from geopy.distance import geodesic
from app.services.timing_tools import track_time

# Configuration du logger
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class TrajectoryAnalyzer:
    """Service d'analyse des trajectoires GPS."""
    
    @staticmethod
    @track_time
    def calculate_distance_between_points(point1: List[float], point2: List[float]) -> float:
        """
        Calcule la distance entre deux points GPS en utilisant la formule de Haversine.
        
        Args:
            point1: [lat, lon, alt] du premier point
            point2: [lat, lon, alt] du deuxième point
            
        Returns:
            Distance en mètres
        """
        if len(point1) < 2 or len(point2) < 2:
            return 0.0
            
        # Utiliser geopy pour un calcul précis
        coord1 = (point1[0], point1[1])
        coord2 = (point2[0], point2[1])
        
        distance_2d = geodesic(coord1, coord2).meters
        
        # Ajouter la composante d'altitude si disponible
        if len(point1) > 2 and len(point2) > 2:
            alt_diff = abs(point1[2] - point2[2])
            # Distance 3D en utilisant le théorème de Pythagore
            distance_3d = math.sqrt(distance_2d**2 + alt_diff**2)
            return distance_3d
            
        return distance_2d
    
    @staticmethod
    @track_time
    def calculate_total_distance(coordinates: List[List[float]]) -> float:
        """
        Calcule la distance totale d'une trace GPS.
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            
        Returns:
            Distance totale en mètres
        """
        if len(coordinates) < 2:
            return 0.0
            
        total_distance = 0.0
        for i in range(1, len(coordinates)):
            total_distance += TrajectoryAnalyzer.calculate_distance_between_points(
                coordinates[i-1], coordinates[i]
            )
            
        return total_distance
    
    @staticmethod
    @track_time
    def calculate_elevation_profile(coordinates: List[List[float]]) -> Dict[str, Any]:
        """
        Calcule le profil d'élévation d'une trace.
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            
        Returns:
            Dictionnaire avec les statistiques d'élévation
        """
        if len(coordinates) < 2:
            return {
                'total_ascent': 0.0,
                'total_descent': 0.0,
                'min_elevation': 0.0,
                'max_elevation': 0.0,
                'elevation_gain': 0.0,
                'elevation_profile': []
            }
        
        elevations = [coord[2] if len(coord) > 2 else 0 for coord in coordinates]
        
        # Filtrer les valeurs nulles ou aberrantes
        valid_elevations = [alt for alt in elevations if alt is not None and alt > -1000 and alt < 10000]
        
        if not valid_elevations:
            return {
                'total_ascent': 0.0,
                'total_descent': 0.0,
                'min_elevation': 0.0,
                'max_elevation': 0.0,
                'elevation_gain': 0.0,
                'elevation_profile': []
            }
        
        # Calcul des montées et descentes
        total_ascent = 0.0
        total_descent = 0.0
        
        # Utiliser un seuil pour éviter le bruit GPS
        elevation_threshold = 3.0  # mètres
        
        for i in range(1, len(elevations)):
            if elevations[i] is not None and elevations[i-1] is not None:
                diff = elevations[i] - elevations[i-1]
                if abs(diff) > elevation_threshold:
                    if diff > 0:
                        total_ascent += diff
                    else:
                        total_descent += abs(diff)
        
        # Calcul du profil d'élévation avec distance cumulative
        distance_cumulative = 0.0
        elevation_profile = []
        
        for i, coord in enumerate(coordinates):
            if i > 0:
                distance_cumulative += TrajectoryAnalyzer.calculate_distance_between_points(
                    coordinates[i-1], coord
                )
            
            elevation_profile.append({
                'distance': distance_cumulative,
                'elevation': coord[2] if len(coord) > 2 else 0,
                'index': i
            })
        
        return {
            'total_ascent': round(total_ascent, 1),
            'total_descent': round(total_descent, 1),
            'min_elevation': round(min(valid_elevations), 1),
            'max_elevation': round(max(valid_elevations), 1),
            'elevation_gain': round(max(valid_elevations) - min(valid_elevations), 1),
            'elevation_profile': elevation_profile
        }
    
    @staticmethod
    @track_time
    def calculate_speed_statistics(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Calcule les statistiques de vitesse à partir des points GPS.
        
        Args:
            points: Liste des points avec informations parsées
            
        Returns:
            Dictionnaire avec les statistiques de vitesse
        """
        speeds = []
        speed_profile = []
        
        for i, point in enumerate(points):
            parsed_info = point.get('parsed_info', {})
            speed_kmh = parsed_info.get('speed_kmh')
            
            if speed_kmh is not None and speed_kmh >= 0:
                speeds.append(speed_kmh)
                speed_profile.append({
                    'index': i,
                    'speed_kmh': speed_kmh,
                    'speed_kts': parsed_info.get('speed_kts', speed_kmh / 1.852),
                    'coordinates': point.get('coordinates', [0, 0])
                })
        
        if not speeds:
            return {
                'avg_speed_kmh': 0.0,
                'max_speed_kmh': 0.0,
                'min_speed_kmh': 0.0,
                'avg_speed_kts': 0.0,
                'max_speed_kts': 0.0,
                'min_speed_kts': 0.0,
                'speed_profile': []
            }
        
        avg_speed = sum(speeds) / len(speeds)
        max_speed = max(speeds)
        min_speed = min(speeds)
        
        return {
            'avg_speed_kmh': round(avg_speed, 1),
            'max_speed_kmh': round(max_speed, 1),
            'min_speed_kmh': round(min_speed, 1),
            'avg_speed_kts': round(avg_speed / 1.852, 1),
            'max_speed_kts': round(max_speed / 1.852, 1),
            'min_speed_kts': round(min_speed / 1.852, 1),
            'speed_profile': speed_profile
        }
    
    @staticmethod
    @track_time
    def estimate_duration_from_points(points: List[Dict[str, Any]], 
                                    avg_speed_kmh: float = None) -> Dict[str, Any]:
        """
        Estime la durée du trajet à partir des points et de la vitesse moyenne.
        
        Args:
            points: Liste des points GPS
            avg_speed_kmh: Vitesse moyenne en km/h (optionnel)
            
        Returns:
            Dictionnaire avec les informations de durée
        """
        if len(points) < 2:
            return {
                'estimated_duration_hours': 0.0,
                'estimated_duration_minutes': 0.0,
                'moving_time_hours': 0.0,
                'total_time_hours': 0.0
            }
        
        # Essayer d'extraire les timestamps si disponibles
        # Pour l'instant, on utilise une estimation basée sur la distance et la vitesse
        
        # Calculer la distance totale entre les points
        coordinates = []
        for point in points:
            lat_lon = point.get('coordinates', [0, 0])  # Valeur par défaut [0, 0]
            altitude = point.get('altitude', 0)         # Valeur par défaut 0
            coordinates.append(lat_lon + [altitude])
        
        if len(coordinates) > 1:
            total_distance = TrajectoryAnalyzer.calculate_total_distance(coordinates)
        
        # Utiliser la vitesse moyenne des points ou une valeur par défaut
        if avg_speed_kmh is None or avg_speed_kmh <= 0:
            # Calculer la vitesse moyenne à partir des points
            speed_stats = TrajectoryAnalyzer.calculate_speed_statistics(points)
            avg_speed_kmh = speed_stats.get('avg_speed_kmh', 50.0)  # 50 km/h par défaut
        
        if avg_speed_kmh > 0:
            duration_hours = (total_distance / 1000) / avg_speed_kmh
        else:
            duration_hours = 0.0
        
        return {
            'estimated_duration_hours': round(duration_hours, 2),
            'estimated_duration_minutes': round(duration_hours * 60, 1),
            'moving_time_hours': round(duration_hours, 2),  # Pour l'instant, identique
            'total_time_hours': round(duration_hours, 2),   # Pour l'instant, identique
            'total_distance_km': round(total_distance / 1000, 2)
        }
    
    @staticmethod
    @track_time
    def analyze_trajectory(features: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyse complète d'une trajectoire GPS.
        
        Args:
            features: Liste des features (polylines et points) du KML
            
        Returns:
            Dictionnaire avec toutes les analyses
        """
        # Séparer les traces et les points
        polylines = [f for f in features if f.get('type') == 'polyline']
        points = [f for f in features if f.get('type') == 'marker' and f.get('is_annotation')]
        
        analysis = {
            'basic_stats': {
                'total_points': len(points),
                'total_traces': len(polylines),
                'has_elevation_data': False,
                'has_speed_data': False
            },
            'distance': {
                'total_distance_m': 0.0,
                'total_distance_km': 0.0
            },
            'elevation': {},
            'speed': {},
            'duration': {},
            'segments': []
        }
        
        # Analyser chaque trace
        all_coordinates = []
        for polyline in polylines:
            coordinates = polyline.get('coordinates', [])
            if coordinates:
                all_coordinates.extend(coordinates)
                
                # Analyse de distance pour cette trace
                trace_distance = TrajectoryAnalyzer.calculate_total_distance(coordinates)
                analysis['distance']['total_distance_m'] += trace_distance
                
                # Vérifier si on a des données d'élévation
                if any(len(coord) > 2 and coord[2] != 0 for coord in coordinates):
                    analysis['basic_stats']['has_elevation_data'] = True
        
        # Distance totale
        analysis['distance']['total_distance_km'] = round(
            analysis['distance']['total_distance_m'] / 1000, 2
        )
        
        # Analyse d'élévation si on a des coordonnées
        if all_coordinates and analysis['basic_stats']['has_elevation_data']:
            analysis['elevation'] = TrajectoryAnalyzer.calculate_elevation_profile(all_coordinates)
        
        # Analyse de vitesse si on a des points avec des données de vitesse
        if points:
            # Vérifier si on a des données de vitesse
            has_speed = any(
                point.get('parsed_info', {}).get('speed_kmh') is not None 
                for point in points
            )
            analysis['basic_stats']['has_speed_data'] = has_speed
            
            if has_speed:
                analysis['speed'] = TrajectoryAnalyzer.calculate_speed_statistics(points)
                
                # Analyse de durée
                avg_speed = analysis['speed'].get('avg_speed_kmh', 0)
                analysis['duration'] = TrajectoryAnalyzer.estimate_duration_from_points(
                    points, avg_speed
                )
        
        return analysis
    
    # ===== PHASE 4: ANALYSES AVANCÉES =====
    
    @staticmethod
    @track_time
    def detect_stops(points: List[Dict[str, Any]],
                    speed_threshold: float = 2.0,
                    time_threshold: int = 300) -> List[Dict[str, Any]]:
        """
        Détecte les arrêts dans une trajectoire GPS.
        
        Args:
            points: Liste des points GPS avec informations parsées
            speed_threshold: Seuil de vitesse en km/h pour considérer un arrêt
            time_threshold: Durée minimale en secondes pour considérer un arrêt
            
        Returns:
            Liste des arrêts détectés avec leurs caractéristiques
        """
        stops = []
        current_stop = None
        
        for i, point in enumerate(points):
            parsed_info = point.get('parsed_info', {})
            speed_kmh = parsed_info.get('speed_kmh', 0)
            
            if speed_kmh <= speed_threshold:
                if current_stop is None:
                    # Début d'un nouvel arrêt
                    current_stop = {
                        'start_index': i,
                        'start_point': point,
                        'points_count': 1,
                        'coordinates': point.get('coordinates', [0, 0])
                    }
                else:
                    # Continuation de l'arrêt
                    current_stop['points_count'] += 1
                    current_stop['end_index'] = i
                    current_stop['end_point'] = point
            else:
                if current_stop is not None and current_stop['points_count'] >= 3:
                    # Fin d'un arrêt significatif
                    current_stop['duration_estimated'] = current_stop['points_count'] * 30  # Estimation 30s par point
                    
                    if current_stop['duration_estimated'] >= time_threshold:
                        stops.append(current_stop)
                    
                current_stop = None
        
        # Traiter le dernier arrêt s'il existe
        if current_stop is not None and current_stop['points_count'] >= 3:
            current_stop['duration_estimated'] = current_stop['points_count'] * 30
            if current_stop['duration_estimated'] >= time_threshold:
                stops.append(current_stop)
        
        return stops
    
    @staticmethod
    @track_time
    def segment_trajectory(coordinates: List[List[float]], points: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Segmente automatiquement une trajectoire en portions homogènes.
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            points: Points GPS avec informations de vitesse (optionnel)
            
        Returns:
            Liste des segments avec leurs caractéristiques
        """
        if len(coordinates) < 3:
            return []
        
        segments = []
        current_segment = {
            'start_index': 0,
            'coordinates': [coordinates[0]],
            'type': 'unknown',
            'distance': 0.0,
            'elevation_change': 0.0
        }
        
        for i in range(1, len(coordinates)):
            coord = coordinates[i]
            prev_coord = coordinates[i-1]
            
            # Calculer la distance et le changement d'élévation
            distance = TrajectoryAnalyzer.calculate_distance_between_points(prev_coord, coord)
            elevation_change = 0
            if len(coord) > 2 and len(prev_coord) > 2:
                elevation_change = coord[2] - prev_coord[2]
            
            current_segment['coordinates'].append(coord)
            current_segment['distance'] += distance
            current_segment['elevation_change'] += elevation_change
            
            # Déterminer le type de segment basé sur l'élévation
            if len(current_segment['coordinates']) >= 5:  # Minimum 5 points pour analyser
                avg_elevation_change = current_segment['elevation_change'] / len(current_segment['coordinates'])
                
                if avg_elevation_change > 2:  # Montée
                    segment_type = 'ascent'
                elif avg_elevation_change < -2:  # Descente
                    segment_type = 'descent'
                else:  # Plat
                    segment_type = 'flat'
                
                # Si le type change, créer un nouveau segment
                if current_segment['type'] == 'unknown':
                    current_segment['type'] = segment_type
                elif current_segment['type'] != segment_type and len(current_segment['coordinates']) > 10:
                    # Finaliser le segment actuel
                    current_segment['end_index'] = i - 1
                    current_segment['length'] = len(current_segment['coordinates'])
                    segments.append(current_segment.copy())
                    
                    # Commencer un nouveau segment
                    current_segment = {
                        'start_index': i - 1,
                        'coordinates': [prev_coord, coord],
                        'type': segment_type,
                        'distance': distance,
                        'elevation_change': elevation_change
                    }
        
        # Ajouter le dernier segment
        if len(current_segment['coordinates']) > 1:
            current_segment['end_index'] = len(coordinates) - 1
            current_segment['length'] = len(current_segment['coordinates'])
            segments.append(current_segment)
        
        # Convertir les propriétés pour correspondre à ce que le frontend attend
        for segment in segments:
            # Convertir distance de mètres vers kilomètres
            segment['distance_km'] = round(segment['distance'] / 1000, 2)
            
            # Calculer la vitesse moyenne si on a des points avec des données de vitesse
            if points and len(points) > 0:
                # Trouver les points correspondant à ce segment
                start_idx = segment.get('start_index', 0)
                end_idx = segment.get('end_index', len(points) - 1)
                
                # S'assurer que les indices sont dans les limites
                start_idx = max(0, min(start_idx, len(points) - 1))
                end_idx = max(start_idx, min(end_idx, len(points) - 1))
                
                segment_points = points[start_idx:end_idx + 1]
                speeds = []
                
                for point in segment_points:
                    parsed_info = point.get('parsed_info', {})
                    speed_kmh = parsed_info.get('speed_kmh')
                    if speed_kmh is not None and speed_kmh >= 0:
                        speeds.append(speed_kmh)
                
                if speeds:
                    segment['avg_speed_kmh'] = round(sum(speeds) / len(speeds), 1)
                else:
                    segment['avg_speed_kmh'] = 0.0
            else:
                segment['avg_speed_kmh'] = 0.0  # Valeur par défaut
        
        return segments
    
    @staticmethod
    @track_time
    def analyze_speed_zones(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyse les zones de vitesse et colore le trajet selon la vitesse.
        
        Args:
            points: Liste des points GPS avec informations de vitesse
            
        Returns:
            Dictionnaire avec l'analyse des zones de vitesse
        """
        
        if not points:
            return {'zones': [], 'speed_distribution': {}}
        
        speeds = []
        speed_points = []
        
        for i, point in enumerate(points):
            parsed_info = point.get('parsed_info', {})
            speed_kmh = parsed_info.get('speed_kmh')
            
            if speed_kmh is not None and speed_kmh >= 0:
                speeds.append(speed_kmh)
                speed_points.append({
                    'index': i,
                    'speed': speed_kmh,
                    'coordinates': point.get('coordinates', [0, 0])
                })
        logger.debug("Speeds : {{speeds}}")
        if not speeds:
            return {'zones': [], 'speed_distribution': {}}
        
        # Calculer les seuils de vitesse
        max_speed = max(speeds)
        min_speed = min(speeds)
        speed_range = max_speed - min_speed
        
        # Définir les zones de vitesse
        zones = []
        logger.debug("Speed Range : {{speed_range}}")
        if speed_range > 0:
            # Zone lente (0-33% de la plage)
            slow_threshold = min_speed + (speed_range * 0.33)
            # Zone moyenne (33-66% de la plage)
            medium_threshold = min_speed + (speed_range * 0.66)
            # Zone rapide (66-100% de la plage)
            
            current_zone = None
            for point in speed_points:
                speed = point['speed']
                
                if speed <= slow_threshold:
                    zone_type = 'slow'
                    color = '#3498db'  # Bleu
                elif speed <= medium_threshold:
                    zone_type = 'medium'
                    color = '#f39c12'  # Orange
                else:
                    zone_type = 'fast'
                    color = '#e74c3c'  # Rouge
                
                if current_zone is None or current_zone['type'] != zone_type:
                    # Nouvelle zone
                    if current_zone is not None:
                        zones.append(current_zone)
                    
                    current_zone = {
                        'type': zone_type,
                        'color': color,
                        'start_index': point['index'],
                        'points': [point],
                        'avg_speed': speed,
                        'min_speed': speed,
                        'max_speed': speed
                    }
                else:
                    # Continuer la zone actuelle
                    current_zone['points'].append(point)
                    current_zone['avg_speed'] = sum(p['speed'] for p in current_zone['points']) / len(current_zone['points'])
                    current_zone['min_speed'] = min(current_zone['min_speed'], speed)
                    current_zone['max_speed'] = max(current_zone['max_speed'], speed)
            
            # Ajouter la dernière zone
            if current_zone is not None:
                current_zone['end_index'] = current_zone['points'][-1]['index']
                zones.append(current_zone)
        
        # Distribution des vitesses
        speed_distribution = {
            'slow_count': len([s for s in speeds if s <= slow_threshold]) if speed_range > 0 else 0,
            'medium_count': len([s for s in speeds if slow_threshold < s <= medium_threshold]) if speed_range > 0 else 0,
            'fast_count': len([s for s in speeds if s > medium_threshold]) if speed_range > 0 else 0,
            'slow_threshold': slow_threshold if speed_range > 0 else 0,
            'medium_threshold': medium_threshold if speed_range > 0 else 0
        }
        
        return {
            'zones': zones,
            'speed_distribution': speed_distribution,
            'total_points': len(speed_points)
        }
    
    @staticmethod
    @track_time
    def calculate_acceleration_zones(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Calcule les zones d'accélération et de décélération.
        
        Args:
            points: Liste des points GPS avec informations de vitesse
            
        Returns:
            Liste des zones d'accélération/décélération
        """
        if len(points) < 3:
            return []
        
        acceleration_zones = []
        accelerations = []
        
        # Calculer l'accélération entre chaque paire de points
        for i in range(1, len(points)):
            prev_point = points[i-1]
            curr_point = points[i]
            
            prev_speed = prev_point.get('parsed_info', {}).get('speed_kmh', 0)
            curr_speed = curr_point.get('parsed_info', {}).get('speed_kmh', 0)
            
            # Estimation du temps (30 secondes par point en moyenne)
            time_diff = 30  # secondes
            
            # Conversion km/h vers m/s pour le calcul d'accélération
            prev_speed_ms = prev_speed / 3.6
            curr_speed_ms = curr_speed / 3.6
            
            acceleration = (curr_speed_ms - prev_speed_ms) / time_diff  # m/s²
            
            accelerations.append({
                'index': i,
                'acceleration': acceleration,
                'speed_change': curr_speed - prev_speed,
                'coordinates': curr_point.get('coordinates', [0, 0])
            })
        
        # Identifier les zones d'accélération/décélération significatives
        current_zone = None
        acceleration_threshold = 0.5  # m/s²
        
        for acc_data in accelerations:
            acceleration = acc_data['acceleration']
            
            if abs(acceleration) > acceleration_threshold:
                zone_type = 'acceleration' if acceleration > 0 else 'deceleration'
                
                if current_zone is None or current_zone['type'] != zone_type:
                    # Nouvelle zone
                    if current_zone is not None and len(current_zone['points']) >= 2:
                        acceleration_zones.append(current_zone)
                    
                    current_zone = {
                        'type': zone_type,
                        'start_index': acc_data['index'],
                        'points': [acc_data],
                        'avg_acceleration': acceleration,
                        'max_acceleration': abs(acceleration)
                    }
                else:
                    # Continuer la zone actuelle
                    current_zone['points'].append(acc_data)
                    current_zone['avg_acceleration'] = sum(p['acceleration'] for p in current_zone['points']) / len(current_zone['points'])
                    current_zone['max_acceleration'] = max(current_zone['max_acceleration'], abs(acceleration))
            else:
                # Fin de zone d'accélération
                if current_zone is not None and len(current_zone['points']) >= 2:
                    current_zone['end_index'] = acc_data['index']
                    acceleration_zones.append(current_zone)
                current_zone = None
        
        # Ajouter la dernière zone si elle existe
        if current_zone is not None and len(current_zone['points']) >= 2:
            current_zone['end_index'] = current_zone['points'][-1]['index']
            acceleration_zones.append(current_zone)
        
        return acceleration_zones
    
    @staticmethod
    @track_time
    def analyze_terrain(coordinates: List[List[float]]) -> Dict[str, Any]:
        """
        Analyse le terrain (pente, exposition, type de terrain).
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            
        Returns:
            Dictionnaire avec l'analyse du terrain
        """
        if len(coordinates) < 3:
            return {
                'slopes': [],
                'avg_slope': 0,
                'max_slope': 0,
                'min_slope': 0,
                'terrain_analysis': {}
            }
        
        slopes = []
        slope_segments = []
        
        for i in range(1, len(coordinates)):
            prev_coord = coordinates[i-1]
            curr_coord = coordinates[i]
            
            if len(prev_coord) > 2 and len(curr_coord) > 2:
                # Calculer la distance horizontale
                horizontal_distance = TrajectoryAnalyzer.calculate_distance_between_points(
                    [prev_coord[0], prev_coord[1]], [curr_coord[0], curr_coord[1]]
                )
                
                # Calculer la différence d'altitude
                elevation_diff = curr_coord[2] - prev_coord[2]
                
                # Calculer la pente en pourcentage
                if horizontal_distance > 0:
                    slope_percent = (elevation_diff / horizontal_distance) * 100
                    slope_degrees = math.degrees(math.atan(elevation_diff / horizontal_distance))
                else:
                    slope_percent = 0
                    slope_degrees = 0
                
                slope_data = {
                    'index': i,
                    'slope_percent': slope_percent,
                    'slope_degrees': slope_degrees,
                    'elevation_diff': elevation_diff,
                    'horizontal_distance': horizontal_distance,
                    'coordinates': curr_coord
                }
                
                slopes.append(slope_data)
        
        if not slopes:
            return {
                'slopes': [],
                'avg_slope': 0,
                'max_slope': 0,
                'min_slope': 0,
                'terrain_analysis': {}
            }
        
        # Statistiques de pente
        slope_values = [s['slope_percent'] for s in slopes]
        avg_slope = sum(slope_values) / len(slope_values)
        max_slope = max(slope_values)
        min_slope = min(slope_values)
        
        # Classification du terrain
        terrain_types = {
            'flat': len([s for s in slope_values if abs(s) <= 5]),      # Plat: ±5%
            'gentle': len([s for s in slope_values if 5 < abs(s) <= 15]),  # Doux: 5-15%
            'moderate': len([s for s in slope_values if 15 < abs(s) <= 30]), # Modéré: 15-30%
            'steep': len([s for s in slope_values if abs(s) > 30])      # Raide: >30%
        }
        
        # Segments de pente homogène
        current_segment = None
        
        for slope_data in slopes:
            slope = slope_data['slope_percent']
            
            # Déterminer le type de pente
            if abs(slope) <= 5:
                slope_type = 'flat'
            elif abs(slope) <= 15:
                slope_type = 'gentle'
            elif abs(slope) <= 30:
                slope_type = 'moderate'
            else:
                slope_type = 'steep'
            
            if current_segment is None or current_segment['type'] != slope_type:
                # Nouveau segment
                if current_segment is not None:
                    slope_segments.append(current_segment)
                
                current_segment = {
                    'type': slope_type,
                    'start_index': slope_data['index'],
                    'slopes': [slope_data],
                    'avg_slope': slope,
                    'length': 1
                }
            else:
                # Continuer le segment
                current_segment['slopes'].append(slope_data)
                current_segment['avg_slope'] = sum(s['slope_percent'] for s in current_segment['slopes']) / len(current_segment['slopes'])
                current_segment['length'] += 1
        
        # Ajouter le dernier segment
        if current_segment is not None:
            current_segment['end_index'] = current_segment['slopes'][-1]['index']
            slope_segments.append(current_segment)
        
        return {
            'slopes': slopes,
            'avg_slope': round(avg_slope, 2),
            'max_slope': round(max_slope, 2),
            'min_slope': round(min_slope, 2),
            'slope_segments': slope_segments,
            'terrain_analysis': {
                'total_points': len(slopes),
                'terrain_distribution': terrain_types,
                'dominant_terrain': max(terrain_types.keys(), key=lambda k: terrain_types[k])
            }
        }
    
    @staticmethod
    @track_time
    def advanced_trajectory_analysis(features: List[Dict[str, Any]],points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyse avancée complète d'une trajectoire GPS (Phase 4).
        
        Args:
            features: Liste des features (polylines et points) du KML
            
        Returns:
            Dictionnaire avec toutes les analyses avancées
        """
        # Séparer les traces et les points
        polylines = [f for f in features if f.get('type') == 'polyline']
        points = [f for f in points if f.get('type') == 'marker' and f.get('is_annotation')]
        
        for p in points:
            logger.debug(p)

        # Récupérer toutes les coordonnées
        all_coordinates = []
        for polyline in polylines:
            coordinates = polyline.get('coordinates', [])
            if coordinates:
                all_coordinates.extend(coordinates)
        
        analysis = {
            'stops': [],
            'segments': [],
            'speed_zones': {},
            'acceleration_zones': [],
            'terrain_analysis': {},
            'points_of_interest': []
        }
        
        # Analyse des arrêts
        if points:
            analysis['stops'] = TrajectoryAnalyzer.detect_stops(points)
        
        # Segmentation de trajectoire
        if all_coordinates:
            analysis['segments'] = TrajectoryAnalyzer.segment_trajectory(all_coordinates, points)
        
        # Analyse des zones de vitesse
        if points:
            analysis['speed_zones'] = TrajectoryAnalyzer.analyze_speed_zones(points)
        
        # Analyse des zones d'accélération
        if points:
            analysis['acceleration_zones'] = TrajectoryAnalyzer.calculate_acceleration_zones(points)
        
        # Analyse du terrain
        if all_coordinates:
            analysis['terrain'] = TrajectoryAnalyzer.analyze_terrain(all_coordinates)
        
        # Points d'intérêt automatiques (virages importants, changements significatifs)
        analysis['points_of_interest'] = TrajectoryAnalyzer.detect_points_of_interest(
            all_coordinates
        )
        
        return analysis
    
    @staticmethod
    @track_time
    def detect_points_of_interest(coordinates: List[List[float]]) -> List[Dict[str, Any]]:
        """
        Détecte automatiquement les points d'intérêt (virages, changements significatifs).
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            points: Points GPS avec informations (optionnel)
            
        Returns:
            Liste des points d'intérêt détectés
        """
        if len(coordinates) < 5:
            return []
        
        points_of_interest = []
        
        # Détecter les virages importants
        for i in range(2, len(coordinates) - 2):
            prev_coord = coordinates[i-2]
            curr_coord = coordinates[i]
            next_coord = coordinates[i+2]
            
            # Calculer l'angle de changement de direction
            # Vecteur 1: de prev vers curr
            vec1 = [curr_coord[0] - prev_coord[0], curr_coord[1] - prev_coord[1]]
            # Vecteur 2: de curr vers next
            vec2 = [next_coord[0] - curr_coord[0], next_coord[1] - curr_coord[1]]
            
            # Calculer l'angle entre les vecteurs
            dot_product = vec1[0] * vec2[0] + vec1[1] * vec2[1]
            mag1 = math.sqrt(vec1[0]**2 + vec1[1]**2)
            mag2 = math.sqrt(vec2[0]**2 + vec2[1]**2)
            
            if mag1 > 0 and mag2 > 0:
                cos_angle = dot_product / (mag1 * mag2)
                cos_angle = max(-1, min(1, cos_angle))  # Clamp pour éviter les erreurs d'arrondi
                angle_rad = math.acos(cos_angle)
                angle_deg = math.degrees(angle_rad)
                
                # Si l'angle est significatif (> 30°), c'est un point d'intérêt
                if angle_deg > 30:
                    poi = {
                        'type': 'turn',
                        'index': i,
                        'coordinates': curr_coord,
                        'angle_degrees': round(angle_deg, 1),
                        'description': f'Virage de {round(angle_deg, 1)}°',
                        'severity': 'sharp' if angle_deg > 60 else 'moderate'
                    }
                    points_of_interest.append(poi)
        
        # Détecter les changements d'altitude significatifs
        if len(coordinates) > 0 and len(coordinates[0]) > 2:
            for i in range(5, len(coordinates) - 5):
                # Calculer la pente moyenne avant et après ce point
                before_coords = coordinates[i-5:i]
                after_coords = coordinates[i:i+5]
                
                before_elevation_change = sum(
                    c[2] - before_coords[j-1][2] for j, c in enumerate(before_coords[1:], 1)
                ) / len(before_coords[1:])
                
                after_elevation_change = sum(
                    c[2] - after_coords[j-1][2] for j, c in enumerate(after_coords[1:], 1)
                ) / len(after_coords[1:])
                
                # Si le changement de pente est significatif
                elevation_change_diff = abs(after_elevation_change - before_elevation_change)
                if elevation_change_diff > 10:  # Changement de plus de 10m de différence
                    poi = {
                        'type': 'elevation_change',
                        'index': i,
                        'coordinates': coordinates[i],
                        'elevation_change': round(elevation_change_diff, 1),
                        'description': f'Changement de pente ({round(elevation_change_diff, 1)}m)',
                        'before_slope': round(before_elevation_change, 1),
                        'after_slope': round(after_elevation_change, 1)
                    }
                    points_of_interest.append(poi)
        
        return points_of_interest