"""
Simplification de traces et export multi-formats (GPX, CSV, GeoJSON, KML).

L'export conserve ce que le visualiseur a chargé : repères, traces (y compris gx:Track et
gx:MultiTrack), horodatages par point et polygones avec leurs trous.
"""

import csv
import io
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import numpy as np

from app.services import file_formats, geo
from app.services.activity_stats import iter_lines
from app.services.timing_tools import track_time

logger = logging.getLogger(__name__)

KML_NS = 'http://www.opengis.net/kml/2.2'
GX_NS = 'http://www.google.com/kml/ext/2.2'
METERS_PER_DEGREE = 111320.0
# Douglas-Peucker coûte n·log(n) sur une trace ordinaire, mais n² sur une trace construite
# pour (zigzag d'amplitude décroissante : un seul point retiré par découpe). Le travail est
# donc plafonné à ce multiple du nombre de points : ~10 fois le besoin d'une vraie trace.
SIMPLIFY_WORK_FACTOR = 200


def _is_waypoint(feature: Dict[str, Any]) -> bool:
    """Point à exporter : les points de trace GPX (marqueurs masqués) sont déjà dans la trace."""
    return feature.get('type') == 'marker' and str(feature.get('visibility', '1')) != '0'


def _lines(features: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], list, List[Optional[str]]]]:
    """(feature, coordonnées, horodatages alignés ou liste vide) pour chaque trace, multi-traces aplaties."""
    for feature, coords, stamps in iter_lines(features):
        if coords:
            yield feature, coords, (stamps if isinstance(stamps, list) and len(stamps) == len(coords) else [])


def _polygons(features: List[Dict[str, Any]]) -> Iterator[Tuple[Dict[str, Any], List[list]]]:
    """(feature, anneaux) pour chaque polygone : contour extérieur puis trous."""
    for feature in features:
        if feature.get('type') == 'polygon' and len(feature.get('coordinates') or []) >= 3:
            yield feature, [feature['coordinates'], *(feature.get('holes') or [])]


def _altitude(coord: list) -> Optional[float]:
    return coord[2] if len(coord) > 2 and coord[2] is not None else None


def _timestamp(feature: Dict[str, Any]) -> Optional[str]:
    time_info = feature.get('time_info')
    return (time_info.get('timestamp') if isinstance(time_info, dict) else None) or None


def _text(parent: ET.Element, tag: str, value: Any) -> None:
    """Sous-élément texte, omis si la valeur est vide."""
    if value not in (None, ''):
        ET.SubElement(parent, tag).text = str(value)


def _csv_text(value: Any) -> str:
    """
    Texte libre destiné à un tableur : une cellule commençant par = + - @ (ou tabulation,
    retour chariot) y serait évaluée comme une formule. L'apostrophe la force en texte.
    """
    text = '' if value is None else str(value)
    return "'" + text if text[:1] in ('=', '+', '-', '@', '\t', '\r') else text


class KMLEditor:
    """Simplification et export des features du visualiseur."""

    @staticmethod
    @track_time
    def simplify_indices(coordinates: List[List[float]], tolerance: float = 0.0001) -> List[int]:
        """
        Indices des points conservés par une simplification de Douglas-Peucker.

        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            tolerance: Écart maximal toléré, en degrés de latitude (1° ≈ 111 km). L'écart est
                mesuré en mètres dans un plan local : il vaut autant d'est en ouest que du nord au sud.

        Lève TooManyPointsError (un ValueError) au-delà du nombre de points accepté par fichier.
        """
        count = len(coordinates)
        if count > file_formats.MAX_FILE_POINTS:
            raise file_formats.TooManyPointsError()
        if count <= 2:
            return list(range(count))
        lat = np.array([c[0] for c in coordinates], dtype=float)
        lon = np.array([c[1] for c in coordinates], dtype=float)
        xy = geo.to_local_xy(lat, lon, float(lat.mean()), float(lon.mean()))
        limit = tolerance * METERS_PER_DEGREE

        # Version itérative : une trace de plusieurs milliers de points ferait déborder la pile
        keep = np.zeros(count, dtype=bool)
        keep[0] = keep[-1] = True
        stack = [(0, count - 1)]
        budget = SIMPLIFY_WORK_FACTOR * count  # points encore examinables, toutes découpes confondues
        while stack:
            first, last = stack.pop()
            if last - first < 2:
                continue
            if budget <= 0:
                # Budget épuisé : le reste est conservé tel quel. Le résultat reste une
                # simplification valide (aucun écart au-delà de la tolérance), seulement moins poussée.
                keep[first:last + 1] = True
                continue
            budget -= last - first
            start, delta = xy[first], xy[last] - xy[first]
            offsets = xy[first + 1:last] - start
            length_sq = float(delta @ delta)
            if length_sq == 0:
                distances = np.hypot(offsets[:, 0], offsets[:, 1])
            else:
                ratio = np.clip(offsets @ delta / length_sq, 0.0, 1.0)
                nearest = offsets - np.outer(ratio, delta)
                distances = np.hypot(nearest[:, 0], nearest[:, 1])
            farthest = int(distances.argmax())
            if distances[farthest] > limit:
                index = first + 1 + farthest
                keep[index] = True
                stack.append((first, index))
                stack.append((index, last))
        if budget <= 0:
            logger.warning("Simplification interrompue (trace de %d points trop coûteuse) : fin de trace conservée telle quelle",
                           count)
        return np.flatnonzero(keep).tolist()

    @staticmethod
    def simplify_trace(coordinates: List[List[float]], tolerance: float = 0.0001) -> List[List[float]]:
        """Trace simplifiée (Douglas-Peucker) : la forme est préservée à la tolérance près."""
        return [coordinates[i] for i in KMLEditor.simplify_indices(coordinates, tolerance)]

    @staticmethod
    @track_time
    def export_to_gpx(features: List[Dict[str, Any]], metadata: Dict[str, Any] = None) -> str:
        """GPX 1.1 : repères en <wpt>, polygones en <rte> fermées (GPX n'a pas de surface), traces en <trk>."""
        gpx = ET.Element('gpx', {'version': '1.1', 'creator': 'KmlViewer',
                                 'xmlns': 'http://www.topografix.com/GPX/1/1'})
        if metadata:
            metadata_elem = ET.SubElement(gpx, 'metadata')
            _text(metadata_elem, 'name', metadata.get('name'))
            _text(metadata_elem, 'desc', metadata.get('description'))
            _text(metadata_elem, 'time', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))

        def point(parent: ET.Element, tag: str, coord: list, stamp: Optional[str] = None) -> ET.Element:
            # Ordre imposé par le schéma : ele, time, puis name/desc
            node = ET.SubElement(parent, tag, {'lat': str(coord[0]), 'lon': str(coord[1])})
            _text(node, 'ele', _altitude(coord))
            _text(node, 'time', stamp)
            return node

        for feature in features:
            if _is_waypoint(feature):
                coords = feature['coordinates']
                wpt = point(gpx, 'wpt', [coords[0], coords[1], feature.get('altitude') or None], _timestamp(feature))
                _text(wpt, 'name', feature.get('name') or 'Point')
                _text(wpt, 'desc', feature.get('description'))

        for feature, rings in _polygons(features):
            for position, ring in enumerate(rings):
                rte = ET.SubElement(gpx, 'rte')
                suffix = '' if position == 0 else f' (trou {position})'
                _text(rte, 'name', f"{feature.get('name') or 'Polygone'}{suffix}")
                _text(rte, 'desc', feature.get('description'))
                for coord in ring:
                    point(rte, 'rtept', coord)

        for number, (feature, coords, stamps) in enumerate(_lines(features), start=1):
            trk = ET.SubElement(gpx, 'trk')
            _text(trk, 'name', feature.get('name') or f'Track {number}')
            _text(trk, 'desc', feature.get('description'))
            _text(trk, 'type', (feature.get('properties') or {}).get('activity_type'))
            trkseg = ET.SubElement(trk, 'trkseg')
            for position, coord in enumerate(coords):
                point(trkseg, 'trkpt', coord, stamps[position] if stamps else None)

        return ET.tostring(gpx, encoding='unicode', xml_declaration=True)

    @staticmethod
    @track_time
    def export_to_csv(features: List[Dict[str, Any]], include_traces: bool = True) -> str:
        """CSV : une ligne par repère, puis (si demandé) par point de trace et par sommet de polygone."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Type', 'Name', 'Description', 'Latitude', 'Longitude', 'Altitude',
                         'Time', 'Speed_KMH', 'Speed_KTS'])

        for feature in features:
            if _is_waypoint(feature):
                coords = feature['coordinates']
                parsed_info = feature.get('parsed_info') or {}
                speed_kmh, speed_kts = parsed_info.get('speed_kmh'), parsed_info.get('speed_kts')
                writer.writerow(['Point', _csv_text(feature.get('name', '')), _csv_text(feature.get('description', '')),
                                 coords[0], coords[1], feature.get('altitude', 0), _csv_text(_timestamp(feature)),
                                 '' if speed_kmh is None else speed_kmh, '' if speed_kts is None else speed_kts])

        if include_traces:
            for feature, coords, stamps in _lines(features):
                name = feature.get('name') or 'Trace'
                for i, coord in enumerate(coords):
                    altitude = _altitude(coord)
                    writer.writerow(['TracePoint', _csv_text(f"{name} - Point {i + 1}"),
                                     _csv_text(feature.get('description', '')),
                                     coord[0], coord[1], 0 if altitude is None else altitude,
                                     _csv_text(stamps[i]) if stamps else '', '', ''])
            for feature, rings in _polygons(features):
                name = feature.get('name') or 'Polygone'
                for position, ring in enumerate(rings):
                    kind = 'PolygonPoint' if position == 0 else 'PolygonHolePoint'
                    for i, coord in enumerate(ring):
                        altitude = _altitude(coord)
                        writer.writerow([kind, _csv_text(f"{name} - Point {i + 1}"), _csv_text(feature.get('description', '')),
                                         coord[0], coord[1], 0 if altitude is None else altitude, '', '', ''])
        return output.getvalue()

    @staticmethod
    @track_time
    def export_to_geojson(features: List[Dict[str, Any]]) -> str:
        """GeoJSON : Point, LineString (horodatages dans `coordTimes`) et Polygon avec trous."""
        def position(coord: list) -> list:
            # GeoJSON utilise [lon, lat, alt]
            altitude = _altitude(coord)
            return [coord[1], coord[0]] if not altitude else [coord[1], coord[0], altitude]

        def entry(geometry_type: str, coordinates: list, feature: Dict[str, Any], **properties: Any) -> Dict[str, Any]:
            return {'type': 'Feature',
                    'geometry': {'type': geometry_type, 'coordinates': coordinates},
                    'properties': {'name': feature.get('name', ''), 'description': feature.get('description', ''),
                                   **{key: value for key, value in properties.items() if value is not None}}}

        collection: List[Dict[str, Any]] = []
        for feature in features:
            if _is_waypoint(feature):
                coords = feature['coordinates']
                parsed_info = feature.get('parsed_info') or {}
                collection.append(entry('Point', position([coords[0], coords[1], feature.get('altitude')]), feature,
                                        time=_timestamp(feature), speed_kmh=parsed_info.get('speed_kmh'),
                                        speed_kts=parsed_info.get('speed_kts')))
        for feature, coords, stamps in _lines(features):
            collection.append(entry('LineString', [position(c) for c in coords], feature, coordTimes=stamps or None))
        for feature, rings in _polygons(features):
            collection.append(entry('Polygon', [[position(c) for c in ring] for ring in rings], feature))
        return json.dumps({'type': 'FeatureCollection', 'features': collection}, indent=2)

    @staticmethod
    @track_time
    def export_to_kml(features: List[Dict[str, Any]], metadata: Dict[str, Any] = None) -> str:
        """KML : une trace horodatée devient un gx:Track (temps conservé), sinon un LineString."""
        ET.register_namespace('', KML_NS)
        ET.register_namespace('gx', GX_NS)
        kml = ET.Element(f'{{{KML_NS}}}kml')
        document = ET.SubElement(kml, f'{{{KML_NS}}}Document')

        def node(parent: ET.Element, tag: str, value: Any = None, namespace: str = KML_NS) -> ET.Element:
            element = ET.SubElement(parent, f'{{{namespace}}}{tag}')
            if value is not None:
                element.text = str(value)
            return element

        def text(coord: list, separator: str = ',') -> str:
            # KML utilise lon, lat, alt
            altitude = _altitude(coord)
            return separator.join(str(v) for v in (coord[1], coord[0], 0 if altitude is None else altitude))

        def placemark(folder: ET.Element, feature: Dict[str, Any], default_name: str) -> ET.Element:
            element = node(folder, 'Placemark')
            node(element, 'name', feature.get('name') or default_name)
            if feature.get('description'):
                node(element, 'description', feature['description'])
            return element

        if metadata:
            if metadata.get('name'):
                node(document, 'name', metadata['name'])
            if metadata.get('description'):
                node(document, 'description', metadata['description'])

        folders = {title: node(document, 'Folder') for title in ('Points', 'Traces', 'Polygones')}
        for title, folder in folders.items():
            node(folder, 'name', title)

        for feature in features:
            if _is_waypoint(feature):
                element = placemark(folders['Points'], feature, 'Point')
                if _timestamp(feature):
                    node(node(element, 'TimeStamp'), 'when', _timestamp(feature))
                coords = feature['coordinates']
                node(node(element, 'Point'), 'coordinates', text([coords[0], coords[1], feature.get('altitude')]))

        for feature, coords, stamps in _lines(features):
            element = placemark(folders['Traces'], feature, 'Trace')
            if stamps and all(stamps):
                track = node(element, 'Track', namespace=GX_NS)
                for stamp in stamps:
                    node(track, 'when', stamp)
                for coord in coords:
                    node(track, 'coord', text(coord, ' '), namespace=GX_NS)
            else:
                node(node(element, 'LineString'), 'coordinates', ' '.join(text(c) for c in coords))

        for feature, rings in _polygons(features):
            polygon = node(placemark(folders['Polygones'], feature, 'Polygone'), 'Polygon')
            for position, ring in enumerate(rings):
                boundary = node(polygon, 'outerBoundaryIs' if position == 0 else 'innerBoundaryIs')
                node(node(boundary, 'LinearRing'), 'coordinates', ' '.join(text(c) for c in ring))

        for folder in folders.values():
            if len(folder) == 1:  # seulement son nom : dossier vide
                document.remove(folder)
        return ET.tostring(kml, encoding='unicode', xml_declaration=True)
