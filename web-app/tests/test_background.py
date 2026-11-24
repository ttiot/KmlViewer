"""Ordonnanceur de fond (T19, EF-10, R3, CA-43, CA-48, CA-51, CA-21d)."""

import logging
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, background, create_app, db as database
from app.audit import Actor
from app.background import FileLock, Scheduler, SyncExecutor, Task, ThreadExecutor, default_tasks
from app.config import TestingConfig
from app.settings import STORE_KEY
from tests.conftest import line, make_gpx

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class HeldExecutor:
    """Exécuteur de tâches longues « bloqué » : garde ce qu'on lui soumet, le test décide quand ça finit."""

    def __init__(self):
        self.pending = []

    def submit(self, fn):
        self.pending.append(fn)

    def finish_all(self):
        pending, self.pending = self.pending, []
        for fn in pending:
            fn()


class FakeLock:
    def __init__(self, held=True):
        self.held = held
        self.attempts = 0

    def try_acquire(self):
        self.attempts += 1
        return self.held

    def release(self):
        self.held = False


@pytest.fixture
def cfg(app):
    return app.config


def counting_task(name, calls, **kw):
    return Task(name, lambda db, cfg: calls.append(name), interval=kw.pop('interval', lambda c: 60), **kw)


# --- Tâches du produit et démarrage -------------------------------------------------------------

def test_default_tasks_match_the_spec_table(cfg):
    tasks = {t.name: t for t in default_tasks()}
    assert set(tasks) == {'settings_reload', 'audit_purge', 'inbox', 'dem_backfill'}
    assert [n for n, t in tasks.items() if t.shared] == ['audit_purge', 'inbox', 'dem_backfill']
    assert [n for n, t in tasks.items() if t.long] == ['inbox', 'dem_backfill']
    assert tasks['audit_purge'].interval(cfg) == 24 * 3600 and tasks['settings_reload'].interval(cfg) == 0


def test_scheduler_exists_but_does_not_run_under_test_or_cli(app):
    assert isinstance(app.extensions['scheduler'], Scheduler) and app.extensions['scheduler']._thread is None


def test_scheduler_starts_in_served_mode_whatever_inbox_and_dem_say(tmp_path, monkeypatch):
    """CA-43 : INBOX_ENABLED=0 et pas de DEM_URL : l'ordonnanceur tourne quand même."""
    started = []
    monkeypatch.setattr(Scheduler, 'start', lambda self: started.append(self))
    monkeypatch.delenv('FLASK_RUN_FROM_CLI', raising=False)

    class Served(TestingConfig):
        TESTING = False
        INBOX_ENABLED = False
        DEM_URL = ''

    create_app(Served, overrides={'DATA_DIR': str(tmp_path / 'd'), 'AUTH_MODE': 'local'})
    assert len(started) == 1


def test_scheduler_does_not_start_for_a_cli_command(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(Scheduler, 'start', lambda self: started.append(self))
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    monkeypatch.delenv('KMLVIEWER_WATCHER', raising=False)

    class Served(TestingConfig):
        TESTING = False

    create_app(Served, overrides={'DATA_DIR': str(tmp_path / 'd'), 'AUTH_MODE': 'local'})
    assert started == []


# --- tick : échéances, activation, isolation -----------------------------------------------------------

def test_each_task_runs_at_its_own_interval_with_an_injected_clock(cfg):
    calls, clock = [], Clock()
    s = Scheduler(cfg, [counting_task('a', calls, interval=lambda c: 10), counting_task('b', calls, interval=lambda c: 30)],
                  clock=clock, lock=FakeLock())
    assert s.tick() == ['a', 'b']                                   # jamais exécutées : dues
    clock.t += 9
    assert s.tick() == []
    clock.t += 1
    assert s.tick() == ['a']
    clock.t += 20
    assert s.tick() == ['a', 'b']


def test_a_task_that_raises_does_not_stop_the_others_and_is_rescheduled(cfg, caplog):
    """CA-43 : une exception n'empêche pas les autres tâches du même tour."""
    calls, clock = [], Clock()

    def boom(db, c):
        raise RuntimeError("panne")

    s = Scheduler(cfg, [Task('bad', boom, interval=lambda c: 60), counting_task('good', calls)], clock=clock, lock=FakeLock())
    with caplog.at_level(logging.ERROR):
        assert s.tick() == ['bad', 'good']
    assert calls == ['good'] and s.state['bad'].last_error == 'RuntimeError' and s.state['good'].last_error is None
    assert 'bad' in caplog.text
    clock.t += 30
    assert s.tick() == []                                          # replanifiée à son intervalle, pas relancée à chaque tour
    clock.t += 30
    assert s.tick() == ['bad', 'good']


def test_a_broken_enabled_or_interval_does_not_block_other_tasks(cfg):
    calls = []

    def bad_enabled(c):
        raise RuntimeError

    s = Scheduler(cfg, [counting_task('x', calls, enabled=bad_enabled), counting_task('y', calls)], clock=Clock(), lock=FakeLock())
    assert s.tick() == ['y']


def test_disabled_tasks_do_not_run_and_activation_is_read_every_tick(cfg):
    flag, calls = {'on': False}, []
    s = Scheduler(cfg, [counting_task('t', calls, enabled=lambda c: flag['on'])], clock=Clock(), lock=FakeLock())
    assert s.tick() == [] and calls == []
    flag['on'] = True                                              # sans redémarrage
    assert s.tick() == ['t'] and calls == ['t']


def test_run_task_runs_one_task_alone_without_lock_or_due_date(cfg):
    calls = []
    s = Scheduler(cfg, [counting_task('a', calls, shared=True), counting_task('b', calls)], clock=Clock(), lock=FakeLock(False))
    s.run_task('a')
    s.run_task('a')
    assert calls == ['a', 'a']
    with pytest.raises(ValueError):
        s.run_task('inconnue')


# --- Verrou : tâches partagées seulement, retenté à chaque tour (CA-48) ----------------------------------

def test_without_the_lock_only_process_tasks_run_and_the_lock_is_retried_every_tick(cfg):
    calls, clock, lock = [], Clock(), FakeLock(held=False)
    s = Scheduler(cfg, [counting_task('settings_reload', calls, interval=lambda c: 0),
                        counting_task('routing_health', calls, interval=lambda c: 0),
                        counting_task('inbox', calls, shared=True, interval=lambda c: 0),
                        counting_task('dem_backfill', calls, shared=True, interval=lambda c: 0),
                        counting_task('audit_purge', calls, shared=True, interval=lambda c: 0)], clock=clock, lock=lock)
    assert s.tick() == ['settings_reload', 'routing_health']
    assert s.tick() == ['settings_reload', 'routing_health'] and lock.attempts == 2
    lock.held = True                                                # l'autre processus s'est arrêté
    assert s.tick() == ['settings_reload', 'routing_health', 'inbox', 'dem_backfill', 'audit_purge']


def test_two_processes_on_the_same_data_dir_share_one_lock(tmp_path):
    """CA-48 avec le vrai verrou fichier : le second n'a pas le verrou tant que le premier le garde."""
    first, second = FileLock(tmp_path / 'inbox.lock'), FileLock(tmp_path / 'inbox.lock')
    assert first.try_acquire() and first.held
    assert not second.try_acquire() and not second.held
    first.release()
    assert second.try_acquire()                                    # reprise au tour suivant
    second.release()


def test_shared_tasks_of_the_second_scheduler_resume_when_the_first_stops(tmp_path, app):
    lock_path = tmp_path / 'shared.lock'
    calls_a, calls_b = [], []

    def make(calls):
        return Scheduler(app.config, [counting_task('settings_reload', calls, interval=lambda c: 0),
                                      counting_task('inbox', calls, shared=True, interval=lambda c: 0)],
                         clock=Clock(), lock=FileLock(lock_path))

    a, b = make(calls_a), make(calls_b)
    a.tick()
    b.tick()
    assert calls_a == ['settings_reload', 'inbox'] and calls_b == ['settings_reload']       # b : pas inbox
    a.stop()
    b.tick()
    assert calls_b == ['settings_reload', 'settings_reload', 'inbox']
    b.stop()


# --- Tâches longues et réveil ciblé (CA-51) -----------------------------------------------------------

def test_a_blocked_long_task_never_freezes_the_short_ones_nor_gets_a_second_instance(cfg):
    """CA-51 : passage DEM de 600 s simulé par un exécuteur bloqué."""
    calls, clock, held = [], Clock(), HeldExecutor()
    tasks = [counting_task('settings_reload', calls, interval=lambda c: 5),
             counting_task('routing_health', calls, interval=lambda c: 60),
             counting_task('dem_backfill', calls, interval=lambda c: 300, shared=True, long=True)]
    s = Scheduler(cfg, tasks, clock=clock, executor=held, lock=FakeLock())
    assert s.tick() == ['settings_reload', 'routing_health', 'dem_backfill'] and len(held.pending) == 1
    assert 'dem_backfill' not in calls                                   # soumise, pas encore exécutée : bloquée
    for _ in range(130):                                                 # 650 s de tours pendant que le DEM « tourne »
        clock.t += 5
        launched = s.tick()
        assert 'dem_backfill' not in launched
    assert calls.count('settings_reload') >= 100 and calls.count('routing_health') >= 10
    assert len(held.pending) == 1                                        # jamais empilée
    held.finish_all()
    assert calls[-1] == 'dem_backfill' and s.state['dem_backfill'].running is False
    clock.t += 299
    assert 'dem_backfill' not in s.tick()
    clock.t += 1
    assert 'dem_backfill' in s.tick()


def test_wake_makes_a_task_due_at_the_next_tick_without_waiting_for_its_interval(cfg):
    calls, clock = [], Clock()
    s = Scheduler(cfg, [counting_task('dem_backfill', calls, interval=lambda c: 300, shared=True, long=True)], clock=clock,
                  lock=FakeLock())
    s.tick()
    clock.t += 5
    assert s.tick() == []                                                # 300 s pas écoulées
    s.wake('dem_backfill')
    assert s.tick() == ['dem_backfill'] and calls == ['dem_backfill', 'dem_backfill']
    with pytest.raises(ValueError):
        s.wake('inconnue')


def test_wake_during_a_run_triggers_one_more_run_right_after(cfg):
    calls, clock, held = [], Clock(), HeldExecutor()
    s = Scheduler(cfg, [counting_task('dem_backfill', calls, interval=lambda c: 300, shared=True, long=True)], clock=clock,
                  executor=held, lock=FakeLock())
    s.tick()
    s.wake('dem_backfill')                                               # import pendant la passe en cours
    held.finish_all()
    clock.t += 1
    assert s.tick() == ['dem_backfill']                                  # relancée aussitôt, pas dans 300 s


def test_wake_sets_the_event_that_wakes_the_loop(cfg):
    s = Scheduler(cfg, [counting_task('a', [])], clock=Clock(), lock=FakeLock())
    s._wake.clear()
    s.wake('a')
    assert s._wake.is_set()


def test_import_with_deferred_elevation_wakes_the_dem_task(app, client):
    scheduler = app.extensions['scheduler']
    scheduler.state['dem_backfill'].last_run = 12345.0
    gpx = make_gpx(line(50)).encode()
    import io
    response = client.post('/api/activities', data={'files': [(io.BytesIO(gpx), 'a.gpx')]}, content_type='multipart/form-data')
    assert response.status_code == 200 and scheduler.state['dem_backfill'].last_run is None


def test_thread_executor_runs_long_tasks_one_at_a_time_in_order():
    executor, order, gate, done = ThreadExecutor(), [], threading.Event(), threading.Event()

    def first():
        gate.wait(5)
        order.append('first')

    executor.submit(first)
    executor.submit(lambda: (order.append('second'), done.set()))
    assert order == []                                                   # la seconde attend la fin de la première
    gate.set()
    assert done.wait(5) and order == ['first', 'second']
    executor.stop()


def test_thread_executor_survives_a_crashing_task():
    executor, done = ThreadExecutor(), threading.Event()
    executor.submit(lambda: (_ for _ in ()).throw(RuntimeError))
    executor.submit(done.set)
    assert done.wait(5)
    executor.stop()


def test_start_runs_the_loop_in_a_thread_and_stop_ends_it(app):
    calls = []
    s = Scheduler(app.config, [counting_task('a', calls, interval=lambda c: 0)], lock=FakeLock())
    s.start()
    deadline = threading.Event()
    for _ in range(100):
        if calls:
            break
        deadline.wait(0.05)
    s.stop()
    assert calls and not s._thread.is_alive()


# --- Les tâches du produit ----------------------------------------------------------------------------

def test_audit_purge_task_deletes_old_rows_with_the_injected_wall_clock_and_the_setting(app, db):
    """CA-43 : la purge supprime les lignes de plus de 90 j (horloge injectée)."""
    for days in (200, 100, 91, 89, 1):
        audit.record(db, 'x.y', actor=Actor.cli(), now=NOW - timedelta(days=days))
    s = Scheduler(app.config, default_tasks(wall_clock=lambda: NOW), clock=Clock(), lock=FakeLock())
    assert s.run_task('audit_purge') == 3
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'x.y'").fetchone()[0] == 2
    app.config[STORE_KEY].set_many(db, {'audit.retention_days': 30}, actor=Actor.cli())
    audit.record(db, 'x.z', actor=Actor.cli(), now=NOW - timedelta(days=45))
    assert s.run_task('audit_purge') >= 2                                # le réglage change la rétention, à chaud


def test_settings_reload_task_picks_up_a_change_made_by_another_process(app, db):
    store = app.config[STORE_KEY]
    other = database.connect(app.config['DB_PATH'])
    type(store)().set_many(other, {'USER_QUOTA_MB': 7}, actor=Actor.cli())
    other.close()
    assert store.get('USER_QUOTA_MB') == 2048
    Scheduler(app.config, default_tasks(), clock=Clock(), lock=FakeLock()).run_task('settings_reload')
    assert store.get('USER_QUOTA_MB') == 7


def test_inbox_and_interval_follow_the_settings_without_restart(app, db):
    """CA-21d : INBOX_ENABLED et INBOX_SCAN_INTERVAL pris en compte au tour suivant."""
    store, clock = app.config[STORE_KEY], Clock()
    app.config['INBOX_ENABLED'] = None                                   # la base décide (TestingConfig le coupait)
    s = Scheduler(app.config, default_tasks(), clock=clock, executor=SyncExecutor(), lock=FakeLock())
    store.set_many(db, {'INBOX_ENABLED': False}, actor=Actor.cli())
    assert 'inbox' not in s.tick()
    store.set_many(db, {'INBOX_ENABLED': True, 'INBOX_SCAN_INTERVAL': 600}, actor=Actor.cli())
    assert 'inbox' in s.tick()                                           # activée sans redémarrage
    clock.t += 100
    assert 'inbox' not in s.tick()
    store.set_many(db, {'INBOX_SCAN_INTERVAL': 60}, actor=Actor.cli())
    assert 'inbox' in s.tick()                                           # nouvel intervalle appliqué au tour suivant


def test_inbox_task_ingests_files_dropped_in_the_folder(app, db):
    inbox = app.config['DATA_DIR'] + '/inbox'
    os.makedirs(inbox, exist_ok=True)
    path = inbox + '/sortie.gpx'
    with open(path, 'wb') as f:
        f.write(make_gpx(line(40)).encode())
    old = os.stat(path).st_mtime - 60
    os.utime(path, (old, old))
    s = Scheduler(app.config, default_tasks(), clock=Clock(), lock=FakeLock())
    assert s.run_task('inbox')['created'] == 1
    assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 1


def test_dem_backfill_task_is_enabled_only_with_a_dem_url(app):
    tasks = {t.name: t for t in default_tasks()}
    assert not tasks['dem_backfill'].enabled(app.config)
    app.config['DEM_URL'] = 'http://dem:5000/v1/x'
    assert tasks['dem_backfill'].enabled(app.config)


def test_status_reports_lock_and_task_states(cfg):
    s = Scheduler(cfg, [counting_task('a', [], shared=True)], clock=Clock(), lock=FakeLock(True))
    s.tick()
    status = s.status()
    assert status['tasks']['a']['runs'] == 1 and status['tasks']['a']['shared'] is True
    assert set(status) == {'lock_held', 'tasks'}


def test_registering_a_task_adds_it_for_the_next_tick(cfg):
    calls = []
    s = Scheduler(cfg, [], clock=Clock(), lock=FakeLock())
    s.register(counting_task('routing_health', calls))
    assert s.tick() == ['routing_health'] and calls == ['routing_health']


def test_legacy_watcher_thread_and_module_wake_are_gone():
    from app.services import inbox_watcher
    assert not hasattr(inbox_watcher, 'InboxWatcher') and not hasattr(inbox_watcher, 'wake')
    assert background.LOCK_NAME == 'inbox.lock'
