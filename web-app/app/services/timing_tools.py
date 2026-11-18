"""Mesure cumulée des temps d'exécution (diagnostic, exposée aux admins par /api/stats)."""

import threading
import time
from functools import wraps
from typing import Dict

_lock = threading.Lock()
_execution_times: Dict[str, Dict[str, float]] = {}


def track_time(func):
    """Décorateur : cumule la durée et le nombre d'appels de la fonction."""
    name = func.__qualname__

    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            duration = time.perf_counter() - start
            with _lock:  # gunicorn sert les requêtes sur plusieurs threads
                entry = _execution_times.setdefault(name, {'total_time': 0.0, 'count': 0})
                entry['total_time'] += duration
                entry['count'] += 1

    return wrapper


def snapshot() -> Dict[str, Dict[str, float]]:
    """Copie cohérente des compteurs."""
    with _lock:
        return {name: dict(entry) for name, entry in _execution_times.items()}
