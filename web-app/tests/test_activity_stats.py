import numpy as np
import pytest

from app.services import activity_stats, geo
from app.services.gpx_parser import GPXParser
from tests.conftest import line, make_gpx


def _track(points, **kwargs):
    return activity_stats.extract_track(GPXParser.parse_gpx_coordinates(make_gpx(points, **kwargs)))


def test_summary_distance_and_times():
    track = _track(line(101))  # 100 intervalles de ~33,36 m, 10 s chacun
    summary = activity_stats.compute_summary(track)
    assert track.name == 'Sortie test' and track.activity_type == 'running'
    assert summary['distance_m'] == pytest.approx(3336, rel=0.01)
    assert summary['has_time'] and summary['duration_s'] == 1000
    assert summary['moving_s'] == 1000
    assert summary['max_speed_ms'] == pytest.approx(3.34, abs=0.05)
    assert summary['best_efforts']['1000'] == pytest.approx(300, abs=10)
    assert '5000' not in summary['best_efforts']


def test_pause_is_excluded_from_moving_time():
    points = line(20) + [line(20)[-1]] * 30 + line(20, lat0=45.0 + 20 * 0.0003)
    summary = activity_stats.compute_summary(_track(points))
    assert summary['moving_s'] < summary['duration_s'] - 250


def test_gps_noise_does_not_inflate_ascent():
    rng = np.random.default_rng(42)
    noisy = line(400, ele=lambda i: 500 + i * 0.5 + rng.normal(0, 2.5))  # vraie montée : 200 m
    summary = activity_stats.compute_summary(_track(noisy))
    naive = sum(max(0, b[2] - a[2]) for a, b in zip(noisy, noisy[1:]))
    assert naive > 500
    assert summary['ascent_gps_m'] == pytest.approx(200, abs=30)


def test_ascent_descent_hysteresis():
    ascent, descent = geo.ascent_descent(np.array([0, 1, 0, 1, 0, 10, 10, 4]), threshold_m=3, smooth_window=1)
    assert (ascent, descent) == (10, 6)


def test_track_without_time():
    summary = activity_stats.compute_summary(_track(line(10), with_time=False))
    assert not summary['has_time'] and summary['duration_s'] is None and summary['best_efforts'] == {}


def test_single_point_is_rejected():
    with pytest.raises(ValueError):
        activity_stats.compute_summary(_track(line(1)))
