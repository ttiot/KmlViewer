from app.services.gpx_parser import GPXParser


def test_parse_gpx_basic():
    gpx = """<?xml version='1.0' encoding='UTF-8'?>
<gpx xmlns='http://www.topografix.com/GPX/1/1' version='1.1' creator='test'>
  <trk>
    <name>Test</name>
    <trkseg>
      <trkpt lat='0.0' lon='0.0'>
        <ele>0</ele>
        <time>2020-01-01T00:00:00Z</time>
      </trkpt>
      <trkpt lat='0.0' lon='0.1'>
        <ele>0</ele>
        <time>2020-01-01T00:10:00Z</time>
      </trkpt>
    </trkseg>
  </trk>
</gpx>
"""
    result = GPXParser.parse_gpx_coordinates(gpx)
    assert result['success'] is True
    assert any(f['type'] == 'polyline' for f in result['features'])
    assert result['total_points'] == 2
    speeds = [p['parsed_info']['speed_kmh'] for p in result['points'] if p['parsed_info']['speed_kmh']]
    assert speeds and speeds[0] is not None


def _gpx(points):
    rows = ''.join(f"<trkpt lat='{lat}' lon='{lon}'>" + (f"<time>{stamp}</time>" if stamp else '') + "</trkpt>"
                   for lat, lon, stamp in points)
    return ("<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'>"
            f"<trk><trkseg>{rows}</trkseg></trk></gpx>")


def test_point_speed_comes_from_the_previous_point():
    # 0,1° de longitude à l'équateur = 11,12 km ; en 10 min : 66,7 km/h
    result = GPXParser.parse_gpx_coordinates(_gpx([
        (0.0, 0.0, '2020-01-01T00:00:00Z'),
        (0.0, 0.1, '2020-01-01T00:10:00Z'),
        (0.0, 0.2, '2020-01-01T00:10:00Z'),   # même instant que le précédent : pas de vitesse
        (0.0, 0.3, None),                     # sans horodatage : pas de vitesse
        (0.0, 0.4, '2020-01-01T00:40:00+00:00'),
        (0.0, 0.5, '2020-01-01T02:50:00+02:00'),  # 00:50 UTC
    ]))
    speeds = [p['parsed_info']['speed_kmh'] for p in result['points']]
    assert speeds[0] is None and speeds[2] is None and speeds[3] is None and speeds[4] is None
    assert abs(speeds[1] - 66.7) < 0.1 and abs(speeds[5] - 66.7) < 0.1
    assert result['points'][1]['parsed_info']['speed_kts'] == round(speeds[1] / 1.852, 1)


def test_long_trace_gets_a_speed_for_every_point():
    """25 000 points : la vitesse est calculée en une passe (plus d'appel géodésique par point)."""
    points = [(45 + i * 0.00003, 6.0, f"2020-01-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z") for i in range(25_000)]
    result = GPXParser.parse_gpx_coordinates(_gpx(points))
    assert result['success'] and result['total_points'] == 25_000
    speeds = [p['parsed_info']['speed_kmh'] for p in result['points'][1:]]
    assert all(abs(speed - 12.0) < 0.1 for speed in speeds)  # 3,34 m par seconde

