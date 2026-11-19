"""
Types d'activité : liste fermée, seule source des clés stockées en base et des libellés
affichés. Module pur (ni Flask ni base) : l'ingestion, les routes et les gabarits le lisent.

Une activité sans type est « non classée » (colonne à NULL). Le type lu dans un fichier
(<type> d'un GPX) est ramené à cette liste ; une valeur inconnue devient « Autre ».
"""

import re
import unicodedata
from typing import Any, List, Optional, Tuple

# (clé stockée, libellé affiché), dans l'ordre des listes déroulantes
CATALOG: Tuple[Tuple[str, str], ...] = (
    ('run', 'Course à pied'),
    ('trail', 'Trail'),
    ('hike', 'Randonnée'),
    ('walk', 'Marche'),
    ('road_bike', 'Vélo de route'),
    ('mtb', 'VTT'),
    ('ski', 'Ski'),
    ('moto', 'Moto'),
    ('car', 'Voiture'),
    ('other', 'Autre'),
)
KEYS = frozenset(key for key, _ in CATALOG)
LABELS = dict(CATALOG)

# Valeur de filtre désignant les activités sans type (jamais stockée)
UNCLASSIFIED = 'unclassified'
UNCLASSIFIED_LABEL = 'Non classé'

# Types pour lesquels un meilleur temps sur 1 km, 5 km… a un sens
RUNNING = frozenset({'run', 'trail'})

# Écritures rencontrées dans les fichiers (Garmin, Strava, Komoot…), une fois normalisées.
# « 9 » et « 1 » : types numériques des exports GPX de Strava (course, vélo).
_SYNONYMS = {
    'run': ('run', 'running', 'course', 'course_a_pied', 'jogging', 'footing', 'street_running', '9'),
    'trail': ('trail', 'trail_running', 'trail_run', 'trailrunning'),
    'hike': ('hike', 'hiking', 'randonnee', 'rando', 'trekking', 'mountaineering'),
    'walk': ('walk', 'walking', 'marche'),
    'road_bike': ('road_bike', 'cycling', 'biking', 'ride', 'bike', 'road_biking', 'road_cycling', 'velo',
                  'velo_de_route', 'gravel', 'gravel_cycling', 'e_bike', '1'),
    'mtb': ('mtb', 'mountain_biking', 'mountain_bike', 'mountainbiking', 'vtt', 'e_mtb'),
    'ski': ('ski', 'skiing', 'alpine_skiing', 'backcountry_skiing', 'nordic_skiing', 'cross_country_skiing',
            'resort_skiing', 'ski_de_fond', 'ski_de_rando', 'snowboarding'),
    'moto': ('moto', 'motorcycling', 'motorcycle', 'motorbike'),
    'car': ('car', 'driving', 'voiture', 'automobile'),
    'other': ('other', 'autre'),
}
_BY_SYNONYM = {synonym: key for key, synonyms in _SYNONYMS.items() for synonym in synonyms}


def from_raw(raw: Any) -> Optional[str]:
    """Clé du catalogue pour un type lu dans un fichier ; None s'il est absent."""
    text = unicodedata.normalize('NFKD', str(raw or '')).encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')
    if not text:
        return None
    return _BY_SYNONYM.get(text, 'other')


def is_filter(value: Any) -> bool:
    """Vrai si `value` est un filtre de type acceptable : une clé du catalogue ou « non classé »."""
    return value in KEYS or value == UNCLASSIFIED


def sql_filter(activity_type: Optional[str]) -> Tuple[str, List[str]]:
    """Fragment SQL (à accoler après un WHERE existant) et ses paramètres ; vide sans filtre."""
    if not activity_type:
        return '', []
    if activity_type == UNCLASSIFIED:
        return " AND (activity_type IS NULL OR activity_type = '')", []
    return " AND activity_type = ?", [activity_type]
