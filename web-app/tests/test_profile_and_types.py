"""Profil utilisateur, catalogue des types d'activité, année affichée du tableau de bord, version."""

from datetime import date, datetime, timezone
from io import BytesIO

import pytest

from app import db as database
from app import version
from app.services import activity_types, dashboard_service, profile_service
from tests.conftest import line, login_as, make_gpx


def _upload(client, *payloads):
    data = {'files': [(BytesIO(p.encode()), f'trace{i}.gpx') for i, p in enumerate(payloads)]}
    return client.post('/api/activities', data=data, content_type='multipart/form-data').get_json()['results']


# ------------------------------------------------------------------ Types d'activité

@pytest.mark.parametrize('raw, expected', [
    ('running', 'run'), ('Trail Running', 'trail'), ('hiking', 'hike'), ('Randonnée', 'hike'),
    ('cycling', 'road_bike'), ('mountain_biking', 'mtb'), ('VTT', 'mtb'), ('9', 'run'),
    ('run', 'run'), ('swimming', 'other'), ('', None), (None, None), ('  ', None),
])
def test_file_type_is_mapped_to_catalog(raw, expected):
    assert activity_types.from_raw(raw) == expected


def test_activity_type_can_be_set_and_cleared(client):
    activity_id = _upload(client, make_gpx(line(40)))[0]['activity_id']
    get_type = lambda: client.get(f'/api/activities/{activity_id}').get_json()['activity']['activity_type']  # noqa: E731
    assert get_type() == 'run'
    assert client.patch(f'/api/activities/{activity_id}', json={'activity_type': 'trail'}).status_code == 200
    assert get_type() == 'trail'
    assert client.patch(f'/api/activities/{activity_id}', json={'activity_type': None}).status_code == 200
    assert get_type() is None
    for bad in ('running', '', 'unclassified', 3, ['run'], "run'; DROP TABLE activities; --"):
        assert client.patch(f'/api/activities/{activity_id}', json={'activity_type': bad}).status_code == 400, bad
    assert client.patch(f'/api/activities/{activity_id}', json={}).status_code == 400
    assert client.patch(f'/api/activities/{activity_id}', json={'user_id': 2}).status_code == 400
    assert get_type() is None


def test_type_filter_covers_unclassified_and_rejects_unknown(client):
    typed, untyped = _upload(client, make_gpx(line(40)), make_gpx(line(50), activity_type=''))
    ids = lambda query: [a['id'] for a in client.get(f'/api/activities{query}').get_json()['activities']]  # noqa: E731
    assert ids('?type=run') == [typed['activity_id']]
    assert ids('?type=unclassified') == [untyped['activity_id']]
    assert len(ids('')) == 2
    assert client.get('/api/activities?type=running').status_code == 400
    assert client.get('/api/dashboard?type=nope').status_code == 400
    assert client.get('/api/dashboard?type=unclassified').get_json()['stats']['totals']['count'] == 1


def test_default_type_applies_to_untyped_imports_only(client):
    assert client.patch('/api/me/profile', json={'default_activity_type': 'hike'}).status_code == 200
    typed, untyped = _upload(client, make_gpx(line(40)), make_gpx(line(50), activity_type=''))
    kind = lambda r: client.get(f"/api/activities/{r['activity_id']}").get_json()['activity']['activity_type']  # noqa: E731
    assert kind(typed) == 'run' and kind(untyped) == 'hike'


def test_migration_normalizes_legacy_types(tmp_path):
    path = str(tmp_path / 'legacy.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    conn.execute("INSERT INTO users (sub) VALUES ('u')")
    for index, legacy in enumerate(['Running', 'cycling', 'kayak', None]):
        conn.execute("INSERT INTO activities (user_id, name, filename, sha256, source, activity_type, start_time, points) "
                     "VALUES (1, 'a', 'a.gpx', ?, 'upload', ?, '2025-01-01T00:00:00Z', x'00')", (str(index), legacy))
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    database.init_db(path)
    conn = database.connect(path)
    assert [r[0] for r in conn.execute("SELECT activity_type FROM activities ORDER BY id")] == ['run', 'road_bike', 'other', None]
    conn.close()


def test_best_efforts_only_for_running_and_top_speed_record(client):
    activity_id = _upload(client, make_gpx(line(101)))[0]['activity_id']
    kinds = lambda: {r['kind'] for r in client.get('/api/dashboard').get_json()['records']}  # noqa: E731
    assert {'distance', 'time', 'speed', 'best_effort'} <= kinds()
    client.patch(f'/api/activities/{activity_id}', json={'activity_type': 'car'})
    assert 'best_effort' not in kinds() and 'speed' in kinds()


def test_track_carries_distance_and_elevation_profile(client):
    activity_id = _upload(client, make_gpx(line(100, ele=lambda i: 100.0 + i)))[0]['activity_id']
    track = client.get(f'/api/activities/{activity_id}/track?max_points=10').get_json()
    assert len(track['distances_m']) == len(track['elevations']) == len(track['coordinates'])
    assert track['distances_m'][0] == 0 and track['distances_m'] == sorted(track['distances_m'])
    assert track['distances_m'][-1] == pytest.approx(99 * 33.36, rel=0.02)
    assert track['elevations'][0] == 100.0 and track['elevations'][-1] == 199.0


def test_track_distance_ignores_the_gap_between_two_traces(client, db):
    from app.services import activity_store
    activity_id = _upload(client, make_gpx(line(20)))[0]['activity_id']
    single = client.get(f'/api/activities/{activity_id}/track').get_json()['distances_m'][-1]
    row = db.execute("SELECT points FROM activities WHERE id = ?", (activity_id,)).fetchone()
    pts = activity_store.unpack_points(row['points'])
    far = [lat + 1.0 for lat in pts['lat']]  # seconde trace, 111 km plus au nord
    blob = activity_store.pack_points(pts['lat'] + far, pts['lon'] * 2, pts['ele'] * 2, pts['t'] * 2, breaks=[0, 20])
    db.execute("UPDATE activities SET points = ? WHERE id = ?", (blob, activity_id))
    db.commit()
    double = client.get(f'/api/activities/{activity_id}/track').get_json()['distances_m']
    assert double[-1] == pytest.approx(2 * single, rel=0.01) and double[20] == double[19]


# ------------------------------------------------------------- Année du tableau de bord

def test_dashboard_falls_back_to_latest_year_with_data(client, db, local_user_id):
    _upload(client, make_gpx(line(101), start=datetime(2024, 6, 1, 8, 0, tzinfo=timezone.utc)))
    stats = lambda **kw: dashboard_service.period_stats(db, local_user_id, 'year', 'UTC', today=date(2026, 11, 19), **kw)  # noqa: E731
    auto = stats()
    assert auto['calendar_year'] == 2026 and auto['latest_year'] == 2024
    assert auto['current_year']['year'] == 2024 and auto['current_year']['count'] == 1
    assert [s['period'] for s in auto['series']] == [f'2024-{m:02d}' for m in range(1, 13)]
    assert [s['count'] for s in auto['series']] == [0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]
    forced = stats(year=2026)
    assert forced['current_year'] == {'year': 2026, 'count': 0, 'distance_km': 0.0, 'ascent_m': 0, 'time_s': 0}
    assert forced['totals']['count'] == 1 and sum(s['count'] for s in forced['series']) == 0


def test_dashboard_empty_library_shows_calendar_year(db, local_user_id):
    stats = dashboard_service.period_stats(db, local_user_id, 'year', 'UTC', today=date(2026, 11, 19))
    assert stats['current_year']['year'] == 2026 and stats['latest_year'] is None


@pytest.mark.parametrize('year', ['abc', '-1', '1800', '99999', '2024.5', '٢٠٢٤'])
def test_dashboard_rejects_invalid_year(client, year):
    assert client.get(f'/api/dashboard?year={year}').status_code == 400


def test_dashboard_accepts_year(client):
    assert client.get('/api/dashboard?period=year&year=2024').get_json()['stats']['current_year']['year'] == 2024


# -------------------------------------------------------------------------- Profil

def test_profile_roundtrip_and_clearing(client):
    assert client.get('/api/me/profile').get_json()['profile'] == dict.fromkeys(profile_service.FIELDS)
    body = {'birth_date': '1985-03-12', 'sex': 'male', 'height_cm': 180, 'weight_kg': 72.46,
            'hr_max': 185, 'hr_rest': 52, 'default_activity_type': 'trail'}
    saved = client.patch('/api/me/profile', json=body).get_json()['profile']
    assert saved == {**body, 'weight_kg': 72.5}
    assert client.patch('/api/me/profile', json={'weight_kg': None, 'sex': None}).status_code == 200
    profile = client.get('/api/me/profile').get_json()['profile']
    assert profile['weight_kg'] is None and profile['sex'] is None and profile['height_cm'] == 180


@pytest.mark.parametrize('body', [
    {}, {'height_cm': 10}, {'height_cm': '180'}, {'height_cm': True}, {'height_cm': 180.5},
    {'weight_kg': 5}, {'weight_kg': 'lourd'}, {'weight_kg': float('1e400')},
    {'hr_max': 300}, {'hr_rest': 5}, {'hr_max': 120, 'hr_rest': 130},
    {'birth_date': '2999-01-01'}, {'birth_date': '12/03/1985'}, {'birth_date': '1850-01-01'}, {'birth_date': 19850312},
    {'sex': 'autre'}, {'sex': 1}, {'default_activity_type': 'running'}, {'default_activity_type': 'unclassified'},
    # Liste blanche : ni l'identité, ni les droits, ni un champ inconnu
    {'is_admin': True}, {'email': 'victime@example.com'}, {'display_name': 'x'}, {'session_token': 'x'},
    {'height_cm': 180, 'is_admin': True},
])
def test_profile_rejects_invalid_or_forbidden_fields(client, db, body):
    assert client.patch('/api/me/profile', json=body).status_code == 400, body
    row = db.execute("SELECT email, height_cm FROM users WHERE sub = 'local'").fetchone()
    assert row['email'] is None and row['height_cm'] is None  # rien n'est écrit, même partiellement


def test_profile_heart_rates_stay_consistent_across_requests(client):
    assert client.patch('/api/me/profile', json={'hr_rest': 60}).status_code == 200
    assert client.patch('/api/me/profile', json={'hr_max': 55}).status_code == 400
    assert client.patch('/api/me/profile', json={'hr_max': 180}).status_code == 200
    assert client.patch('/api/me/profile', json={'hr_rest': 180}).status_code == 400


def test_profiles_are_isolated_and_need_a_session(oidc_app):
    alice, _ = login_as(oidc_app, 'alice')
    bob, _ = login_as(oidc_app, 'bob')
    assert alice.patch('/api/me/profile', json={'weight_kg': 60}).status_code == 200
    assert bob.get('/api/me/profile').get_json()['profile']['weight_kg'] is None
    anonymous = oidc_app.test_client()
    assert anonymous.get('/api/me/profile').status_code == 401
    assert anonymous.patch('/api/me/profile', json={'weight_kg': 60}).status_code == 401
    assert anonymous.get('/profile').status_code == 302
    # Requête modifiante venue d'un autre site : refusée par le contrôle d'origine
    assert alice.patch('/api/me/profile', json={'weight_kg': 99},
                       headers={'Origin': 'https://evil.example'}).status_code == 403


def test_profile_page_renders_values_and_escapes_identity(oidc_app):
    client, _ = login_as(oidc_app, 'carol', name='<script>alert(1)</script>')
    client.patch('/api/me/profile', json={'height_cm': 171, 'default_activity_type': 'mtb'})
    page = client.get('/profile').get_data(as_text=True)
    assert 'value="171"' in page and '<option value="mtb" selected>' in page
    assert '<script>alert(1)</script>' not in page and '&lt;script&gt;' in page


def test_profile_migration_adds_columns_to_existing_database(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    for column, _ in database._PROFILE_COLUMNS:
        conn.execute(f"ALTER TABLE users DROP COLUMN {column}")
    conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'ancien')")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    database.init_db(path)
    conn = database.connect(path)
    row = conn.execute("SELECT * FROM users WHERE sub = 'u'").fetchone()
    assert row['username'] == 'ancien' and row['hr_max'] is None and row['default_activity_type'] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(database.MIGRATIONS)
    conn.close()


# ------------------------------------------------------------------------- Version

def test_version_prefers_build_value_then_git(monkeypatch):
    monkeypatch.setattr(version, 'from_git', lambda: 'abc1234')
    assert version.resolve('v1.8.0') == 'v1.8.0'
    assert version.resolve('unstable-0a1b2c3') == 'unstable-0a1b2c3'
    assert version.resolve(None) == version.resolve('') == version.resolve('dev') == 'abc1234'
    monkeypatch.setattr(version, 'from_git', lambda: None)
    assert version.resolve(None) == 'dev'


@pytest.mark.parametrize('hostile', ['<script>x</script>', 'v1 ; rm -rf', 'a' * 200, '"onload="x'])
def test_version_with_unexpected_characters_is_discarded(monkeypatch, hostile):
    monkeypatch.setattr(version, 'from_git', lambda: None)
    assert version.resolve(hostile) == 'dev'


def test_git_version_is_exact_tag_or_short_sha(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        return None if args[0] == 'describe' else '0a1b2c3'
    monkeypatch.setattr(version, '_git', fake_git)
    assert version.from_git() == '0a1b2c3'
    assert calls == [('describe', '--tags', '--exact-match', 'HEAD'), ('rev-parse', '--short', 'HEAD')]
    monkeypatch.setattr(version, '_git', lambda *args: 'v1.7.1' if args[0] == 'describe' else 'ffffff0')
    assert version.from_git() == 'v1.7.1'


def test_footer_shows_version_on_every_page(app, client):
    app.config['APP_VERSION'] = 'v9.9.9'
    for path in ('/', '/dashboard', '/segments', '/profile', '/admin/users'):
        page = client.get(path)
        assert page.status_code == 200, path
        assert 'version <span class="mono">v9.9.9</span>' in page.get_data(as_text=True), path
    assert client.get('/api/health').get_json()['version'] == 'v9.9.9'
