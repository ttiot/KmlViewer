import pytest

from app.services import dem_tiles


def test_bbox_parsing_and_validation():
    assert dem_tiles.parse_bbox('41,-6,52,10') == (41.0, -6.0, 52.0, 10.0)
    for bad in ('', '1,2,3', 'a,b,c,d', '52,-6,41,10', '0,0,91,0', '0,-181,1,0'):
        with pytest.raises(ValueError):
            dem_tiles.parse_bbox(bad)


def test_tiles_cover_bbox_including_negative_coordinates():
    assert dem_tiles.tiles_for_bbox((45.1, 5.6, 45.3, 5.8)) == [(45, 5)]
    assert dem_tiles.tiles_for_bbox((44.9, -0.5, 45.1, 0.5)) == [(44, -1), (44, 0), (45, -1), (45, 0)]
    # Une borne haute entière ne réclame pas la rangée (ni la colonne) suivante
    assert len(dem_tiles.tiles_for_bbox((41, -6, 52, 10))) == 11 * 16


def test_names_match_opentopodata_and_copernicus_conventions():
    assert dem_tiles.tile_name(45, 5) == 'N45E005'
    assert dem_tiles.tile_name(-34, -71) == 'S34W071'
    assert dem_tiles.tile_url(45, 5).endswith(
        '/Copernicus_DSM_COG_10_N45_00_E005_00_DEM/Copernicus_DSM_COG_10_N45_00_E005_00_DEM.tif')
    assert 'S34_00_W071_00' in dem_tiles.tile_url(-34, -71)


TIFF = b'II*\x00' + b'\x00' * 2048  # signature et taille minimale d'une dalle


class _Response:
    def __init__(self, status, body=b'', length=None):
        self.status_code, self.body = status, body
        self.headers = {'Content-Length': str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dem_tiles.requests.HTTPError(str(self.status_code))

    def iter_content(self, _size):
        yield self.body


def test_fetch_downloads_skips_and_handles_sea(tmp_path, monkeypatch):
    responses = {'N45E005': _Response(200, TIFF), 'N45E006': _Response(404)}
    monkeypatch.setattr(dem_tiles.requests.Session, 'get',
                        lambda self, url, **kw: next(r for n, r in responses.items()
                                                     if n[:3] + '_00_' + n[3:] in url))
    bbox = (45.2, 5.5, 45.4, 6.5)
    assert dict(dem_tiles.fetch(bbox, tmp_path)) == {'N45E005': 'downloaded', 'N45E006': 'absent'}
    assert (tmp_path / 'N45E005.tif').read_bytes() == TIFF
    responses['N45E005'] = _Response(500)  # dalle déjà là : plus aucun appel réseau pour elle
    assert dict(dem_tiles.fetch(bbox, tmp_path))['N45E005'] == 'present'
    assert not list(tmp_path.glob('*.part'))


def test_truncated_download_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dem_tiles, 'RETRY_BACKOFF_S', 0)
    monkeypatch.setattr(dem_tiles.requests.Session, 'get',
                        lambda self, url, **kw: _Response(200, b'court', length=9999))
    with pytest.raises(IOError):
        list(dem_tiles.fetch((45.2, 5.5, 45.4, 5.6), tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_oversized_bbox_is_refused(tmp_path):
    with pytest.raises(ValueError, match='trop grande'):
        list(dem_tiles.fetch((-60, -180, 60, 180), tmp_path))
