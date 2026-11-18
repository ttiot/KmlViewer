from app import db


def test_fresh_database_is_at_latest_schema_version(tmp_path):
    path = str(tmp_path / 'new.sqlite3')
    db.init_db(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()


def test_old_database_is_migrated_without_data_loss(tmp_path):
    """Base créée avant l'ajout de dem_status : la colonne apparaît, les lignes restent."""
    path = str(tmp_path / 'old.sqlite3')
    conn = db.connect(path)
    conn.executescript(db.SCHEMA.replace(
        "    dem_status    TEXT,  -- NULL : jamais tenté ; 'ok' ; 'no_coverage' : hors dalles disponibles\n", ''))
    conn.execute("INSERT INTO users (sub, username) VALUES ('u1', 'alice')")
    conn.execute("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points) "
                 "VALUES (1, 'Sortie', 'a.gpx', 'abc', 'upload', '2026-01-01T00:00:00Z', x'00')")
    conn.commit()
    assert 'dem_status' not in {r['name'] for r in conn.execute("PRAGMA table_info(activities)")}
    conn.close()

    db.init_db(path)
    db.init_db(path)  # idempotent
    conn = db.connect(path)
    row = conn.execute("SELECT name, dem_status FROM activities").fetchone()
    assert row['name'] == 'Sortie' and row['dem_status'] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()
