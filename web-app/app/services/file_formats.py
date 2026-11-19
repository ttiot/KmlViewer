"""
Décodage des fichiers de traces : GPX, KML et KMZ (archive zip contenant un KML).
Indépendant de Flask.
"""

import io
import zipfile
from pathlib import PurePath
from typing import Tuple

SUPPORTED_EXTENSIONS = ('gpx', 'kml', 'kmz')
# Taille maximale du KML une fois décompressé (protège des bombes de décompression) :
# la même que celle d'un fichier envoyé tel quel
MAX_KMZ_UNCOMPRESSED = 16 * 1024 * 1024
# Points acceptés par fichier (repères et sommets de traces confondus). Chaque point coûte
# de l'ordre du kilo-octet une fois parsé : sans plafond, quelques dizaines de ko très
# compressibles suffisent à saturer la mémoire du serveur. 200 000 points = 55 h à 1 Hz.
MAX_FILE_POINTS = 200_000


class FileFormatError(ValueError):
    """Fichier refusé ; le message est affichable tel quel à l'utilisateur."""


class TooManyPointsError(FileFormatError):
    def __init__(self):
        super().__init__(f"Fichier trop volumineux (plus de {MAX_FILE_POINTS} points)")


def extension(filename: str) -> str:
    return PurePath(filename or '').suffix.lower().lstrip('.')


def _kml_from_kmz(raw: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            candidates = [i for i in archive.infolist()
                          if not i.is_dir() and i.filename.lower().endswith('.kml')]
            if not candidates:
                raise FileFormatError("Archive KMZ sans fichier KML")
            # doc.kml à la racine est le document principal ; à défaut, le moins profond
            candidates.sort(key=lambda i: (i.filename.lower() != 'doc.kml', i.filename.count('/'), i.filename))
            info = candidates[0]
            if info.file_size > MAX_KMZ_UNCOMPRESSED:
                raise FileFormatError("Archive KMZ trop volumineuse une fois décompressée")
            with archive.open(info) as handle:
                # Lecture bornée : la taille annoncée dans l'en-tête peut mentir
                data = handle.read(MAX_KMZ_UNCOMPRESSED + 1)
    except FileFormatError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError, OSError, EOFError) as exc:
        raise FileFormatError("Archive KMZ illisible") from exc
    if len(data) > MAX_KMZ_UNCOMPRESSED:
        raise FileFormatError("Archive KMZ trop volumineuse une fois décompressée")
    return data


def decode(raw: bytes, filename: str) -> Tuple[str, str]:
    """
    Retourne (contenu texte, format) avec format dans {'gpx', 'kml'}.
    Lève FileFormatError si l'extension, l'archive ou l'encodage sont invalides.
    """
    ext = extension(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise FileFormatError("Seuls les fichiers .gpx, .kml et .kmz sont pris en charge")
    if not raw:
        raise FileFormatError("Fichier vide")
    if ext == 'kmz':
        raw, ext = _kml_from_kmz(raw), 'kml'
    try:
        return raw.decode('utf-8-sig'), ext
    except UnicodeDecodeError as exc:
        raise FileFormatError("Encodage invalide (UTF-8 attendu)") from exc
