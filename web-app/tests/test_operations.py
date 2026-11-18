"""Exploitation : sauvegarde, tracé allégé, correction d'altitude différée."""

import sqlite3
from io import BytesIO

from app.services import activity_store, dem_service, ingest_service
from tests.conftest import line, make_gpx


def _import(client, n=60):
    data = {'files': [(BytesIO(make_gpx(line(n)).encode()), 'trace.gpx')]}
    response = client.post('/api/activities', data=data, content_type='multipart/form-data')
    return response.get_json()['results'][0]['activity_id']


def test_backup_command(app, client, runner, tmp_path):
    _import(client)
    target = tmp_path / 'copie.sqlite3'
    result = runner.invoke(args=['backup', str(target)])
    assert result.exit_code == 0, result.output
    copy = sqlite3.connect(target)
    assert copy.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    copy.close()


def test_backup_rotation(app, runner):
    backups = app.config['DATA_DIR'] + '/backups'
    import os
    os.makedirs(backups)
    for stamp in ('20250101-000000', '20250102-000000', '20250103-000000'):
        open(f'{backups}/kmlviewer-{stamp}.sqlite3', 'w').close()
    result = runner.invoke(args=['backup', '--keep', '2'])
    assert result.exit_code == 0, result.output
    names = sorted(os.listdir(backups))
    assert len(names) == 2 and 'kmlviewer-20250103-000000.sqlite3' in names


def test_track_is_decimated_with_original_indices(client):
    activity_id = _import(client, 101)
    full = client.get(f'/api/activities/{activity_id}/track').get_json()
    assert full['total_points'] == 101 and full['indices'] == list(range(101))
    light = client.get(f'/api/activities/{activity_id}/track?max_points=10').get_json()
    assert len(light['coordinates']) == len(light['indices']) <= 11
    assert light['indices'][0] == 0 and light['indices'][-1] == 100
    assert light['coordinates'][3] == full['coordinates'][light['indices'][3]]


def test_upload_defers_elevation_to_background(app, client, db, local_user_id, monkeypatch):
    calls = []

    def fake_profile(lat, lon, cfg):
        calls.append(len(lat))
        return [200.0 + i for i in range(len(lat))]

    monkeypatch.setitem(app.config, 'DEM_URL', 'http://dem.test/v1/x')
    monkeypatch.setattr(dem_service, 'corrected_profile', fake_profile)

    activity_id = _import(client)
    assert calls == []  # la requête d'import n'interroge pas le MNT
    row = activity_store.get(db, local_user_id, activity_id)
    assert row['elev_source'] == 'gps' and row['dem_status'] is None

    assert ingest_service.backfill_elevation(db, app.config) == 1
    row = activity_store.get(db, local_user_id, activity_id)
    assert calls == [60] and row['elev_source'] == 'dem' and row['dem_status'] == 'ok'
