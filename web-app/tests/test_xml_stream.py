"""
Lecture en flux des fichiers KML et GPX : mémoire bornée par ce qui est en cours de
traitement, profondeur plafonnée, et même résultat que la lecture de l'arbre complet.
"""

import tracemalloc

import pytest

from app.services import file_formats, xml_stream
from app.services.gpx_parser import GPXParser
from app.services.kml_parser import KMLParser

KML = '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2" ' \
      'xmlns:atom="http://www.w3.org/2005/Atom"><Document>%s</Document></kml>'
GPX = '<gpx xmlns="http://www.topografix.com/GPX/1/1">%s</gpx>'
POINT = '<Placemark><name>%s</name><Point><coordinates>6.1,45.1,1200</coordinates></Point></Placemark>'


def _peak_mb(function, text):
    """Mémoire allouée par l'appel, au plus haut, en plus du texte déjà en mémoire."""
    tracemalloc.start()
    try:
        result = function(text)
        return result, tracemalloc.get_traced_memory()[1] / 1e6
    finally:
        tracemalloc.stop()


# ------------------------------------------------------------------ Mémoire bornée

@pytest.mark.parametrize('parse, document', [
    (KMLParser.parse_kml_coordinates, KML), (GPXParser.parse_gpx_coordinates, GPX)])
def test_ignored_elements_are_not_kept_in_memory(parse, document):
    """2 Mo de balises sans intérêt : l'arbre complet en coûtait ~50 Mo, le flux n'en garde rien."""
    result, peak = _peak_mb(parse, document % ('<a><b/></a>' * 180_000))
    assert result['success'] and result['features'] == []
    assert peak < 3


def test_a_placemark_is_released_once_read():
    """La mémoire tenue pendant la lecture est celle du résultat, pas celle du document."""
    padding = '<ExtendedData>' + '<Data name="k%d"><value>v</value></Data>' * 50 + '</ExtendedData>'
    heavy = '<Placemark><name>p</name>' + (padding % tuple(range(50))) + '</Placemark>'  # sans géométrie
    result, peak = _peak_mb(KMLParser.parse_kml_coordinates, KML % (heavy * 4000))
    assert result['success'] and result['features'] == []
    # 4000 repères de 50 propriétés, lus puis libérés un par un : ~3 Mo quel que soit leur
    # nombre, là où l'arbre complet de ce document en occupait 92
    assert peak < 8


def test_kml_points_are_the_markers_themselves():
    """Pas de copie par repère : points[k] est le k-ième marqueur des features."""
    result = KMLParser.parse_kml_coordinates(KML % (POINT % 'a' + POINT % 'b'))
    markers = [feature for feature in result['features'] if feature['type'] == 'marker']
    assert len(result['points']) == 2 and all(point is marker for point, marker in zip(result['points'], markers))


# -------------------------------------------------------------- Profondeur et limites

@pytest.mark.parametrize('parse, document', [
    (KMLParser.parse_kml_coordinates, KML), (GPXParser.parse_gpx_coordinates, GPX)])
def test_runaway_nesting_is_refused(parse, document):
    """Des balises ouvertes sans fin tiendraient toutes en mémoire : refus dès la limite franchie."""
    assert parse(document % ('<a>' * 150 + '</a>' * 150))['success']
    deep = parse(document % ('<a>' * 300_000))  # jamais refermées
    assert not deep['success']
    assert xml_stream.MAX_DEPTH >= 100  # large marge : un fichier réel reste sous la trentaine


@pytest.mark.parametrize('body', [
    '<atom:link href="x"/>' * 60, '<Folder/>' * 60, '<TimeStamp><when>2024</when></TimeStamp>' * 60,
    '<Style id="s%d"/>' * 60 % tuple(range(60)), '<gx:Tour/>' * 60, '<Placemark/>' * 60,
])
def test_everything_the_kml_reader_accumulates_is_capped(body, monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 50)
    result = KMLParser.parse_kml_coordinates(KML % body)
    assert not result['success'] and 'trop volumineux' in result['error']


def test_empty_gpx_tracks_count_towards_the_limit(monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 50)
    assert GPXParser.parse_gpx_coordinates(GPX % ('<trk/>' * 50))['success']
    result = GPXParser.parse_gpx_coordinates(GPX % ('<trk/>' * 51 + '<rte/>' * 51))
    assert not result['success'] and 'trop volumineux' in result['error']


@pytest.mark.parametrize('parse, document', [
    (KMLParser.parse_kml_coordinates, KML), (GPXParser.parse_gpx_coordinates, GPX)])
def test_entities_and_malformed_documents_are_still_refused(parse, document):
    bomb = '<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>' + document % '<name>&b;</name>'
    external = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>' + document % '<name>&e;</name>'
    for hostile in (bomb, external, document % '<a>', (document % '')[:-3]):
        assert not parse(hostile)['success']


# ------------------------------------------------------------------ Même résultat

def test_kml_document_is_read_in_one_pass():
    body = (
        '<name>Doc</name><description>D</description>'
        '<atom:author><atom:name>Moi</atom:name></atom:author><atom:link href="http://x" rel="self"/>'
        '<Style id="s1"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>'
        '<StyleMap id="m1"><Pair><key>normal</key><styleUrl>#s1</styleUrl></Pair></StyleMap>'
        '<gx:Tour><name>Visite</name></gx:Tour>'
        '<Folder><name>F</name>'
        '<Placemark><name>P</name><description>Vitesse: 12 km/h</description><styleUrl>#m1</styleUrl>'
        '<TimeStamp><when>2024-03-03T10:00:00Z</when></TimeStamp>'
        '<ExtendedData><Data name="a"><value>1</value></Data></ExtendedData>'
        '<Style id="local"><PolyStyle><color>7f00ff00</color></PolyStyle></Style>'
        '<MultiGeometry><Point><coordinates>6.1,45.1,1200</coordinates></Point>'
        '<LineString><coordinates>6,45,1 6.1,45.1,2</coordinates></LineString></MultiGeometry></Placemark>'
        '<Folder><name>G</name><Placemark><gx:Track><when>2024-01-01T00:00:00Z</when>'
        '<gx:coord>6 45 100</gx:coord></gx:Track></Placemark></Folder></Folder>'
        '<Placemark><name>Dehors</name><Point><coordinates>7,46</coordinates></Point>'
        '<Placemark><name>Dedans</name><Point><coordinates>8,47</coordinates></Point></Placemark></Placemark>'
    )
    result = KMLParser.parse_kml_coordinates(KML % body)
    assert result['success']
    metadata = result['metadata']
    assert (metadata['title'], metadata['description'], metadata['author']) == ('Doc', 'D', 'Moi')
    assert metadata['links'] == [{'href': 'http://x', 'rel': 'self'}]
    assert metadata['timestamps'] == ['2024-03-03T10:00:00Z']
    assert metadata['google_extensions'] == {'tours': [{'name': 'Visite'}], 'track_count': 1}
    # Un style déclaré dans un Placemark est connu ; la StyleMap prend les couleurs de son style normal
    assert metadata['styles']['local'] == {'poly_color': '7f00ff00'}
    assert metadata['styles']['m1'] == {'line_color': 'ff0000ff', 'line_width': '3', 'normal': '#s1'}
    assert metadata['folders'] == [{'name': 'F', 'placemark_count': 2}, {'name': 'G', 'placemark_count': 1}]

    assert [(f['type'], f['name']) for f in result['features']] == [
        ('marker', 'P'), ('polyline', 'P'), ('track', 'Élément 2'), ('marker', 'Dehors'), ('marker', 'Dehors')]
    marker = result['features'][0]
    assert marker['style'] == '#m1' and marker['properties'] == {'a': '1'}
    assert marker['time_info'] == {'timestamp': '2024-03-03T10:00:00Z'}
    assert marker['parsed_info']['speed_kmh'] == 12 and marker['coordinates'] == [45.1, 6.1]


def test_gpx_output_order_does_not_depend_on_document_order():
    """Repères, routes, puis chaque trace précédée de ses points, où qu'ils soient dans le fichier."""
    track = ('<trk><name>T</name><trkseg><trkpt lat="45" lon="6"><ele>1</ele><time>2025-06-01T08:00:00Z</time></trkpt>'
             '<trkpt lat="45.001" lon="6"><time>2025-06-01T08:01:00Z</time></trkpt></trkseg></trk>')
    route = '<rte><name>R</name><rtept lat="45.1" lon="6.1"/><rtept lat="45.2" lon="6.2"/></rte>'
    waypoint = '<wpt lat="45.5" lon="6.5"><name>Col</name></wpt>'
    expected = GPXParser.parse_gpx_coordinates(GPX % (waypoint + route + track))
    shuffled = GPXParser.parse_gpx_coordinates(GPX % (track + route + waypoint))
    assert expected['success'] and shuffled == expected
    assert [(f['type'], f['name']) for f in shuffled['features']] == [
        ('marker', 'Col'), ('polyline', 'R'), ('marker', 'trkpt_1'), ('marker', 'trkpt_2'), ('polyline', 'T')]
    assert shuffled['points'][2]['parsed_info']['speed_kmh'] is not None


def test_gpx_points_out_of_place_are_ignored():
    misplaced = ('<metadata><name>M</name><wpt lat="1" lon="1"/></metadata><trkpt lat="2" lon="2"/>'
                 '<trk><trkpt lat="3" lon="3"/><trk><trkseg><trkpt lat="4" lon="4"/></trkseg></trk></trk>')
    result = GPXParser.parse_gpx_coordinates(GPX % misplaced)
    assert result['success'] and result['points'] == [] and result['metadata'] == {'title': 'M'}
    assert [f['coordinates'] for f in result['features']] == [[]]  # la trace, sans point
