"""
Stratégies de détours (ADR-0010, gpx-route-builder RB-4.4, RB-4.11) : à partir de la boucle de base et du manque de distance,
proposent des points de passage techniques (S1) ou des pétales (S2) ; l'hybride enchaîne S1 puis S2.

Ne connaît pas le moteur, seulement `RoutingProvider`. Pur : ni Flask, ni base, ni environnement. Chaque stratégie est une classe
enregistrée dans `STRATEGIES` : en ajouter une ne touche ni `builder.py` ni les routes (CA-R18).

Garanties communes :
  - l'ordre P1 … Pn et la fermeture sont préservés : un candidat qui ne passe pas le contrôle d'ordre (`ctx.accepts`) est écarté ;
  - les points techniques sont contenus dans `ctx.bbox` (RB-S7) ;
  - tous les appels passent par `Budget` : jamais plus de `max_calls`, jamais au-delà de l'échéance ;
  - reproductible : `random.Random(seed)` seul fixe les choix (côté des détours, ancrage des pétales).
"""

import math
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import numpy as np

from app.services import geo
from app.services.routing import quality, zone
from app.services.routing.base import NoRouteFound, RoutingError, RoutingTimeout

M_PER_DEG = 111_194.9
MAX_ITERATIONS = 4
STAGNATION_M = 1.0          # deux itérations qui rendent la même distance : le moteur ignore les points, inutile d'insister


class BudgetExhausted(Exception):
    """Plus d'appel ou plus de temps : la stratégie rend sa meilleure trace."""


class Budget:
    """Appels et temps d'une génération (RB-4.6). `clock` injectable (monotone)."""

    def __init__(self, max_calls: int, total_s: float, clock: Callable = time.monotonic):
        self.max_calls, self.total_s, self._clock = int(max_calls), float(total_s), clock
        self.started = clock()
        self.deadline = self.started + self.total_s
        self.calls = 0

    def time_left(self) -> float:
        return self.deadline - self._clock()

    def calls_left(self) -> int:
        return max(0, self.max_calls - self.calls) if self.time_left() > 0 else 0

    def call(self, fn: Callable, *args):
        """`fn(*args, deadline)` ; compte l'appel AVANT de l'émettre."""
        if self.calls_left() <= 0:
            raise BudgetExhausted()
        self.calls += 1
        return fn(*args, self.deadline)


@dataclass
class Outcome:
    """Résultat d'une stratégie : meilleure trace obtenue (peut rester hors tolérance)."""

    points: list
    distance_m: float
    detours: int = 0
    iterations: int = 0
    warnings: List[str] = field(default_factory=list)       # codes : 'user_point_unreachable', …
    elevations: Optional[tuple] = None
    engine_warnings: tuple = ()                 # avertissements du moteur (RB-4.9), déjà nettoyés par l'interface


@dataclass
class DetourContext:
    provider: object
    profile: str
    points: Sequence                 # points de l'utilisateur conservés (après dédoublonnage), ordre d'origine
    base_points: list                # boucle de base L0 : P1 → … → Pn → P1
    base_distance_m: float
    target_m: float
    tolerance: float                 # fraction (0,15)
    seed: int
    budget: Budget
    bbox: tuple                      # (lat_min, lon_min, lat_max, lon_max) où les points techniques doivent rester (RB-S7)
    accepts: Callable = lambda points: True       # contrôle d'ordre et de fermeture d'un candidat
    area: Optional[list] = None      # sommets [lon, lat] du polygone : aucun point technique n'en sort (RB-1.3b)

    def in_tolerance(self, distance_m: float) -> bool:
        return abs(distance_m - self.target_m) <= self.tolerance * self.target_m

    @property
    def closed_sequence(self) -> list:
        return list(self.points) + [self.points[0]]


def _dist(a, b) -> float:
    return float(geo.haversine(a[0], a[1], b[0], b[1]))


def _clamp(point, bbox):
    lat_min, lon_min, lat_max, lon_max = bbox
    return (min(lat_max, max(lat_min, point[0])), min(lon_max, max(lon_min, point[1])))


def _offset_point(a, b, height_m: float, side: int):
    """Point à `height_m` du milieu de [a, b], perpendiculairement, du côté `side` (+1 / −1)."""
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    cos_lat = max(math.cos(math.radians(mid[0])), 1e-6)
    dx, dy = (b[1] - a[1]) * cos_lat, b[0] - a[0]
    norm = math.hypot(dx, dy) or 1.0
    px, py = -dy / norm, dx / norm
    return (mid[0] + side * py * height_m / M_PER_DEG, mid[1] + side * px * height_m / (M_PER_DEG * cos_lat))


def _sides(seed: int, count: int, rng) -> list:
    """
    Côté (gauche / droite) du décalage de chaque tronçon. Les graines CONSÉCUTIVES donnent des combinaisons DISTINCTES (chiffres
    binaires de la graine, période 2^count) : « Autre proposition » ne retombe pas sur une trace déjà vue tant que le nombre de
    combinaisons le permet ; même graine => mêmes côtés (reproductible, RB-4.4). Au-delà de 16 tronçons : tirage par la graine.
    """
    if count > 16:
        return [rng.choice((-1, 1)) for _ in range(count)]
    return [1 if (seed >> i) & 1 else -1 for i in range(count)]


class DetourStrategy(ABC):
    name = ''
    needs_round_trip = False

    @abstractmethod
    def run(self, ctx: DetourContext, start: Optional[Outcome] = None) -> Outcome:
        """Meilleure trace trouvée. `start` : trace à prolonger (l'hybride y fait greffer ses pétales) ; sinon la base."""


def _better(candidate: Outcome, best: Outcome, target_m: float) -> bool:
    return abs(candidate.distance_m - target_m) < abs(best.distance_m - target_m)


# -- S1 : points de passage latéraux ---------------------------------------------------------------------------------------

class LateralPoints(DetourStrategy):
    """
    Un point technique par tronçon (fermeture comprise), au milieu, décalé perpendiculairement du côté tiré par la graine ;
    le manque Δ est réparti proportionnellement à la longueur des tronçons ; correction itérative de l'ampleur (≤ 4 itérations).
    Point technique non routable : autre côté, puis ampleur réduite de moitié.
    """

    name = 's1'

    def run(self, ctx, start=None):
        rng = random.Random(ctx.seed)
        base = start or Outcome(list(ctx.base_points), ctx.base_distance_m)
        seq = ctx.closed_sequence
        out = Outcome(list(base.points), base.distance_m, elevations=base.elevations, engine_warnings=base.engine_warnings)
        delta = ctx.target_m - ctx.base_distance_m
        if delta <= 0:
            return out
        chords = [_dist(a, b) for a, b in zip(seq, seq[1:])]
        total_chord = sum(chords) or 1.0
        index = ctx.base_distance_m / total_chord                  # routé / vol d'oiseau
        sides = _sides(ctx.seed, len(chords), rng)
        heights = []
        for chord in chords:
            extra = delta * chord / total_chord / max(index, 1.0)
            heights.append(math.sqrt(((chord + extra) / 2) ** 2 - (chord / 2) ** 2))
        # Ampleur initiale variée avec la graine (au-delà des côtés) : plus de variantes distinctes pour peu de tronçons
        best, scale, previous = out, (1.0, 0.8, 1.25)[(ctx.seed >> len(chords)) % 3] if len(chords) <= 16 else 1.0, None
        for iteration in range(1, MAX_ITERATIONS + 1):
            waypoints, techs, tech_segments = [], [], []
            for i, (a, b) in enumerate(zip(seq, seq[1:])):
                waypoints.append(a)
                tech = _clamp(_offset_point(a, b, heights[i] * scale, sides[i]), ctx.bbox)
                if ctx.area:                           # RB-1.3b : ramené dans le polygone, ou pas de point à ce tronçon
                    tech = zone.pull_inside(ctx.area, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), tech)
                if tech is not None:
                    waypoints.append(tech)
                    techs.append(len(waypoints) - 1)
                    tech_segments.append(i)
            waypoints.append(seq[-1])
            try:
                result = ctx.budget.call(ctx.provider.route, waypoints, ctx.profile)
            except NoRouteFound as exc:
                if exc.waypoint_index not in techs:
                    out.warnings.append('user_point_unreachable')
                    break
                bad = tech_segments[techs.index(exc.waypoint_index)]
                if iteration % 2 == 1:
                    sides[bad] = -sides[bad]                       # autre côté d'abord
                else:
                    scale *= 0.5                                    # puis ampleur réduite
                continue
            except (RoutingError, BudgetExhausted):
                break
            distance = quality.length_m(result.points)
            if ctx.accepts(result.points):
                candidate = Outcome(list(result.points), distance, detours=len(techs), iterations=iteration,
                                    elevations=result.elevations_m, engine_warnings=tuple(result.warnings))
                if _better(candidate, best, ctx.target_m):
                    best = candidate
                if ctx.in_tolerance(distance):
                    return candidate
            if previous is not None and abs(distance - previous) < STAGNATION_M:
                break                                              # le moteur ignore les points : inutile d'insister
            previous = distance
            obtained = distance - ctx.base_distance_m
            wanted = ctx.target_m - ctx.base_distance_m
            scale *= min(3.0, max(0.3, wanted / obtained)) if obtained > 1 else 2.0
        best.warnings = list(out.warnings)
        return best


# -- S2 : pétales -----------------------------------------------------------------------------------------------------------

def _graft(trace: list, anchor, petal: list) -> list:
    """Insère la boucle `petal` dans `trace` au sommet le plus proche de `anchor`."""
    arr = np.asarray(trace, dtype=float)
    i = int(np.argmin(geo.haversine(anchor[0], anchor[1], arr[:, 0], arr[:, 1])))
    return list(trace[:i + 1]) + list(petal) + list(trace[i + 1:])


class Petals(DetourStrategy):
    """
    Pétales : boucles natives du moteur (`round_trip`) de longueur Δ/k, greffées sur des points de l'utilisateur tirés par la
    graine ; ajustement de la longueur des pétales (≤ 4 itérations). Exige un moteur doté de `round_trip`.
    """

    name = 's2'
    needs_round_trip = True

    def run(self, ctx, start=None):
        base = start or Outcome(list(ctx.base_points), ctx.base_distance_m)
        out = Outcome(list(base.points), base.distance_m)
        if not getattr(ctx.provider, 'supports_round_trip', True):
            return out
        delta = ctx.target_m - base.distance_m
        if delta <= 0:
            return out
        rng = random.Random(ctx.seed)
        count = max(1, min(len(ctx.points), round(delta / max(0.25 * ctx.target_m, 1.0))))
        anchors = rng.sample(list(ctx.points), count)
        petal_target, best, previous = delta / count, out, None
        for iteration in range(1, MAX_ITERATIONS + 1):
            trace, petals, notes = list(base.points), 0, list(base.engine_warnings)
            try:
                for j, anchor in enumerate(anchors):
                    petal = ctx.budget.call(
                        lambda *a, anchor=anchor, j=j, length=petal_target: ctx.provider.round_trip(
                            anchor, ctx.profile, length, ctx.seed + j, a[-1]))
                    trace = _graft(trace, anchor, list(petal.points))
                    petals += 1
                    notes.extend(petal.warnings)
            except NoRouteFound:
                petal_target *= 0.7
                continue
            except (RoutingError, BudgetExhausted):
                break
            distance = quality.length_m(trace)
            if ctx.accepts(trace):
                candidate = Outcome(trace, distance, detours=petals, iterations=iteration,
                                    engine_warnings=tuple(dict.fromkeys(notes)))
                if _better(candidate, best, ctx.target_m):
                    best = candidate
                if ctx.in_tolerance(distance):
                    return candidate
            if previous is not None and abs(distance - previous) < STAGNATION_M:
                break
            previous = distance
            obtained = distance - base.distance_m
            wanted = ctx.target_m - base.distance_m
            petal_target *= min(3.0, max(0.3, wanted / obtained)) if obtained > 1 else 2.0
        return best


# -- Hybride (RB-4.11) ---------------------------------------------------------------------------------------------------------

class Hybrid(DetourStrategy):
    """S1 d'abord ; si la trace reste trop courte et qu'il reste du budget, un pétale S2 comble le manque restant."""

    name = 'hybrid'
    needs_round_trip = False       # sans `round_trip`, l'hybride se réduit à S1

    def run(self, ctx, start=None):
        first = LateralPoints().run(ctx, start)
        if ctx.in_tolerance(first.distance_m) or first.distance_m >= (1 - ctx.tolerance) * ctx.target_m:
            return first                                          # dans la tolérance, ou trop longue : S2 n'y change rien
        if not getattr(ctx.provider, 'supports_round_trip', True) or ctx.budget.calls_left() <= 0:
            return first
        second = Petals().run(ctx, first)
        second.warnings = first.warnings + second.warnings
        second.detours += first.detours
        return second if _better(second, first, ctx.target_m) else first


STRATEGIES = {cls.name: cls for cls in (LateralPoints, Petals, Hybrid)}


def get_strategy(name: str) -> DetourStrategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        raise ValueError("Stratégie de détours inconnue") from None


__all__ = ['Budget', 'BudgetExhausted', 'DetourContext', 'DetourStrategy', 'Hybrid', 'LateralPoints', 'Outcome', 'Petals',
           'STRATEGIES', 'get_strategy', 'RoutingTimeout']
