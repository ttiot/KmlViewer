"""
Version affichée (pied de page, /api/health).

Dans l'image, elle est injectée au build (ARG APP_VERSION : tag de release, sinon
« unstable-<sha> »). Depuis un clone du dépôt, elle est lue dans git : le tag posé sur le
commit courant, sinon son empreinte courte. Sans l'un ni l'autre : « dev ».
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FALLBACK = 'dev'
_REPO_DIR = Path(__file__).resolve().parent
# La valeur finit dans une page HTML et dans les logs : rien d'autre que ces caractères
_SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+-]{0,63}')


def _git(*args: str) -> Optional[str]:
    """Sortie d'une commande git (arguments fixes, sans shell) ; None si elle échoue."""
    try:
        result = subprocess.run(['git', *args], cwd=_REPO_DIR, capture_output=True, text=True,
                                timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def from_git() -> Optional[str]:
    """Tag du commit courant, sinon son empreinte courte ; None hors d'un dépôt git."""
    return _git('describe', '--tags', '--exact-match', 'HEAD') or _git('rev-parse', '--short', 'HEAD')


def resolve(configured: Optional[str]) -> str:
    """Version à afficher : celle fournie au build si elle existe, sinon celle lue dans git."""
    value = (configured or '').strip()
    if not value or value == FALLBACK:
        value = from_git() or FALLBACK
    if not _SAFE.fullmatch(value):
        logger.warning("Version %r ignorée : caractères inattendus", value)
        return FALLBACK
    return value
