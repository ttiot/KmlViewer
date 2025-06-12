"""GPX parsing service."""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, Any, List
from app.services.timing_tools import track_time
from app.services.trajectory_analyzer import TrajectoryAnalyzer

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

class GPXParser:
    """Simple GPX parser producing features compatible with the KML parser."""

    NAMESPACES = {'gpx': 'http://www.topografix.com/GPX/1/1'}

    @staticmethod
    def _build_parsed_info(lat: float, lon: float, ele: float, speed_kmh: float = None) -> Dict[str, Any]:
        parsed = {
            'speed_kmh': round(speed_kmh, 1) if speed_kmh is not None else None,
            'speed_kts': round(speed_kmh / 1.852, 1) if speed_kmh is not None else None,
            'altitude_ft': round(ele * 3.28084) if ele is not None else None,
            'altitude_m': ele,
            'latitude': lat,
            'longitude': lon,
            'heading': None,
            'raw_description': ''
        }
        return parsed

    @staticmethod
    @track_time
    def parse_gpx_coordinates(gpx_content: str) -> Dict[str, Any]:
        """Parse GPX content and return features and points."""
        try:
            ns = GPXParser.NAMESPACES
            root = ET.fromstring(gpx_content)

            metadata: Dict[str, Any] = {}
            meta_elem = root.find('gpx:metadata', ns)
            if meta_elem is not None:
                name_elem = meta_elem.find('gpx:name', ns)
                if name_elem is not None:
                    metadata['title'] = name_elem.text
                desc_elem = meta_elem.find('gpx:desc', ns)
                if desc_elem is not None:
                    metadata['description'] = desc_elem.text
                time_elem = meta_elem.find('gpx:time', ns)
                if time_elem is not None:
                    metadata['time'] = time_elem.text

            features: List[Dict[str, Any]] = []
            points: List[Dict[str, Any]] = []

            # Waypoints
            for wpt in root.findall('gpx:wpt', ns):
                lat = float(wpt.get('lat'))
                lon = float(wpt.get('lon'))
                ele_elem = wpt.find('gpx:ele', ns)
                ele = float(ele_elem.text) if ele_elem is not None else 0.0
                name_elem = wpt.find('gpx:name', ns)
                desc_elem = wpt.find('gpx:desc', ns)

                parsed_info = GPXParser._build_parsed_info(lat, lon, ele)
                marker = {
                    'type': 'marker',
                    'name': name_elem.text if name_elem is not None else 'Waypoint',
                    'description': desc_elem.text if desc_elem is not None else '',
                    'coordinates': [lat, lon],
                    'altitude': ele,
                    'style': None,
                    'visibility': "1",
                    'time_info': {},
                    'parsed_info': parsed_info,
                    'properties': {}
                }
                features.append(marker)
                points.append(marker)

            # Tracks
            for trk in root.findall('gpx:trk', ns):
                trk_name_elem = trk.find('gpx:name', ns)
                trk_desc_elem = trk.find('gpx:desc', ns)
                trk_name = trk_name_elem.text if trk_name_elem is not None else 'Track'
                trk_desc = trk_desc_elem.text if trk_desc_elem is not None else ''

                coordinates: List[List[float]] = []
                timestamps: List[str] = []
                prev = None

                for seg in trk.findall('gpx:trkseg', ns):
                    for idx, trkpt in enumerate(seg.findall('gpx:trkpt', ns)):
                        lat = float(trkpt.get('lat'))
                        lon = float(trkpt.get('lon'))
                        ele_elem = trkpt.find('gpx:ele', ns)
                        ele = float(ele_elem.text) if ele_elem is not None else 0.0
                        time_elem = trkpt.find('gpx:time', ns)
                        timestamp = time_elem.text if time_elem is not None else None

                        coordinates.append([lat, lon, ele])
                        timestamps.append(timestamp)

                        speed_kmh = None
                        if prev and prev['time'] and timestamp:
                            try:
                                t1 = datetime.fromisoformat(prev['time'].replace('Z', '+00:00'))
                                t2 = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                                dt = (t2 - t1).total_seconds()
                                if dt > 0:
                                    dist = TrajectoryAnalyzer.calculate_distance_between_points(
                                        [prev['lat'], prev['lon'], prev['ele']],
                                        [lat, lon, ele]
                                    )
                                    speed_kmh = dist / dt * 3.6
                            except Exception:
                                speed_kmh = None

                        parsed_info = GPXParser._build_parsed_info(lat, lon, ele, speed_kmh)
                        marker = {
                            'type': 'marker',
                            'name': f'trkpt_{len(points)}',
                            'description': '',
                            'coordinates': [lat, lon],
                            'altitude': ele,
                            'style': None,
                            'visibility': "0",
                            'time_info': {'timestamp': timestamp},
                            'parsed_info': parsed_info,
                            'properties': {}
                        }
                        features.append(marker)
                        points.append(marker)
                        prev = {'lat': lat, 'lon': lon, 'ele': ele, 'time': timestamp}

                polyline = {
                    'type': 'polyline',
                    'name': trk_name,
                    'description': trk_desc,
                    'coordinates': coordinates,
                    'style': None,
                    'time_info': {'timestamps': timestamps},
                    'extensions': {},
                    'properties': {}
                }
                features.append(polyline)

            return {
                'success': True,
                'features': features,
                'points': points,
                'metadata': metadata,
                'total_points': len(points)
            }
        except ET.ParseError as exc:
            return {'success': False, 'error': f'Erreur de parsing XML: {str(exc)}'}
        except Exception as exc:  # pragma: no cover - general catch
            return {'success': False, 'error': f'Erreur lors du traitement: {str(exc)}'}
