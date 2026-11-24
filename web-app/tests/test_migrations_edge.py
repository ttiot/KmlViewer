"""
Cas limites du registre des migrations par nom (ADR-0012, admin-space §7, CA-47).

Complète test_migrations.py : base héritée à une version intermédiaire, ordre d'application,
renommage d'une migration avant fusion, base « en avance », reprise après interruption,
et démarrages concurrents sur un vrai fichier.
"""

import logging
import sqlite3
import threading
import time

import pytest

from app import db


def _names(conn):
    return {r['name'] for r in conn.execute("SELECT name FROM schema_migrations")}


def _columns(conn, table):
    return {r['name'] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(column, calls=None, tag=None):
    def run(conn):
        if calls is not None:
            calls.append(tag or column)
        db._add_column(conn, 'users', column, 'TEXT')
    return run


def _fresh(tmp_path, name='db.sqlite3'):
    path = str(tmp_path / name)
    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    conn.commit()
    return path, conn


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# --- Base existante : données et versions intermédiaires ---------------------------------------

def test_v5_database_keeps_its_data(tmp_path):
    """Une base v5 réelle (avec des lignes) garde toutes ses données après T0."""
    _, conn = _fresh(tmp_path)
    db.migrate(conn, named=[])
    conn.execute("DROP TABLE schema_migrations")
    conn.execute("INSERT INTO users (sub, username) VALUES ('s1', 'alice')")
    conn.execute("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points) "
                 "VALUES (1, 'sortie', 'a.gpx', 'abc', 'upload', '2026-01-01T00:00:00Z', x'00')")
    conn.commit()

    db.migrate(conn, named=[('0006_a', _add_col('col_a'))])

    assert conn.execute("SELECT username FROM users").fetchall()[0][0] == 'alice'
    assert conn.execute("SELECT count(*) FROM activities").fetchone()[0] == 1
    assert _names(conn) == {'0006_a'} and 'col_a' in _columns(conn, 'users')
    conn.close()


def test_positional_migrations_run_before_named_ones(tmp_path):
    """Base restée en v3 : les positionnelles 4 et 5 passent d'abord, la nommée peut s'appuyer dessus."""
    _, conn = _fresh(tmp_path)
    db.migrate(conn, named=[])
    conn.execute("DROP INDEX IF EXISTS idx_users_hr")
    conn.execute("ALTER TABLE users DROP COLUMN hr_max")     # état d'avant la migration 4
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    assert 'hr_max' not in _columns(conn, 'users')

    def needs_profile(c):
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_hr ON users (hr_max)")   # échoue si 4 n'a pas eu lieu

    assert db.migrate(conn, named=[('0006_idx', needs_profile)]) == len(db.MIGRATIONS)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert _names(conn) == {'0006_idx'}
    conn.close()


# --- Ordre et reprise partielle ----------------------------------------------------------------

def test_missing_migrations_are_applied_in_list_order(tmp_path):
    _, conn = _fresh(tmp_path)
    calls = []
    named = [(f'000{i}_m', _add_col(f'c{i}', calls, f'm{i}')) for i in (6, 7, 8, 9)]
    db.migrate(conn, named=named)
    assert calls == ['m6', 'm7', 'm8', 'm9']
    conn.close()


def test_partially_applied_registry_resumes_with_the_rest_only(tmp_path):
    """A passée, B et C manquantes (ex. arrêt après A) ⇒ B puis C, A n'est pas rejouée."""
    _, conn = _fresh(tmp_path)
    calls = []
    named = [('0006_a', _add_col('ca', calls)), ('0007_b', _add_col('cb', calls)), ('0008_c', _add_col('cc', calls))]
    db.migrate(conn, named=named[:1])
    assert calls == ['ca']
    db.migrate(conn, named=named)
    assert calls == ['ca', 'cb', 'cc']
    assert _names(conn) == {'0006_a', '0007_b', '0008_c'}
    conn.close()


def test_gap_in_the_middle_is_filled_not_skipped(tmp_path):
    """A et C enregistrées, B jamais vue (image de branche) ⇒ B est appliquée, ni A ni C rejouées."""
    _, conn = _fresh(tmp_path)
    calls = []
    named = [('0006_a', _add_col('ca', calls)), ('0007_b', _add_col('cb', calls)), ('0008_c', _add_col('cc', calls))]
    conn.execute(db._MIGRATIONS_TABLE)
    conn.executemany("INSERT INTO schema_migrations (name) VALUES (?)", [('0006_a',), ('0008_c',)])
    conn.commit()
    db.migrate(conn, named=named)
    assert calls == ['cb']
    assert _names(conn) == {'0006_a', '0007_b', '0008_c'}
    conn.close()


# --- Renommage avant fusion (v5-n3) ------------------------------------------------------------

def test_renamed_migration_is_applied_and_old_name_is_reported(tmp_path, caplog):
    """Base passée par l'ancienne image (0006_a) ; le code livre 0006_a_v2 ⇒ v2 appliquée, WARNING sur 0006_a."""
    _, conn = _fresh(tmp_path)
    conn.execute(db._MIGRATIONS_TABLE)
    db._add_column(conn, 'users', 'col_old', 'TEXT')
    conn.execute("INSERT INTO schema_migrations (name) VALUES ('0006_a')")
    conn.commit()
    calls = []

    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[('0006_a_v2', _add_col('col_new', calls))])

    assert calls == ['col_new']
    assert _names(conn) == {'0006_a', '0006_a_v2'}                # l'ancien nom n'est ni effacé ni confondu
    msgs = _warnings(caplog)
    assert len(msgs) == 1 and '0006_a' in msgs[0] and '0006_a_v2' not in msgs[0]
    conn.close()


def test_a_name_prefix_is_not_a_match(tmp_path):
    """Nom exact : '0006_a' enregistré ne vaut pas pour '0006_a_v2', ni l'inverse."""
    _, conn = _fresh(tmp_path)
    calls = []
    conn.execute(db._MIGRATIONS_TABLE)
    conn.execute("INSERT INTO schema_migrations (name) VALUES ('0006_a_v2')")
    conn.commit()
    db.migrate(conn, named=[('0006_a', _add_col('ca', calls))])
    assert calls == ['ca']
    conn.close()


def test_unknown_names_are_listed_sorted_and_only_once(tmp_path, caplog):
    _, conn = _fresh(tmp_path)
    conn.execute(db._MIGRATIONS_TABLE)
    conn.executemany("INSERT INTO schema_migrations (name) VALUES (?)", [('0099_z',), ('0050_y',), ('0006_a',)])
    conn.commit()
    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[('0006_a', _add_col('ca'))])
    msgs = _warnings(caplog)
    assert len(msgs) == 1
    assert msgs[0].index('0050_y') < msgs[0].index('0099_z') and '0006_a' not in msgs[0]
    conn.close()


# --- Base « en avance » ------------------------------------------------------------------------

def test_database_ahead_of_the_code_is_left_alone(tmp_path, caplog):
    """user_version supérieur à celui du code (retour arrière de version) : pas d'erreur, pas de rétrogradation."""
    _, conn = _fresh(tmp_path)
    ahead = len(db.MIGRATIONS) + 3
    conn.execute(f"PRAGMA user_version = {ahead}")
    conn.commit()
    with caplog.at_level(logging.WARNING, logger='app.db'):
        assert db.migrate(conn, named=[('0006_a', _add_col('ca'))]) == ahead
    assert conn.execute("PRAGMA user_version").fetchone()[0] == ahead
    assert 'ca' in _columns(conn, 'users') and _names(conn) == {'0006_a'}
    conn.close()


def test_database_with_extra_unknown_migrations_keeps_their_effects(tmp_path):
    """Un nom inconnu ne provoque aucune annulation : sa colonne et son enregistrement restent."""
    _, conn = _fresh(tmp_path)
    conn.execute(db._MIGRATIONS_TABLE)
    db._add_column(conn, 'users', 'from_future', 'TEXT')
    conn.execute("INSERT INTO schema_migrations (name) VALUES ('0200_future')")
    conn.commit()
    db.migrate(conn, named=[])
    assert 'from_future' in _columns(conn, 'users') and _names(conn) == {'0200_future'}
    conn.close()


# --- Échec en cours de migration ---------------------------------------------------------------

def test_failure_releases_the_write_lock(tmp_path):
    """Après un échec, un autre processus peut écrire : le verrou pris par BEGIN IMMEDIATE est rendu."""
    path, conn = _fresh(tmp_path)

    def broken(_c):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db.migrate(conn, named=[('0006_broken', broken)])

    other = db.connect(path)
    other.execute("PRAGMA busy_timeout = 200")
    other.execute("BEGIN IMMEDIATE")      # lèverait OperationalError « locked » si le verrou était resté
    other.rollback()
    other.close()
    conn.close()


def test_base_exception_also_rolls_back(tmp_path):
    """Interruption (KeyboardInterrupt, SystemExit) en cours de migration : rien de partiel, rien d'enregistré."""
    _, conn = _fresh(tmp_path)

    def interrupted(c):
        c.execute("ALTER TABLE users ADD COLUMN half TEXT")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        db.migrate(conn, named=[('0006_int', interrupted)])
    assert 'half' not in _columns(conn, 'users') and _names(conn) == set() and not conn.in_transaction
    conn.close()


def test_effect_and_record_are_atomic(tmp_path):
    """Si l'enregistrement échoue, l'effet de la migration est annulé aussi (même transaction)."""
    _, conn = _fresh(tmp_path)

    def records_itself(c):
        c.execute("ALTER TABLE users ADD COLUMN twice TEXT")
        c.execute("INSERT INTO schema_migrations (name) VALUES ('0006_self')")   # l'INSERT du registre entrera en conflit

    with pytest.raises(Exception) as exc:
        db.migrate(conn, named=[('0006_self', records_itself)])
    assert 'UNIQUE' in str(exc.value) or 'constraint' in str(exc.value).lower()
    assert 'twice' not in _columns(conn, 'users') and _names(conn) == set()
    conn.close()


def test_failed_migration_leaves_the_data_untouched(tmp_path):
    """Migration qui modifie des données puis échoue : les données d'avant sont intactes."""
    _, conn = _fresh(tmp_path)
    conn.execute("INSERT INTO users (sub, username) VALUES ('s1', 'alice')")
    conn.commit()

    def corrupts(c):
        c.execute("UPDATE users SET username = 'mallory'")
        c.execute("DROP TABLE users")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db.migrate(conn, named=[('0006_bad', corrupts)])
    assert conn.execute("SELECT username FROM users").fetchone()[0] == 'alice'
    conn.close()


def test_startup_fails_loudly_and_retries_next_time(tmp_path, monkeypatch):
    """init_db propage l'échec (le service ne démarre pas sur un schéma à moitié migré) puis reprend."""
    path = str(tmp_path / 'boot.sqlite3')
    state = {'fail': True}

    def flaky(c):
        db._add_column(c, 'users', 'col_flaky', 'TEXT')
        if state['fail']:
            raise RuntimeError("disque plein")

    monkeypatch.setattr(db, 'NAMED_MIGRATIONS', [('0006_flaky', flaky), ('0007_next', _add_col('col_next'))])
    with pytest.raises(RuntimeError):
        db.init_db(path)

    state['fail'] = False
    db.init_db(path)
    conn = db.connect(path)
    assert _names(conn) == {'0006_flaky', '0007_next'} and {'col_flaky', 'col_next'} <= _columns(conn, 'users')
    conn.close()


# --- Idempotence de ce qui sera livré ----------------------------------------------------------

def test_every_shipped_migration_survives_a_replay(tmp_path):
    """
    Garde-fou pour T2, T3, T4… : chaque migration livrée dans NAMED_MIGRATIONS, rejouée sur une base
    qui l'a déjà (registre vidé), ne lève rien et rétablit le registre.
    """
    path = str(tmp_path / 'ship.sqlite3')
    db.init_db(path)
    conn = db.connect(path)
    first = _names(conn)
    assert first == {name for name, _ in db.NAMED_MIGRATIONS}
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    conn.close()

    db.init_db(path)

    conn = db.connect(path)
    assert _names(conn) == first
    conn.close()


# --- Deux processus au démarrage ---------------------------------------------------------------

def test_second_starter_waits_for_the_first_and_does_not_replay(tmp_path):
    """
    A est en pleine migration (verrou d'écriture tenu) ; B démarre : il attend, relit le registre
    sous verrou, constate que A a fini et n'applique rien. La migration s'exécute une seule fois.
    """
    path, conn = _fresh(tmp_path)
    conn.close()
    calls, errors = [], []
    in_migration, release = threading.Event(), threading.Event()

    def slow(c):
        calls.append('run')
        c.execute("ALTER TABLE users ADD COLUMN slow_col TEXT")
        in_migration.set()
        assert release.wait(10)

    def starter():
        try:
            c = db.connect(path)
            try:
                db.migrate(c, named=[('0006_slow', slow)])
            finally:
                c.close()
        except Exception as exc:        # pragma: no cover - remonté par l'assertion finale
            errors.append(exc)

    first = threading.Thread(target=starter)
    first.start()
    assert in_migration.wait(10)
    second = threading.Thread(target=starter)
    second.start()
    second.join(0.5)
    assert second.is_alive(), "le second démarreur aurait dû attendre le verrou"
    release.set()
    first.join(10)
    second.join(10)

    assert not errors, errors
    assert calls == ['run']
    check = db.connect(path)
    assert _names(check) == {'0006_slow'} and 'slow_col' in _columns(check, 'users')
    check.close()


def test_many_simultaneous_starts_apply_each_migration_once(tmp_path, monkeypatch):
    """
    Quatre démarrages simultanés (init_db) sur une base existante : chaque migration nommée passe
    une seule fois, sans erreur. La base est créée avant la course pour isoler le registre
    (la création concurrente d'un fichier neuf est couverte par le test xfail plus bas).
    """
    path, seed = _fresh(tmp_path, 'race.sqlite3')
    db.migrate(seed, named=[])
    seed.close()
    calls, errors = [], []
    lock = threading.Lock()

    def counted(column):
        def run(c):
            with lock:
                calls.append(column)
            db._add_column(c, 'users', column, 'TEXT')
        return run

    monkeypatch.setattr(db, 'NAMED_MIGRATIONS', [('0006_a', counted('ca')), ('0007_b', counted('cb'))])
    barrier = threading.Barrier(4)

    def start():
        try:
            barrier.wait(10)
            db.init_db(path)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]

    assert not errors, errors
    assert sorted(calls) == ['ca', 'cb']
    conn = db.connect(path)
    assert _names(conn) == {'0006_a', '0007_b'}
    conn.close()


@pytest.mark.xfail(strict=True, reason="Hors T0 : connect() lance PRAGMA journal_mode = WAL ; face à un verrou "
                   "d'écriture tenu par un autre processus sur un fichier pas encore en WAL, SQLite répond "
                   "« database is locked » sans attendre busy_timeout.")
def test_connect_waits_for_a_writer_on_a_file_not_yet_in_wal_mode(tmp_path):
    """Déterministe : l'autre démarreur tient le verrou d'écriture 0,5 s ; connect() doit attendre, pas échouer."""
    path = str(tmp_path / 'brand_new.sqlite3')
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("CREATE TABLE t (x)")
    other.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.5, lambda: other.execute("COMMIT"))
    timer.start()
    try:
        db.connect(path).close()
    finally:
        timer.join(5)
        other.close()


@pytest.mark.xfail(strict=True, reason="Hors registre nommé : les migrations POSITIONNELLES (1-5) lisent "
                   "user_version puis agissent sans verrou ; un second démarreur qui lit la version pendant "
                   "qu'un premier migre rejoue la même migration (en vrai : « duplicate column name »).")
def test_positional_migration_runs_once_when_two_starters_overlap(tmp_path, monkeypatch):
    """
    Déterministe : la migration 1 dure 0,4 s ; le second démarreur arrive après 0,1 s, quand
    user_version vaut encore 0. Chaque migration ne doit s'exécuter qu'une fois.
    """
    path, seed = _fresh(tmp_path, 'legacy.sqlite3')
    seed.close()
    calls, errors = [], []
    lock = threading.Lock()

    def slow(_conn):
        with lock:
            calls.append('m1')
        time.sleep(0.4)

    monkeypatch.setattr(db, 'MIGRATIONS', [slow, lambda _conn: calls.append('m2')])

    def start():
        try:
            conn = db.connect(path)
            try:
                db.migrate(conn, named=[])
            finally:
                conn.close()
        except Exception as exc:
            errors.append(exc)

    first = threading.Thread(target=start)
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=start)
    second.start()
    first.join(30)
    second.join(30)

    assert not errors, errors
    assert sorted(calls) == ['m1', 'm2'], calls
