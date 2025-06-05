import time
from functools import wraps  # Import pour conserver le nom de la fonction d'origine

# Dictionnaire global pour stocker les durées cumulées des fonctions
execution_times = {}

def track_time(func):
    """
    Décorateur pour suivre le temps d'exécution d'une fonction et le cumuler.
    """
    @wraps(func)  # Conserve le nom et les métadonnées de la fonction décorée
    def wrapper(*args, **kwargs):
        # Heure de début
        start_time = time.time()
        # Appel de la fonction originale
        result = func(*args, **kwargs)
        # Heure de fin
        end_time = time.time()
        
        # Temps d'exécution
        duration = end_time - start_time
        
        # Cumuler la durée dans le dictionnaire
        func_name = func.__name__
        if func_name not in execution_times:
            execution_times[func_name] = {'total_time': 0, 'count': 0}
            
        execution_times[func_name]['total_time'] += duration
        execution_times[func_name]['count'] += 1
        
        return result

    return wrapper