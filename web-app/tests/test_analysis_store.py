"""Analyse avancée enregistrée : calcul unique, relance, invalidation et cloisonnement."""

from io import BytesIO
from pathlib import Path

import pytest

from app.services import advanced_analysis, analysis_store
from tests.conftest import line, login_as, make_gpx

HILLY = line(300, ele=lambda i: 500 + 40 * (i % 60 < 30) * (i % 30) - 40 * (i % 60 >= 30) * (i % 30 - 30))


def _import(client, points=HILLY, name='Sortie'):
    data = {'files': [(BytesIO(make_gpx(points, name=name).encode()), 'trace.gpx')]}
    response = client.post('/api/activities', data=data, content_type='multipart/form-data')
    return response.get_json()['results'][0]['activity_id']


@pytest.fixture
def calls(monkeypatch):
    """Compte les exécutions réelles de l'algorithme."""
    counter = {'count': 0}
    real = advanced_analysis.analyze

    def counting(*args, **kwargs):
        counter['count'] += 1
        return real(*args, **kwargs)
    monkeypatch.setattr(advanced_analysis, 'analyze', counting)
    return counter


def test_nothing_is_stored_until_the_analysis_is_run(client, calls):
    activity_id = _import(client)
    body = client.get(f'/api/activities/{activity_id}/analysis').get_json()
    assert body == {'success': True, 'analysis': None, 'computed_at': None}
    assert calls['count'] == 0


def test_result_is_computed_once_then_served_from_storage(client, calls):
    activity_id = _import(client)
    first = client.post(f'/api/activities/{activity_id}/analysis').get_json()
    assert first['success'] and first['computed_at']
    assert set(first['analysis']) >= {'stops', 'segments', 'speed_zones', 'terrain', 'points_of_interest', 'summary'}
    assert calls['count'] == 1

    for _ in range(3):
        again = client.get(f'/api/activities/{activity_id}/analysis').get_json()
        assert again['analysis'] == first['analysis'] and again['computed_at'] == first['computed_at']
    assert calls['count'] == 1  # relu, jamais recalculé


def test_stored_result_matches_a_direct_analysis_of_the_same_file(client):
    """Le calcul côté serveur donne ce que le visualiseur obtenait en envoyant la trace."""
    activity_id = _import(client)
    parsed = client.get(f'/api/activities/{activity_id}/parsed').get_json()
    direct = client.post('/api/analysis/advanced', json={
        'features': parsed['features'], 'points': parsed['points']}).get_json()['analysis']
    stored = client.post(f'/api/activities/{activity_id}/analysis').get_json()['analysis']
    assert stored == direct
    assert parsed['activity_id'] == activity_id


def test_rerun_recomputes_and_replaces(client, calls, db):
    activity_id = _import(client)
    client.post(f'/api/activities/{activity_id}/analysis')
    db.execute("UPDATE activity_analyses SET computed_at = '2000-01-01T00:00:00Z'")
    db.commit()
    rerun = client.post(f'/api/activities/{activity_id}/analysis').get_json()
    assert calls['count'] == 2 and rerun['computed_at'] != '2000-01-01T00:00:00Z'
    assert db.execute("SELECT COUNT(*) FROM activity_analyses").fetchone()[0] == 1


def test_result_from_another_algorithm_version_is_ignored(client, calls, monkeypatch):
    activity_id = _import(client)
    client.post(f'/api/activities/{activity_id}/analysis')
    monkeypatch.setattr(advanced_analysis, 'ANALYSIS_VERSION', advanced_analysis.ANALYSIS_VERSION + 1)
    assert client.get(f'/api/activities/{activity_id}/analysis').get_json()['analysis'] is None
    assert client.post(f'/api/activities/{activity_id}/analysis').get_json()['analysis']
    assert client.get(f'/api/activities/{activity_id}/analysis').get_json()['analysis']
    assert calls['count'] == 2


def test_corrupted_stored_result_is_treated_as_absent(client, db):
    activity_id = _import(client)
    client.post(f'/api/activities/{activity_id}/analysis')
    db.execute("UPDATE activity_analyses SET result = x'00ff'")
    db.commit()
    assert client.get(f'/api/activities/{activity_id}/analysis').get_json()['analysis'] is None


def test_delete_then_activity_removal_cascade(client, db):
    activity_id = _import(client)
    client.post(f'/api/activities/{activity_id}/analysis')
    assert client.delete(f'/api/activities/{activity_id}/analysis').status_code == 200
    assert client.get(f'/api/activities/{activity_id}/analysis').get_json()['analysis'] is None
    assert client.delete(f'/api/activities/{activity_id}/analysis').status_code == 200  # idempotent

    client.post(f'/api/activities/{activity_id}/analysis')
    client.delete(f'/api/activities/{activity_id}')
    assert db.execute("SELECT COUNT(*) FROM activity_analyses").fetchone()[0] == 0


def test_missing_original_file_is_reported_without_storing(app, client, db):
    activity_id = _import(client)
    for stored in (Path(app.config['DATA_DIR']) / 'files').rglob('*.gpx'):
        stored.unlink()
    response = client.post(f'/api/activities/{activity_id}/analysis')
    assert response.status_code == 500 and 'indisponible' in response.get_json()['error']
    assert db.execute("SELECT COUNT(*) FROM activity_analyses").fetchone()[0] == 0


def test_unknown_activity(client):
    for method in ('get', 'post', 'delete'):
        assert getattr(client, method)('/api/activities/9999/analysis').status_code == 404


def test_another_user_cannot_read_run_or_delete(oidc_app):
    alice, alice_id = login_as(oidc_app, 'alice')
    bob, bob_id = login_as(oidc_app, 'bob')
    activity_id = _import(alice)
    stored = alice.post(f'/api/activities/{activity_id}/analysis').get_json()

    for method in ('get', 'post', 'delete'):
        assert getattr(bob, method)(f'/api/activities/{activity_id}/analysis').status_code == 404
    assert alice.get(f'/api/activities/{activity_id}/analysis').get_json()['analysis'] == stored['analysis']

    # Même en appelant le service directement avec le mauvais utilisateur
    from app.db import connect
    conn = connect(oidc_app.config['DB_PATH'])
    try:
        assert analysis_store.load(conn, bob_id, activity_id) is None
        assert analysis_store.delete(conn, bob_id, activity_id) is False
        with pytest.raises(LookupError):
            analysis_store.compute(conn, oidc_app.config, bob_id, activity_id)
        assert analysis_store.load(conn, alice_id, activity_id) is not None
    finally:
        conn.close()
