from app.services.cache_service import parse_kml_cached, parse_gpx_cached, clear_cache


def test_parse_kml_cached_same_object():
    clear_cache()
    kml = """<?xml version='1.0' encoding='UTF-8'?><kml xmlns='http://www.opengis.net/kml/2.2'>
    <Document><Placemark><name>A</name><Point><coordinates>0,0,0</coordinates></Point></Placemark></Document></kml>"""
    res1 = parse_kml_cached(kml)
    res2 = parse_kml_cached(kml)
    assert res1 is res2
    assert res1['success']


def test_parse_gpx_cached_same_object():
    clear_cache()
    gpx = """<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'>
    <trk><name>T</name><trkseg><trkpt lat='0' lon='0'><ele>0</ele><time>2020-01-01T00:00:00Z</time></trkpt></trkseg></trk></gpx>"""
    r1 = parse_gpx_cached(gpx)
    r2 = parse_gpx_cached(gpx)
    assert r1 is r2
    assert r1['success']
