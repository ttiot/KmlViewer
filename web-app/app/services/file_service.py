"""
Fichiers d'exemple proposés dans le visualiseur.
"""

from pathlib import Path
from typing import Dict, List

from flask import current_app
from werkzeug.utils import secure_filename

from app.services import file_formats


class FileService:
    """Accès aux fichiers d'exemple (KML, KMZ, GPX)."""

    @staticmethod
    def _sample_dir() -> Path:
        """Dossier monté par Docker, sinon web-app/sample_files (développement local)."""
        sample_dir = Path(current_app.config['SAMPLE_FILES_DIR'])
        return sample_dir if sample_dir.is_dir() else Path(current_app.config['SAMPLE_FILES_FALLBACK'])

    @staticmethod
    def get_sample_files() -> List[Dict[str, str]]:
        """Liste les fichiers d'exemple disponibles."""
        directory = FileService._sample_dir()
        if not directory.is_dir():
            return []
        return [{'name': path.name, 'path': path.name}
                for path in sorted(directory.iterdir())
                if path.is_file() and file_formats.extension(path.name) in file_formats.SUPPORTED_EXTENSIONS]

    @staticmethod
    def load_sample_file(filename: str) -> bytes:
        """Contenu brut d'un fichier d'exemple. Lève FileNotFoundError s'il est absent ou refusé."""
        name = secure_filename(filename)
        if not name or file_formats.extension(name) not in file_formats.SUPPORTED_EXTENSIONS:
            raise FileNotFoundError('Fichier non trouvé ou type invalide')
        path = FileService._sample_dir() / name
        if not path.is_file():
            raise FileNotFoundError('Fichier non trouvé ou type invalide')
        return path.read_bytes()
