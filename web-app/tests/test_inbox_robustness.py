"""
Ingestion et dossier surveillé face aux cas tordus : un fichier ne fait jamais tomber le
lot ni le passage, rien ne reste à moitié enregistré, et un dossier d'inbox n'est rattaché
qu'à un compte sans ambiguïté.
"""

import io
import json
import os
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest

from app.auth import users
from app.db import connect
from app.services import activity_store, dashboard_service, ingest_service, inbox_watcher, segment_service
from tests.conftest import line, login_as, make_gpx

KML_LINE = ('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>t</name><LineString>'
            '<coordinates>%s</coordinates></LineString></Placemark></Document></kml>')


def _ingest(db, app, user_id, raw, filename='run.gpx', **kwargs):
    return ingest_service.ingest_bytes(db, app.config, user_id, raw, filename, 'upload', **kwargs)


def _raw_files(app):
    return sorted(p.name for p in (Path(app.config['DATA_DIR']) / 'files').rglob('*') if p.is_file())


def _count(db, table='activities'):
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _corrupted_kmz():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('doc.kml', KML_LINE % ('6,45,0 ' * 4000))
    raw = bytearray(buffer.getvalue())
    raw[60:80] = b'\xff' * 20
    return bytes(raw)


def _drop(path, content, age=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


# ------------------------------------------------------------------ ingest_bytes ne lève pas

def test_ingest_never_raises_and_leaves_nothing_behind(app, db, local_user_id):
    hostile = {
        'altitude infinie': (make_gpx(line(30)).replace('<ele>100.0</ele>', '<ele>inf</ele>', 1).encode(), 'a.gpx'),
        'altitude 1e308': (make_gpx(line(30)).replace('<ele>100.0</ele>', '<ele>1e308</ele>', 1).encode(), 'b.gpx'),
        'kmz corrompu': (_corrupted_kmz(), 'c.kmz'),
        'pas du xml': (b'\x00\x01\x02', 'd.kml'),
        'un seul point': (make_gpx(line(1)).encode(), 'e.gpx'),
    }
    for label, (raw, name) in hostile.items():
        result = _ingest(db, app, local_user_id, raw, name)
        assert result.status == 'error' and result.message and not result.retryable, label
    assert _count(db) == 0 and _raw_files(app) == []


def test_batch_survives_a_corrupted_archive(client, db):
    data = {'files': [(io.BytesIO(make_gpx(line(30)).encode()), 'bon.gpx'), (io.BytesIO(_corrupted_kmz()), 'x.kmz'),
                      (io.BytesIO(make_gpx(line(31)).encode()), 'bon2.gpx')]}
    response = client.post('/api/activities', data=data, content_type='multipart/form-data')
    assert response.status_code == 200  # avant : 500 pour tout le lot, le premier fichier déjà enregistré
    assert [r['status'] for r in response.get_json()['results']] == ['created', 'error', 'created']


@pytest.mark.parametrize('stamp', ['9999-12-31T23:59:59-05:00', '0001-01-01T00:00:00Z', '1970-01-01T00:00:00Z', '2999-01-01T00:00:00Z'])
def test_implausible_dates_are_treated_as_unknown(app, db, local_user_id, client, stamp):
    # Avant : ValueError hors de tout try (an 10000), fichier brut orphelin ; ou date « 1-01-01 » qui cassait le tableau de bord
    raw = make_gpx(line(30)).replace('2025-06-01T08:00:00Z', stamp).encode()
    before = time.time()
    result = _ingest(db, app, local_user_id, raw)
    assert result.status == 'created', result.message
    row = activity_store.get(db, local_user_id, result.activity_id)
    assert row['start_time_known'] == 0
    assert before - 5 <= ingest_service.activity_stats.parse_timestamp(row['start_time']) <= time.time() + 5
    assert client.get('/api/dashboard').status_code == 200


def test_unexpected_failure_is_reported_not_raised(app, db, local_user_id, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError('secret interne')
    monkeypatch.setattr(ingest_service.activity_stats, 'compute_summary', boom)
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode())
    assert result.status == 'error' and 'secret' not in result.message
    assert _count(db) == 0 and _raw_files(app) == []


def test_segment_matching_failure_does_not_lose_the_activity(app, db, local_user_id, monkeypatch):
    monkeypatch.setattr(segment_service, 'match_activity', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode())
    assert result.status == 'created' and _count(db) == 1 and len(_raw_files(app)) == 1


def test_long_filename_keeps_its_extension(app, db, local_user_id):
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode(), 'a' * 250 + '.gpx')
    assert result.status == 'created'
    row = activity_store.get(db, local_user_id, result.activity_id)
    assert len(row['filename']) == 200 and row['filename'].endswith('.gpx')
    # Le fichier d'origine se retrouve (analyse, ré-ouverture) et se supprime avec l'activité
    assert ingest_service.parse_raw(activity_store.raw_file_path(
        app.config['DATA_DIR'], local_user_id, row['sha256'], 'gpx').read_bytes(), row['filename'])['success']
    assert activity_store.delete(db, local_user_id, result.activity_id, app.config['DATA_DIR']) and _raw_files(app) == []


# ------------------------------------------------------------------ Ligne et fichier, ensemble ou rien

def test_file_write_failure_leaves_no_row(app, db, local_user_id, monkeypatch):
    def full_disk(*_args, **_kwargs):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(activity_store, 'write_raw_file', full_disk)
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode())
    assert result.status == 'error' and result.retryable and 'stockage' in result.message
    assert _count(db) == 0 and not db.in_transaction


def test_database_failure_leaves_no_orphan_file(app, db, local_user_id, monkeypatch):
    def refused(*_args, **_kwargs):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr(activity_store, 'insert', refused)
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode())
    assert result.status == 'error' and result.retryable
    assert _raw_files(app) == []

    # Échec au commit, fichier déjà écrit : il est retiré
    monkeypatch.undo()

    class FailingCommit:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def commit(self):
            raise sqlite3.OperationalError('disk I/O error')

    result = _ingest(FailingCommit(db), app, local_user_id, make_gpx(line(31)).encode())
    assert result.status == 'error' and result.retryable
    assert _raw_files(app) == [] and _count(db) == 0


def test_integrity_error_is_not_always_a_duplicate(app, db, local_user_id, monkeypatch):
    monkeypatch.setattr(activity_store, 'insert', lambda *a, **k: (_ for _ in ()).throw(sqlite3.IntegrityError('NOT NULL')))
    result = _ingest(db, app, local_user_id, make_gpx(line(30)).encode())
    assert result.status == 'error' and _raw_files(app) == []


def test_raw_file_is_written_atomically(tmp_path):
    target = tmp_path / 'u' / ('a' * 64 + '.gpx')
    activity_store.write_raw_file(target, b'contenu')
    assert target.read_bytes() == b'contenu' and oct(target.stat().st_mode)[-3:] == '600'
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # pas de fichier partiel laissé
    activity_store.write_raw_file(target, b'autre')
    assert target.read_bytes() == b'autre'


def test_raw_file_reference_is_validated(tmp_path):
    for sha, ext in (('a' * 64, 'exe'), ('../' + 'a' * 61, 'gpx'), ('a' * 63, 'gpx'), ('A' * 64, 'gpx')):
        with pytest.raises(ValueError):
            activity_store.raw_file_path(str(tmp_path), 1, sha, ext)


def test_delete_and_reimport_cannot_cross(app, db, local_user_id):
    """
    Suppression pendant un ré-import du même fichier : le fichier d'origine est retiré sous
    le verrou d'écriture, que le ré-import doit obtenir avant d'écrire le sien. L'activité
    recréée a donc toujours son fichier.
    """
    raw = make_gpx(line(30)).encode()
    first = _ingest(db, app, local_user_id, raw)
    other = connect(app.config['DB_PATH'])
    other.execute("PRAGMA busy_timeout = 200")
    real_remove = activity_store.remove_raw_file
    seen = {}

    def remove_while_reimporting(path):
        # À cet instant la ligne est supprimée mais non validée : le ré-import voit encore
        # l'ancienne activité (doublon) ou attend le verrou, il n'écrit rien
        seen['result'] = ingest_service.ingest_bytes(other, app.config, local_user_id, raw, 'run.gpx', 'upload')
        real_remove(path)
    activity_store.remove_raw_file = remove_while_reimporting
    try:
        assert activity_store.delete(db, local_user_id, first.activity_id, app.config['DATA_DIR'])
    finally:
        activity_store.remove_raw_file = real_remove
        other.close()
    assert seen['result'].status == 'duplicate'
    assert _count(db) == 0 and _raw_files(app) == []
    again = _ingest(db, app, local_user_id, raw)
    assert again.status == 'created' and len(_raw_files(app)) == 1


def test_delete_of_a_missing_activity(app, db, local_user_id):
    assert activity_store.delete(db, local_user_id, 9999, app.config['DATA_DIR']) is False


# ------------------------------------------------------------------ Valeurs non finies déjà en base

def test_non_finite_values_never_reach_the_client(app, db, local_user_id, client):
    result = _ingest(db, app, local_user_id, (KML_LINE % ' '.join(
        f"6,{45 + i * 0.0003},{'inf' if i == 10 else 100}" for i in range(40))).encode(), 'inf.kml')
    row = activity_store.get(db, local_user_id, result.activity_id)
    assert result.status == 'created' and (row['ascent_m'], row['descent_m']) == (0.0, 0.0)  # point non fini ignoré

    # Ligne héritée d'avant le correctif : ni la liste ni le tableau de bord ne doivent en souffrir
    other = _ingest(db, app, local_user_id, make_gpx(line(40)).encode())
    db.execute("UPDATE activities SET ascent_m = ?, descent_m = ?, distance_m = ?, moving_s = ?, best_efforts = ? WHERE id = ?",
               (float('inf'), float('inf'), float('inf'), float('inf'), json.dumps({'1000': float('inf')}), other.activity_id))
    db.execute("UPDATE activities SET start_time = '1-01-01T00:00:00Z' WHERE id = ?", (result.activity_id,))
    db.commit()
    strict = lambda response: json.loads(response.data, parse_constant=lambda name: pytest.fail(f'{name} dans le JSON'))  # noqa: E731
    listing = client.get('/api/activities')
    assert listing.status_code == 200 and len(strict(listing)['activities']) == 2
    assert strict(client.get(f'/api/activities/{other.activity_id}'))['activity']['ascent_m'] is None
    for period in dashboard_service.PERIODS:
        response = client.get(f'/api/dashboard?period={period}')
        assert response.status_code == 200, period
        assert strict(response)['stats']['totals']['count'] == 1  # la ligne à la date illisible est ignorée


# ------------------------------------------------------------------ Dossier surveillé

def test_undecodable_filename_does_not_stop_the_scan(app, db, local_user_id):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'a.gpx', make_gpx(line(30)))
    bad = os.path.join(os.fsencode(inbox), b'm\xe9chant.gpx')  # nom latin-1, illisible en UTF-8
    try:
        with open(bad, 'wb') as handle:
            handle.write(make_gpx(line(31)).encode())
    except OSError:
        pytest.skip("le système de fichiers refuse les noms non UTF-8")
    os.utime(bad, (time.time() - 60, time.time() - 60))
    _drop(inbox / 'zz_apres.gpx', make_gpx(line(32)))

    warned = set()
    counts = inbox_watcher.scan_once(db, app.config, warned)  # avant : UnicodeEncodeError, passage interrompu
    assert counts == {'created': 2, 'duplicate': 0, 'error': 0, 'skipped': 1}
    assert {r['path'] for r in db.execute("SELECT path FROM inbox_files")} == {'a.gpx', 'zz_apres.gpx'}
    assert inbox_watcher.scan_once(db, app.config, warned)['skipped'] == 1


def test_one_failing_file_does_not_stop_the_scan(app, db, local_user_id, monkeypatch):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'a.gpx', make_gpx(line(30)))
    _drop(inbox / 'b.gpx', make_gpx(line(31)))
    real = inbox_watcher._resolve_owner

    def flaky(db_, cfg, folder):
        if not flaky.done:
            flaky.done = True
            raise RuntimeError('imprévu')
        return real(db_, cfg, folder)
    flaky.done = False
    monkeypatch.setattr(inbox_watcher, '_resolve_owner', flaky)
    assert inbox_watcher.scan_once(db, app.config) == {'created': 1, 'duplicate': 0, 'error': 1, 'skipped': 0}
    assert inbox_watcher.scan_once(db, app.config)['created'] == 1  # le fichier en échec est repris


def test_retryable_errors_are_not_remembered(app, db, local_user_id, monkeypatch):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'quota.gpx', make_gpx(line(30)))
    _drop(inbox / 'casse.gpx', '<gpx')
    app.config['USER_QUOTA_MB'] = 1
    monkeypatch.setattr(activity_store, 'user_storage_bytes', lambda *a: 10 ** 9)  # quota plein
    assert inbox_watcher.scan_once(db, app.config) == {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 2}
    assert _count(db, 'inbox_files') == 0

    monkeypatch.undo()  # l'utilisateur a fait de la place : avant, le fichier restait « error » jusqu'à un touch
    app.config['USER_QUOTA_MB'] = 1
    assert inbox_watcher.scan_once(db, app.config) == {'created': 1, 'duplicate': 0, 'error': 1, 'skipped': 0}
    assert {r['path']: r['status'] for r in db.execute("SELECT path, status FROM inbox_files")} == {
        'quota.gpx': 'created', 'casse.gpx': 'error'}
    assert inbox_watcher.scan_once(db, app.config)['error'] == 0  # l'erreur du fichier, elle, est définitive


def test_storage_failure_is_retried(app, db, local_user_id, monkeypatch):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'a.gpx', make_gpx(line(30)))
    monkeypatch.setattr(activity_store, 'write_raw_file', lambda *a: (_ for _ in ()).throw(OSError('disque')))
    assert inbox_watcher.scan_once(db, app.config)['skipped'] == 1 and _count(db, 'inbox_files') == 0
    monkeypatch.undo()
    assert inbox_watcher.scan_once(db, app.config)['created'] == 1


def test_symlink_swapped_in_after_listing_is_not_followed(app, db, local_user_id, tmp_path, monkeypatch):
    inbox = inbox_watcher.inbox_dir(app.config)
    secret = _drop(tmp_path / 'ailleurs' / 'prive.gpx', make_gpx(line(36), name='SECRET'))
    _drop(inbox / 'swap.gpx', make_gpx(line(37), name='leurre'))
    _drop(inbox / 'sous' / 'dossier.gpx', make_gpx(line(38), name='leurre2'))
    _drop(tmp_path / 'ailleurs' / 'dossier.gpx', make_gpx(line(39), name='SECRET2'))
    listed = list(inbox_watcher._iter_files(inbox))
    assert len(listed) == 2

    def racing(_root):
        # Entre le listage et la lecture : le fichier, puis un dossier du chemin, deviennent des liens
        (inbox / 'swap.gpx').unlink()
        (inbox / 'swap.gpx').symlink_to(secret)
        (inbox / 'sous' / 'dossier.gpx').unlink()
        (inbox / 'sous').rmdir()
        (inbox / 'sous').symlink_to(tmp_path / 'ailleurs', target_is_directory=True)
        yield from listed
    monkeypatch.setattr(inbox_watcher, '_iter_files', racing)
    counts = inbox_watcher.scan_once(db, app.config)
    assert counts == {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 0}
    assert _count(db) == 0 and _count(db, 'inbox_files') == 0


def test_special_files_are_ignored(app, db, local_user_id, monkeypatch):
    inbox = inbox_watcher.inbox_dir(app.config)
    target = _drop(inbox / 'tube.gpx', make_gpx(line(30)))
    listed = list(inbox_watcher._iter_files(inbox))
    target.unlink()
    os.mkfifo(target)  # tube nommé substitué : ne doit ni bloquer ni être lu
    monkeypatch.setattr(inbox_watcher, '_iter_files', lambda _root: iter(listed))
    assert inbox_watcher.scan_once(db, app.config) == {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 0}


def test_file_dated_in_the_future_is_ingested(app, db, local_user_id):
    inbox = inbox_watcher.inbox_dir(app.config)
    _drop(inbox / 'futur.gpx', make_gpx(line(30), with_time=False), age=-86400 * 30)  # horloge de l'émetteur en avance
    _drop(inbox / 'frais.gpx', make_gpx(line(31)), age=0)
    _drop(inbox / 'presque.gpx', make_gpx(line(32)), age=-2)
    assert inbox_watcher.scan_once(db, app.config)['created'] == 1  # avant : jamais ingéré, sans aucun log
    row = db.execute("SELECT start_time, start_time_known FROM activities").fetchone()
    # Sans date dans le fichier, la date de repli n'est pas dans le futur
    assert row['start_time_known'] == 0 and row['start_time'] <= activity_store.epoch_to_iso(time.time() + 5)


def test_oversized_file_is_refused_without_being_read(app, db, local_user_id):
    inbox = inbox_watcher.inbox_dir(app.config)
    app.config['MAX_CONTENT_LENGTH'] = 1000
    _drop(inbox / 'gros.gpx', make_gpx(line(200)))
    assert inbox_watcher.scan_once(db, app.config)['error'] == 1
    assert db.execute("SELECT error FROM inbox_files").fetchone()['error'] == 'Fichier trop volumineux'


# ------------------------------------------------------------------ Attribution des dossiers

def _scan(app):
    conn = connect(app.config['DB_PATH'])
    try:
        counts = inbox_watcher.scan_once(conn, app.config)
        owners = [(r['name'], r['user_id']) for r in conn.execute("SELECT name, user_id FROM activities ORDER BY id")]
        return counts, owners
    finally:
        conn.close()


def test_folder_goes_to_a_single_unambiguous_account(oidc_app):
    _, admin = login_as(oidc_app, 'admin')
    _, alice = login_as(oidc_app, 'alice', email='alice@example.com', email_verified=True)
    inbox = inbox_watcher.inbox_dir(oidc_app.config)
    _drop(inbox / 'ALICE' / 'a.gpx', make_gpx(line(30), name='par username'))
    _drop(inbox / 'alice@example.com' / 'b.gpx', make_gpx(line(31), name='par email'))
    counts, owners = _scan(oidc_app)
    assert counts['created'] == 2 and owners == [('par username', alice), ('par email', alice)]


def test_taking_someone_elses_name_gets_nothing(oidc_app):
    login_as(oidc_app, 'admin')
    _, alice = login_as(oidc_app, 'alice', email='alice@example.com', email_verified=True)
    inbox = inbox_watcher.inbox_dir(oidc_app.config)
    conn = connect(oidc_app.config['DB_PATH'])
    try:
        # Homonyme à la casse près ; username égal à l'email d'un autre ; compte inactif homonyme
        users.upsert_from_claims(conn, {'sub': 'bob', 'preferred_username': 'Alice'})
        users.upsert_from_claims(conn, {'sub': 'eve', 'preferred_username': 'alice@example.com'})
        users.upsert_from_claims(conn, {'sub': 'zoe', 'preferred_username': 'zoe'})
        users.upsert_from_claims(conn, {'sub': 'zoe2', 'preferred_username': 'ZOE'})
        conn.execute("UPDATE users SET is_active = 0 WHERE sub = 'zoe2'")
        users.upsert_from_claims(conn, {'sub': 'dan', 'preferred_username': 'dan'})
        conn.execute("UPDATE users SET is_active = 0 WHERE sub = 'dan'")
        conn.commit()
    finally:
        conn.close()
    for folder in ('alice', 'alice@example.com', 'zoe', 'dan', 'inconnu'):
        _drop(inbox / folder / 'x.gpx', make_gpx(line(30), name=folder))
    counts, owners = _scan(oidc_app)
    assert counts == {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 5} and owners == []


def test_default_user_must_be_unambiguous_too(oidc_app):
    _, alice = login_as(oidc_app, 'alice')
    oidc_app.config['INBOX_DEFAULT_USER'] = 'alice'
    inbox = inbox_watcher.inbox_dir(oidc_app.config)
    _drop(inbox / 'racine.gpx', make_gpx(line(30)))
    counts, owners = _scan(oidc_app)
    assert counts['created'] == 1 and owners[0][1] == alice
    login_as(oidc_app, 'alice-bis', preferred_username='Alice')
    _drop(inbox / 'racine2.gpx', make_gpx(line(31)))
    assert _scan(oidc_app)[0]['skipped'] == 1
