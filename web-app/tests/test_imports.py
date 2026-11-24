"""Chaque module app.* doit s'importer seul, dans un interpréteur neuf (pas de cycle d'import)."""
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _modules():
    import app
    return sorted(m.name for m in pkgutil.walk_packages(app.__path__, 'app.'))


@pytest.mark.parametrize('module', _modules())
def test_module_imports_alone(module):
    res = subprocess.run([sys.executable, '-I', '-c', f"import sys; sys.path.insert(0, {str(ROOT)!r}); import {module}"],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr[-800:]
