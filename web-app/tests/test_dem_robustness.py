"""
Client MNT et téléchargement des dalles face à un service défaillant : réponses mal formées,
altitudes inexploitables, trous de couverture, pannes passagères, contenu qui n'est pas une
dalle. Aucun appel réseau : la session HTTP est simulée.
"""

import json

import numpy as np
import pytest

from app.services import dem_service, dem_tiles

CFG = {'DEM_URL': 'http://dem.test/v1/glo30', 'DEM_BATCH_SIZE': 100, 'DEM_TIMEOUT': 1, 'DEM_RETRY_BACKOFF': 0}
LAT, LON = [45 + i * 0.0003 for i in range(400)], [6.0] * 400  # ~33 m entre points, 13,3 km


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dem_service.requests.HTTPError(f"{self.status_code} for url: http://dem.test/?locations=45.0,6.0")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def serve(monkeypatch, elevation, calls=None):
    """Service simulé : `elevation(lat, lon)` par point."""
    def get(self, url, params=None, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        results = [{'elevation': elevation(*map(float, loc.split(',')))} for loc in params['locations'].split('|')]
        return Response({'status': 'OK', 'results': results})
    monkeypatch.setattr(dem_service.requests.Session, 'get', get)


def script(monkeypatch, *steps):
    """Service simulé : une réponse (ou une exception à lever) par appel, la dernière se répète."""
    calls = []

    def get(self, url, params=None, **kwargs):
        step = steps[min(len(calls), len(steps) - 1)]
        calls.append(params['locations'].count('|') + 1)
        if isinstance(step, Exception):
            raise step
        return step(calls[-1]) if callable(step) else step
    monkeypatch.setattr(dem_service.requests.Session, 'get', get)
    return calls


def ok(count):
    return Response({'status': 'OK', 'results': [{'elevation': 800.0}] * count})


# ------------------------------------------------------------- Réponses mal formées

@pytest.mark.parametrize('payload', [[1, 2], None, 'x', 42, {'status': 'OK'}, {'status': 'OK', 'results': 'x'},
                                     {'status': 'OK', 'results': []}, ValueError('pas du JSON')])
def test_malformed_response_is_a_dem_error(monkeypatch, payload):
    # Avant : un corps JSON qui n'est pas un objet levait AttributeError, non rattrapée par l'ingestion
    script(monkeypatch, Response(payload))
    with pytest.raises(dem_service.DemError) as error:
        dem_service.corrected_profile(LAT, LON, CFG)
    assert not isinstance(error.value, dem_service.DemCoverageError)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True, '800', 1e9, -9999.0, None])
def test_unusable_elevation_is_a_hole_never_a_value(monkeypatch, bad):
    # Avant : NaN, True (1.0) et 1e9 passaient ; le NaN finissait en JSON invalide dans les points stockés
    serve(monkeypatch, lambda lat, lon: bad if 45.030 < lat < 45.033 else 800.0)
    profile = dem_service.corrected_profile(LAT, LON, CFG)
    assert np.isfinite(profile).all() and set(profile.round(3)) == {800.0}
    json.dumps(profile.tolist(), allow_nan=False)


def test_long_hole_is_a_coverage_error_not_an_invented_ramp(monkeypatch):
    # Plateau à 500 m, 6,5 km sans altitude, massif à 2500 m : avant, une rampe et +2000 m de D+ « MNT »
    serve(monkeypatch, lambda lat, lon: 500.0 if lat < 45.03 else (None if lat < 45.089 else 2500.0))
    with pytest.raises(dem_service.DemCoverageError, match='sans altitude'):
        dem_service.corrected_profile(LAT, LON, CFG)
    # Le plafond se règle ; un trou court reste comblé
    assert len(dem_service.corrected_profile(LAT, LON, {**CFG, 'DEM_MAX_GAP_M': 10000})) == 400
    serve(monkeypatch, lambda lat, lon: None if 45.030 < lat < 45.033 else 800.0)
    assert len(dem_service.corrected_profile(LAT, LON, CFG)) == 400


def test_hole_at_either_end_is_bounded_too(monkeypatch):
    serve(monkeypatch, lambda lat, lon: None if lat < 45.02 else 800.0)  # 2,2 km au départ
    with pytest.raises(dem_service.DemCoverageError):
        dem_service.corrected_profile(LAT, LON, CFG)
    serve(monkeypatch, lambda lat, lon: None if lat > 45.10 else 800.0)
    with pytest.raises(dem_service.DemCoverageError):
        dem_service.corrected_profile(LAT, LON, CFG)


def test_degenerate_traces(monkeypatch):
    serve(monkeypatch, lambda lat, lon: 800.0)
    with pytest.raises(dem_service.DemError, match='vide'):  # avant : IndexError
        dem_service.corrected_profile([], [], CFG)
    assert dem_service.corrected_profile([45.0], [6.0], CFG).tolist() == [800.0]  # avant : « hors couverture »
    assert dem_service.corrected_profile([45.0, 45.0], [6.0, 6.0], CFG).tolist() == [800.0, 800.0]
    serve(monkeypatch, lambda lat, lon: None)
    with pytest.raises(dem_service.DemCoverageError):
        dem_service.corrected_profile([45.0], [6.0], CFG)


# ----------------------------------------------------- Pannes, tentatives, échéance

def test_transient_failure_is_retried(monkeypatch):
    calls = script(monkeypatch, dem_service.requests.ConnectionError('coupure'), Response({}, 503), ok)
    assert dem_service.fetch_elevations([45.0, 45.1], [6.0, 6.0], CFG) == [800.0, 800.0]
    assert len(calls) == 3


def test_retries_are_bounded(monkeypatch):
    calls = script(monkeypatch, dem_service.requests.Timeout('lent'))
    with pytest.raises(dem_service.DemError, match='3 tentative'):
        dem_service.fetch_elevations([45.0], [6.0], CFG)
    assert len(calls) == 3
    calls = script(monkeypatch, dem_service.requests.Timeout('lent'))
    with pytest.raises(dem_service.DemError):
        dem_service.fetch_elevations([45.0], [6.0], {**CFG, 'DEM_RETRIES': 0})
    assert len(calls) == 1


def test_client_error_is_not_retried_and_does_not_leak_positions(monkeypatch):
    calls = script(monkeypatch, Response({}, 400))
    with pytest.raises(dem_service.DemError) as error:
        dem_service.fetch_elevations([45.0], [6.0], CFG)
    assert len(calls) == 1
    assert '400' in str(error.value) and 'locations' not in str(error.value)


def test_global_time_budget_stops_a_long_trace(monkeypatch):
    # Avant : 500 requêtes séquentielles sans borne (125 min au pire pour 1100 km)
    clock = [1000.0]
    monkeypatch.setattr(dem_service.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(dem_service.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def slow(count):
        clock[0] += 4.0  # chaque lot répond en 4 s
        return ok(count)
    calls = script(monkeypatch, slow)
    lat = [45 + i * 0.0002 for i in range(5000)]
    with pytest.raises(dem_service.DemError, match='Budget de temps'):
        dem_service.corrected_profile(lat, [6.0] * 5000, {**CFG, 'DEM_TOTAL_TIMEOUT': 10})
    assert len(calls) == 3  # 12 s écoulées : le quatrième lot n'est pas demandé


def test_request_timeout_never_exceeds_what_is_left(monkeypatch):
    seen = []
    serve(monkeypatch, lambda lat, lon: 800.0, calls=seen)
    dem_service.fetch_elevations([45.0], [6.0], {**CFG, 'DEM_TIMEOUT': 15, 'DEM_TOTAL_TIMEOUT': 5})
    assert 0 < seen[0]['timeout'] <= 5 and seen[0]['allow_redirects'] is False


def test_url_must_be_http(monkeypatch):
    for url in ('file:///etc/passwd', 'ftp://dem.test', '', None):
        with pytest.raises(dem_service.DemError):
            dem_service.fetch_elevations([45.0], [6.0], {**CFG, 'DEM_URL': url})


# ---------------------------------------------------------------------- Dalles

TIFF = b'II*\x00' + b'\x00' * 4096


class TileResponse:
    def __init__(self, status, body=b'', length=None, headers=True):
        self.status_code, self.body = status, body
        self.headers = {'Content-Length': str(len(body) if length is None else length)} if headers else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = dem_tiles.requests.HTTPError(str(self.status_code))
            error.response = self
            raise error

    def iter_content(self, _size):
        yield self.body


def tiles(monkeypatch, *responses):
    """Une réponse (ou exception) par appel, la dernière se répète ; sans attente entre les tentatives."""
    calls = []

    def get(self, url, **kwargs):
        step = responses[min(len(calls), len(responses) - 1)]
        calls.append(url)
        if isinstance(step, Exception):
            raise step
        return step
    monkeypatch.setattr(dem_tiles.requests.Session, 'get', get)
    monkeypatch.setattr(dem_tiles, 'RETRY_BACKOFF_S', 0)
    return calls


ONE_TILE = (45.2, 5.5, 45.4, 5.6)


def test_bbox_with_integer_upper_bounds_takes_no_extra_row_or_column():
    # Avant : 204 dalles pour l'exemple documenté, dont 28 (~1,3 Go) hors de l'emprise
    assert len(dem_tiles.tiles_for_bbox((41, -6, 52, 10))) == 176
    assert dem_tiles.tiles_for_bbox((41, -6, 52, 10)) == dem_tiles.tiles_for_bbox((41, -6, 51.99, 9.99))
    assert dem_tiles.tiles_for_bbox((45, 6, 45, 6)) == [(45, 6)]            # emprise réduite à un point
    assert dem_tiles.tiles_for_bbox((45, 6, 46, 7)) == [(45, 6)]
    assert dem_tiles.tiles_for_bbox((-0.5, -0.5, 0.5, 0.5)) == [(-1, -1), (-1, 0), (0, -1), (0, 0)]
    assert dem_tiles.tiles_for_bbox((89.5, 179.5, 90, 180)) == [(89, 179)]
    assert dem_tiles.tiles_for_bbox((-90, -180, -89, -179)) == [(-90, -180)]


@pytest.mark.parametrize('body, headers', [(b'<html>portail captif</html>' + b' ' * 4096, False),
                                           (b'', False), (b'II*\x00', True), (b'PK\x03\x04' + b'\x00' * 4096, True)])
def test_content_that_is_not_a_tile_is_refused(tmp_path, monkeypatch, body, headers):
    # Avant : une page HTML en 200 ou un corps vide étaient enregistrés comme dalle, « présente » pour toujours
    calls = tiles(monkeypatch, TileResponse(200, body, headers=headers))
    with pytest.raises(dem_tiles.TileError):
        list(dem_tiles.fetch(ONE_TILE, tmp_path))
    assert len(calls) == dem_tiles.ATTEMPTS and list(tmp_path.iterdir()) == []


def test_invalid_tile_already_on_disk_is_downloaded_again(tmp_path, monkeypatch):
    (tmp_path / 'N45E005.tif').write_bytes(b'<html>ancien portail captif</html>')
    tiles(monkeypatch, TileResponse(200, TIFF))
    assert dict(dem_tiles.fetch(ONE_TILE, tmp_path)) == {'N45E005': 'downloaded'}
    assert (tmp_path / 'N45E005.tif').read_bytes() == TIFF
    calls = tiles(monkeypatch, TileResponse(500))
    assert dict(dem_tiles.fetch(ONE_TILE, tmp_path)) == {'N45E005': 'present'} and calls == []


def test_transient_tile_failures_are_retried(tmp_path, monkeypatch):
    calls = tiles(monkeypatch, dem_tiles.requests.ConnectionError('coupure'), TileResponse(503),
                  TileResponse(200, TIFF, length=99999), TileResponse(200, TIFF))
    monkeypatch.setattr(dem_tiles, 'ATTEMPTS', 4)
    assert dict(dem_tiles.fetch(ONE_TILE, tmp_path)) == {'N45E005': 'downloaded'}
    assert len(calls) == 4 and not list(tmp_path.glob('*.part'))


def test_permanent_tile_errors_are_not_retried(tmp_path, monkeypatch):
    calls = tiles(monkeypatch, TileResponse(403))
    with pytest.raises(dem_tiles.requests.HTTPError):
        list(dem_tiles.fetch(ONE_TILE, tmp_path))
    assert len(calls) == 1
    calls = tiles(monkeypatch, TileResponse(302))  # redirection non suivie : pas une dalle
    with pytest.raises(dem_tiles.TileError):
        list(dem_tiles.fetch(ONE_TILE, tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_tile_is_synced_to_disk_before_it_is_published(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = dem_tiles.os.fsync, dem_tiles.os.replace
    monkeypatch.setattr(dem_tiles.os, 'fsync', lambda fd: (order.append('fsync'), real_fsync(fd))[1])
    monkeypatch.setattr(dem_tiles.os, 'replace', lambda src, dst: (order.append('replace'), real_replace(src, dst))[1])
    tiles(monkeypatch, TileResponse(200, TIFF))
    list(dem_tiles.fetch(ONE_TILE, tmp_path))
    assert order[:2] == ['fsync', 'replace'] and order.count('fsync') == 2  # fichier, puis dossier


def test_oversized_tile_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(dem_tiles, 'MAX_TILE_BYTES', 2048)
    tiles(monkeypatch, TileResponse(200, TIFF))
    with pytest.raises(dem_tiles.TileError, match='volumineuse'):
        list(dem_tiles.fetch(ONE_TILE, tmp_path))
    assert list(tmp_path.iterdir()) == []
