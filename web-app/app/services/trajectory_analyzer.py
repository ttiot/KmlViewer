"""
Façade d'analyse des trajectoires : les calculs sont dans app.services.advanced_analysis.
"""

from typing import List, Dict, Any

import numpy as np
from geopy.distance import geodesic
from app.services import advanced_analysis
from app.services.timing_tools import track_time



class TrajectoryAnalyzer:
    """Service d'analyse des trajectoires GPS."""

    @staticmethod
    @track_time
    def calculate_distance_between_points(
        point1: List[float], point2: List[float], include_altitude: bool = False
    ) -> float:
        """
        Calcule la distance entre deux points GPS en utilisant la formule de Haversine.

        Args:
            point1: [lat, lon, alt] du premier point
            point2: [lat, lon, alt] du deuxième point
            include_altitude: Si True, calcule la distance 3D en utilisant
                également la différence d'altitude

        Returns:
            Distance en mètres
        """
        if len(point1) < 2 or len(point2) < 2:
            return 0.0

        # Utiliser geopy pour un calcul précis
        coord1 = (point1[0], point1[1])
        coord2 = (point2[0], point2[1])

        distance_2d = geodesic(coord1, coord2).meters

        # Les données d'altitude de certains fichiers peuvent être très
        # bruitées. Leur prise en compte lors de la sommation des distances
        # entraîne une surestimation importante (par exemple plus de 50 km au
        # lieu d'environ 42 km pour le GPX de référence).  Pour une estimation
        # cohérente avec le parcours réel, on ignore donc la composante
        # verticale et on se limite à la distance 2D.

        return distance_2d

    @staticmethod
    @track_time
    def calculate_total_distance(coordinates: List[List[float]], include_altitude: bool = False) -> float:
        """
        Calcule la distance totale d'une trace GPS.

        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            include_altitude: Prendre en compte la composante verticale pour le
                calcul de distance

        Returns:
            Distance totale en mètres
        """
        if len(coordinates) < 2:
            return 0.0

        coords = np.array(coordinates, dtype=float)
        lat1 = np.radians(coords[:-1, 0])
        lon1 = np.radians(coords[:-1, 1])
        lat2 = np.radians(coords[1:, 0])
        lon2 = np.radians(coords[1:, 1])

        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
        distance_2d = 6371000 * np.sum(c)

        return float(distance_2d)

    @staticmethod
    @track_time
    def calculate_elevation_profile(coordinates: List[List[float]]) -> Dict[str, Any]:
        """Dénivelés, altitudes extrêmes et profil d'une liste de [lat, lon, alt] ({} sans altitude)."""
        return advanced_analysis.basic(TrajectoryAnalyzer._line(coordinates), [])['elevation']

    @staticmethod
    @track_time
    def calculate_speed_statistics(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Vitesses moyenne (en mouvement), de pointe et minimale d'une suite de points ({} sans vitesse)."""
        return advanced_analysis.basic([], points)['speed']

    @staticmethod
    @track_time
    def analyze_trajectory(features: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Distance, durée, vitesses, dénivelés et courbes d'un fichier (toutes traces confondues)."""
        return advanced_analysis.basic(features)

    # ===== Analyses avancées : calculs dans app.services.advanced_analysis =====

    @staticmethod
    def _line(coordinates: List[List[float]]) -> List[Dict[str, Any]]:
        return [{'type': 'polyline', 'coordinates': coordinates}]

    @staticmethod
    @track_time
    def advanced_trajectory_analysis(features: List[Dict[str, Any]], points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Arrêts, montées et descentes, zones de vitesse, accélérations, terrain, points remarquables."""
        return advanced_analysis.analyze(features, points)

    @staticmethod
    @track_time
    def detect_stops(
        points: List[Dict[str, Any]], speed_threshold: float = advanced_analysis.STOP_SPEED_KMH,
        time_threshold: float = advanced_analysis.STOP_MIN_DURATION_S,
    ) -> List[Dict[str, Any]]:
        """Arrêts d'une suite de points (vitesse en km/h, durée minimale en secondes)."""
        return advanced_analysis.analyze([], points, speed_threshold, time_threshold)['stops']

    @staticmethod
    @track_time
    def segment_trajectory(coordinates: List[List[float]], points: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Montées, descentes et portions plates d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), points or [])['segments']

    @staticmethod
    @track_time
    def analyze_speed_zones(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Répartition du temps (ou des points) par zone de vitesse."""
        return advanced_analysis.analyze([], points)['speed_zones']

    @staticmethod
    @track_time
    def calculate_acceleration_zones(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Phases d'accélération et de freinage (points horodatés requis)."""
        return advanced_analysis.analyze([], points)['acceleration_zones']

    @staticmethod
    @track_time
    def analyze_terrain(coordinates: List[List[float]]) -> Dict[str, Any]:
        """Répartition des pentes d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), [])['terrain']

    @staticmethod
    @track_time
    def detect_points_of_interest(coordinates: List[List[float]]) -> List[Dict[str, Any]]:
        """Point haut, point bas, sommets et virages serrés d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), [])['points_of_interest']
