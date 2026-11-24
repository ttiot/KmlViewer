"""
Génère la liste de mots de passe courants embarquée (admin-space EF-1.4b, ADR-0002).

    python -I scripts/build_common_passwords.py [fichier_source] [--out-dir DOSSIER]

Sans argument, télécharge l'URL figée ci-dessous ; avec un fichier, le lit (même contrôle). Le SHA-256 de
la source est vérifié AVANT tout traitement : une source différente arrête le script sans rien écrire.
L'application, elle, ne télécharge jamais rien : ce script ne sert qu'à régénérer les fichiers, sur
décision (changer le commit et le SHA-256 ci-dessous, relancer, relire les effectifs affichés).

Sortie (aucune date ni donnée variable : mêmes entrées => mêmes octets) :
  common_passwords.txt     UTF-8, une entrée par ligne (\\n), NFKC puis casefold(), 12 à 128 caractères après
                           normalisation, sans doublon, triée par points de code, sans ligne vide
  common_passwords.SOURCE  provenance, SHA-256, paramètres, effectifs, avis de licence MIT de SecLists
Entrée : common_passwords_local.txt (liste d'amorce du projet), fusionnée.

Bibliothèque standard seulement.
"""

import hashlib
import sys
import unicodedata
import urllib.request
from pathlib import Path

SOURCE_URL = ("https://raw.githubusercontent.com/danielmiessler/SecLists/c205c36a445bff37f8e58a9ec829105cd4975c58/"
              "Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt")
SOURCE_COMMIT = "c205c36a445bff37f8e58a9ec829105cd4975c58"
SOURCE_BLOB = "e5e73232c8f4f53edfe9c0fd699f415da8a336e3"
SOURCE_SHA256 = "424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51"
SOURCE_BYTES = 8_557_632
MIN_LENGTH, MAX_LENGTH = 12, 128
MAX_DOWNLOAD = 32 * 1024 * 1024        # borne de lecture : la source fait ~8,5 Mo

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / 'app' / 'auth' / 'wordlists'
LIST_NAME, SOURCE_NAME, LOCAL_NAME = 'common_passwords.txt', 'common_passwords.SOURCE', 'common_passwords_local.txt'

MIT_NOTICE = """MIT License

Copyright (c) 2018 Daniel Miessler

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class SourceError(Exception):
    """Source illisible ou au SHA-256 inattendu : rien n'est écrit."""


def fetch_source(path=None) -> bytes:
    if path:
        data = Path(path).read_bytes()
    else:
        request = urllib.request.Request(SOURCE_URL, headers={'User-Agent': 'build-common-passwords'})
        with urllib.request.urlopen(request, timeout=60) as response:      # noqa: S310 - URL figée, https
            data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise SourceError("Source plus grosse que prévu")
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_SHA256:
        raise SourceError(f"SHA-256 de la source inattendu : {digest} (attendu {SOURCE_SHA256})")
    return data


def normalize(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def keep(entry: str) -> bool:
    """12 à 128 caractères APRÈS normalisation, sans fin de ligne interne (le format est « une entrée par ligne »)."""
    return MIN_LENGTH <= len(entry) <= MAX_LENGTH and '\n' not in entry and '\r' not in entry


def parse_source(data: bytes):
    """(entrées retenues, lignes lues, lignes UTF-8 invalides). On coupe sur \\n et retire un \\r final, pas les espaces."""
    lines = data.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    kept, invalid = set(), 0
    for raw in lines:
        if raw.endswith(b'\r'):
            raw = raw[:-1]
        try:
            entry = normalize(raw.decode('utf-8'))
        except UnicodeDecodeError:
            invalid += 1
            continue
        if keep(entry):
            kept.add(entry)
    return kept, len(lines), invalid


def parse_local(text: str) -> set:
    return {e for e in (normalize(line) for line in text.split('\n')) if keep(e)}


def render_list(entries) -> bytes:
    return ''.join(f'{e}\n' for e in sorted(entries)).encode('utf-8')


def render_source(*, lines, invalid, public, local, common, total, output_sha256, output_bytes) -> str:
    return f"""Liste de mots de passe courants refusés par la politique (admin-space EF-1.4b, ADR-0002).
Fichier généré : common_passwords.txt (ne pas modifier à la main ; régénérer avec scripts/build_common_passwords.py).

SOURCE PUBLIQUE
  Dépôt     : SecLists (https://github.com/danielmiessler/SecLists)
  Fichier   : Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt
  Commit    : {SOURCE_COMMIT}
  Blob      : {SOURCE_BLOB}
  URL figée : {SOURCE_URL}
  SHA-256   : {SOURCE_SHA256}   ({SOURCE_BYTES} octets, 1 000 000 lignes)
  Provenance des données : jeu « ten million passwords » publié par Mark Burnett en 2015 (README du dossier
  SecLists). Statut des données d'origine : décrites comme versées au domaine public par la presse ; le texte
  d'origine n'a pas pu être vérifié. Licence du dépôt SecLists : MIT (avis ci-dessous).

LISTE LOCALE FUSIONNÉE
  common_passwords_local.txt : amorce du projet (mots de passe français et propres au projet).

TRAITEMENT
  découpe sur \\n (un \\r final retiré, pas les espaces) ; décodage UTF-8 strict (ligne invalide ignorée) ;
  normalisation NFKC puis casefold() ; filtre {MIN_LENGTH} à {MAX_LENGTH} caractères APRÈS normalisation ; union avec la
  liste locale ; dédoublonnage ; tri par points de code ; une entrée par ligne, fin de ligne \\n.

EFFECTIFS
  lignes lues dans la source : {lines}
  lignes invalides (UTF-8)  : {invalid}
  entrées publiques retenues: {public}
  entrées de la liste locale: {local}
  communes aux deux listes  : {common}
  total (union)             : {total}
  common_passwords.txt      : {total} entrées, {output_bytes} octets, SHA-256 {output_sha256}

COMMANDE
  cd web-app && python -I scripts/build_common_passwords.py [chemin/vers/la/source]

AVIS DE LICENCE (SecLists, reproduit intégralement)

{MIT_NOTICE}"""


def build(source: bytes, local_text: str, previous: bytes = b''):
    """Fonction pure : (octets de la liste, texte SOURCE, statistiques). Testable sans réseau."""
    public, lines, invalid = parse_source(source)
    local = parse_local(local_text)
    entries = public | local
    output = render_list(entries)
    stats = {
        'lines': lines, 'invalid': invalid, 'public': len(public), 'local': len(local),
        'common': len(public & local), 'total': len(entries), 'output_bytes': len(output),
        'output_sha256': hashlib.sha256(output).hexdigest(),
    }
    old = {e for e in previous.decode('utf-8', 'replace').split('\n') if e}
    stats['added'], stats['removed'] = len(entries - old), len(old - entries)
    return output, render_source(**{k: stats[k] for k in (
        'lines', 'invalid', 'public', 'local', 'common', 'total', 'output_sha256', 'output_bytes')}), stats


def main(argv) -> int:
    args = list(argv)
    out_dir = DEFAULT_OUT_DIR
    if '--out-dir' in args:
        i = args.index('--out-dir')
        out_dir = Path(args[i + 1])
        del args[i:i + 2]
    source_path = args[0] if args else None
    try:
        source = fetch_source(source_path)
        local_text = (out_dir / LOCAL_NAME).read_text(encoding='utf-8')
    except (SourceError, OSError) as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 2
    list_path = out_dir / LIST_NAME
    previous = list_path.read_bytes() if list_path.exists() else b''
    output, source_text, stats = build(source, local_text, previous)
    out_dir.mkdir(parents=True, exist_ok=True)
    list_path.write_bytes(output)
    (out_dir / SOURCE_NAME).write_text(source_text, encoding='utf-8', newline='\n')
    for label, key in (("lignes lues", 'lines'), ("lignes invalides", 'invalid'), ("retenues (publiques)", 'public'),
                       ("liste locale", 'local'), ("communes", 'common'), ("total", 'total'),
                       ("ajoutées vs fichier existant", 'added'), ("retirées vs fichier existant", 'removed')):
        print(f"{label:32}: {stats[key]}")
    print(f"{LIST_NAME}: {stats['output_bytes']} octets, SHA-256 {stats['output_sha256']}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
