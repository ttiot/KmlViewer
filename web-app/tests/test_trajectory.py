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

