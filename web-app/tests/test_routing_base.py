"""Interface de routage et provider factice (TR1)."""

import ast
import math
import pathlib
import threading
import time

import pytest

from app.services import geo
from app.services.routing import base
from app.services.routing.base import (Health, InvalidRequest, InvalidResponse, NoRouteFound, Point, RouteResult,
                                       RoutingError, RoutingProvider, RoutingTimeout, RoutingUnavailable)
from tests.routing_fakes import FakeProvider, length_m, unavailable

D = time.monotonic() + 3600     # échéance lointaine : la plupart des tests ne s'y intéressent pas
A, B, C = (50.6292, 3.0573), (50.6400, 3.0800), (50.6200, 3.1000)    # Lille et environs


# --- base.py ---------------------------------------------------------------------------------

def test_module_is_pure():
    """Ni Flask, ni base, ni réseau, ni environnement (spec §4 : « Pur »)."""
    tree = ast.parse(pathlib.Path(base.__file__).read_text())
    modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    modules |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    # file_formats : constante MAX_FILE_POINTS (RB-S4), module sans Flask ni base. Rien d'autre de app.
    assert modules <= {'math', 'time', 'abc', 'dataclasses', 'typing', 'app.services.file_formats'}


def test_provider_interface_cannot_be_instantiated_and_requires_the_three_methods():
    with pytest.raises(TypeError):
        RoutingProvider()
    assert RoutingProvider.__abstractmethods__ == {'route', 'round_trip', 'health'}


def test_errors_are_routing_errors_with_fixed_messages():
    for cls in (RoutingUnavailable, InvalidResponse, InvalidRequest, NoRouteFound):
        err = cls()
        assert isinstance(err, RoutingError) and str(err) == cls.message
        assert 'http' not in str(err).lower()


def test_no_route_found_carries_a_validated_waypoint_index():
    assert NoRouteFound().waypoint_index is None
    assert NoRouteFound(3).waypoint_index == 3
    for bad in (-1, True, 1.5, '2'):
        with pytest.raises(ValueError):
            NoRouteFound(bad)


@pytest.mark.parametrize('bad', [
    (91, 0), (-90.1, 0), (0, 181), (0, -180.5), (math.nan, 0), (0, math.inf), (None, 1), ('1', '2'), (True, 1),
    (1,), (1, 2, 3), 'ab', None, 5, {'lat': 1, 'lon': 2},
])
def test_invalid_points_are_refused(bad):
    with pytest.raises(InvalidRequest):
        base.validate_point(bad)


def test_valid_points_are_normalized_to_float_points():
    assert base.validate_point((50, 3)) == Point(50.0, 3.0)
    assert base.validate_point([90, -180]) == Point(90.0, -180.0)


@pytest.mark.parametrize('waypoints', [[], [A], [A] * (base.MAX_WAYPOINTS + 1), 'ab', None, [A, (999, 0)], [A, 'x']])
def test_route_request_bounds(waypoints):
    with pytest.raises(InvalidRequest):
        base.validate_route_request(waypoints, 'foot')


@pytest.mark.parametrize('profile', ['', None, 3, 'Foot', 'foo bar', '../x', 'a' * 33, 'pied²', "foot'; --"])
def test_profile_is_a_short_lowercase_token(profile):
    with pytest.raises(InvalidRequest):
        base.validate_profile(profile)
    assert base.validate_profile('racingbike_2') == 'racingbike_2'


@pytest.mark.parametrize('distance,seed', [
    (0, 1), (-5, 1), (math.nan, 1), (math.inf, 1), (base.MAX_DISTANCE_M + 1, 1), ('10', 1), (True, 1),
    (1000, -1), (1000, 2 ** 31), (1000, 1.5), (1000, True), (1000, None),
])
def test_round_trip_request_bounds(distance, seed):
    with pytest.raises(InvalidRequest):
        base.validate_round_trip_request(A, 'foot', distance, seed)


def test_route_result_is_valid_and_immutable():
    result = RouteResult(points=[A, B], distance_m=1500, elevations_m=[10, 12], warnings=['x'])
    assert result.points == (Point(*A), Point(*B)) and result.warnings == ('x',)
    with pytest.raises(AttributeError):
        result.distance_m = 1


@pytest.mark.parametrize('bad', [
    dict(points=[A], distance_m=1), dict(points=[A, B], distance_m=-1), dict(points=[A, B], distance_m=math.nan),
    dict(points=[A, (200, 0)], distance_m=1), dict(points=[A, (math.nan, 1)], distance_m=1),
    dict(points=[A, 'x'], distance_m=1), dict(points=[A, (1, 2, 3)], distance_m=1), dict(points=None, distance_m=1),
    dict(points=[A, B], distance_m=1, elevations_m=[1]), dict(points=[A, B], distance_m=1, elevations_m=[1, math.inf]),
    dict(points=[A, B], distance_m=1, elevations_m=5), dict(points=[A, B], distance_m=1, warnings=5),
])
def test_invalid_engine_response_is_invalid_response_never_invalid_request(bad):
    """TR1-1 : une anomalie du MOTEUR est un 502, pas un 400 qui accuserait l'utilisateur."""
    with pytest.raises(InvalidResponse) as exc:
        RouteResult(**bad)
    assert not isinstance(exc.value, InvalidRequest)


def test_warnings_are_bounded_and_cleaned():
    """TR1-3 : nombre, longueur et caractères de contrôle bornés."""
    many = RouteResult(points=[A, B], distance_m=1, warnings=[f'w{i}' for i in range(50)])
    assert len(many.warnings) == base.MAX_WARNINGS
    long = RouteResult(points=[A, B], distance_m=1, warnings=['x' * 3200, 'a\x00b\x1b[31m\nc'])
    assert len(long.warnings[0]) == base.MAX_WARNING_LENGTH
    assert long.warnings[1] == 'ab[31mc'


@pytest.mark.parametrize('kwargs', [
    dict(available=True, bbox=('a', None)), dict(available=True, bbox=(1, 2, 3)), dict(available=True, bbox=(0, 0, 91, 5)),
    dict(available=True, bbox=(50, 3, 49, 4)), dict(available=True, bbox=(50, 3, 51, 3)),
    dict(available=True, bbox=(0, 0, math.nan, 5)), dict(available=True, bbox=5),
    dict(available=True, profiles=('../x',)), dict(available=True, profiles=5),
    dict(available=True, profiles=tuple(f'p{i}' for i in range(base.MAX_HEALTH_PROFILES + 1))),
    dict(available='oui'),
])
def test_health_from_the_engine_is_validated(kwargs):
    """TR1-4 : emprise et profils non fiables ⇒ InvalidResponse (la tâche de sondage répond Health.down())."""
    with pytest.raises(InvalidResponse):
        Health(**kwargs)


def test_valid_health_is_normalized():
    health = Health(available=True, bbox=[49, 2, 51.6, 6.5], profiles=['foot', 'mtb'])
    assert health.bbox == (49.0, 2.0, 51.6, 6.5) and health.profiles == ('foot', 'mtb')


def test_timeout_is_distinguishable_from_unavailable():
    """TR1-5 : RoutingTimeout est une RoutingUnavailable, mais se teste en premier (504 vs 503)."""
    assert issubclass(RoutingTimeout, RoutingUnavailable)
    assert RoutingTimeout.message != RoutingUnavailable.message
    with pytest.raises(RoutingTimeout):
        FakeProvider(fail=RoutingTimeout()).route([A, B], 'foot', D)
    try:
        FakeProvider(fail=RoutingUnavailable()).route([A, B], 'foot', D)
    except RoutingTimeout:
        pytest.fail("un moteur injoignable n'est pas un délai")
    except RoutingUnavailable:
        pass


def test_provider_declares_round_trip_support():
    assert RoutingProvider.supports_round_trip is True
    assert FakeProvider().supports_round_trip is True


@pytest.mark.parametrize('deadline', [None, math.nan, math.inf, '10', True])
def test_deadline_must_be_a_finite_number(deadline):
    with pytest.raises(InvalidRequest):
        base.validate_deadline(deadline)
    with pytest.raises(InvalidRequest):
        FakeProvider().route([A, B], 'foot', deadline)


def test_health_down():
    assert Health.down() == Health(available=False, bbox=None, profiles=())


# --- Provider factice ------------------------------------------------------------------------

def test_fake_is_a_provider_and_makes_no_network_call(monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail("accès réseau"))
    fake = FakeProvider()
    assert isinstance(fake, RoutingProvider)
    fake.route([A, B], 'foot', D)
    fake.round_trip(A, 'foot', 5000, 1, D)
    fake.health()


def test_straight_route_passes_through_every_waypoint_in_order_with_known_length():
    result = FakeProvider().route([A, B, C], 'foot', D)
    expected = geo.haversine(*A, *B) + geo.haversine(*B, *C)
    assert result.distance_m == pytest.approx(expected, rel=1e-6)
    assert result.points[0] == Point(*A) and result.points[-1] == Point(*C)
    assert Point(*B) in result.points
    assert result.points.index(Point(*A)) < result.points.index(Point(*B)) < result.points.index(Point(*C))


def test_distance_is_a_known_function_of_the_geometry():
    """Plus de sinuosité ⇒ trace plus longue, monotone, et distance_m == longueur haversine de la trace."""
    lengths = []
    for sinuosity in (0.0, 0.05, 0.15, 0.3):
        result = FakeProvider(sinuosity=sinuosity).route([A, B], 'foot', D)
        assert result.distance_m == pytest.approx(length_m(result.points), rel=1e-9)
        lengths.append(result.distance_m)
    assert lengths == sorted(lengths) and lengths[-1] > lengths[0] * 1.05


def test_route_is_deterministic():
    a = FakeProvider(sinuosity=0.2).route([A, B, C], 'hike', D)
    b = FakeProvider(sinuosity=0.2).route([A, B, C], 'hike', D)
    assert a == b


def test_round_trip_is_closed_close_to_target_and_seeded():
    fake = FakeProvider()
    one = fake.round_trip(A, 'foot', 10_000, 7, D)
    assert one.points[0] == one.points[-1] == Point(*A)
    assert one.distance_m == pytest.approx(10_000, rel=0.02)
    assert fake.round_trip(A, 'foot', 10_000, 7, D) == one                    # même graine, même boucle
    assert fake.round_trip(A, 'foot', 10_000, 8, D).points != one.points      # graine différente, autre boucle


def test_calls_are_recorded_in_order_and_inputs_validated_before_any_call():
    fake = FakeProvider()
    fake.route([A, B], 'foot', D)
    fake.round_trip(A, 'mtb', 3000, 1, D)
    assert [c[0] for c in fake.calls] == ['route', 'round_trip']
    with pytest.raises(InvalidRequest):
        fake.route([A], 'foot', D)
    with pytest.raises(InvalidRequest):
        fake.round_trip(A, 'foot', 0, 1, D)
    assert len(fake.calls) == 2           # CA-R2 : une entrée invalide n'atteint pas le moteur


def test_unroutable_point_reports_its_index():
    fake = FakeProvider(unroutable=lambda p: abs(p[0] - B[0]) < 1e-9)
    with pytest.raises(NoRouteFound) as exc:
        fake.route([A, B, C], 'foot', D)
    assert exc.value.waypoint_index == 1


def test_scripted_failures_are_consumed_one_per_call():
    fake = FakeProvider(fail=unavailable(2))
    for _ in range(2):
        with pytest.raises(RoutingUnavailable):
            fake.route([A, B], 'foot', D)
    assert fake.route([A, B], 'foot', D).points            # le troisième appel réussit
    assert len(fake.calls) == 3


def test_slow_provider_uses_the_injected_sleep():
    slept = []
    fake = FakeProvider(delay_s=2.5, sleep=slept.append)
    fake.route([A, B], 'foot', D)
    assert slept == [2.5]


def test_health_toggle():
    fake = FakeProvider()
    assert fake.health().available and fake.health().bbox and 'foot' in fake.health().profiles
    fake.available = False
    assert fake.health() == Health.down() and fake.health_calls == 4


# --- Échéance (TR1-2, RB-4.6, CA-R7) ----------------------------------------------------------

class FakeTime:
    """Horloge et sommeil factices : dormir fait avancer l'horloge, sans attendre."""

    def __init__(self):
        self.now = 1000.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_deadline_already_passed_means_no_call():
    t = FakeTime()
    fake = FakeProvider(clock=t.clock, sleep=t.sleep)
    for call in (lambda: fake.route([A, B], 'foot', t.now), lambda: fake.round_trip(A, 'foot', 5000, 1, t.now - 1)):
        with pytest.raises(RoutingTimeout):
            call()
    assert fake.calls == []


def test_slow_call_is_cut_at_the_deadline_not_at_its_own_duration():
    """Appel de 20 s, échéance dans 5 s ⇒ durée totale 5 s, RoutingTimeout."""
    t = FakeTime()
    fake = FakeProvider(delay_s=20, clock=t.clock, sleep=t.sleep)
    start = t.now
    with pytest.raises(RoutingTimeout):
        fake.route([A, B], 'foot', start + 5)
    assert t.now - start == pytest.approx(5)


def test_total_budget_is_held_over_a_series_of_slow_calls():
    """CA-R7 : un builder qui enchaîne des appels lents ne dépasse jamais son budget total (+ ε)."""
    t = FakeTime()
    fake = FakeProvider(delay_s=4, clock=t.clock, sleep=t.sleep)
    start, budget, done = t.now, 30, 0
    deadline = start + budget
    while True:
        try:
            fake.route([A, B], 'foot', deadline)
            done += 1
        except RoutingTimeout:
            break
    assert done == 7 and len(fake.calls) == 8          # le 8e est coupé à l'échéance
    assert t.now - start == pytest.approx(budget)


def test_call_that_fits_in_the_deadline_completes():
    t = FakeTime()
    fake = FakeProvider(delay_s=2, clock=t.clock, sleep=t.sleep)
    assert fake.route([A, B], 'foot', t.now + 10).points and t.now == 1002.0


# --- Factice sûr entre threads (TR1-6) --------------------------------------------------------

def test_concurrency_is_counted_exactly_across_threads():
    """8 appels qui se chevauchent vraiment : le pic observé vaut 8, et les 8 appels sont tous tracés."""
    barrier = threading.Barrier(8)
    fake = FakeProvider(delay_s=1, sleep=lambda s: barrier.wait(10))
    threads = [threading.Thread(target=fake.route, args=([A, B], 'foot', D)) for _ in range(8)]
    [th.start() for th in threads]
    [th.join(20) for th in threads]
    assert fake.max_concurrent_seen == 8 and len(fake.calls) == 8 and fake._active == 0


def test_scripted_failures_are_consumed_exactly_once_across_threads():
    fake = FakeProvider(fail=unavailable(20))
    outcomes = []

    def work():
        try:
            fake.route([A, B], 'foot', D)
            outcomes.append('ok')
        except RoutingUnavailable:
            outcomes.append('ko')

    threads = [threading.Thread(target=work) for _ in range(40)]
    [th.start() for th in threads]
    [th.join(20) for th in threads]
    assert outcomes.count('ko') == 20 and outcomes.count('ok') == 20
