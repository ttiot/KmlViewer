"""
Simplification de traces et export multi-formats (GPX, CSV, GeoJSON, KML).
"""

import json
import csv
import io
import xml.etree.ElementTree as ET
from typing import List, Dict, Any
from datetime import datetime, timezone
from app.services.timing_tools import track_time


# Features linéaires à coordonnées plates [lat, lon, alt]
LINE_TYPES = ('polyline', 'track')


def _is_waypoint(feature: Dict[str, Any]) -> bool:
    """Point à exporter : les points de trace GPX (marqueurs masqués) sont déjà dans la trace."""
    return feature.get('type') == 'marker' and str(feature.get('visibility', '1')) != '0'


class KMLEditor:
    """Simplification et export des features du visualiseur."""

    @staticmethod
    @track_time
    def simplify_indices(coordinates: List[List[float]], tolerance: float = 0.0001) -> List[int]:
        """
        Indices des points conservés par une simplification de Douglas-Peucker.

        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            tolerance: Écart maximal toléré, en degrés
        """
        count = len(coordinates)
        if count <= 2:
            return list(range(count))

        def distance_to_segment(point, start, end):
            dx, dy = end[0] - start[0], end[1] - start[1]
            length_sq = dx * dx + dy * dy
            if length_sq == 0:
                return ((point[0] - start[0]) ** 2 + (point[1] - start[1]) ** 2) ** 0.5
            ratio = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_sq
            ratio = max(0.0, min(1.0, ratio))
            return ((point[0] - start[0] - ratio * dx) ** 2 + (point[1] - start[1] - ratio * dy) ** 2) ** 0.5

        # Version itérative : une trace de plusieurs milliers de points ferait déborder la pile
        keep = [False] * count
        keep[0] = keep[-1] = True
        stack = [(0, count - 1)]
        while stack:
            first, last = stack.pop()
            max_distance, max_index = 0.0, -1
            for i in range(first + 1, last):
                distance = distance_to_segment(coordinates[i], coordinates[first], coordinates[last])
                if distance > max_distance:
                    max_distance, max_index = distance, i
            if max_index != -1 and max_distance > tolerance:
                keep[max_index] = True
                stack.append((first, max_index))
                stack.append((max_index, last))
        return [i for i, kept in enumerate(keep) if kept]

    @staticmethod
    def simplify_trace(coordinates: List[List[float]], tolerance: float = 0.0001) -> List[List[float]]:
        """Trace simplifiée (Douglas-Peucker) : la forme est préservée à la tolérance près."""
        return [coordinates[i] for i in KMLEditor.simplify_indices(coordinates, tolerance)]

    @staticmethod
    @track_time
    def export_to_gpx(features: List[Dict[str, Any]],
                     metadata: Dict[str, Any] = None) -> str:
        """
        Exporte les données au format GPX.

        Args:
            features: Liste des features KML
            metadata: Métadonnées optionnelles

        Returns:
            Contenu GPX au format string
        """
        # Créer l'élément racine GPX
        gpx = ET.Element('gpx')
        gpx.set('version', '1.1')
        gpx.set('creator', 'KmlViewer')
        gpx.set('xmlns', 'http://www.topografix.com/GPX/1/1')

        # Ajouter les métadonnées
        if metadata:
            metadata_elem = ET.SubElement(gpx, 'metadata')
            if 'name' in metadata:
                name_elem = ET.SubElement(metadata_elem, 'name')
                name_elem.text = metadata['name']
            if 'description' in metadata:
                desc_elem = ET.SubElement(metadata_elem, 'desc')
                desc_elem.text = metadata['description']

            time_elem = ET.SubElement(metadata_elem, 'time')
            time_elem.text = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # Ajouter les waypoints (points)
        for feature in features:
            if _is_waypoint(feature):
                wpt = ET.SubElement(gpx, 'wpt')
                coords = feature.get('coordinates', [0, 0])
                wpt.set('lat', str(coords[0]))
                wpt.set('lon', str(coords[1]))

                if feature.get('altitude'):
                    ele = ET.SubElement(wpt, 'ele')
                    ele.text = str(feature['altitude'])

                name = ET.SubElement(wpt, 'name')
                name.text = feature.get('name', 'Point')

                if feature.get('description'):
                    desc = ET.SubElement(wpt, 'desc')
                    desc.text = feature['description']

        # Ajouter les traces (tracks)
        track_count = 0
        for feature in features:
            if feature.get('type') in LINE_TYPES:
                track_count += 1
                trk = ET.SubElement(gpx, 'trk')

                name = ET.SubElement(trk, 'name')
                name.text = feature.get('name', f'Track {track_count}')

                if feature.get('description'):
                    desc = ET.SubElement(trk, 'desc')
                    desc.text = feature['description']

                trkseg = ET.SubElement(trk, 'trkseg')

                coordinates = feature.get('coordinates', [])
                for coord in coordinates:
                    trkpt = ET.SubElement(trkseg, 'trkpt')
                    trkpt.set('lat', str(coord[0]))
                    trkpt.set('lon', str(coord[1]))

                    if len(coord) > 2 and coord[2] != 0:
                        ele = ET.SubElement(trkpt, 'ele')
                        ele.text = str(coord[2])

        # Convertir en string
        return ET.tostring(gpx, encoding='unicode', xml_declaration=True)

    @staticmethod
    @track_time
    def export_to_csv(features: List[Dict[str, Any]],
                     include_traces: bool = True) -> str:
        """
        Exporte les données au format CSV.

        Args:
            features: Liste des features KML
            include_traces: Inclure les points des traces

        Returns:
            Contenu CSV au format string
        """
        output = io.StringIO()
        writer = csv.writer(output)

        # En-têtes
        headers = ['Type', 'Name', 'Description', 'Latitude', 'Longitude', 'Altitude',
                  'Is_Annotation', 'Speed_KMH', 'Speed_KTS']
        writer.writerow(headers)

        # Points (waypoints)
        for feature in features:
            if _is_waypoint(feature):
                coords = feature.get('coordinates', [0, 0])
                parsed_info = feature.get('parsed_info', {})

                row = [
                    'Point',
                    feature.get('name', ''),
                    feature.get('description', ''),
                    coords[0] if len(coords) > 0 else 0,
                    coords[1] if len(coords) > 1 else 0,
                    feature.get('altitude', 0),
                    feature.get('is_annotation', False),
                    parsed_info.get('speed_kmh', ''),
                    parsed_info.get('speed_kts', '')
                ]
                writer.writerow(row)

        # Points des traces si demandé
        if include_traces:
            for feature in features:
                if feature.get('type') in LINE_TYPES:
                    coordinates = feature.get('coordinates', [])
                    for i, coord in enumerate(coordinates):
                        row = [
                            'TracePoint',
                            f"{feature.get('name', 'Trace')} - Point {i+1}",
                            feature.get('description', ''),
                            coord[0] if len(coord) > 0 else 0,
                            coord[1] if len(coord) > 1 else 0,
                            coord[2] if len(coord) > 2 else 0,
                            False,
                            '',
                            ''
                        ]
                        writer.writerow(row)

        content = output.getvalue()
        output.close()
        return content

    @staticmethod
    @track_time
    def export_to_geojson(features: List[Dict[str, Any]]) -> str:
        """
        Exporte les données au format GeoJSON.

        Args:
            features: Liste des features KML

        Returns:
            Contenu GeoJSON au format string
        """
        geojson = {
            "type": "FeatureCollection",
            "features": []
        }

        for feature in features:
            if _is_waypoint(feature):
                # Point GeoJSON
                geojson_feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": []
                    },
                    "properties": {
                        "name": feature.get('name', ''),
                        "description": feature.get('description', ''),
                        "is_annotation": feature.get('is_annotation', False)
                    }
                }

                coords = feature.get('coordinates', [0, 0])
                altitude = feature.get('altitude', 0)

                # GeoJSON utilise [lon, lat, alt]
                if altitude != 0:
                    geojson_feature["geometry"]["coordinates"] = [coords[1], coords[0], altitude]
                else:
                    geojson_feature["geometry"]["coordinates"] = [coords[1], coords[0]]

                # Ajouter les informations de vitesse si disponibles
                parsed_info = feature.get('parsed_info', {})
                if parsed_info.get('speed_kmh') is not None:
                    geojson_feature["properties"]["speed_kmh"] = parsed_info['speed_kmh']
                if parsed_info.get('speed_kts') is not None:
                    geojson_feature["properties"]["speed_kts"] = parsed_info['speed_kts']

                geojson["features"].append(geojson_feature)

            elif feature.get('type') in LINE_TYPES:
                # LineString GeoJSON
                geojson_feature = {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": []
                    },
                    "properties": {
                        "name": feature.get('name', ''),
                        "description": feature.get('description', '')
                    }
                }

                coordinates = feature.get('coordinates', [])
                for coord in coordinates:
                    # GeoJSON utilise [lon, lat, alt]
                    if len(coord) > 2 and coord[2] != 0:
                        geojson_feature["geometry"]["coordinates"].append([coord[1], coord[0], coord[2]])
                    else:
                        geojson_feature["geometry"]["coordinates"].append([coord[1], coord[0]])

                geojson["features"].append(geojson_feature)

        return json.dumps(geojson, indent=2)

    @staticmethod
    @track_time
    def export_to_kml(features: List[Dict[str, Any]],
                     metadata: Dict[str, Any] = None) -> str:
        """
        Exporte les données au format KML.

        Args:
            features: Liste des features
            metadata: Métadonnées optionnelles

        Returns:
            Contenu KML au format string
        """
        # Créer l'élément racine KML
        kml = ET.Element('kml')
        kml.set('xmlns', 'http://www.opengis.net/kml/2.2')

        document = ET.SubElement(kml, 'Document')

        # Ajouter les métadonnées
        if metadata:
            if 'name' in metadata:
                name = ET.SubElement(document, 'name')
                name.text = metadata['name']
            if 'description' in metadata:
                desc = ET.SubElement(document, 'description')
                desc.text = metadata['description']

        # Créer les dossiers pour organiser les éléments
        points_folder = ET.SubElement(document, 'Folder')
        points_name = ET.SubElement(points_folder, 'name')
        points_name.text = 'Points'

        traces_folder = ET.SubElement(document, 'Folder')
        traces_name = ET.SubElement(traces_folder, 'name')
        traces_name.text = 'Traces'

        # Ajouter les points
        for feature in features:
            if _is_waypoint(feature):
                placemark = ET.SubElement(points_folder, 'Placemark')

                name = ET.SubElement(placemark, 'name')
                name.text = feature.get('name', 'Point')

                if feature.get('description'):
                    desc = ET.SubElement(placemark, 'description')
                    desc.text = feature['description']

                point = ET.SubElement(placemark, 'Point')
                coordinates = ET.SubElement(point, 'coordinates')

                coords = feature.get('coordinates', [0, 0])
                altitude = feature.get('altitude', 0)
                coordinates.text = f"{coords[1]},{coords[0]},{altitude}"

        # Ajouter les traces
        for feature in features:
            if feature.get('type') in LINE_TYPES:
                placemark = ET.SubElement(traces_folder, 'Placemark')

                name = ET.SubElement(placemark, 'name')
                name.text = feature.get('name', 'Trace')

                if feature.get('description'):
                    desc = ET.SubElement(placemark, 'description')
                    desc.text = feature['description']

                linestring = ET.SubElement(placemark, 'LineString')
                coordinates = ET.SubElement(linestring, 'coordinates')

                coord_list = []
                for coord in feature.get('coordinates', []):
                    if len(coord) > 2:
                        coord_list.append(f"{coord[1]},{coord[0]},{coord[2]}")
                    else:
                        coord_list.append(f"{coord[1]},{coord[0]},0")

                coordinates.text = ' '.join(coord_list)

        # Convertir en string
        return ET.tostring(kml, encoding='unicode', xml_declaration=True)
