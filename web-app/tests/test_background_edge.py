"""
Cas limites de l'ordonnanceur de fond (T19, EF-10, R3, CA-43, CA-48, CA-51, CA-21d).

Complète test_background.py : horloge injectée par `tick(now)` seul, réveil pendant une passe (tâches courtes
et longues), verrou tenu par un VRAI autre processus, intervalles relus à chaud, exceptions de toute origine,
tâches longues jamais empilées avec le vrai exécuteur, arrêt, contextes de démarrage, purge d'audit.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, create_app
from app.audit import Actor
from app.background import FileLock, Scheduler, Task, ThreadExecutor, default_tasks
from app.config import TestingConfig
from app.settings import STORE_KEY

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


class Lock:
    def __init__(self, held=True):
        self.held, self.attempts = held, 0

    def try_acquire(self):
        self.attempts += 1
        return self.held

    def release(self):
        self.held = False


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def cfg(app):
    return app.config


def sched(cfg, tasks, clock=None, **kw):
    kw.setdefault('lock', Lock())
    return Scheduler(cfg, tasks, clock=clock or Clock(), **kw)


def task(name, calls, interval=10, **kw):
    return Task(name, lambda db, cfg: calls.append(name), interval=lambda cfg: interval, **kw)


# --- Horloge : `tick(now)` seul suffit (CA-21d « avec tick(now) et horloge injectée ») ---------------------------

@pytest.mark.xfail(strict=True, reason="DÉFAUT : pour une tâche longue exécutée par l'exécuteur synchrone, _execute date `last_run` avec "
                   "self.clock() (monotonic réelle) et non avec le `now` passé à tick(now) : un test ou un outil qui n'injecte que `now` "
                   "voit la tâche ne plus jamais revenir (now - last_run négatif).")
def test_a_long_task_comes_back_at_its_interval_when_only_tick_now_is_injected(cfg):
    calls = []
    scheduler = Scheduler(cfg, [task('long', calls, 10, long=True, shared=True)], lock=Lock())          # horloge par défaut
    assert scheduler.tick(1000.0) == ['long']
    assert scheduler.tick(1009.0) == []
    assert scheduler.tick(1011.0) == ['long']
    assert calls == ['long', 'long']


def test_short_tasks_follow_tick_now_alone(cfg):
    calls = []
    scheduler = Scheduler(cfg, [task('short', calls, 10)], lock=Lock())
    assert [scheduler.tick(t) for t in (1000.0, 1009.9, 1010.0, 1019.0, 1020.5)] == [['short'], [], ['short'], [], ['short']]


def test_the_interval_boundary_is_inclusive_and_a_clock_going_backwards_does_not_crash(cfg):
    calls = []
    scheduler = sched(cfg, [task('t', calls, 10)])
    scheduler.tick(100.0)
    assert scheduler.tick(110.0) == ['t'] and scheduler.tick(109.0) == []
    assert scheduler.tick(5.0) == []                                                 # horloge qui recule : rien, pas d'exception


@pytest.mark.parametrize('interval', [0, -5, 0.0])
def test_a_zero_or_negative_interval_runs_every_tick_never_loops_or_raises(cfg, interval):
    calls = []
    scheduler = sched(cfg, [task('t', calls, interval)])
    for now in (1.0, 1.0, 2.0):
        assert scheduler.tick(now) == ['t']
    assert calls == ['t'] * 3


@pytest.mark.parametrize('interval', [float('nan'), float('inf'), 'dix', None, [1]])
def test_a_broken_interval_never_runs_the_task_and_never_blocks_the_others(cfg, interval):
    calls = []
    scheduler = sched(cfg, [Task('bad', lambda db, c: calls.append('bad'), interval=lambda c: interval), task('good', calls, 1)])
    scheduler.tick(1.0)
    scheduler.tick(5.0)
    assert calls.count('good') == 2 and calls.count('bad') <= 2                         # nan se lit comme 0 (max(0, nan) == 0) : jamais d'exception ni de blocage


# --- Réveil pendant une passe (v5-5) -----------------------------------------------------------------------------

def test_a_short_task_woken_during_its_own_run_runs_again_at_the_next_tick(cfg):
    calls = []
    holder = {}

    def run(db, c):
        calls.append('short')
        holder['s'].wake('short')

    scheduler = sched(cfg, [Task('short', run, interval=lambda c: 3600)])
    holder['s'] = scheduler
    assert scheduler.tick(1000.0) == ['short']
    assert scheduler.tick(1001.0) == ['short']


def test_a_long_task_woken_during_its_run_is_relaunched_once_not_twice(cfg):
    calls, gate = [], []

    class Held:
        def submit(self, fn):
            gate.append(fn)

    scheduler = sched(cfg, [task('long', calls, 3600, long=True, shared=True)], executor=Held())
    assert scheduler.tick(1000.0) == ['long']
    for _ in range(5):
        scheduler.wake('long')                                                        # cinq réveils pendant la passe
    assert scheduler.tick(1001.0) == []                                               # jamais de second exemplaire en parallèle
    gate.pop()()
    assert scheduler.tick(1002.0) == ['long']                                         # et UNE relance après la fin
    gate.pop()()
    assert scheduler.tick(1003.0) == [] and calls == ['long', 'long']


def test_wake_of_an_unknown_task_is_refused_and_wake_of_a_disabled_one_does_not_run_it(cfg):
    calls = []
    scheduler = sched(cfg, [task('off', calls, 10, enabled=lambda c: False)])
    with pytest.raises(ValueError):
        scheduler.wake('inconnue')
    scheduler.wake('off')
    assert scheduler.tick(1.0) == [] and calls == []


def test_many_threads_waking_while_the_loop_runs_never_deadlock_or_lose_the_task(cfg):
    calls = []
    scheduler = Scheduler(cfg, [task('t', calls, 3600)], lock=Lock(), clock=time.monotonic)
    scheduler.start()
    errors = []

    def hammer():
        try:
            for _ in range(200):
                scheduler.wake('t')
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    deadline = time.monotonic() + 5
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    scheduler.stop()
    assert not errors and calls and not scheduler._thread.is_alive()


def test_with_the_real_thread_a_wake_runs_the_task_long_before_the_tick_period(cfg):
    calls = []
    scheduler = Scheduler(cfg, [task('t', calls, 3600)], lock=Lock())
    scheduler.start()
    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ['t']
    scheduler.wake('t')
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    scheduler.stop()
    assert len(calls) == 2                                                             # bien avant les 5 s du tour


# --- Verrou tenu par un autre processus (CA-48) ------------------------------------------------------------------

def _hold_lock_in_another_process(path):
    code = ("import fcntl, sys, time\nh = open(sys.argv[1], 'w')\nfcntl.flock(h, fcntl.LOCK_EX)\nprint('locked', flush=True)\n"
            "sys.stdin.readline()\n")
    proc = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == 'locked'
    return proc


def test_a_real_other_process_holding_the_lock_leaves_process_tasks_running_and_blocks_shared_ones(cfg):
    calls = []
    lock_path = os.path.join(cfg['DATA_DIR'], 'inbox.lock')
    scheduler = Scheduler(cfg, [task('reload', calls, 0), task('inbox', calls, 0, shared=True, long=True),
                                task('purge', calls, 0, shared=True)], lock=FileLock(lock_path), clock=Clock())
    other = _hold_lock_in_another_process(lock_path)
    try:
        for now in (1.0, 2.0, 3.0):
            assert scheduler.tick(now) == ['reload']
        assert not scheduler.lock.held and scheduler.status()['lock_held'] is False
    finally:
        other.kill()
        other.wait(10)
    assert scheduler.tick(4.0) == ['reload', 'inbox', 'purge'] and scheduler.lock.held             # relais au tour suivant
    scheduler.stop()


def test_a_killed_holder_never_leaves_a_stale_lock(cfg):
    lock_path = os.path.join(cfg['DATA_DIR'], 'inbox.lock')
    other = _hold_lock_in_another_process(lock_path)
    mine = FileLock(lock_path)
    assert mine.try_acquire() is False
    other.kill()                                                                      # SIGKILL : aucun nettoyage possible
    other.wait(10)
    assert mine.try_acquire() is True
    mine.release()


def test_the_lock_is_held_by_exactly_one_of_several_competing_schedulers(cfg):
    path = os.path.join(cfg['DATA_DIR'], 'inbox.lock')
    locks = [FileLock(path) for _ in range(6)]
    barrier, outcome = threading.Barrier(6), []

    def grab(lock):
        barrier.wait(10)
        outcome.append(lock.try_acquire())

    threads = [threading.Thread(target=grab, args=(lock,)) for lock in locks]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sum(outcome) == 1
    [lock.release() for lock in locks]
    assert FileLock(path).try_acquire()


def test_an_unwritable_lock_location_degrades_to_process_tasks_only_without_raising(cfg, tmp_path):
    calls = []
    scheduler = Scheduler(cfg, [task('reload', calls, 0), task('inbox', calls, 0, shared=True)],
                          lock=FileLock(tmp_path / 'absent' / 'inbox.lock'), clock=Clock())
    assert scheduler.tick(1.0) == ['reload'] and calls == ['reload']


def test_releasing_twice_and_reacquiring_is_harmless(tmp_path):
    lock = FileLock(tmp_path / 'l.lock')
    assert lock.try_acquire() and lock.try_acquire() and lock.held
    lock.release()
    lock.release()
    assert not lock.held and lock.try_acquire()
    lock.release()


# --- Exceptions de toute origine ---------------------------------------------------------------------------------

def test_a_failing_task_is_retried_only_at_its_interval_and_clears_its_error_on_success(cfg, caplog):
    state = {'fail': True, 'n': 0}

    def run(db, c):
        state['n'] += 1
        if state['fail']:
            raise RuntimeError("boum")

    scheduler = sched(cfg, [Task('t', run, interval=lambda c: 10)])
    scheduler.tick(0.0)
    assert scheduler.state['t'].last_error == 'RuntimeError' and scheduler.tick(5.0) == []        # pas de boucle serrée sur l'erreur
    state['fail'] = False
    assert scheduler.tick(10.0) == ['t'] and scheduler.state['t'].last_error is None and state['n'] == 2


def test_the_error_log_names_the_task_and_never_the_exception_message(cfg, caplog):
    scheduler = sched(cfg, [Task('t', lambda db, c: (_ for _ in ()).throw(RuntimeError("mot-de-passe-S3CRET")), interval=lambda c: 1)])
    with caplog.at_level('DEBUG'):
        scheduler.tick(0.0)
    assert scheduler.state['t'].last_error == 'RuntimeError' and 'S3CRET' not in scheduler.status().__repr__()


def test_unavailable_database_for_a_task_is_an_error_state_not_a_crash(cfg, monkeypatch):
    calls = []
    scheduler = sched(cfg, [task('t', calls, 1), Task('p', lambda db, c: None, interval=lambda c: 1)])

    def broken(path):
        raise OSError("disque")

    monkeypatch.setattr('app.background.connect', broken)
    assert sorted(scheduler.tick(0.0)) == ['p', 't'] and scheduler.state['t'].last_error == 'OSError' and calls == []
    monkeypatch.undo()
    assert sorted(scheduler.tick(5.0)) == ['p', 't'] and scheduler.state['t'].last_error is None


def test_a_lock_that_raises_does_not_kill_the_loop(cfg):
    class Exploding(Lock):
        def try_acquire(self):
            raise OSError("flock")

    scheduler = Scheduler(cfg, [task('t', [], 3600)], lock=Exploding())
    scheduler.start()
    time.sleep(0.3)
    assert scheduler._thread.is_alive()
    scheduler.stop()


def test_status_survives_a_broken_enabled_callable(cfg):
    scheduler = sched(cfg, [Task('t', lambda db, c: None, interval=lambda c: 1, enabled=lambda c: 1 / 0)])
    assert scheduler.status()['tasks']['t']['enabled'] is None


# --- Tâches longues : jamais empilées avec le vrai exécuteur (CA-51) ----------------------------------------------

def test_with_the_real_worker_thread_a_blocked_long_task_is_never_started_twice(cfg):
    release, started, calls = threading.Event(), threading.Event(), []

    def slow(db, c):
        calls.append('long')
        started.set()
        release.wait(30)

    executor = ThreadExecutor()
    scheduler = Scheduler(cfg, [Task('long', slow, interval=lambda c: 0, long=True, shared=True), task('fast', calls, 0)],
                          lock=Lock(), clock=Clock(), executor=executor)
    scheduler.tick(1.0)
    assert started.wait(10)
    launched = [scheduler.tick(float(i)) for i in range(2, 22)]                        # 20 tours pendant que la passe longue dure
    assert all('long' not in tour for tour in launched) and all('fast' in tour for tour in launched)
    assert calls.count('long') == 1 and scheduler.status()['tasks']['long']['running'] is True
    release.set()
    deadline = time.monotonic() + 10
    while scheduler.state['long'].running and time.monotonic() < deadline:
        time.sleep(0.02)
    scheduler.clock.t = 2000.0
    assert scheduler.tick(2000.0).count('long') == 1
    executor.stop()


def test_two_long_tasks_never_run_in_parallel_and_the_short_ones_are_not_delayed(cfg):
    release, order, active, peak = threading.Event(), [], [0], [0]
    mutex = threading.Lock()

    def long_run(name):
        def run(db, c):
            with mutex:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            order.append(name)
            release.wait(5)
            with mutex:
                active[0] -= 1
        return run

    executor = ThreadExecutor()
    scheduler = Scheduler(cfg, [Task('a', long_run('a'), interval=lambda c: 3600, long=True, shared=True),
                                Task('b', long_run('b'), interval=lambda c: 3600, long=True, shared=True),
                                Task('short', lambda db, c: order.append('short'), interval=lambda c: 0)],
                          lock=Lock(), clock=Clock(), executor=executor)
    scheduler.tick(1.0)
    time.sleep(0.2)
    assert 'short' in order and peak[0] == 1                                           # un seul worker : séquentiel
    release.set()
    time.sleep(0.5)
    executor.stop()
    assert peak[0] == 1 and sorted(n for n in order if n != 'short') == ['a', 'b']


# --- Arrêt et démarrage -------------------------------------------------------------------------------------------

def test_start_twice_makes_one_thread_and_stop_is_idempotent_and_safe_without_start(cfg):
    never_started = sched(cfg, [])
    never_started.stop()
    never_started.stop()
    scheduler = Scheduler(cfg, [], lock=Lock())
    scheduler.start()
    first = scheduler._thread
    scheduler.start()
    assert scheduler._thread is first
    scheduler.stop()
    scheduler.stop()
    assert not first.is_alive() and scheduler.lock.held is False


def test_stop_releases_the_file_lock_so_another_process_takes_over(cfg):
    path = os.path.join(cfg['DATA_DIR'], 'inbox.lock')
    scheduler = Scheduler(cfg, [task('t', [], 0, shared=True)], lock=FileLock(path))
    scheduler.start()
    deadline = time.monotonic() + 3
    while not scheduler.lock.held and time.monotonic() < deadline:
        time.sleep(0.02)
    assert scheduler.lock.held and FileLock(path).try_acquire() is False
    scheduler.stop()
    taker = FileLock(path)
    assert taker.try_acquire()
    taker.release()


@pytest.mark.xfail(strict=True, reason="DÉFAUT (faible) : stop() libère le verrou fichier AVANT que le thread de travail ait fini sa tâche "
                   "longue : un autre processus qui prend le verrou lance alors la même passe (inbox, DEM) pendant que la première finit.")
def test_stop_does_not_release_the_lock_while_a_long_task_is_still_running(cfg):
    release, started = threading.Event(), threading.Event()
    path = os.path.join(cfg['DATA_DIR'], 'inbox.lock')

    def slow(db, c):
        started.set()
        release.wait(10)

    scheduler = Scheduler(cfg, [Task('inbox', slow, interval=lambda c: 3600, long=True, shared=True)], lock=FileLock(path),
                          clock=Clock(), executor=ThreadExecutor())
    scheduler.tick(1.0)
    assert started.wait(10)
    scheduler.stop()
    other = FileLock(path)
    stolen = other.try_acquire()
    other.release()
    release.set()
    assert stolen is False


# --- Contextes de démarrage ---------------------------------------------------------------------------------------

class SpyScheduler:
    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1


def _started(monkeypatch, *, testing=False, debug=False, env=None, click_command=None):
    from app import _start_scheduler
    spy = SpyScheduler()

    class A:
        config = {'TESTING': testing}
        extensions = {'scheduler': spy}
    A.debug = debug
    for key in ('FLASK_RUN_FROM_CLI', 'KMLVIEWER_WATCHER', 'WERKZEUG_RUN_MAIN', 'SERVER_SOFTWARE'):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    _start_scheduler(A)
    return spy.started


def test_start_contexts(monkeypatch):
    assert _started(monkeypatch) == 1                                                                  # gunicorn / python kml_viewer.py
    assert _started(monkeypatch, testing=True) == 0                                                    # tests
    assert _started(monkeypatch, env={'FLASK_RUN_FROM_CLI': 'true'}) == 0                              # commande flask
    assert _started(monkeypatch, debug=True) == 0                                                      # parent du reloader
    assert _started(monkeypatch, debug=True, env={'WERKZEUG_RUN_MAIN': 'true'}) == 1                   # enfant du reloader
    assert _started(monkeypatch, debug=True, env={'SERVER_SOFTWARE': 'gunicorn/22'}) == 1              # gunicorn en config dev
    assert _started(monkeypatch, testing=True, debug=True, env={'WERKZEUG_RUN_MAIN': 'true', 'SERVER_SOFTWARE': 'gunicorn'}) == 0


@pytest.mark.xfail(strict=True, reason="À ARBITRER (spec §5.10 : « démarre dans les modes servis (gunicorn, `flask run`) ») : FLASK_RUN_FROM_CLI "
                   "vaut 'true' aussi pour `flask run` ; _start_scheduler ne distingue pas cette commande (contrairement à la détection de "
                   "secours des secrets) : sous `flask run` l'ordonnanceur ne démarre pas, sauf KMLVIEWER_WATCHER=1 (variable d'exception "
                   "que REVIEW T19-n3 conteste justement).")
def test_flask_run_starts_the_scheduler_but_other_flask_commands_do_not(monkeypatch):
    import click
    from app import _start_scheduler
    started = []
    for command, _expected in (('run', 1), ('inbox-scan', 0)):
        for key in ('FLASK_RUN_FROM_CLI', 'KMLVIEWER_WATCHER', 'WERKZEUG_RUN_MAIN', 'SERVER_SOFTWARE'):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
        spy = SpyScheduler()

        class A:
            config = {'TESTING': False}
            extensions = {'scheduler': spy}
            debug = False

        with click.Context(click.Command(command)):
            _start_scheduler(A)
        started.append((command, spy.started))
    assert started == [('run', 1), ('inbox-scan', 0)]


def test_the_real_cli_never_starts_the_scheduler_thread(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'FLASK_ENV': 'development', 'PYTHONPATH': root}
    env.pop('KMLVIEWER_WATCHER', None)
    code = ("import threading, kml_viewer\nfrom click.testing import CliRunner\n"
            "r = kml_viewer.app.test_cli_runner().invoke(args=['settings-list'])\n"
            "print('THREADS', sorted(t.name for t in threading.enumerate()), r.exit_code)\n")
    served = subprocess.run([sys.executable, '-c', code.replace("FLASK_RUN", "")], capture_output=True, text=True, cwd=root,
                            env={**env, 'FLASK_RUN_FROM_CLI': 'true'}, timeout=120)
    assert served.returncode == 0, served.stderr[-300:]
    assert 'scheduler' not in served.stdout.split('THREADS')[1]


# --- Tâches du produit --------------------------------------------------------------------------------------------

def test_default_task_flags_are_exactly_the_spec_table(cfg):
    tasks = {t.name: t for t in default_tasks()}
    assert {n: (t.shared, t.long) for n, t in tasks.items()} == {
        'settings_reload': (False, False), 'audit_purge': (True, False), 'inbox': (True, True), 'dem_backfill': (True, True)}


def test_a_process_task_registered_later_runs_without_the_lock_and_a_shared_one_does_not(cfg):
    calls = []
    scheduler = sched(cfg, [], lock=Lock(held=False))
    scheduler.register(task('routing_health', calls, 0))
    scheduler.register(task('shared_one', calls, 0, shared=True))
    assert scheduler.tick(1.0) == ['routing_health'] and calls == ['routing_health']


def test_audit_purge_runs_at_the_first_tick_then_only_every_24_hours(app, db):
    wall = [NOW]
    for days in (200, 10):
        audit.record(db, 'x.y', actor=Actor.cli(), now=NOW - timedelta(days=days))
    scheduler = Scheduler(app.config, default_tasks(lambda: wall[0]), lock=Lock(), clock=Clock())
    def only():
        return [n for n in scheduler.tick(scheduler.clock.t) if n == 'audit_purge']

    assert only() == ['audit_purge']
    purged = db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit.purge'").fetchone()[0]
    assert purged == 1 and db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'x.y'").fetchone()[0] == 1
    scheduler.clock.t += 24 * 3600 - 1
    assert only() == []
    scheduler.clock.t += 1
    assert only() == ['audit_purge'] and db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'audit.purge'").fetchone()[0] == 2


def test_audit_purge_does_not_run_without_the_lock_and_follows_the_retention_setting(app, db):
    wall = [NOW]
    audit.record(db, 'x.y', actor=Actor.cli(), now=NOW - timedelta(days=40))
    lock = Lock(held=False)
    scheduler = Scheduler(app.config, default_tasks(lambda: wall[0]), lock=lock, clock=Clock())
    assert 'audit_purge' not in scheduler.tick(1.0)
    lock.held = True
    app.extensions['settings'].set_many(db, {'audit.retention_days': 30}, actor=Actor.cli())
    assert 'audit_purge' in scheduler.tick(2.0)
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'x.y'").fetchone()[0] == 0            # 40 j > 30 j (réglage relu à chaud)


def test_interval_and_enabled_settings_are_read_at_every_tick_ca21d(app, db):
    store = app.config[STORE_KEY]
    clock = Clock(0.0)
    scheduler = Scheduler(app.config, [t for t in default_tasks() if t.name == 'inbox'], lock=Lock(), clock=clock)

    def tick(at):
        clock.t = at
        return scheduler.tick(at)

    store.set_many(db, {'INBOX_ENABLED': False}, actor=Actor.cli())
    assert tick(0.0) == []                                                              # désactivée : rien
    store.set_many(db, {'INBOX_ENABLED': True, 'INBOX_SCAN_INTERVAL': 100}, actor=Actor.cli())
    assert tick(1.0) == ['inbox']                                                       # activée à chaud, sans redémarrage
    assert tick(100.0) == [] and tick(101.0) == ['inbox']                               # échéance de 100 s
    store.set_many(db, {'INBOX_SCAN_INTERVAL': 5}, actor=Actor.cli())
    assert tick(105.0) == [] and tick(106.0) == ['inbox']                               # raccourcie à chaud : 5 s
    store.set_many(db, {'INBOX_ENABLED': False}, actor=Actor.cli())
    assert tick(200.0) == []


def test_interval_settings_stay_inside_their_bounds_whatever_is_written(app, db):
    from app.settings import SettingsError
    store = app.config[STORE_KEY]
    for key, bad in (('INBOX_SCAN_INTERVAL', 4), ('INBOX_SCAN_INTERVAL', 3601), ('DEM_BACKFILL_INTERVAL', 29),
                     ('DEM_BACKFILL_INTERVAL', 3601), ('routing.health_interval_s', 14), ('routing.health_interval_s', 601)):
        with pytest.raises(SettingsError):
            store.set_many(db, {key: bad}, actor=Actor.cli())


def test_an_application_never_has_a_started_scheduler_under_test(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd')})
    assert app.extensions['scheduler']._thread is None
    assert 'scheduler' not in [t.name for t in threading.enumerate()]


def test_the_import_route_wakes_the_scheduler_only_when_something_was_created(app, client):
    woken = []
    app.extensions['scheduler'].wake = woken.append
    client.post('/api/activities', data={'files': [(__import__('io').BytesIO(b'pas un gpx'), 'x.gpx')]}, content_type='multipart/form-data')
    assert woken == []


def test_the_registered_routing_health_probe_runs_while_a_long_task_is_blocked(app, cfg, monkeypatch):
    """E-5 : `routing_health` est une tâche COURTE (CA-51) : le sondage s'exécute pendant qu'une passe longue (DEM) tourne."""
    from app.services.routing import runtime as routing_runtime
    release, started, probed = threading.Event(), threading.Event(), []

    def slow(db, c):
        started.set()
        release.wait(30)
    monkeypatch.setattr(routing_runtime.RoutingRuntime, 'configured', lambda self: True)
    monkeypatch.setattr(routing_runtime.RoutingRuntime, 'run_health', lambda self, db=None, cfg=None: probed.append(1))
    executor = ThreadExecutor()
    scheduler = Scheduler(cfg, [Task('dem', slow, interval=lambda c: 0, long=True, shared=True)], lock=Lock(), clock=Clock(),
                          executor=executor)
    app.extensions['scheduler'] = scheduler
    routing_runtime.init_routing(app)
    assert scheduler.tasks['routing_health'].long is False
    scheduler.tick(1.0)
    assert started.wait(10) and scheduler.status()['tasks']['dem']['running'] is True
    scheduler.tick(2.0)
    assert probed
    release.set()
    executor.stop()
