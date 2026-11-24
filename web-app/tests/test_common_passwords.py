"""Liste de mots de passe courants embarquée (T6b, EF-1.4b, CA-8b, ADR-0002)."""

import hashlib
import importlib.util
import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from app.auth import passwords

ROOT = Path(__file__).resolve().parent.parent
WORDLISTS = ROOT / 'app' / 'auth' / 'wordlists'
LIST = WORDLISTS / 'common_passwords.txt'
SCRIPT = ROOT / 'scripts' / 'build_common_passwords.py'
EXPECTED_ENTRIES, EXPECTED_BYTES = 46_187, 675_446
EXPECTED_SHA256 = 'ec1dce6813aac5688c42bdb30f181f790ae56b3e974dd769732753fb0e1fecaa'
SOURCE_SHA256 = '424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51'


def load_script():
    spec = importlib.util.spec_from_file_location('build_common_passwords', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Le fichier livré -----------------------------------------------------------------------

def test_delivered_file_has_the_expected_size_and_hash():
    data = LIST.read_bytes()
    assert hashlib.sha256(data).hexdigest() == EXPECTED_SHA256 and len(data) == EXPECTED_BYTES
    assert data.count(b'\n') == EXPECTED_ENTRIES


def test_every_line_is_normalized_in_bounds_unique_sorted_and_non_empty():
    lines = LIST.read_text(encoding='utf-8').split('\n')
    assert lines.pop() == ''                    # fin de ligne finale, pas de ligne vide ailleurs
    assert len(lines) == EXPECTED_ENTRIES
    assert all(lines), "ligne vide"
    assert all(unicodedata.normalize('NFKC', e).casefold() == e for e in lines)
    assert all(12 <= len(e) <= 128 for e in lines)
    assert len(set(lines)) == len(lines) and lines == sorted(lines)
    assert b'\r' not in LIST.read_bytes()


def test_source_file_carries_provenance_and_the_full_mit_notice():
    text = (WORDLISTS / 'common_passwords.SOURCE').read_text(encoding='utf-8')
    for needle in ('c205c36a445bff37f8e58a9ec829105cd4975c58', 'e5e73232c8f4f53edfe9c0fd699f415da8a336e3', SOURCE_SHA256,
                   EXPECTED_SHA256, 'Mark Burnett', '2015', 'Copyright (c) 2018 Daniel Miessler', 'MIT License',
                   'THE SOFTWARE IS PROVIDED "AS IS"', 'SOFTWARE.', '46146', '46187'):
        assert needle in text, needle
    assert text == text.replace('\r', '')
    assert load_script().MIT_NOTICE in text


def test_local_list_is_merged_and_the_old_python_module_is_gone():
    local = (WORDLISTS / 'common_passwords_local.txt').read_text(encoding='utf-8').split('\n')[:-1]
    assert len(local) == 71 and 'motdepasse1234' in local
    merged = set(LIST.read_text(encoding='utf-8').split('\n'))
    assert set(local) <= merged
    assert not (ROOT / 'app' / 'auth' / 'common_passwords.py').exists()


# --- Politique (CA-8b) ----------------------------------------------------------------------

@pytest.mark.parametrize('candidate', ['qwerty123456', 'QWERTY123456', 'ｑｗｅｒｔｙ１２３４５６', 'motdepasse1234',
                                       'MotDePasse1234'])
def test_listed_passwords_are_refused_whatever_the_case_or_width(candidate):
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy(candidate)
    assert exc.value.code == 'too_common'


def test_a_listed_entry_is_found_and_an_unlisted_one_is_not():
    entries = passwords.common_passwords()
    assert len(entries) == EXPECTED_ENTRIES and isinstance(entries, frozenset)
    assert passwords.is_common('qwerty123456') and not passwords.is_common('cheval-agrafe-lune-42')


def test_loading_is_fast():
    """Mesuré au démarrage < 500 ms dans l'image (spec) ; ici, sur le poste de test."""
    started = time.perf_counter()
    entries = passwords.load_common_passwords()
    assert len(entries) == EXPECTED_ENTRIES and time.perf_counter() - started < 0.5


def test_list_is_loaded_once_per_process_and_not_at_import(monkeypatch):
    monkeypatch.setattr(passwords, '_common', None)
    calls = []
    real = passwords.load_common_passwords
    monkeypatch.setattr(passwords, 'load_common_passwords', lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    passwords.common_passwords()
    passwords.common_passwords()
    assert calls == [1]


# --- Refus de démarrer / jamais d'affaiblissement silencieux ---------------------------------

@pytest.mark.parametrize('content', [None, b'', b'\n\n', b'\xff\xfe\x00bad utf8'])
def test_missing_empty_or_unreadable_list_is_an_error(tmp_path, content):
    path = tmp_path / 'list.txt'
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(passwords.CommonPasswordsError):
        passwords.load_common_passwords(path)


def test_policy_fails_closed_when_the_list_cannot_be_loaded(monkeypatch, tmp_path):
    monkeypatch.setattr(passwords, '_common', None)
    monkeypatch.setattr(passwords, 'WORDLIST', tmp_path / 'absente.txt')
    with pytest.raises(passwords.CommonPasswordsError):
        passwords.check_policy('cheval-agrafe-lune-42')       # jamais « tout est accepté »
    with pytest.raises(passwords.CommonPasswordsError):
        passwords.check_policy('password1234')                # et l'échec n'est pas mis en cache comme succès


def test_a_line_with_a_unicode_line_separator_is_one_entry(tmp_path):
    path = tmp_path / 'l.txt'
    path.write_text('abc defghijkl\nzzzzzzzzzzzz\n', encoding='utf-8')
    assert passwords.load_common_passwords(path) == {'abc defghijkl', 'zzzzzzzzzzzz'}


def test_the_application_never_imports_a_network_client_for_the_list():
    source = Path(passwords.__file__).read_text(encoding='utf-8')
    assert 'urllib' not in source and 'requests' not in source and 'http' not in source.lower().replace('https://', '')


# --- Script de génération -------------------------------------------------------------------

def test_build_is_pure_normalizing_filtering_merging_sorting_deduplicating():
    script = load_script()
    source = (b"short\n" + 'ＡＢＣＤＥＦＧＨＩＪＫＬ\n'.encode() + b"abcdefghijkl\r\nABCDEFGHIJKL\n" + b"  spaces here ok  \n"
              + b"\xff\xfe-invalid-utf8-line\n" + b"x" * 129 + b"\n" + b"\n" + b"lastline-without-newline")
    output, source_text, stats = script.build(source, 'LOCAL-ENTRY-12\nshort\nabcdefghijkl\n')
    lines = output.decode().split('\n')[:-1]
    assert lines == sorted(set(lines))
    assert lines == ['  spaces here ok  ', 'abcdefghijkl', 'lastline-without-newline', 'local-entry-12']
    assert stats['invalid'] == 1 and stats['public'] == 3 and stats['local'] == 2 and stats['common'] == 1
    assert stats['total'] == 4 and stats['lines'] == 9
    assert str(stats['total']) in source_text and stats['output_sha256'] in source_text


def test_build_output_has_no_variable_data():
    script = load_script()
    a = script.build(b'abcdefghijkl\nmnopqrstuvwx\n', 'zzzzzzzzzzzz\n')
    b = script.build(b'abcdefghijkl\nmnopqrstuvwx\n', 'zzzzzzzzzzzz\n')
    assert a[0] == b[0] and a[1] == b[1]


def test_build_reports_entries_added_and_removed_against_the_existing_file():
    script = load_script()
    _, _, stats = script.build(b'abcdefghijkl\nmnopqrstuvwx\n', '', previous=b'abcdefghijkl\nold-entry-here\n')
    assert (stats['added'], stats['removed']) == (1, 1)


def test_a_source_with_another_sha256_stops_the_script_and_writes_nothing(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'common_passwords_local.txt').write_text('motdepasse1234\n')
    bogus = tmp_path / 'bogus.txt'
    bogus.write_text('abcdefghijkl\n')
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), str(bogus), '--out-dir', str(out)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and 'SHA-256' in result.stderr
    assert sorted(p.name for p in out.iterdir()) == ['common_passwords_local.txt']


def test_script_uses_only_the_standard_library_and_pins_the_source():
    text = SCRIPT.read_text(encoding='utf-8')
    for pinned in (SOURCE_SHA256, 'c205c36a445bff37f8e58a9ec829105cd4975c58',
                   'https://raw.githubusercontent.com/danielmiessler/SecLists/'):
        assert pinned in text
    imports = {line.split()[1].split('.')[0] for line in text.split('\n') if line.startswith(('import ', 'from '))}
    assert imports <= set(sys.stdlib_module_names)


@pytest.mark.skipif(not os.environ.get('COMMON_PASSWORDS_SOURCE'),
                    reason="copie de la source (SHA-256 vérifié) non fournie : COMMON_PASSWORDS_SOURCE=<fichier>")
def test_regenerating_from_the_source_copy_gives_identical_bytes(tmp_path):
    for name in ('common_passwords_local.txt',):
        (tmp_path / name).write_bytes((WORDLISTS / name).read_bytes())
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), os.environ['COMMON_PASSWORDS_SOURCE'], '--out-dir',
                             str(tmp_path)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'common_passwords.txt').read_bytes() == LIST.read_bytes()
    assert (tmp_path / 'common_passwords.SOURCE').read_bytes() == (WORDLISTS / 'common_passwords.SOURCE').read_bytes()
