"""
Service d'édition et d'export de fichiers KML.
Phase 4 : Fonctionnalités d'édition et export multi-formats.
"""

import json
import csv
import io
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime
from app.services.timing_tools import track_time


class KMLEditor:
    """Service d'édition et d'export de données KML."""
    
    @staticmethod
    @track_time
    def add_point(features: List[Dict[str, Any]], 
                  coordinates: List[float], 
                  name: str = "Nouveau point", 
                  description: str = "",
                  is_annotation: bool = False) -> Dict[str, Any]:
        """
        Ajoute un nouveau point à la liste des features.
        
        Args:
            features: Liste des features existantes
            coordinates: [lat, lon, alt] du nouveau point
            name: Nom du point
            description: Description du point
            is_annotation: True si c'est une annotation
            
        Returns:
            Dictionnaire avec le résultat de l'opération
        """
        try:
            new_point = {
                'type': 'marker',
                'name': name,
                'description': description,
                'coordinates': coordinates[:2],  # lat, lon
                'altitude': coordinates[2] if len(coordinates) > 2 else 0,
                'is_annotation': is_annotation,
                'index': len([f for f in features if f.get('type') == 'marker']),
                'parsed_info': {
                    'speed_kmh': None,
                    'speed_kts': None
                }
            }
            
            features.append(new_point)
            
            return {
                'success': True,
                'message': 'Point ajouté avec succès',
                'point': new_point,
                'total_points': len([f for f in features if f.get('type') == 'marker'])
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': f'Erreur lors de l\'ajout du point: {str(e)}'
            }
    
    @staticmethod
    @track_time
    def update_point(features: List[Dict[str, Any]], 
                    point_index: int, 
                    updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        Met à jour un point existant.
        
        Args:
            features: Liste des features
            point_index: Index du point à modifier
            updates: Dictionnaire des modifications à apporter
            
        Returns:
            Dictionnaire avec le résultat de l'opération
        """
        try:
            # Trouver le point à modifier
            markers = [f for f in features if f.get('type') == 'marker']
            
            if point_index < 0 or point_index >= len(markers):
                return {
                    'success': False,
                    'error': 'Index de point invalide'
                }
            
            # Trouver l'index dans la liste complète des features
            marker_count = 0
            feature_index = -1
            for i, feature in enumerate(features):
                if feature.get('type') == 'marker':
                    if marker_count == point_index:
                        feature_index = i
                        break
                    marker_count += 1
            
            if feature_index == -1:
                return {
                    'success': False,
                    'error': 'Point non trouvé'
                }
            
            # Appliquer les modifications
            point = features[feature_index]
            
            if 'name' in updates:
                point['name'] = updates['name']
            if 'description' in updates:
                point['description'] = updates['description']
            if 'coordinates' in updates:
                coords = updates['coordinates']
                point['coordinates'] = coords[:2]
                if len(coords) > 2:
                    point['altitude'] = coords[2]
            if 'is_annotation' in updates:
                point['is_annotation'] = updates['is_annotation']
            
            return {
                'success': True,
                'message': 'Point modifié avec succès',
                'point': point
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': f'Erreur lors de la modification du point: {str(e)}'
            }
    
    @staticmethod
    @track_time
    def delete_point(features: List[Dict[str, Any]], point_index: int) -> Dict[str, Any]:
        """
        Supprime un point de la liste des features.
        
        Args:
            features: Liste des features
            point_index: Index du point à supprimer
            
        Returns:
            Dictionnaire avec le résultat de l'opération
        """
        try:
            # Trouver le point à supprimer
            markers = [f for f in features if f.get('type') == 'marker']
            
            if point_index < 0 or point_index >= len(markers):
                return {
                    'success': False,
                    'error': 'Index de point invalide'
                }
            
            # Trouver l'index dans la liste complète des features
            marker_count = 0
            feature_index = -1
            for i, feature in enumerate(features):
                if feature.get('type') == 'marker':
                    if marker_count == point_index:
                        feature_index = i
                        break
                    marker_count += 1
            
            if feature_index == -1:
                return {
                    'success': False,
                    'error': 'Point non trouvé'
                }
            
            # Supprimer le point
            deleted_point = features.pop(feature_index)
            
            # Réindexer les points restants
            marker_count = 0
            for feature in features:
                if feature.get('type') == 'marker':
                    feature['index'] = marker_count
                    marker_count += 1
            
            return {
                'success': True,
                'message': 'Point supprimé avec succès',
                'deleted_point': deleted_point,
                'total_points': len([f for f in features if f.get('type') == 'marker'])
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': f'Erreur lors de la suppression du point: {str(e)}'
            }
    
    @staticmethod
    @track_time
    def simplify_trace(coordinates: List[List[float]], 
                      tolerance: float = 0.0001) -> List[List[float]]:
        """
        Simplifie une trace en réduisant le nombre de points tout en préservant la forme.
        Utilise l'algorithme de Douglas-Peucker.
        
        Args:
            coordinates: Liste de coordonnées [lat, lon, alt]
            tolerance: Tolérance pour la simplification
            
        Returns:
            Liste des coordonnées simplifiées
        """
        if len(coordinates) <= 2:
            return coordinates
        
        def perpendicular_distance(point, line_start, line_end):
            """Calcule la distance perpendiculaire d'un point à une ligne."""
            if line_start == line_end:
                return ((point[0] - line_start[0])**2 + (point[1] - line_start[1])**2)**0.5
            
            # Calcul de la distance perpendiculaire
            A = line_end[0] - line_start[0]
            B = line_end[1] - line_start[1]
            C = point[0] - line_start[0]
            D = point[1] - line_start[1]
            
            dot = A * C + B * D
            len_sq = A * A + B * B
            
            if len_sq == 0:
                return ((point[0] - line_start[0])**2 + (point[1] - line_start[1])**2)**0.5
            
            param = dot / len_sq
            
            if param < 0:
                xx = line_start[0]
                yy = line_start[1]
            elif param > 1:
                xx = line_end[0]
                yy = line_end[1]
            else:
                xx = line_start[0] + param * A
                yy = line_start[1] + param * B
            
            return ((point[0] - xx)**2 + (point[1] - yy)**2)**0.5
        
        def douglas_peucker(points, tolerance):
            """Algorithme de Douglas-Peucker pour la simplification."""
            if len(points) <= 2:
                return points
            
            # Trouver le point le plus éloigné de la ligne entre le premier et le dernier point
            max_distance = 0
            max_index = 0
            
            for i in range(1, len(points) - 1):
                distance = perpendicular_distance(points[i], points[0], points[-1])
                if distance > max_distance:
                    max_distance = distance
                    max_index = i
            
            # Si la distance maximale est supérieure à la tolérance, subdiviser
            if max_distance > tolerance:
                # Récursion sur les deux segments
                left_points = douglas_peucker(points[:max_index + 1], tolerance)
                right_points = douglas_peucker(points[max_index:], tolerance)
                
                # Combiner les résultats (éviter la duplication du point de jonction)
                return left_points[:-1] + right_points
            else:
                # Tous les points sont suffisamment proches de la ligne
                return [points[0], points[-1]]
        
        simplified = douglas_peucker(coordinates, tolerance)
        return simplified
    
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
        gpx.set('creator', 'KML Viewer - Phase 4')
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
            time_elem.text = datetime.utcnow().isoformat() + 'Z'
        
        # Ajouter les waypoints (points)
        for feature in features:
            if feature.get('type') == 'marker':
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
            if feature.get('type') == 'polyline':
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
            if feature.get('type') == 'marker':
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
                if feature.get('type') == 'polyline':
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
            if feature.get('type') == 'marker':
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
                
            elif feature.get('type') == 'polyline':
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
            if feature.get('type') == 'marker':
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
            if feature.get('type') == 'polyline':
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