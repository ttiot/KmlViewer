"""
Profil d'un utilisateur : mesures facultatives et type d'activité par défaut à l'import.

Le nom affiché et l'email n'en font pas partie : ils viennent du fournisseur d'identité à
chaque connexion, et l'email désigne un dossier de l'inbox. Les laisser modifier ici
permettrait de se faire attribuer les fichiers d'un autre compte.
"""

import sqlite3
from datetime import date
from typing import Any, Callable, Dict, Mapping, Optional

from app.services import activity_types

SEXES = ('female', 'male')
MIN_BIRTH_YEAR = 1900

# Bornes larges : elles écartent une faute de frappe, pas une personne
_INT_RANGES = {'height_cm': (50, 260), 'hr_max': (60, 250), 'hr_rest': (20, 150)}
_WEIGHT_RANGE = (20.0, 400.0)

FIELDS = ('birth_date', 'sex', 'height_cm', 'weight_kg', 'hr_max', 'hr_rest', 'default_activity_type')


class ProfileError(ValueError):
    """Valeur de profil refusée ; le message est destiné à l'utilisateur."""


def _birth_date(value: Any, today: date) -> str:
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        parsed = None
    if parsed is None or not date(MIN_BIRTH_YEAR, 1, 1) <= parsed <= today:
        raise ProfileError('Date de naissance invalide')
    return parsed.isoformat()


def _choice(allowed, label: str) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ProfileError(f'{label} invalide')
        return value
    return check


def _integer(field: str, label: str) -> Callable[[Any], int]:
    low, high = _INT_RANGES[field]

    def check(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ProfileError(f'{label} : entier attendu entre {low} et {high}')
        return value
    return check


def _weight(value: Any) -> float:
    low, high = _WEIGHT_RANGE
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
        raise ProfileError(f'Poids : nombre attendu entre {low:g} et {high:g}')
    return round(float(value), 1)


def validate(data: Mapping[str, Any], current: Mapping[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """
    Champs à écrire, validés. Liste blanche : un champ inconnu est refusé, pas ignoré.
    null vide un champ. `current` : profil enregistré, pour contrôler la cohérence des
    fréquences cardiaques quand une seule des deux est modifiée.
    """
    today = today or date.today()
    checks: Dict[str, Callable[[Any], Any]] = {
        'birth_date': lambda value: _birth_date(value, today),
        'sex': _choice(SEXES, 'Sexe'),
        'height_cm': _integer('height_cm', 'Taille'),
        'weight_kg': _weight,
        'hr_max': _integer('hr_max', 'FC max'),
        'hr_rest': _integer('hr_rest', 'FC au repos'),
        'default_activity_type': _choice(activity_types.KEYS, "Type d'activité"),
    }
    unknown = set(data) - set(checks)
    if unknown:
        raise ProfileError('Champ non modifiable : ' + ', '.join(sorted(map(str, unknown))))
    if not data:
        raise ProfileError('Rien à modifier')
    updates = {field: None if value is None else checks[field](value) for field, value in data.items()}

    hr_max = updates['hr_max'] if 'hr_max' in updates else current.get('hr_max')
    hr_rest = updates['hr_rest'] if 'hr_rest' in updates else current.get('hr_rest')
    if hr_max is not None and hr_rest is not None and hr_rest >= hr_max:
        raise ProfileError('La FC au repos doit être inférieure à la FC max')
    return updates


def to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {field: row[field] for field in FIELDS}


def get(db: sqlite3.Connection, user_id: int) -> Dict[str, Any]:
    row = db.execute(f"SELECT {', '.join(FIELDS)} FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise LookupError('Compte introuvable')
    return to_dict(row)


def update(db: sqlite3.Connection, user_id: int, data: Mapping[str, Any]) -> Dict[str, Any]:
    """Valide puis enregistre ; retourne le profil complet. Lève ProfileError si une valeur est refusée."""
    updates = validate(data, get(db, user_id))
    # Les noms de colonnes viennent de la liste blanche de validate, jamais de la requête
    assignments = ', '.join(f"{field} = ?" for field in updates)
    db.execute(f"UPDATE users SET {assignments} WHERE id = ?", (*updates.values(), user_id))
    db.commit()
    return get(db, user_id)


def default_activity_type(db: sqlite3.Connection, user_id: int) -> Optional[str]:
    """Type donné à l'import aux fichiers qui n'en portent pas ; None : ils restent non classés."""
    row = db.execute("SELECT default_activity_type FROM users WHERE id = ?", (user_id,)).fetchone()
    value = row['default_activity_type'] if row else None
    return value if value in activity_types.KEYS else None
