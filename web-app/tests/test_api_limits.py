"""Bornes de l'API : taille des corps, volume par requête, traitements simultanés, entrées mal formées."""

import io
import json
import threading
import zipfile

import pytest

from app.api import editor_routes, payload, uploads, workload
from app.services import file_formats
from tests.conftest import line, make_gpx

HUGE = 10 ** 30  # au-delà de ce qu'une colonne entière SQLite sait stocker


def _files(*items):
    return {'files': [(io.BytesIO(raw if isinstance(raw, bytes) else raw.encode()), name) for raw, name in items]}


def _upload(client, raw, name):
    return client.post('/api/upload', data={'file': (io.BytesIO(raw), name)}, content_type='multipart/form-data')


def _error(response):
    body = response.get_json()
    assert body['success'] is False
    return body['error']


# --- Taille des corps JSON -------------------------------------------------------------------

LARGE_ROUTES = ['/api/analysis/trajectory', '/api/analysis/advanced', '/api/export/gpx']
SMALL_ROUTES = ['/api/analysis/distance', '/api/analysis/elevation-profile', '/api/analysis/stops',
                '/api/analysis/terrain', '/api/editor/simplify-trace', '/api/segments']


def _padded(size):
    return json.dumps({'features': [], 'points': [], 'coordinates': [], 'padding': 'x' * size})


@pytest.mark.parametrize('route', SMALL_ROUTES)
def test_json_routes_refuse_bodies_over_the_low_limit(client, route):
    response = client.post(route, data=_padded(payload.JSON_BODY_LIMIT), content_type='application/json')
    assert response.status_code == 413 and 'volumineuse' in _error(response)


@pytest.mark.parametrize('route', LARGE_ROUTES)
def test_whole_trace_routes_keep_the_upload_limit(client, app, route):
    """Le visualiseur y envoie une trace entière : jusqu'à MAX_CONTENT_LENGTH, pas au-delà."""
    assert client.post(route, data=_padded(3 * payload.JSON_BODY_LIMIT),
                       content_type='application/json').status_code == 200
    too_big = client.post(route, data=_padded(app.config['MAX_CONTENT_LENGTH']), content_type='application/json')
    assert too_big.status_code == 413


def test_body_without_content_length_is_still_bounded(client):
    """Corps annoncé en flux (chunked) : pas de Content-Length à comparer, la lecture s'arrête à la limite."""
    body = _padded(payload.JSON_BODY_LIMIT).encode()
    response = client.post('/api/analysis/distance', input_stream=io.BytesIO(body), content_type='application/json',
                           environ_overrides={'wsgi.input_terminated': True})
    assert response.status_code == 413


# --- Entrées mal formées : jamais de 500 -------------------------------------------------------

@pytest.mark.parametrize('method, path', [
    ('PATCH', '/api/activities/1'), ('POST', '/api/segments'), ('PATCH', '/api/users/1'),
    ('POST', '/api/analysis/distance'), ('POST', '/api/editor/simplify-trace'), ('POST', '/api/export/csv'),
])
@pytest.mark.parametrize('body', ['[1]', '"texte"', '12', 'null', '{pas du json'])
def test_non_object_json_body_is_a_400(client, method, path, body):
    client.get('/api/me')  # provisionne le compte local (cible de /api/users/1)
    response = client.open(path, method=method, data=body, content_type='application/json')
    assert response.status_code == 400 and _error(response)


def test_deeply_nested_json_is_a_400(client):
    body = '{"features":' + '[' * 100_000 + ']' * 100_000 + '}'
    response = client.post('/api/analysis/trajectory', data=body, content_type='application/json')
    assert response.status_code == 400 and _error(response)


@pytest.mark.parametrize('method, path', [
    ('GET', f'/api/activities/{HUGE}'), ('DELETE', f'/api/activities/{HUGE}'), ('GET', f'/api/activities/{HUGE}/track'),
    ('POST', f'/api/activities/{HUGE}/analysis'), ('GET', f'/api/segments/{HUGE}'), ('DELETE', f'/api/segments/{HUGE}'),
    ('DELETE', f'/api/users/{HUGE}'),
])
def test_identifier_beyond_sqlite_range_matches_no_route(client, method, path):
    """404, ou 405 quand Werkzeug voit que le chemin existe pour une autre méthode : jamais d'erreur SQL."""
    response = client.open(path, method=method)
    assert response.status_code in (404, 405) and _error(response)


def test_out_of_range_numbers_in_query_and_body_are_handled(client):
    assert client.get(f'/api/activities?offset={HUGE}&limit={HUGE}').status_code == 200
    assert client.get('/api/activities?offset=-5&limit=abc').status_code == 200
    assert client.get(f'/api/activities/1/track?max_points={HUGE}').status_code == 404
    for body in ({'activity_id': HUGE, 'start_idx': 0, 'end_idx': 5}, {'activity_id': 1, 'start_idx': -1, 'end_idx': 5},
                 {'activity_id': 1, 'start_idx': 0, 'end_idx': HUGE}, {'activity_id': True, 'start_idx': 0, 'end_idx': 5},
                 {'activity_id': '1', 'start_idx': 0, 'end_idx': 5}):
        response = client.post('/api/segments', json={'name': 'Col', **body})
        assert response.status_code in (400, 404), body
    assert client.post('/api/segments', json={'name': ['x'], 'activity_id': 1, 'start_idx': 0, 'end_idx': 5}).status_code == 400
    assert client.patch('/api/activities/1', json={'name': {'a': 1}}).status_code == 400


@pytest.mark.parametrize('coordinates', [['a', 'b', 'c'], [[1, 2], [1, 'x']], [[1, 2], [None, 2]], [[1, 2], {}], [[1], [2]]])
def test_distance_survives_non_numeric_points(client, coordinates):
    """Refus (400) ou points ignorés par le service (200, distance finie) : jamais d'erreur interne."""
    response = client.post('/api/analysis/distance', json={'coordinates': coordinates})
    if response.status_code == 400:
        assert _error(response) == 'Coordonnées invalides'
    else:
        assert response.status_code == 200 and response.get_json()['distance']['total_distance_m'] >= 0


def test_distance_refuses_what_the_service_cannot_compute(client, monkeypatch):
    from app.api import analysis_routes

    def boom(coordinates):
        raise ValueError("could not convert string to float: 'x'")
    monkeypatch.setattr(analysis_routes.TrajectoryAnalyzer, 'calculate_total_distance', staticmethod(boom))
    response = client.post('/api/analysis/distance', json={'coordinates': [[1, 2], [3, 4]]})
    assert response.status_code == 400 and _error(response) == 'Coordonnées invalides'


# --- Simplification ----------------------------------------------------------------------------

def test_simplify_refuses_bad_tolerance_and_too_many_points(client, monkeypatch):
    track = [[45 + i * 1e-4, 6.0] for i in range(10)]
    assert client.post('/api/editor/simplify-trace', json={'coordinates': track, 'tolerance': 1e-4}).status_code == 200
    for tolerance in ('x', [1], None, 0, 1e-12, editor_routes.MIN_TOLERANCE / 2, 2):
        response = client.post('/api/editor/simplify-trace', json={'coordinates': track, 'tolerance': tolerance})
        assert response.status_code == 400, tolerance
    for bad in ([[1, 2], [3, 4], 'ab'], [[1, 2], [3, 4], [float('inf'), 1]]):
        raw = json.dumps({'coordinates': bad}).replace('Infinity', '1e999')
        assert client.post('/api/editor/simplify-trace', data=raw, content_type='application/json').status_code == 400

    monkeypatch.setattr(editor_routes, 'MAX_SIMPLIFY_POINTS', 9)
    response = client.post('/api/editor/simplify-trace', json={'coordinates': track})
    assert response.status_code == 400 and 'trop longue' in _error(response)


def test_simplify_worst_case_stays_within_the_point_cap():
    """Le plafond tient dans le corps JSON accepté : la limite de points est bien celle qui s'applique."""
    body = json.dumps({'coordinates': [[45.123456, 6.123456, 1234.5]] * editor_routes.MAX_SIMPLIFY_POINTS})
    assert len(body) < payload.JSON_BODY_LIMIT


# --- Volume par requête --------------------------------------------------------------------------

def _kmz(count, tag):
    placemarks = f"<Placemark><name>{tag}</name><Point><coordinates>6.1,45.1,1</coordinates></Point></Placemark>" * count
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('doc.kml', f"<kml xmlns='http://www.opengis.net/kml/2.2'><Document>{placemarks}</Document></kml>")
    return buffer.getvalue()


def test_multi_upload_is_bounded_by_a_point_budget(client, monkeypatch):
    """Chaque fichier respecte le plafond par fichier ; ensemble, ils ne dépassent pas le budget de la requête."""
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 40)
    assert uploads.request_point_budget() == 40
    items = [(_kmz(40, f'f{i}'), f'{i}.kmz') for i in range(4)]
    assert all(len(raw) < 2000 for raw, _ in items)
    response = client.post('/api/multi-upload', data=_files(*items), content_type='multipart/form-data')
    results = response.get_json()['files']
    assert [r['success'] for r in results] == [True, False, False, False]
    assert all('trop volumineux' in r['error'] and 'features' not in r for r in results[1:])
    assert sum(len(r.get('features', [])) for r in results) == 40


def test_gpx_points_are_counted_once(client):
    """Un GPX décrit chaque point comme sommet de trace et comme repère : il ne compte qu'une fois."""
    parsed = _upload(client, make_gpx(line(30)).encode(), 'a.gpx').get_json()
    assert uploads.count_points(parsed) == 30


# --- Traitements simultanés ------------------------------------------------------------------------

def test_heavy_routes_answer_503_when_every_slot_is_taken(client, monkeypatch):
    monkeypatch.setattr(workload, '_slots', threading.BoundedSemaphore(1))
    monkeypatch.setattr(workload, 'SLOT_WAIT_S', 0.05)
    gpx = make_gpx(line(30)).encode()
    track = [[45 + i * 1e-4, 6.0] for i in range(10)]
    with workload.heavy_slot():  # un traitement lourd est en cours
        busy = [
            _upload(client, gpx, 'a.gpx'),
            client.post('/api/multi-upload', data=_files((gpx, 'a.gpx')), content_type='multipart/form-data'),
            client.post('/api/analysis/upload-and-analyze', data={'file': (io.BytesIO(gpx), 'a.gpx')},
                        content_type='multipart/form-data'),
            client.post('/api/analysis/trajectory', json={'features': []}),
            client.post('/api/analysis/advanced', json={'features': []}),
            client.post('/api/export/gpx', json={'features': []}),
            client.post('/api/editor/simplify-trace', json={'coordinates': track}),
        ]
        assert [r.status_code for r in busy] == [503] * len(busy)
        assert 'occupé' in _error(busy[0])
        # Les requêtes légères ne sont pas concernées
        assert client.get('/api/activities').status_code == 200
        # L'import répond fichier par fichier : ce qui a pu être importé avant la saturation le reste
        imported = client.post('/api/activities', data=_files((gpx, 'a.gpx'), (gpx, 'b.gpx')),
                               content_type='multipart/form-data').get_json()
        assert imported['success'] is False
        assert [(r['status'], 'occupé' in r['message']) for r in imported['results']] == [('error', True)] * 2
    # La place rendue, tout repasse (y compris après une erreur dans le bloc)
    assert _upload(client, gpx, 'a.gpx').status_code == 200
    assert _upload(client, b'<gpx', 'cassé.gpx').status_code == 400
    assert _upload(client, gpx, 'a.gpx').status_code == 200


def test_concurrent_parsings_never_exceed_the_slots(app, monkeypatch):
    monkeypatch.setattr(workload, '_slots', threading.BoundedSemaphore(2))
    running, peak, lock, release = [0], [0], threading.Lock(), threading.Event()

    def slow_parse(content):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        release.wait(2)
        with lock:
            running[0] -= 1
        return {'success': True, 'features': [], 'points': []}

    monkeypatch.setattr(uploads, 'parse_gpx_cached', slow_parse)
    threads = [threading.Thread(target=lambda: _upload(app.test_client(), b'<gpx/>', 'a.gpx')) for _ in range(6)]
    for thread in threads:
        thread.start()
    threading.Event().wait(0.3)
    release.set()
    for thread in threads:
        thread.join(10)
    assert peak[0] == 2


# --- Messages d'erreur -----------------------------------------------------------------------------

XXE = (b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
       b'<kml><Document><name>&x;</name></Document></kml>')


@pytest.mark.parametrize('raw, name', [(b'<gpx', 'a.gpx'), (b'<kml><Document>', 'a.kml'), (XXE, 'a.kml'),
                                       (XXE.replace(b'kml', b'gpx'), 'a.gpx')])
def test_parser_exception_text_never_reaches_the_client(client, raw, name):
    answers = [
        _error(_upload(client, raw, name)),
        client.post('/api/multi-upload', data=_files((raw, name)),
                    content_type='multipart/form-data').get_json()['files'][0]['error'],
        _error(client.post('/api/analysis/upload-and-analyze', data={'file': (io.BytesIO(raw), name)},
                           content_type='multipart/form-data')),
        client.post('/api/activities', data=_files((raw, name)),
                    content_type='multipart/form-data').get_json()['results'][0]['message'],
    ]
    for answer in answers:
        assert answer and len(answer) <= uploads.MAX_ERROR_LENGTH
        for leak in ('passwd', 'EntitiesForbidden', 'line 1', 'column', 'token', 'Traceback'):
            assert leak not in answer, answer


def test_user_facing_refusals_are_kept_as_written(client):
    assert 'pris en charge' in _error(_upload(client, b'x', 'a.txt'))
    assert uploads.public_error('Quota de stockage atteint : supprime des activités') \
        == 'Quota de stockage atteint : supprime des activités'
    assert uploads.public_error("Erreur lors du traitement: could not convert string to float: 'x'") \
        == 'Erreur lors du traitement'
    assert uploads.public_error(None) == uploads.UNREADABLE_FILE


def test_dashboard_and_segment_errors_are_fixed_messages(client):
    response = client.get('/api/dashboard?period=<script>')
    assert response.status_code == 400 and '<script>' not in _error(response)
    data = {'files': [(io.BytesIO(make_gpx(line(40)).encode()), 'a.gpx')]}
    activity_id = client.post('/api/activities', data=data,
                              content_type='multipart/form-data').get_json()['results'][0]['activity_id']
    response = client.post('/api/segments', json={'name': 'Col', 'activity_id': activity_id, 'start_idx': 5, 'end_idx': 2})
    assert response.status_code == 400 and _error(response).startswith('Tronçon invalide')
