import os
import time

from app.services import activity_store, ingest_service, inbox_watcher
from tests.conftest import line, make_gpx

BOMB = b"""<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">
<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>&lol2;</name></trk></gpx>"""
XXE = b"""<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><name>&xxe;</name></trk></gpx>"""


def _ingest(db, app, user_id, raw, filename='run.gpx'):
    return ingest_service.ingest_bytes(db, app.config, user_id, raw, filename, 'upload')


def test_ingest_creates_then_deduplicates(app, db, local_user_id):
    raw = make_gpx(line(50)).encode()
    first = _ingest(db, app, local_user_id, raw)
    assert first.status == 'created'
    row = activity_store.get(db, local_user_id, first.activity_id)
    assert row['name'] == 'Sortie test' and row['activity_type'] == 'running'
    assert row['start_time'] == '2025-06-01T08:00:00Z' and row['elev_source'] == 'gps'
    stored = activity_store.raw_file_path(app.config['DATA_DIR'], local_user_id, row['sha256'], 'gpx')
    assert stored.read_bytes() == raw and oct(stored.stat().st_mode)[-3:] == '600'

    again = _ingest(db, app, local_user_id, raw, 'copie.gpx')
    assert again.status == 'duplicate' and again.activity_id == first.activity_id


def test_ingest_rejects_bad_input(app, db, local_user_id):
    assert _ingest(db, app, local_user_id, b'<gpx', 'x.gpx').status == 'error'
    assert _ingest(db, app, local_user_id, b'hello', 'x.exe').status == 'error'
    assert _ingest(db, app, local_user_id, b'', 'x.gpx').status == 'error'
    assert _ingest(db, app, local_user_id, b'\xff\xfe\x00', 'x.gpx').status == 'error'
    assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_ingest_rejects_xml_entity_attacks(app, db, local_user_id):
    for payload in (BOMB, XXE):
        result = _ingest(db, app, local_user_id, payload)
        assert result.status == 'error'
        assert 'root:' not in result.message


def test_filename_cannot_escape_storage(app, db, local_user_id):
    result = _ingest(db, app, local_user_id, make_gpx(line(5)).encode(), '../../etc/evil.gpx')
    assert result.status == 'created'
    assert activity_store.get(db, local_user_id, result.activity_id)['filename'] == 'evil.gpx'


def _drop(path, content, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    old = time.time() - age
    os.utime(path, (old, old))


def test_inbox_scan(app, db, local_user_id, tmp_path):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'a.gpx', make_gpx(line(30)))
    _drop(inbox / 'sub' / 'b.gpx', make_gpx(line(40)))
    _drop(inbox / 'cassé.gpx', '<gpx')
    _drop(inbox / 'notes.txt', 'rien')
    _drop(inbox / '.hidden.gpx', make_gpx(line(20)))
    _drop(inbox / 'frais.gpx', make_gpx(line(25)), age=0)  # encore en cours d'écriture
    secret = tmp_path / 'ailleurs.gpx'
    secret.write_text(make_gpx(line(35)))
    (inbox / 'lien.gpx').symlink_to(secret)

    counts = inbox_watcher.scan_once(db, app.config)
    assert counts == {'created': 2, 'duplicate': 0, 'error': 1, 'skipped': 0}
    assert inbox_watcher.scan_once(db, app.config)['created'] == 0  # idempotent
    assert (inbox / 'a.gpx').exists()  # fichiers jamais déplacés
    error = db.execute("SELECT error FROM inbox_files WHERE status = 'error'").fetchone()
    assert error['error']


def test_inbox_maps_folders_to_users(oidc_app, tmp_path):
    from app.db import connect
    from tests.conftest import login_as
    _, alice = login_as(oidc_app, 'alice')
    inbox = inbox_watcher.inbox_dir(oidc_app.config)
    _drop(inbox / 'alice' / 'a.gpx', make_gpx(line(30)))
    _drop(inbox / 'inconnu' / 'b.gpx', make_gpx(line(40)))
    _drop(inbox / 'racine.gpx', make_gpx(line(50)))

    conn = connect(oidc_app.config['DB_PATH'])
    try:
        counts = inbox_watcher.scan_once(conn, oidc_app.config)
        assert counts['created'] == 1 and counts['skipped'] == 2
        owners = [r[0] for r in conn.execute("SELECT user_id FROM activities")]
        assert owners == [alice]
    finally:
        conn.close()
