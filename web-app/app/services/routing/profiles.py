"""
Activité proposée => profil du moteur => type stocké (gpx-route-builder RB-3.1). Module PUR : liste fermée côté serveur.

Gravel = profil de routage seul : le type stocké reste `road_bike` (`activity_types` inchangé). Le nom de profil est validé
par `base.validate_profile` avant tout appel au moteur ; ici, la liste blanche des activités acceptées.
"""

from typing import NamedTuple, Optional

from app.services import activity_types


class ActivityProfile(NamedTuple):
    key: str            # clé proposée (formulaire, API)
    label: str
    profile: str        # profil du moteur
    stored_type: str    # clé de `activity_types` enregistrée avec la trace


_ACTIVITIES = (
    ActivityProfile('run', 'Course à pied', 'foot', 'run'),
    ActivityProfile('trail', 'Trail', 'hike', 'trail'),
    ActivityProfile('hike', 'Randonnée', 'hike', 'hike'),
    ActivityProfile('road_bike', 'Vélo de route', 'racingbike', 'road_bike'),
    ActivityProfile('gravel', 'Gravel', 'gravel', 'road_bike'),
    ActivityProfile('mtb', 'VTT', 'mtb', 'mtb'),
)
ACTIVITIES = {a.key: a for a in _ACTIVITIES}
DEFAULT_ACTIVITY = 'run'

assert all(a.stored_type in activity_types.KEYS for a in _ACTIVITIES)


def get(key) -> Optional[ActivityProfile]:
    """L'entrée désignée par `key` (liste blanche stricte : chaîne exacte), ou None."""
    return ACTIVITIES.get(key) if isinstance(key, str) else None


def default_for(activity_type) -> str:
    """Activité proposée par défaut selon `default_activity_type` du profil de l'utilisateur ; sinon course à pied."""
    for item in _ACTIVITIES:
        if item.stored_type == activity_type and item.key != 'gravel':
            return item.key
    return DEFAULT_ACTIVITY


def options() -> list:
    """Liste proposée au front : `[{key, label}]`, dans l'ordre du tableau RB-3.1."""
    return [{'key': a.key, 'label': a.label} for a in _ACTIVITIES]
