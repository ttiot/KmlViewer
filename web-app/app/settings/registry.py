"""
Registre des réglages administrables (admin-space EF-6.1, ADR-0008). Module PUR : ni base, ni env.

Chaque entrée déclare des BORNES OBLIGATOIRES, basse et haute (M7) : une valeur énorme désactive
une protection aussi sûrement qu'une valeur nulle. Une entrée sans bornes ne se construit pas.

Frontière (EF-6.6) : les réglages d'amorçage et de sécurité (SECRET_KEY, SECRETS_KEY, AUTH_MODE,
OIDC_*, PUBLIC_URL, WEBAUTHN_RP_ID, OUTBOUND_ALLOWED_HOSTS, TRUSTED_HOSTS, TRUST_PROXY*,
SESSION_*, DATA_DIR, DB_PATH, LOG_LEVEL) NE SONT PAS ici et ne doivent pas l'être. Tout ajout
qui touche l'authentification, les sessions, les limites dures ou une destination sortante
demande une revue sécurité (les seuils du limiteur y sont, bornés des deux côtés, EF-6.5).
"""

import math
import re
from dataclasses import dataclass
from typing import Optional

TYPES = ('int', 'float', 'bool', 'str', 'enum', 'list')
_BOOL_TRUE, _BOOL_FALSE = {'1', 'true', 'yes', 'on'}, {'0', 'false', 'no', 'off'}
_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,63}')


@dataclass(frozen=True)
class Setting:
    key: str
    type: str
    default: object
    section: str
    label: str
    help: str = ''
    env: Optional[str] = None          # variable d'environnement qui sert de valeur initiale et de repli
    min: Optional[float] = None        # int / float : obligatoires
    max: Optional[float] = None
    max_length: Optional[int] = None   # str : longueur ; list : longueur de chaque élément (obligatoire)
    choices: Optional[tuple] = None    # enum : obligatoire
    max_items: Optional[int] = None    # list : obligatoire
    requires_restart: bool = False
    secret: bool = False               # valeur masquée dans l'audit et le diagnostic

    def __post_init__(self):
        if not _KEY.fullmatch(self.key) or self.type not in TYPES:
            raise ValueError(f"Réglage mal déclaré : {self.key!r}")
        missing = {
            'int': self.min is None or self.max is None, 'float': self.min is None or self.max is None,
            'str': not self.max_length, 'enum': not self.choices,
            'list': not self.max_items or not self.max_length, 'bool': False,
        }[self.type]
        if missing:
            raise ValueError(f"Réglage {self.key!r} : bornes obligatoires manquantes")
        if self.min is not None and self.min > self.max:
            raise ValueError(f"Réglage {self.key!r} : min > max")
        self.validate(self.default)         # un défaut hors bornes est une erreur de code

    def validate(self, value):
        """Valeur normalisée (stricte : types JSON exacts) ou ValueError(message affichable)."""
        kind = self.type
        if kind == 'int':
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("Entier attendu")
            return self._bounded(value)
        if kind == 'float':
            try:
                finite = (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value))
            except OverflowError:               # entier démesuré (un JSON peut en porter)
                finite = False
            if not finite:
                raise ValueError("Nombre attendu")
            return self._bounded(float(value))
        if kind == 'bool':
            if not isinstance(value, bool):
                raise ValueError("Booléen attendu")
            return value
        if kind == 'str':
            return self._string(value, self.max_length)
        if kind == 'enum':
            if value not in self.choices:
                raise ValueError("Valeur attendue parmi : " + ', '.join(map(str, self.choices)))
            return value
        if not isinstance(value, (list, tuple)) or len(value) > self.max_items:
            raise ValueError(f"Liste de {self.max_items} éléments au plus attendue")
        return [self._string(v, self.max_length) for v in value]

    def parse_env(self, raw):
        """Valeur d'environnement (texte, ou valeur déjà typée venant de la configuration) validée, ou ValueError."""
        if not isinstance(raw, str):
            return self.validate(raw)
        text = raw.strip()
        if self.type == 'int':
            if not re.fullmatch(r'[+-]?\d{1,18}', text):
                raise ValueError("Entier attendu")
            return self.validate(int(text))
        if self.type == 'float':
            try:
                return self.validate(float(text))
            except ValueError:
                raise ValueError("Nombre attendu") from None
        if self.type == 'bool':
            if text.lower() in _BOOL_TRUE:
                return True
            if text.lower() in _BOOL_FALSE:
                return False
            raise ValueError("Booléen attendu")
        if self.type == 'list':
            return self.validate([item.strip() for item in text.split(',') if item.strip()])
        return self.validate(text)

    def _bounded(self, number):
        if not self.min <= number <= self.max:
            raise ValueError(f"Valeur entre {self.min:g} et {self.max:g} attendue")
        return number

    @staticmethod
    def _string(value, max_length):
        if not isinstance(value, str):
            raise ValueError("Texte attendu")
        if len(value) > max_length:
            raise ValueError(f"{max_length} caractères au plus")
        if not all(c.isprintable() for c in value):
            raise ValueError("Caractères de contrôle interdits")
        return value


def _int(key, default, low, high, section, label, **kw):
    return Setting(key, 'int', default, section, label, min=low, max=high, **kw)


_ENTRIES = (
    _int('USER_QUOTA_MB', 2048, 0, 102_400, 'Stockage', "Quota de stockage par utilisateur (Mo)", env='USER_QUOTA_MB',
         help="0 = illimité (conservé pour compatibilité)."),
    _int('audit.retention_days', 90, 30, 3650, 'Sécurité', "Rétention du journal d'audit (jours)"),
    # Seuils du limiteur (ADR-0006, EF-6.5) : lus à chaud, sans remise à zéro des compteurs
    _int('ratelimit.password_per_identifier', 5, 3, 20, 'Sécurité', "Tentatives de mot de passe / 15 min / identifiant"),
    _int('ratelimit.password_per_ip', 20, 10, 100, 'Sécurité', "Tentatives de mot de passe / 15 min / IP"),
    _int('ratelimit.mfa_per_account', 5, 3, 10, 'Sécurité', "Essais de second facteur / 15 min / compte"),
    _int('ratelimit.forgot_per_identifier', 3, 1, 10, 'Sécurité', "Mots de passe oubliés / h / identifiant"),
    _int('ratelimit.forgot_per_ip', 10, 10, 100, 'Sécurité', "Mots de passe oubliés / h / IP"),
    _int('ratelimit.token_per_ip', 10, 5, 30, 'Sécurité', "Essais de jeton / 15 min / IP"),
    _int('routing.rate_limit_per_user', 30, 1, 100, 'Traces planifiées', "Générations / h / utilisateur"),
)

REGISTRY = {s.key: s for s in _ENTRIES}
assert len(REGISTRY) == len(_ENTRIES), "clé de réglage en double"
