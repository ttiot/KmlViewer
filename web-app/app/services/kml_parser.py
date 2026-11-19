"""
Service de parsing des fichiers KML.
Extrait toutes les informations pertinentes des fichiers KML, y compris les extensions Google.
"""

import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from app.services import file_formats, xml_stream
from app.services.timing_tools import track_time

logger = logging.getLogger(__name__)

# Vitesse dans la description d'un point : après un mot-clé, ou seule en début de ligne.
# Un nombre suivi d'une unité au milieu d'une phrase (« vent de 20 km/h ») n'est pas une mesure.
#
# Les descriptions viennent du fichier : les motifs ne doivent jamais revenir en arrière sur
# une longue suite de blancs ou de chiffres (ReDoS, et une regex garde le GIL : toute
# l'application gèle). Chaque répétition est donc bornée, deux répétitions de blancs ne
# se suivent jamais sans séparateur obligatoire, et seul le début de la description est lu.
MAX_DESCRIPTION_SCAN = 2000
_GAP = r'[^\S\n]{0,8}'  # blancs sur une même ligne
_NUMBER = r'(\d{1,7}(?:[.,]\d{1,6})?)'
_KMH, _KTS = r'(?:km/h|kmh|kph)', r'(?:kts|kt|knots?|n(?:oe|œ)uds?)'
_SEPARATOR = r'\s{0,8}(?:[:=]\s{0,8})?'
_KEYWORD = r'(?:vitesse|speed|spd|gs)' + _SEPARATOR
_LINE_START = r'(?:^|<br\s{0,8}/?>|[\n;|])' + _GAP
SPEED_PATTERNS = tuple((re.compile(prefix + _NUMBER + r'\s{0,8}' + unit + r'(?![\w/])', re.IGNORECASE), knots)
                       for prefix in (_KEYWORD, _LINE_START)
                       for unit, knots in ((_KMH, False), (_KTS, True)))
HEADING_PATTERN = re.compile(r'\b(?:cap|heading|hdg|course|direction|dir)' + _SEPARATOR + _NUMBER
                             + r'\s{0,8}(?:°|deg(?:rees?|rés?)?)?(?![\w/.,])', re.IGNORECASE)

KML_NS = 'http://www.opengis.net/kml/2.2'
# Variantes lues comme du KML 2.2 : fichiers Google Earth antérieurs à la norme OGC
KML_NS_ALIASES = ('http://earth.google.com/kml/2.0', 'http://earth.google.com/kml/2.1',
                  'http://earth.google.com/kml/2.2', 'http://www.opengis.net/kml/2.3')
GX_NS, ATOM_NS = 'http://www.google.com/kml/ext/2.2', 'http://www.w3.org/2005/Atom'
_COORD_TOKEN = re.compile(r'\S+')

class KMLParser:
    """Service de parsing complet des fichiers KML avec support des extensions Google."""

    # Namespaces KML standards et extensions
    NAMESPACES = {
        'kml': KML_NS,
        'gx': GX_NS,
        'atom': ATOM_NS
    }

    @staticmethod
    @track_time
    def parse(kml_content: str) -> Dict[str, Any]:
        """
        Parse un fichier KML et extrait toutes les informations disponibles.

        Le document est lu en flux (voir xml_stream) : chaque Placemark est traité puis
        libéré dès qu'il se referme, l'arbre complet n'existe jamais en mémoire.

        Args:
            kml_content: Contenu du fichier KML

        Returns:
            dict: Structure complète avec success, features, metadata et error
        """
        try:
            stream = _KmlStream()
            xml_stream.parse(kml_content, stream)
            return {
                'success': True,
                'features': stream.features,
                'metadata': stream.metadata()
            }

        except ET.ParseError as e:
            return {
                'success': False,
                'error': f'Erreur de parsing XML: {str(e)}'
            }
        except file_formats.TooManyPointsError as e:
            return {'success': False, 'error': str(e)}
        except (ValueError, AttributeError, TypeError, ArithmeticError) as e:
            # Entités interdites (defusedxml), structure inattendue, nombre hors limites…
            logger.info("KML refusé : %s: %s", e.__class__.__name__, str(e)[:200])
            return {
                'success': False,
                'error': f'Erreur lors du traitement: {str(e)[:200]}'
            }

    @staticmethod
    def _style_data(style: ET.Element) -> Dict[str, Any]:
        """Icône et couleurs d'un <Style>."""
        ns = KMLParser.NAMESPACES
        style_data: Dict[str, Any] = {}
        href_elem = style.find('kml:IconStyle/kml:Icon/kml:href', ns)
        if href_elem is not None:
            style_data['icon'] = href_elem.text
        line_style = style.find('kml:LineStyle', ns)
        if line_style is not None:
            color_elem = line_style.find('kml:color', ns)
            width_elem = line_style.find('kml:width', ns)
            if color_elem is not None:
                style_data['line_color'] = color_elem.text
            if width_elem is not None:
                style_data['line_width'] = width_elem.text
        color_elem = style.find('kml:PolyStyle/kml:color', ns)
        if color_elem is not None:
            style_data['poly_color'] = color_elem.text
        return style_data

    @staticmethod
    def _style_map_data(style_map: ET.Element) -> Dict[str, Any]:
        """Paires clé -> styleUrl d'une <StyleMap>."""
        ns = KMLParser.NAMESPACES
        pairs: Dict[str, Any] = {}
        for pair in style_map.findall('kml:Pair', ns):
            key_elem = pair.find('kml:key', ns)
            style_url_elem = pair.find('kml:styleUrl', ns)
            if key_elem is not None and style_url_elem is not None:
                pairs[key_elem.text] = style_url_elem.text
        return pairs

    @staticmethod
    def _point_count(feature: Dict[str, Any]) -> int:
        """Nombre de points portés par une feature (sommets, trous et traces imbriquées compris)."""
        if feature['type'] == 'marker':
            return 1
        if feature['type'] == 'multitrack':
            return sum(len(track['coordinates']) for track in feature['tracks'])
        return len(feature['coordinates']) + sum(len(hole) for hole in feature.get('holes') or [])

    @staticmethod
    def _placemark_features(placemark: ET.Element, index: int, firsts: Dict[str, ET.Element],
                            geometry: Dict[str, List[ET.Element]]) -> List[Dict[str, Any]]:
        """
        Features d'un Placemark : une par géométrie. Un MultiGeometry (plusieurs lignes,
        un point et sa trace…) donne donc plusieurs features du même nom.

        `firsts` (premier descendant de chaque sorte : date, données étendues, extensions)
        et `geometry` (géométries dans l'ordre du document) viennent du parcours unique du
        Placemark fait par _KmlStream : le relire ici pour chaque information coûtait une
        quinzaine de recherches par repère, soit l'essentiel du temps de lecture.
        """
        name_elem = desc_elem = visibility_elem = style_url_elem = None
        for child in placemark:  # premier enfant direct de chaque sorte
            tag = child.tag
            if tag == _NAME:
                name_elem = child if name_elem is None else name_elem
            elif tag == _DESCRIPTION:
                desc_elem = child if desc_elem is None else desc_elem
            elif tag == _VISIBILITY:
                visibility_elem = child if visibility_elem is None else visibility_elem
            elif tag == _STYLE_URL:
                style_url_elem = child if style_url_elem is None else style_url_elem
        name = name_elem.text if name_elem is not None and name_elem.text else f"Élément {index + 1}"
        description = desc_elem.text if desc_elem is not None and desc_elem.text else ""
        visibility = visibility_elem.text if visibility_elem is not None else "1"
        style_ref = style_url_elem.text if style_url_elem is not None else None
        # Partagé par toutes les géométries du Placemark
        common = {'name': name, 'description': description, 'style': style_ref,
                  'time_info': KMLParser._time_info(firsts.get(_TIMESTAMP), firsts.get(_TIMESPAN)),
                  'extensions': KMLParser._extensions(firsts.get(_BALLOON), firsts.get(_DRAW_ORDER)),
                  'properties': KMLParser._extended_data(firsts.get(_EXTENDED_DATA))}
        features: List[Dict[str, Any]] = []

        def bounded(found: List[ET.Element]) -> List[ET.Element]:
            if len(features) + len(found) > file_formats.MAX_FILE_POINTS:
                raise file_formats.TooManyPointsError()
            return found

        def coordinates_of(shapes: List[ET.Element]) -> List[ET.Element]:
            return [child for shape in shapes for child in shape if child.tag == _COORDINATES]

        for point in bounded(coordinates_of(geometry[_POINT])):
            coords = KMLParser._parse_coordinates(point.text or '', limit=1)
            if coords:
                lat, lon, alt = coords[0]
                features.append({
                    'type': 'marker', **common, 'visibility': visibility,
                    'coordinates': [lat, lon], 'altitude': alt,
                    'parsed_info': KMLParser.parse_gps_description(description, lat, lon, alt),
                })

        for linestring in bounded(coordinates_of(geometry[_LINESTRING])):
            coords = KMLParser._parse_coordinates(linestring.text or '')
            if coords:
                features.append({'type': 'polyline', **common, 'coordinates': coords})

        for polygon in bounded(geometry[_POLYGON]):
            features.append(KMLParser._extract_polygon_data(polygon, common))

        multitracks = geometry[_MULTITRACK]
        nested = {id(track) for multitrack in multitracks for track in multitrack if track.tag == _TRACK}
        for track in bounded(geometry[_TRACK]):
            if id(track) not in nested:
                features.append(KMLParser._extract_track_data(track, common))
        for multitrack in bounded(multitracks):
            features.append(KMLParser._extract_multitrack_data(multitrack, common))
        return features

    @staticmethod
    def _parse_coordinates(coord_text: str, limit: Optional[int] = None) -> List[List[float]]:
        """
        Parse une chaîne de coordonnées KML (« lon,lat[,alt] » séparés par des blancs).
        Un point illisible ou non fini est ignoré. `limit` arrête la lecture après ce
        nombre de points (un repère n'en a qu'un).
        """
        coordinates: List[List[float]] = []
        # Lecture au fil de l'eau : un split() matérialiserait des millions de jetons
        # (plusieurs centaines de Mo) avant même le contrôle du plafond
        for token in _COORD_TOKEN.finditer(coord_text):
            parts = token.group().split(',', 3)
            if len(parts) < 2:
                continue
            try:
                lon = file_formats.finite_float(parts[0])
                lat = file_formats.finite_float(parts[1])
                alt = file_formats.altitude(parts[2]) if len(parts) > 2 else 0
            except ValueError:
                continue
            # Arrêt dès le dépassement : inutile de matérialiser des millions de points
            if len(coordinates) >= file_formats.MAX_FILE_POINTS:
                raise file_formats.TooManyPointsError()
            coordinates.append([lat, lon, alt])
            if limit is not None and len(coordinates) >= limit:
                break
        return coordinates

    @staticmethod
    def _time_info(timestamp: Optional[ET.Element], timespan: Optional[ET.Element]) -> Dict[str, Any]:
        """Date d'un Placemark, depuis son premier <TimeStamp> et son premier <TimeSpan>."""
        ns = KMLParser.NAMESPACES
        time_info = {}
        if timestamp is not None:
            when_elem = timestamp.find('kml:when', ns)
            if when_elem is not None:
                time_info['timestamp'] = when_elem.text
        if timespan is not None:
            begin_elem = timespan.find('kml:begin', ns)
            end_elem = timespan.find('kml:end', ns)
            if begin_elem is not None:
                time_info['begin'] = begin_elem.text
            if end_elem is not None:
                time_info['end'] = end_elem.text
        return time_info

    @staticmethod
    def _extensions(balloon_visibility: Optional[ET.Element], draw_order: Optional[ET.Element]) -> Dict[str, Any]:
        """Extensions Google d'un Placemark (gx:balloonVisibility, gx:drawOrder)."""
        extensions = {}
        if balloon_visibility is not None:
            extensions['balloon_visibility'] = balloon_visibility.text
        if draw_order is not None:
            extensions['draw_order'] = draw_order.text
        return extensions

    @staticmethod
    def _extended_data(extended_data: Optional[ET.Element]) -> Dict[str, Any]:
        """Données étendues d'un Placemark, depuis son premier <ExtendedData>."""
        ns = KMLParser.NAMESPACES
        properties = {}
        if extended_data is not None:
            for data in extended_data.findall('kml:Data', ns):
                name = data.get('name')
                value_elem = data.find('kml:value', ns)
                if name and value_elem is not None:
                    properties[name] = value_elem.text
            schema_data = extended_data.find('kml:SchemaData', ns)
            if schema_data is not None:
                for data in schema_data.findall('kml:SimpleData', ns):
                    name = data.get('name')
                    if name and data.text:
                        properties[name] = data.text
        return properties

    @staticmethod
    def _extract_polygon_data(polygon: ET.Element, common: Dict[str, Any]) -> Dict[str, Any]:
        """Extrait les données d'un polygone ; `common` porte ce qui vient du Placemark."""
        ns = KMLParser.NAMESPACES

        # Contour extérieur
        outer_boundary = polygon.find('.//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates', ns)

        coordinates = []
        if outer_boundary is not None and outer_boundary.text:
            coordinates = KMLParser._parse_coordinates(outer_boundary.text)

        # Contours intérieurs (trous)
        holes = []
        inner_boundaries = polygon.findall('.//kml:innerBoundaryIs/kml:LinearRing/kml:coordinates', ns)

        for inner in inner_boundaries:
            if inner.text:
                hole_coords = KMLParser._parse_coordinates(inner.text)
                if hole_coords:
                    holes.append(hole_coords)

        result = {'type': 'polygon', **common, 'coordinates': coordinates}

        if holes:
            result['holes'] = holes

        return result

    @staticmethod
    def _extract_track_data(track: ET.Element, common: Dict[str, Any], name: Optional[str] = None) -> Dict[str, Any]:
        """Extrait les données d'une trace Google (gx:Track) ; `name` remplace celui du Placemark."""
        ns = KMLParser.NAMESPACES

        # <when> et <gx:coord> vont par paires : un point illisible emporte son horodatage,
        # sinon toute la suite serait décalée
        coord_elements = track.findall('gx:coord', ns)
        when_elements = track.findall('kml:when', ns)
        paired = len(when_elements) == len(coord_elements)
        if len(coord_elements) > file_formats.MAX_FILE_POINTS:
            raise file_formats.TooManyPointsError()

        coordinates = []
        timestamps = []
        kept = []
        for position, coord_elem in enumerate(coord_elements):
            parts = (coord_elem.text or '').split(None, 3)
            try:
                lon, lat = file_formats.finite_float(parts[0]), file_formats.finite_float(parts[1])
                alt = file_formats.altitude(parts[2]) if len(parts) > 2 else 0
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

        result = {'type': 'track', **common, 'coordinates': coordinates}
        if name is not None:
            result['name'] = name

        if timestamps:
            result['timestamps'] = timestamps

        if track_data:
            result['track_data'] = track_data

        return result

    @staticmethod
    def _extract_multitrack_data(multitrack: ET.Element, common: Dict[str, Any]) -> Dict[str, Any]:
        """Extrait les données d'une multi-trace Google (gx:MultiTrack)."""
        ns = KMLParser.NAMESPACES
        track_list: List[Dict[str, Any]] = []
        total = 0
        for track in multitrack.findall('gx:Track', ns):
            track_data = KMLParser._extract_track_data(track, common, f"{common['name']}_track_{len(track_list)}")
            total += max(1, len(track_data['coordinates']))
            if total > file_formats.MAX_FILE_POINTS:
                raise file_formats.TooManyPointsError()
            track_list.append(track_data)

        return {'type': 'multitrack', **common, 'tracks': track_list}

    @staticmethod
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

        # Seul le début est analysé : borne le coût quelle que soit la description
        text = description[:MAX_DESCRIPTION_SCAN]
        for pattern, knots in SPEED_PATTERNS:
            match = pattern.search(text)
            if match:
                value = float(match.group(1).replace(',', '.'))
                parsed_info['speed_kts'] = value if knots else round(value / 1.852, 1)
                parsed_info['speed_kmh'] = round(value * 1.852, 1) if knots else value
                break

        match = HEADING_PATTERN.search(text)
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
    def parse_kml_coordinates(kml_content: str, display_mode: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse un fichier KML et extrait les coordonnées des traces GPS et tous les points.

        Args:
            kml_content: Contenu du fichier KML
            display_mode: sans effet (les unités sont choisies à l'affichage) ; accepté le temps
                que les appelants cessent de le passer

        Returns:
            dict: Données formatées pour Leaflet avec traces et points séparés
        """
        result = KMLParser.parse(kml_content)
        if not result['success']:
            return result
        features = result['features']
        # Le k-ième point est le k-ième marqueur des features : ce sont les mêmes objets, pas
        # une copie par repère (un fichier peut en compter 200 000). La description reste celle
        # du fichier : la mise en forme des mesures (unités) est faite à l'affichage.
        points = [feature for feature in features if feature['type'] == 'marker']
        return {
            'success': True,
            'features': features,
            'points': points,
            'metadata': result['metadata'],
            'total_points': len(points)
        }


# --------------------------------------------------------------------- Lecture en flux

def _kml(name: str) -> str:
    return f'{{{KML_NS}}}{name}'


_PLACEMARK, _FOLDER, _DOCUMENT = _kml('Placemark'), _kml('Folder'), _kml('Document')
_NAME, _DESCRIPTION = _kml('name'), _kml('description')
_STYLE, _STYLE_MAP, _TIMESTAMP, _TIMESPAN = _kml('Style'), _kml('StyleMap'), _kml('TimeStamp'), _kml('TimeSpan')
_TOUR, _TRACK, _MULTITRACK = f'{{{GX_NS}}}Tour', f'{{{GX_NS}}}Track', f'{{{GX_NS}}}MultiTrack'
_ATOM_AUTHOR, _ATOM_NAME, _ATOM_LINK = f'{{{ATOM_NS}}}author', f'{{{ATOM_NS}}}name', f'{{{ATOM_NS}}}link'
_VISIBILITY, _STYLE_URL, _EXTENDED_DATA = _kml('visibility'), _kml('styleUrl'), _kml('ExtendedData')
_COORDINATES, _POINT, _LINESTRING, _POLYGON = _kml('coordinates'), _kml('Point'), _kml('LineString'), _kml('Polygon')
_BALLOON, _DRAW_ORDER = f'{{{GX_NS}}}balloonVisibility', f'{{{GX_NS}}}drawOrder'
# Dans un Placemark : sortes dont seul le premier descendant compte, et géométries (toutes)
_FIRSTS = frozenset((_TIMESTAMP, _TIMESPAN, _EXTENDED_DATA, _BALLOON, _DRAW_ORDER))
_GEOMETRIES = (_POINT, _LINESTRING, _POLYGON, _TRACK, _MULTITRACK)
# Éléments dont le sous-arbre entier est nécessaire pour les lire. Les autres ne servent
# que par leur texte ou leur présence.
_SUBTREES = frozenset((_PLACEMARK, _STYLE, _STYLE_MAP, _TIMESTAMP, _TIMESPAN))
# Élément enfant -> clé retenue, selon ce qui le contient (premier rencontré seulement)
_CHILD_KEYS = {
    'document': {_NAME: 'title', _DESCRIPTION: 'description'},
    'folder': {_NAME: 'name', _DESCRIPTION: 'description'},
    'tour': {_NAME: 'name', _DESCRIPTION: 'description'},
    'author': {_ATOM_NAME: 'name'},
}


class _KmlStream(xml_stream.StreamHandler):
    """
    Assemble features et métadonnées au fil de la lecture.

    Chaque Placemark est reçu entier, converti en features puis libéré : la mémoire tenue
    pendant la lecture est celle du résultat, pas celle du document. Un Placemark imbriqué
    dans un autre (invalide) fait partie du premier, comme tout ce que contient un élément
    reçu en sous-arbre. Tout ce qui est accumulé est plafonné par MAX_FILE_POINTS : un
    fichier de 16 Mo de dossiers, de liens ou de styles vides ne remplit pas la mémoire.
    """

    canonical_ns = KML_NS
    ns_aliases = KML_NS_ALIASES

    def __init__(self) -> None:
        self.features: List[Dict[str, Any]] = []
        self._placemarks = 0
        self._points = 0
        self._document: Optional[Dict[str, Any]] = None  # premier <Document> du fichier
        self._links: List[Dict[str, str]] = []
        self._folders: List[Dict[str, Any]] = []
        self._open_folders: List[Dict[str, Any]] = []
        self._timestamps: Optional[List[Any]] = None  # None : aucun <TimeStamp> rencontré
        self._timespans: Optional[List[Dict[str, Any]]] = None
        self._tours: List[Dict[str, Any]] = []
        self._tracks = 0
        self._multitracks = 0
        self._styles: Dict[str, Dict[str, Any]] = {}
        self._style_maps: List[Any] = []  # (id, paires), résolues une fois tous les styles connus
        # Un contexte par élément ouvert hors sous-arbre : (genre, données) ou None
        self._contexts: List[Any] = []
        self._harvesters = {
            _TIMESTAMP: self._timestamp, _TIMESPAN: self._timespan, _STYLE: self._style,
            _STYLE_MAP: self._style_map, _TOUR: self._nested_tour,
            _TRACK: self._count_track, _MULTITRACK: self._count_multitrack,
        }

    @staticmethod
    def _bounded(items: Any) -> Any:
        """Refuse le fichier plutôt que de laisser une liste de métadonnées grossir sans fin."""
        if len(items) >= file_formats.MAX_FILE_POINTS:
            raise file_formats.TooManyPointsError()
        return items

    # ------------------------------------------------------------ Hors sous-arbre

    def opened(self, element: ET.Element) -> bool:
        tag = element.tag
        if tag in _SUBTREES:
            if tag == _PLACEMARK:
                if self._placemarks >= file_formats.MAX_FILE_POINTS:
                    raise file_formats.TooManyPointsError()
                if self._open_folders:
                    self._open_folders[-1]['placemark_count'] += 1
            return True

        context = None
        if tag == _FOLDER:
            data: Dict[str, Any] = {'placemark_count': 0}
            self._bounded(self._folders).append(data)
            self._open_folders.append(data)
            context = ('folder', data)
        elif tag == _DOCUMENT:
            if self._document is None:
                self._document = {}
                context = ('document', self._document)
        elif tag == _TOUR:
            data = {}
            self._bounded(self._tours).append(data)
            context = ('tour', data)
        elif tag == _TRACK:
            self._tracks += 1
        elif tag == _MULTITRACK:
            self._multitracks += 1
        elif tag == _ATOM_AUTHOR:
            parent = self._contexts[-1] if self._contexts else None
            # Seul le premier auteur du document compte, qu'il porte un nom ou non
            if parent is not None and parent[0] == 'document' and 'author_seen' not in parent[1]:
                parent[1]['author_seen'] = True
                context = ('author', {})
        self._contexts.append(context)
        return False

    def closed(self, element: ET.Element) -> None:
        context = self._contexts.pop()
        parent = self._contexts[-1] if self._contexts else None
        if context is not None:
            kind, data = context
            if kind == 'folder':
                self._open_folders.pop()
                count = data.pop('placemark_count')
                data['placemark_count'] = count  # après le nom et la description
                if self._open_folders:
                    self._open_folders[-1]['placemark_count'] += count
            elif kind == 'author' and 'name' in data and parent is not None:
                parent[1]['author'] = data['name']
        if parent is None:
            return
        kind, data = parent
        key = _CHILD_KEYS[kind].get(element.tag)
        if key is not None:
            data.setdefault(key, element.text)
        elif kind == 'document' and element.tag == _ATOM_LINK:
            self._bounded(self._links).append(
                {name: element.attrib[name] for name in ('href', 'rel', 'type') if name in element.attrib})

    # ------------------------------------------------------------------ Sous-arbres

    def subtree(self, element: ET.Element) -> None:
        """
        Un seul parcours du sous-arbre : il relève ce que le document déclare n'importe où
        (styles, dates, traces) et, pour un Placemark, de quoi construire ses features.
        """
        is_placemark = element.tag == _PLACEMARK
        firsts: Dict[str, ET.Element] = {}
        geometry: Dict[str, List[ET.Element]] = {tag: [] for tag in _GEOMETRIES} if is_placemark else {}
        harvesters = self._harvesters
        for node in element.iter():
            tag = node.tag
            harvest = harvesters.get(tag)
            if harvest is not None:
                harvest(node)
            if is_placemark and node is not element:
                if tag in geometry:
                    geometry[tag].append(node)
                elif tag in _FIRSTS and tag not in firsts:
                    firsts[tag] = node
        if not is_placemark:
            return
        extracted = KMLParser._placemark_features(element, self._placemarks, firsts, geometry)
        self._placemarks += 1
        # Un Placemark ou une feature sans point compte pour un : le plafond borne aussi
        # le nombre d'entités (500 000 polygones vides tenaient dans 16 Mo)
        self._points += max(1, sum(max(1, KMLParser._point_count(feature)) for feature in extracted))
        if self._points > file_formats.MAX_FILE_POINTS:
            raise file_formats.TooManyPointsError()
        self.features.extend(extracted)

    def _timestamp(self, node: ET.Element) -> None:
        if self._timestamps is None:
            self._timestamps = []
        when = node.find(_kml('when'))
        if when is not None and when.text:
            self._bounded(self._timestamps).append(when.text)

    def _timespan(self, node: ET.Element) -> None:
        if self._timespans is None:
            self._timespans = []
        span = {}
        for key in ('begin', 'end'):
            child = node.find(_kml(key))
            if child is not None:
                span[key] = child.text
        self._bounded(self._timespans).append(span)

    def _style(self, node: ET.Element) -> None:
        style_id = node.get('id')
        if style_id:
            if style_id not in self._styles:
                self._bounded(self._styles)
            self._styles[style_id] = KMLParser._style_data(node)

    def _style_map(self, node: ET.Element) -> None:
        style_id = node.get('id')
        if style_id:
            self._bounded(self._style_maps).append((style_id, KMLParser._style_map_data(node)))

    def _nested_tour(self, node: ET.Element) -> None:
        tour = {}
        for key, tag in (('name', _NAME), ('description', _DESCRIPTION)):
            child = node.find(tag)
            if child is not None:
                tour[key] = child.text
        self._bounded(self._tours).append(tour)

    def _count_track(self, _node: ET.Element) -> None:
        self._tracks += 1

    def _count_multitrack(self, _node: ET.Element) -> None:
        self._multitracks += 1

    # -------------------------------------------------------------------- Résultat

    def metadata(self) -> Dict[str, Any]:
        """Métadonnées du document, une fois la lecture terminée."""
        metadata: Dict[str, Any] = {}
        document = self._document or {}
        for key in ('title', 'description', 'author'):
            if key in document:
                metadata[key] = document[key]
        if self._links:
            metadata['links'] = self._links
        if self._timestamps is not None:
            metadata['timestamps'] = self._timestamps
        if self._timespans is not None:
            metadata['timespans'] = self._timespans

        extensions: Dict[str, Any] = {}
        if self._tours:
            extensions['tours'] = self._tours
        if self._tracks:
            extensions['track_count'] = self._tracks
        if self._multitracks:
            extensions['multitrack_count'] = self._multitracks
        if extensions:
            metadata['google_extensions'] = extensions

        styles = dict(self._styles)
        for style_id, pairs in self._style_maps:
            # Une StyleMap prend les couleurs de son style « normal » : c'est elle que
            # les placemarks référencent dans les fichiers Google Earth
            target = styles.get(str(pairs.get('normal') or '').lstrip('#'))
            styles[style_id] = {**(target or {}), **pairs}
        if styles:
            metadata['styles'] = styles
        if self._folders:
            metadata['folders'] = self._folders
        return metadata

