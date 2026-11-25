"""
Public Suffix List embarquée (admin-space EF-2.4b, E-13) : décide si un nom est un SUFFIXE PUBLIC (`fr`, `co.uk`, `github.io`),
pour refuser un `WEBAUTHN_RP_ID` qui couvrirait des sites qui ne sont pas les siens. Aucun accès réseau, jamais : la liste est un
fichier du dépôt (`publicsuffix_data/public_suffix_list.dat`, figé par `scripts/build_public_suffix_list.py`,
SHA-256 dans `SOURCE`).

Algorithme standard de la liste : règles normales, jokers (`*.ck`), exceptions (`!www.ck`), règle implicite `*` ; comparaison sur
la forme ASCII (punycode) en minuscules. Sections ICANN et PRIVATE prises ensemble. Module sans Flask.
"""

import threading
from pathlib import Path
from typing import NamedTuple, Optional

LIST_PATH = Path(__file__).resolve().parent / 'publicsuffix_data' / 'public_suffix_list.dat'
MAX_LIST_BYTES = 4 * 1024 * 1024
MIN_RULES = 1000          # une liste de moins de mille règles n'est pas la vraie : refus (fermé par défaut)


class PublicSuffixError(Exception):
    """Liste absente, illisible, vide ou tronquée : le démarrage est refusé (jamais de validation silencieusement affaiblie)."""


class SuffixList(NamedTuple):
    normal: frozenset
    wildcard: frozenset      # parents des règles `*.X` (X)
    exception: frozenset     # règles `!X` (X)


def to_ascii(name: str) -> Optional[str]:
    """Forme ASCII (punycode) en minuscules ; None si le nom n'est pas convertible."""
    name = (name or '').strip().lower()
    if not name or name.startswith('.') or name.endswith('.') or '..' in name:
        return None
    try:
        return name.encode('idna').decode('ascii')
    except UnicodeError:
        return None


def parse(text: str) -> SuffixList:
    normal, wildcard, exception = set(), set(), set()
    for line in text.splitlines():
        rule = line.split()[0] if line.split() else ''
        if not rule or rule.startswith('//'):
            continue
        rule = rule.lower()
        target = normal
        if rule.startswith('!'):
            target, rule = exception, rule[1:]
        elif rule.startswith('*.'):
            target, rule = wildcard, rule[2:]
        ascii_rule = to_ascii(rule)
        if ascii_rule is None:
            continue
        target.add(ascii_rule)
    return SuffixList(frozenset(normal), frozenset(wildcard), frozenset(exception))


def load(path=None) -> SuffixList:
    path = Path(path) if path is not None else LIST_PATH
    try:
        if path.stat().st_size > MAX_LIST_BYTES:
            raise PublicSuffixError("Liste des suffixes publics anormalement grosse")
        text = path.read_text(encoding='utf-8')
    except OSError:
        raise PublicSuffixError("Liste des suffixes publics absente ou illisible") from None
    except UnicodeDecodeError:
        raise PublicSuffixError("Liste des suffixes publics illisible (UTF-8 attendu)") from None
    parsed = parse(text)
    if len(parsed.normal) + len(parsed.wildcard) < MIN_RULES:
        raise PublicSuffixError("Liste des suffixes publics vide ou tronquée")
    return parsed


def public_suffix(domain: str, rules: SuffixList) -> Optional[str]:
    """Suffixe public de `domain` (forme ASCII) selon la liste ; None si le nom est inutilisable."""
    ascii_name = to_ascii(domain)
    if ascii_name is None:
        return None
    labels = ascii_name.split('.')
    count = len(labels)
    best = 1                                                    # règle implicite `*` : un label
    for i in range(count):
        candidate = '.'.join(labels[i:])
        if candidate in rules.exception:
            return '.'.join(labels[i + 1:]) or None             # exception : le suffixe est la règle privée de son premier label
        if candidate in rules.normal:
            best = max(best, count - i)
        if i + 1 < count and '.'.join(labels[i + 1:]) in rules.wildcard:
            best = max(best, count - i)
    return '.'.join(labels[count - best:])


def is_public_suffix(domain: str, rules: SuffixList) -> bool:
    ascii_name = to_ascii(domain)
    return ascii_name is not None and public_suffix(ascii_name, rules) == ascii_name


_lock = threading.Lock()
_rules: Optional[SuffixList] = None


def rules() -> SuffixList:
    """Liste chargée une fois par processus ; PublicSuffixError si elle est absente ou invalide."""
    global _rules
    if _rules is None:
        with _lock:
            if _rules is None:
                _rules = load()
    return _rules


def init_public_suffix() -> int:
    """Chargement au démarrage (comptes à mot de passe disponibles) ; retourne le nombre de règles."""
    loaded = rules()
    return len(loaded.normal) + len(loaded.wildcard) + len(loaded.exception)
