"""
Décodage des fichiers de traces : GPX, KML et KMZ (archive zip contenant un KML), et
garde-fous communs aux deux parsers (plafonds, nombres finis, espaces de noms).
Indépendant de Flask.
"""

import codecs
import io
import math
import re
import zipfile
import zlib
from pathlib import PurePath
from typing import Optional, Tuple

SUPPORTED_EXTENSIONS = ('gpx', 'kml', 'kmz')
# Taille maximale du KML une fois décompressé (protège des bombes de décompression) :
# la même que celle d'un fichier envoyé tel quel
MAX_KMZ_UNCOMPRESSED = 16 * 1024 * 1024
# Points acceptés par fichier (repères et sommets de traces confondus). Chaque point coûte
# de l'ordre du kilo-octet une fois parsé : sans plafond, quelques dizaines de ko très
# compressibles suffisent à saturer la mémoire du serveur. 200 000 points = 55 h à 1 Hz.
# Un élément sans point (Placemark vide, polygone sans contour) compte pour un : c'est
# donc aussi le plafond du nombre de Placemarks et de features d'un fichier.
MAX_FILE_POINTS = 200_000
# Au-delà, une altitude n'est pas une mesure ; bornée, une somme de dénivelés reste finie
MAX_ABS_ALTITUDE_M = 100_000.0
MAX_NAME_LENGTH = 200

# Encodages acceptés dans la déclaration XML (nom normalisé par codecs.lookup)
_ENCODINGS = frozenset({'utf-8', 'ascii', 'iso8859-1', 'iso8859-15', 'cp1252'})
_XML_ENCODING = re.compile(rb"""^<\?xml[^>]{0,200}?encoding\s{0,8}=\s{0,8}["']([A-Za-z0-9._-]{1,40})["']""")
_NUMBER = re.compile(r'[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')  # chiffres ASCII seulement


class FileFormatError(ValueError):
    """Fichier refusé ; le message est affichable tel quel à l'utilisateur."""


class TooManyPointsError(FileFormatError):
    def __init__(self):
        super().__init__(f"Fichier trop volumineux (plus de {MAX_FILE_POINTS} points)")


def extension(filename: str) -> str:
    """Extension en minuscules, sans le point. Seul endroit où elle se calcule."""
    return PurePath(filename or '').suffix.lower().lstrip('.')


def safe_name(filename: str, max_length: int = MAX_NAME_LENGTH) -> str:
    """
    Nom de fichier réduit à son dernier composant et à max_length caractères. C'est le
    radical qui est tronqué : l'extension, qui décide du format, est conservée.
    """
    name = PurePath(str(filename or '').replace('\\', '/')).name
    if len(name) <= max_length:
        return name or 'fichier'
    suffix = PurePath(name).suffix[:max_length - 1]
    return name[:max_length - len(suffix)] + suffix


def finite_float(value: Optional[str]) -> float:
    """
    Nombre décimal fini. Lève ValueError sinon : float() accepte « inf », « nan » et
    « 1_0 », qui donneraient un JSON invalide (Infinity) ou des totaux infinis en base.
    """
    text = (value or '').strip()
    if not _NUMBER.match(text):
        raise ValueError(f"Nombre invalide : {text[:20]!r}")
    number = float(text)
    if not math.isfinite(number):  # « 1e999 »
        raise ValueError(f"Nombre hors limites : {text[:20]!r}")
    return number


def altitude(value: Optional[str]) -> float:
    """Altitude en mètres ; absente ou vide : 0. Lève ValueError si elle n'est pas plausible."""
    if value is None or not value.strip():
        return 0.0
    number = finite_float(value)
    if abs(number) > MAX_ABS_ALTITUDE_M:
        raise ValueError("Altitude hors limites")
    return number


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
    except (zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, RuntimeError, NotImplementedError,
            OSError, EOFError, ValueError, OverflowError) as exc:
        # zlib.error : flux deflate corrompu, que zipfile laisse remonter tel quel
        raise FileFormatError("Archive KMZ illisible") from exc
    if len(data) > MAX_KMZ_UNCOMPRESSED:
        raise FileFormatError("Archive KMZ trop volumineuse une fois décompressée")
    return data


def _declared_encoding(raw: bytes) -> str:
    """
    Encodage annoncé par le fichier : marque d'ordre des octets, sinon déclaration XML,
    sinon UTF-8. Seule une courte liste est acceptée ; le texte décodé est ensuite passé
    à defusedxml, qui ignore alors la déclaration : aucune surface XML n'est ajoutée.
    """
    if raw.startswith(codecs.BOM_UTF8):
        return 'utf-8-sig'
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return 'utf-16'
    match = _XML_ENCODING.match(raw.lstrip()[:300])
    if not match:
        return 'utf-8'
    try:
        name = codecs.lookup(match.group(1).decode('ascii')).name
    except LookupError as exc:
        raise FileFormatError("Encodage non pris en charge") from exc
    if name not in _ENCODINGS:
        raise FileFormatError("Encodage non pris en charge")
    return name


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
    encoding = _declared_encoding(raw)
    try:
        return raw.decode(encoding), ext
    except UnicodeDecodeError as exc:
        raise FileFormatError(f"Encodage invalide ({'UTF-8' if encoding.startswith('utf-8') else encoding} attendu)") from exc
