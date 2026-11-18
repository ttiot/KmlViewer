import numpy as np
import pytest

from app.services import activity_store, dem_service, ingest_service
from tests.conftest import line, make_gpx

CFG = {'DEM_URL': 'http://dem.test/v1/glo30', 'DEM_BATCH_SIZE': 100, 'DEM_TIMEOUT': 1}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise dem_service.requests.HTTPError(str(self.status))

    def json(self):
        return self.payload


def _fake_dem(monkeypatch, elevation=lambda lat, lon: 1000 + (lat - 45.0) * 10000, calls=None):
    def get(self, url, params=None, **kwargs):
        if calls is not None:
            calls.append(params['locations'].count('|') + 1)
        results = [{'elevation': elevation(*map(float, loc.split(',')))}
                   for loc in params['locations'].split('|')]
        return FakeResponse({'status': 'OK', 'results': results})
    monkeypatch.setattr(dem_service.requests.Session, 'get', get)


def test_profile_is_subsampled_and_interpolated(monkeypatch):
    calls = []
    _fake_dem(monkeypatch, calls=calls)
    points = line(1000, dlat=0.00005)  # ~5,5 m entre points
    profile = dem_service.corrected_profile([p[0] for p in points], [p[1] for p in points], CFG)
    assert len(profile) == 1000
    assert sum(calls) < 300 and max(calls) <= 100
    assert profile[0] == pytest.approx(1000, abs=0.5) and profile[-1] == pytest.approx(1499.5, abs=1)
    assert np.all(np.diff(profile) >= 0)


def test_dem_errors(monkeypatch):
    lat, lon = [45.0, 45.001], [6.0, 6.0]
    monkeypatch.setattr(dem_service.requests.Session, 'get',
                        lambda self, url, **kw: FakeResponse({'status': 'INVALID_REQUEST'}))
    with pytest.raises(dem_service.DemError):
        dem_service.corrected_profile(lat, lon, CFG)
    monkeypatch.setattr(dem_service.requests.Session, 'get', lambda self, url, **kw: FakeResponse({}, 500))
    with pytest.raises(dem_service.DemError):
        dem_service.corrected_profile(lat, lon, CFG)
    _fake_dem(monkeypatch, elevation=lambda lat, lon: None)
    with pytest.raises(dem_service.DemError):
        dem_service.corrected_profile(lat, lon, CFG)
    with pytest.raises(dem_service.DemError):
        dem_service.fetch_elevations(lat, lon, {**CFG, 'DEM_URL': 'file:///etc/passwd'})


def test_ingest_uses_dem_and_survives_outage(app, db, local_user_id, monkeypatch):
    rng = np.random.default_rng(1)
    noisy = line(300, ele=lambda i: 800 + rng.normal(0, 6))  # terrain plat, GPS bruité
    cfg = {**app.config, **CFG}

    _fake_dem(monkeypatch, elevation=lambda lat, lon: 800.0)
    ok = ingest_service.ingest_bytes(db, cfg, local_user_id, make_gpx(noisy).encode(), 'a.gpx', 'upload')
    row = activity_store.get(db, local_user_id, ok.activity_id)
    assert row['elev_source'] == 'dem' and row['ascent_m'] == 0 and row['ascent_gps_m'] > 0
    assert activity_store.unpack_points(row['points'])['ele_dem'][0] == 800.0

    def down(self, url, **kw):
        raise dem_service.requests.ConnectionError('down')
    monkeypatch.setattr(dem_service.requests.Session, 'get', down)
    ko = ingest_service.ingest_bytes(db, cfg, local_user_id, make_gpx(noisy, name='B').encode(), 'b.gpx', 'upload')
    assert ko.status == 'created'
    assert activity_store.get(db, local_user_id, ko.activity_id)['elev_source'] == 'gps'


def test_backfill_corrects_activities_ingested_during_outage(app, db, local_user_id, monkeypatch):
    cfg = {**app.config, **CFG}

    def down(self, url, **kw):
        raise dem_service.requests.ConnectionError('down')
    monkeypatch.setattr(dem_service.requests.Session, 'get', down)
    result = ingest_service.ingest_bytes(db, cfg, local_user_id, make_gpx(line(200)).encode(), 'a.gpx', 'upload')
    assert activity_store.get(db, local_user_id, result.activity_id)['dem_status'] is None
    assert ingest_service.backfill_elevation(db, cfg) == 0  # toujours en panne : rien, et pas d'erreur

    _fake_dem(monkeypatch, elevation=lambda lat, lon: 800.0)
    assert ingest_service.backfill_elevation(db, cfg) == 1
    row = activity_store.get(db, local_user_id, result.activity_id)
    assert row['elev_source'] == 'dem' and row['dem_status'] == 'ok'
    assert ingest_service.backfill_elevation(db, cfg) == 0  # rien à refaire


def test_backfill_does_not_retry_tracks_without_coverage(app, db, local_user_id, monkeypatch):
    cfg = {**app.config, **CFG}
    _fake_dem(monkeypatch, elevation=lambda lat, lon: None)
    result = ingest_service.ingest_bytes(db, cfg, local_user_id, make_gpx(line(50)).encode(), 'a.gpx', 'upload')
    row = activity_store.get(db, local_user_id, result.activity_id)
    assert row['dem_status'] == 'no_coverage' and row['elev_source'] == 'gps'
    assert activity_store.pending_dem(db, 10) == []


def test_backfill_is_a_noop_without_dem(app, db, local_user_id):
    ingest_service.ingest_bytes(db, app.config, local_user_id, make_gpx(line(50)).encode(), 'a.gpx', 'upload')
    assert ingest_service.backfill_elevation(db, app.config) == 0
