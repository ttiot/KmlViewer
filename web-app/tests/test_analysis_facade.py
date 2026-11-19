"""
Façade TrajectoryAnalyzer : distance validée (jamais de NaN ni d'exception), une seule
formule de distance, et chaque méthode ne calcule que ce qu'elle renvoie.
"""

import json
import math

import numpy as np
import pytest

from app.services import advanced_analysis, geo, track_series, trajectory_analyzer
from app.services.trajectory_analyzer import TrajectoryAnalyzer

LEG_M = 11119.49  # 0,1° de latitude


@pytest.mark.parametrize('coordinates, expected', [
    ([[45, 6], [float('nan'), 6], [45.1, 6]], LEG_M),      # avant : NaN, sérialisé tel quel (JSON invalide)
    ([[45, 6], [45.1, 6, 100]], LEG_M),                    # avant : ValueError (mélange 2D/3D) -> 500
    ([[45, 6], 'x', None, [45.1, 6]], LEG_M),              # avant : ValueError -> 500
    ([[45, 6], ['a', 'b'], [True, 6], [45.1, 6]], LEG_M),  # non numérique, booléen
    ([[4500, 6], [45.1, 6000]], 0.0),                      # avant : 7281 km entre deux points hors bornes
    ([[45, 6], [float('inf'), 6], [1e308, 6], [45.1, 6]], LEG_M),
    ([[45], [46]], 0.0),                                   # avant : IndexError
    ([45, 46], 0.0),
    ([], 0.0),
    ([[45, 6]], 0.0),
    ('abc', 0.0),
    (None, 0.0),
])
def test_total_distance_ignores_unusable_points_and_never_fails(coordinates, expected):
    total = TrajectoryAnalyzer.calculate_total_distance(coordinates)
    assert math.isfinite(total) and total == pytest.approx(expected, abs=0.5)
    json.dumps(total, allow_nan=False)


def test_total_distance_uses_the_shared_formula():
    coordinates = [[45 + i * 0.001, 6 + i * 0.002, 100] for i in range(500)]
    lat, lon = np.array([c[0] for c in coordinates]), np.array([c[1] for c in coordinates])
    assert TrajectoryAnalyzer.calculate_total_distance(coordinates) == pytest.approx(float(geo.cumulative_distance(lat, lon)[-1]))
    analysis = TrajectoryAnalyzer.analyze_trajectory([{'type': 'polyline', 'coordinates': coordinates}])
    assert analysis['distance']['total_distance_m'] == pytest.approx(TrajectoryAnalyzer.calculate_total_distance(coordinates), abs=0.1)


def test_total_distance_is_not_capped_like_an_analysis(monkeypatch):
    monkeypatch.setattr(track_series, 'MAX_TRACK_POINTS', 10)
    assert TrajectoryAnalyzer.calculate_total_distance([[45 + i * 0.001, 6] for i in range(50)]) > 5000


def test_facade_has_no_second_distance_formula():
    assert not hasattr(trajectory_analyzer, 'geodesic')
    assert not hasattr(TrajectoryAnalyzer, 'calculate_distance_between_points')
    with pytest.raises(TypeError):
        TrajectoryAnalyzer.calculate_total_distance([[45, 6], [45.1, 6]], include_altitude=True)


def test_each_facade_method_only_computes_its_own_section(monkeypatch):
    coordinates = [[45 + i * 0.0001, 6.0, 500 + i] for i in range(300)]
    points = [{'type': 'marker', 'coordinates': [45 + i * 0.001, 6.0], 'altitude': 300,
               'parsed_info': {'speed_kmh': 0.5 if 20 <= i < 26 else 40 + i % 7}} for i in range(60)]
    expected = advanced_analysis.analyze([{'type': 'polyline', 'coordinates': coordinates}], [])
    by_points = advanced_analysis.analyze([], points)

    computed = []
    for name in ('segment_climbs', 'analyze_terrain', 'analyze_speed_zones', 'acceleration_zones',
                 'points_of_interest', '_summary'):
        original = getattr(advanced_analysis, name)
        monkeypatch.setattr(advanced_analysis, name,
                            lambda *args, _name=name, _original=original: (computed.append(_name), _original(*args))[1])

    def only(call, *arguments):
        computed.clear()
        return call(*arguments), list(computed)

    assert only(TrajectoryAnalyzer.segment_trajectory, coordinates) == (expected['segments'], ['segment_climbs'])
    assert only(TrajectoryAnalyzer.analyze_terrain, coordinates) == (expected['terrain'], ['analyze_terrain'])
    assert only(TrajectoryAnalyzer.detect_points_of_interest, coordinates) == (
        expected['points_of_interest'], ['points_of_interest'])
    assert only(TrajectoryAnalyzer.detect_stops, points) == (by_points['stops'], [])
    assert only(TrajectoryAnalyzer.analyze_speed_zones, points) == (by_points['speed_zones'], ['analyze_speed_zones'])
    assert only(TrajectoryAnalyzer.calculate_acceleration_zones, points) == ([], ['acceleration_zones'])
    assert len(by_points['stops']) == 1
