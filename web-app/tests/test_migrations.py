"""Registre des migrations par nom (ADR-0012, admin-space T0, CA-47)."""

import logging
import sqlite3

import pytest

from app import db


def _names(conn):
    return [r['name'] for r in conn.execute("SELECT name FROM schema_migrations ORDER BY applied_at, rowid")]


def _columns(conn, table):
    return {r['name'] for r in conn.execute(f"PRAGMA table_info({table})")}


def _v5_database(tmp_path):
    """Base dans l'état actuel (5 migrations positionnelles), sans registre nommé."""
    path = str(tmp_path / 'v5.sqlite3')
    conn = db.connect(path)
    conn.executescript(db.SCHEMA)
    conn.commit()
    db.migrate(conn, named=[])
    conn.execute("DROP TABLE schema_migrations")
    conn.commit()
    return conn


def _add_col(column):
    """Migration idempotente de test : ajoute une colonne à users."""
    return lambda conn: db._add_column(conn, 'users', column, 'TEXT')


@pytest.fixture
def conn(tmp_path):
    c = _v5_database(tmp_path)
    yield c
    c.close()


def test_v5_database_gets_every_named_migration_recorded(conn):
    """CA-47 : base v5 ⇒ toutes les migrations nommées appliquées et enregistrées."""
    named = [('0006_a', _add_col('col_a')), ('0007_b', _add_col('col_b'))]
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)

    assert db.migrate(conn, named=named) == len(db.MIGRATIONS)

    assert _names(conn) == ['0006_a', '0007_b']
    assert {'col_a', 'col_b'} <= _columns(conn, 'users')
    # Les migrations nommées ne touchent jamais à user_version
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_applied_migrations_are_not_replayed(conn):
    calls = []
    named = [('0006_a', lambda c: calls.append('a'))]
    db.migrate(conn, named=named)
    db.migrate(conn, named=named)
    assert calls == ['a']


def test_reverse_order_applies_only_the_missing_one(conn):
    """CA-47 : B appliquée avant A (ordre inverse de la liste) ⇒ seule A est appliquée, B n'est pas rejouée."""
    calls = []
    named = [('0006_a', lambda c: (calls.append('a'), db._add_column(c, 'users', 'col_a', 'TEXT'))),
             ('0007_b', lambda c: (calls.append('b'), db._add_column(c, 'users', 'col_b', 'TEXT')))]
    # État laissé par une image de branche : B passée, A jamais vue
    conn.execute(db._MIGRATIONS_TABLE)
    db._add_column(conn, 'users', 'col_b', 'TEXT')
    conn.execute("INSERT INTO schema_migrations (name) VALUES ('0007_b')")
    conn.commit()

    db.migrate(conn, named=named)

    assert calls == ['a']
    assert sorted(_names(conn)) == ['0006_a', '0007_b']
    assert {'col_a', 'col_b'} <= _columns(conn, 'users')


def test_unknown_recorded_name_warns_and_startup_continues(conn, caplog):
    """CA-47 : nom inconnu du code ⇒ WARNING listant ces noms, démarrage OK, rien d'annulé."""
    conn.execute(db._MIGRATIONS_TABLE)
    conn.execute("INSERT INTO schema_migrations (name) VALUES ('0099_branch_only')")
    conn.commit()
    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[('0006_a', _add_col('col_a'))])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and '0099_branch_only' in warnings[0].getMessage()
    assert '0006_a' not in warnings[0].getMessage()
    assert '0099_branch_only' in _names(conn) and 'col_a' in _columns(conn, 'users')


def test_no_warning_when_every_recorded_name_is_known(conn, caplog):
    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[('0006_a', _add_col('col_a'))])
        db.migrate(conn, named=[('0006_a', _add_col('col_a'))])
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_replay_after_registry_emptied_by_hand_is_harmless(conn):
    """CA-47 : idempotence — rejouer sur une base qui l'a déjà (table vidée à la main) ne casse rien."""
    named = [('0006_a', lambda c: (db._add_column(c, 'users', 'col_a', 'TEXT'),
                                   c.execute("CREATE INDEX IF NOT EXISTS idx_users_col_a ON users (col_a)")))]
    db.migrate(conn, named=named)
    conn.execute("DELETE FROM schema_migrations")
    conn.commit()
    db.migrate(conn, named=named)
    assert _names(conn) == ['0006_a']


def test_failed_migration_is_rolled_back_and_not_recorded(conn):
    """Une migration qui échoue ne laisse ni trace partielle ni enregistrement, et arrête la suite."""
    def broken(c):
        c.execute("ALTER TABLE users ADD COLUMN half_done TEXT")
        raise RuntimeError("boom")

    ran = []
    named = [('0006_ok', _add_col('col_ok')), ('0007_broken', broken), ('0008_after', lambda c: ran.append(1))]
    with pytest.raises(RuntimeError):
        db.migrate(conn, named=named)

    assert _names(conn) == ['0006_ok']          # celle d'avant reste acquise (sa propre transaction)
    assert 'half_done' not in _columns(conn, 'users')
    assert ran == []
    assert not conn.in_transaction

    # Une fois corrigée, la reprise applique le reste
    db.migrate(conn, named=[('0006_ok', _add_col('col_ok')), ('0007_broken', _add_col('col_fixed')),
                            ('0008_after', lambda c: ran.append(1))])
    assert _names(conn) == ['0006_ok', '0007_broken', '0008_after'] and ran == [1]


def test_migration_already_applied_by_another_process_is_skipped(conn):
    """Deux processus au démarrage : celui qui perd la course relit la table sous verrou et passe."""
    conn.execute(db._MIGRATIONS_TABLE)
    other = db.connect(conn.execute("PRAGMA database_list").fetchone()['file'])
    other.execute("INSERT INTO schema_migrations (name) VALUES ('0006_a')")
    other.commit()
    other.close()
    calls = []
    assert db._apply_named(conn, '0006_a', lambda c: calls.append(1)) is False
    assert calls == []


def test_registry_rejects_duplicate_and_malformed_names(conn):
    noop = lambda c: None  # noqa: E731
    for bad in ([('0006_a', noop), ('0006_a', noop)],
                [('', noop)], [('Bad Name', noop)], [('../x', noop)], [("a'; DROP TABLE users;--", noop)],
                [('0006_a', None)], [(6, noop)]):
        with pytest.raises(ValueError):
            db.migrate(conn, named=bad)
    # Rien n'a été créé ni appliqué avant la validation
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'schema_migrations'").fetchone() is None


def test_shipped_registry_is_well_formed():
    db._check_registry(db.NAMED_MIGRATIONS)


def test_fresh_database_has_the_registry_table_and_every_named_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'NAMED_MIGRATIONS', [('0006_a', _add_col('col_a'))])
    path = str(tmp_path / 'new.sqlite3')
    db.init_db(path)
    db.init_db(path)  # idempotent
    c = db.connect(path)
    assert _names(c) == ['0006_a'] and 'col_a' in _columns(c, 'users')
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    c.close()


def test_registry_table_shape(conn):
    db.migrate(conn, named=[])
    info = {r['name']: r for r in conn.execute("PRAGMA table_info(schema_migrations)")}
    assert info['name']['pk'] == 1 and info['applied_at']['notnull'] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO schema_migrations (name) VALUES ('x')")
        conn.execute("INSERT INTO schema_migrations (name) VALUES ('x')")


# --- Durcissements issus de la relecture de T0 (C1, C2, C4) --------------------------------------

def test_migration_that_commits_itself_is_an_error_and_is_not_recorded(conn, caplog):
    """C1 : un commit() dans la migration casse l'atomicité ⇒ erreur explicite, nom non enregistré."""
    def commits(c):
        db._add_column(c, 'users', 'col_early', 'TEXT')
        c.commit()

    with caplog.at_level(logging.ERROR, logger='app.db'):
        with pytest.raises(RuntimeError, match='0006_commits.*transaction'):
            db.migrate(conn, named=[('0006_commits', commits)])
    assert _names(conn) == []
    assert not conn.in_transaction
    assert 'partiellement' in caplog.text


def test_migration_that_rolls_back_itself_is_an_error(conn):
    def rolls_back(c):
        c.rollback()

    with pytest.raises(RuntimeError):
        db.migrate(conn, named=[('0006_rb', rolls_back)])
    assert _names(conn) == []


def test_connect_pins_the_transaction_mode(tmp_path):
    """C2 : le mode est figé, pas le défaut de la version de Python."""
    c = db.connect(str(tmp_path / 'x.sqlite3'))
    assert c.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
    c.execute("BEGIN IMMEDIATE")      # ne doit pas lever
    c.rollback()
    c.close()


def test_positional_version_above_the_code_warns_and_is_left_alone(conn, caplog):
    """C4 : user_version > 5 ⇒ WARNING, rien d'appliqué ni modifié."""
    conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS) + 2}")
    conn.commit()
    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[])
    assert 'supérieure' in caplog.text
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) + 2


def test_no_positional_warning_on_a_normal_database(conn, caplog):
    with caplog.at_level(logging.WARNING, logger='app.db'):
        db.migrate(conn, named=[])
    assert 'supérieure' not in caplog.text
