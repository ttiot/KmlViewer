"""
Service de parsing des fichiers KML.
Extrait du code original pour une meilleure séparation des responsabilités.
"""

import re
import xml.etree.ElementTree as ET
import logging
from app.services.timing_tools import track_time
from xml.etree.ElementTree import tostring
from typing import Dict, Any

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
    """Service de parsing des fichiers KML."""
    
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

    # Fonction pour récupérer tous les <Placemark> sauf ceux dans un <Folder> avec <name>Temps
    @staticmethod
    @track_time
    def get_placemarks_points(kml_content,folder_name_to_search):
        placemarks = []
        # Définir les namespaces KML
        namespaces = {
            'kml': 'http://www.opengis.net/kml/2.2',
            'gx': 'http://www.google.com/kml/ext/2.2'
        }
        
        try:
            root = ET.fromstring(kml_content)
            # Parcourir tous les <Folder>
            folders = root.findall('.//kml:Folder', namespaces)
            if not folders:
                folders = root.findall('.//Folder')

            for folder in folders:
                # Récupérer le nom du Folder
                folder_name_elem = folder.find('kml:name', namespaces)
                if folder_name_elem is None:
                    folder_name_elem = folder.find('name')
                folder_name = folder_name_elem.text if folder_name_elem is not None else None
                logger.debug(f"Folder : {folder_name}")

                # Ignorer les Folders ayant pour nom "Temps"
                if folder_name and folder_name.strip().lower() != folder_name_to_search.strip().lower():
                    continue
            
                #Trouver tous les Placemark dans ce Folder
                placemarks = folder.findall('.//kml:Placemark', namespaces)
                if not placemarks:
                    # placemarks = folder.findall('.//Placemark')
                    placemarks = root.findall('.//Placemark')
                logger.debug(f"Nombre de Placemark points : {len(placemarks)}")
            
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
        
        Args:
            kml_content: Contenu du fichier KML
            display_mode: Mode d'affichage ('double' ou 'simple')
            
        Returns:
            dict: Données formatées pour Leaflet avec traces et points séparés
        """
        try:
            root = ET.fromstring(kml_content)
            
            # Définir les namespaces KML
            namespaces = {
                'kml': 'http://www.opengis.net/kml/2.2',
                'gx': 'http://www.google.com/kml/ext/2.2'
            }
            
            features = []
            points = []
            
            # Récupérer les placemarks
            filtered_placemarks = KMLParser.get_placemarks_points(kml_content,'Points')
            placemarks_cache = {tostring(pl): pl for pl in filtered_placemarks}
            placemarks = root.findall('.//kml:Placemark', namespaces)
            logger.debug(f"Nombre de Placemark à traiter : {len(placemarks)}")

            for i, placemark in enumerate(placemarks):
                # Récupérer le nom
                name_elem = placemark.find('kml:name', namespaces)
                if name_elem is None:
                    name_elem = placemark.find('name')
                name = name_elem.text if name_elem is not None else f"Élément {i+1}"
                
                # Récupérer la description
                desc_elem = placemark.find('kml:description', namespaces)
                if desc_elem is None:
                    desc_elem = placemark.find('description')
                description = desc_elem.text if desc_elem is not None else ""
                
                # Récupérer les informations de style/couleur si disponibles
                style_url = placemark.find('kml:styleUrl', namespaces)
                if style_url is None:
                    style_url = placemark.find('styleUrl')
                style_ref = style_url.text if style_url is not None else None
                
                # Chercher les coordonnées dans LineString (traces GPS)
                linestring = placemark.find('.//kml:LineString/kml:coordinates', namespaces)
                if linestring is None:
                    linestring = placemark.find('.//LineString/coordinates')
                
                if linestring is not None and linestring.text:
                    coordinates_text = linestring.text.strip()
                    coordinates = []
                    
                    for coord_line in coordinates_text.split():
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
                    
                    if coordinates:
                        features.append({
                            'type': 'polyline',
                            'name': name,
                            'description': description,
                            'coordinates': coordinates,
                            'style': style_ref
                        })
                
                # Chercher les coordonnées dans Point
                point = placemark.find('.//kml:Point/kml:coordinates', namespaces)
                if point is None:
                    point = placemark.find('.//Point/coordinates')
                
                if point is not None and point.text:
                    coord_text = point.text.strip()
                    parts = coord_text.split(',')
                    if len(parts) >= 2:
                        try:
                            lon = float(parts[0])
                            lat = float(parts[1])
                            alt = float(parts[2]) if len(parts) > 2 else 0
                            
                            # Déterminer si c'est un point d'annotation ou un point principal
                            is_annotation = KMLParser._is_annotation_point(placemark, placemarks_cache)
                            
                            # Parser les informations GPS de la description
                            parsed_info = KMLParser.parse_gps_description(description, lat, lon, alt)
                            formatted_description = KMLParser.format_point_description(parsed_info, display_mode)
                            
                            point_data = {
                                'type': 'marker',
                                'name': name,
                                'description': formatted_description,
                                'raw_description': description,
                                'coordinates': [lat, lon],
                                'altitude': alt,
                                'style': style_ref,
                                'is_annotation': is_annotation,
                                'index': len(points),
                                'parsed_info': parsed_info
                            }
                            
                            points.append(point_data)
                            features.append(point_data)
                            
                        except ValueError:
                            pass
            
            return {
                'success': True,
                'features': features,
                'points': points,
                'total_points': len(points)
            }
            
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
    def _is_annotation_point(placemark, placemarks_cache) -> bool:

        if tostring(placemark) in placemarks_cache:
            # logger.debug(f"Placemark trouvé")
            return True
        
        return False