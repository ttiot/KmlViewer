"""
Lecture XML en flux, à mémoire bornée, pour les fichiers venant des utilisateurs.

Construire l'arbre complet d'un document avant d'en lire le premier élément coûte
plusieurs dizaines de fois sa taille : 16 Mo de balises vides occupent ~450 Mo. Ici le
document est lu d'un trait et seul ce qui est en cours de traitement reste en mémoire :

- un `StreamHandler` dit, à l'ouverture de chaque élément, s'il veut son sous-arbre entier
  (un Placemark, un point de trace) ; il le reçoit complet à sa fermeture ;
- tout autre élément est remis à sa fermeture, sans ses enfants (déjà traités), le temps
  d'y lire son texte et ses attributs ;
- dans les deux cas l'élément est ensuite détaché de son parent, donc libéré.

Le parseur reste celui de defusedxml : entités et DTD externes sont refusées avant tout
traitement (XXE, bombes d'entités). Fonctions sans dépendance à Flask.
"""

import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Optional

from defusedxml.ElementTree import DefusedXMLParser

# Profondeur maximale d'imbrication. Un fichier réel reste sous la trentaine (dossiers KML
# compris) ; sans borne, quelques Mo de balises ouvertes sans jamais être fermées tiennent
# tous ensemble en mémoire, puisqu'aucun élément ne se termine.
MAX_DEPTH = 200


class DepthError(ValueError):
    """Document plus profondément imbriqué que MAX_DEPTH."""

    def __init__(self) -> None:
        super().__init__(f"Document XML trop imbriqué (plus de {MAX_DEPTH} niveaux)")


class StreamHandler:
    """
    Ce qu'un format fait du flux. À dériver ; `canonical_ns` et `ns_aliases` ramènent les
    variantes connues d'un espace de noms (ou son absence) à celui que le format attend.
    """

    canonical_ns: str = ''
    ns_aliases: Iterable[str] = ()

    def opened(self, element: ET.Element) -> bool:
        """
        Un élément s'ouvre (attributs lisibles, ni texte ni enfants encore). Retourner True
        pour recevoir son sous-arbre entier dans `subtree` ; sinon il sera remis à `closed`.
        """
        return False

    def subtree(self, element: ET.Element) -> None:
        """Sous-arbre demandé par `opened`, complet."""

    def closed(self, element: ET.Element) -> None:
        """Élément refermé : son texte et ses attributs sont lisibles, ses enfants déjà partis."""


class _Target:
    """Cible du parseur XML : construit les éléments, les remet au handler, puis les libère."""

    def __init__(self, handler: StreamHandler) -> None:
        self._handler = handler
        self._builder = ET.TreeBuilder()
        # Le texte va droit au constructeur d'éléments, sans passer par du code Python
        self.data = self._builder.data
        self._open: List[ET.Element] = []
        self._kept_depth = 0  # > 0 : à l'intérieur d'un sous-arbre demandé par le handler
        self._names: Optional[Dict[str, str]] = None  # balise lue -> balise normalisée
        self._old: Optional[str] = None  # préfixe à remplacer ; None : rien à renommer
        self._new = ''

    def _prefixes(self, root_tag: str) -> None:
        """Décide, sur la racine, quel espace de noms ramener à celui du format."""
        handler = self._handler
        current = root_tag[1:].split('}', 1)[0] if root_tag.startswith('{') else ''
        if current == handler.canonical_ns or (current and current not in handler.ns_aliases):
            self._old, self._new = None, ''
        else:
            self._old, self._new = (f'{{{current}}}' if current else ''), f'{{{handler.canonical_ns}}}'
        self._names = {}

    def _name(self, tag: str) -> str:
        names = self._names if self._names is not None else {}
        known = names.get(tag)
        if known is None:
            old = self._old or ''
            if old:
                known = self._new + tag[len(old):] if tag.startswith(old) else tag
            else:
                known = tag if tag.startswith('{') else self._new + tag
            names[tag] = known
        return known

    def start(self, tag: str, attrib: Dict[str, str]) -> None:
        if self._names is None:
            self._prefixes(tag)
        if len(self._open) >= MAX_DEPTH:
            raise DepthError()
        element = self._builder.start(tag if self._old is None else self._name(tag), attrib)
        self._open.append(element)
        if self._kept_depth:
            self._kept_depth += 1
        elif self._handler.opened(element):
            self._kept_depth = 1

    def end(self, tag: str) -> None:
        element = self._builder.end(tag if self._old is None else self._name(tag))
        self._open.pop()
        if self._kept_depth:
            self._kept_depth -= 1
            if self._kept_depth:
                return  # encore à l'intérieur du sous-arbre demandé
            self._handler.subtree(element)
        else:
            self._handler.closed(element)
        if self._open:
            # Détaché de son parent : plus rien ne le retient, il est libéré. La liste des
            # enfants du parent reste donc vide ou presque, le retrait est immédiat.
            self._open[-1].remove(element)

    def close(self) -> None:
        self._builder.close()


def parse(content: str, handler: StreamHandler) -> None:
    """
    Lit `content` d'un trait en remettant ses éléments à `handler`. Lève ET.ParseError si
    le document est mal formé, DepthError s'il est trop imbriqué, et les ValueError de
    defusedxml (EntitiesForbidden, ExternalReferenceForbidden) s'il déclare des entités.
    """
    parser: Any = DefusedXMLParser(target=_Target(handler), forbid_dtd=False,
                                   forbid_entities=True, forbid_external=True)
    parser.feed(content)
    parser.close()
