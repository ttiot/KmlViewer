"""
Cas limites de l'interface de routage et du provider factice (TR1, RB-S4, CA-R15, RB-S1, RB-S8).

Complète test_routing_base.py : bornes exactes, entiers démesurés, nombre de points,
déterminisme (instances, processus), délai et horloge injectés, messages sans donnée interne.
"""

import copy
import hashlib
import math
import os
import pathlib
import subprocess
import sys
import time

import pytest

from app.services.file_formats import MAX_FILE_POINTS
from app.services.routing import base
from app.services.routing.base import (InvalidRequest, InvalidResponse, NoRouteFound, Point, RouteResult,
                                       RoutingError, RoutingTimeout, RoutingUnavailable)
from tests.routing_fakes import FakeProvider, length_m, unavailable

D = time.monotonic() + 3600
A, B, C = (50.6292, 3.0573), (50.6400, 3.0800), (50.6200, 3.1000)
HUGE = 10 ** 400            # un entier JSON valide que float() ne sait pas convertir


# --- Coordonnées : bornes exactes, NaN, démesure ---------------------------------------------

@pytest.mark.parametrize('point', [(90, 180), (-90, -180), (90.0, -180.0), (0, 0), (-0.0, -0.0), (89.999999999, 179.999999999)])
def test_bounds_are_inclusive(point):
    assert base.validate_point(point) == Point(float(point[0]), float(point[1]))


@pytest.mark.parametrize('point', [
    (90.000000001, 0), (-90.000000001, 0), (0, 180.000000001), (0, -180.000000001),
    (float('nan'), float('nan')), (-math.inf, 0), (0, -math.inf), (1e308, 0), (0, -1e308), (91, 0),
])
def test_just_outside_the_bounds_and_non_finite_are_refused(point):
    with pytest.raises(InvalidRequest):
        base.validate_point(point)


@pytest.mark.parametrize('call', [
    lambda: base.validate_point((HUGE, 0)),
    lambda: base.validate_point((0, -HUGE)),
    lambda: base.validate_route_request([(HUGE, 0), (1, 1)], 'foot'),
    lambda: base.validate_round_trip_request((1, 1), 'foot', HUGE, 1),
    lambda: base.validate_deadline(HUGE),
], ids=['lat', 'lon', 'route', 'distance', 'deadline'])
def test_oversized_integers_are_an_invalid_request_not_an_overflow(call):
    with pytest.raises(InvalidRequest):
        call()


@pytest.mark.parametrize('kwargs', [dict(distance_m=HUGE), dict(distance_m=1, elevations_m=[1, HUGE])],
                         ids=['distance', 'elevation'])
def test_oversized_integers_in_an_engine_response_are_an_invalid_response(kwargs):
    with pytest.raises(InvalidResponse):
        RouteResult(points=[A, B], **kwargs)


def test_waypoints_exactly_at_the_ceiling_are_accepted_and_one_more_is_not():
    points = [A, B] * (base.MAX_WAYPOINTS // 2)
    assert len(base.validate_route_request(points, 'foot')[0]) == base.MAX_WAYPOINTS
    with pytest.raises(InvalidRequest):
        base.validate_route_request(points + [A], 'foot')
    assert len(base.validate_route_request(tuple(points[:2]), 'foot')[0]) == 2


def test_generators_and_unsized_iterables_are_refused_not_consumed():
    consumed = []

    def gen():
        for p in (A, B, C):
            consumed.append(p)
            yield p

    with pytest.raises(InvalidRequest):
        base.validate_route_request(gen(), 'foot')
    assert consumed == []


def test_a_bad_point_anywhere_in_the_list_is_found():
    for position in (0, 1, 99, 199):
        points = [A] * 200
        points[position] = (math.nan, 0)
        with pytest.raises(InvalidRequest):
            base.validate_route_request(points, 'foot')


@pytest.mark.parametrize('distance', [1e-9, 1, 1000, base.MAX_DISTANCE_M])
@pytest.mark.parametrize('seed', [0, 1, base.MAX_SEED])
def test_round_trip_request_inclusive_bounds(distance, seed):
    assert base.validate_round_trip_request(A, 'foot', distance, seed)[2:] == (float(distance), seed)


# --- Réponse du moteur : nombre de points (RB-S4) --------------------------------------------

def test_route_result_accepts_exactly_max_file_points():
    result = RouteResult(points=[(50 + i * 1e-6, 3.0) for i in range(MAX_FILE_POINTS)], distance_m=1)
    assert len(result.points) == MAX_FILE_POINTS


def test_route_result_refuses_more_than_max_file_points():
    with pytest.raises(InvalidResponse):
        RouteResult(points=[(50 + i * 1e-6, 3.0) for i in range(MAX_FILE_POINTS + 1)], distance_m=1)


def test_fake_never_returns_more_than_max_file_points():
    waypoints = [(0, (i % 2) * 90) for i in range(base.MAX_WAYPOINTS)]
    assert len(FakeProvider().route(waypoints, 'foot', D).points) <= MAX_FILE_POINTS


def test_fake_round_trip_at_the_distance_ceiling_stays_below_max_file_points():
    result = FakeProvider().round_trip(A, 'foot', base.MAX_DISTANCE_M, 1, D)
    assert len(result.points) <= MAX_FILE_POINTS
    assert result.distance_m == pytest.approx(base.MAX_DISTANCE_M, rel=0.02)


@pytest.mark.parametrize('start', [(89.99, 0.0), (0.0, 179.99), (0.0, -179.99)])
def test_fake_round_trip_near_the_edges_does_not_blame_the_engine(start):
    try:
        FakeProvider().round_trip(start, 'foot', 50_000, 1, D)
    except NoRouteFound:
        pass


# --- Déterminisme du factice ------------------------------------------------------------------

def test_same_seed_gives_the_same_loop_regardless_of_instance_history_and_settings():
    fresh = FakeProvider().round_trip(A, 'foot', 12_000, 42, D)
    used = FakeProvider(sinuosity=0.4, delay_s=0.0)
    used.route([A, B, C], 'foot', D)
    used.round_trip(B, 'mtb', 3000, 1, D)
    assert used.round_trip(A, 'foot', 12_000, 42, D) == fresh
    assert FakeProvider().round_trip(A, 'gravel', 12_000, 42, D) == fresh      # le profil n'y change rien


def test_seeds_give_different_loops_and_all_start_at_the_same_point():
    loops = {seed: FakeProvider().round_trip(A, 'foot', 8000, seed, D) for seed in (0, 1, 2, 3, 99, base.MAX_SEED)}
    assert len({loop.points for loop in loops.values()}) == len(loops)
    for loop in loops.values():
        assert loop.points[0] == loop.points[-1] == Point(*A)
        assert loop.distance_m == pytest.approx(8000, rel=0.02)


@pytest.mark.parametrize('distance', [500, 5_000, 80_000, 400_000])
@pytest.mark.parametrize('start', [(50.6, 3.0), (70.0, 25.0), (-33.9, 151.2), (0.0, 0.0)])
def test_round_trip_length_tracks_the_target_at_every_scale_and_latitude(distance, start):
    result = FakeProvider().round_trip(start, 'foot', distance, 5, D)
    assert result.distance_m == pytest.approx(distance, rel=0.03)
    assert result.distance_m == pytest.approx(length_m(result.points), rel=1e-9)


def test_determinism_holds_across_processes_and_hash_seeds():
    """Deux processus aux PYTHONHASHSEED différents produisent des traces strictement identiques."""
    code = (
        "import hashlib, time\n"
        "from tests.routing_fakes import FakeProvider\n"
        "d = time.monotonic() + 3600\n"
        "f = FakeProvider(sinuosity=0.2)\n"
        "r = [f.round_trip((50.6, 3.0), 'foot', 9000, s, d).points for s in (0, 7, 123456)]\n"
        "r.append(f.route([(50.6, 3.0), (50.7, 3.1), (50.65, 3.2)], 'hike', d).points)\n"
        "print(hashlib.sha256(repr(r).encode()).hexdigest())\n"
    )
    root = str(pathlib.Path(__file__).resolve().parents[1])
    digests = set()
    for hash_seed in ('0', '1', '4242'):
        env = {**os.environ, 'PYTHONHASHSEED': hash_seed, 'PYTHONPATH': root}
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, cwd=root, check=True)
        digests.add(out.stdout.strip())
    assert len(digests) == 1 and len(next(iter(digests))) == 64


def test_route_does_not_depend_on_the_deadline_or_the_call_count():
    fake = FakeProvider(sinuosity=0.1)
    first = fake.route([A, B, C], 'foot', D)
    assert fake.route([A, B, C], 'foot', D + 1000) == first
    assert fake.route([A, B, C], 'mtb', time.monotonic() + 5) == first


def test_route_with_identical_consecutive_points_is_valid():
    result = FakeProvider().route([A, A], 'foot', D)
    assert result.distance_m == 0 and len(result.points) >= 2


def test_route_to_the_very_same_waypoint_list_is_not_mutated():
    waypoints = [list(A), list(B), list(C)]
    snapshot = copy.deepcopy(waypoints)
    FakeProvider().route(waypoints, 'foot', D)
    assert waypoints == snapshot


# --- Délai et horloge injectés ---------------------------------------------------------------

class FakeTime:
    def __init__(self, now=1000.0):
        self.now, self.sleeps = now, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_no_delay_never_touches_sleep_or_clock_beyond_the_deadline_check():
    t = FakeTime()
    fake = FakeProvider(sleep=t.sleep, clock=t.clock)
    fake.route([A, B], 'foot', t.now + 10)
    assert t.sleeps == [] and t.now == 1000.0


def test_default_clock_and_sleep_are_the_real_ones():
    fake = FakeProvider(delay_s=0.05)
    start = time.monotonic()
    fake.route([A, B], 'foot', time.monotonic() + 5)
    assert 0.04 <= time.monotonic() - start < 2


def test_delay_exactly_equal_to_the_time_left_times_out_at_the_deadline():
    t = FakeTime()
    fake = FakeProvider(delay_s=5, sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingTimeout):
        fake.route([A, B], 'foot', t.now + 5)
    assert t.sleeps == [5] and t.now == 1005.0


def test_delay_just_under_the_time_left_completes():
    t = FakeTime()
    fake = FakeProvider(delay_s=4.999, sleep=t.sleep, clock=t.clock)
    assert fake.route([A, B], 'foot', t.now + 5).points
    assert t.now == pytest.approx(1004.999)


def test_sleep_is_capped_at_the_time_left():
    t = FakeTime()
    fake = FakeProvider(delay_s=100, sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingTimeout):
        fake.round_trip(A, 'foot', 5000, 1, t.now + 3)
    assert t.sleeps == [3]


def test_deadline_exactly_now_is_already_passed():
    t = FakeTime()
    fake = FakeProvider(sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingTimeout):
        fake.route([A, B], 'foot', t.now)
    assert fake.calls == []


def test_timeout_wins_over_a_scripted_failure_when_the_call_is_too_slow():
    t = FakeTime()
    fake = FakeProvider(delay_s=10, fail=unavailable(1), sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingTimeout):
        fake.route([A, B], 'foot', t.now + 2)


def test_scripted_failure_is_raised_after_the_delay_when_it_fits():
    t = FakeTime()
    fake = FakeProvider(delay_s=1, fail=unavailable(1), sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingUnavailable) as exc:
        fake.route([A, B], 'foot', t.now + 10)
    assert not isinstance(exc.value, RoutingTimeout) and t.now == 1001.0


def test_expired_deadline_does_not_consume_a_scripted_failure():
    t = FakeTime()
    fake = FakeProvider(fail=unavailable(1), sleep=t.sleep, clock=t.clock)
    with pytest.raises(RoutingTimeout):
        fake.route([A, B], 'foot', t.now - 1)
    with pytest.raises(RoutingUnavailable):
        fake.route([A, B], 'foot', t.now + 10)       # l'échec scripté attendait toujours


def test_active_counter_returns_to_zero_after_every_kind_of_outcome():
    t = FakeTime()
    fake = FakeProvider(delay_s=3, fail=[RoutingUnavailable(), None, None], unroutable=lambda p: p == B,
                        sleep=t.sleep, clock=t.clock)
    for call in (lambda: fake.route([A, C], 'foot', t.now + 100),      # échec scripté
                 lambda: fake.route([A, B], 'foot', t.now + 100),      # NoRouteFound
                 lambda: fake.route([A, C], 'foot', t.now + 1)):       # délai
        with pytest.raises(RoutingError):
            call()
    assert fake._active == 0 and fake.max_concurrent_seen == 1


# --- Messages et chaînage (CA-R15, RB-S8) -----------------------------------------------------

def _raised(fake_call):
    with pytest.raises(RoutingError) as exc:
        fake_call()
    return exc.value


def test_no_error_message_carries_coordinates_urls_or_the_response():
    cases = [
        lambda: FakeProvider(unroutable=lambda p: True).route([A, B], 'foot', D),
        lambda: FakeProvider(fail=unavailable(1)).route([A, B], 'foot', D),
        lambda: FakeProvider().route([A, (999.123456, 0)], 'foot', D),
        lambda: FakeProvider().route([A, B], 'foot', time.monotonic() - 5),
        lambda: FakeProvider().round_trip(A, 'foot', -1234.5678, 1, D),
        lambda: RouteResult(points=[A, (88.8888, 999.5)], distance_m=1),
    ]
    for case in cases:
        err = _raised(case)
        text = f"{err!s} {err!r} {err.args}"
        assert not any(token in text for token in ('50.6', '999', '1234', '88.8888', 'http', '://')), text


def test_errors_raised_by_the_fake_do_not_chain_the_original_exception():
    """RB-S8 : `raise … from None` ; un __cause__ ou un __context__ finirait dans une trace journalisée."""
    for case in (lambda: FakeProvider().route([A, (999, 0)], 'foot', D),
                 lambda: RouteResult(points=[A, (999, 0)], distance_m=1),
                 lambda: RouteResult(points=[A, B], distance_m=-1)):
        err = _raised(case)
        assert err.__cause__ is None and (err.__context__ is None or err.__suppress_context__)


def test_every_error_class_has_a_distinct_fixed_message_and_status_mapping_order():
    classes = (RoutingUnavailable, RoutingTimeout, InvalidResponse, InvalidRequest, NoRouteFound)
    assert len({cls.message for cls in classes}) == len(classes)
    assert issubclass(RoutingTimeout, RoutingUnavailable)
    assert all(issubclass(cls, RoutingError) for cls in classes)
    assert not issubclass(InvalidResponse, InvalidRequest) and not issubclass(InvalidRequest, InvalidResponse)


def test_no_route_found_index_does_not_appear_in_its_message():
    err = NoRouteFound(7)
    assert '7' not in str(err) and err.waypoint_index == 7


def test_error_digest_is_stable():
    """Garde-fou : les messages sont une interface (le builder les mappe sur des codes HTTP)."""
    digest = hashlib.sha256('|'.join(c.message for c in (RoutingUnavailable, RoutingTimeout, InvalidResponse,
                                                         InvalidRequest, NoRouteFound)).encode()).hexdigest()
    assert len(digest) == 64
