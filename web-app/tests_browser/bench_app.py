"""Point d'entrée gunicorn de l'instance « registre de test » du banc : `bench_app:app` (registre : bench_registry.py)."""

import bench_registry  # noqa: F401  (effet de bord : complète REGISTRY avant la création de l'application)
import kml_viewer

app = kml_viewer.app
