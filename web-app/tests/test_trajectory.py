import pytest
from app.services.trajectory_analyzer import TrajectoryAnalyzer
from app.services.gpx_parser import GPXParser


def test_calculate_elevation_profile_basic():
    # 100 m de montée régulière, 20 m de descente, sur 1,2 km
    coords = [[45 + i * 0.0001, 6.0, 500 + (i if i < 100 else 200 - i)] for i in range(121)]

    result = TrajectoryAnalyzer.calculate_elevation_profile(coords)

    assert result["total_ascent"] == pytest.approx(100.0, abs=6)
    assert result["total_descent"] == pytest.approx(20.0, abs=6)
    assert result["min_elevation"] == 500.0
    assert result["max_elevation"] == 600.0
    assert result["elevation_profile"][-1]["distance"] == pytest.approx(1334, abs=5)


def test_speed_statistics_with_outlier():
    points = []
    speeds = [10, 12, 11, 120, 9]
    for spd in speeds:
        points.append({
            'parsed_info': {'speed_kmh': spd, 'speed_kts': spd / 1.852},
            'coordinates': [45.0 + 0.001 * len(points), 6.0],
        })

    stats = TrajectoryAnalyzer.calculate_speed_statistics(points)

    assert stats['max_speed_kmh'] < 50
    assert stats['avg_speed_kmh'] < 20


def test_advanced_analysis_with_gpx():
    gpx = """<?xml version='1.0' encoding='UTF-8'?>
<gpx xmlns='http://www.topografix.com/GPX/1/1' version='1.1' creator='test'>
  <trk>
    <name>Test</name>
    <trkseg>
      <trkpt lat='0.0' lon='0.0'>
        <ele>0</ele>
        <time>2020-01-01T00:00:00Z</time>
      </trkpt>
      <trkpt lat='0.0' lon='0.1'>
        <ele>0</ele>
        <time>2020-01-01T00:05:00Z</time>
      </trkpt>
      <trkpt lat='0.0' lon='0.2'>
        <ele>0</ele>
        <time>2020-01-01T00:10:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""

    res = GPXParser.parse_gpx_coordinates(gpx)
    features = res['features']
    points = res['points']

    analysis = TrajectoryAnalyzer.advanced_trajectory_analysis(features, points)

    assert analysis['speed_zones']['total_points'] > 0
    assert analysis['summary']['avg_moving_speed_kmh'] == pytest.approx(133.6, rel=0.01)
    # Altitude nulle partout : pas de segment inventé, la raison est donnée
    assert analysis['segments'] == [] and 'segments' in analysis['summary']['unavailable']

