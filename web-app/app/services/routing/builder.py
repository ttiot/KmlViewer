"""
Orchestration d'une génération (gpx-route-builder §5.4) : validation stricte (RB-S1, RB-S1b), boucle de base, détours,
essais de graine (RB-4.12), budget (RB-4.6), contrôles qualité (RB-4.7), avertissements, GPX (RB-4.8).

Ne lit ni l'environnement ni la base : la configuration arrive dans `BuildConfig`. Aucun appel au moteur avant la fin de la
validation. La stratégie de détours vient de `detours.STRATEGIES` : en ajouter une ne touche pas ce module (CA-R18).
Les erreurs du moteur (`RoutingUnavailable`, `RoutingTimeout`, `InvalidResponse`, `NoRouteFound`) remontent telles quelles quand
aucune trace n'est disponible ; avec une trace en main, un échec ou un budget épuisé n'est qu'un avertissement.
"""

import logging
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Sequence

from app.services import geo
from app.services.file_formats import MAX_FILE_POINTS
from app.services.routing import detours, profiles, quality
from app.services.routing.base import (InvalidRequest, InvalidResponse, NoRouteFound, RoutingError, RoutingProvider,
                                       RoutingTimeout, validate_point)

logger = logging.getLogger(__name__)

MIN_DISTANCE_KM = 1.0               # RB-3.2 : 1 à 300 km (réglage)
DUPLICATE_M = 5.0                   # RB-S1 : doublons consécutifs et Pn proche de P1
MIN_SEED_CALLS = 3                  # RB-4.12 : un essai de graine supplémentaire exige au moins 3 appels et 5 s restants
MIN_SEED_SECONDS = 5.0
GPX_NS = 'http://www.topografix.com/GPX/1/1'
MAX_SEED = 2 ** 31 - 1


class InvalidBuild(ValueError):
    """Entrée refusée avant tout appel au moteur (400). Message destiné à l'utilisateur, sans donnée interne."""


class UnreachablePoint(NoRouteFound):
    """Point de l'utilisateur non approchable (422). `number` : numéro d'ORIGINE, 1-based, tel qu'envoyé (m12)."""

    def __init__(self, number: int, waypoint_index: Optional[int] = None):
        super().__init__(waypoint_index)
        self.number = number


@dataclass(frozen=True)
class BuildConfig:
    """Réglages d'une génération (valeurs du registre `routing.*`, bornes déjà garanties à l'écriture)."""

    max_waypoints: int = 50
    max_distance_km: float = 300.0
    tolerance_pct: float = 15.0
    snap_tolerance_m: float = 50.0
    overlap_warning_pct: float = 25.0
    overlap_warning_pct_2pts: float = 40.0
    max_calls: int = 15
    total_budget_s: float = 30.0
    detour_strategy: str = 'hybrid'
    seed_retries: int = 2
    coverage_bbox: Optional[tuple] = None        # (lat_min, lon_min, lat_max, lon_max) ; RB-1.3, vérifié avant tout appel

    @classmethod
    def from_settings(cls, get: Callable[[str], object]) -> 'BuildConfig':
        bbox = None
        try:
            raw = get('routing.coverage_bbox')                     # registre : [lon_min, lat_min, lon_max, lat_max]
            bbox = (float(raw[1]), float(raw[0]), float(raw[3]), float(raw[2])) if raw else None     # -> (lat, lon, lat, lon)
        except (KeyError, TypeError, ValueError, IndexError):
            bbox = None
        return cls(max_waypoints=int(get('routing.max_waypoints')), max_distance_km=float(get('routing.max_distance_km')),
                   tolerance_pct=float(get('routing.distance_tolerance_pct')),
                   snap_tolerance_m=float(get('routing.snap_tolerance_m')),
                   overlap_warning_pct=float(get('routing.overlap_warning_pct')),
                   overlap_warning_pct_2pts=float(get('routing.overlap_warning_pct_2pts')),
                   max_calls=int(get('routing.max_calls')), total_budget_s=float(get('routing.total_budget_s')),
                   detour_strategy=str(get('routing.detour_strategy')),
                   seed_retries=int(get('routing.seed_retries')), coverage_bbox=bbox)


@dataclass
class Generation:
    """Une trace générée : GPX prêt à parser, trace, et méta (`route_meta`, RB-4.9). Aucune coordonnée dans les journaux."""

    gpx: bytes
    points: list
    meta: dict
    warnings: List[dict] = field(default_factory=list)
    name: str = ''


def _warning(code: str, message: str) -> dict:
    return {'code': code, 'message': message}


# -- Validation ------------------------------------------------------------------------------------------------------------

def _validated_points(raw_points, config: BuildConfig):
    if (isinstance(raw_points, (str, bytes)) or not hasattr(raw_points, '__len__')
            or not 1 <= len(raw_points) <= config.max_waypoints):
        raise InvalidBuild(f"Entre 1 et {config.max_waypoints} points attendus")
    try:
        return [validate_point(p) for p in raw_points]
    except InvalidRequest:
        raise InvalidBuild("Coordonnées invalides") from None


def _validated_target(distance_km, config: BuildConfig) -> Optional[float]:
    if distance_km is None:
        return None
    if (isinstance(distance_km, bool) or not isinstance(distance_km, (int, float)) or not math.isfinite(distance_km)
            or not MIN_DISTANCE_KM <= distance_km <= config.max_distance_km):
        raise InvalidBuild(f"Distance : de {MIN_DISTANCE_KM:g} à {config.max_distance_km:g} km")
    return float(distance_km) * 1000.0


def _deduplicated(points: list):
    """Retire les doublons consécutifs (< 5 m). Retourne (points, numéros d'origine 1-based)."""
    kept, numbers = [points[0]], [1]
    for number, point in enumerate(points[1:], start=2):
        if quality_distance(kept[-1], point) >= DUPLICATE_M:
            kept.append(point)
            numbers.append(number)
    return kept, numbers


def quality_distance(a, b) -> float:
    return float(geo.haversine(a[0], a[1], b[0], b[1]))


def _technical_bbox(points: Sequence, margin_m: float, limit: Optional[tuple]) -> tuple:
    """RB-S7 : emprise des points de l'utilisateur élargie de T/4, rabattue dans la couverture configurée."""
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    dlat = margin_m / detours.M_PER_DEG
    dlon = margin_m / (detours.M_PER_DEG * max(math.cos(math.radians(sum(lats) / len(lats))), 1e-6))
    box = (max(-90.0, min(lats) - dlat), max(-180.0, min(lons) - dlon),
           min(90.0, max(lats) + dlat), min(180.0, max(lons) + dlon))
    if limit:
        box = (max(box[0], limit[0]), max(box[1], limit[1]), min(box[2], limit[2]), min(box[3], limit[3]))
    return box


# -- Génération ------------------------------------------------------------------------------------------------------------

@dataclass
class Prepared:
    """Entrée VALIDÉE d'une génération (aucun appel au moteur n'a eu lieu) : points conservés et numérotation d'origine."""

    entry: object
    target_m: Optional[float]
    kept: list
    numbers: list
    warnings: List[dict]
    seed: int


def prepare(raw_points, activity, distance_km, seed, config: BuildConfig) -> Prepared:
    """Validation stricte (RB-S1, RB-S1b) : lève `InvalidBuild` (400). Les routes comptent une génération APRÈS cette étape."""
    # 1. validation, AUCUN appel sortant avant la fin
    entry = profiles.get(activity)
    if entry is None:
        raise InvalidBuild("Activité inconnue")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise InvalidBuild("Graine invalide")
    points = _validated_points(raw_points, config)
    target_m = _validated_target(distance_km, config)
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    diagonal = float(geo.haversine(min(lats), min(lons), max(lats), max(lons)))
    if diagonal > config.max_distance_km * 1000.0:
        raise InvalidBuild("Points trop éloignés les uns des autres")
    if config.coverage_bbox is not None:
        lat_min, lon_min, lat_max, lon_max = config.coverage_bbox
        if not all(lat_min <= p[0] <= lat_max and lon_min <= p[1] <= lon_max for p in points):
            raise InvalidBuild("Un point est hors de la zone couverte")
    kept, numbers = _deduplicated(points)
    warnings: List[dict] = []
    if len(kept) > 1 and quality_distance(kept[-1], kept[0]) < DUPLICATE_M:
        numbers_dropped = numbers.pop()
        kept.pop()
        warnings.append(_warning('closing_point_removed', f"Le point {numbers_dropped} est confondu avec le point 1 : retiré."))
    if len(kept) == 1 and target_m is None:
        raise InvalidBuild("Distance requise avec un seul point")
    if len(kept) < len(points):
        logger.info("Génération : %d points réduits à %d", len(points), len(kept))
    return Prepared(entry, target_m, kept, numbers, warnings, seed)


def generate(provider: RoutingProvider, raw_points, activity, distance_km, seed, config: BuildConfig, *,
             clock: Callable = time.monotonic, today: Optional[datetime] = None) -> Generation:
    """
    Génère une boucle fermée. Lève `InvalidBuild` (400, avant tout appel), `UnreachablePoint` (422 avec numéro d'origine),
    ou l'erreur du moteur quand aucune trace n'a pu être obtenue.
    """
    return execute(provider, prepare(raw_points, activity, distance_km, seed, config), config, clock=clock, today=today)


def execute(provider: RoutingProvider, prepared: Prepared, config: BuildConfig, *, clock: Callable = time.monotonic,
            today: Optional[datetime] = None) -> Generation:
    """Calcule la trace d'une entrée validée : boucle de base, détours, essais de graine, contrôles, GPX."""
    entry, target_m, kept, numbers, warnings, seed = (prepared.entry, prepared.target_m, prepared.kept, prepared.numbers,
                                                      list(prepared.warnings), prepared.seed)
    budget = detours.Budget(config.max_calls, config.total_budget_s, clock)
    notes: list = []                      # avertissements du moteur (RB-4.9)
    tolerance = config.tolerance_pct / 100.0
    meta = {'profile': entry.profile, 'activity': entry.key, 'points': len(kept), 'point_numbers': list(numbers),
            'target_m': target_m, 'seed': seed}
    if len(kept) == 1:
        trace, elevations, tried, last_seed = _single_point(provider, kept[0], entry.profile, target_m, tolerance, seed,
                                                            budget, config, meta, notes, numbers[0])
        meta.update(d0_m=None, detours=0, strategy='round_trip')
    else:
        trace, elevations, tried, last_seed = _loop(provider, kept, numbers, entry.profile, target_m, tolerance, seed, budget,
                                                    config, meta, warnings, notes)

    for note in dict.fromkeys(notes):
        warnings.append(_warning('engine', note))
    trace = quality.decimate(trace, MAX_FILE_POINTS)  # borne des fichiers (TR2-2)
    distance = quality.length_m(trace)
    overlap = quality.overlap_rate(trace)
    threshold = config.overlap_warning_pct_2pts if len(kept) == 2 else config.overlap_warning_pct
    if overlap * 100.0 > threshold:
        warnings.append(_warning('overlap', "La trace emprunte plusieurs fois les mêmes voies."))
    gap = quality.target_gap(distance, target_m)
    if target_m and abs(gap) > tolerance and not any(w['code'] == 'min_distance' for w in warnings):
        text = f"Distance obtenue : {distance / 1000:.1f} km pour {target_m / 1000:.1f} km voulus ({gap * 100:+.0f} %)."
        warnings.append(_warning('target_gap', text))
        if len(kept) == 2:                                      # RB-4.14 : suggestion distincte, à code stable
            warnings.append(_warning('add_waypoint',
                                     "Ajoute un point de passage pour une boucle plus proche de la distance voulue."))
    meta.update(distance_m=distance, gap_pct=None if gap is None else round(gap * 100, 1),
                gap_m=None if not target_m else distance - target_m, overlap_pct=round(overlap * 100, 1),
                closure_m=quality.closure_m(trace), calls=budget.calls, attempts=tried, last_seed=last_seed,
                warnings=[w['message'] for w in warnings])
    logger.info("Génération : profil=%s points=%d distance=%.0f appels=%d essais=%d durée=%.1fs", entry.profile, len(kept),
                distance, budget.calls, tried, clock() - budget.started)
    name = default_name(today)
    return Generation(gpx=build_gpx(trace, kept, entry.stored_type, elevations, today), points=trace, meta=meta,
                      warnings=warnings, name=name)


def _single_point(provider, start, profile, target_m, tolerance, seed, budget, config, meta, notes, number=1):
    """RB-4.1 : boucle native ; hors tolérance ⇒ graine suivante dans la limite des essais et du budget."""
    best, tried, last_seed = None, 0, seed
    for attempt in range(1 + config.seed_retries):
        if attempt and not _enough_left(budget):
            break
        last_seed = seed + attempt
        try:
            result = budget.call(provider.round_trip, start, profile, target_m, last_seed)
        except (detours.BudgetExhausted, RoutingTimeout):
            break
        except NoRouteFound:
            if best is None:
                raise UnreachablePoint(number, 0) from None         # le seul point de l'utilisateur : son numéro d'origine
            break
        except RoutingError:
            if best is None:
                raise
            break
        tried += 1
        notes.extend(result.warnings)
        trace = list(result.points)
        candidate = (abs(quality.length_m(trace) - target_m), quality.overlap_rate(trace), trace, result.elevations_m)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
        if candidate[0] <= tolerance * target_m:
            break
    if best is None:
        raise RoutingTimeout()
    return best[2], best[3], tried, last_seed


def _enough_left(budget) -> bool:
    return budget.calls_left() >= MIN_SEED_CALLS and budget.time_left() >= MIN_SEED_SECONDS


def _loop(provider, kept, numbers, profile, target_m, tolerance, seed, budget, config, meta, warnings, notes):
    """RB-4.2 à RB-4.4 : boucle de base, comparaison à la cible, détours et essais de graine."""
    sequence = list(kept) + [kept[0]]
    try:
        base = budget.call(provider.route, sequence, profile)
    except NoRouteFound as exc:
        index = exc.waypoint_index
        if index is None or not 0 <= index <= len(kept):
            raise NoRouteFound() from None              # index absent ou hors de la liste envoyée : aucun point n'est accusé
        raise UnreachablePoint(numbers[index % len(kept)], index) from None
    base_points = list(base.points)
    if quality.closure_m(base_points) > config.snap_tolerance_m:
        logger.warning("Boucle de base non refermée par le moteur")
        raise InvalidResponse()                      # une « boucle » qui ne revient pas au départ n'en est pas une (CA-R4)
    snapped, gaps = quality.snap_to_trace(base_points, kept)
    for gap_m, number in zip(gaps, numbers):
        if gap_m > config.snap_tolerance_m:
            raise UnreachablePoint(number)
    d0 = quality.length_m(base_points)
    meta.update(d0_m=d0, detours=0, strategy='base')
    notes.extend(base.warnings)
    if not target_m or abs(d0 - target_m) <= tolerance * target_m:
        return base_points, base.elevations_m, 1, seed
    if d0 > (1 + tolerance) * target_m:
        warnings.append(_warning('min_distance', f"Distance minimale passant par tes points : {d0 / 1000:.1f} km."))
        return base_points, base.elevations_m, 1, seed

    strategy = detours.get_strategy(config.detour_strategy)
    bbox = _technical_bbox(kept, target_m / 4.0, config.coverage_bbox)

    def accepts(candidate) -> bool:
        return (quality.closure_m(candidate) <= config.snap_tolerance_m
                and quality.ordered_pass(candidate, snapped, config.snap_tolerance_m))

    best, best_key, tried, last_seed = None, None, 0, seed
    for attempt in range(1 + config.seed_retries):
        if attempt and not _enough_left(budget):
            break
        last_seed = seed + attempt
        ctx = detours.DetourContext(provider=provider, profile=profile, points=kept, base_points=base_points, base_distance_m=d0,
                                    target_m=target_m, tolerance=tolerance, seed=last_seed, budget=budget, bbox=bbox,
                                    accepts=accepts)
        outcome = strategy.run(ctx)
        tried += 1
        key = (abs(outcome.distance_m - target_m), quality.overlap_rate(outcome.points))
        if best is None or key < best_key:
            best, best_key = outcome, key
        if ctx.in_tolerance(outcome.distance_m):
            break
    meta.update(detours=best.detours, strategy=strategy.name, iterations=best.iterations)
    notes.extend(best.engine_warnings)
    for code in dict.fromkeys(best.warnings):
        if code == 'user_point_unreachable':
            warnings.append(_warning(code, "Un point n'a pas pu être approché pour allonger la trace."))
    return best.points, best.elevations, tried, last_seed


# -- GPX ---------------------------------------------------------------------------------------------------------------------

def default_name(today: Optional[datetime] = None) -> str:
    return f"Trace planifiée du {(today or datetime.now()).strftime('%d/%m/%Y')}"


def rename_gpx(gpx: bytes, name: str) -> bytes:
    """Remplace `<trk><name>` d'un GPX produit par `build_gpx` : API XML (échappement automatique), jamais une concaténation."""
    ET.register_namespace('', GPX_NS)
    root = ET.fromstring(gpx)
    node = root.find(f'{{{GPX_NS}}}trk/{{{GPX_NS}}}name')
    node.text = name
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def build_gpx(trace: Sequence, user_points: Sequence, stored_type: str, elevations: Optional[Sequence] = None,
              today: Optional[datetime] = None, name: Optional[str] = None) -> bytes:
    """
    GPX 1.1 par l'API XML (échappement automatique, jamais de concaténation) : P1…Pn en `<wpt>` (jamais les points
    techniques), un `<trk>` / un `<trkseg>`, `<type>` = type stocké, aucun horodatage (RB-4.8).
    """
    ET.register_namespace('', GPX_NS)
    root = ET.Element(f'{{{GPX_NS}}}gpx', {'version': '1.1', 'creator': 'kmlViewer'})
    for number, (lat, lon) in enumerate(user_points, start=1):
        waypoint = ET.SubElement(root, f'{{{GPX_NS}}}wpt', {'lat': f'{lat:.6f}', 'lon': f'{lon:.6f}'})
        ET.SubElement(waypoint, f'{{{GPX_NS}}}name').text = f'P{number}'
    track = ET.SubElement(root, f'{{{GPX_NS}}}trk')
    ET.SubElement(track, f'{{{GPX_NS}}}name').text = name or default_name(today)
    ET.SubElement(track, f'{{{GPX_NS}}}type').text = stored_type
    segment = ET.SubElement(track, f'{{{GPX_NS}}}trkseg')
    with_elevation = elevations is not None and len(elevations) == len(trace)
    for i, (lat, lon) in enumerate(trace):
        point = ET.SubElement(segment, f'{{{GPX_NS}}}trkpt', {'lat': f'{lat:.6f}', 'lon': f'{lon:.6f}'})
        if with_elevation:
            ET.SubElement(point, f'{{{GPX_NS}}}ele').text = f'{elevations[i]:.1f}'
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
