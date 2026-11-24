"""
Provider de routage factice, déterministe, sans réseau (TR1) : sert aux tests du builder, des
détours et des routes (CA-R2 à R10, R15, R16, R24…). Réservé aux tests, volontairement absent de
app/ : il ne peut pas être choisi comme moteur par un réglage.

Géométrie connue, donc distance routée = fonction connue de la géométrie : chaque tronçon
entre deux points est la droite, ondulée perpendiculairement par un sinus d'amplitude
`sinuosity` × longueur du tronçon. `distance_m` est la longueur HAVERSINE de la trace
renvoyée (cohérente avec sa géométrie). Un point de passage unique et des points proches
donnent donc des distances prévisibles ; sinuosity=0 : lignes droites exactes.
"""

import math
import random
import threading
import time
from typing import Callable, Optional

from app.services.geo import haversine
from app.services.routing.base import (Health, NoRouteFound, RouteResult, RoutingProvider, RoutingTimeout,
                                       RoutingUnavailable, time_left, validate_deadline,
                                       validate_round_trip_request, validate_route_request)

M_PER_DEG_LAT = 111_194.9
STEP_M = 25.0
MAX_SAMPLES_PER_LEG = 4000
MAX_POINTS = 200_000          # MAX_FILE_POINTS : un adaptateur conforme (RB-S4) simplifie au-delà
LOOP_VERTICES = 36


def length_m(points) -> float:
    """Longueur haversine d'une suite de points (même formule que le reste de l'application)."""
    return float(sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:])))


def _clamp(point):
    """Ramène un point calculé dans les bornes WGS84 (pôles, antiméridien) sans toucher un point valide."""
    lat, lon = point
    lat = min(90.0, max(-90.0, lat))
    if not -180.0 <= lon <= 180.0:
        lon = (lon + 180.0) % 360.0 - 180.0
    return (lat, lon)


def _leg(a, b, sinuosity: float, side: int = 1, max_samples: int = MAX_SAMPLES_PER_LEG):
    """Points de a à b (inclus), échantillonnés tous les STEP_M, ondulés perpendiculairement."""
    dist = float(haversine(a[0], a[1], b[0], b[1]))
    n = max(1, min(max_samples, int(dist // STEP_M) + 1))
    cos_lat = max(math.cos(math.radians((a[0] + b[0]) / 2)), 1e-6)
    dx, dy = (b[1] - a[1]) * cos_lat, b[0] - a[0]          # direction en degrés « isotropes »
    norm = math.hypot(dx, dy) or 1.0
    px, py = -dy / norm, dx / norm                         # perpendiculaire unitaire
    amplitude_deg = sinuosity * dist / M_PER_DEG_LAT
    out = []
    for i in range(n + 1):
        t = i / n
        wave = side * amplitude_deg * math.sin(math.pi * t * 4) if 0 < i < n else 0.0
        out.append(_clamp((a[0] + dy * t + py * wave, a[1] + (dx * t + px * wave) / cos_lat)))
    return out


class FakeProvider(RoutingProvider):
    """
    Réglages :
      sinuosity    ondulation des tronçons (0 = droites) ;
      unroutable   fonction(point) -> bool : point non approchable ⇒ NoRouteFound(index) ;
      fail         exception (ou liste d'exceptions consommées une par appel) levée à la place du résultat ;
      delay_s      durée simulée de chaque appel, via `sleep` (injectable : horloge factice). Le factice
                   respecte l'échéance : déjà passée ⇒ RoutingTimeout sans appel enregistré ; plus longue
                   que le temps restant ⇒ dort jusqu'à l'échéance puis RoutingTimeout ;
      clock        horloge monotone (injectable, avec `sleep`, pour un temps factice) ;
      available    état renvoyé par health() ; bbox / profiles : emprise et profils annoncés.
    Traces : `calls` (nom, arguments) dans l'ordre ; `health_calls` ; `max_concurrent_seen`.
    """

    def __init__(self, sinuosity: float = 0.0, unroutable: Optional[Callable] = None, fail=None, delay_s: float = 0.0,
                 sleep: Callable = time.sleep, clock: Callable = time.monotonic, available: bool = True, bbox=(49.0, 2.0, 51.6, 6.5),
                 profiles=('foot', 'hike', 'racingbike', 'gravel', 'mtb')):
        self.sinuosity = sinuosity
        self.unroutable = unroutable
        self.fail = list(fail) if isinstance(fail, (list, tuple)) else fail
        self.delay_s, self._sleep, self._clock = delay_s, sleep, clock
        self._lock = threading.Lock()   # instrumentation seulement, jamais tenu pendant le « sommeil »
        self.available, self.bbox, self.profiles = available, bbox, tuple(profiles)
        self.calls: list = []
        self.health_calls = 0
        self._active = self.max_concurrent_seen = 0

    # -- instrumentation --------------------------------------------------------------------
    def _enter(self, name, deadline, *args):
        left = time_left(deadline, self._clock)
        if left <= 0:
            raise RoutingTimeout()
        with self._lock:
            self.calls.append((name, *args))
            self._active += 1
            self.max_concurrent_seen = max(self.max_concurrent_seen, self._active)
            if isinstance(self.fail, list):
                failure = self.fail.pop(0) if self.fail else None
            else:
                failure = self.fail
        try:
            if self.delay_s:
                self._sleep(min(self.delay_s, left))
                if self.delay_s >= left:
                    raise RoutingTimeout()
            if failure is not None:
                raise failure
        finally:
            with self._lock:
                self._active -= 1

    # -- interface ---------------------------------------------------------------------------
    def route(self, waypoints, profile, deadline):
        points, profile = validate_route_request(waypoints, profile)
        self._enter('route', validate_deadline(deadline), points, profile)
        for index, point in enumerate(points):
            if self.unroutable and self.unroutable(point):
                raise NoRouteFound(index)
        trace = [points[0]]
        per_leg = min(MAX_SAMPLES_PER_LEG, (MAX_POINTS - 1) // (len(points) - 1))
        for i, (a, b) in enumerate(zip(points, points[1:])):
            # Le côté alterne d'un tronçon à l'autre : déterministe, sans état
            trace.extend(_leg(a, b, self.sinuosity, 1 if i % 2 == 0 else -1, per_leg)[1:])
        return RouteResult(points=trace, distance_m=length_m(trace))

    def round_trip(self, start, profile, distance_m, seed, deadline):
        start, profile, distance_m, seed = validate_round_trip_request(start, profile, distance_m, seed)
        self._enter('round_trip', validate_deadline(deadline), start, profile, distance_m, seed)
        if self.unroutable and self.unroutable(start):
            raise NoRouteFound(0)
        # Polygone régulier de périmètre ≈ distance_m, tourné par la graine, passant par `start`
        rng = random.Random(seed)
        bearing = rng.uniform(0, 2 * math.pi)
        radius = distance_m / (2 * LOOP_VERTICES * math.sin(math.pi / LOOP_VERTICES))
        cos_lat = max(math.cos(math.radians(start[0])), 1e-6)
        cx = start[1] + math.cos(bearing) * radius / (M_PER_DEG_LAT * cos_lat)
        cy = start[0] + math.sin(bearing) * radius / M_PER_DEG_LAT
        ring = []
        for k in range(LOOP_VERTICES):
            angle = bearing + math.pi + 2 * math.pi * k / LOOP_VERTICES
            ring.append(_clamp((cy + math.sin(angle) * radius / M_PER_DEG_LAT,
                         cx + math.cos(angle) * radius / (M_PER_DEG_LAT * cos_lat))))
        ring.append(ring[0])
        ring[0] = ring[-1] = tuple(start)
        trace = []
        for a, b in zip(ring, ring[1:]):
            trace.extend(_leg(a, b, 0.0)[(1 if trace else 0):])
        return RouteResult(points=trace, distance_m=length_m(trace))

    def health(self):
        with self._lock:
            self.health_calls += 1
        if not self.available:
            return Health.down()
        return Health(available=True, bbox=self.bbox, profiles=self.profiles)


def unavailable(times: int = 1):
    """Liste d'échecs « moteur injoignable » à passer à FakeProvider(fail=…)."""
    return [RoutingUnavailable() for _ in range(times)]
