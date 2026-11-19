"""
Parsers face à des fichiers hostiles ou inhabituels : chaque fichier reste borné en temps
et en volume, les valeurs non finies sont refusées, les variantes de format sont lues.
Les gardes de temps sont larges : elles distinguent « linéaire » de « quadratique »
(secondes contre minutes), pas une machine rapide d'une machine lente.
"""

import io
import json
import time
import zipfile

import pytest
from xml.sax.saxutils import escape

from app.services import cache_service, file_formats, segment_service
from app.services.gpx_parser import GPXParser
from app.services.kml_editor import KMLEditor
from app.services.kml_parser import HEADING_PATTERN, MAX_DESCRIPTION_SCAN, SPEED_PATTERNS, KMLParser
from tests.conftest import line, make_gpx

KML = '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>%s</Document></kml>'
GPX = '<gpx xmlns="http://www.topografix.com/GPX/1/1">%s</gpx>'
LINE = '<Placemark><name>t</name><LineString><coordinates>6,45,0 6.01,45,0</coordinates></LineString></Placemark>'
TRACK = ('<trk><name>T</name><trkseg><trkpt lat="45" lon="6"><ele>%s</ele><time>2025-06-01T08:00:00Z</time></trkpt>'
         '<trkpt lat="45.001" lon="6"><ele>100</ele><time>2025-06-01T08:01:00Z</time></trkpt></trkseg></trk>')


def _timed(function, *args):
    start = time.perf_counter()
    result = function(*args)
    return result, time.perf_counter() - start


def _point(description):
    return KML % (f'<Placemark><description>{escape(description)}</description>'
                  '<Point><coordinates>6,45</coordinates></Point></Placemark>')


# ------------------------------------------------------------ Descriptions (ReDoS)

@pytest.mark.parametrize('description', [
    'gs' + ' ' * 200_000, 'cap' + ' ' * 200_000, '\n' * 200_000, 'vitesse:' + ' ' * 200_000 + 'x',
    'speed ' + '9' * 200_000, ';' * 100_000 + ' ' * 100_000, '<br' + ' ' * 200_000,
])
def test_description_patterns_do_not_backtrack(description):
    # Avant : 25 s pour 20 000 espaces, quadratique, GIL tenu (toute l'application gelée)
    for pattern in [HEADING_PATTERN, *(p for p, _ in SPEED_PATTERNS)]:
        _, elapsed = _timed(pattern.search, description)  # motif seul, sans la borne de longueur
        assert elapsed < 2, pattern.pattern
    result, elapsed = _timed(KMLParser.parse_kml_coordinates, _point(description))
    assert result['success'] and elapsed < 2 and len(result['points']) == 1


def test_only_the_start_of_a_description_is_scanned():
    late = 'x' * (MAX_DESCRIPTION_SCAN + 10) + '\nVitesse: 50 km/h'
    assert KMLParser.parse_gps_description(late, 45.0, 6.0, 0)['speed_kmh'] is None
    assert KMLParser.parse_gps_description('Vitesse: 50 km/h ' + 'x' * 5000, 45.0, 6.0, 0)['speed_kmh'] == 50.0
    # La description elle-même n'est pas tronquée
    assert KMLParser.parse_gps_description(late, 45.0, 6.0, 0)['raw_description'] == late


# ------------------------------------------------------------------ Volumes par fichier

def test_features_without_points_count_towards_the_limit(monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 50)
    for body in ('<Placemark><Polygon/></Placemark>' * 51,                       # polygones vides
                 '<Placemark/>' * 51,                                             # Placemarks sans géométrie
                 '<Placemark><MultiGeometry>' + '<Polygon/>' * 51 + '</MultiGeometry></Placemark>',
                 '<Folder/>' * 51):
        result = KMLParser.parse_kml_coordinates(KML % body)
        assert not result['success'] and 'trop volumineux' in result['error'], body[:40]
    assert KMLParser.parse_kml_coordinates(KML % ('<Placemark><Polygon/></Placemark>' * 50))['success']


def test_oversized_coordinates_are_refused_without_materialising_them(monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 1000)
    calls = []
    real = file_formats.finite_float
    monkeypatch.setattr(file_formats, 'finite_float', lambda value: calls.append(1) or real(value))
    body = '<Placemark><LineString><coordinates>' + '1,1 ' * 500_000 + '</coordinates></LineString></Placemark>'
    result = KMLParser.parse_kml_coordinates(KML % body)
    assert not result['success'] and 'trop volumineux' in result['error']
    assert len(calls) < 2 * 1002  # lecture arrêtée au plafond, pas après 500 000 points


def test_nested_folders_are_scanned_once():
    # Avant : un parcours de sous-arbre par dossier, 8 s pour 20 000 dossiers imbriqués.
    # La lecture en flux borne la profondeur (xml_stream.MAX_DEPTH) : les dossiers sont ici
    # imbriqués par groupes de 150, bien au-delà de ce qu'un fichier réel contient.
    depth, groups = 150, 400
    body = ('<Folder><name>d</name>' * depth + LINE + '</Folder>' * depth) * groups
    result, elapsed = _timed(KMLParser.parse_kml_coordinates, KML % body)
    assert result['success'] and elapsed < 10
    folders = result['metadata']['folders']
    assert len(folders) == depth * groups and all(f['placemark_count'] == 1 for f in (folders[0], folders[-1]))


def test_folder_counts_and_order_are_kept():
    body = ('<Folder><name>A</name>' + LINE + '<Folder><name>A1</name>' + LINE * 2 + '</Folder></Folder>'
            '<Folder><name>B</name></Folder>' + LINE)
    result = KMLParser.parse_kml_coordinates(KML % body)
    assert [(f['name'], f['placemark_count']) for f in result['metadata']['folders']] == [('A', 3), ('A1', 2), ('B', 0)]
    assert len(result['features']) == 4


def test_nested_placemarks_and_many_geometries_stay_linear():
    nested = ('<Placemark>' * 150 + '<Point><coordinates>6,45</coordinates></Point>' + '</Placemark>' * 150) * 200
    result, elapsed = _timed(KMLParser.parse_kml_coordinates, KML % nested)
    assert result['success'] and len(result['features']) == 200 and elapsed < 10
    # Les données du Placemark ne sont plus relues pour chacune de ses géométries
    many = '<Placemark><MultiGeometry>' + '<Polygon/>' * 20_000 + '</MultiGeometry></Placemark>'
    result, elapsed = _timed(KMLParser.parse_kml_coordinates, KML % many)
    assert result['success'] and len(result['features']) == 20_000 and elapsed < 10


def test_gpx_routes_count_towards_the_limit(monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 50)
    route = '<rte>' + '<rtept lat="45" lon="6"/>' * 51 + '</rte>'
    assert 'trop volumineux' in GPXParser.parse_gpx_coordinates(GPX % route)['error']


# ------------------------------------------------------------------ Valeurs non finies

@pytest.mark.parametrize('value', ['inf', '-inf', 'nan', 'Infinity', '1e999', '1_0', '0x10', '', 'abc', '1e9'])
def test_gpx_refuses_non_finite_or_absurd_values(value):
    for content in (GPX % (TRACK % value), GPX % (TRACK % '100').replace('lat="45"', f'lat="{value}"')):
        if value == '' and '<ele></ele>' in content:
            continue  # altitude vide : absente, voir test_gpx_variants_are_read
        if value == '1e9' and 'lat="1e9"' in content:
            continue  # fini : hors plage, écarté plus loin (activity_stats), pas ici
        result = GPXParser.parse_gpx_coordinates(content)
        assert result == {'success': False, 'error': 'Fichier GPX invalide'}, (value, content[:80])


def test_kml_skips_non_finite_points_and_stays_json_safe():
    coords = '6,45,100 inf,45,0 6,nan,0 6,45,inf 6,45,1e999 1_0,45,0 6.001,45.001,110 6,45,1e9'
    body = (f'<Placemark><LineString><coordinates>{coords}</coordinates></LineString></Placemark>'
            '<Placemark><Point><coordinates>nan,inf,1_0</coordinates></Point></Placemark>'
            '<Placemark><Point><coordinates>6,45,inf</coordinates></Point></Placemark>'
            '<Placemark xmlns:gx="http://www.google.com/kml/ext/2.2"><gx:Track><when>2025-06-01T08:00:00Z</when>'
            '<when>2025-06-01T08:01:00Z</when><gx:coord>6 45 inf</gx:coord><gx:coord>6 45 10</gx:coord></gx:Track></Placemark>')
    result = KMLParser.parse_kml_coordinates(KML % body)
    assert result['success']
    json.dumps(result, allow_nan=False)  # ni NaN ni Infinity dans la réponse
    polyline = next(f for f in result['features'] if f['type'] == 'polyline')
    assert polyline['coordinates'] == [[45.0, 6.0, 100.0], [45.001, 6.001, 110.0]]
    assert result['points'] == []
    track = next(f for f in result['features'] if f['type'] == 'track')
    assert track['coordinates'] == [[45.0, 6.0, 10.0]] and track['timestamps'] == ['2025-06-01T08:01:00Z']


def test_finite_float_helper():
    assert [file_formats.finite_float(v) for v in ('6', ' -6.5 ', '.5', '6.', '1e3', '+2E-2')] == [6, -6.5, 0.5, 6, 1000, 0.02]
    assert file_formats.altitude(None) == file_formats.altitude('  ') == 0.0
    for bad in ('inf', 'nan', '1_0', '1e999', '', None, '١٢'):
        with pytest.raises(ValueError):
            file_formats.finite_float(bad)
    with pytest.raises(ValueError):
        file_formats.altitude('1e7')


# ------------------------------------------------------------------ Variantes de format

@pytest.mark.parametrize('namespace', ['http://www.opengis.net/kml/2.2', 'http://earth.google.com/kml/2.1',
                                       'http://earth.google.com/kml/2.0', None])
def test_kml_namespace_variants_are_read(namespace):
    content = KML % ('<Folder><name>D</name>' + LINE + '</Folder>')
    content = content.replace(' xmlns="http://www.opengis.net/kml/2.2"', f' xmlns="{namespace}"' if namespace else '')
    result = KMLParser.parse_kml_coordinates(content)
    assert result['success'] and [f['type'] for f in result['features']] == ['polyline']
    assert result['metadata']['folders'] == [{'name': 'D', 'placemark_count': 1}]


def test_kml_extensions_survive_namespace_normalisation():
    content = ('<kml xmlns="http://earth.google.com/kml/2.1" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>'
               '<Placemark><gx:Track><when>2025-06-01T08:00:00Z</when><gx:coord>6 45 0</gx:coord></gx:Track></Placemark>'
               '</Document></kml>')
    track = KMLParser.parse_kml_coordinates(content)['features'][0]
    assert track['type'] == 'track' and track['timestamps'] == ['2025-06-01T08:00:00Z']


def test_unknown_root_namespace_is_left_alone():
    result = KMLParser.parse_kml_coordinates(KML.replace('http://www.opengis.net/kml/2.2', 'urn:autre') % LINE)
    assert result['success'] and result['features'] == []


@pytest.mark.parametrize('namespace', ['http://www.topografix.com/GPX/1/1', 'http://www.topografix.com/GPX/1/0', None])
def test_gpx_variants_are_read(namespace):
    body = ('<name>Fichier</name><time>2024-03-02T10:00:00Z</time><wpt lat="45" lon="6"><ele/><name>Col</name></wpt>'
            '<rte><name>Itinéraire</name><rtept lat="45" lon="6"><ele>10</ele></rtept><rtept lat="45.01" lon="6"/></rte>'
            + (TRACK % '').replace('<ele></ele>', '<ele/>'))
    content = (GPX % body).replace(' xmlns="http://www.topografix.com/GPX/1/1"', f' xmlns="{namespace}"' if namespace else '')
    result = GPXParser.parse_gpx_coordinates(content)
    assert result['success'] and result['total_points'] == 3
    lines = [f for f in result['features'] if f['type'] == 'polyline']
    assert [(f['name'], len(f['coordinates'])) for f in lines] == [('Itinéraire', 2), ('T', 2)]
    assert lines[0]['coordinates'] == [[45.0, 6.0, 10.0], [45.01, 6.0, 0.0]] and lines[0]['time_info'] == {'timestamps': []}
    assert result['points'][0]['altitude'] == 0.0 and result['points'][1]['altitude'] == 0.0  # <ele/> : absente
    assert result['metadata']['title'] == 'Fichier' and result['metadata']['time'] == '2024-03-02T10:00:00Z'


def test_track_markers_keep_the_viewer_contract():
    """Le k-ième marqueur des features est points[k] ; un point de trace est masqué et porte ce que lit le front."""
    result = GPXParser.parse_gpx_coordinates(GPX % ('<wpt lat="45" lon="6"><name>Col</name></wpt>' + TRACK % '100'))
    markers = [f for f in result['features'] if f['type'] == 'marker']
    assert all(a is b for a, b in zip(markers, result['points'])) and len(markers) == result['total_points'] == 3
    waypoint, first, second = result['points']
    assert waypoint['visibility'] == '1' and first['visibility'] == second['visibility'] == '0'
    assert set(first) >= {'type', 'name', 'description', 'coordinates', 'altitude', 'visibility', 'time_info', 'parsed_info'}
    assert first['time_info'] == {'timestamp': '2025-06-01T08:00:00Z'} and first['altitude'] == 100.0
    assert set(second['parsed_info']) >= {'speed_kmh', 'speed_kts', 'heading', 'altitude_m'}
    assert second['parsed_info']['speed_kmh'] > 0 and second['parsed_info']['altitude_m'] == 100.0


# ------------------------------------------------------------------ Encodage, KMZ

def test_declared_encoding_is_honoured():
    latin = '<?xml version="1.0" encoding="ISO-8859-1"?>' + GPX % '<trk><name>Tracé d\'été</name></trk>'
    for raw in (latin.encode('latin-1'), latin.replace('ISO-8859-1', 'UTF-16').encode('utf-16'),
                latin.replace('ISO-8859-1', 'UTF-8').encode('utf-8-sig'), (GPX % "<trk><name>Tracé d'été</name></trk>").encode()):
        content, fmt = file_formats.decode(raw, 'a.gpx')
        assert fmt == 'gpx'
        result = GPXParser.parse_gpx_coordinates(content)
        assert result['success'] and result['features'][0]['name'] == "Tracé d'été"


@pytest.mark.parametrize('raw', [
    b'<?xml version="1.0" encoding="utf-7"?><gpx/>',            # encodage hors liste
    b'<?xml version="1.0" encoding="rot_13"?><gpx/>',
    b'<?xml version="1.0" encoding="nimporte"?><gpx/>',
    b'<?xml version="1.0" encoding="ascii"?><gpx>\xe9</gpx>',   # contenu contraire à la déclaration
    b'\xff\xfe\x00', b'<gpx>\xff</gpx>',
])
def test_bad_encodings_are_refused(raw):
    with pytest.raises(file_formats.FileFormatError):
        file_formats.decode(raw, 'a.gpx')


def test_entity_attacks_stay_refused_whatever_the_encoding():
    xxe = ('<?xml version="1.0" encoding="ISO-8859-1"?><!DOCTYPE gpx [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
           + GPX % '<trk><name>&xxe;</name></trk>')
    for raw in (xxe.encode('latin-1'), xxe.replace('ISO-8859-1', 'UTF-16').encode('utf-16')):
        content, _ = file_formats.decode(raw, 'a.gpx')
        result = GPXParser.parse_gpx_coordinates(content)
        assert not result['success'] and 'root:' not in json.dumps(result)


def test_corrupted_kmz_is_a_format_error():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('doc.kml', KML % (LINE * 2000))
    raw = bytearray(buffer.getvalue())
    raw[60:80] = b'\xff' * 20  # flux deflate corrompu : zipfile laisse remonter zlib.error
    with pytest.raises(file_formats.FileFormatError, match='illisible'):
        file_formats.decode(bytes(raw), 'x.kmz')


def test_safe_name_keeps_the_extension():
    name = file_formats.safe_name('a' * 250 + '.GPX')
    assert len(name) == 200 and file_formats.extension(name) == 'gpx'
    assert file_formats.safe_name('../../etc/evil.gpx') == 'evil.gpx'
    assert file_formats.safe_name('..\\..\\evil.kml') == 'evil.kml'
    assert file_formats.safe_name('') == 'fichier' and file_formats.safe_name('court.kmz') == 'court.kmz'


# ------------------------------------------------------------------ Simplification

def _sawtooth(count):
    """Zigzag d'amplitude décroissante : Douglas-Peucker n'en retire qu'un point par découpe."""
    return [[45 + (1 if i % 2 else -1) * 0.01 * (count - i) / count, 6 + i * 1e-5] for i in range(count)]


def test_simplification_work_is_bounded():
    # Avant : 37 s pour 40 000 points, quadratique
    coordinates = _sawtooth(40_000)
    kept, elapsed = _timed(KMLEditor.simplify_indices, coordinates, 1e-9)
    assert elapsed < 10
    # Résultat toujours valide : extrémités gardées, indices croissants, rien d'inventé
    assert kept[0] == 0 and kept[-1] == len(coordinates) - 1 and kept == sorted(set(kept))


def test_simplification_refuses_more_points_than_a_file_may_hold(monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 100)
    assert len(KMLEditor.simplify_indices([[45 + i * 1e-4, 6.0] for i in range(100)])) == 2
    with pytest.raises(ValueError, match='trop volumineux'):
        KMLEditor.simplify_indices([[45 + i * 1e-4, 6.0] for i in range(101)])


def test_ordinary_trace_is_fully_simplified_within_the_budget(caplog):
    # Sinusoïde bruitée de 50 000 points : le budget ne doit pas tronquer un cas réel
    import math
    coordinates = [[45 + 0.001 * math.sin(i / 300), 6 + i * 1e-5] for i in range(50_000)]
    kept = KMLEditor.simplify_indices(coordinates, 1e-5)
    assert 50 < len(kept) < 5000 and 'interrompue' not in caplog.text


# ------------------------------------------------------------------ Tronçons

def test_segment_search_is_bounded_on_an_oscillating_trace(caplog):
    # Avant : 28 s pour 4 000 points (un suivi complet relancé à chaque passage au départ)
    metre = 1 / 111320
    seg_lat, seg_lon = [45 + i * 15 * metre for i in range(40)], [6.0] * 40
    count = 40_000
    lat = [45 + (45 * metre if i % 2 else 0) for i in range(count)]
    efforts, elapsed = _timed(segment_service.find_efforts, seg_lat, seg_lon, lat, [6.0] * count, list(range(count)))
    assert efforts == [] and elapsed < 20
    assert 'interrompue' in caplog.text


def test_segment_search_still_finds_every_lap():
    metre = 1 / 111320
    seg_lat, seg_lon = [45 + i * 15 * metre for i in range(40)], [6.0] * 40
    lap = [45 + i * 15 * metre for i in range(40)] + [45 + i * 15 * metre for i in range(39, 0, -1)]
    lat = lap * 20
    efforts = segment_service.find_efforts(seg_lat, seg_lon, lat, [6.0] * len(lat), list(range(len(lat))))
    assert len(efforts) == 20


# ------------------------------------------------------------------ Cache

def test_cache_counts_what_a_result_really_holds(monkeypatch):
    cache_service.clear_cache()
    points = '<trk><trkseg>' + '<trkpt lat="1" lon="1"/>' * 2000 + '</trkseg></trk>'
    content = GPX % points
    result = GPXParser.parse_gpx_coordinates(content)
    estimate = cache_service.estimate_memory(result, len(content))
    assert estimate > 20 * len(content)  # 24 octets par point dans le fichier, plus d'un ko en mémoire

    monkeypatch.setattr(cache_service, 'MAX_ENTRY_MEMORY', estimate - 1)
    cache_service.parse_gpx_cached(content)
    assert len(cache_service._parse_cache) == 0  # petit fichier, gros résultat : pas retenu

    monkeypatch.setattr(cache_service, 'MAX_ENTRY_MEMORY', estimate + 1000)
    monkeypatch.setattr(cache_service, 'MAX_TOTAL_MEMORY', 2 * estimate + 2000)
    for k in range(4):
        cache_service.parse_gpx_cached(GPX % (f'<trk><name>{k}</name></trk>' + points))
    assert len(cache_service._parse_cache) == 2
    assert set(cache_service._memory) == set(cache_service._parse_cache) == set(cache_service._weights)
    assert sum(cache_service._memory.values()) <= 2 * estimate + 2000
    cache_service.clear_cache()
    assert not cache_service._memory


def test_display_mode_is_still_accepted_and_ignored():
    content = KML % LINE
    assert KMLParser.parse_kml_coordinates(content, 'double') == KMLParser.parse_kml_coordinates(content)
    cache_service.clear_cache()
    assert cache_service.parse_kml_cached(content, 'simple') is cache_service.parse_kml_cached(content)
    cache_service.clear_cache()


def test_long_gpx_is_parsed_in_bounded_time():
    result, elapsed = _timed(GPXParser.parse_gpx_coordinates, make_gpx(line(50_000, dlat=0.00003), step_s=1))
    assert result['success'] and result['total_points'] == 50_000 and elapsed < 20
