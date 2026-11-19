"""
Fichiers envoyés à l'API : lecture de la requête, parsing, budget de points par requête et
messages d'erreur présentables. Commun au visualiseur (routes.py), à l'analyse et à la
bibliothèque.
"""

import logging
import re
from typing import Any, Dict, List

from flask import request

from app.errors import ApiError
from app.services import file_formats
from app.services.cache_service import parse_gpx_cached, parse_kml_cached

logger = logging.getLogger(__name__)

MAX_FILES_PER_UPLOAD = 50
# Un parser ajoute le texte de l'exception après un deux-points collé (« Erreur de parsing
# XML: unclosed token… ») ; les messages rédigés pour l'utilisateur écrivent « mot : suite ».
_EXCEPTION_DETAIL = re.compile(r'(?<! ):.*', re.DOTALL)
MAX_ERROR_LENGTH = 200
UNREADABLE_FILE = 'Fichier illisible'


def uploaded_file(empty_error: str):
    """Fichier unique du champ 'file'. Lève ApiError s'il manque."""
    file = request.files.get('file')
    if file is None or not file.filename:
        raise ApiError(empty_error)
    return file


def uploaded_files() -> list:
    """Fichiers du champ 'files', en nombre borné. Lève ApiError s'il n'y en a aucun ou trop."""
    files = [f for f in request.files.getlist('files') if f and f.filename]
    if not files:
        raise ApiError('Aucun fichier fourni')
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise ApiError(f'{MAX_FILES_PER_UPLOAD} fichiers maximum par envoi')
    return files


def request_point_budget() -> int:
    """
    Points parsés qu'une même requête peut renvoyer, tous fichiers confondus : l'équivalent
    d'un fichier plein. Le plafond par fichier ne borne pas la requête : cinquante fichiers
    de quelques dizaines de ko chacun, tous au plafond, tenaient ensemble en mémoire.
    Un fichier plein coûte jusqu'à ~700 Mo au pic : avec deux traitements lourds simultanés
    (workload.HEAVY_SLOTS), ce budget tient dans la limite mémoire du conteneur (2 Go).
    """
    return file_formats.MAX_FILE_POINTS


def _vertices(feature: Any) -> int:
    """Sommets d'une trace ou d'un polygone ; 0 pour un repère."""
    if not isinstance(feature, dict):
        return 0
    tracks = feature.get('tracks')
    if isinstance(tracks, list):  # gx:MultiTrack
        return sum(_vertices(track) for track in tracks)
    coords = feature.get('coordinates')
    if not isinstance(coords, list) or not coords or not isinstance(coords[0], (list, tuple)):
        return 0
    if coords[0] and isinstance(coords[0][0], (list, tuple)):  # polygone : liste d'anneaux
        return sum(len(ring) for ring in coords if isinstance(ring, (list, tuple)))
    return len(coords)


def count_points(parsed: Dict[str, Any]) -> int:
    """
    Taille d'un résultat de parsing, dans l'unité du plafond par fichier : le plus grand du
    nombre de repères et du nombre de sommets (un GPX décrit chaque point des deux façons).
    """
    features = parsed.get('features')
    if not isinstance(features, list):
        return 0
    vertices = sum(_vertices(feature) for feature in features)
    return max(vertices, len(features) - sum(1 for feature in features if _vertices(feature)))


def public_error(message: Any, filename: str = '') -> str:
    """
    Message d'erreur présentable au client à partir de celui d'un parser ou de l'ingestion :
    le détail d'exception qu'ils y collent part dans les logs, pas dans la réponse.
    """
    text = str(message or '')
    shown = _EXCEPTION_DETAIL.sub('', text).strip()[:MAX_ERROR_LENGTH] or UNREADABLE_FILE
    if shown != text:
        logger.info("Fichier %r refusé, détail non transmis au client : %s", filename, text[:500])
    return shown


def parse_upload(file) -> Dict[str, Any]:
    """
    Parse un fichier envoyé (GPX, KML ou KMZ). Le résultat sort d'un cache partagé : ne pas le
    modifier. Lève ApiError si le fichier est refusé ou illisible.
    """
    try:
        content, fmt = file_formats.decode(file.read(), file.filename)
        parsed = parse_gpx_cached(content) if fmt == 'gpx' else parse_kml_cached(content)
    except file_formats.FileFormatError as exc:
        raise ApiError(str(exc)) from exc  # message rédigé pour l'utilisateur (contrat de la classe)
    if not parsed.get('success'):
        raise ApiError(public_error(parsed.get('error'), file.filename))
    return parsed


def parse_uploads(files: list) -> List[Dict[str, Any]]:
    """
    Parse plusieurs fichiers ; un fichier refusé n'empêche pas les autres. Une fois le budget
    de points de la requête consommé, les fichiers restants sont refusés sans être conservés.
    """
    budget = request_point_budget()
    results = []
    for file in files:
        try:
            parsed = parse_upload(file)
            budget -= count_points(parsed)
            if budget < 0:
                raise ApiError('Envoi trop volumineux : charge ce fichier séparément')
            # Copie superficielle : le résultat sort d'un cache partagé, on n'y écrit pas
            result = {**parsed, 'filename': file.filename}
        except ApiError as exc:
            result = {'filename': file.filename, 'success': False, 'error': exc.message}
        results.append(result)
    return results
