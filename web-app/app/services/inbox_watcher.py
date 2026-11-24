"""
Dossier surveillé : chaque GPX/KML déposé est ingéré automatiquement.

Arborescence :
    <INBOX_DIR>/<username ou email>/…   -> bibliothèque de cet utilisateur
    <INBOX_DIR>/*.gpx                   -> INBOX_DEFAULT_USER (ou le compte local sans OIDC)

Les fichiers ne sont ni déplacés ni supprimés (le volume peut être monté en lecture
seule, et ça ne perturbe pas un outil de synchro type Syncthing) : le suivi se fait
dans la table inbox_files. Scrutation par polling, fiable sur bind mounts et NFS.
"""

import logging
import os
import sqlite3
import stat as stat_module
import time
from pathlib import Path
from typing import Dict, Iterator, Mapping, Optional, Set, Tuple

from app.auth import modes, users
from app.services import file_formats, ingest_service

logger = logging.getLogger(__name__)

STABLE_AGE_S = 5  # un fichier modifié il y a moins de 5 s est peut-être encore en cours d'écriture
MAX_DEPTH = 8

def inbox_dir(cfg: Mapping) -> Path:
    return Path(cfg.get('INBOX_DIR') or Path(cfg['DATA_DIR']) / 'inbox')


def _iter_files(root: Path) -> Iterator[Tuple[Optional[str], Path]]:
    """(dossier utilisateur ou None, fichier) pour chaque candidat ; liens symboliques ignorés."""
    def walk(directory: Path, owner: Optional[str], depth: int):
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda e: e.name)
        except OSError as exc:
            logger.warning("Lecture de %s impossible : %s", directory, exc)
            return
        for entry in entries:
            try:
                if entry.name.startswith('.') or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        yield from walk(Path(entry.path), owner or entry.name, depth + 1)
                elif entry.is_file(follow_symlinks=False) and \
                        file_formats.extension(entry.name) in ingest_service.INGEST_EXTENSIONS:
                    yield owner, Path(entry.path)
            except OSError as exc:
                logger.warning("Inbox : %s illisible : %s", entry.path, exc)
    yield from walk(root, None, 0)


def _open_nofollow(root: Path, parts: Tuple[str, ...]) -> int:
    """
    Ouvre <root>/<parts…> en lecture sans suivre aucun lien symbolique, composant par
    composant à partir du descripteur de la racine. Le fichier lu est donc forcément sous
    la racine, même si un lien est glissé à la place d'un dossier ou du fichier entre le
    listage et la lecture (sinon : lecture du fichier d'un autre utilisateur). Lève OSError.
    """
    directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            deeper = os.open(part, directory, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = deeper
        # O_NONBLOCK : un tube nommé substitué au fichier ne doit pas bloquer la tâche de fond
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)


def _resolve_owner(db: sqlite3.Connection, cfg: Mapping, folder: Optional[str]) -> Optional[sqlite3.Row]:
    """
    Compte auquel rattacher un dossier. Avec OIDC, le nom du dossier (ou INBOX_DEFAULT_USER)
    doit désigner un seul compte, tous comptes confondus : username ou email, sans tenir
    compte de la casse, comptes inactifs compris. Dès qu'un second compte porte ce nom, à
    quelque titre que ce soit, le dossier n'est rattaché à personne : se donner le
    username ou l'email d'un autre ne fait donc pas recevoir ses fichiers.
    """
    if modes.auth_mode(cfg) == 'local':       # mode validé au démarrage ; absent => erreur, jamais de repli ouvert
        return users.get_or_create_local_user(db)
    login = folder or cfg.get('INBOX_DEFAULT_USER')
    if not login:
        return None
    rows = db.execute(
        "SELECT * FROM users WHERE sub != ? AND (lower(username) = lower(?) OR lower(email) = lower(?)) LIMIT 2",
        (users.LOCAL_SUB, login, login)).fetchall()
    if len(rows) > 1:
        logger.debug("Inbox : « %s » désigne plusieurs comptes, aucun n'est retenu", login)  # averti une fois par l'appelant
        return None
    return rows[0] if rows and rows[0]['is_active'] else None


def _warn_once(warned: Set[str], label: str, message: str, *args) -> None:
    """Un avertissement par sujet et par processus : le scan repasse toutes les 30 s."""
    if label not in warned:
        warned.add(label)
        logger.warning(message, *args)


def _process_file(db: sqlite3.Connection, cfg: Mapping, root: Path, folder: Optional[str], path: Path,
                  max_size: int, warned: Set[str]) -> Optional[str]:
    """
    Traite un fichier candidat. Retourne le statut à compter ('created', 'duplicate',
    'error', 'skipped'), ou None s'il n'y a rien à faire (déjà vu, encore en cours d'écriture).
    """
    relative = path.relative_to(root)
    key = str(relative)
    try:
        key.encode('utf-8')
    except UnicodeEncodeError:
        # Nom non UTF-8 (ancienne archive Windows…) : inutilisable comme clé, on passe au suivant
        _warn_once(warned, f"name:{key!r}", "Inbox : nom de fichier non UTF-8 ignoré (%r), renomme-le", key)
        return 'skipped'

    try:
        fd = _open_nofollow(root, relative.parts)
    except OSError as exc:
        _warn_once(warned, f"open:{key}", "Inbox : %s non lu (%s)", key, exc)
        return None
    with os.fdopen(fd, 'rb') as handle:
        # Taille, date et contenu viennent du même descripteur : c'est bien le fichier lu qui est contrôlé
        stat = os.fstat(handle.fileno())
        if not stat_module.S_ISREG(stat.st_mode):
            return None
        now = time.time()
        # abs() : une date de modification dans le futur (horloge décalée) ne bloque pas le fichier pour toujours
        if abs(now - stat.st_mtime) < STABLE_AGE_S:
            return None
        seen = db.execute("SELECT size, mtime FROM inbox_files WHERE path = ?", (key,)).fetchone()
        if seen is not None and seen['size'] == stat.st_size and abs(seen['mtime'] - stat.st_mtime) < 1e-3:
            return None

        owner = _resolve_owner(db, cfg, folder)
        if owner is None:
            # Pas d'enregistrement : le fichier sera repris dès que le compte existera
            label = folder or '(racine)'
            _warn_once(warned, label, "Inbox : aucun utilisateur actif et unique pour « %s », fichiers en attente", label)
            return 'skipped'

        if stat.st_size > max_size:
            result = ingest_service.IngestResult('error', message="Fichier trop volumineux")
        else:
            try:
                raw = handle.read(max_size + 1)  # borné même si le fichier grossit pendant la lecture
            except OSError as exc:
                result = ingest_service.IngestResult('error', message=f"Lecture impossible : {exc}", retryable=True)
            else:
                result = ingest_service.ingest_bytes(db, cfg, owner['id'], raw, path.name, 'inbox',
                                                     fallback_epoch=min(stat.st_mtime, now))

    if result.status == 'error' and result.retryable:
        # Quota, disque, base : rien à reprocher au fichier. Il n'est pas inscrit, donc
        # réessayé au prochain passage, sans intervention.
        _warn_once(warned, f"retry:{key}", "Inbox : %s en attente (%s), nouvel essai à chaque passage", key, result.message)
        return 'skipped'

    db.execute(
        "INSERT INTO inbox_files (path, size, mtime, status, error, activity_id) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, "
        "status = excluded.status, error = excluded.error, activity_id = excluded.activity_id, "
        "processed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')",
        (key, stat.st_size, stat.st_mtime, result.status,
         result.message if result.status == 'error' else None, result.activity_id))
    db.commit()
    warned.discard(f"retry:{key}")
    if result.status == 'error':
        logger.warning("Inbox : %s rejeté (%s)", key, result.message)
    else:
        logger.info("Inbox : %s -> %s (activité %s)", key, result.status, result.activity_id)
    return result.status


def scan_once(db: sqlite3.Connection, cfg: Mapping, warned: Optional[set] = None) -> Dict[str, int]:
    """Un passage sur le dossier surveillé. Retourne les compteurs par statut."""
    root = inbox_dir(cfg)
    counts = {'created': 0, 'duplicate': 0, 'error': 0, 'skipped': 0}
    if not root.is_dir():
        return counts
    warned = warned if warned is not None else set()
    max_size = cfg.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)

    for folder, path in _iter_files(root):
        # Un fichier, quel qu'il soit, n'arrête pas le passage : ceux des autres utilisateurs suivent
        try:
            status = _process_file(db, cfg, root, folder, path, max_size, warned)
        except Exception:  # noqa: BLE001 - voir ci-dessus
            label = f"crash:{path!r}"
            if label not in warned:
                warned.add(label)
                logger.exception("Inbox : erreur inattendue sur %r, fichier ignoré", str(path))
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            status = 'error'
        if status:
            counts[status] += 1
    return counts
