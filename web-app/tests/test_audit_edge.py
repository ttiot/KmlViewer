"""
Cas limites du journal d'audit (T2, EF-9.1 à 9.3, CA-24, m8, m9).

Complète test_audit.py : masquage (casse, profondeur, listes, noms de clés réalistes), injection
et bornes des filtres, pagination complète, purge (transaction de l'appelant, identifiants jamais
réutilisés), enregistrement dans la transaction de l'appelant, concurrence, acteur `cli`.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, db

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / 'a.sqlite3')
    db.init_db(p)
    return p


@pytest.fixture
def conn(path):
    c = db.connect(path)
    yield c
    c.close()


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]


def details_of(conn, details):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), details=details)
    return conn.execute("SELECT details FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0]


# --- Masquage ---------------------------------------------------------------------------------

@pytest.mark.parametrize('key', ['password', 'PASSWORD', 'Password', 'new_password', 'old_Password', 'passwd', 'smtp_pass',
                                 'token', 'TOKEN', 'access_token', 'id_token', 'invite_token', 'reset_token',
                                 'secret', 'client_secret', 'secrets_key', 'totp_secret', 'hash', 'password_hash',
                                 'api_key', 'apikey', 'API_KEY', 'credential', 'credentials', 'cookie', 'Set-Cookie',
                                 'otp', 'totp', 'recovery_codes', 'recovery', 'code', 'CODE'])
def test_sensitive_keys_are_masked_whatever_the_case_and_the_value_type(conn, key):
    for value in ('hunter2-valeur', 123456789, ['hunter2-valeur'], {'hunter2-valeur': 'hunter2-valeur'}, None):
        text = details_of(conn, {key: value})
        assert 'hunter2-valeur' not in text and '123456789' not in text, (key, value)
        assert json.loads(text)[key] == audit.MASK


def test_masking_reaches_every_depth_and_every_container(conn):
    details = {'a': {'b': {'c': [{'d': ({'password': 'S1'},)}]}}, 'liste': [[{'token': 'S2'}]],
               'tuple': ({'api_key': 'S3'},), 'ok': 'visible'}
    text = details_of(conn, details)
    assert not any(s in text for s in ('S1', 'S2', 'S3')) and json.loads(text)['ok'] == 'visible'


def test_a_masked_key_hides_its_whole_subtree(conn):
    text = details_of(conn, {'credentials': {'user': 'alice', 'x': 1}})
    assert json.loads(text) == {'credentials': audit.MASK}


def test_top_level_containers_and_scalars_are_accepted(conn):
    assert json.loads(details_of(conn, [{'token': 'S'}, 'visible'])) == [{'token': audit.MASK}, 'visible']
    assert json.loads(details_of(conn, 'texte')) == 'texte'
    assert json.loads(details_of(conn, 0)) == 0
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'))
    assert rows(conn)[-1]['details'] is None


def test_non_string_keys_are_handled(conn):
    text = details_of(conn, {1: 'v', b'password': 'S', ('t',): 'w', None: 'n'})
    assert 'S"' not in text.replace(audit.MASK, '') and json.loads(text)


def test_masking_does_not_mutate_the_callers_object(conn):
    details = {'password': 'S', 'n': {'token': 'T'}}
    details_of(conn, details)
    assert details == {'password': 'S', 'n': {'token': 'T'}}


def test_masking_is_idempotent_on_already_masked_values(conn):
    assert json.loads(details_of(conn, {'password': audit.MASK, 'n': audit.MASK}))['n'] == audit.MASK


@pytest.mark.parametrize('key', ['status_code', 'key_name', 'passage', 'tokens_count', 'keyboard', 'monkey', 'codec', 'target'])
def test_harmless_keys_are_not_hidden_needlessly_unless_they_look_sensitive(conn, key):
    """Documente les faux positifs : seules les clés contenant un fragment sensible sont masquées (« key » et « code » : égalité)."""
    value = json.loads(details_of(conn, {key: 'v'}))[key]
    assert value in ('v', audit.MASK)
    if key in ('status_code', 'key_name', 'keyboard', 'monkey', 'codec', 'target'):
        assert value == 'v'


def test_realistic_secret_key_names_are_masked(conn):
    leaks = []
    for key in ('mfa_code', 'verification_code', 'invite_code', 'backup_codes', 'authorization', 'Authorization', 'bearer',
                'jwt', 'x-api-key', 'API-KEY', 'private_key', 'access_key', 'signing_key', 'session_key', 'pwd', 'mdp',
                'jeton', 'mot_de_passe'):
        if json.loads(details_of(conn, {key: 'VALEUR-SECRETE'}))[key] != audit.MASK:
            leaks.append(key)
    assert not leaks, leaks


def test_ip_addresses_do_not_end_up_in_details(conn):
    for key in ('ip', 'client_ip', 'remote_addr'):
        assert json.loads(details_of(conn, {key: '203.0.113.9'}))[key] == audit.MASK


def test_details_total_size_limit_is_exact_and_counted_in_characters(conn):
    def many(n, value='v'):
        return {f'k{i:04d}': value for i in range(n)}

    n = 1
    while len(json.dumps(many(n + 1), ensure_ascii=False)) <= audit.MAX_DETAILS_CHARS:
        n += 1
    assert len(json.loads(details_of(conn, many(n)))) == n                          # pile sous la limite : gardé
    assert json.loads(details_of(conn, many(n + 1))) == {'tronqué': True}           # au-dessus : remplacé
    assert len(json.loads(details_of(conn, many(n, 'é')))) == n                    # caractères, pas octets


def test_long_values_are_shortened_but_keys_and_structure_survive(conn):
    got = json.loads(details_of(conn, {'old': 'x' * 5000, 'items': list(range(100)), 'deep': {'a': 'y' * 500}}))
    assert len(got['old']) < audit.MAX_VALUE_CHARS + 20 and got['old'].endswith('[tronqué]')
    assert len(got['items']) == audit.MAX_LIST_ITEMS + 1 and got['deep']['a'].endswith('[tronqué]')


def test_secret_never_survives_shortening_or_truncation(conn):
    text = details_of(conn, {'password': 'SECRET' * 2000, 'blob': 'x' * 5000, 'n': {'token': 'TOK' * 900}})
    assert 'SECRET' not in text and 'TOK' not in text and len(text) <= audit.MAX_DETAILS_CHARS


@pytest.mark.parametrize('shape', ['circular', 'deep'])
def test_hostile_nesting_does_not_crash_the_audited_action(conn, shape):
    if shape == 'circular':
        details = {}
        details['self'] = details
    else:
        details = cur = {}
        for _ in range(3000):
            cur['n'] = {}
            cur = cur['n']
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), details=details)      # tracée, tronquée ou refusée proprement : jamais RecursionError
    assert len(rows(conn)) == 1


def test_stored_details_are_strict_json(conn):
    text = details_of(conn, {'x': float('nan'), 'y': float('inf')})
    json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


# --- Champs libres et acteur ------------------------------------------------------------------

def test_bidi_and_zero_width_characters_are_stripped_from_free_text(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'ad‮min​'), target_id='a⁦b\u0085c d', ip='1.2.3.4‮')
    row = rows(conn)[0]
    assert row['actor_label'] == 'admin' and row['target_id'] == 'abcd' and row['ip'] == '1.2.3.4'


def test_target_id_zero_and_empty_target_type_are_kept_or_nulled_correctly(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), target_id=0, target_type='')
    row = rows(conn)[0]
    assert row['target_id'] == '0' and row['target_type'] is None


@pytest.mark.parametrize('label', ['\n\x00', '\u202e', '', None, 5, b'x', '   ', ' '])
def test_an_actor_label_that_cleans_to_nothing_or_is_not_text_is_refused(conn, label):
    with pytest.raises(ValueError):
        audit.Actor.user(1, label)
    assert rows(conn) == []


@pytest.mark.parametrize('name', ['cli', 'system'])
def test_a_user_named_cli_or_system_is_typed_user_so_it_cannot_impersonate(conn, name):
    """Arbitré par la spec (EF-9.1, CA-24b) : le type est structurel, le libellé ne prouve rien."""
    actor = audit.Actor.from_user({'id': 5, 'username': name})
    assert actor.type == 'user' and actor.id == 5
    audit.record(conn, 'x.y', actor=actor)
    assert rows(conn)[0]['actor_type'] == 'user'


def test_actor_from_user_accepts_real_rows(conn):
    conn.execute("INSERT INTO users (sub, username, email) VALUES ('s', 'carol', 'c@x.org')")
    conn.commit()
    user = conn.execute("SELECT * FROM users WHERE sub = 's'").fetchone()
    assert audit.Actor.from_user(user) == audit.Actor('user', user['id'], 'carol')


def test_cli_rows_have_a_null_actor_id_and_the_cli_type_in_the_database(conn):
    audit.record(conn, 'secrets.rotate', actor=audit.Actor.cli())
    assert conn.execute("SELECT actor_type, actor_id, actor_label FROM audit_log").fetchone()[:] == ('cli', None, '')


def test_row_survives_deletion_of_its_actor_and_keeps_the_dangling_id(conn):
    conn.execute("INSERT INTO users (sub, username) VALUES ('s', 'dave')")
    conn.commit()
    uid = conn.execute("SELECT id FROM users").fetchone()[0]
    audit.record(conn, 'x.y', actor=audit.Actor.user(uid, 'dave'))
    conn.execute("DELETE FROM users")
    conn.commit()
    assert (rows(conn)[0]['actor_id'], rows(conn)[0]['actor_label']) == (uid, 'dave')


@pytest.mark.parametrize('action', ['x', 'a.b.c', 'a_b.c_d', 'a' * 64, 'a1.b2'])
def test_valid_action_shapes(conn, action):
    audit.record(conn, action, actor=audit.Actor.user(1, 'a'))
    assert rows(conn)[0]['action'] == action


@pytest.mark.parametrize('action', ['x.y\n', ' x.y', 'x.y ', 'x-y', 'X.y', 'é.y', '.x', 'x.y;', 'x\x00y'])
def test_action_must_match_exactly_no_trailing_newline_tricks(conn, action):
    with pytest.raises(ValueError):
        audit.record(conn, action, actor=audit.Actor.user(1, 'a'))


def test_ids_are_never_reused_after_a_purge(conn):
    first = audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=NOW - timedelta(days=400))
    audit.purge(conn, 30, now=NOW)
    conn.execute("DELETE FROM audit_log")
    conn.commit()
    assert audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a')) > first


def test_aware_non_utc_now_is_converted_to_utc(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))))
    assert rows(conn)[0]['at'] == '2026-01-01T10:00:00Z'


# --- Transaction de l'appelant ----------------------------------------------------------------

def test_a_refused_record_leaves_the_callers_transaction_intact(conn):
    """Action invalide dans une transaction de l'appelant : l'erreur ne défait pas ce qui précède."""
    with db.write_transaction(conn):
        conn.execute("INSERT INTO users (sub, username) VALUES ('s', 'erin')")
        with pytest.raises(ValueError):
            audit.record(conn, 'Bad Action', actor=audit.Actor.user(1, 'a'))
        audit.record(conn, 'user.create', actor=audit.Actor.user(1, 'a'))
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1 and len(rows(conn)) == 1


def test_record_inside_a_caller_transaction_is_invisible_to_others_until_commit(path, conn):
    other = db.connect(path)
    try:
        with db.write_transaction(conn):
            audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'))
            assert other.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
        assert other.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    finally:
        other.close()


def test_the_action_and_its_trace_roll_back_together(conn):
    """Un changement et sa ligne d'audit : si la suite échoue, ni l'un ni l'autre."""
    with pytest.raises(RuntimeError), db.write_transaction(conn):
        conn.execute("INSERT INTO users (sub, username) VALUES ('s', 'frank')")
        audit.record(conn, 'user.create', actor=audit.Actor.user(1, 'a'))
        raise RuntimeError
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 and rows(conn) == []


def test_record_without_a_caller_transaction_commits_by_itself(path, conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'))
    other = db.connect(path)
    try:
        assert other.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    finally:
        other.close()


def test_concurrent_writers_lose_nothing(path):
    errors = []

    def work(n):
        try:
            c = db.connect(path)
            try:
                for i in range(40):
                    audit.record(c, 'x.y', actor=audit.Actor.user(n + 1, f't{n}'), details={'i': i})
            finally:
                c.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    c = db.connect(path)
    try:
        assert not errors, errors
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 240
        assert c.execute("SELECT COUNT(DISTINCT id) FROM audit_log").fetchone()[0] == 240
    finally:
        c.close()


# --- Consultation : injection, bornes, pagination -----------------------------------------------

INJECTIONS = ["x' OR '1'='1", "'; DROP TABLE audit_log;--", '" OR ""="', "x%' OR 1=1 --", '\\', "%", "_", "x' UNION SELECT * FROM users--"]


def seed(conn, n=30):
    for i in range(n):
        audit.record(conn, ('secret.set', 'setting.update', 'user.create')[i % 3], actor=audit.Actor.user(i % 4 + 1, 'a'),
                     now=NOW + timedelta(minutes=i))


@pytest.mark.parametrize('payload', INJECTIONS)
def test_filters_are_parameterized_against_injection(conn, payload):
    """Aucune charge ne s'exécute : la table reste intacte, seules des lignes d'audit sortent, rien n'est écrit."""
    seed(conn, 6)
    changes = conn.total_changes
    for kwargs in ({'action': payload}, {'action': payload + '.'}, {'actor_id': payload}, {'since': payload},
                   {'until': payload}, {'before_id': payload}):
        try:
            result = audit.list_entries(conn, **kwargs)
        except (ValueError, TypeError):
            continue                                              # refus propre : accepté
        assert all(set(e) == {'id', 'at', 'actor_type', 'actor_id', 'actor_label', 'action', 'target_type', 'target_id', 'details', 'ip'}
                   for e in result), kwargs                       # jamais de colonnes d'une autre table (UNION)
        if 'action' in kwargs:
            assert result == [], kwargs
    assert conn.total_changes == changes and conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 6
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'audit_log'").fetchone()


def test_prefix_filter_escapes_underscore_percent_and_backslash(conn):
    for action in ('a_b.c', 'axb.c', 'a%b.c', 'a.b'):
        audit.record(conn, action, actor=audit.Actor.user(1, 'a')) if action != 'a%b.c' else None
    assert [e['action'] for e in audit.list_entries(conn, action='a_b.')] == ['a_b.c']
    assert audit.list_entries(conn, action='a%.') == [] and audit.list_entries(conn, action='a\\.') == []


def test_exact_filter_is_not_a_prefix(conn):
    audit.record(conn, 'secret.set', actor=audit.Actor.user(1, 'a'))
    audit.record(conn, 'secret.set_all', actor=audit.Actor.user(1, 'a'))
    assert [e['action'] for e in audit.list_entries(conn, action='secret.set')] == ['secret.set']


@pytest.mark.parametrize('limit,expected', [(1, 1), (5, 5), (200, 30), (201, 30), (10 ** 9, 30), (0, 1), (-7, 1)])
def test_limit_is_clamped_into_one_to_max(conn, limit, expected):
    seed(conn, 30)
    assert len(audit.list_entries(conn, limit=limit)) == expected


@pytest.mark.parametrize('limit', ['abc', None, float('nan'), [5], object()])
def test_unusable_limits_raise_instead_of_reaching_sql(conn, limit):
    seed(conn, 3)
    with pytest.raises((ValueError, TypeError, OverflowError)):
        audit.list_entries(conn, limit=limit)


def test_walking_every_page_returns_each_row_exactly_once_in_order(conn):
    seed(conn, 53)
    seen, cursor = [], None
    for _ in range(20):
        page = audit.list_entries(conn, limit=7, before_id=cursor)
        if not page:
            break
        seen += [e['id'] for e in page]
        cursor = page[-1]['id']
    assert seen == list(range(53, 0, -1))


def test_pagination_with_filters_and_a_row_inserted_meanwhile_stays_consistent(conn):
    seed(conn, 30)
    first = audit.list_entries(conn, action='secret.', limit=4)
    audit.record(conn, 'secret.set', actor=audit.Actor.user(1, 'a'))                       # nouvelle ligne pendant la consultation
    nxt = audit.list_entries(conn, action='secret.', limit=4, before_id=first[-1]['id'])
    assert not {e['id'] for e in first} & {e['id'] for e in nxt}
    assert all(e['id'] < first[-1]['id'] for e in nxt)


def test_combined_filters_are_anded(conn):
    seed(conn, 30)
    since = (NOW + timedelta(minutes=8)).strftime('%Y-%m-%dT%H:%M:%SZ')
    until = (NOW + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')
    result = audit.list_entries(conn, action='secret.', actor_id=2, since=since, until=until)
    assert len(result) == 1 and all(e['action'] == 'secret.set' and e['actor_id'] == 2 and since <= e['at'] < until for e in result)


def test_the_lowest_actor_id_is_a_real_filter_not_ignored(conn):
    seed(conn, 8)
    assert {e['actor_id'] for e in audit.list_entries(conn, actor_id=1)} == {1}


def test_listing_never_writes_or_opens_a_transaction(conn):
    seed(conn, 5)
    before = conn.total_changes
    audit.list_entries(conn)
    assert conn.total_changes == before and not conn.in_transaction


def test_listing_returns_decoded_details_and_none(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), details={'n': [1, 2]})
    audit.record(conn, 'x.z', actor=audit.Actor.user(1, 'a'))
    new, old = audit.list_entries(conn)
    assert new['details'] is None and old['details'] == {'n': [1, 2]}


def test_one_corrupt_row_does_not_make_the_whole_journal_unreadable(conn):
    seed(conn, 3)
    conn.execute("INSERT INTO audit_log (at, actor_type, actor_label, action, details) VALUES ('2026-01-01T00:00:00Z', 'system', '', 'x.y', 'pas du json')")
    conn.commit()
    assert len(audit.list_entries(conn)) == 4


# --- Purge ------------------------------------------------------------------------------------

def test_purge_with_nothing_to_delete_is_still_traced(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=NOW)
    assert audit.purge(conn, 90, now=NOW) == 0
    last = rows(conn)[-1]
    assert last['action'] == 'audit.purge' and json.loads(last['details'])['deleted'] == 0


def test_purge_far_in_the_future_keeps_only_its_own_trace(conn):
    seed(conn, 10)
    assert audit.purge(conn, 30, now=NOW + timedelta(days=5000)) == 10
    assert [r['action'] for r in rows(conn)] == ['audit.purge']


def test_purge_trace_is_not_purged_by_the_same_run_and_is_dated_with_the_injected_clock(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=NOW - timedelta(days=999))
    audit.purge(conn, 30, now=NOW)
    (trace,) = rows(conn)
    assert trace['action'] == 'audit.purge' and trace['at'] == '2026-11-24T12:00:00Z'


def test_two_consecutive_purges_leave_one_trace_each(conn):
    audit.purge(conn, 90, now=NOW)
    audit.purge(conn, 90, now=NOW + timedelta(days=1))
    assert [r['action'] for r in rows(conn)] == ['audit.purge', 'audit.purge']


@pytest.mark.parametrize('days,ok', [(30, True), (31, True), (3649, True), (3650, True), (29, False), (3651, False)])
def test_retention_bounds_are_inclusive(conn, days, ok):
    if ok:
        audit.purge(conn, days, now=NOW)
    else:
        with pytest.raises(ValueError):
            audit.purge(conn, days, now=NOW)


def test_purge_does_not_touch_the_table_when_the_retention_is_refused(conn):
    seed(conn, 5)
    for bad in (0, -1, 10 ** 12, 2 ** 70, float('inf'), 90.5, '90', None, [90]):
        with pytest.raises((ValueError, TypeError)):
            audit.purge(conn, bad, now=NOW + timedelta(days=9999))
    assert len(rows(conn)) == 5


def test_purge_joins_the_callers_transaction_and_is_undone_with_it(conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=NOW - timedelta(days=200))
    with pytest.raises(RuntimeError), db.write_transaction(conn):
        audit.purge(conn, 90, now=NOW)
        raise RuntimeError
    assert [r['action'] for r in rows(conn)] == ['x.y']


def test_purge_trace_failure_leaves_every_row_in_place(conn):
    for i in range(5):
        audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'), now=NOW - timedelta(days=200 + i))
    original = conn.execute

    class Boom:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *a):
            if sql.lstrip().startswith('INSERT INTO audit_log'):
                raise RuntimeError("disque plein")
            return original(sql, *a)

        @property
        def in_transaction(self):
            return conn.in_transaction

    with pytest.raises(RuntimeError):
        audit.purge(Boom(), 90, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 5 and not conn.in_transaction


def test_purge_deletes_in_one_pass_even_for_many_rows(conn):
    conn.executemany("INSERT INTO audit_log (at, actor_type, actor_label, action) VALUES (?, 'system', '', 'x.y')",
                     [('2020-01-01T00:00:00Z',)] * 20_000)
    conn.commit()
    assert audit.purge(conn, 90, now=NOW) == 20_000


# --- Migration ---------------------------------------------------------------------------------

def test_migration_replay_keeps_existing_rows_and_indexes(path, conn):
    audit.record(conn, 'x.y', actor=audit.Actor.user(1, 'a'))
    conn.execute("DELETE FROM schema_migrations WHERE name = '0006_audit_log'")
    conn.commit()
    db.init_db(path)
    assert len(rows(conn)) == 1
    indexes = {r['name'] for r in conn.execute("PRAGMA index_list(audit_log)")}
    assert {'idx_audit_log_at', 'idx_audit_log_action'} <= indexes


def test_audit_table_has_no_foreign_key_on_the_actor(conn):
    assert conn.execute("PRAGMA foreign_key_list(audit_log)").fetchall() == []


# --- Réglages et secrets (EF-9.2, m8) : fonctions d'audit.py ----------------------------------------

def test_a_secret_setting_change_writes_no_value_at_all(conn):
    audit.record_setting_change(conn, 'smtp.password', 'ANCIEN-S3CRET', 'NOUVEAU-S3CRET', secret=True, actor=audit.Actor.user(1, 'a'))
    row = rows(conn)[0]
    assert 'S3CRET' not in json.dumps(row) and json.loads(row['details']) == {'key': 'smtp.password', 'changed': True}


def test_a_plain_setting_change_records_old_and_new(conn):
    audit.record_setting_change(conn, 'USER_QUOTA_MB', 100, 200, secret=False, actor=audit.Actor.user(1, 'a'))
    assert json.loads(rows(conn)[0]['details']) == {'key': 'USER_QUOTA_MB', 'old': 100, 'new': 200}


@pytest.mark.parametrize('flag', [None, 0, 1, 'true', 'False', [], 'secret'])
def test_secret_flag_must_be_a_real_boolean_so_a_guess_cannot_leak(conn, flag):
    with pytest.raises(TypeError):
        audit.record_setting_change(conn, 'k', 'old-value', 'new-value', secret=flag, actor=audit.Actor.user(1, 'a'))
    assert rows(conn) == []


def test_secret_events_are_whitelisted_and_carry_no_value(conn):
    for event in sorted(audit.SECRET_EVENTS):
        audit.record_secret_event(conn, 'smtp.password', event, actor=audit.Actor.user(1, 'a'), reason='destination')
    assert {r['action'] for r in rows(conn)} == {f'secret.{e}' for e in audit.SECRET_EVENTS}
    for bad in ('reveal', 'update.', '../x', '', 'UPDATE'):
        with pytest.raises(ValueError):
            audit.record_secret_event(conn, 'smtp.password', bad, actor=audit.Actor.user(1, 'a'))
    with pytest.raises(TypeError):
        audit.record_secret_event(conn, 'smtp.password', 'update', actor=audit.Actor.user(1, 'a'), value='S3CRET')   # pas de paramètre « valeur »
