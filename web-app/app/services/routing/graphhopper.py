"""
Adaptateur GraphHopper (11.0, ADR-0007) pour l'interface `RoutingProvider`. Pas de Flask, pas de base : la politique de
destination (`OUTBOUND_ALLOWED_HOSTS`, RB-S3) est INJECTÉE (`guard`) et appelée avant CHAQUE appel sortant.

  - entrée validée (`base.validate_*`) avant tout appel ; échéance respectée, reprises comprises (RB-S6) ;
  - redirections jamais suivies (RB-S3) ; réponse lue en flux, bornée à 10 Mo, JSON borné, coordonnées validées, points
    ≤ MAX_FILE_POINTS (décimation sinon) (RB-S4) ;
  - erreurs fixes, jamais chaînées (`from None`) : ni URL, ni corps, ni coordonnée ne sortent (RB-S8, RB-S9) ; le détail va
    au journal de l'adaptateur sans coordonnée ;
  - `health()` ne lève jamais.
Le cas « point non approchable » du moteur (400, `PointNotFoundException`, `point_index`) devient `NoRouteFound(index)`.
"""

import json
import logging
import threading
import time
from typing import Callable, Optional

import requests

from app import outbound
from app.services.file_formats import MAX_FILE_POINTS
from app.services.routing.base import (Health, InvalidResponse, NoRouteFound, RouteResult, RoutingProvider, RoutingTimeout,
                                       RoutingUnavailable, time_left, validate_deadline, validate_round_trip_request,
                                       validate_route_request)

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
CHUNK = 64 * 1024
HEALTH_TIMEOUT_S = 3.0
MAX_VISITED_NODES = 5_000_000      # borne large (2,2 M mesuré pour 440 km) ; seul le paramètre de REQUÊTE compte en 11.0
MAX_RETRIES = 1                    # reprise sur 5xx / connexion refusée, décomptée du budget (RB-S6)
MIN_RETRY_LEFT_S = 0.5             # pas de reprise s'il reste moins que cela


def _decimate(points: list, limit: int = MAX_FILE_POINTS) -> list:
    """Au plus `limit` points, premier et dernier conservés, répartition régulière."""
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


class GraphHopperProvider(RoutingProvider):
    supports_round_trip = True

    def __init__(self, base_url: str, *, guard: Callable[[str], None], timeout_s: float = 20.0, api_key: Optional[str] = None,
                 allow_cleartext_key: bool = False, session=None, clock=time.monotonic):
        """
        `guard` est OBLIGATOIRE (TR3-2) : la politique de destination (`outbound.guard`) tourne avant chaque appel ; un adaptateur
        sans garde échouerait ouvert. La clé API part en EN-TÊTE (jamais dans l'URL, que `urllib3` journalise) et
        seulement en https, sauf `allow_cleartext_key` (réseau interne explicitement choisi) (TR3-3).
        """
        if not callable(guard):
            raise TypeError("guard obligatoire")
        outbound.normalize_url(base_url)                                         # http(s), sans identifiants, hôte valide
        if api_key and not base_url.lower().startswith('https://') and not allow_cleartext_key:
            raise ValueError("Une clé API ne part pas en clair : https requis")
        self._destination = outbound.destination_of_url(base_url)
        self._base = base_url.rstrip('/')
        self._timeout = float(timeout_s)
        self._key = api_key or None
        self._guard = guard
        self._session = session if session is not None else requests.Session()
        self._clock = clock

    # -- transport ---------------------------------------------------------------------------------------------------

    def _get(self, path: str, params, *, timeout: float, deadline: Optional[float]):
        """Un GET borné : (code HTTP, octets lus). Lève RoutingUnavailable / RoutingTimeout / InvalidResponse."""
        if self._guard is not None:
            try:
                self._guard(self._destination)
            except Exception:  # noqa: BLE001 - refus audité par la garde elle-même ; message fixe côté appelant
                raise RoutingUnavailable() from None
        headers = {'Authorization': f'Bearer {self._key}'} if self._key else None
        try:
            response = self._session.get(f'{self._base}{path}', params=params, timeout=timeout, allow_redirects=False,
                                         stream=True, **({'headers': headers} if headers else {}))
        except requests.Timeout:
            raise RoutingTimeout() from None
        except requests.RequestException:
            raise RoutingUnavailable() from None
        try:
            status = response.status_code
            if 300 <= status < 400:
                logger.warning("Moteur de routage : redirection refusée (%s)", status)
                raise InvalidResponse()
            body = bytearray()
            # Échéance de la LECTURE (TR3-1) : un minuteur ferme la connexion à l'échéance (débloque une lecture lente) et
            # le temps restant est contrôlé entre les blocs ; un moteur qui goutte-à-goutte ne retient jamais au-delà
            watchdog = None
            if deadline is not None:
                watchdog = threading.Timer(max(0.0, time_left(deadline, self._clock)), response.close)
                watchdog.daemon = True
                watchdog.start()
            try:
                for chunk in response.iter_content(CHUNK):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        logger.warning("Moteur de routage : réponse trop volumineuse")
                        raise InvalidResponse()
                    if deadline is not None and time_left(deadline, self._clock) <= 0:
                        raise RoutingTimeout()
            except (RoutingTimeout, InvalidResponse):
                raise
            except requests.Timeout:
                raise RoutingTimeout() from None
            except Exception:  # noqa: BLE001 - connexion fermée par le minuteur, coupure : jamais le détail
                if deadline is not None and time_left(deadline, self._clock) <= 0:
                    raise RoutingTimeout() from None
                raise RoutingUnavailable() from None
            finally:
                if watchdog is not None:
                    watchdog.cancel()
            if deadline is not None and time_left(deadline, self._clock) < 0:
                raise RoutingTimeout()
            return status, bytes(body)
        finally:
            response.close()

    def _call(self, path: str, params, deadline: float):
        """GET avec reprises limitées sur 5xx / connexion, jamais au-delà de l'échéance."""
        deadline = validate_deadline(deadline)
        attempt = 0
        while True:
            left = time_left(deadline, self._clock)
            if left <= 0:
                raise RoutingTimeout()
            try:
                status, body = self._get(path, params, timeout=min(self._timeout, left), deadline=deadline)
                failure = status >= 500 or status == 429
            except RoutingTimeout:
                raise
            except RoutingUnavailable:
                status, body, failure = None, b'', True
            if not failure:
                return status, body
            attempt += 1
            if attempt > MAX_RETRIES or time_left(deadline, self._clock) < MIN_RETRY_LEFT_S:
                logger.warning("Moteur de routage indisponible (statut %s)", status)
                raise RoutingUnavailable()

    @staticmethod
    def _json(body: bytes):
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise InvalidResponse() from None

    # -- erreurs du moteur ------------------------------------------------------------------------------------------

    def _client_error(self, status: int, body: bytes):
        """400 du moteur : point non approchable / pas de liaison => NoRouteFound ; le reste est une incohérence (502)."""
        try:
            data = json.loads(body)
            hints = data.get('hints') if isinstance(data, dict) else None
            first = hints[0] if isinstance(hints, list) and hints and isinstance(hints[0], dict) else {}
            details = str(first.get('details', ''))
            message = str(data.get('message', '')) if isinstance(data, dict) else ''
            index = first.get('point_index')
        except (ValueError, RecursionError, AttributeError):
            raise InvalidResponse() from None
        if 'PointNotFoundException' in details:
            raise NoRouteFound(index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None)
        if 'maximum nodes exceeded' in message.lower():
            raise NoRouteFound(None)                         # trajet trop complexe pour la borne : aucun itinéraire exploitable
        if 'ConnectionNotFoundException' in details or 'PointOutOfBoundsException' in details:
            raise NoRouteFound(index if isinstance(index, int) and not isinstance(index, bool) and index >= 0 else None)
        logger.warning("Moteur de routage : requête refusée (%s, %s)", status, details[:80])
        raise InvalidResponse()

    def _result(self, status: int, body: bytes) -> RouteResult:
        if status == 400:
            self._client_error(status, body)
        if status != 200:
            logger.warning("Moteur de routage : statut inattendu %s", status)
            raise InvalidResponse()
        data = self._json(body)
        try:
            path = data['paths'][0]
            coordinates = path['points']['coordinates']
            distance = path['distance']
            if not isinstance(coordinates, list):
                raise TypeError
            coordinates = _decimate(coordinates)
            points, elevations = [], []
            for item in coordinates:
                lon, lat, *rest = item
                points.append((lat, lon))
                if rest:
                    elevations.append(rest[0])
            has_elevation = len(elevations) == len(points)
            return RouteResult(points=tuple(points), distance_m=distance,
                               elevations_m=tuple(elevations) if has_elevation else None)
        except (KeyError, IndexError, TypeError, ValueError):
            raise InvalidResponse() from None

    # -- RoutingProvider -------------------------------------------------------------------------------------------

    def route(self, waypoints, profile, deadline):
        points, profile = validate_route_request(waypoints, profile)
        params = [('point', f'{p.lat:.6f},{p.lon:.6f}') for p in points]
        params += [('profile', profile), ('points_encoded', 'false'), ('instructions', 'false'), ('calc_points', 'true'),
                   ('max_visited_nodes', MAX_VISITED_NODES)]
        return self._result(*self._call('/route', params, deadline))

    def round_trip(self, start, profile, distance_m, seed, deadline):
        start, profile, distance_m, seed = validate_round_trip_request(start, profile, distance_m, seed)
        params = [('point', f'{start.lat:.6f},{start.lon:.6f}'), ('profile', profile), ('algorithm', 'round_trip'),
                  ('round_trip.distance', int(distance_m)), ('round_trip.seed', seed), ('points_encoded', 'false'),
                  ('instructions', 'false'), ('max_visited_nodes', MAX_VISITED_NODES)]
        return self._result(*self._call('/route', params, deadline))

    def health(self) -> Health:
        """Sonde `/health` puis `/info` (emprise annoncée et profils : diagnostic seulement, RB-1.3). Ne lève jamais."""
        deadline = self._clock() + HEALTH_TIMEOUT_S * 2
        try:
            status, _ = self._get('/health', [], timeout=HEALTH_TIMEOUT_S, deadline=deadline)
            if status != 200:
                return Health.down()
            bbox, profiles = None, ()
            try:
                info_status, body = self._get('/info', [], timeout=HEALTH_TIMEOUT_S, deadline=deadline)
                if info_status == 200:
                    info = self._json(body)
                    raw = info.get('bbox')
                    if isinstance(raw, list) and len(raw) == 4:
                        bbox = (raw[1], raw[0], raw[3], raw[2])          # [lon_min, lat_min, lon_max, lat_max] -> (lat, lon…)
                    profiles = tuple(p['name'] for p in info.get('profiles', []))
            except (RoutingUnavailable, InvalidResponse, AttributeError, KeyError, TypeError):
                bbox, profiles = None, ()
            return Health(available=True, bbox=bbox, profiles=profiles)
        except InvalidResponse:
            return Health.down()
        except Exception:  # noqa: BLE001 - la sonde ne lève jamais
            return Health.down()
