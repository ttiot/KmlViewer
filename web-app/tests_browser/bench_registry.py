"""
Registre de TEST du banc (CA-78, CA-79) : un réglage `list`, un `requires_restart` et un `secret`, que le registre v1 n'a pas.
Importé AVANT la création de l'application (bench_app.py et seed.py, profil `registry`) : ajoute les entrées à REGISTRY.
Jamais importé par l'application elle-même : seule la deuxième instance de test les voit.
"""

from app.settings.registry import REGISTRY, Setting

TEST_SETTINGS = (
    Setting('bench.tags', 'list', [], 'Général', "Étiquettes (banc)", max_items=3, max_length=12),
    Setting('bench.restart', 'int', 5, 'Général', "Réglage à redémarrage (banc)", min=1, max=50, unit='', requires_restart=True),
    Setting('bench.secret', 'str', '', 'Général', "Secret du banc", max_length=40, secret=True),
)
for _setting in TEST_SETTINGS:
    REGISTRY[_setting.key] = _setting
