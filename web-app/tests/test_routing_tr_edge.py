"""
TR3 (adaptateur GraphHopper) et TR2 (builder, détours) en profondeur (rôle test), écrit depuis gpx-route-builder (RB-4.x, RB-S3 à RB-S9,
CA-R2 à R33) et l'interface RoutingProvider, pas depuis le code.

 A. L'adaptateur contre un VRAI serveur HTTP local (sockets) : requêtes émises, redirections, flux borné, bombe gzip, goutte-à-goutte,
    reprises, erreurs sans fuite, garde, clé d'API, sonde de santé.
 B. Le builder : invariants vérifiés sur des centaines d'entrées aléatoires (graines fixes) avec injection de pannes ; cas limites.
"""

import gzip
import json
import logging
import math
import random
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from app import outbound
from app.services.gpx_parser import GPXParser
from app.services.routing import quality
from app.services.routing.base import (Health, InvalidRequest, InvalidResponse, NoRouteFound, RouteResult, RoutingError,
                                       RoutingProvider, RoutingTimeout, RoutingUnavailable)
from app.services.routing.builder import BuildConfig, InvalidBuild, UnreachablePoint, generate
from app.services.routing.graphhopper import GraphHopperProvider, MAX_RESPONSE_BYTES
from tests.routing_fakes import FakeProvider

NS = {'g': 'http://www.topografix.com/GPX/1/1'}


# ===== A. L'adaptateur contre un serveur HTTP réel ==============================================================================

class Engine(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, script):
        super().__init__(('127.0.0.1', 0), _Handler)
        self.script, self.requests = script, []
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server_address[1]}'


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self.server.requests.append({'path': self.path, 'headers': dict(self.headers)})
        try:
            self.server.script(self, len(self.server.requests))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *args):
        pass


def reply(h, status, body=b'', headers=None, content_type='application/json'):
    h.send_response(status)
    h.send_header('Content-Type', content_type)
    h.send_header('Content-Length', str(len(body)))
    for key, value in (headers or {}).items():
        h.send_header(key, value)
    h.end_headers()
    h.wfile.write(body)


def route_body(coords=((3.05, 50.63), (3.10, 50.66), (3.05, 50.63)), distance=9000.0, extra=None):
    body = {'paths': [{'distance': distance, 'points': {'type': 'LineString', 'coordinates': [list(c) for c in coords]}}]}
    body.update(extra or {})
    return json.dumps(body).encode()


@contextmanager
def engine(script):
    server = Engine(script)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def provider_for(server, **kw):
    kw.setdefault('timeout_s', 2.0)
    if kw.get('api_key'):
        kw.setdefault('allow_cleartext_key', True)                  # serveur de test en http local (TR3-3 : choix explicite)
    return GraphHopperProvider(server.url, **{'guard': NO_GUARD, **kw})


def soon(seconds=5.0):
    return time.monotonic() + seconds


NO_GUARD = lambda destination: None   # noqa: E731 - la garde est obligatoire (TR3-2) ; ces tests n'exercent pas la liste blanche
WAYPOINTS = [(50.63, 3.05), (50.66, 3.10)]


def test_the_requests_sent_on_the_wire_are_exactly_the_documented_ones():
    with engine(lambda h, n: reply(h, 200, route_body())) as server:
        result = provider_for(server, api_key='k&e=y#1 2').route(WAYPOINTS, 'foot', soon())
        provider_for(server).round_trip((50.63, 3.05), 'hike', 12345.9, 7, soon())
    first, second = (parse_qs(urlsplit(r['path']).query) for r in server.requests)
    assert urlsplit(server.requests[0]['path']).path == '/route'
    assert first['point'] == ['50.630000,3.050000', '50.660000,3.100000'] and first['profile'] == ['foot']
    assert first['points_encoded'] == ['false'] and first['instructions'] == ['false'] and 'key' not in first        # clé en EN-TÊTE, jamais dans l'URL (TR3-3)
    assert second['algorithm'] == ['round_trip'] and second['round_trip.distance'] == ['12345'] and second['round_trip.seed'] == ['7']
    assert 'key' not in second and set(second) <= {'point', 'profile', 'algorithm', 'round_trip.distance', 'round_trip.seed',
                                                   'points_encoded', 'instructions', 'max_visited_nodes'}
    assert result.points[0] == (50.63, 3.05)                                              # lat, lon en sortie
    assert 'authorization' in {k.lower() for k in server.requests[0]['headers']}
    assert 'authorization' not in {k.lower() for k in server.requests[1]['headers']}


def test_a_base_url_with_a_path_prefix_keeps_it():
    with engine(lambda h, n: reply(h, 200, route_body())) as server:
        GraphHopperProvider(server.url + '/gh/', guard=NO_GUARD, timeout_s=2).route(WAYPOINTS, 'foot', soon())
    assert urlsplit(server.requests[0]['path']).path == '/gh/route'


@pytest.mark.parametrize('url', ['ftp://h:1', 'file:///etc/passwd', 'http://', 'http://u:p@h:1', 'h:1', '', 'http://a b:1',
                                 'http://h:99999', 'javascript:1', 'http://h:1\n'])
def test_the_constructor_refuses_unusable_urls(url):
    with pytest.raises((ValueError, outbound.OutboundError)):
        GraphHopperProvider(url, guard=NO_GUARD)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_redirects_are_never_followed_even_to_another_local_server(status):
    with engine(lambda h, n: reply(h, 200, b'{}')) as target:
        with engine(lambda h, n: reply(h, status, b'', {'Location': target.url + '/route'})) as server:
            with pytest.raises(InvalidResponse):
                provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert target.requests == []                                                           # RB-S3 : aucune requête vers la cible


def test_an_oversized_valid_body_is_cut_while_streaming_not_after_reading_it_all():
    """Le corps est du JSON VALIDE de 26 Mo : seule la borne de taille peut le refuser (un JSON illisible le serait de toute façon)."""
    unit = b'[3,50],'
    chunk = (unit * (65536 // len(unit)))[:65536 - 65536 % len(unit)]
    prefix = b'{"paths":[{"distance":1,"points":{"coordinates":['
    suffix = b'[3.1,50.1]]}}]}'
    sent = []

    def script(h, n):
        h.send_response(200)
        h.send_header('Content-Type', 'application/json')
        h.send_header('Transfer-Encoding', 'chunked')
        h.end_headers()
        pieces = [prefix] + [chunk] * 400 + [suffix]
        for piece in pieces:
            h.wfile.write(f'{len(piece):x}\r\n'.encode() + piece + b'\r\n')
            sent.append(len(piece))
        h.wfile.write(b'0\r\n\r\n')
    with engine(script) as server:
        started = time.monotonic()
        with pytest.raises(InvalidResponse):
            provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert time.monotonic() - started < 5
    assert sum(sent) < 20 * 1024 * 1024, "le client a continué à lire bien au-delà de la borne de 10 Mo"


def test_a_gzip_bomb_is_refused_on_the_decoded_size():
    bomb = gzip.compress(b'0' * (200 * 1024 * 1024), 9)                                    # ~200 Ko -> 200 Mo
    assert len(bomb) < 1024 * 1024
    with engine(lambda h, n: reply(h, 200, bomb, {'Content-Encoding': 'gzip'})) as server:
        started = time.monotonic()
        with pytest.raises(InvalidResponse):
            provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert time.monotonic() - started < 10


def test_a_body_trickled_slowly_cannot_outlive_the_deadline():
    def script(h, n):
        h.send_response(200)
        h.send_header('Content-Type', 'application/json')
        h.send_header('Transfer-Encoding', 'chunked')
        h.end_headers()
        for _ in range(30):                                                               # 6 s au total, un octet toutes les 0,2 s
            h.wfile.write(b'1\r\n ' + b'\r\n')
            h.wfile.flush()
            time.sleep(0.2)
    with engine(script) as server:
        started = time.monotonic()
        with pytest.raises(RoutingTimeout):
            provider_for(server, timeout_s=2.0).route(WAYPOINTS, 'foot', soon(1.0))
        assert time.monotonic() - started < 2.5


def test_a_server_that_never_answers_times_out_within_the_deadline():
    def script(h, n):
        time.sleep(4)
    with engine(script) as server:
        started = time.monotonic()
        with pytest.raises(RoutingTimeout):
            provider_for(server, timeout_s=10.0).route(WAYPOINTS, 'foot', soon(0.7))
    assert time.monotonic() - started < 2.0


def test_a_5xx_is_retried_once_and_never_more():
    with engine(lambda h, n: reply(h, 503, b'down')) as server:
        with pytest.raises(RoutingUnavailable):
            provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert len(server.requests) == 2
    with engine(lambda h, n: reply(h, 503, b'x') if n == 1 else reply(h, 200, route_body())) as server:
        assert provider_for(server).route(WAYPOINTS, 'foot', soon()).distance_m == 9000.0
    assert len(server.requests) == 2


@pytest.mark.parametrize('status', [400, 401, 403, 404, 405, 413, 414, 418, 422])
def test_a_client_error_is_never_retried_and_is_an_invalid_response(status):
    with engine(lambda h, n: reply(h, status, b'{"message": "x"}')) as server:
        with pytest.raises(InvalidResponse):
            provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert len(server.requests) == 1


def test_a_closed_port_is_unavailable_without_a_chained_cause():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(RoutingUnavailable) as exc:
        GraphHopperProvider(f'http://127.0.0.1:{port}', guard=NO_GUARD, timeout_s=1).route(WAYPOINTS, 'foot', soon(3))
    assert exc.value.__cause__ is None and (exc.value.__context__ is None or exc.value.__suppress_context__)
    assert str(port) not in str(exc.value) and '127.0.0.1' not in repr(exc.value) and exc.value.args == (exc.value.message,)


HOSTILE_200 = [
    b'', b'null', b'[]', b'"x"', b'{}', b'{"paths": []}', b'{"paths": {}}', b'{"paths": [null]}', b'{"paths": [[]]}',
    b'{"paths": [{"distance": 1}]}', b'{"paths": [{"points": {}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": "abc"}}]}',
    b'{"paths": [{"distance": 1, "points": "encodedpolyline"}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3, 50]]}}]}',                       # un seul point
    b'{"paths": [{"distance": NaN, "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": Infinity, "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": -5, "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": "9", "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": true, "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1e999, "points": {"coordinates": [[3, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[NaN, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3, Infinity], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3, 91], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[181, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [["3", "50"], ["3.1", "50.1"]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[true, false], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[null, null], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [{"a": 1, "b": 2}, [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": ["ab", "cd"]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3, 50, "high"], [3.1, 50.1, 2]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[3, 50, NaN], [3.1, 50.1, 2]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[1e400, 50], [3.1, 50.1]]}}]}',
    b'{"paths": [{"distance": 1, "points": {"coordinates": [[' + b'9' * 400 + b', 50], [3.1, 50.1]]}}]}',
    b'\xff\xfe\x00bad', b'{"paths": [', b'[' * 5000, b'{"a":' * 5000 + b'1' + b'}' * 5000,
]


@pytest.mark.parametrize('body', HOSTILE_200)
def test_a_hostile_200_body_is_an_invalid_response_never_a_crash_nor_a_wrong_trace(body):
    with engine(lambda h, n: reply(h, 200, body)) as server:
        with pytest.raises(InvalidResponse):
            provider_for(server).route(WAYPOINTS, 'foot', soon())
        with pytest.raises(InvalidResponse):
            provider_for(server).round_trip((50.63, 3.05), 'foot', 5000, 1, soon())


def test_a_huge_trace_is_decimated_to_the_file_limit_keeping_the_ends():
    from app.services.file_formats import MAX_FILE_POINTS
    coords = [(3.0 + i * 1e-6, 50.0 + i * 1e-6) for i in range(MAX_FILE_POINTS + 5000)]
    with engine(lambda h, n: reply(h, 200, route_body(coords))) as server:
        if len(route_body(coords)) > MAX_RESPONSE_BYTES:
            pytest.skip("trace plus grosse que la limite de réponse : refusée avant la décimation")
        result = provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert len(result.points) <= MAX_FILE_POINTS and result.points[0] == (50.0, 3.0)
    assert result.points[-1] == (50.0 + (MAX_FILE_POINTS + 4999) * 1e-6, 3.0 + (MAX_FILE_POINTS + 4999) * 1e-6)


def test_engine_warnings_and_text_never_reach_the_result_unclean():
    body = route_body(extra=None)
    payload = json.loads(body)
    payload['hints'] = {'warning': '<script>alert(1)</script>\x00\x1b[31m' + 'x' * 5000}
    with engine(lambda h, n: reply(h, 200, json.dumps(payload).encode())) as server:
        result = provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert all('\x00' not in w and '\x1b' not in w and len(w) <= 200 for w in result.warnings)


@pytest.mark.parametrize('hint,expected', [
    ({'details': 'com.graphhopper.util.exceptions.PointNotFoundException: Cannot find point 1: 50.0,3.0', 'point_index': 1}, 1),
    ({'details': 'PointNotFoundException', 'point_index': 0}, 0),
    ({'details': 'PointNotFoundException', 'point_index': -1}, None), ({'details': 'PointNotFoundException', 'point_index': True}, None),
    ({'details': 'PointNotFoundException', 'point_index': '1'}, None), ({'details': 'PointNotFoundException', 'point_index': 1.5}, None),
    ({'details': 'PointNotFoundException'}, None), ({'details': 'ConnectionNotFoundException'}, None),
])
def test_point_not_found_gives_no_route_found_with_a_safe_index_and_never_echoes_the_message(hint, expected):
    body = json.dumps({'message': 'Cannot find point 1: 50.63,3.05', 'hints': [hint]}).encode()
    with engine(lambda h, n: reply(h, 400, body)) as server:
        with pytest.raises(NoRouteFound) as exc:
            provider_for(server).route(WAYPOINTS, 'foot', soon())
    assert exc.value.waypoint_index == expected and '50.63' not in str(exc.value) and '50.63' not in repr(exc.value.args)


def _leak_run(caplog, with_400_echo=False):
    caplog.set_level(logging.DEBUG)
    secret = 'SECRET-API-KEY-123'
    scripts = [lambda h, n: reply(h, 503, b'x 50.63,3.05'), lambda h, n: reply(h, 200, b'{bad 50.63'),
               lambda h, n: reply(h, 302, b'', {'Location': 'http://x/?key=' + secret}),
               lambda h, n: reply(h, 418, b'50.63,3.05')]
    if with_400_echo:
        scripts = [lambda h, n: reply(h, 400, b'{"hints": [{"details": "Other 50.63,3.05 key=' + secret.encode() + b'"}]}')]
    messages = []
    for script in scripts:
        with engine(script) as server:
            with pytest.raises(RoutingError) as exc:
                provider_for(server, api_key=secret).route(WAYPOINTS, 'foot', soon())
            messages.append(str(exc.value) + repr(exc.value) + repr(exc.value.args))
            assert exc.value.__cause__ is None
    return secret, messages


def _blob(records):
    return '\n'.join(r.getMessage() + str(r.exc_info) for r in records)


FORBIDDEN = ('SECRET-API-KEY-123', '50.63', '3.05', '127.0.0.1', '/route', 'point=')


def test_no_url_coordinate_or_key_appears_in_error_messages_or_the_adapters_own_logs(caplog):
    secret, messages = _leak_run(caplog)
    own = [r for r in caplog.records if not r.name.startswith(('urllib3', 'requests'))]
    blob = '\n'.join(messages) + _blob(own)
    for forbidden in FORBIDDEN:
        assert forbidden not in blob, forbidden


def test_the_adapter_does_not_log_the_text_of_an_unexpected_400(caplog):
    _leak_run(caplog, with_400_echo=True)
    own = [r for r in caplog.records if not r.name.startswith(('urllib3', 'requests'))]
    assert '50.63' not in _blob(own)


def test_urllib3_debug_logs_do_not_leak_the_key_or_the_coordinates(caplog):
    _leak_run(caplog)
    blob = _blob([r for r in caplog.records if r.name.startswith(('urllib3', 'requests'))])
    for forbidden in ('SECRET-API-KEY-123', '50.63'):
        assert forbidden not in blob, forbidden


def test_the_guard_is_called_before_every_attempt_with_the_destination_only_and_a_refusal_sends_nothing():
    seen = []
    with engine(lambda h, n: reply(h, 503, b'x')) as server:
        with pytest.raises(RoutingUnavailable):
            provider_for(server, guard=lambda dest: seen.append(dest)).route(WAYPOINTS, 'foot', soon())
        assert len(seen) == len(server.requests) == 2                                      # une fois par tentative, reprise comprise
        assert all(d is not None for d in seen)
        assert '50.63' not in ' '.join(map(str, seen)) and 'point' not in ' '.join(map(str, seen))

    def refuse(dest):
        raise PermissionError('refusé')
    with engine(lambda h, n: reply(h, 200, route_body())) as server:
        with pytest.raises(RoutingUnavailable) as exc:
            provider_for(server, guard=refuse).route(WAYPOINTS, 'foot', soon())
        assert server.requests == [] and exc.value.__cause__ is None
        assert provider_for(server, guard=refuse).health() == Health.down() and server.requests == []


@pytest.mark.parametrize('call', [
    lambda p: p.route([(91, 3), (50, 3)], 'foot', soon()), lambda p: p.route([(50, 3)], 'foot', soon()),
    lambda p: p.route([(50, 3), (50.1, 3.1)], 'Foot!', soon()), lambda p: p.route([(50, 3), (50.1, 3.1)], 'foot', float('nan')),
    lambda p: p.route('ab', 'foot', soon()), lambda p: p.route([(50, 3)] * 201, 'foot', soon()),
    lambda p: p.round_trip((50, 3), 'foot', 0, 1, soon()), lambda p: p.round_trip((50, 3), 'foot', 2_000_000, 1, soon()),
    lambda p: p.round_trip((50, 3), 'foot', 5000, -1, soon()), lambda p: p.round_trip((50, 3), 'foot', 5000, 2 ** 31, soon()),
    lambda p: p.round_trip((50, 3), 'foot', float('nan'), 1, soon()), lambda p: p.round_trip((50, 3), 'foot', 5000, True, soon()),
])
def test_invalid_input_never_reaches_the_wire(call):
    with engine(lambda h, n: reply(h, 200, route_body())) as server:
        with pytest.raises(InvalidRequest):
            call(provider_for(server))
    assert server.requests == []


def test_an_expired_deadline_sends_nothing():
    with engine(lambda h, n: reply(h, 200, route_body())) as server:
        with pytest.raises(RoutingTimeout):
            provider_for(server).route(WAYPOINTS, 'foot', time.monotonic() - 1)
    assert server.requests == []


def test_health_never_raises_and_is_bounded_whatever_the_engine_does():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    started = time.monotonic()
    assert GraphHopperProvider(f'http://127.0.0.1:{port}', guard=NO_GUARD, timeout_s=1).health() == Health.down()
    assert time.monotonic() - started < 3
    for script in (lambda h, n: reply(h, 500, b'x'), lambda h, n: reply(h, 302, b'', {'Location': 'http://127.0.0.1:1/'}),
                   lambda h, n: reply(h, 200, b'not json') if n > 1 else reply(h, 200, b'OK')):
        with engine(script) as server:
            h = provider_for(server).health()
            assert isinstance(h, Health)


def test_a_slow_engine_makes_health_return_within_its_own_bound():
    def script(h, n):
        time.sleep(8)
    with engine(script) as server:
        started = time.monotonic()
        assert provider_for(server, timeout_s=30).health() == Health.down()
        assert time.monotonic() - started < 5                      # HEALTH_TIMEOUT_S = 3 s, pas le délai d'appel de 30 s


def test_health_reads_the_extent_in_lat_lon_order_and_ignores_the_declared_extent_for_availability():
    info = {'bbox': [-8.32, 48.83, 6.42, 54.60], 'profiles': [{'name': 'foot'}, {'name': 'bike'}]}
    with engine(lambda h, n: reply(h, 200, b'OK') if h.path.startswith('/health') else reply(h, 200, json.dumps(info).encode())) as server:
        health = provider_for(server).health()
    assert health.available and health.bbox == (48.83, -8.32, 54.60, 6.42) and health.profiles == ('foot', 'bike')


@pytest.mark.parametrize('profiles', [[{'name': 'mtb-fast'}], [{'name': 'Hike'}], [{'name': f'p{i}'} for i in range(25)]])
def test_a_healthy_engine_with_unusual_profile_names_stays_available(profiles):
    info = {'bbox': [-8.32, 48.83, 6.42, 54.60], 'profiles': profiles}
    with engine(lambda h, n: reply(h, 200, b'OK') if h.path.startswith('/health') else reply(h, 200, json.dumps(info).encode())) as server:
        assert provider_for(server).health().available


# ===== B. Le builder ============================================================================================================

COVERAGE = (49.0, 2.0, 51.6, 6.5)
CFG = BuildConfig(coverage_bbox=COVERAGE)
TODAY = None


class Flaky(FakeProvider):
    """Fake qui échoue au hasard (graine fixe) : panne moteur, point non approchable, délai, réponse invalide."""

    def __init__(self, rng, rate, **kw):
        super().__init__(**kw)
        self._rng, self._rate = rng, rate

    def _enter(self, name, deadline, *args):
        super()._enter(name, deadline, *args)
        if self._rng.random() < self._rate:
            kind = self._rng.choice(('unavailable', 'invalid', 'nofound', 'timeout', 'nofound_far'))
            if kind == 'unavailable':
                raise RoutingUnavailable()
            if kind == 'invalid':
                raise InvalidResponse()
            if kind == 'timeout':
                raise RoutingTimeout()
            raise NoRouteFound(self._rng.randrange(0, 12) if kind == 'nofound' else 10 ** 6)


def random_case(rng, extreme=False):
    n = rng.choice((1, 1, 2, 2, 3, 4, 5, 8))
    if extreme:
        base = rng.choice(((89.9, 179.9), (-89.9, -179.9), (0.0, 0.0), (89.99, 0.0), (0.0, 179.99), (50.6, 3.1)))
        spread = rng.choice((0.0001, 0.01, 0.2))
        points = [(max(-90, min(90, base[0] + rng.uniform(-spread, spread))), max(-180, min(180, base[1] + rng.uniform(-spread, spread))))
                  for _ in range(n)]
    else:
        points = [(rng.uniform(49.2, 51.4), rng.uniform(2.2, 6.3)) for _ in range(n)] if rng.random() < 0.5 else \
                 [(50.63 + rng.uniform(-0.05, 0.05), 3.06 + rng.uniform(-0.08, 0.08)) for _ in range(n)]
    if rng.random() < 0.15 and n > 1:
        points[rng.randrange(1, n)] = points[rng.randrange(0, n)]                    # doublons exacts
    if rng.random() < 0.1 and n > 1:
        points[-1] = (points[0][0] + 1e-5, points[0][1] + 1e-5)                      # dernier point confondu avec le premier
    target = None if (n > 1 and rng.random() < 0.25) else rng.choice((1.0, 3.0, 8.0, 15.0, 30.0, 80.0, 150.0, 300.0, rng.uniform(1, 300)))
    return points, rng.choice(('run', 'hike', 'bike', 'road_bike', 'gravel', 'mtb', 'walk')), target, rng.randrange(0, 2 ** 31)


def check_generation(gen, provider, config):
    assert len(gen.points) >= 2
    root = ET.fromstring(gen.gpx)
    waypoints = root.findall('g:wpt', NS)
    assert len(waypoints) == gen.meta['points'] and len(root.findall('g:trk', NS)) == 1 and len(root.findall('.//g:trkseg', NS)) == 1
    assert root.find('.//g:time', NS) is None and root.find('.//g:metadata', NS) is None
    assert GPXParser.parse_gpx_coordinates(gen.gpx.decode('utf-8'))               # lisible par le parseur de l'application


def test_two_hundred_random_generations_keep_every_invariant_even_with_random_engine_failures():
    rng = random.Random(20261124)
    documented = (InvalidBuild, UnreachablePoint, RoutingError)
    outcomes = {'ok': 0, 'invalid': 0, 'engine': 0}
    for case in range(200):
        points, activity, target, seed = random_case(rng)
        cfg = BuildConfig(coverage_bbox=COVERAGE, max_calls=rng.choice((1, 2, 3, 5, 15)), seed_retries=rng.choice((0, 1, 2)),
                          detour_strategy=rng.choice(('s1', 's2', 'hybrid')), tolerance_pct=rng.choice((5.0, 15.0, 30.0)),
                          total_budget_s=rng.choice((5.0, 30.0)))
        provider = Flaky(rng, rng.choice((0.0, 0.0, 0.1, 0.3)), sinuosity=rng.choice((0.0, 0.05, 0.2)))
        try:
            gen = generate(provider, points, activity, target, seed, cfg)
        except InvalidBuild:
            outcomes['invalid'] += 1
            assert provider.calls == [] or points and False, "InvalidBuild APRÈS un appel au moteur"
            continue
        except documented as exc:
            outcomes['engine'] += 1
            if isinstance(exc, UnreachablePoint):
                assert 1 <= exc.number <= len(points), (case, exc.number, len(points))
            continue
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"cas {case}: exception non documentée {type(exc).__name__}: {exc}")
        outcomes['ok'] += 1
        assert len(provider.calls) <= gen.meta['calls'] <= cfg.max_calls, (case, len(provider.calls), gen.meta['calls'])
        assert gen.meta['attempts'] <= 1 + cfg.seed_retries and gen.meta['seed'] == seed
        check_generation(gen, provider, cfg)
        assert gen.meta['point_numbers'] == sorted(gen.meta['point_numbers']) and gen.meta['point_numbers'][0] == 1
        for kind, *rest in provider.calls:                                              # points techniques : dans la couverture
            if kind == 'route':
                for wp in rest[0]:
                    assert COVERAGE[0] - 1e-9 <= wp[0] <= COVERAGE[2] + 1e-9 and COVERAGE[1] - 1e-9 <= wp[1] <= COVERAGE[3] + 1e-9, (case, wp)
    assert outcomes['ok'] > 80 and outcomes['engine'] > 5, outcomes


def test_extreme_coordinates_poles_and_antimeridian_never_crash_the_builder():
    rng = random.Random(7)
    documented = (InvalidBuild, UnreachablePoint, RoutingError)
    for case in range(120):
        points, activity, target, seed = random_case(rng, extreme=True)
        try:
            gen = generate(FakeProvider(sinuosity=rng.choice((0.0, 0.1))), points, activity, target, seed, BuildConfig())
        except documented:
            continue
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"cas {case} {points}: {type(exc).__name__}: {exc}")
        assert all(-90 <= la <= 90 and -180 <= lo <= 180 and math.isfinite(la) and math.isfinite(lo) for la, lo in gen.points)
        ET.fromstring(gen.gpx)


def test_the_same_inputs_give_byte_identical_gpx_and_meta_for_random_cases():
    rng = random.Random(99)
    from datetime import datetime
    today = datetime(2026, 11, 24)
    for _ in range(25):
        points, activity, target, seed = random_case(rng)
        try:
            a = generate(FakeProvider(sinuosity=0.1), points, activity, target, seed, CFG, today=today)
            b = generate(FakeProvider(sinuosity=0.1), points, activity, target, seed, CFG, today=today)
        except (InvalidBuild, RoutingError):
            continue
        assert a.gpx == b.gpx and a.meta == b.meta


def test_the_seed_ceiling_does_not_break_retries():
    """Graine maximale : les essais suivants dépassent 2^31-1 ; la trace déjà obtenue doit rester rendue, sans exception."""
    class Stubborn1(FakeProvider):
        def round_trip(self, start, profile, distance_m, seed, deadline):
            return super().round_trip(start, profile, distance_m * 0.3, seed, deadline)      # toujours trop courte
    gen = generate(Stubborn1(), [(50.63, 3.06)], 'run', 20.0, 2 ** 31 - 1, CFG)
    assert gen.meta['attempts'] >= 1 and len(gen.points) > 10


def test_the_engine_warnings_reach_route_meta_RB49():
    """RB-4.9 : route_meta porte les avertissements du moteur."""
    class Warns(FakeProvider):
        def route(self, waypoints, profile, deadline):
            r = super().route(waypoints, profile, deadline)
            return RouteResult(points=r.points, distance_m=r.distance_m, warnings=('Tronçon hors réseau pédestre',))
    gen = generate(Warns(), [(50.63, 3.05), (50.66, 3.10)], 'run', None, 1, CFG)
    assert any('hors réseau' in w for w in gen.meta['warnings']) or any('hors réseau' in w['message'] for w in gen.warnings)


class IndexLiar(FakeProvider):
    """Moteur qui accuse un point d'index absurde."""

    def __init__(self, index):
        super().__init__()
        self.index = index

    def route(self, waypoints, profile, deadline):
        self._enter('route', deadline, waypoints, profile)
        raise NoRouteFound(self.index)


@pytest.mark.parametrize('index', [0, 1, 2, 3])
def test_an_unreachable_point_is_blamed_with_its_original_number_closing_point_included(index):
    points = [(50.63, 3.05), (50.66, 3.10), (50.64, 3.15)]                # 3 points : la liste envoyée en compte 4 (fermeture)
    with pytest.raises(UnreachablePoint) as exc:
        generate(IndexLiar(index), points, 'run', None, 1, CFG)
    assert exc.value.number == [1, 2, 3, 1][index]


def test_an_out_of_range_engine_index_does_not_blame_an_innocent_point():
    with pytest.raises(NoRouteFound) as exc:
        generate(IndexLiar(10 ** 6), [(50.63, 3.05), (50.66, 3.10), (50.64, 3.15)], 'run', None, 1, CFG)
    assert not isinstance(exc.value, UnreachablePoint)


def test_a_single_point_that_cannot_be_approached_is_reported_with_its_number():
    with pytest.raises(UnreachablePoint) as exc:
        generate(FakeProvider(unroutable=lambda p: True), [(50.63, 3.06)], 'run', 10.0, 1, CFG)
    assert exc.value.number == 1


def test_a_provider_returning_a_trace_far_from_the_points_is_an_unreachable_point_not_a_trace():
    class Elsewhere(FakeProvider):
        def route(self, waypoints, profile, deadline):
            self._enter('route', deadline, waypoints, profile)
            return RouteResult(points=[(50.0, 5.0), (50.01, 5.01), (50.0, 5.0)], distance_m=2500.0)
    with pytest.raises(UnreachablePoint):
        generate(Elsewhere(), [(50.63, 3.05), (50.66, 3.10)], 'run', None, 1, CFG)


def test_a_provider_that_ignores_the_closing_point_is_not_returned_as_a_closed_loop():
    """Le moteur doit finir au point de départ ; une base qui ne se referme pas n'est pas une boucle (CA-R4 : la trace finit où elle commence)."""
    class Open(FakeProvider):
        def route(self, waypoints, profile, deadline):
            self._enter('route', deadline, waypoints, profile)
            r = FakeProvider.route(self, list(waypoints)[:-1], profile, deadline + 100)       # sans le retour au départ
            return r
    try:
        gen = generate(Open(), [(50.63, 3.05), (50.66, 3.10), (50.64, 3.15)], 'run', None, 1, CFG)
    except (RoutingError, InvalidBuild):
        return
    assert quality.closure_m(gen.points) <= CFG.snap_tolerance_m, quality.closure_m(gen.points)


def test_a_target_just_inside_the_tolerance_edges_is_a_single_call_and_just_outside_triggers_work():
    points = [(50.63, 3.05), (50.66, 3.10)]
    d0 = generate(FakeProvider(sinuosity=0.0), points, 'run', None, 1, CFG).meta['distance_m']
    for target_m in (d0 / 1.15 * (1 + 1e-6), d0 / 0.85 * (1 - 1e-6)):
        provider = FakeProvider(sinuosity=0.0)
        gen = generate(provider, points, 'run', target_m / 1000, 1, CFG)
        assert len(provider.calls) == 1 and gen.meta['detours'] == 0
        assert not any(w['code'] in ('min_distance', 'target_gap') for w in gen.warnings)
    short = FakeProvider(sinuosity=0.0)                                         # cible 2 % au-delà de la borne basse : des détours
    generate(short, points, 'run', d0 / 0.85 * 1.02 / 1000, 1, CFG)
    assert len(short.calls) > 1
    long = FakeProvider(sinuosity=0.0)                                          # cible 2 % en deçà de la borne haute : avertissement, 1 appel
    gen = generate(long, points, 'run', d0 / 1.15 * 0.98 / 1000, 1, CFG)
    assert len(long.calls) == 1 and any(w['code'] == 'min_distance' for w in gen.warnings)


def test_never_more_than_max_calls_and_never_more_calls_than_counted_with_a_failing_engine():
    rng = random.Random(3)
    for max_calls in (1, 2, 3, 4, 6, 15):
        for rate in (0.0, 0.5, 1.0):
            provider = Flaky(rng, rate, sinuosity=0.1)
            cfg = BuildConfig(coverage_bbox=COVERAGE, max_calls=max_calls)
            try:
                generate(provider, [(50.63, 3.05), (50.66, 3.12), (50.60, 3.10)], 'run', 40.0, 5, cfg)
            except (InvalidBuild, RoutingError):
                pass
            assert len(provider.calls) <= max_calls


def test_the_time_budget_is_never_exceeded_with_a_slow_provider_and_a_fake_clock():
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s
    provider = FakeProvider(sinuosity=0.1, delay_s=7.0, sleep=sleep, clock=lambda: now[0])
    cfg = BuildConfig(coverage_bbox=COVERAGE, total_budget_s=20.0)
    try:
        generate(provider, [(50.63, 3.05), (50.66, 3.12)], 'run', 40.0, 5, cfg, clock=lambda: now[0])
    except RoutingError:
        pass
    assert now[0] <= 20.0 + 1e-9 and len(provider.calls) <= 3


def test_the_progress_of_a_generation_is_independent_between_threads():
    """Aucun état partagé : deux générations simultanées (autres graines) donnent les mêmes traces que séquentiellement."""
    from datetime import datetime
    today = datetime(2026, 11, 24)
    inputs = [([(50.63, 3.05), (50.66, 3.10), (50.64, 3.15)], 'run', 30.0, s) for s in range(6)]
    expected = [generate(FakeProvider(sinuosity=0.1), p, a, t, s, CFG, today=today).gpx for p, a, t, s in inputs]
    results = [None] * len(inputs)

    def run(i):
        p, a, t, s = inputs[i]
        results[i] = generate(FakeProvider(sinuosity=0.1), p, a, t, s, CFG, today=today).gpx
    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(inputs))]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert results == expected


def test_the_provider_base_class_cannot_be_used_without_its_three_methods():
    with pytest.raises(TypeError):
        RoutingProvider()
