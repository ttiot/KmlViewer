"""Adaptateur GraphHopper (TR3) : réponses ENREGISTRÉES du moteur 11.0 (tests/fixtures/graphhopper), aucun appel réseau."""

import json
import pathlib

import pytest
import requests

from app import outbound
from app.services.file_formats import MAX_FILE_POINTS
from app.services.routing import profiles  # noqa: F401
from app.services.routing.base import (Health, InvalidRequest, InvalidResponse, NoRouteFound, RoutingError, RoutingTimeout,
                                       RoutingUnavailable)
from app.services.routing.graphhopper import MAX_RESPONSE_BYTES, GraphHopperProvider, _decimate

FIX = pathlib.Path(__file__).parent / 'fixtures' / 'graphhopper'
BASE = 'http://graphhopper.internal:8989'
LILLE = (50.6292, 3.0573)


def fixture(name) -> bytes:
    return (FIX / f'{name}.json').read_bytes()


class FakeResponse:
    def __init__(self, status=200, body=b'', chunks=None, raises=None):
        self.status_code = status
        self._chunks = chunks if chunks is not None else [body]
        self._raises = raises
        self.closed = False

    def iter_content(self, size):
        for chunk in self._chunks:
            if self._raises:
                raise self._raises
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    """Rejoue une liste de réponses (ou d'exceptions) ; enregistre les appels."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def provider(*outcomes, **kw):
    session = FakeSession(*outcomes)
    clock = kw.pop('clock', Clock())
    return GraphHopperProvider(BASE, session=session, clock=clock, **{"guard": lambda destination: None, **kw}), session, clock


def ok(name='route'):
    return FakeResponse(200, fixture(name))


# --- Succès sur réponses enregistrées -------------------------------------------------------------------------------------

def test_route_decodes_the_recorded_response_with_lat_lon_order():
    gh, session, clock = provider(ok('route'))
    result = gh.route([LILLE, (50.6371, 3.0630), (50.6300, 3.0700)], 'foot', clock() + 10)
    recorded = json.loads(fixture('route'))['paths'][0]
    assert len(result.points) == 49 and result.distance_m == recorded['distance']
    lon, lat = recorded['points']['coordinates'][0]
    assert result.points[0] == (lat, lon) and 50 < result.points[0][0] < 51 and 3 < result.points[0][1] < 4
    assert result.elevations_m is None                                           # moteur sans altitude : le DEM local fait foi


def test_route_request_parameters_and_transport_options():
    gh, session, clock = provider(ok('route'))
    gh.route([LILLE, (50.6371, 3.0630)], 'hike', clock() + 10)
    url, kw = session.calls[0]
    assert url == f'{BASE}/route' and kw['allow_redirects'] is False and kw['stream'] is True
    params = kw['params']
    assert [v for k, v in params if k == 'point'] == ['50.629200,3.057300', '50.637100,3.063000']
    assert dict(p for p in params if p[0] != 'point') == {'profile': 'hike', 'points_encoded': 'false', 'instructions': 'false',
                                                           'calc_points': 'true', 'max_visited_nodes': 5_000_000}


def test_round_trip_parameters_and_result():
    gh, session, clock = provider(ok('round_trip'))
    result = gh.round_trip(LILLE, 'foot', 5000.7, 42, clock() + 10)
    params = dict(session.calls[0][1]['params'])
    assert params['algorithm'] == 'round_trip' and params['round_trip.distance'] == 5000 and params['round_trip.seed'] == 42
    assert params['max_visited_nodes'] == 5_000_000              # seul le paramètre de requête est appliqué par GraphHopper 11.0
    assert params['point'] == '50.629200,3.057300' and result.distance_m == pytest.approx(4384.182) and len(result.points) == 110


def test_the_elevation_of_the_engine_is_kept_when_every_point_has_one():
    body = json.dumps({'paths': [{'distance': 10.0, 'points': {'coordinates': [[3.0, 50.0, 12.5], [3.1, 50.1, 13.0]]}}]})
    gh, _, clock = provider(FakeResponse(200, body.encode()))
    result = gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert result.elevations_m == (12.5, 13.0)
    mixed = json.dumps({'paths': [{'distance': 10.0, 'points': {'coordinates': [[3.0, 50.0, 12.5], [3.1, 50.1]]}}]})
    gh, _, clock = provider(FakeResponse(200, mixed.encode()))
    assert gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10).elevations_m is None


def test_the_response_is_closed_after_use():
    response = ok('route')
    gh, _, clock = provider(response)
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert response.closed


# --- Erreurs du moteur ----------------------------------------------------------------------------------------------------

def test_point_not_found_gives_no_route_found_with_the_waypoint_index():
    gh, _, clock = provider(FakeResponse(400, fixture('error_point_not_found')))
    with pytest.raises(NoRouteFound) as exc:
        gh.route([(48.8566, 2.3522), LILLE], 'foot', clock() + 10)
    assert exc.value.waypoint_index == 0


def test_an_unknown_profile_is_an_incoherence_not_an_accusation_of_the_user():
    gh, _, clock = provider(FakeResponse(400, fixture('error_unknown_profile')))
    with pytest.raises(InvalidResponse):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)


@pytest.mark.parametrize('body', [b'', b'not json', b'[]', b'{"hints": "x"}', b'{"hints": [1]}'])
def test_unreadable_400_bodies_are_invalid_responses(body):
    gh, _, clock = provider(FakeResponse(400, body))
    with pytest.raises(InvalidResponse):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)


def test_the_visited_nodes_limit_is_sent_on_every_call_and_its_error_is_no_route():
    body = json.dumps({'message': 'maximum nodes exceeded: 50', 'hints': [{'message': 'maximum nodes exceeded: 50', 'details': 'x'}]})
    gh, session, clock = provider(FakeResponse(400, body.encode()), ok('route'), ok('round_trip'))
    with pytest.raises(NoRouteFound) as exc:
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert exc.value.waypoint_index is None
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    gh.round_trip(LILLE, 'foot', 5000, 1, clock() + 10)
    assert all(dict(c[1]['params'])['max_visited_nodes'] == 5_000_000 for c in session.calls)


def test_connection_not_found_is_no_route_without_index():
    body = json.dumps({'message': 'x', 'hints': [{'message': 'x', 'details': 'com.graphhopper.util.exceptions.ConnectionNotFoundException'}]})
    gh, _, clock = provider(FakeResponse(400, body.encode()))
    with pytest.raises(NoRouteFound) as exc:
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert exc.value.waypoint_index is None


def test_a_5xx_is_retried_once_then_unavailable_and_a_late_success_is_returned():
    gh, session, clock = provider(FakeResponse(503), FakeResponse(500))
    with pytest.raises(RoutingUnavailable):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert len(session.calls) == 2
    gh, session, clock = provider(FakeResponse(500), ok('route'))
    assert gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10).points
    assert len(session.calls) == 2


def test_no_retry_when_the_deadline_leaves_no_room():
    gh, session, clock = provider(FakeResponse(500))
    with pytest.raises(RoutingUnavailable):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 0.2)
    assert len(session.calls) == 1


def test_connection_errors_are_retried_then_unavailable_without_chained_cause():
    gh, session, clock = provider(requests.ConnectionError('http://secret-host/route?point=50.6,3.0'))
    with pytest.raises(RoutingUnavailable) as exc:
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert len(session.calls) == 2 and exc.value.__cause__ is None and (exc.value.__suppress_context__ or exc.value.__context__ is None)
    assert 'secret' not in str(exc.value) and '50.6' not in str(exc.value)


def test_a_timeout_is_not_retried_and_is_a_routing_timeout():
    gh, session, clock = provider(requests.Timeout())
    with pytest.raises(RoutingTimeout):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert len(session.calls) == 1


def test_a_timeout_while_reading_the_body_is_a_routing_timeout():
    gh, _, clock = provider(FakeResponse(200, chunks=[b'{"pa'], raises=requests.Timeout()))
    with pytest.raises(RoutingTimeout):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)


def test_the_call_timeout_is_the_smaller_of_the_setting_and_the_time_left():
    gh, session, clock = provider(ok('route'), timeout_s=20)
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 5)
    assert session.calls[0][1]['timeout'] == pytest.approx(5)
    gh, session, clock = provider(ok('route'), timeout_s=3)
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 50)
    assert session.calls[0][1]['timeout'] == 3


def test_an_expired_deadline_makes_no_call():
    gh, session, clock = provider(ok('route'))
    with pytest.raises(RoutingTimeout):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() - 1)
    assert session.calls == []


@pytest.mark.parametrize('status', [301, 302, 307, 308])
def test_redirects_are_never_followed(status):
    gh, session, clock = provider(FakeResponse(status, b''))
    with pytest.raises(InvalidResponse):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert len(session.calls) == 1 and session.calls[0][1]['allow_redirects'] is False


@pytest.mark.parametrize('status', [401, 403, 404, 418])
def test_other_client_statuses_are_invalid_responses(status):
    gh, _, clock = provider(FakeResponse(status, b'{}'))
    with pytest.raises(InvalidResponse):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)


def test_an_oversized_body_is_refused_while_streaming():
    big = [b'x' * (1024 * 1024)] * (MAX_RESPONSE_BYTES // (1024 * 1024) + 2)
    gh, _, clock = provider(FakeResponse(200, chunks=big))
    with pytest.raises(InvalidResponse):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)


@pytest.mark.parametrize('body', [
    b'', b'nope', b'[]', b'{}', b'{"paths": []}', b'{"paths": [{}]}', b'{"paths": [{"distance": 1, "points": {}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": "x"}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3.0, 50.0]]}}]}',                        # un seul point
    b'{"paths": [{"distance": "x", "points": {"coordinates": [[3,50],[3.1,50.1]]}}]}',
    b'{"paths": [{"distance": -5, "points": {"coordinates": [[3,50],[3.1,50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3,50],[3.1,95]]}}]}',                    # latitude hors bornes
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3,50],[3.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3,50],["a","b"]]}}]}',
    b'{"paths": [{"distance": NaN, "points": {"coordinates": [[3,50],[3.1,50.1]]}}]}',
    b'{"paths": [{"distance": 1e999, "points": {"coordinates": [[3,50],[3.1,50.1]]}}]}',
    b'[' * 100000,
])
def test_malformed_200_responses_are_invalid_responses(body):
    gh, _, clock = provider(FakeResponse(200, body))
    with pytest.raises(InvalidResponse) as exc:
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert exc.value.__cause__ is None


def test_decimation_keeps_the_ends_and_the_limit():
    pts = [[i / 1000, 50.0] for i in range(MAX_FILE_POINTS + 5000)]
    out = _decimate(pts)
    assert len(out) == MAX_FILE_POINTS and out[0] == pts[0] and out[-1] == pts[-1]
    assert _decimate(pts[:10]) == pts[:10]


# --- Entrée, destination, clé ---------------------------------------------------------------------------------------------

def test_invalid_input_is_refused_before_any_call():
    gh, session, clock = provider(ok('route'))
    for call in (lambda: gh.route([LILLE], 'foot', clock() + 5), lambda: gh.route([LILLE, (91, 0)], 'foot', clock() + 5),
                 lambda: gh.route([LILLE, LILLE], 'Foot;rm', clock() + 5), lambda: gh.route([LILLE, LILLE], 'foot', float('nan')),
                 lambda: gh.round_trip(LILLE, 'foot', 0, 1, clock() + 5), lambda: gh.round_trip(LILLE, 'foot', 1000, -1, clock() + 5)):
        with pytest.raises(InvalidRequest):
            call()
    assert session.calls == []


@pytest.mark.parametrize('url', ['ftp://h/', 'http://u:p@h:8989', 'http://', '', 'http://h\n/x', 'javascript:alert(1)'])
def test_base_url_must_be_a_plain_http_destination(url):
    with pytest.raises(outbound.OutboundError):
        GraphHopperProvider(url, guard=lambda destination: None)


def test_the_guard_runs_before_every_outbound_call_and_a_refusal_prevents_the_call():
    seen = []
    gh, session, clock = provider(ok('route'), guard=seen.append)
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    gh.health()
    assert seen[0] == 'http://graphhopper.internal:8989' and len(seen) == 1 + len(session.calls) - 1

    def refuse(destination):
        raise RuntimeError('refusé')

    gh, session, clock = provider(ok('route'), guard=refuse)
    with pytest.raises(RoutingUnavailable):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert session.calls == [] and gh.health() == Health.down()


def test_the_api_key_travels_in_a_header_never_in_the_url_and_never_over_cleartext_by_default():
    gh, session, clock = provider(ok('route'))
    gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    assert session.calls[0][1]['headers'] == {'Connection': 'close'} and 'key' not in dict(session.calls[0][1]['params'])
    gh, session, clock = provider(FakeResponse(503), api_key='s3cret', allow_cleartext_key=True)
    with pytest.raises(RoutingError) as exc:
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 10)
    kw = session.calls[0][1]
    assert kw['headers']['Authorization'] == 'Bearer s3cret' and 's3cret' not in repr(kw['params']) and 's3cret' not in str(exc.value)
    assert 's3cret' not in session.calls[0][0]
    with pytest.raises(ValueError):
        GraphHopperProvider(BASE, guard=lambda d: None, api_key='s3cret')                    # http + clé : refusé
    GraphHopperProvider('https://graphhopper.internal', guard=lambda d: None, api_key='s3cret')     # https : accepté


def test_the_guard_is_mandatory_at_construction():
    for bad in (None, 'oui', 5):
        with pytest.raises(TypeError):
            GraphHopperProvider(BASE, guard=bad)
    with pytest.raises(TypeError):
        GraphHopperProvider(BASE)


def test_a_slow_trickling_body_is_cut_at_the_deadline_not_at_the_end_of_the_stream():
    """TR3-1 : l'échéance borne la LECTURE du corps ; un moteur qui goutte-à-goutte ne retient pas au-delà."""
    clock = Clock()

    class Trickle(FakeResponse):
        def iter_content(self, size):
            for _ in range(100):
                clock.now += 1.0                                          # chaque bloc arrive une seconde plus tard
                yield b' '

    gh, session, _ = provider(Trickle(200), clock=clock)
    with pytest.raises(RoutingTimeout):
        gh.route([LILLE, (50.7, 3.1)], 'foot', clock() + 2)
    assert clock.now - 1000.0 <= 4.0                                       # arrêt au premier bloc après l'échéance


# --- health() -------------------------------------------------------------------------------------------------------------

def test_health_reads_the_recorded_info_with_lat_lon_order():
    gh, session, _ = provider(FakeResponse(200, b'OK'), ok('info'))
    health = gh.health()
    assert health.available and health.bbox == (48.82898, -8.323083, 54.598956, 6.423934)
    assert health.profiles == ('foot', 'hike', 'bike', 'racingbike', 'mtb', 'gravel')
    assert [c[0] for c in session.calls] == [f'{BASE}/health', f'{BASE}/info'] and session.calls[0][1]['timeout'] == 3.0


@pytest.mark.parametrize('outcomes', [
    [requests.ConnectionError()], [requests.Timeout()], [FakeResponse(503)], [FakeResponse(302)], [FakeResponse(404)],
])
def test_health_is_down_when_the_engine_does_not_answer_healthy(outcomes):
    gh, _, _ = provider(*outcomes)
    assert gh.health() == Health.down()


@pytest.mark.parametrize('info', [b'', b'nope', b'[]', b'{"bbox": [1, 2], "profiles": []}', b'{"bbox": [5, 5, 4, 4], "profiles": []}',
                                  b'{"bbox": [1,2,3,4], "profiles": ["x"]}', b'{"bbox": [1,2,3,4], "profiles": [{"name": "A;b"}]}'])
def test_garbage_info_never_raises_and_never_yields_a_wrong_extent(info):
    gh, _, _ = provider(FakeResponse(200, b'OK'), FakeResponse(200, info))
    health = gh.health()
    assert isinstance(health, Health) and (health == Health.down() or health.available)      # profil inattendu : ignoré, moteur disponible


def test_health_with_an_unreachable_info_keeps_the_engine_available():
    gh, _, _ = provider(FakeResponse(200, b'OK'), requests.ConnectionError())
    assert gh.health().available


# --- Profils (RB-3.1) -----------------------------------------------------------------------------------------------------

def test_the_activity_table_matches_the_spec():
    table = {a.key: (a.profile, a.stored_type) for a in profiles.ACTIVITIES.values()}
    assert table == {'run': ('foot', 'run'), 'trail': ('hike', 'trail'), 'hike': ('hike', 'hike'),
                     'road_bike': ('racingbike', 'road_bike'), 'gravel': ('gravel', 'road_bike'), 'mtb': ('mtb', 'mtb')}


@pytest.mark.parametrize('value', [None, '', 'bike', 'foot', 'RUN', ' run', 1, ['run'], 'run\n'])
def test_unknown_activities_are_refused(value):
    assert profiles.get(value) is None


def test_default_activity_follows_the_user_default_type_otherwise_running():
    assert [profiles.default_for(t) for t in ('run', 'trail', 'hike', 'road_bike', 'mtb', 'walk', None, 'car')] == \
        ['run', 'trail', 'hike', 'road_bike', 'mtb', 'run', 'run', 'run']
    assert [o['key'] for o in profiles.options()] == ['run', 'trail', 'hike', 'road_bike', 'gravel', 'mtb']


# --- TR3-1 : l'échéance borne aussi un moteur lent sur les EN-TÊTES et sur un corps avec Content-Length (serveur réel) --------------

def _slow_server(kind):
    """Serveur TCP réel : `headers` égrène les en-têtes, `body` annonce un Content-Length puis égrène le corps."""
    import socket as socket_module
    import threading as threading_module
    import time as time_module
    listener = socket_module.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(5)
    stop = threading_module.Event()

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            threading_module.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn):
        try:
            conn.settimeout(5)
            conn.recv(65536)
            payload = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n'
            if kind == 'headers':
                for byte in payload:
                    conn.sendall(bytes([byte]))
                    time_module.sleep(0.3)
            else:
                conn.sendall(payload)
                for _ in range(200):
                    conn.sendall(b' ')
                    time_module.sleep(0.3)
        except OSError:
            pass
        finally:
            conn.close()

    threading_module.Thread(target=serve, daemon=True).start()
    return listener, stop


@pytest.mark.parametrize('kind', ['headers', 'body'])
def test_a_slow_real_engine_is_cut_at_the_deadline_on_headers_and_on_a_content_length_body(kind):
    import time as time_module
    listener, stop = _slow_server(kind)
    try:
        gh = GraphHopperProvider(f'http://127.0.0.1:{listener.getsockname()[1]}', guard=lambda d: None, timeout_s=20)
        started = time_module.monotonic()
        with pytest.raises(RoutingTimeout):
            gh.route([LILLE, (50.7, 3.1)], 'foot', time_module.monotonic() + 1.0)
        assert time_module.monotonic() - started < 3.0
    finally:
        stop.set()
        listener.close()


# --- TR3-1b : résolution DNS / connexion TCP bornées par l'échéance --------------------------------------------------------------

def test_a_blocked_name_resolution_is_cut_at_the_deadline(monkeypatch):
    import socket as socket_module
    import threading as threading_module
    import time as time_module
    release = threading_module.Event()

    def slow_resolver(*args, **kwargs):
        release.wait(8)                                                    # un DNS qui ne répond pas
        raise socket_module.gaierror('timeout')

    monkeypatch.setattr(socket_module, 'getaddrinfo', slow_resolver)
    gh = GraphHopperProvider('http://moteur-qui-ne-repond-pas.example:8989', guard=lambda d: None, timeout_s=30)
    started = time_module.monotonic()
    try:
        with pytest.raises(RoutingTimeout):
            gh.route([LILLE, (50.7, 3.1)], 'foot', time_module.monotonic() + 1.0)
        assert time_module.monotonic() - started < 2.5
    finally:
        release.set()


def test_a_connection_that_never_completes_is_cut_at_the_deadline():
    """Socket en écoute dont la file d'attente est saturée : la connexion TCP suivante reste en attente."""
    import socket as socket_module
    import time as time_module
    listener = socket_module.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(0)
    fillers = []
    try:
        for _ in range(6):                                                 # sature la file d'acceptation
            filler = socket_module.socket()
            filler.setblocking(False)
            try:
                filler.connect(listener.getsockname())
            except (BlockingIOError, OSError):
                pass
            fillers.append(filler)
        gh = GraphHopperProvider(f'http://127.0.0.1:{listener.getsockname()[1]}', guard=lambda d: None, timeout_s=30)
        started = time_module.monotonic()
        with pytest.raises((RoutingTimeout, RoutingUnavailable)):
            gh.route([LILLE, (50.7, 3.1)], 'foot', time_module.monotonic() + 1.0)
        assert time_module.monotonic() - started < 2.5
    finally:
        for filler in fillers:
            filler.close()
        listener.close()
