"""
Fidélité aux données : ce qui est affiché ou exporté vient du fichier, jamais d'une
estimation ; rien n'est perdu ni remplacé en silence.
"""

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.services import activity_stats, ingest_service
from app.services.gpx_parser import GPXParser
from app.services.kml_editor import KMLEditor
from app.services.kml_parser import KMLParser
from app.services.trajectory_analyzer import TrajectoryAnalyzer
from tests.conftest import line, make_gpx

T0 = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)

KML = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2"><Document>
<Style id="rouge"><LineStyle><color>ff0000ff</color><width>4</width></LineStyle></Style>
<StyleMap id="carte"><Pair><key>normal</key><styleUrl>#rouge</styleUrl></Pair>
 <Pair><key>highlight</key><styleUrl>#rouge</styleUrl></Pair></StyleMap>
<Placemark><name>Refuge</name><description>Ouvert en été, eau potable</description>
 <TimeStamp><when>2025-06-01T08:00:00Z</when></TimeStamp><Point><coordinates>6.0,45.0</coordinates></Point></Placemark>
<Placemark><name>Multi</name><styleUrl>#carte</styleUrl><MultiGeometry>
 <LineString><coordinates>6.0,45.0,100 6.001,45.001,110</coordinates></LineString>
 <LineString><coordinates>6.1,45.1,100 6.101,45.101,110 6.102,45.102,120</coordinates></LineString>
 <Point><coordinates>6.1,45.1,100</coordinates></Point></MultiGeometry></Placemark>
<Placemark><name>Zone</name><Polygon>
 <outerBoundaryIs><LinearRing><coordinates>6,45 6.1,45 6.1,45.1 6,45.1 6,45</coordinates></LinearRing></outerBoundaryIs>
 <innerBoundaryIs><LinearRing><coordinates>6.04,45.04 6.06,45.04 6.06,45.06 6.04,45.04</coordinates></LinearRing></innerBoundaryIs>
</Polygon></Placemark>
<Placemark><name>Trace Google</name><gx:Track>
 <when>2025-06-01T08:00:00Z</when><when>2025-06-01T08:01:00Z</when><when>2025-06-01T08:02:00Z</when><when>2025-06-01T08:03:00Z</when>
 <gx:coord>6.0 45.0 100</gx:coord><gx:coord>illisible</gx:coord><gx:coord>6.0 45.005 120</gx:coord><gx:coord>6.0 45.01 140</gx:coord>
 <ExtendedData><SchemaData schemaUrl="#s"><gx:SimpleArrayData name="speed">
  <gx:value>12</gx:value><gx:value>99</gx:value><gx:value>13</gx:value><gx:value>14</gx:value></gx:SimpleArrayData></SchemaData></ExtendedData>
</gx:Track></Placemark>
<Placemark><name>Deux jours</name><gx:MultiTrack>
 <gx:Track><when>2025-06-02T08:00:00Z</when><when>2025-06-02T08:01:00Z</when>
  <gx:coord>7.0 46.0 0</gx:coord><gx:coord>7.0 46.001 0</gx:coord></gx:Track>
 <gx:Track><when>2025-06-03T08:00:00Z</when><when>2025-06-03T08:01:00Z</when>
  <gx:coord>7.1 46.0 0</gx:coord><gx:coord>7.1 46.001 0</gx:coord></gx:Track>
</gx:MultiTrack></Placemark>
</Document></kml>"""


@pytest.fixture
def parsed():
    result = KMLParser.parse_kml_coordinates(KML)
    assert result['success']
    return result


def by_type(features, kind):
    return [f for f in features if f['type'] == kind]


def paused_gpx():
    """10 km/h pendant 15 min, 30 min de pause, 10 km/h pendant 15 min."""
    rows = []
    for i, (lat, lon, ele) in enumerate(line(180, dlat=0.00025)):
        stamp = T0 + timedelta(seconds=i * 10 + (1800 if i >= 90 else 0))
        rows.append(f"<trkpt lat='{lat:.6f}' lon='{lon:.6f}'><ele>{ele}</ele><time>{stamp:%Y-%m-%dT%H:%M:%SZ}</time></trkpt>")
    return ("<?xml version='1.0'?><gpx xmlns='http://www.topografix.com/GPX/1/1'><trk><name>Pause</name>"
            f"<trkseg>{''.join(rows)}</trkseg></trk></gpx>")


# ------------------------------------------------------------- Analyse de base

def test_basic_duration_and_speeds_are_measured_not_estimated():
    features = GPXParser.parse_gpx_coordinates(paused_gpx())['features']
    analysis = TrajectoryAnalyzer.analyze_trajectory(features)
    json.dumps(analysis, allow_nan=False)

    duration = analysis['duration']
    assert duration['total_s'] == pytest.approx(3590, abs=5)       # 59 min 50 s réelles
    assert duration['stopped_s'] == pytest.approx(1800, abs=30)
    assert duration['moving_s'] == pytest.approx(1790, abs=30)
    assert duration['stops_count'] == 1
    assert 'estimated_duration_minutes' not in duration
    speed = analysis['speed']
    assert speed['avg_speed_kmh'] == pytest.approx(10, abs=0.3)   # en mouvement
    assert speed['min_speed_kmh'] < 1                             # l'arrêt se voit
    assert speed['max_speed_kmh'] == pytest.approx(10, abs=0.5)
    assert speed['speed_profile'][-1]['distance'] == pytest.approx(analysis['distance']['total_distance_m'], abs=1)


def test_basic_flat_track_at_altitude_has_no_elevation_gain():
    # L'ancien lissage écrasait les bords vers 0 : +726 m sur une trace plate à 1500 m
    features = [{'type': 'polyline', 'coordinates': [[45 + i * 0.0003, 6.0, 1500.0] for i in range(300)]}]
    elevation = TrajectoryAnalyzer.analyze_trajectory(features)['elevation']
    assert (elevation['total_ascent'], elevation['total_descent']) == (0.0, 0.0)
    assert elevation['min_elevation'] == elevation['max_elevation'] == 1500.0


def test_basic_short_noisy_track_does_not_sum_the_noise():
    rng = np.random.default_rng(0)
    features = [{'type': 'polyline', 'coordinates': [[45 + i * 0.0003, 6.0, 1500 + rng.normal(0, 2)] for i in range(99)]}]
    assert TrajectoryAnalyzer.analyze_trajectory(features)['elevation']['total_ascent'] < 15


def test_basic_untimed_track_reports_no_duration_and_no_speed():
    features = GPXParser.parse_gpx_coordinates(make_gpx(line(50), with_time=False))['features']
    analysis = TrajectoryAnalyzer.analyze_trajectory(features)
    assert analysis['duration'] == {} and analysis['speed'] == {}
    assert analysis['distance']['total_distance_km'] > 1


def test_basic_analysis_counts_google_tracks(parsed):
    analysis = TrajectoryAnalyzer.analyze_trajectory(by_type(parsed['features'], 'track'))
    assert analysis['distance']['total_distance_km'] == pytest.approx(1.11, abs=0.02)
    assert analysis['basic_stats']['has_time_data'] and analysis['duration']['total_s'] == 180
    multi = TrajectoryAnalyzer.analyze_trajectory(by_type(parsed['features'], 'multitrack'))
    # Deux traces à 8 km l'une de l'autre : pas de liaison fantôme entre elles
    assert multi['distance']['total_distance_km'] == pytest.approx(0.22, abs=0.01)


def test_basic_charts_stay_small_on_a_long_track():
    features = GPXParser.parse_gpx_coordinates(make_gpx(line(6000, dlat=0.00003, ele=lambda i: 100 + i * 0.1), step_s=1))['features']
    analysis = TrajectoryAnalyzer.analyze_trajectory(features)
    assert len(analysis['speed']['speed_profile']) <= 1500 and len(analysis['elevation']['elevation_profile']) <= 1500


# ------------------------------------------------------------------ Lecture KML

def test_point_keeps_its_own_description(parsed):
    refuge = parsed['points'][0]
    assert refuge['description'] == 'Ouvert en été, eau potable'
    assert refuge['time_info'] == {'timestamp': '2025-06-01T08:00:00Z'}
    assert 'is_annotation' not in refuge
    assert parsed['metadata']['timestamps'] == ['2025-06-01T08:00:00Z']


def test_every_geometry_of_a_placemark_is_read(parsed):
    multi = [f for f in parsed['features'] if f['name'] == 'Multi']
    assert sorted((f['type'], len(f['coordinates'])) for f in multi) == [('marker', 2), ('polyline', 2), ('polyline', 3)]
    # Le k-ième marqueur des features reste le k-ième point
    assert [p['name'] for p in parsed['points']] == [f['name'] for f in by_type(parsed['features'], 'marker')]
    assert len(by_type(parsed['features'], 'track')) == 1  # les traces d'un MultiTrack ne sont pas comptées deux fois
    assert len(by_type(parsed['features'], 'multitrack')[0]['tracks']) == 2


def test_style_map_resolves_to_its_normal_style(parsed):
    assert parsed['metadata']['styles']['carte']['line_color'] == 'ff0000ff'
    assert parsed['metadata']['styles']['carte']['line_width'] == '4'


def test_google_track_data_and_timestamps_stay_aligned(parsed):
    track = by_type(parsed['features'], 'track')[0]
    # Le point illisible emporte son horodatage et sa mesure
    assert len(track['coordinates']) == 3
    assert track['timestamps'] == ['2025-06-01T08:00:00Z', '2025-06-01T08:02:00Z', '2025-06-01T08:03:00Z']
    assert track['track_data'] == {'speed': ['12', '13', '14']}


@pytest.mark.parametrize('description, speed, heading', [
    ('Vitesse: 120 km/h, Cap: 180°', 120.0, 180.0),
    ('Speed 12 kts hdg 270', 22.2, 270.0),
    ('65 kt<br>FL120', 120.4, None),
    ('Vitesse : 12,5 km/h', 12.5, None),
    ('Sommet avec 2 knots de vent', None, None),
    ('Parking limité à 30 km/h, capacité 40 places', None, None),
    ('Route de 12 km/hameau', None, None),
    ('Cap: 720', None, None),
])
def test_speed_is_only_read_where_it_is_a_measurement(description, speed, heading):
    info = KMLParser.parse_gps_description(description, 45.0, 6.0, 0)
    assert (info['speed_kmh'], info['heading']) == (speed, heading)


# ----------------------------------------------------------------------- Export

def test_gpx_export_round_trip_keeps_time_and_speed():
    original = GPXParser.parse_gpx_coordinates(paused_gpx())
    exported = KMLEditor.export_to_gpx(original['features'], {'name': 'Pause'})
    again = GPXParser.parse_gpx_coordinates(exported)
    assert again['success']
    before, after = by_type(original['features'], 'polyline')[0], by_type(again['features'], 'polyline')[0]
    assert after['coordinates'] == before['coordinates']
    assert after['time_info']['timestamps'] == before['time_info']['timestamps']
    assert TrajectoryAnalyzer.analyze_trajectory(again['features'])['duration']['total_s'] == pytest.approx(3590, abs=5)


def test_kml_export_round_trip_keeps_everything(parsed):
    again = KMLParser.parse_kml_coordinates(KMLEditor.export_to_kml(parsed['features'], {'name': 'Tout'}))
    assert again['success']
    names = lambda features, kind: sorted(f['name'] for f in by_type(features, kind))  # noqa: E731
    assert names(again['features'], 'marker') == names(parsed['features'], 'marker')
    assert again['points'][0]['time_info'] == {'timestamp': '2025-06-01T08:00:00Z'}
    zone = by_type(again['features'], 'polygon')[0]
    assert len(zone['coordinates']) == 5 and len(zone['holes']) == 1
    # Traces horodatées (gx:Track, et les deux d'un MultiTrack) : temps conservé
    tracks = by_type(again['features'], 'track')
    assert len(tracks) == 3 and all(len(t['timestamps']) == len(t['coordinates']) for t in tracks)
    assert len(by_type(again['features'], 'polyline')) == 2


def test_other_exports_keep_polygons_times_and_multitracks(parsed):
    geojson = json.loads(KMLEditor.export_to_geojson(parsed['features']))
    kinds = [f['geometry']['type'] for f in geojson['features']]
    assert kinds.count('Point') == 2 and kinds.count('LineString') == 5 and kinds.count('Polygon') == 1
    polygon = next(f for f in geojson['features'] if f['geometry']['type'] == 'Polygon')
    assert len(polygon['geometry']['coordinates']) == 2 and polygon['geometry']['coordinates'][0][0] == [6.0, 45.0]
    timed = [f for f in geojson['features'] if 'coordTimes' in f['properties']]
    assert len(timed) == 3 and 'is_annotation' not in geojson['features'][0]['properties']

    gpx = KMLEditor.export_to_gpx(parsed['features'])
    assert gpx.count('<trk>') == 5 and gpx.count('<rte>') == 2 and '<time>2025-06-03T08:01:00Z</time>' in gpx

    rows = list(csv.reader(io.StringIO(KMLEditor.export_to_csv(parsed['features']))))
    assert rows[0] == ['Type', 'Name', 'Description', 'Latitude', 'Longitude', 'Altitude', 'Time', 'Speed_KMH', 'Speed_KTS']
    assert rows[1][:3] == ['Point', 'Refuge', 'Ouvert en été, eau potable'] and rows[1][6] == '2025-06-01T08:00:00Z'
    assert {row[0] for row in rows[1:]} == {'Point', 'TracePoint', 'PolygonPoint', 'PolygonHolePoint'}
    assert any(row[6] == '2025-06-01T08:02:00Z' for row in rows if row[0] == 'TracePoint')
    assert {row[0] for row in csv.reader(io.StringIO(KMLEditor.export_to_csv(parsed['features'], False)))} == {'Type', 'Point'}


def test_simplification_tolerance_is_the_same_in_every_direction():
    # Même trace en dents de scie de 20 m d'amplitude, orientée nord-sud puis est-ouest, à 60° N
    metre_lat, metre_lon = 1 / 111320, 1 / (111320 * 0.5)
    north = [[60 + i * 50 * metre_lat, 10 + (20 * metre_lon if i % 2 else 0), 0] for i in range(41)]
    east = [[60 + (20 * metre_lat if i % 2 else 0), 10 + i * 50 * metre_lon, 0] for i in range(41)]
    for tolerance_m, kept in ((10, 41), (30, 2)):
        tolerance = tolerance_m / 111320
        assert len(KMLEditor.simplify_indices(north, tolerance)) == kept
        assert len(KMLEditor.simplify_indices(east, tolerance)) == kept


# ----------------------------------------------------------------- Bibliothèque

def test_gps_jump_creates_no_record():
    points = line(200, dlat=0.00025)
    clean = activity_stats.compute_summary(activity_stats.extract_track(GPXParser.parse_gpx_coordinates(make_gpx(points))))
    points[100] = (points[100][0] + 0.011, points[100][1], 100.0)  # point aberrant à 1,2 km
    jumped = activity_stats.compute_summary(activity_stats.extract_track(GPXParser.parse_gpx_coordinates(make_gpx(points))))
    assert jumped['distance_m'] == pytest.approx(clean['distance_m'], rel=0.01)
    assert jumped['max_speed_ms'] == pytest.approx(clean['max_speed_ms'], rel=0.05)
    assert jumped['best_efforts']['1000'] == pytest.approx(clean['best_efforts']['1000'], abs=11)


def test_sustained_high_speed_is_not_mistaken_for_a_jump():
    # Marche puis voiture : la portion rapide dure, elle reste dans la distance
    points = line(100, dlat=0.00001) + [(45.001 + i * 0.002, 6.0, 100.0) for i in range(1, 60)]
    summary = activity_stats.compute_summary(activity_stats.extract_track(GPXParser.parse_gpx_coordinates(make_gpx(points))))
    assert summary['distance_m'] == pytest.approx(13240, rel=0.02)
    assert summary['max_speed_ms'] == pytest.approx(22.2, rel=0.05)


def test_undated_activity_is_flagged_and_dated_from_file_when_possible(app, db, local_user_id):
    def ingest(raw, name):
        result = ingest_service.ingest_bytes(db, app.config, local_user_id, raw, name, 'upload')
        assert result.status == 'created', result.message
        return db.execute("SELECT start_time, start_time_known FROM activities WHERE id = ?", (result.activity_id,)).fetchone()

    row = ingest(make_gpx(line(20), with_time=False).encode(), 'sans-date.gpx')
    assert row['start_time_known'] == 0
    dated = make_gpx(line(21), with_time=False).replace('<trk>', '<metadata><time>2024-03-02T10:00:00Z</time></metadata><trk>')
    row = ingest(dated.encode(), 'date-metadonnees.gpx')
    assert (row['start_time'], row['start_time_known']) == ('2024-03-02T10:00:00Z', 1)
    row = ingest(make_gpx(line(22)).encode(), 'horodatee.gpx')
    assert (row['start_time'], row['start_time_known']) == ('2025-06-01T08:00:00Z', 1)


def test_recompute_stats_repairs_stored_activities(app, db, local_user_id, runner, client):
    points = line(200, dlat=0.00025)
    points[100] = (points[100][0] + 0.011, points[100][1], 100.0)
    result = ingest_service.ingest_bytes(db, app.config, local_user_id, make_gpx(points).encode(), 'saut.gpx', 'upload')
    # Valeurs telles qu'une version antérieure les aurait stockées
    db.execute("UPDATE activities SET distance_m = 7922.6, best_efforts = ? WHERE id = ?",
               (json.dumps({'1000': 10.0}), result.activity_id))
    db.commit()
    assert '1 activité(s) recalculée(s)' in runner.invoke(args=['recompute-stats']).output
    activity = client.get(f'/api/activities/{result.activity_id}').get_json()['activity']
    assert activity['distance_m'] == pytest.approx(5540, rel=0.02) and activity['best_efforts']['1000'] > 300
    assert activity['start_time_known'] is True
