"""
Service de parsing des fichiers KML.
Extrait toutes les informations pertinentes des fichiers KML, y compris les extensions Google.
"""

import re
import xml.etree.ElementTree as ET
from defusedxml.ElementTree import fromstring as safe_fromstring  # anti XXE / bombes d'entités
import logging
from app.services.timing_tools import track_time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Vitesse dans la description d'un point : après un mot-clé, ou seule en début de ligne.
# Un nombre suivi d'une unité au milieu d'une phrase (« vent de 20 km/h ») n'est pas une mesure.
_NUMBER = r'(\d+(?:[.,]\d+)?)'
_KMH, _KTS = r'(?:km/h|kmh|kph)', r'(?:kts|kt|knots?|n(?:oe|œ)uds?)'
_KEYWORD = r'(?:vitesse|speed|spd|gs)\s*[:=]?\s*'
_LINE_START = r'(?:^|<br\s*/?>|\n|[;|])\s*'
SPEED_PATTERNS = tuple((re.compile(prefix + _NUMBER + r'\s*' + unit + r'(?![\w/])', re.IGNORECASE), knots)
                       for prefix in (_KEYWORD, _LINE_START)
                       for unit, knots in ((_KMH, False), (_KTS, True)))
HEADING_PATTERN = re.compile(r'\b(?:cap|heading|hdg|course|direction|dir)\s*[:=]?\s*' + _NUMBER
                             + r'\s*(?:°|deg(?:rees?|rés?)?)?(?![\w/.,])', re.IGNORECASE)

class KMLParser:
    """Service de parsing complet des fichiers KML avec support des extensions Google."""

    # Namespaces KML standards et extensions
    NAMESPACES = {
        'kml': 'http://www.opengis.net/kml/2.2',
        'gx': 'http://www.google.com/kml/ext/2.2',
        'atom': 'http://www.w3.org/2005/Atom'
    }

    @staticmethod
    @track_time
    def parse(kml_content: str) -> Dict[str, Any]:
        """
        Parse un fichier KML et extrait toutes les informations disponibles.

        Args:
            kml_content: Contenu du fichier KML

        Returns:
            dict: Structure complète avec success, features, metadata et error
        """
        try:
            root = safe_fromstring(kml_content)

            # Extraire les métadonnées globales
            metadata = KMLParser._extract_metadata(root)

            # Extraire toutes les entités géographiques
            features = KMLParser._extract_features(root)

            return {
                'success': True,
                'features': features,
                'metadata': metadata
            }

        except ET.ParseError as e:
            return {
                'success': False,
                'error': f'Erreur de parsing XML: {str(e)}'
            }
        except (ValueError, AttributeError, TypeError) as e:
            return {
                'success': False,
                'error': f'Erreur lors du traitement: {str(e)}'
            }

    @staticmethod
    def _extract_metadata(root: ET.Element) -> Dict[str, Any]:
        """Extrait toutes les métadonnées du document KML."""
        metadata = {}
        ns = KMLParser.NAMESPACES

        # Document principal
        document = root.find('.//kml:Document', ns)
        if document is not None:
            # Nom du document
            name_elem = document.find('kml:name', ns)
            if name_elem is not None:
                metadata['title'] = name_elem.text

            # Description du document
            desc_elem = document.find('kml:description', ns)
            if desc_elem is not None:
                metadata['description'] = desc_elem.text

            # Auteur (atom:author)
            author_elem = document.find('atom:author', ns)
            if author_elem is not None:
                author_name = author_elem.find('atom:name', ns)
                if author_name is not None:
                    metadata['author'] = author_name.text

            # Liens (atom:link)
            links = document.findall('atom:link', ns)
            if links:
                metadata['links'] = []
                for link in links:
                    link_data = {}
                    if 'href' in link.attrib:
                        link_data['href'] = link.attrib['href']
                    if 'rel' in link.attrib:
                        link_data['rel'] = link.attrib['rel']
                    if 'type' in link.attrib:
                        link_data['type'] = link.attrib['type']
                    metadata['links'].append(link_data)

        # Informations de temps (TimeStamp, TimeSpan)
        KMLParser._extract_time_info(root, metadata)

        # Extensions Google
        KMLParser._extract_google_extensions(root, metadata)

        # Styles définis
        KMLParser._extract_styles(root, metadata)

        # Informations sur les dossiers
        KMLParser._extract_folder_structure(root, metadata)

        return metadata

    @staticmethod
    def _extract_time_info(root: ET.Element, metadata: Dict[str, Any]):
        """Extrait les informations temporelles."""
        ns = KMLParser.NAMESPACES

        # TimeStamp
        timestamps = root.findall('.//kml:TimeStamp', ns)
        if timestamps:
            metadata['timestamps'] = []
            for ts in timestamps:
                when_elem = ts.find('kml:when', ns)
                if when_elem is not None and when_elem.text:
                    metadata['timestamps'].append(when_elem.text)

        # TimeSpan
        timespans = root.findall('.//kml:TimeSpan', ns)
        if timespans:
            metadata['timespans'] = []
            for ts in timespans:
                timespan_data = {}
                begin_elem = ts.find('kml:begin', ns)
                end_elem = ts.find('kml:end', ns)
                if begin_elem is not None:
                    timespan_data['begin'] = begin_elem.text
                if end_elem is not None:
                    timespan_data['end'] = end_elem.text
                metadata['timespans'].append(timespan_data)

    @staticmethod
    def _extract_google_extensions(root: ET.Element, metadata: Dict[str, Any]):
        """Extrait les extensions Google (gx:*)."""
        ns = KMLParser.NAMESPACES
        extensions = {}

        # gx:Tour
        tours = root.findall('.//gx:Tour', ns)
        if tours:
            extensions['tours'] = []
            for tour in tours:
                tour_data = {}
                name_elem = tour.find('kml:name', ns)
                if name_elem is not None:
                    tour_data['name'] = name_elem.text
                desc_elem = tour.find('kml:description', ns)
                if desc_elem is not None:
                    tour_data['description'] = desc_elem.text
                extensions['tours'].append(tour_data)

        # gx:Track
        tracks = root.findall('.//gx:Track', ns)
        if tracks:
            extensions['track_count'] = len(tracks)

        # gx:MultiTrack
        multitracks = root.findall('.//gx:MultiTrack', ns)
        if multitracks:
            extensions['multitrack_count'] = len(multitracks)

        if extensions:
            metadata['google_extensions'] = extensions

    @staticmethod
    def _extract_styles(root: ET.Element, metadata: Dict[str, Any]):
        """Extrait les définitions de styles."""
        ns = KMLParser.NAMESPACES
        styles = {}

        # Style individuels
        style_elements = root.findall('.//kml:Style', ns)
        for style in style_elements:
            style_id = style.get('id')
            if style_id:
                style_data = {}

                # IconStyle
                icon_style = style.find('kml:IconStyle', ns)
                if icon_style is not None:
                    icon_elem = icon_style.find('kml:Icon', ns)
                    if icon_elem is not None:
                        href_elem = icon_elem.find('kml:href', ns)
                        if href_elem is not None:
                            style_data['icon'] = href_elem.text

                # LineStyle
                line_style = style.find('kml:LineStyle', ns)
                if line_style is not None:
                    color_elem = line_style.find('kml:color', ns)
                    width_elem = line_style.find('kml:width', ns)
                    if color_elem is not None:
                        style_data['line_color'] = color_elem.text
                    if width_elem is not None:
                        style_data['line_width'] = width_elem.text

                # PolyStyle
                poly_style = style.find('kml:PolyStyle', ns)
                if poly_style is not None:
                    color_elem = poly_style.find('kml:color', ns)
                    if color_elem is not None:
                        style_data['poly_color'] = color_elem.text

                styles[style_id] = style_data

        # StyleMap
        style_maps = root.findall('.//kml:StyleMap', ns)
        for style_map in style_maps:
            style_id = style_map.get('id')
            if style_id:
                pairs = style_map.findall('kml:Pair', ns)
                style_map_data = {}
                for pair in pairs:
                    key_elem = pair.find('kml:key', ns)
                    style_url_elem = pair.find('kml:styleUrl', ns)
                    if key_elem is not None and style_url_elem is not None:
                        style_map_data[key_elem.text] = style_url_elem.text
                # Une StyleMap prend les couleurs de son style « normal » : c'est elle que
                # les placemarks référencent dans les fichiers Google Earth
                target = styles.get(str(style_map_data.get('normal') or '').lstrip('#'))
                styles[style_id] = {**(target or {}), **style_map_data}

        if styles:
            metadata['styles'] = styles

    @staticmethod
    def _extract_folder_structure(root: ET.Element, metadata: Dict[str, Any]):
        """Extrait la structure des dossiers."""
        ns = KMLParser.NAMESPACES
        folders = root.findall('.//kml:Folder', ns)

        if folders:
            folder_info = []
            for folder in folders:
                folder_data = {}
                name_elem = folder.find('kml:name', ns)
                if name_elem is not None:
                    folder_data['name'] = name_elem.text

                desc_elem = folder.find('kml:description', ns)
                if desc_elem is not None:
                    folder_data['description'] = desc_elem.text

                # Compter les placemarks dans ce dossier
                placemarks = folder.findall('.//kml:Placemark', ns)
                folder_data['placemark_count'] = len(placemarks)

                folder_info.append(folder_data)

            metadata['folders'] = folder_info

    @staticmethod
    def _extract_features(root: ET.Element) -> List[Dict[str, Any]]:
        """Extrait toutes les entités géographiques."""
        features = []
        ns = KMLParser.NAMESPACES

        # Trouver tous les Placemark
        placemarks = root.findall('.//kml:Placemark', ns)
        for i, placemark in enumerate(placemarks):
            features.extend(KMLParser._extract_placemark_data(placemark, i))

        return features

    @staticmethod
    def _extract_placemark_data(placemark: ET.Element, index: int) -> List[Dict[str, Any]]:
        """
        Features d'un Placemark : une par géométrie. Un MultiGeometry (plusieurs lignes,
        un point et sa trace…) donne donc plusieurs features du même nom.
        """
        ns = KMLParser.NAMESPACES
        name_elem = placemark.find('kml:name', ns)
        name = name_elem.text if name_elem is not None and name_elem.text else f"Élément {index + 1}"
        desc_elem = placemark.find('kml:description', ns)
        description = desc_elem.text if desc_elem is not None and desc_elem.text else ""
        visibility_elem = placemark.find('kml:visibility', ns)
        visibility = visibility_elem.text if visibility_elem is not None else "1"
        style_url_elem = placemark.find('kml:styleUrl', ns)
        style_ref = style_url_elem.text if style_url_elem is not None else None
        time_info = KMLParser._extract_placemark_time(placemark)
        extensions = KMLParser._extract_placemark_extensions(placemark)
        properties = KMLParser._extract_extended_data(placemark)
        common = {'name': name, 'description': description, 'style': style_ref,
                  'time_info': time_info, 'extensions': extensions, 'properties': properties}
        features: List[Dict[str, Any]] = []

        for point in placemark.findall('.//kml:Point/kml:coordinates', ns):
            coords = KMLParser._parse_coordinates((point.text or '').strip())
            if coords:
                lat, lon, alt = coords[0]
                features.append({
                    'type': 'marker', **common, 'visibility': visibility,
                    'coordinates': [lat, lon], 'altitude': alt,
                    'parsed_info': KMLParser.parse_gps_description(description, lat, lon, alt),
                })

        for linestring in placemark.findall('.//kml:LineString/kml:coordinates', ns):
            coords = KMLParser._parse_coordinates((linestring.text or '').strip())
            if coords:
                features.append({'type': 'polyline', **common, 'coordinates': coords})

        for polygon in placemark.findall('.//kml:Polygon', ns):
            features.append(KMLParser._extract_polygon_data(polygon, name, description, style_ref,
                                                            time_info, extensions, placemark))

        multitracks = placemark.findall('.//gx:MultiTrack', ns)
        nested = {id(track) for multitrack in multitracks for track in multitrack.findall('gx:Track', ns)}
        for track in placemark.findall('.//gx:Track', ns):
            if id(track) not in nested:
                features.append(KMLParser._extract_track_data(track, name, description, style_ref,
                                                              time_info, extensions, placemark))
        for multitrack in multitracks:
            features.append(KMLParser._extract_multitrack_data(multitrack, name, description, style_ref,
                                                               time_info, extensions, placemark))
        return features

    @staticmethod
    def _parse_coordinates(coord_text: str) -> List[List[float]]:
        """Parse une chaîne de coordonnées KML."""
        coordinates = []
        for coord_line in coord_text.split():
            if coord_line.strip():
                parts = coord_line.split(',')
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        alt = float(parts[2]) if len(parts) > 2 else 0
                        coordinates.append([lat, lon, alt])
                    except ValueError:
                        continue
        return coordinates

    @staticmethod
    def _extract_placemark_time(placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les informations temporelles d'un placemark."""
        ns = KMLParser.NAMESPACES
        time_info = {}

        # TimeStamp
        timestamp = placemark.find('.//kml:TimeStamp', ns)
        if timestamp is not None:
            when_elem = timestamp.find('kml:when', ns)
            if when_elem is not None:
                time_info['timestamp'] = when_elem.text

        # TimeSpan
        timespan = placemark.find('.//kml:TimeSpan', ns)
        if timespan is not None:
            begin_elem = timespan.find('kml:begin', ns)
            end_elem = timespan.find('kml:end', ns)
            if begin_elem is not None:
                time_info['begin'] = begin_elem.text
            if end_elem is not None:
                time_info['end'] = end_elem.text

        return time_info

    @staticmethod
    def _extract_placemark_extensions(placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les extensions Google d'un placemark."""
        ns = KMLParser.NAMESPACES
        extensions = {}

        # gx:balloonVisibility
        balloon_vis = placemark.find('.//gx:balloonVisibility', ns)
        if balloon_vis is not None:
            extensions['balloon_visibility'] = balloon_vis.text

        # gx:drawOrder
        draw_order = placemark.find('.//gx:drawOrder', ns)
        if draw_order is not None:
            extensions['draw_order'] = draw_order.text

        return extensions

    @staticmethod
    def _extract_extended_data(placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les données étendues (ExtendedData)."""
        ns = KMLParser.NAMESPACES
        properties = {}

        extended_data = placemark.find('.//kml:ExtendedData', ns)
        if extended_data is not None:
            # Data elements
            data_elements = extended_data.findall('kml:Data', ns)
            for data in data_elements:
                name = data.get('name')
                value_elem = data.find('kml:value', ns)
                if name and value_elem is not None:
                    properties[name] = value_elem.text

            # SchemaData
            schema_data = extended_data.find('kml:SchemaData', ns)
            if schema_data is not None:
                simple_data = schema_data.findall('kml:SimpleData', ns)
                for data in simple_data:
                    name = data.get('name')
                    if name and data.text:
                        properties[name] = data.text

        return properties

    @staticmethod
    def _extract_polygon_data(polygon: ET.Element, name: str, description: str, style_ref: str,
                            time_info: Dict[str, Any], extensions: Dict[str, Any], placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les données d'un polygone."""
        ns = KMLParser.NAMESPACES

        # Contour extérieur
        outer_boundary = polygon.find('.//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates', ns)

        coordinates = []
        if outer_boundary is not None and outer_boundary.text:
            coordinates = KMLParser._parse_coordinates(outer_boundary.text.strip())

        # Contours intérieurs (trous)
        holes = []
        inner_boundaries = polygon.findall('.//kml:innerBoundaryIs/kml:LinearRing/kml:coordinates', ns)

        for inner in inner_boundaries:
            if inner.text:
                hole_coords = KMLParser._parse_coordinates(inner.text.strip())
                if hole_coords:
                    holes.append(hole_coords)

        result = {
            'type': 'polygon',
            'name': name,
            'description': description,
            'coordinates': coordinates,
            'style': style_ref,
            'time_info': time_info,
            'extensions': extensions,
            'properties': KMLParser._extract_extended_data(placemark)
        }

        if holes:
            result['holes'] = holes

        return result

    @staticmethod
    def _extract_track_data(track: ET.Element, name: str, description: str, style_ref: str,
                          time_info: Dict[str, Any], extensions: Dict[str, Any], placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les données d'une trace Google (gx:Track)."""
        ns = KMLParser.NAMESPACES

        # <when> et <gx:coord> vont par paires : un point illisible emporte son horodatage,
        # sinon toute la suite serait décalée
        coord_elements = track.findall('gx:coord', ns)
        when_elements = track.findall('kml:when', ns)
        paired = len(when_elements) == len(coord_elements)

        coordinates = []
        timestamps = []
        kept = []
        for position, coord_elem in enumerate(coord_elements):
            parts = (coord_elem.text or '').strip().split()
            try:
                lon, lat = float(parts[0]), float(parts[1])
                alt = float(parts[2]) if len(parts) > 2 else 0
            except (IndexError, ValueError):
                continue
            coordinates.append([lat, lon, alt])
            kept.append(position)
            if paired:
                timestamps.append((when_elements[position].text or '').strip() or None)

        # Données par point (vitesse, cap, fréquence cardiaque…) : un tableau par grandeur
        track_data: Dict[str, List[Optional[str]]] = {}
        for array in track.findall('kml:ExtendedData/kml:SchemaData/gx:SimpleArrayData', ns):
            values = [(value.text or '').strip() or None for value in array.findall('gx:value', ns)]
            if array.get('name') and len(values) == len(coord_elements):
                track_data[array.get('name')] = [values[position] for position in kept]

        result = {
            'type': 'track',
            'name': name,
            'description': description,
            'coordinates': coordinates,
            'style': style_ref,
            'time_info': time_info,
            'extensions': extensions,
            'properties': KMLParser._extract_extended_data(placemark)
        }

        if timestamps:
            result['timestamps'] = timestamps

        if track_data:
            result['track_data'] = track_data

        return result

    @staticmethod
    def _extract_multitrack_data(multitrack: ET.Element, name: str, description: str, style_ref: str,
                               time_info: Dict[str, Any], extensions: Dict[str, Any], placemark: ET.Element) -> Dict[str, Any]:
        """Extrait les données d'une multi-trace Google (gx:MultiTrack)."""
        ns = KMLParser.NAMESPACES

        tracks = multitrack.findall('gx:Track', ns)
        track_list = []

        for track in tracks:
            track_data = KMLParser._extract_track_data(track, f"{name}_track_{len(track_list)}",
                                                     description, style_ref, time_info, extensions, placemark)
            track_list.append(track_data)

        return {
            'type': 'multitrack',
            'name': name,
            'description': description,
            'tracks': track_list,
            'style': style_ref,
            'time_info': time_info,
            'extensions': extensions,
            'properties': KMLParser._extract_extended_data(placemark)
        }

    @staticmethod
    @track_time
    def parse_gps_description(description: str, lat: float, lon: float, alt: float) -> Dict[str, Any]:
        """
        Extrait vitesse et cap de la description d'un point de traceur GPS.

        Args:
            description: Description du point GPS
            lat: Latitude du point
            lon: Longitude du point
            alt: Altitude en mètres

        Returns:
            dict: Informations parsées formatées
        """
        parsed_info = {
            'speed_kmh': None,
            'speed_kts': None,
            'altitude_ft': None,
            'altitude_m': alt,
            'latitude': lat,
            'longitude': lon,
            'heading': None,
            'raw_description': description
        }

        if not description:
            return parsed_info

        for pattern, knots in SPEED_PATTERNS:
            match = pattern.search(description)
            if match:
                value = float(match.group(1).replace(',', '.'))
                parsed_info['speed_kts'] = value if knots else round(value / 1.852, 1)
                parsed_info['speed_kmh'] = round(value * 1.852, 1) if knots else value
                break

        match = HEADING_PATTERN.search(description)
        if match:
            heading = float(match.group(1).replace(',', '.'))
            if 0 <= heading <= 360:
                parsed_info['heading'] = heading

        # Conversion altitude en pieds
        if alt:
            parsed_info['altitude_ft'] = round(alt * 3.28084)

        return parsed_info

    @staticmethod
    @track_time
    def parse_kml_coordinates(kml_content: str, display_mode: str = 'double') -> Dict[str, Any]:
        """
        Parse un fichier KML et extrait les coordonnées des traces GPS et tous les points.

        Args:
            kml_content: Contenu du fichier KML
            display_mode: ignoré (les unités sont choisies à l'affichage), conservé pour l'API

        Returns:
            dict: Données formatées pour Leaflet avec traces et points séparés
        """
        result = KMLParser.parse(kml_content)
        if not result['success']:
            return result
        features = result['features']
        # Le k-ième point est le k-ième marqueur des features. La description reste celle du
        # fichier : la mise en forme des mesures (unités) est faite à l'affichage.
        points = [{**feature, 'raw_description': feature['description'], 'index': index}
                  for index, feature in enumerate(f for f in features if f['type'] == 'marker')]
        return {
            'success': True,
            'features': features,
            'points': points,
            'metadata': result['metadata'],
            'total_points': len(points)
        }
