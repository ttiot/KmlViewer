"""
Ordonnanceur de fond (admin-space §5.10, R3) : un thread, des tâches à échéances propres.

  - Démarre TOUJOURS dans les modes servis (gunicorn, `flask run`), quels que soient INBOX_ENABLED et DEM_URL ;
    jamais en CLI ni en TESTING (app/__init__.py). Aucun réglage n'exige donc de redémarrage : intervalles et
    conditions d'activation sont relus à CHAQUE tour (EF-6.4).
  - Deux catégories de tâches (v5-1) :
      * PARTAGÉES (`inbox`, `dem_backfill`, `audit_purge`) : ressources communes à tous les processus (dossier,
        base) ; protégées par un verrou fichier (`flock` non bloquant). Sans le verrou, le processus ne les
        exécute pas mais le RETENTE à chaque tour : il prend le relais si l'autre s'arrête (CA-48).
      * DE PROCESSUS (`settings_reload`, `routing_health`) : état en mémoire du processus ; exécutées partout.
  - Tâches LONGUES (`inbox`, `dem_backfill`) dans un thread de travail dédié, une à la fois, jamais empilées :
    une passe DEM de 10 minutes ne fige ni le sondage de santé ni la relecture des réglages (v5-6, CA-51).
  - `wake(tâche)` rend une tâche due au tour suivant (v5-5) : un import qui diffère la correction d'altitude
    appelle wake('dem_backfill').
  - Testable sans thread : `tick(now)` avec horloge injectée et exécuteur injectable, `run_task(nom)` (EF-10.4).
  - Une tâche qui lève une exception est journalisée et replanifiée à son intervalle ; les autres s'exécutent.
"""

import fcntl
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app import audit
from app.db import connect
from app.services import dem_service, inbox_watcher, ingest_service
from app.services.routing import coverage_state
from app.settings import STORE_KEY, effective

logger = logging.getLogger(__name__)

TICK_SECONDS = 5          # un tour toutes les 5 s au plus (EF-10.3)
AUDIT_PURGE_INTERVAL = 24 * 3600
LOCK_NAME = 'inbox.lock'  # nom historique du verrou fichier, conservé


@dataclass
class Task:
    """
    Une tâche. `interval(cfg)` et `enabled(cfg)` sont réévalués à chaque tour (réglages à chaud). `run(db, cfg)` est
    appelable seule. `shared` : protégée par le verrou. `long` : exécutée par le thread de travail.
    """
    name: str
    run: Callable
    interval: Callable
    enabled: Callable = lambda cfg: True
    shared: bool = False
    long: bool = False


@dataclass
class TaskState:
    last_run: Optional[float] = None       # None : jamais exécutée ou réveillée => due
    running: bool = False
    rerun: bool = False                    # réveillée pendant son exécution
    last_error: Optional[str] = None
    runs: int = 0


class SyncExecutor:
    """Exécuteur des tâches longues, synchrone : tests et appels sans thread."""

    def submit(self, fn) -> None:
        fn()


class ThreadExecutor:
    """Un thread de travail dédié : les tâches longues s'exécutent une à la fois, dans l'ordre de soumission."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name='background-worker', daemon=True)
        self._thread.start()

    def submit(self, fn) -> None:
        self._queue.put(fn)

    def _loop(self) -> None:
        while True:
            fn = self._queue.get()
            if fn is None:
                return
            try:
                fn()
            except Exception:  # noqa: BLE001 - le thread de travail survit à tout (fn journalise déjà)
                logger.exception("Tâche longue : erreur inattendue")

    def stop(self, timeout: float = 30.0) -> bool:
        """Arrête le thread de travail APRÈS la tâche en cours. Retourne vrai s'il s'est arrêté dans le délai."""
        self._queue.put(None)
        self._thread.join(timeout)
        return not self._thread.is_alive()


class FileLock:
    """Verrou fichier non bloquant, retenté à volonté : un seul processus exécute les tâches partagées."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def try_acquire(self) -> bool:
        if self._handle is not None:
            return True
        try:
            handle = open(self.path, 'w')  # noqa: SIM115 - gardé ouvert tant que le verrou est détenu
        except OSError as exc:
            logger.error("Verrou %s illisible : %s", self.path, exc)
            return False
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()           # ferme => libère le flock
            self._handle = None


def default_tasks(wall_clock: Callable = lambda: datetime.now(timezone.utc)) -> List[Task]:
    """Tâches du produit. `routing_health` est ajoutée par le module de routage (TR5) via Scheduler.register()."""
    def inbox_run(db, cfg):
        root = inbox_watcher.inbox_dir(cfg)
        root.mkdir(parents=True, exist_ok=True)
        return inbox_watcher.scan_once(db, cfg, inbox_warned)

    inbox_warned: set = set()

    def purge_run(db, cfg):
        return audit.purge(db, effective(cfg, 'audit.retention_days'), now=wall_clock())

    def dem_run(db, cfg):
        if not dem_service.is_enabled(cfg):
            dem_service.audit_refusal(db, cfg)      # configurée mais hors OUTBOUND_ALLOWED_HOSTS : refus audité (dédoublonné)
            return 0
        if cfg.get('ROUTING_ZONE_BUILD'):
            requeued = coverage_state.consume_dem_wakeup(db)    # nouvelles dalles : les activités hors couverture sont retentées
            if requeued:
                logger.info("Couverture MNT étendue : %d activité(s) remise(s) en file", requeued)
        return ingest_service.backfill_elevation(db, cfg)

    def reload_run(db, cfg):
        store = cfg.get(STORE_KEY)
        if store is not None:
            store.refresh(db)

    return [
        Task('settings_reload', reload_run, interval=lambda cfg: 0),
        Task('audit_purge', purge_run, interval=lambda cfg: AUDIT_PURGE_INTERVAL, shared=True),
        Task('inbox', inbox_run, interval=lambda cfg: effective(cfg, 'INBOX_SCAN_INTERVAL'),
             enabled=lambda cfg: bool(effective(cfg, 'INBOX_ENABLED')), shared=True, long=True),
        Task('dem_backfill', dem_run, interval=lambda cfg: effective(cfg, 'DEM_BACKFILL_INTERVAL'),
             enabled=lambda cfg: bool(dem_service.dem_url(cfg)), shared=True, long=True),
    ]


class Scheduler:
    def __init__(self, cfg, tasks: Optional[List[Task]] = None, *, clock: Callable = time.monotonic,
                 wall_clock: Callable = lambda: datetime.now(timezone.utc), executor=None, lock=None):
        self.cfg = cfg
        self.clock = clock
        self.tasks: Dict[str, Task] = {t.name: t for t in (tasks if tasks is not None else default_tasks(wall_clock))}
        self.state: Dict[str, TaskState] = {name: TaskState() for name in self.tasks}
        self.executor = executor if executor is not None else SyncExecutor()
        self._own_executor = executor is None
        self.lock = lock if lock is not None else FileLock(Path(cfg['DATA_DIR']) / LOCK_NAME)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._mutex = threading.Lock()

    # -- enregistrement et réveil ----------------------------------------------------------------
    def register(self, task: Task) -> None:
        with self._mutex:
            self.tasks[task.name] = task
            self.state.setdefault(task.name, TaskState())

    def wake(self, name: str) -> None:
        """Rend la tâche due au prochain tour et réveille l'ordonnanceur (v5-5)."""
        if name not in self.tasks:
            raise ValueError(f"Tâche inconnue : {name!r}")
        with self._mutex:
            state = self.state[name]
            state.last_run = None
            if state.running:
                state.rerun = True
        self._wake.set()

    # -- exécution ---------------------------------------------------------------------------------
    def run_task(self, name: str, db=None):
        """Exécute UNE tâche tout de suite, sans verrou ni échéance (tests, outils). Retourne son résultat."""
        if name not in self.tasks:
            raise ValueError(f"Tâche inconnue : {name!r}")
        own = db is None
        db = connect(self.cfg['DB_PATH']) if own else db
        try:
            return self.tasks[name].run(db, self.cfg)
        finally:
            if own:
                db.close()

    def _execute(self, task: Task, now: Optional[float] = None) -> None:
        """
        Corps d'une exécution : connexion propre, exception journalisée, état mis à jour. `now` : horloge du tour pour
        une tâche courte (l'échéance se compte depuis le début du tour) ; une tâche longue date sa fin avec l'horloge.
        """
        state = self.state[task.name]
        error = None
        try:
            db = connect(self.cfg['DB_PATH'])
            try:
                task.run(db, self.cfg)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 - une tâche en erreur n'affecte pas les autres
            error = exc.__class__.__name__
            logger.exception("Tâche de fond %s : erreur, replanifiée à son intervalle", task.name)
        with self._mutex:
            state.last_error = error
            state.runs += 1
            state.running = False
            state.last_run = None if state.rerun else (self.clock() if now is None else now)
            state.rerun = False

    def _due(self, task: Task, now: float) -> bool:
        state = self.state[task.name]
        if state.running:
            return False                    # jamais d'empilement
        if state.last_run is None:
            return True
        return now - state.last_run >= max(0, float(task.interval(self.cfg)))

    def tick(self, now: Optional[float] = None) -> List[str]:
        """
        Un tour : tente le verrou (tâches partagées), puis exécute les tâches courtes dues et soumet les longues à
        l'exécuteur. Retourne les noms lancés dans ce tour.
        """
        now = self.clock() if now is None else now
        has_lock = self.lock.try_acquire()          # retenté à chaque tour
        launched = []
        for task in list(self.tasks.values()):
            try:
                if not task.enabled(self.cfg) or (task.shared and not has_lock) or not self._due(task, now):
                    continue
            except Exception:  # noqa: BLE001 - un réglage illisible ne bloque pas les autres tâches
                logger.exception("Tâche %s : échéance ou activation illisible", task.name)
                continue
            state = self.state[task.name]
            if task.long:
                with self._mutex:
                    state.running = True
                self.executor.submit(lambda t=task: self._execute(t))
            else:
                with self._mutex:
                    state.running = True
                self._execute(task, now)
            launched.append(task.name)
        return launched

    def status(self) -> dict:
        """Pour le diagnostic : verrou détenu ou non, état de chaque tâche."""
        return {'lock_held': self.lock.held, 'tasks': {
            name: {'enabled': self._safe(lambda t=task: bool(t.enabled(self.cfg))), 'shared': task.shared, 'long': task.long,
                   'running': self.state[name].running, 'runs': self.state[name].runs,
                   'last_error': self.state[name].last_error}
            for name, task in self.tasks.items()}}

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    # -- thread ----------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        if self._own_executor and isinstance(self.executor, SyncExecutor):
            self.executor = ThreadExecutor()
        self._thread = threading.Thread(target=self._loop, name='scheduler', daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - l'ordonnanceur survit à tout
                logger.exception("Ordonnanceur : tour en erreur, nouvel essai au prochain")
            self._wake.wait(TICK_SECONDS)

    def stop(self, timeout: float = 30.0) -> bool:
        """
        Arrêt propre. Le verrou fichier n'est libéré que si AUCUNE tâche n'est plus en cours : libérer pendant qu'une
        tâche partagée tourne laisserait un autre processus la lancer en même temps (double exécution). Sinon il est
        gardé jusqu'à la sortie du processus (le système le libère alors). Retourne vrai si le verrou a été libéré.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        stop_executor = getattr(self.executor, 'stop', None)
        if stop_executor:
            stop_executor(timeout)
        with self._mutex:
            busy = [name for name, state in self.state.items() if state.running]
        if busy:
            logger.warning("Arrêt de l'ordonnanceur : tâche(s) %s encore en cours, verrou conservé", ', '.join(busy))
            return False
        self.lock.release()
        return True
