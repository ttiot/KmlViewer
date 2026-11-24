"""Nature d'une trace : réalisée / planifiée (TR4, RB-5.3 à RB-5.6, CA-R13, CA-R14, CA-R22, ADR-0009)."""

import json
import sqlite3

import pytest

from app import db as database
from app.services import activity_store, dashboard_service, ingest_service, segment_service
from app import create_app
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES, line, login_as, make_gpx


def run(n=300, **kw):
    """~10 km en ligne droite, 10 s entre les points : une vraie sortie de course (meilleurs efforts calculés)."""
    return make_gpx(line(n, dlat=0.0003), activity_type='running', **kw).encode()


def planned_gpx(n=300, dlat=0.0003, lon0=6.0):
    return make_gpx(line(n, dlat=dlat, lon0=lon0), activity_type='run', with_time=False).encode()


@pytest.fixture
def user(db):
    db.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    db.commit()
    return db.execute("SELECT id FROM users WHERE sub = 'u'").fetchone()[0]


@pytest.fixture
def oidc_app(tmp_path):
    """Application avec comptes distincts (le mode local ignore la session et n'a qu'un utilisateur)."""
    return create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'oidc'),
                                                'TRUSTED_HOSTS': ['localhost']})


def ingest(db, app, user, raw, name='a.gpx', **kw):
    result = ingest_service.ingest_bytes(db, app.config, user, raw, name, kw.pop('source', 'upload'), **kw)
    assert result.status == 'created', result
    return result.activity_id


def row(db, activity_id):
    return db.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()


# --- Migration (RB-5.4, CA-R14) ----------------------------------------------------------------

def test_migration_is_named_stable_and_adds_the_column_and_the_index(db):
    assert db.execute("SELECT 1 FROM schema_migrations WHERE name = '0011_activities_kind'").fetchone()
    info = {r['name']: r for r in db.execute("PRAGMA table_info(activities)")}
    assert info['kind']['notnull'] == 1 and info['kind']['dflt_value'] == "'activity'"
    indexes = {r['name']: [c['name'] for c in db.execute(f"PRAGMA index_info({r['name']})")]
               for r in db.execute("PRAGMA index_list(activities)")}
    assert ['user_id', 'kind', 'start_time'] in indexes.values()


def test_database_refuses_an_unknown_kind(db, user):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points, kind) "
                   "VALUES (?, 'n', 'f.gpx', 'x', 's', '2026-01-01T00:00:00Z', x'00', 'autre')", (user,))


def test_existing_database_gets_activity_everywhere_and_loses_nothing(tmp_path):
    """CA-R14 : base existante (avant TR4) => `activity` partout, lignes intactes, idempotent."""
    path = str(tmp_path / 'old.sqlite3')
    conn = database.connect(path)
    conn.executescript(database.SCHEMA)
    database.migrate(conn, named=[])
    conn.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    conn.executemany("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, distance_m, points) "
                     "VALUES (1, ?, 'f.gpx', ?, 'upload', '2026-01-01T00:00:00Z', ?, x'00')",
                     [('a', 'h1', 1000.0), ('b', 'h2', 2000.0)])
    conn.commit()
    assert 'kind' not in {r['name'] for r in conn.execute("PRAGMA table_info(activities)")}
    conn.close()
    database.init_db(path)
    database.init_db(path)
    conn = database.connect(path)
    got = [tuple(r) for r in conn.execute("SELECT name, distance_m, kind FROM activities ORDER BY id")]
    assert got == [('a', 1000.0, 'activity'), ('b', 2000.0, 'activity')]
    conn.close()


# --- Ingestion (RB-5.3, RB-5.6) -----------------------------------------------------------------

def test_default_ingestion_is_an_activity_even_without_timestamps(app, db, user):
    """CA-R14 / RB-5.6 : upload ou inbox d'un GPX sans temps => kind = 'activity'."""
    untimed = ingest(db, app, user, planned_gpx(), 'sans-temps.gpx')
    assert row(db, untimed)['kind'] == 'activity' and row(db, untimed)['has_time'] == 0


def test_upload_route_and_inbox_always_produce_activities(app, client, db):
    response = client.post('/api/activities', data={'files': [(__import__('io').BytesIO(planned_gpx()), 'x.gpx')]},
                           content_type='multipart/form-data')
    assert response.status_code == 200
    assert {r['kind'] for r in db.execute("SELECT kind FROM activities")} == {'activity'}


def test_planned_ingestion_marks_the_row_and_computes_no_best_efforts(app, db, user):
    planned = ingest(db, app, user, planned_gpx(), 'trace.gpx', source='route_builder', kind='planned')
    r = row(db, planned)
    assert (r['kind'], r['source'], r['start_time_known'], r['has_time']) == ('planned', 'route_builder', 0, 0)
    assert r['best_efforts'] == '{}' and r['distance_m'] > 9000          # distance et D+ restent calculés


def test_planned_ingestion_never_keeps_best_efforts_even_if_the_file_has_times(app, db, user):
    planned = ingest(db, app, user, run(), 'timed.gpx', source='route_builder', kind='planned')
    assert row(db, planned)['best_efforts'] == '{}'
    assert json.loads(row(db, ingest(db, app, user, run(310), 'real.gpx'))['best_efforts'])        # témoin : une sortie réelle en a


@pytest.mark.parametrize('kind', ['', 'Planned', 'autre', None, 'activity; DROP TABLE activities'])
def test_unknown_kind_is_an_error_result_not_an_exception(app, db, user, kind):
    result = ingest_service.ingest_bytes(db, app.config, user, run(), 'a.gpx', 'upload', kind=kind)
    assert result.status == 'error'
    assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0


def test_planned_track_counts_toward_the_quota(app, db, user):
    ingest(db, app, user, planned_gpx(), 'trace.gpx', source='route_builder', kind='planned')
    assert activity_store.user_storage_bytes(app.config['DATA_DIR'], user) > 0


# --- Filtres (RB-5.5, CA-R13) ---------------------------------------------------------------------

@pytest.fixture
def library(app, db, user):
    """Une sortie réelle, et une trace planifiée de même géométrie."""
    real = ingest(db, app, user, run(), 'real.gpx')
    planned = ingest(db, app, user, planned_gpx(), 'plan.gpx', source='route_builder', kind='planned')
    return real, planned


def snapshot(db, user):
    return {
        'week': dashboard_service.period_stats(db, user, 'week', 'Europe/Paris'),
        'month': dashboard_service.period_stats(db, user, 'month', 'Europe/Paris'),
        'year': dashboard_service.period_stats(db, user, 'year', 'Europe/Paris', year=2025),
        'records': dashboard_service.records(db, user),
        'types': activity_store.list_types(db, user), 'count': activity_store.count_for_user(db, user),
    }


def test_a_planned_track_changes_neither_stats_records_types_nor_counter(app, db, user):
    """CA-R13."""
    ingest(db, app, user, run(), 'real.gpx')
    before = snapshot(db, user)
    assert before['records'] and before['count'] == 1
    ingest(db, app, user, planned_gpx(400), 'plan.gpx', source='route_builder', kind='planned')    # plus longue que la sortie
    assert snapshot(db, user) == before
    assert activity_store.count_planned_for_user(db, user) == 1


def test_a_planned_track_of_another_type_does_not_add_a_type_to_the_filter(app, db, user):
    ingest(db, app, user, run(), 'real.gpx')
    raw = make_gpx(line(100), activity_type='cycling', with_time=False).encode()
    ingest(db, app, user, raw, 'velo.gpx', source='route_builder', kind='planned')
    assert activity_store.list_types(db, user) == ['run']


def test_records_of_a_database_holding_only_planned_tracks_are_empty(app, db, user):
    ingest(db, app, user, planned_gpx(), 'plan.gpx', source='route_builder', kind='planned')
    assert dashboard_service.records(db, user) == [] and activity_store.count_for_user(db, user) == 0
    stats = dashboard_service.period_stats(db, user, 'month', 'Europe/Paris')
    assert not any(b.get('count') for b in stats.get('buckets', []) if isinstance(b, dict))


def test_library_lists_both_kinds_by_default_with_the_kind_and_filters_on_demand(app, db, user, library):
    both = activity_store.list_for_user(db, user)
    assert sorted(a['kind'] for a in both) == ['activity', 'planned']
    assert [a['kind'] for a in activity_store.list_for_user(db, user, kind='planned')] == ['planned']
    assert [a['kind'] for a in activity_store.list_for_user(db, user, kind='activity')] == ['activity']
    with pytest.raises(ValueError):
        activity_store.list_for_user(db, user, kind='autre')


def test_library_api_filter_and_planned_counter(oidc_app):
    app = oidc_app
    client, user = login_as(app, 'sub-lib')
    conn = database.connect(app.config['DB_PATH'])
    real = ingest_service.ingest_bytes(conn, app.config, user, run(), 'r.gpx', 'upload')
    plan = ingest_service.ingest_bytes(conn, app.config, user, planned_gpx(), 'p.gpx', 'route_builder', kind='planned')
    conn.close()
    assert (real.status, plan.status) == ('created', 'created')
    data = client.get('/api/activities').get_json()
    assert sorted(a['kind'] for a in data['activities']) == ['activity', 'planned'] and data['planned_count'] == 1
    assert [a['kind'] for a in client.get('/api/activities?kind=planned').get_json()['activities']] == ['planned']
    assert [a['kind'] for a in client.get('/api/activities?kind=activity').get_json()['activities']] == ['activity']
    assert client.get('/api/activities?kind=nope').status_code == 400
    assert client.get('/api/activities?kind=').status_code == 200            # vide = toutes
    assert client.get(f'/api/activities/{plan.activity_id}').get_json()['activity']['kind'] == 'planned'


def test_users_api_counts_realised_and_planned_separately(oidc_app):
    app = oidc_app
    client, user = login_as(app, 'sub-admin', admin=True)
    conn = database.connect(app.config['DB_PATH'])
    ingest_service.ingest_bytes(conn, app.config, user, run(), 'r.gpx', 'upload')
    ingest_service.ingest_bytes(conn, app.config, user, planned_gpx(), 'p.gpx', 'route_builder', kind='planned')
    conn.close()
    mine = next(u for u in client.get('/api/users').get_json()['users'] if u['id'] == user)
    assert (mine['nb_activities'], mine['nb_planned']) == (1, 1)


def test_kind_is_not_editable(app, db, user, library, oidc_app):
    _, planned = library
    assert 'kind' not in activity_store.EDITABLE_COLUMNS
    with pytest.raises(ValueError):
        activity_store.update_fields(db, user, planned, {'kind': 'activity'})
    client, uid = login_as(oidc_app, 'sub-edit')
    conn = database.connect(oidc_app.config['DB_PATH'])
    own = ingest_service.ingest_bytes(conn, oidc_app.config, uid, planned_gpx(), 'p.gpx', 'route_builder', kind='planned')
    conn.close()
    client.patch(f'/api/activities/{own.activity_id}', json={'kind': 'activity', 'name': 'nouveau'})
    kind = database.connect(oidc_app.config['DB_PATH']).execute("SELECT kind FROM activities WHERE id = ?", (own.activity_id,)).fetchone()[0]
    assert kind == 'planned'


def test_pending_elevation_catch_up_includes_planned_tracks(app, db, user, library):
    _, planned = library
    assert planned in [r['id'] for r in activity_store.pending_dem(db, 100)]


# --- Tronçons : aucun effort (RB-5.5) -------------------------------------------------------------

def test_a_planned_track_creates_no_effort_and_is_excluded_from_rankings(app, db, user):
    real = ingest(db, app, user, run(), 'real.gpx')
    segment = segment_service.create(db, user, 'Montée', real, 50, 200)
    assert db.execute("SELECT COUNT(*) FROM efforts").fetchone()[0] >= 1
    before = db.execute("SELECT COUNT(*) FROM efforts").fetchone()[0]
    planned = ingest(db, app, user, planned_gpx(), 'plan.gpx', source='route_builder', kind='planned')
    assert db.execute("SELECT COUNT(*) FROM efforts WHERE activity_id = ?", (planned,)).fetchone()[0] == 0
    assert segment_service.match_activity(db, user, planned) == 0
    segment_service.match_segment(db, user, segment)
    assert db.execute("SELECT COUNT(*) FROM efforts WHERE activity_id = ?", (planned,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM efforts").fetchone()[0] == before
    # Même un effort forcé en base n'apparaît pas au classement
    db.execute("INSERT INTO efforts (segment_id, activity_id, start_idx, end_idx, start_time, elapsed_s) "
               "VALUES (?, ?, 0, 5, '2026-01-01T00:00:00Z', 1.0)", (segment, planned))
    db.commit()
    detail = segment_service.detail(db, user, segment)
    assert planned not in {e['activity_id'] for e in detail['efforts']}


def test_a_segment_can_still_be_created_from_a_planned_track(app, db, user):
    planned = ingest(db, app, user, planned_gpx(), 'plan.gpx', source='route_builder', kind='planned')
    segment = segment_service.create(db, user, 'Depuis une trace planifiée', planned, 10, 100)
    assert segment and segment_service.detail(db, user, segment)['efforts'] == []


# --- Recalcul des résumés (M14, CA-R22) ---------------------------------------------------------------

def test_recompute_summaries_forces_empty_best_efforts_for_planned_tracks_and_leaves_the_rest(app, db, user):
    real = ingest(db, app, user, run(), 'real.gpx')
    planned = ingest(db, app, user, planned_gpx(), 'plan.gpx', source='route_builder', kind='planned')
    segment = segment_service.create(db, user, 'T', real, 50, 200)
    before = snapshot(db, user)
    efforts_before = [tuple(r) for r in db.execute("SELECT * FROM efforts ORDER BY id")]
    # Une valeur non vide (ancienne donnée, bug) doit être ramenée à '{}' par le recalcul
    db.execute("UPDATE activities SET best_efforts = '{\"1000\": 123.0}', distance_m = 1 WHERE id = ?", (planned,))
    db.commit()
    assert ingest_service.recompute_summaries(db) == 2
    assert row(db, planned)['best_efforts'] == '{}' and row(db, planned)['distance_m'] > 9000      # distance recalculée
    assert json.loads(row(db, real)['best_efforts'])
    assert snapshot(db, user) == before
    assert [tuple(r) for r in db.execute("SELECT * FROM efforts ORDER BY id")] == efforts_before
    assert segment_service.detail(db, user, segment)['efforts']


def test_recompute_stats_command_leaves_planned_best_efforts_empty(app, db, user, runner, library):
    _, planned = library
    db.execute("UPDATE activities SET best_efforts = '{\"5000\": 1.0}' WHERE id = ?", (planned,))
    db.commit()
    assert runner.invoke(args=['recompute-stats']).exit_code == 0
    assert row(db, planned)['best_efforts'] == '{}'
