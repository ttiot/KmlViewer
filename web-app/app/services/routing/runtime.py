"""
Exécution du constructeur de traces côté application (gpx-route-builder TR5 : RB-1.1, RB-1.2, RB-S3, RB-S5, P-TR5-3, 6, 9) :
fournisseur construit depuis les réglages, état de santé en cache (sondé par la tâche `routing_health`, jamais à la demande d'une
page), places de génération (une par utilisateur, `routing.max_concurrent` pour l'instance, refus IMMÉDIAT), résultats en mémoire.
Le module de routage proprement dit ne lit ni l'environnement ni la base : tout passe par ici.
"""

import logging
import threading
import time
from typing import Optional

from app import db as database
from app import outbound
from app.secrets_store import SecretsError
from app.services.routing.base import Health, RoutingError, RoutingUnavailable
from app.services.routing.builder import BuildConfig
from app.services.routing.graphhopper import GraphHopperProvider
from app.services.routing import coverage_state, zone
from app.services.routing.results import ResultStore
from app.settings import effective

logger = logging.getLogger(__name__)


class RoutingRuntime:
    def __init__(self, app, *, clock=time.monotonic, wall_clock=time.time):
        self.app = app
        self.results = ResultStore(clock=clock)
        self._clock, self._wall = clock, wall_clock
        self._lock = threading.Lock()
        self._health: Optional[dict] = None          # None : jamais sondé (démarrage) => « starting » (RB-1.2, n8)
        self._active_total = 0
        self._active_users: set = set()
        self.provider_factory = self._build_provider  # remplaçable en test (moteur factice)

    # -- configuration -----------------------------------------------------------------------------------------------
    @property
    def cfg(self):
        return self.app.config

    def destination(self) -> Optional[str]:
        url = effective(self.cfg, 'routing.base_url')
        if not url:
            return None
        try:
            return outbound.destination_of_url(url)
        except outbound.OutboundError:
            return None

    def configured(self) -> bool:
        """Moteur configuré ET destination autorisée par OUTBOUND_ALLOWED_HOSTS (RB-1.1, RB-S3)."""
        destination = self.destination()
        return destination is not None and outbound.allowed(self.cfg, 'routing', destination)

    def requested_zone(self) -> list:
        """Zone demandée : valeur effective du réglage (jamais vide : repli sur le défaut)."""
        return zone.effective_zone(lambda key: effective(self.cfg, key))

    def accepted_zone(self) -> list:
        """Zone qui accepte ou refuse les points : la zone ACTIVE avec `ROUTING_ZONE_BUILD=1`, sinon la zone demandée (ZD-3)."""
        requested = self.requested_zone()
        if not coverage_state.enabled(self.cfg):
            return requested
        conn = database.connect(self.cfg['DB_PATH'])
        try:
            return coverage_state.accepted_zone(conn, self.cfg, requested)
        finally:
            conn.close()

    def coverage_status(self) -> Optional[str]:
        """Statut unique de la couverture (ZD-3) ; None si le mode zone-pilote est désactivé."""
        if not coverage_state.enabled(self.cfg):
            return None
        conn = database.connect(self.cfg['DB_PATH'])
        try:
            return coverage_state.read(conn)['status']
        finally:
            conn.close()

    def build_config(self) -> BuildConfig:
        accepted = self.accepted_zone()
        return BuildConfig.from_settings(lambda key: accepted if key == 'routing.coverage_polygon' else effective(self.cfg, key))

    # -- fournisseur -----------------------------------------------------------------------------------------------------
    def _guard(self, destination: str) -> None:
        conn = database.connect(self.cfg['DB_PATH'])
        try:
            outbound.guard(conn, self.cfg, 'routing', destination)       # refus audité, vérifié avant CHAQUE appel (RB-S3)
        finally:
            conn.close()

    def _api_key(self, destination: str) -> Optional[str]:
        conn = database.connect(self.cfg['DB_PATH'])
        try:
            return self.app.extensions['secrets'].effective(conn, 'routing.api_key', destination)
        except SecretsError:
            raise RoutingUnavailable() from None                         # clé liée à une autre destination : « à ressaisir »
        finally:
            conn.close()

    def _build_provider(self):
        destination = self.destination()
        if destination is None or not self.configured():
            raise RoutingUnavailable()
        key = self._api_key(destination)
        try:
            # Une clé ne part en clair (http) que si l'exploitant l'a EXPLICITEMENT autorisé (ROUTING_ALLOW_CLEARTEXT_KEY, TR5-2)
            return GraphHopperProvider(effective(self.cfg, 'routing.base_url'), guard=self._guard,
                                       timeout_s=float(effective(self.cfg, 'routing.timeout_s')), api_key=key,
                                       allow_cleartext_key=bool(self.cfg.get('ROUTING_ALLOW_CLEARTEXT_KEY')))
        except ValueError:
            logger.warning("Moteur de routage : une clé API est configurée pour une destination http (en clair) : refusé")
            raise RoutingUnavailable() from None

    def provider(self):
        return self.provider_factory()

    # -- santé ---------------------------------------------------------------------------------------------------------------
    def run_health(self, db=None, cfg=None) -> dict:
        """Tâche `routing_health` : sonde le moteur (premier passage au démarrage, puis `routing.health_interval_s`)."""
        if not self.configured():
            with self._lock:
                self._health = None
            return {'configured': False}
        try:
            health = self.provider().health()
        except RoutingError:
            health = Health.down()
        except Exception:  # noqa: BLE001 - la sonde ne lève jamais
            logger.warning("Sonde du moteur de routage en échec")
            health = Health.down()
        with self._lock:
            self._health = {'available': health.available, 'checked_at': self._wall(), 'bbox': health.bbox,
                            'profiles': health.profiles}
        return {'configured': True, 'available': health.available}

    def state(self) -> str:
        """`ok`, `starting` (cache vide : traité comme injoignable, RB-1.2) ou `unavailable`."""
        with self._lock:
            health = self._health
        if self.coverage_status() == 'starting':          # graphe basculé, moteur en redémarrage (ZD-3), motif mensuel compris
            return 'starting'
        if health is None:
            return 'starting'
        return 'ok' if health['available'] else 'unavailable'

    def diagnostic(self) -> dict:
        """P-TR5-9 : pour l'onglet admin des fonctions bêta (RB-1.2, RB-1.3)."""
        with self._lock:
            health = self._health
        area = self.accepted_zone()                                           # la zone ACTIVE (inventaire n° 17)
        coverage = zone.envelope_lonlat(area)
        info = health['bbox'] if health else None
        contained = None
        if info and coverage:
            lon_min, lat_min, lon_max, lat_max = coverage                  # registre : [lon_min, lat_min, lon_max, lat_max]
            contained = info[0] <= lat_min and info[1] <= lon_min and info[2] >= lat_max and info[3] >= lon_max
        return {'configured': self.configured(), 'available': bool(health and health['available']),
                'checked_at': health['checked_at'] if health else None, 'info_bbox': list(info) if info else None,
                'coverage_contained': contained, 'zone_build': coverage_state.enabled(self.cfg),
                'profiles': list(health['profiles']) if health else []}

    def neutral_test(self) -> dict:
        """« Tester » le moteur (EF-5.6, r4) : ok ou ko SEULEMENT, rendu à l'échéance `routing.timeout_s`."""
        def probe(_timeout):
            if not self.configured():
                return False
            return bool(self.provider().health().available)             # garde de destination appliquée par le fournisseur
        return outbound.run_neutral_test(probe, float(effective(self.cfg, 'routing.timeout_s')))

    # -- concurrence (P-TR5-6) -----------------------------------------------------------------------------------------------
    def acquire(self, user_id: int) -> Optional[str]:
        """Prend les places d'une génération ; None si c'est fait, sinon la raison du refus immédiat (`user` | `instance`)."""
        with self._lock:
            if user_id in self._active_users:
                return 'user'
            if self._active_total >= int(effective(self.cfg, 'routing.max_concurrent')):
                return 'instance'
            self._active_users.add(user_id)
            self._active_total += 1
        return None

    def release(self, user_id: int) -> None:
        with self._lock:
            if user_id in self._active_users:
                self._active_users.discard(user_id)
                self._active_total = max(0, self._active_total - 1)


def init_routing(app) -> RoutingRuntime:
    """Crée le runtime, enregistre la tâche `routing_health` dans l'ordonnanceur et le diagnostic des fonctions bêta."""
    from app.background import Task
    runtime = RoutingRuntime(app)
    app.extensions['routing'] = runtime
    scheduler = app.extensions.get('scheduler')
    if scheduler is not None:
        scheduler.register(Task('routing_health', runtime.run_health,
                                interval=lambda cfg: effective(cfg, 'routing.health_interval_s'),
                                enabled=lambda cfg: runtime.configured(), shared=False, long=False))   # E-5 : COURTE (CA-51)
    app.extensions['routing_test'] = runtime.neutral_test            # onglet « Clés API » de l'admin (EF-5.6)
    app.extensions.setdefault('feature_diagnostics', {})['route_builder'] = runtime.diagnostic
    return runtime
