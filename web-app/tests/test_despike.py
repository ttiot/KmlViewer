"""
Sauts GPS et horodatages dupliqués : un point aberrant est écarté, un déplacement réel
(même bref, même entouré d'arrêts) ne l'est jamais. Cas chiffrés de la revue de code.
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.services import activity_stats, advanced_analysis as aa, geo

M = 1 / 111320.0  # degré de latitude par mètre
T0 = datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc)


def track(positions_m, times, elevations=None):
    """Polyline à partir de positions (est, nord) en mètres et de temps en secondes."""
    elevations = elevations or [0] * len(positions_m)
    coords = [[45 + y * M, 6 + x * M / math.cos(math.radians(45)), ele] for (x, y), ele in zip(positions_m, elevations)]
    stamps = [(T0 + timedelta(seconds=float(t))).strftime('%Y-%m-%dT%H:%M:%SZ') for t in times]
    return [{'type': 'polyline', 'coordinates': coords, 'time_info': {'timestamps': stamps}}]


def phases(*legs, step_s=10):
    """Trace nord-sud : suite de (vitesse m/s, nombre de pas)."""
    positions, times, north, clock = [], [], 0.0, 0
    for speed, count in legs:
        for _ in range(count):
            north += speed * step_s
            clock += step_s
            positions.append((0, north))
            times.append(clock)
    return track(positions, times), north


def library(features):
    return activity_stats.compute_summary(activity_stats.extract_track({'features': features}))


@pytest.mark.parametrize('moving_steps', [3, 5, 6])
def test_short_move_between_two_long_stops_is_kept(moving_steps):
    # Avant : 3 ou 5 pas à 8 m/s entre deux arrêts donnaient 0 m et « Aucune trace exploitable »
    features, truth = phases((0, 30), (8, moving_steps), (0, 30))
    summary = aa.analyze(features, [])['summary']
    assert summary['distance_km'] == pytest.approx(truth / 1000, abs=0.01)
    assert not any('saut' in note for note in summary['notes'])
    assert library(features)['distance_m'] == pytest.approx(truth, rel=0.01)


def test_short_real_burst_is_not_flattened():
    # Marche à 1,5 m/s avec 5 pas à 15 m/s : 1950 m réels (avant : 1259 m, pointe 5,4 km/h)
    features, truth = phases((1.5, 40), (15, 5), (1.5, 40))
    summary = aa.analyze(features, [])['summary']
    assert summary['distance_km'] == pytest.approx(1.95, abs=0.03)
    assert summary['max_speed_kmh'] == pytest.approx(54, abs=1)
    assert library(features)['distance_m'] == pytest.approx(truth, rel=0.01)


def test_out_and_back_outlier_is_still_removed():
    positions = [(0, 3.0 * i) for i in range(600)]
    positions[300] = (400.0, 900.0)   # un point à 400 m
    positions[450] = (0.0, 2000.0)    # un point 650 m en avant sur l'axe
    summary = aa.analyze(track(positions, range(600)), [])['summary']
    assert summary['distance_km'] == pytest.approx(1.797, abs=0.01)
    assert summary['max_speed_kmh'] == pytest.approx(10.8, abs=1)
    assert any(note.startswith('2 saut(s)') for note in summary['notes'])


def test_two_consecutive_outliers_are_removed_but_a_real_detour_is_not():
    positions = [(0, 3.0 * i) for i in range(600)]
    positions[300], positions[301] = (400.0, 900.0), (410.0, 903.0)
    assert aa.analyze(track(positions, range(600)), [])['summary']['distance_km'] == pytest.approx(1.797, abs=0.01)
    # Dix points à l'écart : ce n'est plus un saut, la distance parcourue est conservée
    detour = [(0, 3.0 * i) for i in range(600)]
    for i in range(300, 310):
        detour[i] = (400.0, 3.0 * i)
    assert aa.analyze(track(detour, range(600)), [])['summary']['distance_km'] > 2.5


def test_outlier_at_the_very_first_point_is_brought_back_to_local_pace():
    positions = [(0, 3.0 * i) for i in range(200)]
    positions[0] = (0.0, -800.0)  # premier point à froid, 800 m en arrière
    lat = np.array([45 + y * M for _, y in positions])
    lon = np.full(200, 6.0)
    steps, jumps = geo.despike_steps(lat, lon, geo.step_distances(lat, lon), np.ones(199))
    assert jumps == 1 and steps.sum() == pytest.approx(597, abs=6)


def test_despike_handles_empty_and_tiny_inputs():
    empty = np.zeros(0)
    assert geo.despike_steps(empty, empty, empty, empty)[1] == 0
    lat, lon = np.array([45.0, 45.01]), np.array([6.0, 6.0])
    steps, jumps = geo.despike_steps(lat, lon, geo.step_distances(lat, lon), np.array([1.0]))
    assert jumps == 0 and steps[0] == pytest.approx(1112, abs=2)


# ------------------------------------------------------- Horodatages dupliqués

def test_duplicate_times_are_spread_without_touching_pauses():
    spread = geo.spread_duplicate_times(np.array([0.0, 0.0, 1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 603.0, 603.0]))
    # La pause de 600 s n'est pas étalée : l'intervalle typique (1 s) borne la répartition
    assert spread.tolist() == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 603.0, 603.5]
    clean = np.array([0.0, 1.0, 2.0])
    assert geo.spread_duplicate_times(clean) is clean
    same = np.zeros(5)
    assert geo.spread_duplicate_times(same) is same  # aucun intervalle : rien à répartir


@pytest.mark.parametrize('hz', [1, 2, 5, 10])
def test_logger_above_one_hertz_stamped_to_the_second(hz):
    # 40 m/s pendant 10 min, horodatage tronqué à la seconde.
    # Avant : 11,99 km à 2 Hz, 19,18 km à 5 Hz, 21,58 km à 10 Hz ; pointe et temps en mouvement nuls en bibliothèque.
    count = 600 * hz
    positions = [(0, 40.0 * i / hz) for i in range(count)]
    features = track(positions, [i // hz for i in range(count)])
    truth = positions[-1][1]
    summary = aa.analyze(features, [])['summary']
    assert summary['distance_km'] == pytest.approx(truth / 1000, rel=0.005)
    assert summary['avg_moving_speed_kmh'] == pytest.approx(144, abs=1)
    assert summary['max_speed_kmh'] == pytest.approx(144, abs=1)
    stored = library(features)
    assert stored['distance_m'] == pytest.approx(truth, rel=0.005)
    assert stored['max_speed_ms'] == pytest.approx(40, abs=0.3)
    assert stored['moving_s'] == pytest.approx(600, abs=1.5)
    assert any('réparti' in note for note in summary['notes']) is (hz > 1)


def test_bike_at_five_hertz_has_its_real_top_speed():
    # 10 m/s à 5 Hz : la bibliothèque annonçait 1,4 km/h de pointe, l'analyse 39,4 km/h
    count = 3000
    features = track([(0, 10.0 * i / 5) for i in range(count)], [i // 5 for i in range(count)])
    assert aa.analyze(features, [])['summary']['max_speed_kmh'] == pytest.approx(36, abs=0.5)
    assert library(features)['max_speed_ms'] == pytest.approx(10, abs=0.15)
