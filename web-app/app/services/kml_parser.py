"""
Service de parsing des fichiers KML.
Extrait toutes les informations pertinentes des fichiers KML, y compris les extensions Google.
"""

import re
import xml.etree.ElementTree as ET
import logging
from app.services.timing_tools import track_time
from xml.etree.ElementTree import tostring
from typing import Dict, Any, List, Optional

# Configuration du logger
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

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
            root = ET.fromstring(kml_content)
            
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
                when_elem = ts.find('kml:when', ns) or ts.find('when')
                if when_elem is not None:
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
                styles[style_id] = style_map_data
        
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
            feature = KMLParser._extract_placemark_data(placemark, i)
            if feature:
                features.append(feature)
        
        return features
    
    @staticmethod
    def _extract_placemark_data(placemark: ET.Element, index: int) -> Optional[Dict[str, Any]]:
        """Extrait les données d'un Placemark."""
        ns = KMLParser.NAMESPACES
        # logger.debug("placemark : %s",placemark)
        # Informations de base
        name_elem = placemark.find('kml:name', ns)
        
        name = name_elem.text if name_elem is not None else f"Élément {index + 1}"
        
        desc_elem = placemark.find('kml:description', ns)
        description = desc_elem.text if desc_elem is not None else ""
        # logger.debug("Descr : %s",description)
        
        #visibility
        visibility_elem = placemark.find('kml:visibility', ns)
        visibility = visibility_elem.text if visibility_elem is not None else "1"

        # Style
        style_url_elem = placemark.find('kml:styleUrl', ns)
        style_ref = style_url_elem.text if style_url_elem is not None else None
        
        # Informations temporelles
        time_info = KMLParser._extract_placemark_time(placemark)
        
        # Extensions Google pour ce placemark
        extensions = KMLParser._extract_placemark_extensions(placemark)
        
        # Géométrie - Point
        point = placemark.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            # logger.debug("Point")
            coords = KMLParser._parse_coordinates(point.text.strip())
            if coords:
                # logger.debug("Point avec coord")
                lat, lon, alt = coords[0]
                parsed_info = KMLParser.parse_gps_description(description, lat, lon, alt)
                
                return {
                    'type': 'marker',
                    'name': name,
                    'visibility': visibility,
                    'description': description,
                    'coordinates': [lat, lon],
                    'altitude': alt,
                    'style': style_ref,
                    'time_info': time_info,
                    'extensions': extensions,
                    'parsed_info': parsed_info,
                    'properties': KMLParser._extract_extended_data(placemark)
                }
        
        # Géométrie - LineString
        linestring = placemark.find('.//kml:LineString/kml:coordinates', ns) #or placemark.find('.//LineString/coordinates')
        if linestring is not None and linestring.text:
            coords = KMLParser._parse_coordinates(linestring.text.strip())
            # logger.debug("Linestring")
            if coords:
                # logger.debug("Linestring avec coord")
                return {
                    'type': 'polyline',
                    'name': name,
                    'description': description,
                    'coordinates': coords,
                    'style': style_ref,
                    'time_info': time_info,
                    'extensions': extensions,
                    'properties': KMLParser._extract_extended_data(placemark)
                }
        
        # Géométrie - Polygon
        polygon = placemark.find('.//kml:Polygon', ns) #or placemark.find('.//Polygon')
        if polygon is not None:
            # logger.debug("Polygon")
            return KMLParser._extract_polygon_data(polygon, name, description, style_ref, time_info, extensions, placemark)
        
        # Géométrie - gx:Track (traces GPS Google)
        track = placemark.find('.//gx:Track', ns)
        if track is not None:
            # logger.debug("Track")
            return KMLParser._extract_track_data(track, name, description, style_ref, time_info, extensions, placemark)
        
        # Géométrie - gx:MultiTrack
        multitrack = placemark.find('.//gx:MultiTrack', ns)
        if multitrack is not None:
            # logger.debug("Multitrack")
            return KMLParser._extract_multitrack_data(multitrack, name, description, style_ref, time_info, extensions, placemark)
        
        return None
    
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
        
        # Coordonnées de la trace
        coord_elements = track.findall('gx:coord', ns)
        when_elements = track.findall('kml:when', ns)
        
        coordinates = []
        timestamps = []
        
        for coord_elem in coord_elements:
            if coord_elem.text:
                parts = coord_elem.text.strip().split()
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        alt = float(parts[2]) if len(parts) > 2 else 0
                        coordinates.append([lat, lon, alt])
                    except ValueError:
                        continue
        
        for when_elem in when_elements:
            if when_elem.text:
                timestamps.append(when_elem.text)
        
        # Données étendues de la trace (vitesse, cap, etc.)
        extended_data_elements = track.findall('gx:ExtendedData', ns)
        track_data = []
        
        for ext_data in extended_data_elements:
            schema_data = ext_data.find('gx:SchemaData', ns)
            if schema_data is not None:
                simple_data = schema_data.findall('gx:SimpleData', ns)
                data_point = {}
                for data in simple_data:
                    name_attr = data.get('name')
                    if name_attr and data.text:
                        data_point[name_attr] = data.text
                if data_point:
                    track_data.append(data_point)
        
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
    
    # Méthodes utilitaires conservées pour compatibilité
    @staticmethod
    @track_time
    def parse_gps_description(description: str, lat: float, lon: float, alt: float) -> Dict[str, Any]:
        """
        Parse la description d'un point GPS pour extraire les informations de vol.
        
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
        
        # Recherche de la vitesse (différents formats possibles)
        speed_patterns = [
            r'(?:vitesse|speed|spd)[\s:]*(\d+(?:\.\d+)?)\s*(?:km/h|kmh|kph)',
            r'(?:vitesse|speed|spd)[\s:]*(\d+(?:\.\d+)?)\s*(?:kt|kts|knots)',
            r'(\d+(?:\.\d+)?)\s*(?:km/h|kmh|kph)',
            r'(\d+(?:\.\d+)?)\s*(?:kt|kts|knots)'
        ]
        
        for pattern in speed_patterns:
            match = re.search(pattern, description, re.IGNORECASE)
            if match:
                speed_value = float(match.group(1))
                if 'kt' in pattern.lower() or 'knot' in pattern.lower():
                    parsed_info['speed_kts'] = speed_value
                    parsed_info['speed_kmh'] = round(speed_value * 1.852, 1)
                else:
                    parsed_info['speed_kmh'] = speed_value
                    parsed_info['speed_kts'] = round(speed_value / 1.852, 1)
                break
        
        # Recherche du cap/heading
        heading_patterns = [
            r'(?:cap|heading|hdg|course)[\s:]*(\d+(?:\.\d+)?)\s*(?:°|deg|degrees?)?',
            r'(?:direction|dir)[\s:]*(\d+(?:\.\d+)?)\s*(?:°|deg|degrees?)?'
        ]
        
        for pattern in heading_patterns:
            match = re.search(pattern, description, re.IGNORECASE)
            if match:
                parsed_info['heading'] = float(match.group(1))
                break
        
        # Conversion altitude en pieds
        if alt:
            parsed_info['altitude_ft'] = round(alt * 3.28084)
        
        return parsed_info

    @staticmethod
    @track_time
    def format_point_description(parsed_info: Dict[str, Any], display_mode: str = 'double') -> str:
        """
        Formate la description d'un point selon le mode d'affichage choisi.
        
        Args:
            parsed_info: Informations parsées du point
            display_mode: 'double' pour double unité, 'simple' pour unité simple
            
        Returns:
            str: Description formatée
        """
        lines = []
        
        # Ligne 1: Vitesse
        if parsed_info['speed_kmh'] is not None:
            if display_mode == 'double':
                lines.append(f"Vitesse: {parsed_info['speed_kmh']} km/h ({parsed_info['speed_kts']} kts)")
            else:
                lines.append(f"Vitesse: {parsed_info['speed_kmh']} km/h")
        
        # Ligne 2: Altitude
        if parsed_info['altitude_m'] is not None:
            if display_mode == 'double':
                lines.append(f"Altitude: {parsed_info['altitude_ft']} ft ({round(parsed_info['altitude_m'])} m)")
            else:
                lines.append(f"Altitude: {round(parsed_info['altitude_m'])} m")
        
        # Ligne 3: Coordonnées
        if parsed_info['latitude'] is not None and parsed_info['longitude'] is not None:
            lines.append(f"Position: {parsed_info['latitude']:.6f}°, {parsed_info['longitude']:.6f}°")
        
        # Ligne 4: Cap
        if parsed_info['heading'] is not None:
            lines.append(f"Cap: {parsed_info['heading']}°")
        
        return '<br>'.join(lines) if lines else parsed_info['raw_description']

    @staticmethod
    @track_time
    def get_placemarks_points(kml_content, folder_name_to_search):
        """Méthode conservée pour compatibilité."""
        placemarks = []
        namespaces = KMLParser.NAMESPACES
        
        try:
            root = ET.fromstring(kml_content)
            folders = root.findall('.//kml:Folder', namespaces)

            for folder in folders:
                folder_name_elem = folder.find('kml:name', namespaces)
                folder_name = folder_name_elem.text if folder_name_elem is not None else None
                logger.debug("Folder : %s", folder_name)

                if folder_name and folder_name.strip().lower() != folder_name_to_search.strip().lower():
                    continue
            
                placemarks = folder.findall('.//kml:Placemark', namespaces)
                if not placemarks:
                    placemarks = root.findall('.//Placemark')
                logger.debug("Nombre de Placemark points : %d", len(placemarks))
            
            return placemarks
        except ET.ParseError as e:
            return {
                'success': False,
                'error': f'Erreur de parsing XML: {str(e)}'
            }
        except (IOError, OSError) as e:
            return {
                'success': False,
                'error': f'Erreur lors du traitement: {str(e)}'
            }

    @staticmethod
    @track_time
    def parse_kml_coordinates(kml_content: str, display_mode: str = 'double') -> Dict[str, Any]:
        """
        Parse un fichier KML et extrait les coordonnées des traces GPS et tous les points.
        Méthode conservée pour compatibilité avec l'ancien code.
        
        Args:
            kml_content: Contenu du fichier KML
            display_mode: Mode d'affichage ('double' ou 'simple')
            
        Returns:
            dict: Données formatées pour Leaflet avec traces et points séparés
        """
        try:
            # Utiliser la nouvelle méthode parse et adapter le format de sortie
            result = KMLParser.parse(kml_content)
            
            if not result['success']:
                return result
            
            features = result['features']
            points = []
            
            # Séparer les points des autres features et adapter le format
            for feature in features:
                if feature['type'] == 'marker':
                    # Adapter le format pour compatibilité
                    parsed_info = feature.get('parsed_info', {})
                    formatted_description = KMLParser.format_point_description(parsed_info, display_mode)
                    
                    point_data = {
                        'type': 'marker',
                        'name': feature['name'],
                        'description': formatted_description,
                        'raw_description': feature['description'],
                        'coordinates': feature['coordinates'],
                        'altitude': feature.get('altitude', 0),
                        'style': feature.get('style'),
                        'is_annotation': False,  # Valeur par défaut
                        'index': len(points),
                        'parsed_info': parsed_info
                    }
                    
                    points.append(point_data)
            
            return {
                'success': True,
                'features': features,
                'points': points,
                'metadata': result['metadata'],
                'total_points': len(points)
            }
            
        except (ValueError, AttributeError, TypeError) as e:
            return {
                'success': False,
                'error': f'Erreur lors du traitement: {str(e)}'
            }
