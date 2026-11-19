"""
Façade d'analyse des trajectoires : les calculs sont dans app.services.advanced_analysis
et app.services.track_series. Chaque méthode ne calcule que ce qu'elle renvoie.
"""

from typing import Any, Dict, List, Optional

from app.services import advanced_analysis, geo, track_series
from app.services.timing_tools import track_time


class TrajectoryAnalyzer:
    """Service d'analyse des trajectoires GPS."""

    @staticmethod
    @track_time
    def calculate_total_distance(coordinates: List[List[float]]) -> float:
        """
        Distance totale (m) d'une liste de [lat, lon] ou [lat, lon, alt], par la même formule
        que toutes les autres mesures (haversine, sans composante verticale : l'altitude GPS
        est trop bruitée pour être sommée).

        Ne lève jamais et renvoie toujours un nombre fini : un point illisible, non numérique,
        NaN ou hors bornes est ignoré, comme dans le reste de l'analyse.
        """
        if not isinstance(coordinates, list):
            return 0.0
        raw = track_series.raw_from_lines(TrajectoryAnalyzer._line(coordinates), max_points=None)
        return float(geo.step_distances(raw['lat'], raw['lon']).sum())

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
    def analyze_trajectory(features: List[Dict[str, Any]], points: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Distance, durée, vitesses, dénivelés et courbes d'un fichier (toutes traces confondues).
        Sans `points`, les points de mesure sont les marqueurs présents dans `features`.
        """
        return advanced_analysis.basic(features, points)

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
        return advanced_analysis.analyze([], points, speed_threshold, time_threshold, only=('stops',))['stops']

    @staticmethod
    @track_time
    def segment_trajectory(coordinates: List[List[float]], points: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Montées, descentes et portions plates d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), points or [], only=('segments',))['segments']

    @staticmethod
    @track_time
    def analyze_speed_zones(points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Répartition du temps (ou des points) par zone de vitesse."""
        return advanced_analysis.analyze([], points, only=('speed_zones',))['speed_zones']

    @staticmethod
    @track_time
    def calculate_acceleration_zones(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Phases d'accélération et de freinage (points horodatés requis)."""
        return advanced_analysis.analyze([], points, only=('acceleration_zones',))['acceleration_zones']

    @staticmethod
    @track_time
    def analyze_terrain(coordinates: List[List[float]]) -> Dict[str, Any]:
        """Répartition des pentes d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), [], only=('terrain',))['terrain']

    @staticmethod
    @track_time
    def detect_points_of_interest(coordinates: List[List[float]]) -> List[Dict[str, Any]]:
        """Point haut, point bas, sommets et virages serrés d'une liste de [lat, lon, alt]."""
        return advanced_analysis.analyze(TrajectoryAnalyzer._line(coordinates), [],
                                         only=('points_of_interest',))['points_of_interest']
