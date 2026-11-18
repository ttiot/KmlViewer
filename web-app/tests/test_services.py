"""
Tests pour les services de l'application.
"""

from app.services.kml_parser import KMLParser


def test_parse_gps_description():
    """Test du parsing de description GPS."""
    description = "Vitesse: 120 km/h, Cap: 180°"
    parsed = KMLParser.parse_gps_description(description, 45.0, 2.0, 1000.0)

    assert parsed['speed_kmh'] == 120.0
    assert parsed['speed_kts'] == round(120.0 / 1.852, 1)
    assert parsed['heading'] == 180.0
    assert parsed['latitude'] == 45.0
    assert parsed['longitude'] == 2.0
    assert parsed['altitude_m'] == 1000.0
    assert parsed['altitude_ft'] == round(1000.0 * 3.28084)


def test_parse_kml_coordinates_invalid_xml():
    """Test du parsing avec XML invalide."""
    invalid_kml = "<?xml version='1.0'?><invalid><unclosed>"
    result = KMLParser.parse_kml_coordinates(invalid_kml)

    assert result['success'] is False
    assert 'Erreur de parsing XML' in result['error']


def test_parse_kml_coordinates_empty():
    """Test du parsing avec KML vide mais valide."""
    empty_kml = """<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
        <Document>
        </Document>
    </kml>"""

    result = KMLParser.parse_kml_coordinates(empty_kml)

    assert result['success'] is True
    assert result['features'] == []
    assert result['points'] == []
    assert result['total_points'] == 0
