"""
Service d'analyse de trajectoires GPS.
Implémente les fonctionnalités d'analyse de base de la Phase 3.
"""

import math
from typing import List, Dict, Any
from geopy.distance import geodesic
from app.services.timing_tools import track_time


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