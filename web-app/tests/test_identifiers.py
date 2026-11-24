"""Identifiants de compte : format et identifiants réservés (EF-1.2, EF-1.2b, CA-24b)."""

import pytest

from app.auth import identifiers
from app.auth.identifiers import IdentifierError, check_identifier, is_reserved


@pytest.mark.parametrize('name', ['cli', 'CLI', 'System', 'LOCAL', 'admin', 'Root', ' root ', 'ｒｏｏｔ', 'ＣＬＩ'])
def test_reserved_identifiers_are_refused_whatever_the_case_or_width(name):
    """EF-1.2b : insensible à la casse, après normalisation (NFKC)."""
    assert is_reserved(name)
    with pytest.raises(IdentifierError) as exc:
        check_identifier(name)
    assert exc.value.unavailable and exc.value.message == identifiers.UNAVAILABLE_MESSAGE


def test_the_reserved_list_is_the_one_in_the_spec():
    assert identifiers.RESERVED_IDENTIFIERS == {'cli', 'system', 'local', 'admin', 'root'}


@pytest.mark.parametrize('name', ['alice', 'a.b-c_d', 'abc', 'x' * 64, 'cli2', 'my-cli', 'systems', 'administrator', 'Julien.M'])
def test_ordinary_identifiers_are_accepted_and_normalized(name):
    assert check_identifier(name) == name.strip().casefold()


@pytest.mark.parametrize('name', ['', 'ab', 'x' * 65, 'a@b', 'a b', 'a/b', 'é-é-é', None, 5, b'abc', 'a\nb', 'x' * 10_000])
def test_bad_format_is_refused_with_the_format_message_not_the_unavailable_one(name):
    with pytest.raises(IdentifierError) as exc:
        check_identifier(name)
    assert exc.value.message == identifiers.FORMAT_MESSAGE and not exc.value.unavailable


def test_reserved_and_taken_share_the_same_message():
    """Pas d'oracle : un identifiant réservé répond comme un identifiant déjà pris (« indisponible »)."""
    assert identifiers.UNAVAILABLE_MESSAGE == "Identifiant indisponible"
