from io import BytesIO

from tests.conftest import line, login_as, make_gpx


def _upload(client, *payloads):
    data = {'files': [(BytesIO(p.encode()), f'trace{i}.gpx') for i, p in enumerate(payloads)]}
    return client.post('/api/activities', data=data, content_type='multipart/form-data')


def test_upload_list_dashboard(client):
    response = _upload(client, make_gpx(line(101)), make_gpx(line(60), name='Autre'))
    assert response.status_code == 200
    assert [r['status'] for r in response.get_json()['results']] == ['created', 'created']

    listing = client.get('/api/activities').get_json()
    assert len(listing['activities']) == 2 and listing['types'] == ['running']
    assert 'points' not in listing['activities'][0] and 'sha256' not in listing['activities'][0]

    dash = client.get('/api/dashboard?period=year').get_json()
    assert dash['stats']['totals']['count'] == 2
    assert dash['stats']['totals']['distance_km'] > 5
    assert any(s['period'] == '2025' and s['count'] == 2 for s in dash['stats']['series'])
    assert dash['records'][0]['label'] == 'Plus longue distance'
    assert client.get('/api/dashboard?period=decade').status_code == 400
    for period in ('week', 'month'):
        assert len(client.get(f'/api/dashboard?period={period}').get_json()['stats']['series']) == 12


def test_activity_parsed_rename_delete(client):
    activity_id = _upload(client, make_gpx(line(30))).get_json()['results'][0]['activity_id']
    parsed = client.get(f'/api/activities/{activity_id}/parsed').get_json()
    assert parsed['success'] and parsed['features']
    assert client.patch(f'/api/activities/{activity_id}', json={'name': 'Renommée'}).status_code == 200
    assert client.get(f'/api/activities/{activity_id}').get_json()['activity']['name'] == 'Renommée'
    assert client.patch(f'/api/activities/{activity_id}', json={'name': ''}).status_code == 400
    assert client.delete(f'/api/activities/{activity_id}').status_code == 200
    assert client.get(f'/api/activities/{activity_id}').status_code == 404


def test_pages_render(client):
    for path in ('/', '/dashboard', '/segments'):
        assert client.get(path).status_code == 200


def test_segment_api_validation(client):
    activity_id = _upload(client, make_gpx(line(100))).get_json()['results'][0]['activity_id']
    post = lambda **body: client.post('/api/segments', json=body)  # noqa: E731
    assert post(name='S', activity_id=activity_id, start_idx=10, end_idx=60).status_code == 201
    assert post(name='S', activity_id=activity_id, start_idx=60, end_idx=10).status_code == 400
    assert post(name='S', activity_id=activity_id, start_idx=0, end_idx=1).status_code == 400  # trop court
    assert post(name='S', activity_id=activity_id, start_idx='0', end_idx=50).status_code == 400
    assert post(name='', activity_id=activity_id, start_idx=10, end_idx=60).status_code == 400
    assert post(name='S', activity_id=9999, start_idx=10, end_idx=60).status_code == 404


def test_users_are_isolated(oidc_app):
    alice, _ = login_as(oidc_app, 'alice')
    bob, _ = login_as(oidc_app, 'bob')
    activity_id = _upload(alice, make_gpx(line(100))).get_json()['results'][0]['activity_id']
    segment_id = alice.post('/api/segments', json={
        'name': 'À moi', 'activity_id': activity_id, 'start_idx': 10, 'end_idx': 60}).get_json()['segment']['id']

    assert bob.get('/api/activities').get_json()['activities'] == []
    assert bob.get('/api/segments').get_json()['segments'] == []
    assert bob.get('/api/dashboard').get_json()['stats']['totals']['count'] == 0
    for method, path in [
        ('get', f'/api/activities/{activity_id}'), ('get', f'/api/activities/{activity_id}/track'),
        ('get', f'/api/activities/{activity_id}/parsed'), ('delete', f'/api/activities/{activity_id}'),
        ('patch', f'/api/activities/{activity_id}'), ('post', f'/api/activities/{activity_id}/elevation'),
        ('get', f'/api/segments/{segment_id}'), ('delete', f'/api/segments/{segment_id}'),
        ('post', f'/api/segments/{segment_id}/rescan'),
    ]:
        kwargs = {'json': {'name': 'vol'}} if method == 'patch' else {}
        assert getattr(bob, method)(path, **kwargs).status_code == 404, (method, path)
    # Bob ne peut pas bâtir un tronçon sur la trace d'Alice
    assert bob.post('/api/segments', json={'name': 'x', 'activity_id': activity_id,
                                           'start_idx': 10, 'end_idx': 60}).status_code == 404
    # Le même fichier importé par Bob est une activité distincte, pas un doublon d'Alice
    assert _upload(bob, make_gpx(line(100))).get_json()['results'][0]['status'] == 'created'
    assert alice.get(f'/api/activities/{activity_id}').status_code == 200
