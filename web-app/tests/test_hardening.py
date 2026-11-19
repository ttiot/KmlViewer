"""Non-régression des durcissements de sécurité (redirections, session, hôtes, identité, volumes)."""

import io
import logging
import time
import zipfile
from datetime import timedelta

import pytest
from flask import request

from app import create_app
from app.auth import oidc, users
from app.auth.guard import safe_next
from app.config import TestingConfig
from app.services import cache_service, file_formats
from tests.conftest import OIDC_OVERRIDES, line, login_as, make_gpx


def _files(*items):
    return {'files': [(io.BytesIO(raw if isinstance(raw, bytes) else raw.encode()), name) for raw, name in items]}


def _post_files(client, path, *items):
    return client.post(path, data=_files(*items), content_type='multipart/form-data')


# --- Redirection après connexion ------------------------------------------------------

@pytest.mark.parametrize('evil', ['/\t/evil.io', '/\n/evil.io', '/\r/evil.io', '/ /evil.io', '/\x00/evil.io',
                                  '\t//evil.io', '/\x7f/evil.io'])
def test_safe_next_rejects_whitespace_smuggling(app, evil):
    with app.test_request_context():
        assert safe_next(evil) == '/'


def test_login_never_redirects_off_site(oidc_app, monkeypatch):
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: {'userinfo': {'sub': 'u1'}})
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_redirect', lambda uri: ('', 302))
    client = oidc_app.test_client()
    client.get('/auth/login?next=/%09/evil.io/phish')
    assert client.get('/auth/callback?code=x&state=y').headers['Location'] == '/'


# --- Session ---------------------------------------------------------------------------

def test_session_lifetime_is_twelve_hours_and_absolute(oidc_app, monkeypatch):
    assert oidc_app.permanent_session_lifetime == timedelta(hours=12)
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: {'userinfo': {'sub': 'u1'}})
    client = oidc_app.test_client()
    client.get('/auth/callback?code=x&state=y')
    assert client.get('/api/me').status_code == 200
    # L'activité ne prolonge pas la session : treize heures après la connexion, elle est finie
    with client.session_transaction() as session:
        session['login_at'] = time.time() - 13 * 3600
    assert client.get('/api/me').status_code == 401


def test_session_without_login_date_is_refused(oidc_app):
    _, user_id = login_as(oidc_app, 'alice')
    client = oidc_app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user_id
    assert client.get('/api/me').status_code == 401


def test_logout_is_not_reachable_by_get(oidc_app):
    client, _ = login_as(oidc_app, 'alice')
    assert client.get('/auth/logout').status_code == 405
    assert client.get('/api/me').status_code == 200


def test_oidc_refuses_a_short_secret_key(tmp_path):
    class Short(TestingConfig):
        TESTING = False
        INBOX_ENABLED = False
        SECRET_KEY = 'trop-courte'
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        create_app(Short, overrides={'DATA_DIR': str(tmp_path / 'data'), **OIDC_OVERRIDES})


# --- Hôtes et en-têtes -----------------------------------------------------------------

def test_local_mode_only_answers_to_local_hosts(client):
    assert client.get('/api/me').status_code == 200
    assert client.get('/api/me', headers={'Host': '127.0.0.1:8080'}).status_code == 200
    assert client.get('/api/me', headers={'Host': 'rebind.evil.io:8080'}).status_code == 400
    response = client.delete('/api/activities/1', headers={'Host': 'rebind.evil.io', 'Origin': 'http://rebind.evil.io'})
    assert response.status_code == 400


def test_trusted_hosts_can_be_configured(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), 'TRUSTED_HOSTS': ['traces.lan']})
    client = app.test_client()
    assert client.get('/api/me', headers={'Host': 'traces.lan'}).status_code == 200
    assert client.get('/api/me', headers={'Host': 'localhost'}).status_code == 400


def test_forwarded_host_is_ignored_unless_asked(tmp_path):
    def host_seen(**overrides):
        app = create_app(TestingConfig, overrides={
            'DATA_DIR': str(tmp_path / 'data'), 'TRUST_PROXY': 1, 'TRUSTED_HOSTS': ['localhost', 'public.io'],
            **overrides})
        app.add_url_rule('/host', view_func=lambda: request.host)
        return app.test_client().get('/host', headers={'X-Forwarded-Host': 'public.io'}).get_data(as_text=True)

    assert host_seen() == 'localhost'
    assert host_seen(TRUST_PROXY_HOST=True) == 'public.io'


def test_hsts_only_over_https(client):
    assert 'Strict-Transport-Security' not in client.get('/api/health').headers
    assert 'Strict-Transport-Security' in client.get('/api/health', base_url='https://localhost').headers


def test_log_lines_cannot_be_forged_through_the_path(client, caplog):
    with caplog.at_level(logging.WARNING, logger='app.security'):
        assert client.post('/api/x%0aFAUSSE%20LIGNE', headers={'Origin': 'http://evil.io'}).status_code == 403
    assert all('\n' not in record.getMessage() for record in caplog.records)


# --- Identité des comptes --------------------------------------------------------------

def test_login_matches_email_or_username_never_both(db):
    victim = users.upsert_from_claims(db, {'sub': 'v', 'email': 'boss@corp.io', 'email_verified': True,
                                            'preferred_username': 'boss'})
    users.upsert_from_claims(db, {'sub': 'x', 'email': 'x@evil.io', 'preferred_username': 'boss@corp.io'})
    assert users.find_by_login(db, 'boss@corp.io')['id'] == victim['id']
    assert users.find_by_login(db, 'BOSS')['id'] == victim['id']
    # Tant que la victime n'existe pas, son email ne désigne personne
    db.execute("DELETE FROM users WHERE id = ?", (victim['id'],))
    db.commit()
    assert users.find_by_login(db, 'boss@corp.io') is None


def test_ambiguous_login_designates_nobody(db):
    users.upsert_from_claims(db, {'sub': 'a', 'preferred_username': 'sam'})
    users.upsert_from_claims(db, {'sub': 'b', 'preferred_username': 'Sam'})
    assert users.find_by_login(db, 'sam') is None


def test_unverified_email_is_not_kept(db):
    user = users.upsert_from_claims(db, {'sub': 'a', 'email': 'boss@corp.io', 'email_verified': False})
    assert user['email'] is None and user['username'] == 'a'
    assert users.upsert_from_claims(db, {'sub': 'b', 'email': 'b@corp.io', 'email_verified': True})['email']


def test_user_admin_refuses_an_ambiguous_login(app, db, runner):
    users.upsert_from_claims(db, {'sub': 'a', 'preferred_username': 'sam'})
    users.upsert_from_claims(db, {'sub': 'b', 'preferred_username': 'SAM'})
    users.upsert_from_claims(db, {'sub': 'c', 'preferred_username': 'zoe'})
    db.execute("UPDATE users SET is_admin = 0")
    db.commit()
    result = runner.invoke(args=['user-admin', 'sam'])
    assert result.exit_code != 0 and 'plusieurs comptes' in result.output
    assert db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0
    assert runner.invoke(args=['user-admin', 'zoe']).exit_code == 0
    assert [r['username'] for r in db.execute("SELECT username FROM users WHERE is_admin = 1")] == ['zoe']
    assert runner.invoke(args=['user-admin', 'zoe', '--revoke']).exit_code == 0
    assert db.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0] == 0


# --- Volumes ---------------------------------------------------------------------------

def _kml(*counts):
    """KML d'une ligne par compte donné."""
    placemarks = ''.join("<Placemark><LineString><coordinates>" + '6.1,45.1,1 ' * count
                         + "</coordinates></LineString></Placemark>" for count in counts)
    return f"<kml xmlns='http://www.opengis.net/kml/2.2'><Document>{placemarks}</Document></kml>"


def test_files_with_too_many_points_are_refused(client, monkeypatch):
    monkeypatch.setattr(file_formats, 'MAX_FILE_POINTS', 50)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('doc.kml', _kml(51))
    results = _post_files(client, '/api/multi-upload', (buffer.getvalue(), 'bombe.kmz'),
                          (make_gpx(line(51)), 'long.gpx'), (_kml(30, 30), 'deux.kml'),
                          (_kml(50), 'ok.kml')).get_json()['files']
    assert [r['success'] for r in results] == [False, False, False, True]
    assert all('trop volumineux' in r['error'] for r in results[:3])
    # Un fichier au plafond passe ; le budget d'une requête vaut un fichier plein, donc à part
    assert _post_files(client, '/api/multi-upload', (make_gpx(line(50)), 'ok.gpx')).get_json()['files'][0]['success']
    imported = _post_files(client, '/api/activities', (make_gpx(line(51)), 'long.gpx')).get_json()['results'][0]
    assert imported['status'] == 'error' and 'trop volumineux' in imported['message']


def test_kmz_limit_matches_the_upload_limit(app):
    assert file_formats.MAX_KMZ_UNCOMPRESSED <= app.config['MAX_CONTENT_LENGTH']


def test_parse_cache_is_bounded_in_volume(monkeypatch):
    cache_service.clear_cache()
    monkeypatch.setattr(cache_service, 'MAX_ENTRY_BYTES', 5000)
    monkeypatch.setattr(cache_service, 'MAX_TOTAL_BYTES', 9000)
    big = make_gpx(line(200))
    assert len(big) > 5000 and cache_service.parse_gpx_cached(big)['success']
    assert len(cache_service._parse_cache) == 0  # trop gros : jamais retenu
    small = [make_gpx(line(40, lat0=45.0 + i)) for i in range(4)]
    for content in small:
        cache_service.parse_gpx_cached(content)
    kept = sum(cache_service._weights.values())
    assert 0 < kept <= 9000 and len(cache_service._parse_cache) < 4
    assert set(cache_service._weights) == set(cache_service._parse_cache)
    cache_service.clear_cache()


def test_multi_upload_caps_the_number_of_files(client):
    items = [('x', f'{i}.gpx') for i in range(51)]
    response = _post_files(client, '/api/multi-upload', *items)
    assert response.status_code == 400 and 'maximum' in response.get_json()['error']


def test_storage_quota(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), 'USER_QUOTA_MB': 1})
    client = app.test_client()
    padding = '<!-- ' + 'x' * 600_000 + ' -->'
    first = make_gpx(line(30)) + padding
    second = make_gpx(line(31)) + padding
    statuses = [_post_files(client, '/api/activities', (raw, name)).get_json()['results'][0]
                for raw, name in ((first, 'a.gpx'), (second, 'b.gpx'))]
    assert statuses[0]['status'] == 'created'
    assert statuses[1]['status'] == 'error' and 'Quota' in statuses[1]['message']
    # Supprimer libère de la place
    client.delete(f"/api/activities/{statuses[0]['activity_id']}")
    assert _post_files(client, '/api/activities', (second, 'b.gpx')).get_json()['results'][0]['status'] == 'created'


# --- Export ----------------------------------------------------------------------------

def test_csv_export_neutralises_formulas(client):
    features = [
        {'type': 'marker', 'name': '=HYPERLINK("http://evil.io","x")', 'description': '@SUM(1)', 'coordinates': [1, 2]},
        {'type': 'polyline', 'name': '+cmd', 'description': '-2+3', 'coordinates': [[1, 2, 3], [1.1, 2.1, -4]]},
    ]
    rows = client.post('/api/export/csv', json={'features': features}).data.decode().splitlines()[1:]
    assert rows[0].startswith('Point,"\'=HYPERLINK') and ",'@SUM(1)," in rows[0]
    assert rows[1].startswith("TracePoint,'+cmd - Point 1,'-2+3,")
    assert rows[2].endswith(',1.1,2.1,-4,,,')  # les nombres restent des nombres
