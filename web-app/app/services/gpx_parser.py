"""GPX parsing service."""
import logging
import math
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.services import file_formats, geo, xml_stream
from app.services.activity_stats import parse_timestamp
from app.services.timing_tools import track_time

logger = logging.getLogger(__name__)

GPX_NS = 'http://www.topografix.com/GPX/1/1'
# GPX 1.0 (anciens GPS et exports) : mêmes éléments pour ce qui est lu ici
GPX_NS_ALIASES = ('http://www.topografix.com/GPX/1/0',)


class _Budget:
    """Points encore acceptés pour le fichier (repères, points de trace et de route confondus)."""

    def __init__(self) -> None:
        self.left = file_formats.MAX_FILE_POINTS

    def take(self) -> None:
        if self.left <= 0:
            raise file_formats.TooManyPointsError()
        self.left -= 1


class GPXParser:
    """Simple GPX parser producing features compatible with the KML parser."""

    NAMESPACES = {'gpx': GPX_NS}

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
    def _apply_speeds(markers: List[Dict[str, Any]], timestamps: List[Optional[str]]) -> None:
        """
        Vitesse de chaque point de trace depuis le précédent (distance / temps écoulé), en
        un seul calcul vectorisé : une trace de plusieurs dizaines de milliers de points ne
        doit pas coûter un appel géodésique par point. Sans horodatage exploitable (absent,
        illisible, identique ou antérieur au précédent), le point reste sans vitesse.
        """
        if len(markers) < 2:
            return
        epochs = [parse_timestamp(stamp) for stamp in timestamps]
        t = np.array([math.nan if epoch is None else epoch for epoch in epochs], dtype=float)
        lat = np.array([marker['coordinates'][0] for marker in markers], dtype=float)
        lon = np.array([marker['coordinates'][1] for marker in markers], dtype=float)
        with np.errstate(divide='ignore', invalid='ignore'):
            dt = np.diff(t)
            speeds = np.where(dt > 0, geo.step_distances(lat, lon) / dt * 3.6, np.nan)
        for marker, speed_kmh in zip(markers[1:], speeds.tolist()):
            if math.isfinite(speed_kmh):
                marker['parsed_info'].update(speed_kmh=round(speed_kmh, 1), speed_kts=round(speed_kmh / 1.852, 1))

    @staticmethod
    def _text(parent: ET.Element, tag: str, default: Optional[str] = None) -> Optional[str]:
        elem = parent.find(f'gpx:{tag}', GPXParser.NAMESPACES)
        return elem.text if elem is not None else default

    @staticmethod
    def _position(point: ET.Element) -> Tuple[float, float, float]:
        """
        (lat, lon, altitude) d'un wpt, trkpt ou rtept. Lève ValueError si une coordonnée est
        absente ou n'est pas un nombre fini ; une altitude absente ou vide (<ele/>) vaut 0.
        """
        return (file_formats.finite_float(point.get('lat')), file_formats.finite_float(point.get('lon')),
                file_formats.altitude(GPXParser._text(point, 'ele')))

    @staticmethod
    def _waypoint(wpt: ET.Element) -> Dict[str, Any]:
        lat, lon, ele = GPXParser._position(wpt)
        return {
            'type': 'marker',
            'name': GPXParser._text(wpt, 'name', 'Waypoint'),
            'description': GPXParser._text(wpt, 'desc', ''),
            'coordinates': [lat, lon],
            'altitude': ele,
            'style': None,
            'visibility': "1",
            'time_info': {},
            'parsed_info': GPXParser._build_parsed_info(lat, lon, ele),
            'properties': {}
        }

    @staticmethod
    def _track_marker(index: int, lat: float, lon: float, ele: float, timestamp: Optional[str]) -> Dict[str, Any]:
        """
        Marqueur masqué d'un point de trace : le visualiseur en attend un par point (le k-ième
        marqueur des features est points[k]). Il y en a jusqu'à 200 000 par fichier : il ne
        porte que les champs lus par le visualiseur et l'analyse, pas ceux d'un repère
        (style, propriétés, altitude en pieds, copie des coordonnées, description brute).
        """
        return {
            'type': 'marker',
            'name': f'trkpt_{index}',
            'description': '',
            'coordinates': [lat, lon],
            'altitude': ele,
            'visibility': "0",
            'time_info': {'timestamp': timestamp},
            'parsed_info': {'speed_kmh': None, 'speed_kts': None, 'altitude_m': ele, 'heading': None},
        }

    @staticmethod
    def _line(name: Optional[str], description: Optional[str], coordinates: List[List[float]],
              timestamps: List[Optional[str]], activity_type: str = '') -> Dict[str, Any]:
        return {
            'type': 'polyline',
            'name': name,
            'description': description,
            'coordinates': coordinates,
            'style': None,
            'time_info': {'timestamps': timestamps},
            'extensions': {},
            'properties': {'activity_type': activity_type} if activity_type else {}
        }

    @staticmethod
    @track_time
    def parse_gpx_coordinates(gpx_content: str) -> Dict[str, Any]:
        """
        Parse GPX content and return features and points.

        Le document est lu en flux (voir xml_stream) : chaque point est traité puis libéré
        dès qu'il se referme, l'arbre complet n'existe jamais en mémoire.
        """
        try:
            stream = _GpxStream()
            xml_stream.parse(gpx_content, stream)
            features, points = stream.assemble()
            metadata = stream.metadata()
            return {
                'success': True,
                'features': features,
                'points': points,
                'metadata': metadata,
                'total_points': len(points)
            }
        except ET.ParseError as exc:
            return {'success': False, 'error': f'Erreur de parsing XML: {str(exc)}'}
        except file_formats.TooManyPointsError as exc:
            return {'success': False, 'error': str(exc)}
        except (ValueError, TypeError, AttributeError, ArithmeticError) as exc:
            # Coordonnées absentes, non numériques ou non finies, entités interdites (defusedxml)…
            logger.info("GPX refusé : %s: %s", exc.__class__.__name__, str(exc)[:200])
            return {'success': False, 'error': 'Fichier GPX invalide'}


# --------------------------------------------------------------------- Lecture en flux

def _gpx(name: str) -> str:
    return f'{{{GPX_NS}}}{name}'


_WPT, _RTE, _RTEPT, _TRK, _TRKSEG, _TRKPT = (_gpx(name) for name in ('wpt', 'rte', 'rtept', 'trk', 'trkseg', 'trkpt'))
_METADATA, _ELE, _TIME = _gpx('metadata'), _gpx('ele'), _gpx('time')
# Élément enfant -> clé retenue, selon ce qui le contient (premier rencontré seulement)
_DOCUMENT_KEYS = {_gpx('name'): 'title', _gpx('desc'): 'description', _gpx('time'): 'time'}
_LINE_KEYS = {_gpx('name'): 'name', _gpx('desc'): 'desc', _gpx('type'): 'type'}
_CHILD_KEYS = {'root': _DOCUMENT_KEYS, 'metadata': _DOCUMENT_KEYS, 'rte': _LINE_KEYS, 'trk': _LINE_KEYS}
# Points reçus en sous-arbre, selon l'élément qui les contient directement
_POINTS = {'root': _WPT, 'rte': _RTEPT, 'trkseg': _TRKPT}


class _GpxStream(xml_stream.StreamHandler):
    """
    Relève repères, routes et traces au fil de la lecture ; `assemble` les range ensuite dans
    l'ordre attendu par le visualiseur (repères, routes, puis chaque trace précédée de ses
    points), quel que soit leur ordre dans le fichier.

    Seuls comptent les éléments à leur place : <wpt>, <rte> et <trk> sous la racine, <rtept>
    sous une route, <trkpt> sous un <trkseg> de trace.
    """

    canonical_ns = GPX_NS
    ns_aliases = GPX_NS_ALIASES

    def __init__(self) -> None:
        self._budget = _Budget()
        self._waypoints: List[Dict[str, Any]] = []
        self._routes: List[Dict[str, Any]] = []
        self._tracks: List[Dict[str, Any]] = []
        self._document: Dict[str, Any] = {}   # GPX 1.0 : nom, description et date sous la racine
        self._metadata: Optional[Dict[str, Any]] = None  # premier <metadata>
        # Un contexte par élément ouvert hors sous-arbre : (genre, données) ou None
        self._contexts: List[Any] = []

    def opened(self, element: ET.Element) -> bool:
        tag = element.tag
        parent = self._contexts[-1] if self._contexts else None
        if not self._contexts:
            context: Any = ('root', self._document)
        elif parent is None:
            context = None
        elif _POINTS.get(parent[0]) == tag:
            return True  # point : reçu entier par subtree()
        elif parent[0] == 'root' and tag in (_RTE, _TRK):
            context = ('rte' if tag == _RTE else 'trk', {'coordinates': [], 'timestamps': []})
        elif parent[0] == 'root' and tag == _METADATA and self._metadata is None:
            self._metadata = {}
            context = ('metadata', self._metadata)
        elif parent[0] == 'trk' and tag == _TRKSEG:
            context = ('trkseg', parent[1])  # les points vont à la trace
        else:
            context = None
        self._contexts.append(context)
        return False

    def subtree(self, element: ET.Element) -> None:
        kind, data = self._contexts[-1]
        self._budget.take()
        if kind == 'root':
            self._waypoints.append(GPXParser._waypoint(element))
            return
        # Premier <ele> et premier <time> du point, lus en un parcours de ses enfants : deux
        # recherches par point pèsent sur une trace de 200 000 points
        ele = stamp = None
        seen_ele = seen_time = False
        for child in element:
            tag = child.tag
            if tag == _ELE and not seen_ele:
                ele, seen_ele = child.text, True
            elif tag == _TIME and not seen_time:
                stamp, seen_time = child.text, True
        data['coordinates'].append([file_formats.finite_float(element.get('lat')),
                                    file_formats.finite_float(element.get('lon')), file_formats.altitude(ele)])
        if kind == 'trkseg':
            data['timestamps'].append(stamp)

    def closed(self, element: ET.Element) -> None:
        context = self._contexts.pop()
        if context is not None and context[0] in ('rte', 'trk'):
            kind, data = context
            if not data['coordinates']:
                # Une route ou une trace vide compte pour un point : le plafond borne aussi
                # le nombre de lignes (des millions de <trk/> tiennent dans 16 Mo)
                self._budget.take()
            if kind == 'trk':
                self._tracks.append(data)
            elif data['coordinates']:
                self._routes.append(data)
        parent = self._contexts[-1] if self._contexts else None
        if parent is not None:
            key = _CHILD_KEYS.get(parent[0], {}).get(element.tag)
            if key is not None:
                parent[1].setdefault(key, element.text)

    def metadata(self) -> Dict[str, Any]:
        """Titre, description et date : ceux de <metadata>, à défaut ceux de la racine (GPX 1.0)."""
        metadata = dict(self._metadata or {})
        for key, value in self._document.items():
            metadata.setdefault(key, value)
        return metadata

    def assemble(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """(features, points) dans l'ordre attendu : le k-ième marqueur des features est points[k]."""
        features: List[Dict[str, Any]] = list(self._waypoints)
        points: List[Dict[str, Any]] = list(self._waypoints)
        # Routes : itinéraires sans mesure. Tracées comme une ligne, sans marqueur par
        # point (rien à y lire) ni horodatage.
        for route in self._routes:
            features.append(GPXParser._line(route.get('name', 'Route'), route.get('desc', ''), route['coordinates'], []))
        for track in self._tracks:
            coordinates, timestamps = track['coordinates'], track['timestamps']
            markers = [GPXParser._track_marker(len(points) + position, lat, lon, ele, timestamp)
                       for position, ((lat, lon, ele), timestamp) in enumerate(zip(coordinates, timestamps))]
            GPXParser._apply_speeds(markers, timestamps)
            features.extend(markers)
            points.extend(markers)
            features.append(GPXParser._line(track.get('name', 'Track'), track.get('desc', ''), coordinates,
                                            timestamps, (track.get('type') or '').strip()))
        return features, points
