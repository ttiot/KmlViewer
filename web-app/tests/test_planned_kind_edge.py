"""
Cas limites de la nature des traces (TR4, RB-5.3 à RB-5.6, CA-R13, CA-R14, CA-R22, ADR-0009).

Complète test_planned_kind.py : chaque chemin qui lit `activities` ou `efforts` (API comprise) face
à une trace planifiée, `kind` invalide ou non modifiable, dossier surveillé, suppression, quota,
migration d'une base existante portant des données (activités, tronçons, efforts).
"""

import io
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app import create_app
from app import db as database
from app.config import TestingConfig
from app.services import activity_store, dashboard_service, ingest_service, inbox_watcher, segment_service
from tests.conftest import OIDC_OVERRIDES, line, login_as, make_gpx


def run(n=300, **kw):
    return make_gpx(line(n, dlat=0.0003), activity_type='running', **kw).encode()


def planned_gpx(n=300, dlat=0.0003, lon0=6.0, activity_type='run'):
    return make_gpx(line(n, dlat=dlat, lon0=lon0), activity_type=activity_type, with_time=False).encode()


@pytest.fixture
def user(db):
    db.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    db.commit()
    return db.execute("SELECT id FROM users WHERE sub = 'u'").fetchone()[0]


@pytest.fixture
def api(tmp_path):
    """Application OIDC (comptes distincts) et connexion de test."""
    return create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'oidc'), 'TRUSTED_HOSTS': ['localhost']})


def ingest(db, app, user, raw, name='a.gpx', *, source='upload', **kw):
    result = ingest_service.ingest_bytes(db, app.config, user, raw, name, source, **kw)
    assert result.status == 'created', result
    return result.activity_id


def save_planned(db, app, user, raw=None, name='plan.gpx'):
    return ingest(db, app, user, raw or planned_gpx(), name, source='route_builder', kind='planned')


def row(db, activity_id):
    return db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()


def api_session(app, sub, admin=False):
    client, uid = login_as(app, sub, admin=admin)
    conn = database.connect(app.config['DB_PATH'])
    return client, uid, conn


# --- Tous les lecteurs de `activities` face à une trace planifiée (RB-5.5) ---------------------------

def test_the_dashboard_api_is_byte_identical_before_and_after_a_planned_track(api):
    client, uid, conn = api_session(api, 'sub-dash')
    ingest(conn, api, uid, run(), 'real.gpx')
    urls = ['/api/dashboard?period=week', '/api/dashboard?period=month', '/api/dashboard?period=year',
            '/api/dashboard?period=year&type=run', '/api/dashboard?period=month&type=road_bike', '/api/segments']
    before = {u: client.get(u).get_data() for u in urls}
    assert all(client.get(u).status_code == 200 for u in urls)
    save_planned(conn, api, uid, planned_gpx(500))
    save_planned(conn, api, uid, planned_gpx(400, activity_type='cycling'), 'velo.gpx')
    assert {u: client.get(u).get_data() for u in urls} == before
    conn.close()


def test_the_library_type_filter_and_counters_ignore_planned_tracks(api):
    client, uid, conn = api_session(api, 'sub-types')
    ingest(conn, api, uid, run(), 'real.gpx')
    save_planned(conn, api, uid, planned_gpx(activity_type='cycling'))
    data = client.get('/api/activities').get_json()
    assert data['types'] == ['run'] and data['planned_count'] == 1 and len(data['activities']) == 2
    assert client.get('/api/activities?type=road_bike').get_json()['activities'][0]['kind'] == 'planned'   # la liste, elle, l'inclut
    assert client.get('/api/activities?type=road_bike&kind=activity').get_json()['activities'] == []
    conn.close()


def _segment_with_a_forced_planned_effort(db, app, user):
    real = ingest(db, app, user, run(), 'real.gpx')
    segment = segment_service.create(db, user, 'T', real, 50, 200)
    planned = save_planned(db, app, user)
    before = segment_service.list_for_user(db, user)
    db.execute("INSERT INTO efforts (segment_id, activity_id, start_idx, end_idx, start_time, elapsed_s) "
               "VALUES (?, ?, 0, 5, '2030-01-01T00:00:00Z', 0.5)", (segment, planned))      # effort forcé (corruption, ancienne donnée)
    db.commit()
    return segment, planned, before


def test_segment_detail_never_ranks_an_effort_of_a_planned_track(db, app, user):
    segment, planned, _ = _segment_with_a_forced_planned_effort(db, app, user)
    assert planned not in {e['activity_id'] for e in segment_service.detail(db, user, segment)['efforts']}


def test_segment_list_never_counts_an_effort_of_a_planned_track(db, app, user):
    _, _, before = _segment_with_a_forced_planned_effort(db, app, user)
    assert segment_service.list_for_user(db, user) == before


def test_segment_rescan_route_never_creates_efforts_for_planned_tracks(api):
    client, uid, conn = api_session(api, 'sub-rescan')
    real = ingest(conn, api, uid, run(), 'real.gpx')
    segment = segment_service.create(conn, uid, 'T', real, 50, 200)
    planned = save_planned(conn, api, uid)
    assert client.post(f'/api/segments/{segment}/rescan').status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM efforts WHERE activity_id = ?", (planned,)).fetchone()[0] == 0
    conn.close()


def test_a_segment_created_from_a_planned_track_ranks_only_real_activities(db, app, user):
    planned = save_planned(db, app, user)
    segment = segment_service.create(db, user, 'Depuis le plan', planned, 10, 200)
    ingest(db, app, user, run(), 'real.gpx')
    segment_service.match_segment(db, user, segment)
    efforts = segment_service.detail(db, user, segment)['efforts']
    assert planned not in {e['activity_id'] for e in efforts}


def test_stats_and_records_with_a_type_filter_ignore_planned_tracks(db, app, user):
    ingest(db, app, user, run(), 'real.gpx')
    before = (dashboard_service.records(db, user, 'run'), dashboard_service.period_stats(db, user, 'month', 'Europe/Paris', 'run'))
    save_planned(db, app, user, planned_gpx(600))
    after = (dashboard_service.records(db, user, 'run'), dashboard_service.period_stats(db, user, 'month', 'Europe/Paris', 'run'))
    assert after == before


def test_two_users_never_see_each_others_planned_tracks_anywhere(api):
    alice, a_id, a_conn = api_session(api, 'sub-alice')
    bob, b_id, b_conn = api_session(api, 'sub-bob')
    plan = save_planned(a_conn, api, a_id)
    assert plan not in [a['id'] for a in bob.get('/api/activities').get_json()['activities']]
    assert bob.get(f'/api/activities/{plan}').status_code == 404
    assert bob.delete(f'/api/activities/{plan}').status_code == 404
    assert bob.get('/api/activities').get_json()['planned_count'] == 0
    assert alice.get('/api/activities').get_json()['planned_count'] == 1
    a_conn.close()
    b_conn.close()


# --- kind : invalide, non modifiable, non fourni par le client ---------------------------------------

@pytest.mark.parametrize('value', ['Planned', 'ACTIVITY', 'planifiée', 'activity ', ' planned', 'activity;DROP TABLE activities',
                                   "' OR '1'='1", 'null', '0', '%', 'planned%00'])
def test_kind_filter_refuses_anything_but_the_two_exact_values(api, value):
    client, uid, conn = api_session(api, 'sub-filter')
    ingest(conn, api, uid, run(), 'real.gpx')
    assert client.get('/api/activities', query_string={'kind': value}).status_code == 400
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1
    conn.close()


def test_kind_filter_with_repeated_or_empty_values(api):
    client, uid, conn = api_session(api, 'sub-rep')
    ingest(conn, api, uid, run(), 'real.gpx')
    save_planned(conn, api, uid)
    assert len(client.get('/api/activities?kind=').get_json()['activities']) == 2
    assert len(client.get('/api/activities?kind=planned&kind=activity').get_json()['activities']) == 1
    conn.close()


def test_pagination_and_type_filter_combine_with_kind(db, app, user):
    for i in range(5):
        ingest(db, app, user, run(300 + i), f'r{i}.gpx')
        save_planned(db, app, user, planned_gpx(300 + i, lon0=6.1 + i * 0.01), f'p{i}.gpx')
    planned = activity_store.list_for_user(db, user, kind='planned')
    assert len(planned) == 5 and {a['kind'] for a in planned} == {'planned'}
    page = activity_store.list_for_user(db, user, kind='planned', limit=2, offset=3)
    assert len(page) == 2 and {a['id'] for a in page} <= {a['id'] for a in planned}
    assert activity_store.list_for_user(db, user, 'run', kind='activity', limit=500) == activity_store.list_for_user(db, user, 'run', kind='activity')
    assert len(activity_store.list_for_user(db, user)) == 10


def test_the_client_cannot_choose_the_kind_at_upload(api):
    client, uid, conn = api_session(api, 'sub-up')
    response = client.post('/api/activities', data={'files': [(io.BytesIO(planned_gpx()), 'x.gpx')], 'kind': 'planned'},
                           content_type='multipart/form-data')
    assert response.status_code == 200
    assert {r['kind'] for r in conn.execute("SELECT kind FROM activities")} == {'activity'}
    conn.close()


@pytest.mark.parametrize('body', [{'kind': 'activity'}, {'kind': 'planned'}, {'kind': 'autre', 'name': 'x'}, {'KIND': 'activity'}])
def test_patch_never_changes_the_kind_in_either_direction(api, body):
    client, uid, conn = api_session(api, 'sub-patch')
    plan = save_planned(conn, api, uid)
    real = ingest(conn, api, uid, run(), 'real.gpx')
    for activity_id, expected in ((plan, 'planned'), (real, 'activity')):
        client.patch(f'/api/activities/{activity_id}', json=body)
        assert row(conn, activity_id)['kind'] == expected
    conn.close()


def test_update_fields_refuses_kind_and_unknown_columns(db, app, user):
    plan = save_planned(db, app, user)
    for fields in ({'kind': 'activity'}, {'kind': 'planned', 'name': 'x'}, {'distance_m': 1}, {'user_id': 99}):
        with pytest.raises(ValueError):
            activity_store.update_fields(db, user, plan, fields)
    assert row(db, plan)['kind'] == 'planned' and row(db, plan)['name'] != 'x'


def test_the_database_itself_refuses_a_bad_kind_on_insert_and_update(db, app, user):
    plan = save_planned(db, app, user)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE activities SET kind = 'autre' WHERE id = ?", (plan,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE activities SET kind = NULL WHERE id = ?", (plan,))
    db.rollback()
    assert row(db, plan)['kind'] == 'planned'


@pytest.mark.parametrize('kind', ['', 'Planned', 'autre', None, 0, ['planned'], 'planned\x00', 'activity; DROP TABLE activities'])
def test_ingest_with_an_invalid_kind_creates_nothing_and_writes_no_file(app, db, user, kind):
    result = ingest_service.ingest_bytes(db, app.config, user, run(), 'a.gpx', 'upload', kind=kind)
    assert result.status == 'error' and db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    files = Path(app.config['DATA_DIR']) / 'files'
    assert not files.exists() or not [p for p in files.rglob('*') if p.is_file()]


# --- Dossier surveillé et dédoublonnage (RB-5.6, CA-R14) ---------------------------------------------

def test_the_inbox_always_produces_activities_even_for_a_gpx_without_time(app, db, user):
    inbox = inbox_watcher.inbox_dir(app.config) / 'u'
    inbox.mkdir(parents=True)
    path = inbox / 'sans-temps.gpx'
    path.write_bytes(planned_gpx())
    stamp = time.time() - 60
    os.utime(path, (stamp, stamp))
    counts = inbox_watcher.scan_once(db, app.config)
    assert counts['created'] == 1
    assert {r['kind'] for r in db.execute("SELECT kind FROM activities")} == {'activity'}


def test_re_uploading_the_exact_gpx_of_a_planned_track_is_a_duplicate_and_stays_planned(app, db, user):
    """RB-5.2 : le dédoublonnage SHA-256 reste actif ; la trace existante n'est pas transformée en activité."""
    raw = planned_gpx()
    plan = save_planned(db, app, user, raw)
    again = ingest_service.ingest_bytes(db, app.config, user, raw, 'plan.gpx', 'upload')
    assert again.status == 'duplicate' and again.activity_id == plan and row(db, plan)['kind'] == 'planned'


def test_the_same_gpx_saved_twice_as_planned_is_a_duplicate(app, db, user):
    raw = planned_gpx()
    save_planned(db, app, user, raw)
    again = ingest_service.ingest_bytes(db, app.config, user, raw, 'plan.gpx', 'route_builder', kind='planned')
    assert again.status == 'duplicate'


def test_a_planned_track_is_counted_in_the_quota_and_refused_beyond_it(app, db, user):
    app.config['USER_QUOTA_MB'] = 1
    folder = Path(app.config['DATA_DIR']) / 'files' / str(user)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'filler.bin').write_bytes(b'0' * (1024 * 1024 + 1))
    result = ingest_service.ingest_bytes(db, app.config, user, planned_gpx(), 'p.gpx', 'route_builder', kind='planned')
    assert result.status == 'error' and 'Quota' in result.message


# --- Suppression --------------------------------------------------------------------------------------

def test_deleting_a_planned_track_removes_it_and_its_file_without_touching_the_statistics(api):
    client, uid, conn = api_session(api, 'sub-del')
    real = ingest(conn, api, uid, run(), 'real.gpx')
    plan = save_planned(conn, api, uid)
    stats = client.get('/api/dashboard?period=year').get_data()
    sha = row(conn, plan)['sha256']
    assert client.delete(f'/api/activities/{plan}').status_code == 200
    assert row(conn, plan) is None and row(conn, real) is not None
    assert not [p for p in (Path(api.config['DATA_DIR']) / 'files').rglob(f'{sha}*')]
    assert client.get('/api/dashboard?period=year').get_data() == stats
    assert client.get('/api/activities').get_json()['planned_count'] == 0
    assert client.delete(f'/api/activities/{plan}').status_code == 404
    conn.close()


def test_after_deletion_the_same_gpx_can_be_saved_again(app, db, user):
    raw = planned_gpx()
    plan = save_planned(db, app, user, raw)
    assert activity_store.delete(db, user, plan, app.config['DATA_DIR'])
    assert row(db, save_planned(db, app, user, raw)) is not None


@pytest.mark.xfail(strict=True, reason="À ARBITRER (RB-5.2 n7, « nouvelle trace, nouvel id ») : `activities.id` est un INTEGER PRIMARY KEY sans "
                   "AUTOINCREMENT, SQLite réattribue l'id d'une trace supprimée. Le dédoublonnage du futur results.py sur « la trace "
                   "d'id N existe encore » prendrait alors une AUTRE trace pour la sienne (409 à tort). La table `users` a reçu "
                   "AUTOINCREMENT pour la même raison.")
def test_the_id_of_a_deleted_activity_is_never_given_to_another_one(app, db, user):
    plan = save_planned(db, app, user)
    assert activity_store.delete(db, user, plan, app.config['DATA_DIR'])
    assert ingest(db, app, user, run(), 'autre.gpx') != plan


def test_deleting_a_real_activity_leaves_planned_tracks_and_their_counter_alone(app, db, user):
    real = ingest(db, app, user, run(), 'real.gpx')
    save_planned(db, app, user)
    assert activity_store.delete(db, user, real, app.config['DATA_DIR'])
    assert activity_store.count_for_user(db, user) == 0 and activity_store.count_planned_for_user(db, user) == 1


def test_deleting_the_account_removes_both_kinds_and_their_files(api):
    admin, admin_id, conn = api_session(api, 'sub-admin', admin=True)
    _, victim, vconn = api_session(api, 'sub-victim')
    ingest(vconn, api, victim, run(), 'real.gpx')
    save_planned(vconn, api, victim)
    vconn.close()
    users = {u['id']: u for u in admin.get('/api/users').get_json()['users']}
    assert (users[victim]['nb_activities'], users[victim]['nb_planned']) == (1, 1)
    assert admin.delete(f'/api/users/{victim}').status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE user_id = ?", (victim,)).fetchone()[0] == 0
    conn.close()


# --- Ouverture, altitude, analyse d'une trace planifiée (RB-5.5) -----------------------------------------

def test_a_planned_track_opens_like_any_track(api):
    client, uid, conn = api_session(api, 'sub-open')
    plan = save_planned(conn, api, uid)
    for url in (f'/api/activities/{plan}', f'/api/activities/{plan}/track', f'/api/activities/{plan}/parsed'):
        assert client.get(url).status_code == 200, url
    assert client.get(f'/api/activities/{plan}').get_json()['activity']['kind'] == 'planned'
    conn.close()


def test_advanced_analysis_is_available_on_a_planned_track_and_leaves_the_kind_alone(api):
    client, uid, conn = api_session(api, 'sub-an')
    plan = save_planned(conn, api, uid)
    assert client.post(f'/api/activities/{plan}/analysis').status_code == 200
    assert client.get(f'/api/activities/{plan}/analysis').status_code == 200
    assert row(conn, plan)['kind'] == 'planned' and row(conn, plan)['best_efforts'] == '{}'
    conn.close()


def test_elevation_catch_up_keeps_kind_and_empty_best_efforts(app, db, user):
    plan = save_planned(db, app, user)
    assert plan in [r['id'] for r in activity_store.pending_dem(db, 100)]
    activity_store.set_dem_status(db, user, plan, 'no_coverage')
    assert row(db, plan)['kind'] == 'planned' and row(db, plan)['best_efforts'] == '{}'


# --- recompute-stats (M14, CA-R22) --------------------------------------------------------------------------

def test_recompute_is_idempotent_and_scoped_by_user(db, app, user):
    db.execute("INSERT INTO users (sub, username) VALUES ('v', 'v')")
    db.commit()
    other = db.execute("SELECT id FROM users WHERE sub = 'v'").fetchone()[0]
    mine = save_planned(db, app, user)
    theirs = save_planned(db, app, other, planned_gpx(lon0=7.0))
    db.execute("UPDATE activities SET best_efforts = '{\"1000\": 9.0}'")
    db.commit()
    assert ingest_service.recompute_summaries(db, user) == 1
    assert row(db, mine)['best_efforts'] == '{}' and row(db, theirs)['best_efforts'] == '{"1000": 9.0}'     # l'autre compte n'est pas touché
    assert ingest_service.recompute_summaries(db) == 2
    assert row(db, theirs)['best_efforts'] == '{}'
    assert ingest_service.recompute_summaries(db) == 2
    assert {r['kind'] for r in db.execute("SELECT kind FROM activities")} == {'planned'}


def test_recompute_never_changes_a_kind_and_keeps_real_best_efforts(db, app, user):
    real = ingest(db, app, user, run(), 'real.gpx')
    plan = save_planned(db, app, user)
    efforts = row(db, real)['best_efforts']
    ingest_service.recompute_summaries(db)
    assert (row(db, real)['kind'], row(db, plan)['kind']) == ('activity', 'planned')
    assert json.loads(row(db, real)['best_efforts']) == json.loads(efforts)


def test_recompute_forces_empty_best_efforts_for_a_timed_planned_track(db, app, user):
    plan = ingest(db, app, user, run(), 'timed.gpx', source='route_builder', kind='planned')
    db.execute("UPDATE activities SET best_efforts = '{\"5000\": 1.0}' WHERE id = ?", (plan,))
    db.commit()
    ingest_service.recompute_summaries(db)
    assert row(db, plan)['best_efforts'] == '{}'


# --- Migration d'une base existante portant des données (RB-5.4, CA-R14) ------------------------------

def _downgrade_to_before_kind(path):
    """Rejoue l'état d'une base antérieure à TR4 : colonne `kind`, son index et son enregistrement retirés."""
    conn = database.connect(path)
    conn.execute("DROP INDEX IF EXISTS idx_activities_user_kind_start")
    conn.execute("ALTER TABLE activities DROP COLUMN kind")
    conn.execute("DELETE FROM schema_migrations WHERE name = '0011_activities_kind'")
    conn.commit()
    assert 'kind' not in {r['name'] for r in conn.execute("PRAGMA table_info(activities)")}
    conn.close()


def test_a_populated_database_keeps_every_figure_and_gets_activity_everywhere(tmp_path):
    cfg = TestingConfig
    app = create_app(cfg, overrides={'DATA_DIR': str(tmp_path / 'data')})
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    conn.commit()
    ids = [ingest(conn, app, 1, run(300 + 40 * i), f'r{i}.gpx') for i in range(3)]
    ids.append(ingest(conn, app, 1, planned_gpx(), 'sans-temps.gpx'))
    segment = segment_service.create(conn, 1, 'T', ids[0], 50, 200)
    snapshot = (dashboard_service.period_stats(conn, 1, 'year', 'Europe/Paris', year=2025), dashboard_service.records(conn, 1),
                activity_store.list_types(conn, 1), activity_store.count_for_user(conn, 1),
                [tuple(r) for r in conn.execute("SELECT * FROM efforts ORDER BY id")],
                segment_service.detail(conn, 1, segment))
    summaries = [tuple(r) for r in conn.execute("SELECT id, name, distance_m, best_efforts, start_time FROM activities ORDER BY id")]
    conn.close()
    path = app.config['DB_PATH']
    _downgrade_to_before_kind(path)
    for _ in range(2):                                                         # migrer deux fois : idempotent
        database.init_db(path)
    conn = database.connect(path)
    assert [r['kind'] for r in conn.execute("SELECT kind FROM activities ORDER BY id")] == ['activity'] * 4
    assert [tuple(r) for r in conn.execute("SELECT id, name, distance_m, best_efforts, start_time FROM activities ORDER BY id")] == summaries
    after = (dashboard_service.period_stats(conn, 1, 'year', 'Europe/Paris', year=2025), dashboard_service.records(conn, 1),
             activity_store.list_types(conn, 1), activity_store.count_for_user(conn, 1),
             [tuple(r) for r in conn.execute("SELECT * FROM efforts ORDER BY id")], segment_service.detail(conn, 1, segment))
    assert after == snapshot                                                   # CA-R14 : statistiques identiques
    conn.close()


def test_after_migration_the_constraint_and_the_index_are_enforced_and_usable(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data')})
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    ingest(conn, app, 1, run(), 'r.gpx')
    conn.close()
    _downgrade_to_before_kind(app.config['DB_PATH'])
    database.init_db(app.config['DB_PATH'])
    conn = database.connect(app.config['DB_PATH'])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE activities SET kind = 'autre'")
    plan = save_planned(conn, app, 1)
    assert row(conn, plan)['kind'] == 'planned'
    plan_text = ' '.join(str(r[-1]) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT id FROM activities WHERE user_id = 1 AND kind = 'activity' ORDER BY start_time"))
    assert 'idx_activities_user_kind_start' in plan_text
    conn.close()


def test_migration_on_an_empty_activities_table_and_on_a_fresh_database(tmp_path):
    path = str(tmp_path / 'fresh.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0011_activities_kind'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    conn.close()
    _downgrade_to_before_kind(path)
    database.init_db(path)


def test_migration_is_fast_on_a_large_table(tmp_path):
    """ADD COLUMN avec valeur par défaut : aucune réécriture de la table, même avec 20 000 lignes."""
    path = str(tmp_path / 'big.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    conn.executemany("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points) "
                     "VALUES (1, 'a', 'f.gpx', ?, 'upload', '2026-01-01T00:00:00Z', x'00')", [(f'h{i}',) for i in range(20_000)])
    conn.commit()
    conn.close()
    _downgrade_to_before_kind(path)
    started = time.monotonic()
    database.init_db(path)
    assert time.monotonic() - started < 10
    conn = database.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM activities WHERE kind = 'activity'").fetchone()[0] == 20_000
    conn.close()


# --- Trace planifiée AVEC horodatage : le seul garde-fou est `kind`, pas `has_time` -------------------------

def test_a_timed_planned_track_never_gets_efforts_from_any_matching_path(db, app, user):
    """`has_time = 1` ne protège pas ici : seul le filtre sur `kind` écarte la trace (match_activity, match_segment, rescan)."""
    real = ingest(db, app, user, run(), 'real.gpx')
    segment = segment_service.create(db, user, 'T', real, 50, 200)
    planned = ingest(db, app, user, run(310), 'timed-plan.gpx', source='route_builder', kind='planned')
    assert row(db, planned)['has_time'] == 1 and row(db, planned)['kind'] == 'planned'
    assert segment_service.match_activity(db, user, planned) == 0
    segment_service.match_segment(db, user, segment)
    assert db.execute("SELECT COUNT(*) FROM efforts WHERE activity_id = ?", (planned,)).fetchone()[0] == 0
    assert planned not in {e['activity_id'] for e in segment_service.detail(db, user, segment)['efforts']}


def test_a_segment_created_from_a_timed_planned_track_gets_no_effort_from_it(db, app, user):
    planned = ingest(db, app, user, run(), 'timed-plan.gpx', source='route_builder', kind='planned')
    segment = segment_service.create(db, user, 'Depuis le plan', planned, 50, 200)
    assert segment_service.detail(db, user, segment)['efforts'] == []
    assert db.execute("SELECT COUNT(*) FROM efforts").fetchone()[0] == 0


def test_ingesting_a_real_activity_after_the_segment_still_matches_only_real_ones(db, app, user):
    planned = ingest(db, app, user, run(), 'timed-plan.gpx', source='route_builder', kind='planned')
    segment = segment_service.create(db, user, 'T', planned, 50, 200)
    real = ingest(db, app, user, run(320), 'real.gpx')
    assert segment_service.match_activity(db, user, real) >= 1
    assert {e['activity_id'] for e in segment_service.detail(db, user, segment)['efforts']} == {real}
