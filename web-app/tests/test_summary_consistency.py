"""
Une seule source de calcul : la bibliothèque (résumé d'activité) et le visualiseur (analyse)
mesurent la même chose, trace par trace, et les synthèses portent sur toute la trace.
Cas chiffrés de la revue de code.
"""

import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.services import activity_stats, advanced_analysis as aa, track_series

M = 1 / 111320.0
T0 = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)


def track(positions_m, elevations, times, lat0=45.0, lon0=6.0):
    coords = [[lat0 + y * M, lon0 + x * M / math.cos(math.radians(lat0)), ele]
              for (x, y), ele in zip(positions_m, elevations)]
    stamps = [None if t is None else (T0 + timedelta(seconds=float(t))).strftime('%Y-%m-%dT%H:%M:%SZ')
              for t in times]
    return {'type': 'polyline', 'coordinates': coords, 'time_info': {'timestamps': stamps}}


def analyze(features, **kwargs):
    analysis = aa.analyze(features, [], **kwargs)
    json.dumps(analysis, allow_nan=False)
    return analysis


def library(features):
    return activity_stats.compute_summary(activity_stats.extract_track({'features': features}))


@pytest.fixture
def rng():
    return np.random.default_rng(7)


# ------------------------------------------------------ Bibliothèque = visualiseur

def test_elevation_noise_during_a_stop_adds_no_climb_in_the_library_either(rng):
    # Avant : 255 m de D+ en bibliothèque pour 0 m dans l'analyse
    positions = [(0, 3.0 * i) for i in range(200)] + [(0, 600.0)] * 600 + [(0, 600 + 3.0 * i) for i in range(1, 200)]
    elevations = [50.0] * 200 + [50 + rng.normal(0, 8) for _ in range(600)] + [50.0] * 199
    features = [track(positions, elevations, range(len(positions)))]
    summary, stored = analyze(features)['summary'], library(features)
    assert stored['ascent_gps_m'] < 10 and stored['ascent_gps_m'] == pytest.approx(summary['ascent_m'], abs=0.5)
    assert stored['moving_s'] == pytest.approx(summary['moving_s'], abs=0.5)
    assert stored['duration_s'] == pytest.approx(summary['duration_s'], abs=0.5)
    assert stored['max_speed_ms'] * 3.6 == pytest.approx(summary['max_speed_kmh'], abs=0.1)
    assert stored['distance_m'] / 1000 == pytest.approx(summary['distance_km'], abs=0.01)


def test_flat_walk_with_noisy_altitude(rng):
    # 1 h à plat, bruit ±3 m : 98,8 m de D+ en bibliothèque avant, 5 m dans l'analyse
    count = 3600
    features = [track([(0, 1.4 * i) for i in range(count)], [200 + rng.normal(0, 3) for _ in range(count)], range(count))]
    assert library(features)['ascent_gps_m'] < 15
    assert library(features)['ascent_gps_m'] == pytest.approx(analyze(features)['summary']['ascent_m'], abs=0.5)


def test_missing_elevations_written_as_zero_are_not_a_cliff_in_the_library():
    # Avant : +1200 / −1200 m pour quatre altitudes absentes sur une trace à 1500 m
    elevations = [1500.0] * 300
    for i in (10, 11, 50, 120):
        elevations[i] = 0.0
    stored = library([track([(0, 10.0 * i) for i in range(300)], elevations, range(300))])
    assert (stored['ascent_gps_m'], stored['descent_gps_m']) == (0.0, 0.0)


def test_separate_tracks_are_not_joined_in_the_library():
    # Sans horodatage, à 50 km l'une de l'autre : 55,99 km avant, 5,97 km attendus
    first = track([(0, 10.0 * i) for i in range(300)], [2000.0] * 300, [None] * 300)
    far = track([(50000, 10.0 * i) for i in range(300)], [2000.0] * 300, [None] * 300)
    assert library([first, far])['distance_m'] == pytest.approx(5973, abs=10)
    assert analyze([first, far])['summary']['distance_km'] == pytest.approx(5.97, abs=0.01)


def test_multi_day_tracks_measure_each_day_only():
    # Jour 1 à 2000 m, jour 2 à 500 m et à 8 km : avant, 1500 m de descente et une pente de −1251 %
    day1 = track([(0, 10.0 * i) for i in range(300)], [2000.0] * 300, range(300))
    day2 = track([(8000, 10.0 * i) for i in range(300)], [500.0] * 300, range(86400, 86700))
    analysis = analyze([day1, day2])
    summary = analysis['summary']
    assert (summary['ascent_m'], summary['descent_m']) == (0.0, 0.0)
    assert summary['duration_s'] == 598 and summary['distance_km'] == pytest.approx(5.97, abs=0.01)
    assert [segment['type'] for segment in analysis['segments']] == ['flat', 'flat']
    terrain = analysis['terrain']
    assert terrain['min_slope'] == 0.0 and terrain['max_slope'] == 0.0
    assert terrain['terrain_analysis']['terrain_distribution']['steep']['percent'] == 0.0
    highest = next(poi for poi in analysis['points_of_interest'] if poi['type'] == 'highest')
    lowest = next(poi for poi in analysis['points_of_interest'] if poi['type'] == 'lowest')
    assert highest['index'] < 300 <= lowest['index']  # chacun dans sa trace
    stored = library([day1, day2])
    # Avant : 14,5 km, 86 699 s, 1500 m de descente
    assert stored['distance_m'] == pytest.approx(5973, abs=10)
    assert stored['duration_s'] == 598 and stored['descent_gps_m'] == 0.0


def test_tracks_out_of_chronological_order_keep_their_time():
    # Avant : « Horodatages désordonnés », toutes les analyses temporelles désactivées
    late = track([(0, 10.0 * i) for i in range(100)], [0] * 100, range(1000, 1100))
    early = track([(5000, 10.0 * i) for i in range(100)], [0] * 100, range(0, 100))
    summary = analyze([late, early])['summary']
    assert summary['has_time'] and summary['duration_s'] == 198
    assert summary['avg_moving_speed_kmh'] == pytest.approx(36, abs=0.5)
    assert any('ordre chronologique' in note for note in summary['notes'])
    stored = library([late, early])
    assert stored['has_time'] and stored['start_epoch'] == T0.timestamp()
    # À l'intérieur d'une trace, un temps qui recule reste inexploitable
    broken = track([(0, 10.0 * i) for i in range(100)], [0] * 100, list(range(50)) + list(range(20, 70)))
    assert analyze([broken])['summary']['has_time'] is False


def test_terrain_elevation_replaces_the_file_elevation(rng):
    # Terrain plat à 800 m, GPS bruité : avec l'altitude MNT l'analyse et la bibliothèque disent 0 m
    count = 400
    features = [track([(0, 10.0 * i) for i in range(count)], [800 + rng.normal(0, 6) for _ in range(count)], range(count))]
    gps = analyze(features)['summary']
    dem = analyze(features, elevation=[800.0] * count)['summary']
    assert gps['elevation_source'] == 'gps' and gps['ascent_m'] > 0
    assert dem['elevation_source'] == 'dem' and dem['ascent_m'] == 0
    assert aa.basic(features, [], elevation=[800.0] * count)['elevation']['source'] == 'dem'
    # Altitude non alignée : ignorée, et dite
    ignored = analyze(features, elevation=[800.0] * 3)['summary']
    assert ignored['elevation_source'] == 'gps' and any('non alignée' in note for note in ignored['notes'])

    parsed = activity_stats.extract_track({'features': features})
    climb = [800 + 0.5 * i for i in range(count)]
    ascent, descent = activity_stats.elevation_gain(climb, 'dem', parsed.lat, parsed.lon, parsed.breaks)
    assert ascent == pytest.approx(199.5, abs=4) and descent == 0
    assert activity_stats.elevation_gain(climb, 'dem')[0] == pytest.approx(199.5, abs=4)  # ancien appel, sans position


# --------------------------------------------- Plafonds à la sérialisation seulement

def test_more_than_max_items_stops_are_all_counted_in_the_summary():
    # 260 arrêts de 90 s : avant, la synthèse n'en retranchait que 200 (moyenne 3,8 km/h au lieu de 10,8)
    positions, times, north, clock = [], [], 0.0, 0
    for _ in range(260):
        for moving in (True,) * 20 + (False,) * 90:
            north += 3.0 if moving else 0.0
            clock += 1
            positions.append((0, north))
            times.append(clock)
    features = [track(positions, [0] * len(positions), times)]
    analysis = analyze(features)
    summary = analysis['summary']
    assert len(analysis['stops']) == aa.MAX_ITEMS and summary['stops_count'] == 260
    listed = sum(stop['duration_s'] for stop in analysis['stops'])
    assert summary['stopped_s'] == pytest.approx(listed * 260 / 200, rel=0.01)
    assert summary['moving_s'] + summary['stopped_s'] == pytest.approx(summary['duration_s'], abs=1)
    assert summary['avg_moving_speed_kmh'] > 5  # 3,8 km/h avant
    assert any('260 arrêts' in note for note in summary['notes'])
    assert library(features)['moving_s'] == pytest.approx(summary['moving_s'], abs=1)


# ------------------------------------------------------------------------ Arrêts

def test_slow_but_real_progress_is_not_a_stop():
    # 1,5 km/h à l'horizontale pendant 30 min (pente raide) : avant, un arrêt de 1799 s et 0 s en mouvement
    count = 1800
    features = [track([(0, 1.5 / 3.6 * i) for i in range(count)], [1000 + 0.15 * i for i in range(count)], range(count))]
    summary = analyze(features)['summary']
    assert analyze(features)['stops'] == [] and summary['moving_s'] == 1799
    assert summary['avg_moving_speed_kmh'] == pytest.approx(1.5, abs=0.1)


def test_rest_inside_a_slow_climb_is_still_found():
    positions, north = [], 0.0
    for speed, seconds in ((0.42, 600), (0.0, 300), (0.42, 600)):
        for _ in range(seconds):
            north += speed
            positions.append((0, north))
    stops = analyze([track(positions, [0] * len(positions), range(len(positions)))])['stops']
    assert len(stops) == 1 and stops[0]['duration_s'] == pytest.approx(300, abs=40)


@pytest.mark.parametrize('period_s, speed', [(30, 10), (30, 31.4), (15, 10)])
def test_orbit_is_not_a_stop(period_s, speed):
    # Orbite sans vent dont la période vaut la fenêtre d'arrêt : avant, un « arrêt » de 214 s à 113 km/h
    radius = speed * period_s / (2 * math.pi)
    positions = [(0, speed * i) for i in range(120)]
    y0 = positions[-1][1]
    positions += [(radius * (1 - math.cos(2 * math.pi * i / period_s)), y0 + radius * math.sin(2 * math.pi * i / period_s))
                  for i in range(1, 241)]
    x1, y1 = positions[-1]
    positions += [(x1, y1 + speed * i) for i in range(1, 120)]
    analysis = analyze([track(positions, [0] * len(positions), range(len(positions)))])
    assert analysis['stops'] == [] and analysis['summary']['moving_s'] == analysis['summary']['duration_s']


def test_recording_pause_with_a_small_move_is_still_a_stop():
    # Pause de 10 min pendant laquelle on s'est déplacé de 150 m : rien n'a été mesuré, c'est un arrêt
    positions = [(0, 3.0 * i) for i in range(100)] + [(0, 450 + 3.0 * i) for i in range(100)]
    times = [i if i < 100 else i + 600 for i in range(200)]
    stops = analyze([track(positions, [0] * 200, times)])['stops']
    assert [stop['duration_s'] for stop in stops] == [601.0]


# -------------------------------------------------- Antiméridien, altitude, seuils

def test_antimeridian_crossing_creates_no_turn_and_keeps_narrow_bounds():
    # Avant : deux « Épingle (180°) » fantômes au passage de 180°
    coords = [[-17.0, 179.9 + 0.001 * i, 100.0 + 0.2 * i] for i in range(200)]
    coords = [[lat, lon if lon <= 180 else lon - 360, ele] for lat, lon, ele in coords]
    analysis = analyze([{'type': 'polyline', 'coordinates': coords}])
    assert analysis['summary']['distance_km'] == pytest.approx(21.16, abs=0.05)
    assert [poi for poi in analysis['points_of_interest'] if poi['type'] == 'turn'] == []
    (south, west), (north, east) = analysis['segments'][0]['bounds']
    assert east - west == pytest.approx(0.199, abs=0.01)  # et non 360° de large


def test_altitude_above_9000_m_is_kept():
    # Avant : tout ce qui dépassait 9000 m était jugé aberrant (maximum affiché 8970 m)
    count = 600
    elevations = [min(11000, 500 + 60 * i, 500 + 60 * (count - i)) for i in range(count)]
    features = [track([(0, 200.0 * i) for i in range(count)], elevations, [10 * i for i in range(count)])]
    basic = aa.basic(features, [])
    assert basic['elevation']['max_elevation'] == 11000 and basic['notes'] == []
    absurd = [track([(0, 200.0 * i) for i in range(count)], [1e9 if i == 5 else 500.0 for i in range(count)], [None] * count)]
    assert aa.basic(absurd, [])['elevation']['max_elevation'] == 500


def test_four_percent_slope_is_a_climb_and_a_false_flat_going_up():
    # Avant : segment « ascent » mais terrain « flat » de direction « flat »
    count = 500
    features = [track([(0, 10.0 * i) for i in range(count)], [100 + 0.4 * i for i in range(count)], range(count))]
    analysis = analyze(features)
    assert [(s['type'], s['avg_slope']) for s in analysis['segments']] == [('ascent', 4.0)]
    assert [(s['type'], s['direction']) for s in analysis['terrain']['slope_segments']] == [('flat', 'up')]
    level = [track([(0, 10.0 * i) for i in range(count)], [100.0 + (i % 2) for i in range(count)], range(count))]
    assert [(s['type'], s['direction']) for s in analyze(level)['terrain']['slope_segments']] == [('flat', 'flat')]


# ------------------------------------------------------------------- Cas dégénérés

def test_series_from_arrays_rejects_inconsistent_input():
    with pytest.raises(ValueError):
        track_series.series_from_arrays([45.0], [6.0], [0.0], [None])
    with pytest.raises(ValueError):
        track_series.series_from_arrays([45.0, 45.1], [6.0], [0.0, 0.0], [None, None])


def test_analysis_sections_can_be_computed_alone():
    features = [track([(0, 10.0 * i) for i in range(300)], [100 + i for i in range(300)], range(300))]
    assert set(aa.analyze(features, [], only=('stops',))) == {'stops'}
    assert aa.analyze(features, [], only=('segments',))['segments'] == aa.analyze(features, [])['segments']
    assert aa.analyze([], [], only=('terrain', 'summary')) == {
        'terrain': {}, 'summary': aa.empty_analysis()['summary']}
    with pytest.raises(ValueError, match='inconnue'):
        aa.analyze(features, [], only=('nimporte',))
