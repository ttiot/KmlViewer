import pytest
from app.services.trajectory_analyzer import TrajectoryAnalyzer


def test_calculate_elevation_profile_basic():
    coords = [
        [0, 0, 0],
        [0, 0, 10],
        [0, 0, 10],
        [0, 0, 20],
        [0, 0, 15],
        [0, 0, 15],
        [0, 0, 30],
    ]

    result = TrajectoryAnalyzer.calculate_elevation_profile(coords)

    assert result["total_ascent"] == pytest.approx(35.0, rel=1e-2)
    assert result["total_descent"] == pytest.approx(5.0, rel=1e-2)
    assert result["min_elevation"] == 0.0
    assert result["max_elevation"] == 30.0


def test_speed_statistics_with_outlier():
    points = []
    speeds = [10, 12, 11, 120, 9]
    for i, spd in enumerate(speeds):
        points.append({
            'parsed_info': {'speed_kmh': spd, 'speed_kts': spd / 1.852},
            'coordinates': [0.0, 0.0],
        })

    stats = TrajectoryAnalyzer.calculate_speed_statistics(points)

    assert stats['max_speed_kmh'] < 50
    assert stats['avg_speed_kmh'] < 20

