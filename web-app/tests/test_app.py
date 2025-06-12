"""
Tests de base pour l'application.
"""

import pytest


def test_app_creation(app):
    """Test que l'application se crée correctement."""
    assert app is not None
    assert app.config['TESTING'] is True


def test_main_route(client):
    """Test de la route principale."""
    response = client.get('/')
    assert response.status_code == 200
    assert b'Visualiseur KML' in response.data


def test_api_health(client):
    """Test de l'endpoint de santé de l'API."""
    response = client.get('/api/health')
    assert response.status_code == 200
    
    json_data = response.get_json()
    assert json_data['status'] == 'healthy'
    assert json_data['version'] == '2.0.0'
    assert json_data['api'] == 'KML/GPX Viewer API'


def test_api_sample_files(client):
    """Test de l'endpoint de listing des fichiers d'exemple."""
    response = client.get('/api/sample-files')
    assert response.status_code == 200
    
    json_data = response.get_json()
    assert 'files' in json_data
    assert isinstance(json_data['files'], list)


def test_api_upload_no_file(client):
    """Test de l'upload sans fichier."""
    response = client.post('/api/upload')
    assert response.status_code == 400
    
    json_data = response.get_json()
    assert json_data['success'] is False
    assert 'Aucun fichier sélectionné' in json_data['error']


def test_api_multi_upload(client):
    """Test de l'upload multiple."""
    from io import BytesIO

    kml = """<?xml version='1.0' encoding='UTF-8'?><kml xmlns='http://www.opengis.net/kml/2.2'><Document></Document></kml>"""
    gpx = """<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'><trk><trkseg><trkpt lat='0' lon='0'><ele>0</ele><time>2020-01-01T00:00:00Z</time></trkpt></trkseg></trk></gpx>"""

    data = {
        'files': [
            (BytesIO(kml.encode('utf-8')), 'a.kml'),
            (BytesIO(gpx.encode('utf-8')), 'b.gpx'),
        ]
    }

    response = client.post('/api/multi-upload', data=data, content_type='multipart/form-data')
    assert response.status_code == 200
    json_data = response.get_json()
    assert 'files' in json_data
    assert len(json_data['files']) == 2
    assert all(f['success'] for f in json_data['files'])


def test_api_upload_empty_filename(client):
    """Test de l'upload avec nom de fichier vide."""
    data = {'file': (None, '')}
    response = client.post('/api/upload', data=data)
    assert response.status_code == 400
    
    json_data = response.get_json()
    assert json_data['success'] is False
    assert 'Aucun fichier sélectionné' in json_data['error']
