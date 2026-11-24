"""
Interface commune des moteurs de routage (gpx-route-builder §4, ADR-0007).

Module PUR : ni Flask, ni base, ni réseau, ni lecture de l'environnement. Il fixe :
  - les types échangés (Point, RouteResult, Health) ;
  - les erreurs, dont le message est FIXE : aucune ne transporte l'URL du moteur, sa réponse
    ni des coordonnées (RB-S8, RB-S9, CA-R15) ; un adaptateur journalise son détail à part ;
  - la validation d'entrée que tout adaptateur applique avant le moindre appel sortant
    (RB-S1) : coordonnées finies et bornées, profil, distance, graine.

Les bornes métier (nombre de points, distance maximale, emprise) restent au builder : ici
seulement ce qui garantit qu'un adaptateur ne reçoit jamais une valeur absurde ou hostile.
"""

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import NamedTuple, Optional, Sequence

from app.services.file_formats import MAX_FILE_POINTS

MAX_WAYPOINTS = 200               # plafond dur d'un appel (le réglage routing.max_waypoints est ≤ 100)
MAX_DISTANCE_M = 1_000_000.0      # 1000 km, plafond de routing.max_distance_km
MAX_SEED = 2 ** 31 - 1
MAX_PROFILE_LENGTH = 32
MAX_WARNINGS = 10
MAX_WARNING_LENGTH = 200
MAX_HEALTH_PROFILES = 20
_PROFILE_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789_')


class Point(NamedTuple):
    """Coordonnée WGS84 en degrés. Une simple paire (lat, lon) est acceptée partout."""

    lat: float
    lon: float


class RoutingError(Exception):
    """Racine des erreurs de routage. Le message est fixe et sans détail interne."""

    message = "Erreur de routage"

    def __init__(self):
        super().__init__(self.message)


class RoutingUnavailable(RoutingError):
    """Moteur injoignable, en erreur répétée, ou expiré (HTTP 503)."""

    message = "Service de routage indisponible"


class RoutingTimeout(RoutingUnavailable):
    """
    Échéance (budget total) ou délai d'un appel atteint. Règle pour le builder : RoutingTimeout
    sans trace exploitable ⇒ 504 ; toute autre RoutingUnavailable ⇒ 503 (CA-R15).
    """

    message = "Délai du service de routage dépassé"


class InvalidResponse(RoutingError):
    """Réponse du moteur illisible, hors bornes ou incohérente (HTTP 502, RB-S4)."""

    message = "Réponse du service de routage invalide"


class InvalidRequest(RoutingError):
    """Entrée refusée avant tout appel au moteur (HTTP 400)."""

    message = "Requête de routage invalide"


class NoRouteFound(RoutingError):
    """
    Aucun itinéraire : point non approchable (hors réseau, hors données) ou boucle impossible
    (HTTP 422). `waypoint_index` est la position (0-based) dans la liste transmise au moteur,
    None si aucun point précis n'est en cause ; le builder la ramène au numéro d'origine (m12).
    """

    message = "Aucun itinéraire trouvé"

    def __init__(self, waypoint_index: Optional[int] = None):
        super().__init__()
        if waypoint_index is not None and (isinstance(waypoint_index, bool) or not isinstance(waypoint_index, int)
                                           or waypoint_index < 0):
            raise ValueError("waypoint_index : entier >= 0 ou None")
        self.waypoint_index = waypoint_index


@dataclass(frozen=True)
class RouteResult:
    """
    Trace calculée. `points` : au moins 2 points, dans l'ordre du parcours. `distance_m` :
    distance routée annoncée par le moteur. `elevations_m` : altitude du moteur, parallèle à
    `points`, ou None (le DEM local la remplace de toute façon, RB-4.8). `warnings` : avertissements
    du moteur, texte sans donnée interne (RB-4.9).
    """

    points: tuple
    distance_m: float
    elevations_m: Optional[tuple] = None
    warnings: tuple = ()

    def __post_init__(self):
        # Construit à partir de la RÉPONSE du moteur : toute anomalie est une InvalidResponse (502),
        # jamais une InvalidRequest (400) qui accuserait l'utilisateur
        try:
            if len(self.points) > MAX_FILE_POINTS:      # RB-S4 : un adaptateur simplifie avant de construire
                raise InvalidResponse()
            points = tuple(validate_point(p) for p in self.points)
            elevations = None if self.elevations_m is None else tuple(self.elevations_m)
            warnings = tuple(_clean_warning(w) for w in list(self.warnings)[:MAX_WARNINGS])
        except (InvalidRequest, InvalidResponse, TypeError):
            raise InvalidResponse() from None
        if len(points) < 2 or not _finite(self.distance_m) or self.distance_m < 0:
            raise InvalidResponse()
        if elevations is not None and (len(elevations) != len(points) or not all(_finite(e) for e in elevations)):
            raise InvalidResponse()
        object.__setattr__(self, 'points', points)
        object.__setattr__(self, 'elevations_m', elevations)
        object.__setattr__(self, 'warnings', warnings)


def _clean_warning(value) -> str:
    """Texte du moteur, non fiable : caractères de contrôle retirés, longueur bornée (RB-4.9, RB-S9)."""
    text = ''.join(c for c in str(value) if c.isprintable())
    return text[:MAX_WARNING_LENGTH]


@dataclass(frozen=True)
class Health:
    """
    État du moteur tel que sondé (jamais à la demande d'une page, R3). `bbox` :
    (lat_min, lon_min, lat_max, lon_max) de l'emprise couverte ; `profiles` : profils annoncés.
    """

    available: bool
    bbox: Optional[tuple] = None
    profiles: tuple = ()

    def __post_init__(self):
        # Emprise et profils viennent du moteur (non fiable) et partent au front : validés ici.
        # Anomalie ⇒ InvalidResponse, que la tâche de sondage traduit en Health.down()
        if not isinstance(self.available, bool):
            raise InvalidResponse()
        bbox = self.bbox
        if bbox is not None:
            try:
                lat_min, lon_min, lat_max, lon_max = bbox
                corners = (validate_point((lat_min, lon_min)), validate_point((lat_max, lon_max)))
            except (InvalidRequest, TypeError, ValueError):
                raise InvalidResponse() from None
            if not (corners[0].lat < corners[1].lat and corners[0].lon < corners[1].lon):
                raise InvalidResponse()
            object.__setattr__(self, 'bbox', (corners[0].lat, corners[0].lon, corners[1].lat, corners[1].lon))
        try:
            profiles = tuple(validate_profile(p) for p in self.profiles)
        except (InvalidRequest, TypeError):
            raise InvalidResponse() from None
        if len(profiles) > MAX_HEALTH_PROFILES:
            raise InvalidResponse()
        object.__setattr__(self, 'profiles', profiles)

    @classmethod
    def down(cls) -> 'Health':
        return cls(available=False)


def _finite(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:       # entier trop grand pour un float (un JSON peut en porter)
        return False


def validate_point(point) -> Point:
    """Point valide (latitude −90..90, longitude −180..180, finies) ou InvalidRequest."""
    try:
        lat, lon = point
    except (TypeError, ValueError):
        raise InvalidRequest() from None
    if not (_finite(lat) and _finite(lon)) or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise InvalidRequest()
    return Point(float(lat), float(lon))


def validate_profile(profile) -> str:
    if (not isinstance(profile, str) or not profile or len(profile) > MAX_PROFILE_LENGTH
            or not all(c in _PROFILE_CHARS for c in profile)):
        raise InvalidRequest()
    return profile


def validate_deadline(deadline) -> float:
    """Échéance en secondes de l'horloge monotone (time.monotonic) : nombre fini, sinon InvalidRequest."""
    if not _finite(deadline):
        raise InvalidRequest()
    return float(deadline)


def time_left(deadline: float, clock=time.monotonic) -> float:
    """Secondes restantes avant l'échéance (négatif si dépassée). Sert de timeout à un appel sortant."""
    return deadline - clock()


def validate_route_request(waypoints: Sequence, profile) -> tuple:
    """Entrée de `route` : 2 à MAX_WAYPOINTS points valides, profil valide. Retourne (points, profil)."""
    if isinstance(waypoints, (str, bytes)) or not hasattr(waypoints, '__len__') or not 2 <= len(waypoints) <= MAX_WAYPOINTS:
        raise InvalidRequest()
    return tuple(validate_point(p) for p in waypoints), validate_profile(profile)


def validate_round_trip_request(start, profile, distance_m, seed) -> tuple:
    """Entrée de `round_trip` : point, profil, distance finie dans ]0, MAX_DISTANCE_M], graine entière bornée."""
    if not _finite(distance_m) or not 0 < distance_m <= MAX_DISTANCE_M:
        raise InvalidRequest()
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise InvalidRequest()
    return validate_point(start), validate_profile(profile), float(distance_m), seed


class RoutingProvider(ABC):
    """
    Moteur de routage. Contrat commun à tous les adaptateurs :
      - valider l'entrée (validate_*) AVANT tout appel sortant ;
      - `deadline` (time.monotonic) : ne JAMAIS la dépasser, reprises et attentes comprises ;
        timeout d'un appel = min(timeout par appel, time_left(deadline)) ; échéance déjà passée
        ⇒ RoutingTimeout sans appel ; échéance ou délai atteint en cours d'appel ⇒ RoutingTimeout ;
      - ne lever que des RoutingError, sans chaîner l'exception d'origine (`raise … from None`) :
        son `__cause__` porte l'URL et le corps de la réponse et finirait dans une trace
        journalisée (RB-S8) ; le détail va au journal de l'adaptateur, sans coordonnées ;
      - jamais d'URL, de réponse ni de coordonnée dans un message ;
      - ne pas suivre de redirection (RB-S3) ; lire la réponse en flux, bornée à 10 Mo, JSON borné,
        points ≤ MAX_FILE_POINTS (simplifier sinon) ; avertissements traduits en codes ou
        texte court (RouteResult les borne et nettoie, il ne peut pas les « expurger ») (RB-S4).
    `supports_round_trip` : False pour un moteur sans boucle native (ADR-0007 plan B) ; le builder
    et la stratégie de détours n'appellent alors pas `round_trip` (ADR-0010, S1 seule).
    """

    supports_round_trip = True

    @abstractmethod
    def route(self, waypoints: Sequence, profile: str, deadline: float) -> RouteResult:
        """Trajet P1 → … → Pn (aucune fermeture ici : c'est le travail du builder)."""

    @abstractmethod
    def round_trip(self, start, profile: str, distance_m: float, seed: int, deadline: float) -> RouteResult:
        """Boucle au départ de `start`, longueur ≈ `distance_m`, reproductible à graine égale."""

    @abstractmethod
    def health(self) -> Health:
        """Sonde le moteur. Ne lève pas : un moteur injoignable répond `Health.down()`."""
