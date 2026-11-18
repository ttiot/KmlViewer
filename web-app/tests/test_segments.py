from app.services import ingest_service, segment_service
from tests.conftest import line, make_gpx


def _coords(points):
    return [p[0] for p in points], [p[1] for p in points]


def test_find_efforts_single_pass():
    track = line(100)
    seg_lat, seg_lon = _coords(track[20:60])
    lat, lon = _coords(track)
    t = [i * 10.0 for i in range(100)]
    efforts = segment_service.find_efforts(seg_lat, seg_lon, lat, lon, t)
    assert len(efforts) == 1
    start, end, elapsed = efforts[0]
    assert (start, end) == (20, 59) and elapsed == 390


def test_reverse_direction_does_not_match():
    track = line(100)
    seg_lat, seg_lon = _coords(track[20:60])
    lat, lon = _coords(list(reversed(track)))
    assert segment_service.find_efforts(seg_lat, seg_lon, lat, lon, [i * 10.0 for i in range(100)]) == []


def test_leaving_the_segment_does_not_match():
    track = line(100)
    seg_lat, seg_lon = _coords(track[20:60])
    detour = track[:40] + line(40, lat0=track[40][0], lon0=6.0, dlat=0.0, dlon=0.0005)
    lat, lon = _coords(detour)
    assert segment_service.find_efforts(seg_lat, seg_lon, lat, lon, [i * 10.0 for i in range(len(detour))]) == []


def test_two_laps_with_gps_offset():
    out = line(60)
    back = list(reversed(out))
    shifted = [(la, lo + 0.00012, e) for la, lo, e in out]  # ~9 m de décalage latéral
    laps = out + back + shifted
    seg_lat, seg_lon = _coords(out[10:50])
    lat, lon = _coords(laps)
    efforts = segment_service.find_efforts(seg_lat, seg_lon, lat, lon, [i * 5.0 for i in range(len(laps))])
    assert [(s, e) for s, e, _ in efforts] == [(10, 49), (130, 169)]


def test_segment_lifecycle(app, db, local_user_id):
    ingest = lambda pts, step: ingest_service.ingest_bytes(  # noqa: E731
        db, app.config, local_user_id, make_gpx(pts, step_s=step).encode(), f'r{step}.gpx', 'upload')
    slow = ingest(line(100), 10)
    segment_id = segment_service.create(db, local_user_id, 'Montée test', slow.activity_id, 20, 60)
    assert len(segment_service.detail(db, local_user_id, segment_id)['efforts']) == 1

    # Une nouvelle trace plus rapide est chronométrée automatiquement à l'ingestion
    fast = ingest(line(100), 8)
    efforts = segment_service.detail(db, local_user_id, segment_id)['efforts']
    assert [e['activity_id'] for e in efforts] == [fast.activity_id, slow.activity_id]
    assert efforts[0]['elapsed_s'] < efforts[1]['elapsed_s'] and efforts[0]['rank'] == 1

    listing = segment_service.list_for_user(db, local_user_id)[0]
    assert listing['nb_efforts'] == 2 and listing['best_s'] == efforts[0]['elapsed_s']
    assert segment_service.delete(db, local_user_id, segment_id)
    assert db.execute("SELECT COUNT(*) FROM efforts").fetchone()[0] == 0
