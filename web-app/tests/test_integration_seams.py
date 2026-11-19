"""Raccords entre l'ingestion, le stockage, le MNT et l'analyse enregistrée."""

import io

from app.services import activity_store, analysis_store, dem_service, ingest_service
from tests.conftest import line


def _two_tracks_gpx(gap_deg=0.5):
    """Deux traces de 40 points séparées de ~55 km ; la seconde est 1500 m plus haut."""
    def track(points, start_hour):
        rows = ''.join(
            f"<trkpt lat='{lat:.6f}' lon='{lon:.6f}'><ele>{ele}</ele><time>2025-06-01T{start_hour:02d}:{i // 60:02d}:{i % 60:02d}Z</time></trkpt>"
            for i, (lat, lon, ele) in enumerate(points))
        return f"<trk><name>Sortie</name><trkseg>{rows}</trkseg></trk>"
    first = line(40, ele=lambda i: 500.0)
    second = line(40, lat0=45.0 + gap_deg, ele=lambda i: 2000.0)
    return ("<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'>"
            f"{track(first, 8)}{track(second, 10)}</gpx>").encode()


def _import(client, raw, name='deux.gpx'):
    result = client.post('/api/activities', data={'files': (io.BytesIO(raw), name)},
                         content_type='multipart/form-data').get_json()['results'][0]
    assert result['status'] == 'created', result
    return result['activity_id']


def test_track_breaks_are_stored_and_survive_a_recompute(client, db, local_user_id):
    """Recalculer les statistiques ne doit pas relier deux traces par une liaison fantôme."""
    activity_id = _import(client, _two_tracks_gpx())
    row = activity_store.get(db, local_user_id, activity_id)
    assert activity_store.unpack_points(row['points'])['breaks'] == [0, 40]
    distance = row['distance_m']
    assert distance < 3000  # 2 × 39 pas de ~33 m, sans les ~55 km entre les deux traces

    assert ingest_service.recompute_summaries(db, local_user_id) == 1
    assert activity_store.get(db, local_user_id, activity_id)['distance_m'] == distance


def test_points_stored_before_breaks_existed_are_still_read(db, local_user_id):
    blob = activity_store.pack_points([45.0, 45.001], [6.0, 6.0], [100.0, 101.0], [None, None])
    points = activity_store.unpack_points(blob)
    points.pop('breaks')  # activité enregistrée avant l'ajout du champ
    assert (points.get('breaks') or []) == []


def test_terrain_elevation_gain_ignores_the_step_between_tracks(client, app, db, local_user_id, monkeypatch):
    """La marche de 1500 m entre deux traces n'est un dénivelé ni en bibliothèque ni dans l'analyse."""
    monkeypatch.setitem(app.config, 'DEM_URL', 'http://dem.test/v1/x')
    monkeypatch.setattr(dem_service, 'corrected_profile',
                        lambda lat, lon, cfg: [500.0] * 40 + [2000.0] * 40)
    activity_id = _import(client, _two_tracks_gpx())
    result = ingest_service.recompute_elevation(db, app.config, local_user_id, activity_id)
    assert result['elev_source'] == 'dem' and result['ascent_m'] < 10

    # L'analyse enregistrée mesure sur la même altitude que la bibliothèque
    saved = analysis_store.compute(db, app.config, local_user_id, activity_id)
    summary = saved['analysis']['summary']
    assert summary['elevation_source'] == 'dem' and summary['ascent_m'] < 10


def test_failing_activity_does_not_block_the_elevation_backlog(client, app, db, local_user_id, monkeypatch):
    """Une trace dont la correction échoue à coup sûr passe en fin de file, les suivantes sont servies."""
    monkeypatch.setitem(app.config, 'DEM_URL', 'http://dem.test/v1/x')
    ids = [_import(client, _two_tracks_gpx(gap_deg=0.5 + i / 10), f'{i}.gpx') for i in range(6)]
    broken = set(ids[:4])  # plus d'échecs que DEM_MAX_FAILURES_PER_PASS, en tête de file
    current = {}

    def fake_recompute(db_, cfg, user_id, activity_id):
        current['id'] = activity_id
        if activity_id in broken:
            raise dem_service.DemError('Budget de temps MNT dépassé')
        activity_store.set_dem_status(db_, user_id, activity_id, 'ok')
        db_.commit()
        return {}

    monkeypatch.setattr(ingest_service, 'recompute_elevation', fake_recompute)
    monkeypatch.setattr(ingest_service, '_dem_failed', set())
    corrected = sum(ingest_service.backfill_elevation(db, app.config) for _ in range(3))
    assert corrected == 2  # les deux activités saines, malgré quatre échecs permanents devant elles
    pending = {row['id'] for row in activity_store.pending_dem(db, 50)}
    assert pending == broken  # toujours en attente : retentées dès que le service le permet
