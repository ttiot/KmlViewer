"""Cache mémoire des fichiers parsés, pour ne pas reparser un fichier renvoyé à l'identique."""

import hashlib
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

from .gpx_parser import GPXParser
from .kml_parser import KMLParser

# Cache LRU borné en nombre d'entrées ET en volume : borné seulement en nombre, seize gros
# fichiers distincts retiendraient plusieurs Go (déni de service par épuisement mémoire).
# Deux volumes sont comptés, et un résultat qui dépasse l'un ou l'autre n'est pas retenu :
# - la taille du fichier source ;
# - la mémoire que le résultat occupe réellement, estimée d'après ce qu'il contient. Elle
#   ne se déduit pas de la taille du fichier : un point de trace GPX de 24 octets donne
#   plus d'un kilo-octet d'objets (4 fichiers de 3,9 Mo retenaient 770 Mo).
MAX_ENTRIES = 16
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ENTRY_MEMORY = 48 * 1024 * 1024
MAX_TOTAL_MEMORY = 128 * 1024 * 1024
# Coûts mesurés (CPython 64 bits), arrondis au-dessus : un marqueur avec ses dictionnaires,
# un sommet [lat, lon, alt] et son horodatage, une feature et ses champs communs
MARKER_MEMORY = 1200
VERTEX_MEMORY = 250
FEATURE_MEMORY = 600
_parse_cache: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
_weights: Dict[Tuple[str, str, str], int] = {}
_memory: Dict[Tuple[str, str, str], int] = {}


def estimate_memory(result: Dict[str, Any], source_length: int) -> int:
    """Mémoire retenue par un résultat de parsing, en octets (estimation par excès)."""
    total = 2 * source_length  # textes repris du fichier : noms, descriptions, horodatages
    pending = list(result.get('features') or [])
    while pending:
        feature = pending.pop()
        if not isinstance(feature, dict):
            continue
        if feature.get('type') == 'marker':
            total += MARKER_MEMORY
            continue
        total += FEATURE_MEMORY + VERTEX_MEMORY * len(feature.get('coordinates') or [])
        total += VERTEX_MEMORY * sum(len(hole) for hole in feature.get('holes') or [])
        total += 60 * sum(len(values) for values in (feature.get('track_data') or {}).values())
        pending.extend(feature.get('tracks') or [])
    return total


_lock = threading.Lock()


def _get_or_compute(key, weight: int, compute):
    with _lock:
        if key in _parse_cache:
            _parse_cache.move_to_end(key)
            return _parse_cache[key]
    value = compute()  # hors verrou : le parsing peut être long
    if weight > MAX_ENTRY_BYTES:
        return value
    memory = estimate_memory(value, weight)
    if memory > MAX_ENTRY_MEMORY:
        return value
    with _lock:
        if key in _parse_cache:
            return _parse_cache[key]
        _parse_cache[key] = value
        _weights[key] = weight
        _memory[key] = memory
        while (len(_parse_cache) > MAX_ENTRIES or sum(_weights.values()) > MAX_TOTAL_BYTES
               or sum(_memory.values()) > MAX_TOTAL_MEMORY):
            evicted, _ = _parse_cache.popitem(last=False)
            _weights.pop(evicted, None)
            _memory.pop(evicted, None)
    return value


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parse_kml_cached(content: str, display_mode: Optional[str] = None) -> Dict[str, Any]:
    """
    Parse KML content using a cache to avoid duplicate work.
    display_mode est sans effet (les unités sont choisies à l'affichage) : accepté le temps
    que les appelants cessent de le passer.
    """
    key = ("kml", _hash_content(content), "")
    return _get_or_compute(key, len(content), lambda: KMLParser.parse_kml_coordinates(content))


def parse_gpx_cached(content: str) -> Dict[str, Any]:
    """Parse GPX content using a cache to avoid duplicate work."""
    key = ("gpx", _hash_content(content), "")
    return _get_or_compute(key, len(content), lambda: GPXParser.parse_gpx_coordinates(content))


def clear_cache() -> None:
    """Clear the parsing cache (mainly for tests)."""
    with _lock:
        _parse_cache.clear()
        _weights.clear()
        _memory.clear()
