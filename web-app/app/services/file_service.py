"""
Fichiers d'exemple proposés dans le visualiseur.
Indépendant de Flask : la configuration est un simple mapping, passé par l'appelant.
"""

import logging
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from werkzeug.utils import secure_filename

from app.services import file_formats

logger = logging.getLogger(__name__)


def _config(cfg: Optional[Mapping]) -> Mapping:
    """
    Configuration fournie par l'appelant. Sans elle (appelants historiques), celle de
    l'application Flask courante : import local, pour que le module se charge sans Flask.
    """
    if cfg is not None:
        return cfg
    from flask import current_app
    return current_app.config


class FileService:
    """Accès aux fichiers d'exemple (KML, KMZ, GPX)."""

    @staticmethod
    def _sample_dir(cfg: Optional[Mapping] = None) -> Path:
        """Dossier monté par Docker, sinon web-app/sample_files (développement local)."""
        cfg = _config(cfg)
        sample_dir = Path(cfg['SAMPLE_FILES_DIR'])
        return sample_dir if sample_dir.is_dir() else Path(cfg['SAMPLE_FILES_FALLBACK'])

    @staticmethod
    def get_sample_files(cfg: Optional[Mapping] = None) -> List[Dict[str, str]]:
        """Liste les fichiers d'exemple disponibles."""
        directory = FileService._sample_dir(cfg)
        if not directory.is_dir():
            return []
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            logger.warning("Dossier d'exemples %s illisible : %s", directory, exc)
            return []
        return [{'name': path.name, 'path': path.name}
                for path in entries
                if path.is_file() and file_formats.extension(path.name) in file_formats.SUPPORTED_EXTENSIONS]

    @staticmethod
    def load_sample_file(filename: str, cfg: Optional[Mapping] = None) -> bytes:
        """Contenu brut d'un fichier d'exemple. Lève FileNotFoundError s'il est absent ou refusé."""
        name = secure_filename(filename)
        if not name or file_formats.extension(name) not in file_formats.SUPPORTED_EXTENSIONS:
            raise FileNotFoundError('Fichier non trouvé ou type invalide')
        path = FileService._sample_dir(cfg) / name
        if not path.is_file():
            raise FileNotFoundError('Fichier non trouvé ou type invalide')
        try:
            return path.read_bytes()
        except OSError as exc:
            logger.warning("Fichier d'exemple %s illisible : %s", name, exc)
            raise FileNotFoundError('Fichier non trouvé ou type invalide') from exc
