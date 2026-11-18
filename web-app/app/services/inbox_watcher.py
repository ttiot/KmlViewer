"""
Dossier surveillé : chaque GPX/KML déposé est ingéré automatiquement.

Arborescence :
    <INBOX_DIR>/<username ou email>/…   -> bibliothèque de cet utilisateur
    <INBOX_DIR>/*.gpx                   -> INBOX_DEFAULT_USER (ou le compte local sans OIDC)

Les fichiers ne sont ni déplacés ni supprimés (le volume peut être monté en lecture
seule, et ça ne perturbe pas un outil de synchro type Syncthing) : le suivi se fait
dans la table inbox_files. Scrutation par polling, fiable sur bind mounts et NFS.
"""

import fcntl
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Tuple

from app.auth import users
from app.db import connect
from app.services import ingest_service

logger = logging.getLogger(__name__)

STABLE_AGE_S = 5  # un fichier modifié il y a moins de 5 s est peut-être encore en cours d'écriture

# Réveil anticipé de la tâche de fond (ex. après un import, pour corriger l'altitude sans attendre)
_wake_event = threading.Event()


def wake() -> None:
    _wake_event.set()


def inbox_dir(cfg: Mapping) -> Path:
    return Path(cfg.get('INBOX_DIR') or Path(cfg['DATA_DIR']) / 'inbox')


def _iter_files(root: Path) -> Iterator[Tuple[Optional[str], Path]]:
    """(dossier utilisateur ou None, fichier) pour chaque candidat ; liens symboliques ignorés."""
    def walk(directory: Path, owner: Optional[str], depth: int):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            logger.warning("Lecture de %s impossible : %s", directory, exc)
            return
        for entry in entries:
            if entry.name.startswith('.') or entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth < 8:
                    yield from walk(Path(entry.path), owner or entry.name, depth + 1)
            elif entry.is_file(follow_symlinks=False) and \
                    ingest_service.file_extension(entry.name) in ingest_service.INGEST_EXTENSIONS:
                yield owner, Path(entry.path)
    yield from walk(root, None, 0)


def _resolve_owner(db: sqlite3.Connection, cfg: Mapping, folder: Optional[str]) -> Optional[sqlite3.Row]:
    if not cfg.get('OIDC_ISSUER'):
        return users.get_or_create_local_user(db)
    login = folder or cfg.get('INBOX_DEFAULT_USER')
    return users.find_by_login(db, login) if login else None


def scan_once(db: sqlite3.Connection, cfg: Mapping, warned: Optional[set] = None) -> Dict[str, int]:
    """Un passage sur le dossier surveillé. Retourne les compteurs par statut."""
    root = inbox_dir(cfg)
    counts = {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 0}
    if not root.is_dir():
        return counts
    warned = warned if warned is not None else set()
    max_size = cfg.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)
    now = time.time()

    for folder, path in _iter_files(root):
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < STABLE_AGE_S:
            continue
        key = str(path.relative_to(root))
        seen = db.execute("SELECT size, mtime FROM inbox_files WHERE path = ?", (key,)).fetchone()
        if seen is not None and seen['size'] == stat.st_size and abs(seen['mtime'] - stat.st_mtime) < 1e-3:
            continue

        owner = _resolve_owner(db, cfg, folder)
        if owner is None:
            # Pas d'enregistrement : le fichier sera repris dès que le compte existera
            label = folder or '(racine)'
            if label not in warned:
                warned.add(label)
                logger.warning("Inbox : aucun utilisateur actif pour « %s », fichiers en attente", label)
            counts['skipped'] += 1
            continue

        if stat.st_size > max_size:
            result = ingest_service.IngestResult('error', message="Fichier trop volumineux")
        else:
            try:
                result = ingest_service.ingest_bytes(db, cfg, owner['id'], path.read_bytes(), path.name,
                                                     'inbox', fallback_epoch=stat.st_mtime)
            except OSError as exc:
                result = ingest_service.IngestResult('error', message=f"Lecture impossible : {exc}")
            except Exception as exc:  # noqa: BLE001 - un fichier pourri ne doit pas tuer le watcher
                logger.exception("Inbox : erreur inattendue sur %s", key)
                db.rollback()
                result = ingest_service.IngestResult('error', message=f"Erreur interne : {exc.__class__.__name__}")

        db.execute(
            "INSERT INTO inbox_files (path, size, mtime, status, error, activity_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, "
            "status = excluded.status, error = excluded.error, activity_id = excluded.activity_id, "
            "processed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
            (key, stat.st_size, stat.st_mtime, result.status,
             result.message if result.status == 'error' else None, result.activity_id))
        db.commit()
        counts[result.status] += 1
        if result.status == 'error':
            logger.warning("Inbox : %s rejeté (%s)", key, result.message)
        else:
            logger.info("Inbox : %s -> %s (activité %s)", key, result.status, result.activity_id)
    return counts


class InboxWatcher(threading.Thread):
    """
    Tâche de fond : scrutation du dossier surveillé (si INBOX_ENABLED) et rattrapage de la
    correction d'altitude. Un verrou fichier garantit un seul thread actif par DATA_DIR.
    """

    def __init__(self, cfg: Mapping):
        super().__init__(name='inbox-watcher', daemon=True)
        self.cfg = dict(cfg)
        self.interval = max(5, int(self.cfg.get('INBOX_SCAN_INTERVAL', 30)))
        self._stop_event = threading.Event()
        self._lock_file = None

    def _acquire_lock(self) -> bool:
        lock_path = Path(self.cfg['DATA_DIR']) / 'inbox.lock'
        self._lock_file = open(lock_path, 'w')  # noqa: SIM115 - gardé ouvert toute la vie du thread
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._lock_file.close()
            return False

    def stop(self) -> None:
        self._stop_event.set()
        wake()

    def run(self) -> None:
        if not self._acquire_lock():
            logger.info("Inbox : un autre processus surveille déjà le dossier, rien à faire ici")
            return
        scan_inbox = bool(self.cfg.get('INBOX_ENABLED'))
        if scan_inbox:
            root = inbox_dir(self.cfg)
            try:
                root.mkdir(parents=True, exist_ok=True)
                logger.info("Inbox : surveillance de %s toutes les %d s", root, self.interval)
            except OSError as exc:
                logger.error("Inbox : création de %s impossible (%s), surveillance désactivée", root, exc)
                scan_inbox = False
        warned: set = set()
        while not self._stop_event.is_set():
            _wake_event.clear()
            try:
                db = connect(self.cfg['DB_PATH'])
                try:
                    if scan_inbox:
                        scan_once(db, self.cfg, warned)
                    ingest_service.backfill_elevation(db, self.cfg)
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 - le watcher doit survivre à tout
                logger.exception("Tâche de fond : passage en erreur, nouvel essai au prochain cycle")
            _wake_event.wait(self.interval)
