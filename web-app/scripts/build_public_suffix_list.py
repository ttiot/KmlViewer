"""
Fige la Public Suffix List embarquée (admin-space EF-2.4b, E-13).

    python -I scripts/build_public_suffix_list.py [fichier_liste] [fichier_licence] [--out-dir DOSSIER]

Sans argument, télécharge les deux fichiers à leur commit figé ci-dessous ; avec des fichiers, les lit (même contrôle). Le SHA-256
est vérifié AVANT toute écriture : une source différente arrête le script sans rien écrire. L'application ne télécharge JAMAIS
rien : ce script ne sert qu'à régénérer les fichiers, sur décision (changer le commit et les SHA-256, relancer, relire).

Sortie (aucune donnée variable : mêmes entrées => mêmes octets) :
  public_suffix_list.dat     la liste, octet pour octet (sections ICANN et PRIVATE)
  public_suffix_list.SOURCE  provenance, SHA-256, date du commit, avis de licence MPL-2.0 complet

Bibliothèque standard seulement.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

COMMIT = "1e0f6aeb9cf9c9066ad32589d25cb7c85c93431e"
COMMIT_DATE = "2026-11-25"
BASE = f"https://raw.githubusercontent.com/publicsuffix/list/{COMMIT}/"
LIST_URL, LICENSE_URL = BASE + "public_suffix_list.dat", BASE + "LICENSE"
LIST_SHA256 = "975cab65363bf21b2d7d264432d42ec27483bf8e6d59b66bc27b282797137088"
LIST_BYTES = 343_839
LICENSE_SHA256 = "66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172"
MAX_DOWNLOAD = 4 * 1024 * 1024

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / 'app' / 'auth' / 'publicsuffix_data'
LIST_NAME, SOURCE_NAME = 'public_suffix_list.dat', 'public_suffix_list.SOURCE'


class SourceError(Exception):
    """Source illisible ou au SHA-256 inattendu : rien n'est écrit."""


def fetch(url: str, path, expected: str) -> bytes:
    if path:
        data = Path(path).read_bytes()
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'build-public-suffix-list'})
        with urllib.request.urlopen(request, timeout=60) as response:      # noqa: S310 - URL figée, https
            data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise SourceError("Source plus grosse que prévue")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise SourceError(f"SHA-256 inattendu : {digest} (attendu {expected})")
    return data


def render_source(license_text: str) -> str:
    return f"""Public Suffix List embarquée (admin-space EF-2.4b, E-13) : validation de WEBAUTHN_RP_ID, sans accès réseau.
Fichier figé : public_suffix_list.dat (ne pas modifier à la main ; régénérer avec scripts/build_public_suffix_list.py).

SOURCE PUBLIQUE
  Projet    : Public Suffix List (https://publicsuffix.org/, https://github.com/publicsuffix/list)
  Fichier   : public_suffix_list.dat (sections ICANN et PRIVATE)
  Commit    : {COMMIT} ({COMMIT_DATE})
  URL figée : {LIST_URL}
  SHA-256   : {LIST_SHA256}   ({LIST_BYTES} octets)
  Licence   : Mozilla Public License 2.0 (MPL-2.0), https://mozilla.org/MPL/2.0/ ; le fichier est redistribué INCHANGÉ,
              son en-tête de licence est conservé. Texte complet ci-dessous (SHA-256 du fichier LICENSE : {LICENSE_SHA256}).

UTILISATION
  Lu une fois par processus au démarrage (app/auth/publicsuffix.py), jamais téléchargé. Absent, illisible ou vide : refus de
  démarrer quand les comptes à mot de passe sont disponibles.

MISE À JOUR
  Sur décision seulement : changer COMMIT, COMMIT_DATE et les SHA-256 du script, relancer
  `python -I scripts/build_public_suffix_list.py`, relire le diff.

AVIS DE LICENCE (MPL-2.0)
-------------------------
{license_text}"""


def main(argv) -> int:
    args, out_dir = [], DEFAULT_OUT_DIR
    it = iter(argv)
    for arg in it:
        if arg == '--out-dir':
            out_dir = Path(next(it))
        else:
            args.append(arg)
    list_path = args[0] if len(args) > 0 else None
    license_path = args[1] if len(args) > 1 else None
    try:
        data = fetch(LIST_URL, list_path, LIST_SHA256)
        licence = fetch(LICENSE_URL, license_path, LICENSE_SHA256)
    except (SourceError, OSError) as exc:
        print(f"Échec : {exc}", file=sys.stderr)
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / LIST_NAME).write_bytes(data)
    (out_dir / SOURCE_NAME).write_text(render_source(licence.decode('utf-8')), encoding='utf-8')
    print(f"{LIST_NAME} : {len(data)} octets, SHA-256 {LIST_SHA256}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
