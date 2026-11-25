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
import re
import socket
import threading
import time
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from app import outbound
from app.services.file_formats import MAX_FILE_POINTS
from app.services.routing.base import (MAX_HEALTH_PROFILES, Health, InvalidResponse, NoRouteFound, RouteResult,
                                       RoutingProvider, RoutingTimeout, RoutingUnavailable, time_left, validate_deadline,
                                       validate_round_trip_request, validate_route_request)

logger = logging.getLogger(__name__)
# urllib3 journalise l'URL (coordonnées de la trace) dès DEBUG : jamais dans les journaux (RB-S9, CA-R17)
logging.getLogger('urllib3').setLevel(logging.WARNING)

_PROFILE_NAME = re.compile(r'[a-z0-9_]{1,32}')
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
CHUNK = 64 * 1024
HEALTH_TIMEOUT_S = 3.0
MAX_VISITED_NODES = 5_000_000      # borne large (2,2 M mesuré pour 440 km) ; seul le paramètre de REQUÊTE compte en 11.0
MAX_RETRIES = 1                    # reprise sur 5xx / connexion refusée, décomptée du budget (RB-S6)
MIN_RETRY_LEFT_S = 0.5             # pas de reprise s'il reste moins que cela


_local = threading.local()


class _TrackedHTTP(HTTPConnection):
    def connect(self):
        super().connect()
        _track(self.sock)


class _TrackedHTTPS(HTTPSConnection):
    def connect(self):
        super().connect()
        _track(self.sock)


class _Registry:
    """Sockets d'UN appel. Une fois fermé (échéance), tout socket créé ensuite est coupé aussitôt."""

    def __init__(self):
        self._socks: list = []
        self._lock = threading.Lock()
        self.closed = False

    def add(self, sock) -> None:
        with self._lock:
            if not self.closed:
                self._socks.append(sock)
                return
        _cut(sock)

    def close(self) -> None:
        """Échéance : coupe les sockets de l'appel (et ceux créés plus tard)."""
        with self._lock:
            self.closed = True
            socks, self._socks = self._socks, []
        for sock in socks:
            _cut(sock)

    def detach(self) -> None:
        """Fin NORMALE de l'appel : on ne touche plus aux sockets (ils appartiennent à la session de l'appel, qui les ferme)."""
        with self._lock:
            self.closed = True
            self._socks = []


def _cut(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _track(sock) -> None:
    registry = getattr(_local, 'registry', None)
    if registry is not None and sock is not None:
        registry.add(sock)


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _TrackedHTTP


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _TrackedHTTPS


class _DeadlineAdapter(HTTPAdapter):
    """Adaptateur dont les connexions s'inscrivent dans le registre de l'appel en cours : le minuteur d'échéance peut ainsi
    COUPER le socket (shutdown) même au milieu de la lecture des en-têtes, là où `timeout` est remis à zéro à chaque octet."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': _HTTPPool, 'https': _HTTPSPool}


def _new_session():
    session = requests.Session()
    for scheme in ('http://', 'https://'):
        session.mount(scheme, _DeadlineAdapter())
    return session


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
        self._session = session                          # None : une session NEUVE par appel (aucune connexion réutilisée)
        self._clock = clock

    # -- transport ---------------------------------------------------------------------------------------------------

    def _get(self, path: str, params, *, timeout: float, deadline: Optional[float]):
        """Un GET borné : (code HTTP, octets lus). Lève RoutingUnavailable / RoutingTimeout / InvalidResponse."""
        if self._guard is not None:
            try:
                self._guard(self._destination)
            except Exception:  # noqa: BLE001 - refus audité par la garde elle-même ; message fixe côté appelant
                raise RoutingUnavailable() from None
        headers = {'Connection': 'close'}               # pas de connexion réutilisée : un registre de sockets par appel
        if self._key:
            headers['Authorization'] = f'Bearer {self._key}'
        # ÉCHÉANCE (TR3-1, TR3-1b) : l'appel tourne dans un fil de travail et l'appelant n'attend que le temps restant, quoi qu'il
        # arrive (résolution DNS, connexion TCP, en-têtes, corps : `timeout` seul est remis à zéro à chaque octet reçu). À
        # l'échéance, les sockets de l'appel sont coupés (ceux créés plus tard le sont à leur création) et le fil est abandonné.
        if deadline is None:
            return self._exchange(path, params, timeout, None, headers)
        registry = _Registry()
        outcome: dict = {}
        done = threading.Event()

        def work():
            _local.registry = registry
            try:
                outcome['value'] = self._exchange(path, params, timeout, deadline, headers)
            except BaseException as exc:  # noqa: BLE001 - relayée à l'appelant
                outcome['error'] = exc
            finally:
                _local.registry = None
                done.set()

        threading.Thread(target=work, name='routing-call', daemon=True).start()
        if not done.wait(max(0.0, time_left(deadline, self._clock))):
            registry.close()
            raise RoutingTimeout()
        registry.detach()
        if 'error' in outcome:
            raise outcome['error']
        return outcome['value']

    def _exchange(self, path, params, timeout, deadline, headers):
        expired = lambda: deadline is not None and time_left(deadline, self._clock) <= 0      # noqa: E731
        own = self._session is None
        session = _new_session() if own else self._session
        try:
            response = session.get(f'{self._base}{path}', params=params, timeout=timeout, allow_redirects=False,
                                   stream=True, headers=headers)
        except requests.Timeout:
            if own:
                session.close()
            raise RoutingTimeout() from None
        except requests.TooManyRedirects:
            if own:
                session.close()
            raise InvalidResponse() from None
        except Exception:  # noqa: BLE001 - connexion coupée par l'échéance ou injoignable : jamais le détail
            if own:
                session.close()
            raise (RoutingTimeout() if expired() else RoutingUnavailable()) from None
        try:
            status = response.status_code
            if 300 <= status < 400:
                logger.warning("Moteur de routage : redirection refusée (%s)", status)
                raise InvalidResponse()
            body = bytearray()
            try:
                for chunk in response.iter_content(CHUNK):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        logger.warning("Moteur de routage : réponse trop volumineuse")
                        raise InvalidResponse()
                    if expired():
                        raise RoutingTimeout()
            except (RoutingTimeout, InvalidResponse):
                raise
            except requests.Timeout:
                raise RoutingTimeout() from None
            except Exception:  # noqa: BLE001 - connexion coupée : jamais le détail
                raise (RoutingTimeout() if expired() else RoutingUnavailable()) from None
            if deadline is not None and time_left(deadline, self._clock) < 0:
                raise RoutingTimeout()
            return status, bytes(body)
        finally:
            response.close()
            if own:
                session.close()                      # ferme la connexion de CET appel, jamais celle d'un autre

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
        logger.warning("Moteur de routage : requête refusée (statut %s)", status)       # jamais le texte du moteur (coordonnées)
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
                    names = (p.get('name') for p in info.get('profiles', []) if isinstance(p, dict))
                    # Un profil au nom inattendu est IGNORÉ (le moteur reste disponible), au plus MAX_HEALTH_PROFILES retenus
                    profiles = tuple(n for n in names if isinstance(n, str) and _PROFILE_NAME.fullmatch(n))[:MAX_HEALTH_PROFILES]
            except (RoutingUnavailable, InvalidResponse, AttributeError, KeyError, TypeError):
                bbox, profiles = None, ()
            return Health(available=True, bbox=bbox, profiles=profiles)
        except InvalidResponse:
            return Health.down()
        except Exception:  # noqa: BLE001 - la sonde ne lève jamais
            return Health.down()
