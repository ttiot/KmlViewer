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
