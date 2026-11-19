"""API du visualiseur : formats de fichiers, analyse, simplification, export, erreurs."""

import io
import re
import zipfile
from pathlib import Path

import pytest

from app.services import file_formats
from app.services.kml_editor import KMLEditor
from tests.conftest import line, login_as, make_gpx

KML = """<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>Refuge</name><description>Halte</description>
<Point><coordinates>6.0,45.0,1200</coordinates></Point></Placemark>
<Placemark><name>Sentier</name><LineString><coordinates>
6.0,45.0,1200 6.001,45.001,1210 6.002,45.002,1220 6.003,45.003,1230</coordinates></LineString></Placemark>
</Document></kml>"""
WEB_APP = Path(__file__).resolve().parent.parent


def _kmz(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _upload(client, raw, filename):
    return client.post('/api/upload', data={'file': (io.BytesIO(raw), filename)},
                       content_type='multipart/form-data')


def _features(client):
    return _upload(client, make_gpx(line(40)).encode(), 'trace.gpx').get_json()['features']


# ------------------------------------------------------------------ Formats

def test_kmz_is_unpacked(client):
    response = _upload(client, _kmz({'images/logo.png': 'x', 'doc.kml': KML}), 'sortie.kmz')
    assert response.status_code == 200
    data = response.get_json()
    assert data['success'] and {f['name'] for f in data['features']} == {'Refuge', 'Sentier'}


def test_kmz_rejections(client, monkeypatch):
    assert _upload(client, b'pas un zip', 'x.kmz').status_code == 400
    assert _upload(client, _kmz({'lisezmoi.txt': 'rien'}), 'x.kmz').status_code == 400
    monkeypatch.setattr(file_formats, 'MAX_KMZ_UNCOMPRESSED', 100)
    response = _upload(client, _kmz({'doc.kml': KML}), 'x.kmz')
    assert response.status_code == 400 and 'volumineuse' in response.get_json()['error']


def test_upload_rejects_unknown_extension_and_bad_encoding(client):
    assert _upload(client, b'abc', 'notes.txt').status_code == 400
    assert _upload(client, b'\xff\xfe\x00', 'trace.gpx').status_code == 400


def test_multi_upload_reports_each_file(client):
    data = {'files': [(io.BytesIO(make_gpx(line(10)).encode()), 'a.gpx'), (io.BytesIO(b'x'), 'b.txt'),
                      (io.BytesIO(_kmz({'doc.kml': KML})), 'c.kmz')]}
    files = client.post('/api/multi-upload', data=data, content_type='multipart/form-data').get_json()['files']
    assert [(f['filename'], f['success']) for f in files] == [('a.gpx', True), ('b.txt', False), ('c.kmz', True)]


def test_kmz_ingested_in_library(client):
    data = {'files': [(io.BytesIO(_kmz({'doc.kml': KML})), 'sortie.kmz')]}
    result = client.post('/api/activities', data=data, content_type='multipart/form-data').get_json()['results'][0]
    assert result['status'] == 'created'
    parsed = client.get(f"/api/activities/{result['activity_id']}/parsed").get_json()
    assert parsed['success'] and any(f['name'] == 'Sentier' for f in parsed['features'])


# ------------------------------------------------------------------ Analyse

def test_analysis_trajectory_and_advanced(client):
    features = _features(client)
    data = client.post('/api/analysis/trajectory', json={'features': features}).get_json()
    assert data['success'] and data['analysis']['distance']['total_distance_km'] > 1
    points = [f for f in features if f['type'] == 'marker']
    advanced = client.post('/api/analysis/advanced', json={'features': features, 'points': points})
    assert advanced.status_code == 200 and advanced.get_json()['success']


@pytest.mark.parametrize('path, body', [
    ('/api/analysis/trajectory', {}),
    ('/api/analysis/trajectory', {'features': 'abc'}),
    ('/api/analysis/elevation-profile', {'coordinates': [[45, 6, 100]]}),
    ('/api/analysis/distance', {}),
    ('/api/analysis/segments', {'coordinates': [[45, 6, 0], [45.1, 6, 0]]}),
    ('/api/analysis/stops', {'points': [], 'speed_threshold': 'vite'}),
    ('/api/analysis/advanced', {'features': 'abc'}),
    ('/api/editor/simplify-trace', {'coordinates': [[45, 6], [45.1, 6], ['a', 'b']]}),
    ('/api/editor/simplify-trace', {'coordinates': [[45, 6], [45.1, 6], [45.2, 6]], 'tolerance': -1}),
    ('/api/export/gpx', {}),
    ('/api/export/gpx', {'features': ['abc']}),
])
def test_invalid_payloads_are_rejected(client, path, body):
    response = client.post(path, json=body)
    assert response.status_code == 400
    assert response.get_json()['success'] is False


def test_analysis_coordinate_endpoints(client):
    coordinates = [[lat, lon, ele] for lat, lon, ele in line(20, ele=lambda i: 100.0 + i)]
    for path, key in (('elevation-profile', 'elevation_profile'), ('distance', 'distance'),
                      ('segments', 'segments'), ('terrain', 'terrain_analysis'),
                      ('points-of-interest', 'points_of_interest')):
        response = client.post(f'/api/analysis/{path}', json={'coordinates': coordinates})
        assert response.status_code == 200 and key in response.get_json(), path


# ------------------------------------------------------- Simplification, export

def test_simplify_keeps_shape_and_reports_indices(client):
    # Trace en L : seuls les deux bouts et l'angle (indice 49) portent la forme
    corner = 45.0 + 49 * 0.0001
    coordinates = [[45.0 + i * 0.0001, 6.0, 0] for i in range(50)] + [[corner, 6.0 + i * 0.0001, 0] for i in range(1, 50)]
    data = client.post('/api/editor/simplify-trace', json={'coordinates': coordinates}).get_json()
    assert data['success'] and data['simplified_points'] == 3 and data['kept_indices'] == [0, 49, 98]
    assert data['simplified_coordinates'] == [coordinates[i] for i in data['kept_indices']]


def test_simplify_handles_long_traces():
    coordinates = [[45.0 + i * 1e-5, 6.0 + (i % 2) * 1e-3, 0] for i in range(5000)]  # zigzag : rien à retirer
    assert len(KMLEditor.simplify_trace(coordinates, 1e-5)) == 5000


def test_exports(client):
    features = _features(client)
    features.append({'type': 'marker', 'name': 'Col <b>', 'description': 'Vue', 'coordinates': [45.0, 6.0],
                     'altitude': 1500, 'visibility': '1'})
    body = {'features': features, 'filename': '../ma sortie', 'metadata': {'name': 'Sortie'}}
    expected = {'gpx': b'<trkpt', 'kml': b'<gx:Track>', 'geojson': b'"LineString"', 'csv': b'TracePoint'}
    for fmt, marker in expected.items():
        response = client.post(f'/api/export/{fmt}', json=body)
        assert response.status_code == 200 and marker in response.data, fmt
        assert response.headers['Content-Disposition'] == f'attachment; filename=ma_sortie.{fmt}'
    gpx = client.post('/api/export/gpx', json=body).data
    # Les points de trace GPX (marqueurs masqués) ne deviennent pas des waypoints
    assert gpx.count(b'<wpt') == 1 and b'Col &lt;b&gt;' in gpx
    assert client.post('/api/export/pdf', json=body).status_code == 404
    assert set(client.get('/api/export/formats').get_json()['formats']) == set(expected)


# ------------------------------------------------------------- Erreurs, accès

def test_unexpected_error_is_logged_not_leaked(client, monkeypatch, caplog):
    def boom(_features):
        raise RuntimeError('secret interne')
    monkeypatch.setattr('app.api.analysis_routes.TrajectoryAnalyzer.analyze_trajectory', boom)
    response = client.post('/api/analysis/trajectory', json={'features': []})
    assert response.status_code == 500
    assert response.get_json() == {'success': False, 'error': 'Erreur interne du serveur'}
    assert 'secret interne' in caplog.text


def test_api_http_errors_are_json(client):
    response = client.get('/api/inexistant')
    assert response.status_code == 404 and response.get_json()['success'] is False
    assert client.get('/api/upload').status_code == 405
    assert client.get('/page-inexistante').status_code == 404


def test_stats_requires_admin(oidc_app):
    admin, _ = login_as(oidc_app, 'alice')  # premier compte : administrateur
    member, _ = login_as(oidc_app, 'bob')
    assert admin.get('/api/stats').status_code == 200
    assert member.get('/api/stats').status_code == 403


# ------------------------------------------------------------------ Front

def test_csp_allows_only_own_scripts(client):
    csp = client.get('/').headers['Content-Security-Policy']
    assert "script-src 'self';" in csp


def test_no_inline_handlers_nor_cdn():
    """La CSP interdit les scripts inline et tiers : templates et scripts doivent s'y tenir."""
    handler = re.compile(r'\son[a-z]+\s*=\s*["\']', re.IGNORECASE)
    sources = [*(WEB_APP / 'templates').rglob('*.html'), *(WEB_APP / 'static' / 'js').rglob('*.js')]
    assert len(sources) > 15
    for path in sources:
        text = path.read_text(encoding='utf-8')
        assert not handler.search(text), f'gestionnaire inline dans {path.name}'
        assert not re.search(r'<script(?![^>]*\ssrc=)[^>]*>', text), f'script inline dans {path.name}'
        assert not re.search(r'(src|href)="https?://', text), f'ressource externe dans {path.name}'


def test_vendor_checksums():
    import hashlib
    vendor = WEB_APP / 'static' / 'vendor'
    lines = (vendor / 'SHA256SUMS').read_text().splitlines()
    assert len(lines) >= 15
    for entry in lines:
        digest, name = entry.split(maxsplit=1)
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == digest, name


def test_library_save_is_driven_by_the_upload_not_by_a_second_listener():
    """
    L'écouteur du champ de saisie le vide après lecture : un second écouteur qui relirait
    input.files ne verrait rien, et la case « Enregistrer dans ma bibliothèque » resterait
    sans effet avec le sélecteur de fichiers. L'envoi doit donc passer les fichiers lui-même.
    """
    viewer = WEB_APP / 'static' / 'js' / 'viewer'
    assert 'saveToLibrary(files)' in (viewer / 'files.js').read_text(encoding='utf-8')
    library = (viewer / 'library.js').read_text(encoding='utf-8')
    assert 'input.files' not in library and "getElementById('fileInput')" not in library
