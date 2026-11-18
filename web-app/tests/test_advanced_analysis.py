"""Analyse avancée : scénarios synthétiques dont le résultat attendu est connu."""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.services import advanced_analysis as aa
from app.services.gpx_parser import GPXParser
from tests.conftest import line, make_gpx

M = 1 / 111320.0  # degré de latitude par mètre
T0 = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)


def track(positions_m, elevations, times):
    """Polyline à partir de positions (est, nord) en mètres ; `times` en secondes ou None."""
    coords = [[45 + y * M, 6 + x * M / math.cos(math.radians(45)), ele]
              for (x, y), ele in zip(positions_m, elevations)]
    stamps = [None if t is None else (T0 + timedelta(seconds=float(t))).strftime('%Y-%m-%dT%H:%M:%SZ')
              for t in times]
    return [{'type': 'polyline', 'coordinates': coords, 'time_info': {'timestamps': stamps}}]


def analyze(features, points=()):
    analysis = aa.analyze(features, list(points))
    json.dumps(analysis, allow_nan=False)  # toujours sérialisable, jamais de NaN
    return analysis


@pytest.fixture
def rng():
    return np.random.default_rng(7)


# ------------------------------------------------------------------------ Arrêts

def test_stop_is_found_despite_gps_jitter(rng):
    # 5 min de marche, 3 min immobile (le GPS tremble de ±3 m), 5 min de marche
    positions, north = [], 0.0
    for phase, seconds in (('move', 300), ('still', 180), ('move', 300)):
        for _ in range(seconds):
            north += 1.5 if phase == 'move' else 0.0
            noise = 3.0 if phase == 'still' else 0.5
            positions.append((rng.normal(0, noise), north + rng.normal(0, noise)))
    analysis = analyze(track(positions, [0] * len(positions), range(len(positions))))

    assert len(analysis['stops']) == 1
    stop = analysis['stops'][0]
    assert stop['duration_s'] == pytest.approx(180, abs=25)
    assert stop['lat'] == pytest.approx(45 + 450 * M, abs=10 * M)
    assert stop['start_time'].startswith('2025-06-01T08:0')
    summary = analysis['summary']
    assert summary['stopped_s'] == stop['duration_s']
    assert summary['moving_s'] + summary['stopped_s'] == pytest.approx(summary['duration_s'], abs=1)


def test_recording_pause_counts_as_a_stop():
    # Pause automatique du GPS : 10 min sans aucun point
    times = [i if i < 100 else i + 600 for i in range(200)]
    analysis = analyze(track([(0, 3.0 * i) for i in range(200)], [0] * 200, times))
    assert [stop['duration_s'] for stop in analysis['stops']] == [601.0]
    assert analysis['summary']['avg_moving_speed_kmh'] == pytest.approx(10.8, abs=0.3)


def test_short_halt_is_ignored_and_threshold_is_configurable():
    times = [i if i < 100 else i + 40 for i in range(200)]
    features = track([(0, 3.0 * i) for i in range(200)], [0] * 200, times)
    assert analyze(features)['stops'] == []
    assert len(aa.analyze(features, [], stop_min_duration_s=30)['stops']) == 1


def test_no_time_and_no_speed_means_unavailable_not_invented():
    points = [{'type': 'marker', 'coordinates': [lat, lon], 'parsed_info': {'speed_kmh': None}}
              for lat, lon, _ in line(50)]
    analysis = analyze(track([(0, 10.0 * i) for i in range(50)], [0] * 50, [None] * 50), points)
    assert analysis['stops'] == [] and analysis['speed_zones'] == {}
    unavailable = analysis['summary']['unavailable']
    assert {'stops', 'speed_zones', 'acceleration_zones', 'segments', 'terrain'} <= set(unavailable)
    assert analysis['summary']['moving_s'] is None


def test_points_with_device_speed_are_used_when_nothing_is_timestamped():
    # KML de traceur : pas de trace horodatée, la vitesse est dans la description des points
    points = [{'type': 'marker', 'coordinates': [45 + i * 0.001, 6.0], 'altitude': 300,
               'parsed_info': {'speed_kmh': 0.5 if 20 <= i < 26 else 40 + i % 7}} for i in range(60)]
    analysis = analyze([], points)
    assert analysis['summary']['source'] == 'points' and analysis['summary']['speed_source'] == 'device'
    assert len(analysis['stops']) == 1
    stop = analysis['stops'][0]
    assert (stop['start_index'], stop['end_index'], stop['duration_s']) == (20, 25, None)
    assert analysis['speed_zones']['basis'] == 'points'


# ----------------------------------------------------------- Montées et terrain

def test_noisy_climb_and_descent_are_two_segments(rng):
    # 5 km à +8 % puis 5 km à -8 %, altitude bruitée de ±3 m
    n = 1000
    elevations = [1000 + 0.8 * (i if i < n // 2 else n - i) + rng.normal(0, 3) for i in range(n)]
    analysis = analyze(track([(0, 10.0 * i) for i in range(n)], elevations, [3 * i for i in range(n)]))

    assert [segment['type'] for segment in analysis['segments']] == ['ascent', 'descent']
    climb, descent = analysis['segments']
    assert climb['elevation_change'] == pytest.approx(400, abs=15)
    assert climb['avg_slope'] == pytest.approx(8, abs=0.5) and descent['avg_slope'] == pytest.approx(-8, abs=0.5)
    assert climb['avg_speed_kmh'] == pytest.approx(12, abs=0.5)
    assert len(climb['path']) <= aa.PATH_POINTS and len(climb['bounds']) == 2

    terrain = analysis['terrain']
    assert terrain['max_slope'] < 14 and terrain['min_slope'] > -14  # le bruit ne fabrique pas de mur
    assert terrain['terrain_analysis']['dominant_terrain'] == 'gentle'
    assert terrain['terrain_analysis']['terrain_distribution']['gentle']['percent'] > 90
    assert [(s['type'], s['direction']) for s in terrain['slope_segments']] == [('gentle', 'up'), ('gentle', 'down')]

    highest = next(poi for poi in analysis['points_of_interest'] if poi['type'] == 'highest')
    assert highest['distance_km'] == pytest.approx(5.0, abs=0.2)
    assert analysis['summary']['ascent_m'] == pytest.approx(400, abs=20)


def test_noisy_flat_track_stays_flat(rng):
    n = 1000
    elevations = [200 + rng.normal(0, 3) for _ in range(n)]
    analysis = analyze(track([(0, 10.0 * i) for i in range(n)], elevations, [None] * n))
    assert [segment['type'] for segment in analysis['segments']] == ['flat']
    assert analysis['terrain']['terrain_analysis']['terrain_distribution']['flat']['percent'] > 98
    assert analysis['summary']['ascent_m'] < 30


def test_elevation_noise_during_a_stop_adds_no_climb(rng):
    # Altitude qui dérive de ±8 m pendant 10 min d'arrêt, sur une trace plate
    positions = [(0, 3.0 * i) for i in range(200)] + [(0, 600.0)] * 600 + [(0, 600 + 3.0 * i) for i in range(1, 200)]
    elevations = [50.0] * 200 + [50 + rng.normal(0, 8) for _ in range(600)] + [50.0] * 199
    analysis = analyze(track(positions, elevations, range(len(positions))))
    assert len(analysis['stops']) == 1
    assert [segment['type'] for segment in analysis['segments']] == ['flat']
    assert analysis['summary']['ascent_m'] < 10


def test_missing_elevations_written_as_zero_are_interpolated():
    elevations = [1500.0 + i for i in range(100)]
    for i in (10, 11, 50):
        elevations[i] = 0.0
    analysis = analyze(track([(0, 10.0 * i) for i in range(100)], elevations, [None] * 100))
    assert analysis['terrain']['min_slope'] > 0  # pas de gouffre de 1500 m
    assert any('interpolée' in note for note in analysis['summary']['notes'])


def test_without_elevation_terrain_is_unavailable():
    analysis = analyze(track([(0, 10.0 * i) for i in range(100)], [0] * 100, range(100)))
    assert analysis['segments'] == [] and analysis['terrain'] == {}
    assert 'altitude' in analysis['summary']['unavailable']['terrain']


# --------------------------------------------------------------------- Vitesses

def test_speed_zones_split_moving_time_and_exclude_stops():
    # 10 min à 1,5 m/s, 5 min d'arrêt, 10 min à 6 m/s
    positions, north = [], 0.0
    for speed, seconds in ((1.5, 600), (0.0, 300), (6.0, 600)):
        for _ in range(seconds):
            north += speed
            positions.append((0, north))
    zones = analyze(track(positions, [0] * len(positions), range(len(positions))))['speed_zones']
    by_type = {zone['type']: zone for zone in zones['zones']}
    assert sum(zone['percent'] for zone in zones['zones']) == pytest.approx(100, abs=0.5)
    assert by_type['slow']['percent'] == pytest.approx(50, abs=3) and by_type['fast']['percent'] == pytest.approx(50, abs=3)
    assert by_type['fast']['avg_speed'] == pytest.approx(21.6, abs=0.5)
    assert zones['basis'] == 'time'


def test_single_gps_jump_does_not_set_top_speed():
    positions = [(0, 3.0 * i) for i in range(600)]
    positions[300] = (400.0, 900.0)  # saut isolé de 400 m
    analysis = analyze(track(positions, [0] * 600, range(600)))
    assert analysis['summary']['max_speed_kmh'] == pytest.approx(10.8, abs=1)
    assert analysis['stops'] == []
    assert any('saut' in note for note in analysis['summary']['notes'])


def test_sharp_turns_are_reported_with_their_angle():
    positions = ([(0, 5.0 * i) for i in range(100)] + [(5.0 * i, 500) for i in range(1, 100)]
                 + [(495 - 5.0 * i, 505) for i in range(1, 100)])
    turns = [poi for poi in analyze(track(positions, [0] * len(positions), range(len(positions))))['points_of_interest']
             if poi['type'] == 'turn']
    assert [turn['severity'] for turn in turns] == ['moderate', 'sharp']
    assert turns[0]['distance_km'] == pytest.approx(0.5, abs=0.03)
    assert turns[0]['angle_degrees'] == pytest.approx(90, abs=15)


# ------------------------------------------------------------------- Robustesse

def test_garbage_input_never_crashes():
    features = [
        None, 'x', 42,
        {'type': 'polyline', 'coordinates': [[45, 6, 'a'], [float('nan'), 6], None, [45.001, 6.0, None], ['45', 6],
                                             [95, 6, 0], [45.002, 6.0, 1e9], [45.003, 6, True]]},
        {'type': 'polyline', 'coordinates': 'abc'},
        {'type': 'polyline', 'coordinates': [[45, 6]], 'time_info': 'x'},
        {'type': 'multitrack', 'tracks': [{'type': 'track', 'coordinates': [[45.1, 6.1, 5], [45.2, 6.1, 9]],
                                           'timestamps': ['pas une date', None]}]},
    ]
    points = [None, {'type': 'marker'}, {'type': 'marker', 'coordinates': [1, 'a']},
              {'type': 'marker', 'coordinates': [45, 6], 'parsed_info': 'x', 'time_info': 3}]
    analysis = analyze(features, points)
    assert analysis['summary']['points'] == 7 and analysis['summary']['has_time'] is False


@pytest.mark.parametrize('features, points', [
    ([], []),
    ([{'type': 'polyline', 'coordinates': [[45, 6, 100]]}], []),
    ([{'type': 'polyline', 'coordinates': [[45, 6, 100]] * 50}], []),  # trace immobile
    ([], [{'type': 'marker', 'coordinates': [45, 6]}]),
])
def test_degenerate_tracks_give_an_explained_empty_result(features, points):
    analysis = analyze(features, points)
    assert analysis['stops'] == [] and analysis['segments'] == [] and analysis['terrain'] == {}
    assert analysis['summary']['unavailable']['stops'] == aa.NO_TRACK


def test_separate_tracks_are_not_joined_by_a_phantom_leg():
    first = track([(0, 10.0 * i) for i in range(50)], [0] * 50, range(50))[0]
    far = track([(50000, 10.0 * i) for i in range(50)], [0] * 50, range(100, 150))[0]
    analysis = analyze([first, far])
    assert analysis['summary']['distance_km'] == pytest.approx(0.98, abs=0.01)
    assert analysis['summary']['max_speed_kmh'] < 40


def test_unordered_timestamps_disable_time_analyses():
    times = list(range(50)) + list(range(20, 70))
    analysis = analyze(track([(0, 10.0 * i) for i in range(100)], [0] * 100, times))
    assert analysis['summary']['has_time'] is False
    assert any('désordonnés' in note for note in analysis['summary']['notes'])


def test_large_track_stays_fast_and_compact(rng):
    n = 50000
    positions = np.cumsum(rng.normal(2, 1, (n, 2)), axis=0).tolist()
    elevations = (500 + np.cumsum(rng.normal(0, 0.5, n))).tolist()
    analysis = analyze(track(positions, elevations, range(n)))
    assert len(json.dumps(analysis)) < 400_000
    for key in ('stops', 'segments', 'acceleration_zones', 'points_of_interest'):
        assert len(analysis[key]) <= aa.MAX_ITEMS
    assert len(analysis['terrain']['slope_segments']) <= aa.MAX_ITEMS


def test_too_many_points_is_refused(monkeypatch):
    monkeypatch.setattr(aa, 'MAX_TRACK_POINTS', 10)
    with pytest.raises(ValueError):
        aa.analyze(track([(0, 10.0 * i) for i in range(20)], [0] * 20, range(20)), [])


# ------------------------------------------------------------------ De bout en bout

def test_parsed_gpx_with_and_without_time():
    points = line(200, ele=lambda i: 100.0 + 2 * i)
    timed = GPXParser.parse_gpx_coordinates(make_gpx(points))
    analysis = analyze(timed['features'], timed['points'])
    assert analysis['summary']['speed_source'] == 'time'
    assert [segment['type'] for segment in analysis['segments']] == ['ascent']
    assert analysis['summary']['avg_moving_speed_kmh'] == pytest.approx(12, abs=0.3)

    untimed = GPXParser.parse_gpx_coordinates(make_gpx(points, with_time=False))
    analysis = analyze(untimed['features'], untimed['points'])
    assert analysis['summary']['speed_source'] is None
    assert [segment['type'] for segment in analysis['segments']] == ['ascent']


def test_advanced_endpoint(client, monkeypatch):
    parsed = GPXParser.parse_gpx_coordinates(make_gpx(line(60, ele=lambda i: 100.0 + 3 * i)))
    body = {'features': parsed['features'], 'points': parsed['points']}
    response = client.post('/api/analysis/advanced', json=body)
    assert response.status_code == 200
    analysis = response.get_json()['analysis']
    assert set(analysis) == {'stops', 'segments', 'speed_zones', 'acceleration_zones', 'terrain',
                             'points_of_interest', 'summary'}
    assert 'NaN' not in response.get_data(as_text=True)

    assert client.post('/api/analysis/advanced', json={'features': [None, 3], 'points': ['x']}).status_code == 200
    monkeypatch.setattr(aa, 'MAX_TRACK_POINTS', 10)
    refused = client.post('/api/analysis/advanced', json=body)
    assert refused.status_code == 400 and 'volumineuse' in refused.get_json()['error']


def test_stops_endpoint_validates_thresholds(client):
    points = GPXParser.parse_gpx_coordinates(make_gpx(line(30)))['points']
    assert client.post('/api/analysis/stops', json={'points': points}).get_json()['stops_count'] == 0
    for bad in ({'speed_threshold': -1}, {'time_threshold': 'nan'}, {'time_threshold': None}):
        assert client.post('/api/analysis/stops', json={'points': points, **bad}).status_code == 400
