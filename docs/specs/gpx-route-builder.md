# Spec — Générateur de traces planifiées (bêta)

Statut : **v6, validée par l'utilisateur, en développement** (re-review v5 intégrée) ; spike TR0 et mesures cible faits, reste l'appréciation visuelle (§13) · Mise à jour : 2026-11-24 · Rôle : spec
Arbitrages utilisateur relayés par coord le 2026-11-24 (cf. §12). Corrections de [REVIEW.md](../../REVIEW.md) intégrées (review v3 et « Re-review v4 » : R3, n7, n8 et découpage pour cette spec), références sur les exigences touchées. Depuis la v3 : toujours une boucle fermée, détours ajoutés pour atteindre la distance.
Dépend de : [admin-space.md](admin-space.md) — **bloquant** pour les fonctions bêta (EF-7), les réglages (EF-6), les secrets et destinations sortantes (EF-5, EF-6.8) et le limiteur de débit.
ADR : [0007 moteur de routage](../adr/0007-moteur-routage.md) · [0009 traces planifiées](../adr/0009-itineraires-planifies.md) · [0010 stratégie de détours](../adr/0010-strategie-detours.md).

Principes imposés : **modulaire** et **sécurité d'abord**.

**Vocabulaire** (arbitré) : dans l'UI, le résultat est une **Trace**, marquée du badge **Planifiée**. Le mot « itinéraire » n'apparaît plus dans l'UI. Les identifiants de code restent en anglais (`route_builder`, `/api/route-builder`, `kind='planned'`).

---

## 1. Contexte

L'utilisateur prépare une sortie : il place des points sur la carte, choisit une activité et une distance approximative, clique « Terminer », et obtient une **boucle fermée**, routée sur les routes et chemins réels, passant par tous ses points dans l'ordre, allongée par des détours si besoin pour approcher la distance voulue. La trace s'affiche comme n'importe quel GPX (profil, analyse, export) et s'enregistre dans la bibliothèque, badge « Planifiée », sans fausser les statistiques.

Existant réutilisable (chemins relatifs à `web-app/`, n1) :

| Besoin | Existant |
|---|---|
| Carte Leaflet | `static/js/viewer/map.js` (`initMap`, `Viewer.map`) |
| Affichage d'un fichier parsé | `parse_gpx_cached` (`app/services/cache_service.py`) → payload du visualiseur (`app/api/uploads.py`, `static/js/viewer/payload.js`) |
| Export | `POST /api/export/<fmt>` (`app/api/editor_routes.py`) |
| Enregistrement | `ingest_service.ingest_bytes(..., source=...)` (quota, dédoublonnage SHA-256, DEM) |
| Altitude | `dem_service` (OpenTopoData local, si `DEM_URL`) |
| Distances géodésiques | `app/services/geo.py` |
| Bornage des traitements lourds | `heavy_slot()` (`app/api/workload.py`) : 2 places pour toute l'instance, attente 20 s puis 503 — réservé ici au parse final (M9) |
| Recalcul des résumés | `ingest_service.recompute_summaries` (`flask recompute-stats`) — à filtrer (M14) |
| Appels HTTP sortants robustes | modèle de `dem_service.py` (timeouts, reprises, budget total) |
| Service auto-hébergé | modèle `opentopodata` dans `docker-compose.yml` |

Contraintes : CSP `connect-src 'self'` (tout passe par le serveur) ; moteur **auto-hébergé** (GraphHopper, ADR-0007). « Les points ne sortent pas de l'infrastructure » est **garanti** par la liste blanche `OUTBOUND_ALLOWED_HOSTS` appliquée à `routing.base_url` (admin-space EF-6.8, ADR-0011), pas seulement par l'intention de l'admin (B3).

**Emprise couverte** (arbitrée) : **Hauts-de-France + Belgique**, en un seul graphe : une boucle peut traverser la frontière. Périmètre belge recommandé : **pays entier** (ADR-0007). Hors de cette emprise, aucune trace n'est générée.

## 2. Objectifs

1. Mode « Créer une trace » dans le visualiseur, derrière la fonction bêta `route_builder`.
2. Placement et édition de points de passage.
3. Paramètres : **activité** et **distance approximative**.
4. « Terminer » ⇒ boucle fermée passant par tous les points dans l'ordre, avec détours pour approcher la distance.
5. Enregistrement dans la bibliothèque, badge « Planifiée », exclu des statistiques.
6. Moteur interchangeable et stratégie de détours interchangeable.

## 3. Hors périmètre (v1)

- Navigation guidée ; temps estimé ; routage en direct à chaque point (génération sur « Terminer » seulement). Seule exception : pendant qu'un résultat est affiché, **déplacer, insérer, ajouter ou retirer un point** régénère la trace au relâchement (RB-2.7).
- Trace ouverte (A → B) : toute trace générée est une boucle.
- Raccourcir une boucle trop longue en retirant ou déplaçant des points de l'utilisateur.
- Édition fine de la trace générée (on régénère).
- Moteur SaaS ; partage entre utilisateurs ; conversion en activité réalisée.

---

## 4. Découpage modulaire

| Module | Rôle |
|---|---|
| `app/services/routing/base.py` | Interface `RoutingProvider` : `route(waypoints, profile)`, `round_trip(start, profile, distance_m, seed)`, `health()`. Types, erreurs (`RoutingUnavailable` ⇒ 503, `NoRouteFound` ⇒ 422, `InvalidRequest` ⇒ 400, `InvalidResponse` ⇒ 502 : réponse du moteur non conforme, RB-S4). Pur. |
| `app/services/routing/graphhopper.py` | Adaptateur GraphHopper (ADR-0007). |
| `app/services/routing/profiles.py` | Activité proposée ⇒ profil moteur ⇒ type stocké. |
| `app/services/routing/detours.py` | Interface `DetourStrategy` et stratégies (ADR-0010) : à partir de la boucle de base et du manque de distance, propose des points de passage intermédiaires ou des pétales. Ne connaît pas le moteur, seulement `RoutingProvider`. |
| `app/services/routing/builder.py` | Orchestration : validation, boucle de base, boucle d'ajustement, budget d'appels, contrôles qualité, DEM, GPX. |
| `app/services/routing/results.py` | Résultats générés gardés en mémoire (par utilisateur, TTL) en vue de l'enregistrement. |
| `app/api/route_builder_routes.py` | Routes HTTP sous `feature_required('route_builder')`, limitées en débit. |
| `static/js/viewer/route-builder.js` | Édition, paramètres, appel API, injection du résultat. |
| `routing/` (racine) + `docker-compose.yml` | Configuration GraphHopper (profils, modèle gravel) et service compose — **ops**. |

Le module `routing` ne lit ni `os.environ` ni la base : sa configuration vient de `settings.store`.

---

## 5. Exigences fonctionnelles

### 5.1 Accès

- **RB-1.1** Fonction bêta `route_builder`, `off` par défaut, activable pour une **liste d'utilisateurs** ou **tous**. Indisponible ⇒ bouton absent, routes 404.
- **RB-1.2 (M13)** Deux états distincts :
  - **non configuré** (`routing.base_url` vide, ou destination hors liste blanche) ⇒ fonction **invisible** et routes 404, même activée ; l'admin le signale ;
  - **injoignable** ⇒ fonction **visible** ; `options` renvoie `available: false` et le front affiche « service momentanément indisponible » ; `route` répond 503.
  L'état de santé est sondé par la tâche `routing_health` de l'**ordonnanceur de fond** (admin-space §5.10, R3), qui tourne dans toute instance servie, inbox et DEM activés ou non : `health()` du provider, **premier passage au démarrage**, puis toutes les 60 s (réglage 15–600 s). Le résultat (état, rectangle annoncé par `/info` pour le seul diagnostic, date) est mis en cache ; aucune page ni requête utilisateur ne déclenche de sondage synchrone. Dernier état et date affichés dans l'onglet admin « Fonctions bêta ».
  **Cache vide** (démarrage, moteur jamais joint, n8) ⇒ fonction traitée comme **injoignable** : `options` renvoie `available: false` (la couverture configurée reste renvoyée, RB-1.3), le front affiche « service en cours de démarrage » et ne permet pas de générer ; `route` répond 503.
- **RB-1.3 (constat du spike)** Couverture : le rectangle englobant de `/info` (ex. `[-8.32, 48.83, 6.42, 54.60]` au spike, élargi par les ferries et les bords des extraits) est **bien plus large** que la zone réellement couverte ; il n'est donc **pas** utilisé pour la couverture. La couverture est un **rectangle configuré côté appli**, réglage `routing.coverage_bbox` (§5.6). Valeur par défaut **`[1.35, 48.83, 6.42, 51.55]`** (décision journalisée par coord le 2026-11-25, sur proposition de spec) : le sud (`48,83`) et l'est (`6,42`) sont ceux du rectangle annoncé par `/info` sur les données réelles, plus étroit à ces deux bords que la première valeur `[1.35, 48.80, 6.45, 51.55]` ; avec elle, le diagnostic signalait en permanence « rectangle non contenu ». Avec le défaut retenu, `coverage_contained = true` sur le `/info` réel, sans perte de zone utile (les bandes retirées sont hors des données). Règles :
  - `GET /api/route-builder/options` le renvoie ; le front l'affiche en mode édition (contour discret) et refuse localement un point hors rectangle ;
  - le serveur refait le contrôle **avant tout appel au moteur** : un point de l'utilisateur hors du rectangle ⇒ **400** « hors de la zone couverte » ; les points techniques des détours (RB-S7) sont eux aussi rabattus dans ce rectangle ;
  - un point dans le rectangle mais que le moteur n'accroche à aucune voie (hors des données réelles : Pays-Bas, Luxembourg, Allemagne, Grand Est, Normandie… ; ou trop loin d'une voie) ⇒ **422** « Le point N est trop loin d'une voie praticable : rapproche-le d'un chemin. » ; sans numéro de point ⇒ 422 « Aucune trace possible avec ces points : essaie d'autres points. » (libellés alignés sur la décision de coord du 2026-11-25, STATUS n° 47 : distinguer le 400 « hors de la zone couverte » d'un point mal accroché ; les deux causes du 422 ne sont pas distinguées, le moteur ne le permet pas) ;
  - diagnostic : si le rectangle configuré **n'est pas contenu** dans celui de `/info` (données importées plus petites que la couverture annoncée), l'onglet admin « Fonctions bêta » le signale ; aucun effet sur le service.

### 5.2 Édition des points (front)

- **RB-2.1** Entrée « Créer une trace » dans la barre d'outils du visualiseur ; un mode actif à la fois : ouvrir le constructeur **ferme** l'éditeur de points du visualiseur (et tout autre mode qui écoute la carte), et réciproquement ; un clic sur la carte n'est traité que par le mode actif (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-6 : à corriger, les deux modes s'ouvrent ensemble dans le code livré).
- **RB-2.2** Clic/tap ⇒ ajoute un point à la fin ; glisser ⇒ déplace ; clic sur un point ⇒ supprimer / insérer avant ; annuler / rétablir (Ctrl+Z / Ctrl+Y et boutons) ; « Tout effacer » avec confirmation **in-page**.
- **RB-2.3** Points numérotés dans l'ordre de passage ; le point 1 est le départ **et** l'arrivée. La prévisualisation montre le segment de fermeture (dernier point → point 1).
- **RB-2.4** 1 à **50** points (réglage, plafond dur 100), contrôle front et serveur.
- **RB-2.5** Prévisualisation à vol d'oiseau (pointillés, boucle fermée) et sa longueur, calculées localement.
- **RB-2.6 (n5)** Avertissement in-page avant de quitter avec des points non générés ; points en cours en `localStorage` (try/catch) : positions des points, **activité et distance** saisies, rien d'autre (décision de coord, 2026-11-25, à relire par l'utilisateur : activité et distance gardées pour ne pas perdre le formulaire au rechargement). Ces positions sont **effacées à la déconnexion** (formulaire de logout, **depuis n'importe quelle page** : l'effacement est porté par le script commun chargé partout, pas par le script du mode ; décision de l'utilisateur, 2026-11-25, matrice de conformité, E-7 : à corriger) et quand l'utilisateur courant change (clé de stockage suffixée par l'identifiant de compte ; une clé d'un autre compte est supprimée à l'ouverture du mode). Elles sont aussi **effacées après un enregistrement réussi** (RB-5.1, y compris le cas « déjà enregistrée ») : un rechargement ne restaure alors aucun point (défaut trouvé par l'utilisateur, 2026-11-25, CA-R40). Une modification faite ensuite dans la page est un nouveau travail : elle est de nouveau stockée.
- **RB-2.7 Modifier les points sur le résultat affiché** (retours de l'utilisateur, 2026-11-25 : après « Terminer », déplacer un point n'est pas simple ; il veut aussi ajouter un point sur la trace pour l'étendre ou choisir un chemin plus précis, avec un recalcul automatique). Décisions de coord et de l'utilisateur du 2026-11-25 : avec un résultat affiché, **déplacer, insérer, ajouter et retirer** un point régénèrent la trace ; **« Tout effacer »** revient à l'édition. Cette version **remplace** la décision précédente « ajout ou retrait ⇒ retour à l'édition ».
  - **Portée** : s'applique quand un résultat est affiché (panneau de résultat visible). En édition (avant « Terminer » ou après « Modifier les points »), RB-2.2 ne change pas : les mêmes gestes modifient les points **sans aucune génération**.
  - **Gestes**, à la souris et au toucher :
    - **déplacer** : glisser un point P1…Pn ; recalcul au relâchement ;
    - **insérer** : **appui sur la trace** de résultat = un nouveau point apparaît sous le pointeur ; il suit le pointeur tant que l'appui dure ; au **relâchement**, il est inséré à la **position du relâchement** et la trace est recalculée. Un clic simple et un glisser sont le **même geste** : seule compte la position au relâchement. Le point est inséré **entre les deux points voisins du segment appuyé** (voir « Segment » ci-dessous) ;
    - **ajouter** : clic ou toucher **sur la carte hors de la trace** (et hors d'un point) ⇒ point ajouté **après Pn, avant le retour à P1** ; recalcul aussitôt. Un glisser hors de la trace déplace la carte, comme avant ;
    - **retirer** : « Retirer ce point » dans le menu d'un point (clic sur le point, RB-2.2), ou clic droit sur le point ⇒ recalcul aussitôt ;
    - rien n'est envoyé pendant un glisser, seulement au relâchement ou à la validation.
  - **Où l'appui compte** :
    - **tolérance** : un appui est « sur la trace » à **10 px** d'écran au plus de la trace avec une souris, **16 px** au plus au toucher ou au stylet. Au-delà, c'est un appui sur la carte ;
    - **priorité** : un point P1…Pn passe avant la trace (zone de prise d'au moins 24 × 24 px) ; la trace passe avant la carte ;
    - au toucher, un glisser qui **commence sur la trace** crée un point au lieu de déplacer la carte. Effet assumé : pour déplacer la carte, on commence le geste à côté de la trace ; la tolérance de 16 px reste étroite pour cela.
  - **Indication** :
    - à la souris, au survol de la trace (dans la tolérance) : curseur `pointer`, petit point fantôme qui suit le pointeur le long de la trace et infobulle « Clique ou glisse pour ajouter un point ». Rien de cela hors de la tolérance, ni pendant une génération ;
    - le panneau de résultat porte une aide d'une ligne, valable aussi au toucher : « Appuie sur la trace pour ajouter un point à cet endroit, sur la carte pour en ajouter un à la fin ; glisse un point pour le déplacer. ».
  - **Segment** d'une insertion :
    - chaque Pi a une position le long de la trace : celle du point de la trace le plus proche de Pi. Ces positions sont strictement croissantes (RB-4.7) ;
    - un appui à la position *x* le long de la trace, avec pos(Pi) ≤ *x* < pos(Pi+1), insère le nouveau point **entre Pi et Pi+1** ;
    - avant pos(P1) ou après pos(Pn) : segment de **fermeture** (Pn → P1) ⇒ le point est ajouté **après Pn**, comme un ajout hors trace ; P1 reste le départ ;
    - avec 1 point, toute la trace est le segment de fermeture : le nouveau point devient P2 ;
    - c'est le segment **de l'appui** qui compte, même si le point est ensuite glissé loin de lui.
  - **Mêmes paramètres** : la régénération envoie à `POST /api/route-builder/route` :
    - les points après le geste (renumérotés dans l'ordre) ;
    - l'**activité** et la **distance voulue** du résultat affiché, pas celles du formulaire si elles ont été changées depuis. Pour changer de paramètres, on passe par « Terminer ». Pas de distance dans le résultat affiché ⇒ pas de distance envoyée. Le panneau de résultat affiche la distance voulue utilisée (« Distance voulue : 20 km ») ;
    - la **graine de la trace affichée** (`route_meta.seed`), pour que la trace ne change que par le geste, et **pas** de `previous_result_id` (réservé à « Autre proposition », RB-4.5).
    Aucune nouvelle route ; les contrôles du serveur s'appliquent tels quels (RB-S1, RB-1.3, RB-S5).
  - **Distance voulue conservée** quand un ajout ou une insertion allonge la boucle (décision de spec, 2026-11-25) :
    - la distance voulue est une saisie de l'utilisateur ; le système ne la change pas sans lui, comme il ne retire ni ne déplace jamais un point (RB-4.3) ;
    - un point ajouté pour « choisir un chemin plus précis » ne doit pas allonger la boucle : avec la même cible, le générateur réduit ses détours pour rester proche de T ;
    - si le nouveau point rend la boucle minimale plus longue que la cible (D0 > (1 + τ)·T), RB-4.3 s'applique déjà : la boucle minimale est rendue avec l'avertissement « distance minimale passant par tes points : D0 km ». L'utilisateur voit le chiffre et décide ;
    - une mise à jour automatique (T augmentée de l'allongement) serait imprévisible : l'allongement routé n'est connu qu'après la génération, ce qui coûterait un calcul de plus, et des modifications successives feraient dériver la cible ;
    - pour **étendre** la boucle : changer la distance dans le formulaire, puis « Terminer » (régénère avec les points du moment). Sans distance voulue (2 points ou plus), la boucle passe simplement par le nouveau point.
  - **Minimum de points** : il reste toujours au moins 1 point.
    - Avec un seul point, « Retirer ce point » est inactif ; « Tout effacer » sert à recommencer.
    - Passer de 2 points à 1 sans distance voulue dans le résultat affiché ⇒ refusé par la page, sans requête : « Un seul point demande une distance voulue : indique-la puis « Terminer ». ». Une boucle d'un point exige une distance (RB-3.2, CA-R2).
    - Retirer P1 ⇒ P2 devient le départ et l'arrivée (renumérotation).
  - **Maximum de points** : à `max_waypoints` (RB-2.4), un appui sur la trace ou sur la carte ne crée aucun point : message « 50 points au plus. », aucune requête.
  - **Résultat précédent gardé** tant que la nouvelle génération n'a pas abouti :
    - pendant le calcul, la trace précédente reste affichée, atténuée, avec « Recalcul en cours… » et l'indicateur et le bouton « Annuler » de RB-4.10 ; « Enregistrer » et « Autre proposition » sont inactifs ;
    - succès ⇒ la nouvelle trace remplace la précédente (une seule trace de résultat sur la carte) ; résumé, avertissements et nom prérempli sont mis à jour ; la mention « Enregistrée » disparaît ;
    - **échec** (400, 422, 429, 502, 503, 504, réseau) ou **« Annuler »** ⇒ **retour à l'état précédent** : point déplacé revenu, point inséré ou ajouté supprimé, point retiré remis, numérotation d'avant ; les points stockés (RB-2.6) sont ceux d'avant ; la trace précédente redevient active avec ses boutons, et son `result_id` reste celui qu'« Enregistrer » envoie. Message dans le panneau, annoncé aux lecteurs d'écran (`role="alert"`) : « Recalcul impossible : <message du serveur>. La trace précédente est conservée. » ; pour « Annuler » : « Recalcul abandonné. La trace précédente est conservée. ». Aucune entrée d'historique.
  - **Annuler / Rétablir** (RB-2.2, boutons et Ctrl+Z / Ctrl+Y) :
    - un geste **réussi** (déplacer, insérer, ajouter, retirer) est une entrée d'historique qui porte les points **et** le résultat ;
    - Annuler ⇒ points d'avant et trace d'avant réaffichés **sans nouvelle génération** ; Rétablir ⇒ l'inverse. « Enregistrer » envoie le `result_id` de la trace affichée ;
    - **« Tout effacer »**, confirmé, revient à l'édition : panneau de résultat masqué, trace retirée de la carte, aucune requête. C'est une entrée d'historique : Annuler rétablit les points **et** le résultat quitté, sans requête ;
    - les résultats sont gardés dans l'historique du front, **10 au plus** ; plus ancien, l'état ne porte que ses points : l'annuler revient à l'édition (panneau de résultat masqué, « Terminer » disponible), sans génération ;
    - un résultat réaffiché peut avoir expiré côté serveur (30 min, RB-5.2) : « Enregistrer » répond alors 404 avec « régénère la trace », comme aujourd'hui.
  - **Clavier**, alternative à la souris et au toucher :
    - chaque point est atteignable par Tab, dans l'ordre de passage ; nom accessible : « Point 3 : flèches pour déplacer, Entrée pour valider ou ouvrir le menu, Suppr pour retirer » ;
    - **déplacer** : flèches, 10 px d'écran par appui ; Maj + flèches, 50 px. Le point bouge à l'écran, rien n'est envoyé. **Entrée** valide (même effet qu'un relâchement) ; **Échap**, ou la perte du focus sans Entrée, remet le point, sans requête ;
    - **retirer** : **Suppr** (ou Retour arrière) sur un point ⇒ même effet que « Retirer ce point » ;
    - **insérer** : Entrée sur un point sans déplacement en attente ouvre son menu, dont les boutons sont atteignables au clavier. « **Ajouter un point après celui-ci** » crée un nouveau point au milieu, à vol d'oiseau, entre Pi et Pi+1 (entre Pn et P1 pour le dernier ; avec 1 point, à 50 px à l'est de P1). Ce point reçoit le focus ; on le déplace aux flèches ; Entrée l'insère entre Pi et Pi+1 et recalcule ; Échap le supprime, sans requête ;
    - **ajouter à la fin** : bouton « Ajouter un point à la fin » du panneau de résultat ⇒ même chose qu'« Ajouter un point après » sur Pn ;
    - en édition, les mêmes touches modifient les points sans génération (RB-2.2).
  - **Une seule génération à la fois** (décision de spec) : pendant toute génération (« Terminer », « Autre proposition » ou geste de RB-2.7), les points sont **verrouillés** : glisser impossible, appui sur la trace ou sur la carte sans effet, indication de survol masquée, flèches et Suppr ignorées, Annuler / Rétablir inactifs, et le message « Calcul en cours : attends le résultat ou annule. ». Pas de file d'attente, pas de « dernier geste gagnant » : chaque génération coûte jusqu'à 15 appels au moteur, et le serveur n'en accepte qu'une par utilisateur et deux sur l'instance (RB-S5). Après « Annuler », la place côté serveur peut rester prise (P-TR5-6) : un nouveau geste reçoit alors 429, traité comme un échec. Chaque geste validé compte dans la limite de 30 générations par heure (RB-S5).
  - **Points limites** (décisions de spec, 2026-11-25) :
    - relâchement d'un point déplacé, inséré ou ajouté **hors de `routing.coverage_bbox`** ⇒ point déplacé revenu, ou nouveau point supprimé ; message « Hors de la zone couverte. », aucune requête (P-TR6-4). La zone reste tracée en pointillés ;
    - point dans la zone mais loin d'une voie ⇒ 422 du serveur « Le point N est trop loin d'une voie praticable… » ⇒ échec, retour à l'état précédent ;
    - déplacement de **moins de 5 m** ⇒ rien ne se passe (pas de requête, pas d'historique) ;
    - point déplacé, inséré ou ajouté à moins de 5 m d'un de ses **deux voisins** dans l'ordre de la boucle (Pn et P1 compris) ⇒ refusé, message « Trop près du point N. », aucune requête : le dédoublonnage du serveur (RB-S1b) changerait sinon la numérotation sans que l'utilisateur l'ait voulu ;
    - **1 point** : déplacer P1 régénère la boucle `round_trip` avec la même distance ;
    - emprise trop grande (RB-S1) ⇒ 400 du serveur ⇒ échec, retour à l'état précédent.
  - **Trace enregistrée jamais modifiée** : un geste ne fait que créer un **nouveau résultat** en mémoire (nouveau `result_id`). Aucune requête vers une activité enregistrée (ni `PATCH`, ni `DELETE`, ni `save`). Une trace déjà enregistrée garde son fichier, ses points et son nom ; enregistrer le nouveau résultat crée une **nouvelle** activité.

### 5.3 Paramètres

- **RB-3.1** **Activité** (liste fermée serveur) :

| Proposée | Profil moteur | Type stocké |
|---|---|---|
| Course à pied | `foot` | `run` |
| Trail | `hike` | `trail` |
| Randonnée | `hike` | `hike` |
| Vélo de route | `racingbike` | `road_bike` |
| Gravel | `gravel` (modèle personnalisé) | `road_bike` |
| VTT | `mtb` | `mtb` |

  Gravel = profil de routage seul ; `activity_types` inchangé. Noms de profils confirmés au spike TR0 (`foot`, `hike`, `racingbike`, `gravel`, `mtb` ; `bike` disponible mais non proposé). Défaut : `default_activity_type` du profil s'il correspond, sinon course à pied.
- **RB-3.2** **Distance approximative** : km, de 1 à `routing.max_distance_km` (défaut 300). Le pas de 0,5 km est celui du **champ de saisie du front** seulement : le serveur accepte tout nombre fini dans les bornes, sans arrondi (interprétation de dev confirmée, 2026-11-24). **Obligatoire avec 1 point** ; facultative avec ≥ 2 points (sans elle, boucle de base sans détour ; arbitré).
- **RB-3.3** Validation serveur (liste blanche, bornes).

### 5.4 Génération — arbitrage Q-R1 v3 : boucle fermée, points dans l'ordre, détours

Notations : P1…Pn les points de l'utilisateur, T la distance cible, τ la tolérance (réglage, défaut 15 %).

- **RB-4.1 — 1 point.** Boucle native du moteur au départ de P1, longueur ≈ T (`round_trip`). Hors tolérance ⇒ essais de graine selon RB-4.12 : au plus **`1 + routing.seed_retries`** graines au total, sous la même condition de budget restant ; puis meilleure proposition (interprétation de dev confirmée, 2026-11-24).
- **RB-4.2 — ≥ 2 points, boucle de base.** L0 = trajet routé P1 → P2 → … → Pn → P1, longueur D0.
- **RB-4.3 — Comparaison à la cible.**
  - pas de T, ou |D0 − T| ≤ τ·T ⇒ L0 retournée ;
  - D0 > (1 + τ)·T ⇒ L0 retournée avec l'avertissement « distance minimale passant par tes points : D0 km » (le système ne retire ni ne déplace jamais un point) ;
  - D0 < (1 − τ)·T ⇒ **détours** (RB-4.4), manque Δ = T − D0.
- **RB-4.4 — Détours.** La stratégie active (ADR-0010) ajoute des **points de passage techniques** entre deux points de l'utilisateur (ou des pétales partant d'un point de l'utilisateur) pour allonger la boucle de Δ :
  - l'ordre P1 … Pn est **toujours** respecté, la boucle **toujours** fermée sur P1 ;
  - les points techniques ne sont jamais montrés comme points de l'utilisateur ni exportés en `<wpt>` ;
  - ajustement itératif : on mesure la distance routée obtenue, on corrige l'ampleur des détours, au plus 4 itérations ;
  - un point technique non routable (lac, zone sans voie) est déplacé (autre côté, ampleur réduite) avant d'être abandonné ;
  - la graine (`seed`) fixe les choix aléatoires de la stratégie (côté des détours, tronçons allongés, départ des pétales) : même graine + mêmes entrées + mêmes données OSM ⇒ même trace.
- **RB-4.5 — « Autre proposition »** : nouvelle graine, mêmes points et paramètres.
- **RB-4.6 — Budget.** Au plus **15 appels au moteur par génération** (réglage, plafond dur 30) et un budget de temps total (réglage, défaut 30 s). Budget épuisé ⇒ meilleure trace obtenue + avertissement de l'écart.
- **RB-4.7 — Contrôles qualité** (calculés sur la trace finale, renvoyés dans `route_meta`) :
  - fermeture : la trace commence et finit au point de la route le plus proche de P1 ;
  - passage ordonné : chaque Pi est à moins de 50 m (réglage) d'un point de la trace, à des positions croissantes le long de la trace ; un Pi non approchable (hors réseau) ⇒ 422 avec son numéro (**numéro d'origine**, tel qu'envoyé par le client, avant dédoublonnage, m12) ;
  - **taux de recouvrement** : part de la distance parcourue deux fois (aller-retour, tronçon repris) ; > 25 % ⇒ avertissement « la trace emprunte plusieurs fois les mêmes voies » ;
  - écart à la cible (km et %).
- **RB-4.8 — Sortie.** GPX 1.1, un `<trk>` / un `<trkseg>`, `<name>` « Trace planifiée du JJ/MM/AAAA » (modifiable à l'enregistrement), `<type>` = type stocké, **aucun horodatage**, P1…Pn en `<wpt>`. Altitude (interprétation de dev confirmée, 2026-11-24) : le GPX généré porte l'altitude du moteur **seulement si chaque point en a une** (sinon aucun `<ele>`) ; l'altitude MNT n'est **pas** calculée à la génération mais **à l'enregistrement**, par l'ingestion, comme pour un upload (RB-5.3). Conséquence assumée : D+/D− de la prévisualisation (`route_meta`) viennent du moteur, ou valent `null` ; ceux de la trace enregistrée viennent du MNT quand il est configuré. Le GPX passe par `parse_gpx_cached` et revient sous la même forme qu'un upload. **Après la correction**, la trace enregistrée montre partout l'altitude MNT : `/parsed` (superposition), D+ du panneau, profil, altitude sous le curseur, analyse enregistrée invalidée puis recalculée. Avant la correction, le panneau affiche « altitude en cours de calcul » : voir [altitude-mnt.md](altitude-mnt.md), EA-1 à EA-5, CA-A1 à CA-A6 (défaut trouvé par test, 2026-11-25).
- **RB-4.9 — `route_meta`** : distance obtenue, D+/D−, cible, écart, D0, nombre de détours, taux de recouvrement, appels consommés, graine, profil, avertissements du moteur, `result_id`.
- **RB-4.10 — Attente.** Une génération peut durer plusieurs secondes : indicateur de progression et bouton « Annuler » (le front abandonne la requête ; le serveur respecte son budget de temps quoi qu'il arrive).

### 5.4b Exigences issues du spike TR0 (décisions D2 à D4 de l'utilisateur, 2026-11-24 ; ADR-0010)

Résultats détaillés dans l'ADR-0010. Ces exigences complètent RB-4.1 à RB-4.10.

- **RB-4.11 (stratégie, D2)** `routing.detour_strategy = hybrid` (défaut) — S1 (points latéraux) d'abord ; si la trace reste hors tolérance et qu'il reste du budget, un pétale S2 (`round_trip` depuis un point de l'utilisateur) comble le manque restant. Valeurs permises du réglage : `s1`, `s2`, `hybrid`. Le budget RB-4.6 (15 appels, 30 s) s'applique à l'ensemble.
- **RB-4.12 (essais de graine, D2)** Trace hors tolérance et budget restant suffisant (au moins 3 appels et 5 s) ⇒ nouvel essai avec la graine suivante, **au plus 2 essais supplémentaires** (réglage `routing.seed_retries` 0–2) ; la meilleure trace (plus faible écart, puis plus faible recouvrement) est retournée ; `route_meta` indique le nombre d'essais. « Autre proposition » (RB-4.5) part de la graine suivant la dernière essayée.
- **RB-4.13 (seuil de recouvrement, D3)** Seuil d'avertissement **différencié** — `routing.overlap_warning_pct` (≥ 3 points, défaut 25) et `routing.overlap_warning_pct_2pts` (2 points, défaut **40**). Pour 1 point (`round_trip`), le seuil général s'applique.
- **RB-4.14 (2 points en relief ou zone pauvre en voies, D4)** Trace à 2 points restée hors tolérance après tous les essais ⇒ avertissement d'écart (RB-4.7) **complété** de « ajoute un point de passage pour une boucle plus proche de la distance voulue » ; aucune stratégie supplémentaire en bêta.
- **Critères associés** : CA-R30 [CI] hybride — provider factice où S1 n'atteint pas la tolérance ⇒ pétale S2 appelé, jamais plus de 15 appels au total ; CA-R31 [CI] essais de graine — provider factice qui n'entre dans la tolérance qu'à la 3e graine ⇒ trace retournée issue de la 3e graine, `route_meta.attempts = 3` ; budget restant insuffisant ⇒ pas d'essai supplémentaire ; CA-R32 [CI] recouvrement — trace à 2 points recouvrant à 35 % ⇒ pas d'avertissement, à 45 % ⇒ avertissement ; trace à 5 points à 30 % ⇒ avertissement ; CA-R33 [CI] — 2 points hors tolérance ⇒ avertissement contenant la suggestion d'ajout de point ; CA-R34 [spike/recette] hybride réel ≥ 87 % dans la tolérance à ≥ 2 points sur le jeu du spike (borne haute estimée : 95 %), max 15 appels.

### 5.5 Enregistrement (Q-R5 ; ADR-0009)

- **RB-5.1 (m11)** Bouton « Enregistrer dans la bibliothèque » après génération, jamais automatique. `POST /api/route-builder/save {result_id, name?}`. `name` : 1 à 200 caractères après suppression des espaces de bord, sans caractère de contrôle (sinon 400) ; défaut « Trace planifiée du JJ/MM/AAAA ». Le `<name>` du GPX est écrit par l'API XML (échappement automatique), **jamais** par concaténation de chaînes.
- **RB-5.2 (m10, m11)** Résultats gardés **en mémoire** : 30 min, 20 au plus par utilisateur **et** plafond global de 200 résultats ou 256 Mo (le premier atteint), éviction LRU. Le client ne renvoie pas le GPX. `result_id` inconnu, expiré, évincé ou d'un autre utilisateur ⇒ 404. **Dédoublonnage** sur le `result_id` : un résultat déjà enregistré garde une **référence composée** de la trace créée : `activity_id`, `user_id` et **SHA-256 du GPX enregistré** (celui calculé par l'ingestion, colonne `activities.sha256`). Un second `save` (même avec un autre `name`) ⇒ 409 avec cet id **si la trace existe encore**, c'est-à-dire si une ligne vérifie à la fois `id = activity_id`, `user_id`, `sha256` mémorisé, `kind = 'planned'` et `source = 'route_builder'` ; sinon (trace supprimée, **ou id réattribué à une autre activité** : `activities.id` n'a pas d'`AUTOINCREMENT`), le `save` **enregistre à nouveau** (nouvelle trace, nouvel id) (n7, test TR5). L'id seul n'est jamais utilisé comme preuve d'existence ; la table `activities` n'est pas reconstruite. Le dédoublonnage SHA-256 de l'ingestion reste actif en plus (même GPX octet pour octet).
- **RB-5.3** Enregistrement via `ingest_service.ingest_bytes` avec `source='route_builder'` et **`kind='planned'`** ; `start_time` = date d'enregistrement, `start_time_known=0`, `has_time=0`. Altitude corrigée en différé (P-TR5-7) : tant qu'elle n'est pas faite, `dem_pending: true` et indication dans le panneau ; une fois faite, toutes les vues et l'analyse enregistrée suivent l'altitude MNT ([altitude-mnt.md](altitude-mnt.md), CA-A1 : trace planifiée sans `<ele>`).
- **RB-5.4** Marqueur : colonne `activities.kind TEXT NOT NULL DEFAULT 'activity'`, valeurs `activity` | `planned`. Index `(user_id, kind, start_time)`. Base existante ⇒ `activity` partout.
- **RB-5.5** Impact sur l'existant (filtre explicite sur `kind`) :

| Composant | Fichier | Trace `planned` |
|---|---|---|
| Statistiques par période | `dashboard_service._rows` / `period_stats` | **Exclue** |
| Records | `dashboard_service.records` | **Exclue** |
| Meilleurs efforts | ingestion (`activity_stats`) | Non calculés (`'{}'`) |
| Recalcul des résumés (M14) | `ingest_service.recompute_summaries` (`flask recompute-stats`) | Distance, D+ et durées recalculés ; **`best_efforts` forcé à `'{}'`** pour `kind='planned'` (la requête lit `kind`) |
| Efforts sur tronçons, classements | `segment_service.match_activity`, `match_segment`, classement | **Exclue** ; créer un tronçon depuis elle reste possible |
| Liste de la bibliothèque | `activity_store.list_for_user` | Incluse, `kind` renvoyé, badge **Planifiée**, filtre **Toutes · Réalisées · Planifiées**, **défaut Toutes** (arbitré) |
| Types présents (filtre) | `activity_store.list_types` | `activity` seulement |
| Compteurs | `activity_store.count_for_user`, `nb_activities` de `/api/users` | `activity` seulement ; nombre de traces planifiées affiché à part |
| Rattrapage d'altitude | `activity_store.pending_dem` | Incluse |
| Quota | `ingest_service` | Comptée |
| Analyse avancée | `analysis_routes` | Disponible ; sections dépendant du temps vides |
| Modification | `activity_store.EDITABLE_COLUMNS` | `kind` non modifiable |

- **RB-5.6** Upload et inbox ⇒ toujours `kind='activity'`, même sans horodatage.

### 5.6 Configuration (réglages admin, section « Traces planifiées »)

| Réglage | Type | Défaut |
|---|---|---|
| ~~`routing.provider`~~ | — | **pas de réglage en v1** : GraphHopper est le seul fournisseur ; un autre adaptateur reste possible sans toucher au builder (CA-R18) (décision de coord, 2026-11-25, à relire par l'utilisateur, E-28) |
| `routing.base_url` | url `http(s)` | vide (ex. `http://graphhopper:8989`) |
| `routing.api_key` | secret | vide (inutile en auto-hébergé) |
| `routing.timeout_s` | float 1–60 | 20 (par appel) |
| `routing.total_budget_s` | float 5–120 | 30 |
| `routing.max_calls` | int 1–30 | 15 |
| `routing.detour_strategy` | enum `s1` \| `s2` \| `hybrid` | `hybrid` (D2) |
| `routing.seed_retries` | int 0–2 | 2 (D2) |
| `routing.max_waypoints` | int 2–100 | 50 |
| `routing.max_distance_km` | int 1–1000 | 300 |
| `routing.coverage_bbox` | `[lon_min, lat_min, lon_max, lat_max]` en degrés décimaux, 4 nombres finis ; lon dans [−180, 180], lat dans [−90, 90] ; min < max ; côté du rectangle ≤ 2 000 km | `[1.35, 48.83, 6.42, 51.55]` (Hauts-de-France + Belgique, contenu dans l'emprise que le moteur annonce : RB-1.3) |
| `routing.distance_tolerance_pct` | int 1–100 | 15 |
| `routing.snap_tolerance_m` | int 10–500 | 50 |
| `routing.overlap_warning_pct` | int 1–100 | 25 (1 point et ≥ 3 points) |
| `routing.overlap_warning_pct_2pts` | int 1–100 | 40 (D3) |
| `routing.rate_limit_per_user` | int 1–100 / heure | 30 |
| `routing.max_concurrent` | int 1–4 | 2 (générations simultanées sur l'instance, M9) |
| `routing.health_interval_s` | int 15–600 | 60 (M13) |
| `routing.profiles` | table, ≤ 10 entrées, valeurs dans la liste annoncée par le moteur | RB-3.1 |

Priorité base > env (ADR-0008). `routing.base_url` : URL `http(s)` ≤ 500 caractères, soumise à `OUTBOUND_ALLOWED_HOSTS` ; `routing.api_key` liée à `routing.base_url` (admin-space EF-5.5) : changer l'URL efface la clé (B3).

---

## 6. Sécurité et robustesse

- **RB-S1** Entrée stricte : JSON borné, coordonnées finies et bornées, nombre de points ≤ max, doublons consécutifs (< 5 m) retirés, emprise bornée (diagonale ≤ `max_distance_km`), activité en liste blanche, distance bornée, graine entière bornée. Violation ⇒ 400, **aucun appel sortant**.
- **RB-S1b (m12)** Dédoublonnage, trois cas tranchés :
  - les numéros renvoyés dans les erreurs et `route_meta` sont ceux **d'origine** (1-based, tels qu'envoyés) ;
  - points qui se réduisent à **un seul** après dédoublonnage sans distance cible ⇒ 400 « distance requise » (règle du cas 1 point) ;
  - **Pn à moins de 5 m de P1** ⇒ Pn retiré (la boucle se referme sur P1 de toute façon) ; signalé dans `route_meta.warnings`.
- **RB-S2** Appels au moteur uniquement côté serveur ; aucune donnée d'identification transmise.
- **RB-S3 (B3)** Destination du moteur : `routing.base_url` modifiable par un admin seulement, `http(s)` sans identifiants, **soumise à `OUTBOUND_ALLOWED_HOSTS` à l'écriture et à chaque appel** (admin-space EF-6.8) ; redirections jamais suivies ; clé éventuelle envoyée seulement si sa destination liée est la destination effective (EF-5.5).
- **RB-S4** Réponse du moteur non fiable : taille lue bornée (10 Mo, en flux), JSON borné, coordonnées validées, points ≤ `MAX_FILE_POINTS` (simplification sinon) ; anomalie ⇒ l'adaptateur lève `InvalidResponse` ⇒ 502 générique.
- **RB-S5 (M9)** Charge : une génération coûte jusqu'à `max_calls` appels et `total_budget_s`.
  - **Sémaphore dédié** aux générations (`routing.max_concurrent`, défaut 2), **non bloquant** : pas de place ⇒ **429 immédiat** « service occupé, réessaie dans un instant ». Il est distinct de `heavy_slot()`, pour qu'une génération n'occupe jamais les places des uploads et analyses.
  - `heavy_slot()` n'est pris que pour le **parse final** du GPX (`parse_gpx_cached`), après la fin des appels au moteur ; sa saturation garde son comportement existant (503 après 20 s), seul cas où une génération répond 503 hors moteur.
  - Par utilisateur : 30 générations / heure et une seule simultanée (429).
- **RB-S6** Timeouts par appel + budget total (RB-4.6) ; reprises sur 5xx limitées et décomptées du budget.
- **RB-S7** Points techniques des détours contenus dans l'emprise des points de l'utilisateur élargie de `T / 4` (jamais de point technique à l'autre bout du pays).
- **RB-S8** Logs sans coordonnées : nombre de points, distances, profil, stratégie, appels, durée, statut.
- **RB-S9** Messages d'erreur sans détail interne.
- **RB-S10** `result_id` ≥ 128 bits, lié à l'utilisateur.
- **RB-S11** Moteur (ops) : réseau interne, aucun port publié, conteneur durci, version épinglée.

## 7. API

| Méthode | Chemin | Effet |
|---|---|---|
| GET | `/api/route-builder/options` | Activités, bornes, tolérance, emprise couverte, `available` (état de santé en cache) |
| POST | `/api/route-builder/route` | `{points: [[lat, lon], …], activity, distance_km?, seed?}` ⇒ payload visualiseur + `route_meta` (noms des champs alignés sur le code livré, TR5) |
| POST | `/api/route-builder/save` | `{result_id, name?}` ⇒ trace enregistrée (`kind='planned'`) |

Codes (M9, M13) :

| Code | Cas |
|---|---|
| 200 | Succès |
| 400 | Entrée invalide (dont point hors emprise, nom invalide) |
| 404 | Fonction indisponible pour l'utilisateur, moteur **non configuré**, `result_id` inconnu |
| 409 | Résultat déjà enregistré |
| 413 | Quota de stockage atteint à l'enregistrement : message « Quota de stockage atteint : supprime des activités » (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-8 : le code livré répond 503) |
| 422 | Point non approchable, hors des données, aucune boucle possible |
| 429 | Limite par utilisateur, génération simultanée du même utilisateur, **sémaphore de génération plein** |
| 502 | Réponse du moteur invalide (`InvalidResponse`) |
| 503 | Moteur **injoignable** (état en cache ou échec à l'appel) ; saturation de `heavy_slot()` au parse final |
| 504 | Budget de temps dépassé sans aucune trace exploitable |

---

## 8. Critères d'acceptation

Les critères marqués **[CI]** se vérifient en intégration continue avec le provider factice (aucun appel réseau) ; ceux marqués **[banc]** avec le banc de test navigateur local, hors CI (admin-space EF-6.17, `web-app/tests_browser/`) : moteur du banc pour les cas nominaux, réponses de `POST /api/route-builder/route` interceptées dans le navigateur (`page.route`) pour injecter échecs et lenteurs ; ceux marqués **[spike]** se mesurent sur GraphHopper réel au spike TR0 puis en recette, et leur résultat est consigné dans l'ADR-0007 ou 0010 (m13).

| # | Critère |
|---|---|
| CA-R1 [CI] (M13) | Fonction `off`, `users` sans l'utilisateur, ou moteur **non configuré** (URL vide ou hors liste blanche) ⇒ routes 404, bouton absent. `users` avec l'utilisateur ou `all` et moteur configuré ⇒ 200. |
| CA-R2 [CI] | 0 point, 51 points, coordonnée invalide, activité inconnue, distance 0 ou 301, 1 point sans distance ⇒ 400, **aucun** appel au provider. |
| CA-R3 [CI] | 1 point + T ⇒ `round_trip` appelé ; ≥ 2 points ⇒ `route` appelé d'abord avec P1…Pn, P1 dans cet ordre. |
| CA-R4 [CI] | **Fermeture et ordre** sur provider factice : la trace commence et finit au même point ; chaque Pi est à ≤ 50 m de la trace, à des positions strictement croissantes ; vrai aussi après détours. |
| CA-R4s [spike] | Même vérification sur GraphHopper réel pour tout le jeu de cas de l'ADR-0010. |
| CA-R5 [CI] | D0 dans la tolérance ⇒ aucun détour, 1 seul appel. D0 > (1 + τ)·T ⇒ aucun détour, avertissement « distance minimale » avec D0. |
| CA-R6 [CI] | D0 < (1 − τ)·T avec un provider factice dont la distance routée est une fonction connue de la géométrie ⇒ détours insérés, ajustement convergent dans la tolérance en ≤ 4 itérations ; provider factice qui ne converge pas ⇒ meilleure trace + avertissement d'écart. |
| CA-R6s [spike] | Taux de générations dans la tolérance ≥ la cible de l'ADR-0010 (≥ 80 %) sur le jeu de cas. |
| CA-R7 [CI] | Jamais plus de `max_calls` appels ni plus de `total_budget_s` par génération (provider factice compteur, provider factice lent). |
| CA-R8 [CI] (m14) | Même graine + mêmes entrées ⇒ même trace (provider factice déterministe). Sur un cas construit qui admet plusieurs détours (≥ 3 tronçons allongeables), **10 graines différentes donnent au moins 2 traces distinctes**. |
| CA-R9 [CI] | Points techniques : absents des `wpt`, contenus dans l'emprise RB-S7. |
| CA-R10 [CI] | Taux de recouvrement calculé ; > seuil ⇒ avertissement (cas construit : aller-retour pur). |
| CA-R11 [CI] | Le payload renvoyé est identique à celui de `/api/upload` pour le même GPX. GPX : 1.1 valide, un `trk`, `wpt` = points utilisateur, `type` = type stocké (`road_bike` pour gravel), aucun `time`. |
| CA-R12 [CI] | Enregistrement : `kind='planned'`, `source='route_builder'`, `start_time_known=0`. `result_id` d'un autre utilisateur ou expiré ⇒ 404. |
| CA-R13 [CI] | Une trace planifiée enregistrée ne change ni `period_stats`, ni `records`, ni les types, ni le compteur d'activités, et ne crée aucun effort ; elle est listée avec `kind` et apparaît sous le filtre par défaut « Toutes ». |
| CA-R14 [CI] | Upload ou inbox d'un GPX sans temps ⇒ `kind='activity'`. Migration : base existante ⇒ `activity` partout, statistiques identiques. |
| CA-R15 [CI] | Erreurs provider : budget dépassé sans trace ⇒ 504 ; 5xx répétés ou injoignable ⇒ 503 ; réponse invalide ⇒ 502 ; point non approchable ⇒ 422 avec son numéro d'origine. Aucun corps ne contient l'URL ni la réponse du moteur. |
| CA-R16 [CI] (M9, v5-2) | Horloge injectée, limiteur à deux compteurs : 31e génération dans une même fenêtre fixe d'une heure ⇒ 429, et au plus 60 (2 × seuil) sur toute fenêtre glissante d'une heure ; seconde génération simultanée du même utilisateur ⇒ 429 ; génération au-delà de `max_concurrent` ⇒ **429 immédiat** (sans attente) ; pendant 2 générations lentes en cours, un upload passe dans `heavy_slot()` **sans attendre**. |
| CA-R17 [CI] (n6) | Logs sans coordonnées ; le JS du mode n'émet **aucun fetch/XHR** hors de l'origine (les tuiles de fond de carte, hors origine par nature, ne sont pas concernées). |
| CA-R18 [CI] | Ajouter un adaptateur ou une stratégie de détours ne modifie ni `builder.py` ni les routes. |
| CA-R19 [CI] | UI : aucun libellé « itinéraire » ; badge « Planifiée » sur les traces `planned`. |
| CA-R20 [CI] | Point hors de `routing.coverage_bbox` ⇒ refusé côté front et 400 « Le point N est hors de la zone couverte. » côté serveur sans appel au moteur ; point dans le rectangle mais hors données ou loin d'une voie ⇒ 422 « Le point N est trop loin d'une voie praticable : rapproche-le d'un chemin. » (RB-1.3). |
| CA-R20b [CI] (spike) | Provider factice dont `/info` annonce `[-8.32, 48.83, 6.42, 54.60]` : un point à Londres (−0,13 ; 51,51) ou à Amsterdam (4,90 ; 52,37) ⇒ **400** sans appel au moteur (couverture configurée, pas `/info`) ; Lille (3,06 ; 50,63) et Liège (5,57 ; 50,63) acceptés. `options` renvoie `routing.coverage_bbox` même cache vide. Écriture du réglage avec min ≥ max, une latitude de 95 ou un côté de 3 000 km ⇒ 400. Rectangle configuré non contenu dans celui de `/info` ⇒ signalement dans l'admin. Point technique de détour hors rectangle ⇒ jamais envoyé au moteur. |
| CA-R21 [spike] | Une boucle passant par un point en Hauts-de-France et un point en Belgique est générée, fermée, et franchit la frontière sans interruption de la trace. |
| CA-R22 [CI] (M14) | Après enregistrement d'une trace planifiée puis `flask recompute-stats` : `best_efforts` de la trace = `'{}'`, agrégats du tableau de bord, records et efforts sur tronçons **inchangés**. |
| CA-R23 [CI] (B3) | `routing.base_url` hors `OUTBOUND_ALLOWED_HOSTS` ⇒ 400 à l'écriture ; forcée en base ⇒ aucun appel sortant (serveur factice : aucune requête reçue), fonction traitée comme non configurée ; changer `routing.base_url` efface `routing.api_key`. |
| CA-R24 [CI] (M13, R3) | Sondage exécuté par `run_task('routing_health')` (sans thread) : moteur factice arrêté ⇒ `options` renvoie `available: false`, le bouton reste visible, `route` ⇒ 503 ; aucun appel au moteur pendant le rendu des pages ; moteur relancé ⇒ `available: true` au sondage suivant. |
| CA-R25 [CI] (m11) | `name` de 201 caractères ou avec caractère de contrôle ⇒ 400 ; `name` contenant `</name><wpt …>` ⇒ GPX valide, texte échappé. Deuxième `save` du même `result_id` avec un autre nom ⇒ 409 avec l'id existant, aucune seconde ligne ; après suppression de cette trace, nouveau `save` ⇒ 200, nouvelle trace (n7). |
| CA-R25b [CI] (n7, test TR5) | Trace planifiée enregistrée (id N) puis supprimée ; une autre activité du même utilisateur (upload) reçoit l'id N réattribué ⇒ nouveau `save` du résultat ⇒ **200**, nouvelle trace créée, l'activité N n'est ni renvoyée en 409 ni modifiée. Même cas avec l'id N réattribué à une activité d'un **autre** utilisateur ⇒ 200, rien n'est révélé de cette activité. |
| CA-R26 [CI] (m12) | P2 à 3 m de P1 ⇒ retiré, erreurs et `route_meta` numérotées d'après l'envoi d'origine ; deux points confondus sans distance ⇒ 400 « distance requise » ; Pn à 3 m de P1 ⇒ retiré, avertissement. |
| CA-R27 [CI] (m10) | 201e résultat en mémoire (tous utilisateurs) ⇒ le plus ancien est évincé ; son `save` ⇒ 404. |
| CA-R28 (n5) | Déconnexion ⇒ points en cours absents du `localStorage` ; un autre compte ouvrant le mode sur le même navigateur ne voit pas les points du précédent. |
| CA-R29 [CI] (n8, R3) | Instance démarrée sans aucun sondage exécuté (cache vide) ⇒ `options` : `available: false`, couverture configurée présente ; `route` ⇒ 503 sans appel au moteur. Après `run_task('routing_health')` réussi ⇒ `available: true`. Avec `INBOX_ENABLED=0` et sans `DEM_URL`, la tâche `routing_health` est bien planifiée par l'ordonnanceur (`tick(now)`). |
| CA-R35 [CI/recette] (RB-2.1, E-6) | Éditeur de points ouvert puis « Créer une trace » ⇒ l'éditeur est fermé ; constructeur ouvert puis ouverture de l'éditeur ⇒ le constructeur est fermé (points en cours conservés en `localStorage`) ; un clic sur la carte avec le constructeur ouvert ajoute **un** point de trace et n'ouvre **aucun** formulaire de point. Test de comportement dans un navigateur (pas une recherche de chaîne), ou recette manuelle à défaut. |
| CA-R36 [CI/recette] (RB-2.6, E-7) | Points en cours dans le constructeur, puis déconnexion depuis **le tableau de bord**, **l'administration**, **le profil** et **les tronçons** (une fois chacun) ⇒ aucune clé `kmlviewer.routeBuilder.*` dans le `localStorage` après la déconnexion, y compris quand la fonction bêta n'est pas disponible pour le compte. La valeur stockée ne contient que les points, l'activité et la distance. |
| CA-R37 [CI] (§7, E-8) | Quota de stockage de l'utilisateur atteint, `save` d'un résultat valide ⇒ **413** avec le message « Quota de stockage atteint : supprime des activités » ; aucune activité créée ; le résultat reste en mémoire et un `save` après libération de place ⇒ 200. |
| CA-R38 [CI] (RB-4.7, E-24) | **1 point** : provider factice dont la boucle `round_trip` commence à plus de `snap_tolerance_m` (50 m) de P1, ou ne se referme pas sur son point de départ ⇒ trace écartée comme à ≥ 2 points ; si aucune graine ne donne de trace valable ⇒ **422** « Le point 1 est trop loin d'une voie praticable : rapproche-le d'un chemin. » (ou « Aucune trace possible… » si la fermeture seule échoue) ; boucle qui part à 30 m de P1 et se referme ⇒ 200, `route_meta` avec écart et recouvrement. |
| CA-R39 [CI] (P-TR5-7, E-26) | `APP_TIMEZONE=Europe/Paris`, horloge injectée au 2026-11-25 à 23 h 30 UTC : `route` ⇒ `filename` (nom prérempli) « Trace planifiée du 26/11/2026 » ; `save` sans `name` ⇒ même date. Au 2026-11-25 à 12 h UTC ⇒ « …du 25/11/2026 ». |
| CA-R40 [banc] (RB-2.6, défaut de l'utilisateur) | 3 points, « Terminer », « Enregistrer » réussi ⇒ aucune clé `kmlviewer.routeBuilder.*` dans le `localStorage` ; rechargement puis ouverture du constructeur ⇒ **aucun point restauré**, compteur à 0 ; aucun avertissement de départ (`beforeunload`) après l'enregistrement. Même résultat quand `save` répond 409 « déjà enregistrée ». Enregistrement en échec (413, 503) ⇒ points toujours stockés. Après l'enregistrement, un point ajouté dans la page ⇒ de nouveau stocké. |
| CA-R41 [banc] (RB-2.7, déplacer) | 3 points, activité et distance 20 km, « Terminer » ⇒ résultat affiché (graine `s`). Puis distance changée à 30 dans le formulaire. Glisser P2 d'environ 1 km dans la zone couverte, à la souris : **aucune** requête pendant le glisser ; au relâchement, **un** `POST /api/route-builder/route` avec P2 déplacé, la même activité, `distance_km: 20`, `seed: s`, sans `previous_result_id`. Pendant le calcul (réponse retardée) : trace précédente toujours affichée, « Recalcul en cours… », « Enregistrer » et « Autre proposition » inactifs. Réponse ⇒ une seule trace de résultat sur la carte, résumé mis à jour, « Distance voulue : 20 km » ; aucune requête `save` ni vers `/api/activities`. Même scénario au doigt (contexte `hasTouch`) ⇒ même requête et même résultat. En édition (« Modifier les points »), glisser ⇒ point déplacé, aucune requête. |
| CA-R42 [banc] (RB-2.7, insérer) | Résultat affiché avec P1, P2, P3. Souris à 5 px de la trace, entre les positions de P2 et P3 ⇒ curseur `pointer`, point fantôme et infobulle « Clique ou glisse pour ajouter un point » ; à 20 px ⇒ rien de cela. **Clic** à 5 px de la trace entre P2 et P3 ⇒ un `POST` avec les points P1, P2, nouveau, P3, le nouveau point à la position du clic. **Appui** au même endroit, glisser de 200 px, relâchement ⇒ un `POST` avec le nouveau point à la position du relâchement, toujours entre P2 et P3 ; aucune requête pendant le glisser. Appui sur la trace après la position de P3 (fermeture) ⇒ point ajouté après P3. Au doigt (`hasTouch`) : toucher à 12 px de la trace ⇒ insertion ; glisser commencé à 12 px de la trace ⇒ insertion, la carte ne bouge pas ; glisser commencé à 40 px ⇒ la carte se déplace, aucun point, aucune requête. Appui sur la trace dans la zone de prise de P2 ⇒ c'est P2 qui est saisi (déplacement), pas d'insertion. Résultat à 1 point : appui sur la trace ⇒ le nouveau point devient P2. |
| CA-R43 [banc] (RB-2.7, ajouter et retirer) | Résultat affiché avec 3 points, distance 20 km. Clic sur la carte à 40 px de la trace ⇒ un `POST` avec P1, P2, P3, nouveau, et `distance_km: 20` (distance conservée). Glisser sur la carte loin de la trace ⇒ la carte se déplace, aucune requête. « Retirer ce point » sur P2 (menu), puis clic droit sur un point ⇒ un `POST` chacun, sans le point retiré. Retirer P1 ⇒ l'ancien P2 est numéroté 1 et envoyé en premier. Résultat à 2 points sans distance voulue : retirer un point ⇒ refusé, message « Un seul point demande une distance voulue… », aucune requête. Résultat à 1 point : « Retirer ce point » inactif. 50 points : appui sur la trace ou clic sur la carte ⇒ « 50 points au plus. », aucun point créé, aucune requête. |
| CA-R44 [banc] (RB-2.7, clavier) | Tab jusqu'à « Point 2 … » ; flèche droite trois fois ⇒ le point a bougé d'environ 30 px, aucune requête ; Entrée ⇒ un `POST`. Refait avec Échap à la place d'Entrée ⇒ point revenu, aucune requête ; refait avec Tab (perte du focus) ⇒ idem. Suppr sur « Point 2 » ⇒ un `POST` sans P2. Entrée sur « Point 2 » sans déplacement ⇒ menu ouvert, boutons atteignables par Tab ; « Ajouter un point après celui-ci » ⇒ nouveau point au milieu de P2 et P3, focalisé ; flèche haut puis Entrée ⇒ un `POST` avec le nouveau point entre P2 et P3 ; refait avec Échap ⇒ point supprimé, aucune requête. Bouton « Ajouter un point à la fin » ⇒ nouveau point entre P3 et P1, Entrée ⇒ `POST` avec le point après P3. En édition, les mêmes touches modifient les points sans requête. |
| CA-R45 [banc] (RB-2.7, échec) | Réponse interceptée 422 « Le point 2 est trop loin d'une voie praticable : rapproche-le d'un chemin. » après un déplacement ⇒ P2 revenu à sa position d'avant, `localStorage` égal à celui d'avant le geste, trace précédente active, message « Recalcul impossible : Le point 2 est trop loin… La trace précédente est conservée. » dans un `role="alert"` ; « Enregistrer » envoie le `result_id` précédent. Même retour après une insertion (nouveau point supprimé, numérotation d'avant), un ajout et un retrait (point remis à son rang). Même retour pour 429, 503, 504 et une coupure réseau. « Annuler » pendant un calcul retardé ⇒ même retour, message « Recalcul abandonné… ». Aucune nouvelle entrée d'historique après un échec. |
| CA-R46 [banc] (RB-2.7, historique) | Après un déplacement, une insertion, un ajout et un retrait réussis : quatre Ctrl+Z ⇒ chaque fois les points et la trace d'avant, **sans requête** ; quatre Ctrl+Y ⇒ l'inverse, sans requête ; « Enregistrer » après un Ctrl+Z ⇒ `save` avec le `result_id` affiché. « Tout effacer » confirmé ⇒ panneau de résultat masqué, aucune trace de résultat sur la carte, aucune requête ; Ctrl+Z ⇒ points et trace d'avant réaffichés, sans requête. Onze gestes réussis, puis onze Ctrl+Z ⇒ les dix premiers réaffichent une trace ; le onzième revient à l'édition (panneau de résultat masqué), toujours sans requête. |
| CA-R47 [banc] (RB-2.7, points limites) | Pendant un calcul retardé : glisser un point ⇒ il ne bouge pas ; survol de la trace sans indication ; appui sur la trace, clic sur la carte, flèches, Suppr et Ctrl+Z sans effet ; jamais plus d'un `POST …/route` en cours sur tout le scénario. Point déplacé, inséré (appui sur la trace puis glisser) ou ajouté, relâché hors de `coverage_bbox` ⇒ point revenu ou nouveau point supprimé, « Hors de la zone couverte. », aucune requête. Insertion relâchée à 3 m de P3 ⇒ refusée, « Trop près du point 3. », aucune requête. Déplacement de 3 m ⇒ aucune requête, aucune entrée d'historique. Un seul point : glisser P1 ⇒ `POST` avec un point et la distance du résultat affiché. |
| CA-R48 [banc] (RB-2.7, trace enregistrée intacte) | Résultat enregistré (activité N) ; glisser P2, puis insérer un point sur la trace ⇒ nouveaux résultats, la mention « Enregistrée » disparaît. `GET /api/activities/N` et son fichier : nom, `sha256` et points inchangés ; aucune requête `PATCH`, `DELETE` ni `save` pendant les gestes. « Enregistrer » ⇒ nouvelle activité N′ ≠ N ; la bibliothèque compte une trace planifiée de plus. |

## 9. Cas limites et risques

**Cas limites**
- Tous les points identiques ⇒ traité comme 1 point.
- 2 points ⇒ boucle de base = aller par un chemin, retour souvent par le même : recouvrement élevé, avertissement ; les détours tendent à l'ouvrir.
- Points très denses (tronçons courts) ⇒ peu de place pour des détours latéraux : la stratégie allonge en priorité les tronçons les plus longs, dont la fermeture.
- T bien supérieure à D0 (5 km de points pour 100 km voulus) ⇒ détours très amples ; RB-S7 et le budget bornent ; avertissement si non atteint.
- Point de l'utilisateur dans une impasse ⇒ aller-retour inévitable vers ce point.
- Zone pauvre en voies (île, vallée en cul-de-sac, littoral) ⇒ détours impossibles d'un côté : autre côté, puis abandon avec avertissement.
- Trace sortant de l'emprise OSM importée ⇒ 422. Cas typiques : points proches de la limite des Hauts-de-France côté Normandie, Île-de-France ou Grand Est ; détour qui chercherait à sortir de l'emprise (la stratégie le rabat à l'intérieur, sinon avertissement).
- Boucle franco-belge : passage de frontière autorisé ; règles d'accès différentes selon le pays (chemins agricoles, pistes cyclables obligatoires…) appliquées par le moteur d'après les données OSM, à observer au spike.
- Points posés aux abords immédiats de la frontière, sur une voie coupée entre les deux extraits (jointure de fusion défaillante) ⇒ 422 : cas à tester explicitement au spike.
- Redémarrage entre génération et enregistrement ⇒ `result_id` perdu (404, « régénère la trace »).

**Risques (mesurés au spike TR0 et sur la machine cible « dune », ADR-0007 et 0010)**

| Risque | Effet | Atténuation |
|---|---|---|
| Détours peu naturels (zigzags, éperons, aller-retour) | Trace inexploitable en vrai | Taux de recouvrement + avertissement ; choix de stratégie au spike ; « Autre proposition » |
| Non-convergence vers T | Écart à la cible | Itérations bornées, meilleure proposition + avertissement |
| Charge moteur ×15 par génération | Latence, saturation | Budget d'appels, 2 générations simultanées max, limite par utilisateur. Mesuré sur dune : `route` à 5 points p95 ≤ 378 ms par profil, donc 15 appels ≈ 6 s au pire, sous le budget de 30 s |
| Latence de plusieurs secondes | Expérience dégradée | Progression + Annuler ; budget de temps |
| Points techniques sur voies privées / interdites au profil | Trace non praticable | Le moteur ne route que les voies permises au profil ; avertissements moteur remontés |
| Qualité du profil gravel | Trace hors sujet | Modèle personnalisé évalué au spike : évite surtout le bitume sans viser le gravier (limite connue, ADR-0007) ; jugement terrain par l'utilisateur |
| Mémoire du moteur | Arrêt du service (OOM) | Mesuré sur dune : 1,2 Go après charge à `-Xmx1g`, 0 OOM ; retenu `-Xmx1536m` dans une limite de 2 Go, à vérifier à la recette de TR7 |

---

## 10. Dépendances

| Dépend de | Quoi | Bloquant |
|---|---|---|
| admin-space T5 (EF-7) | `route_builder` + `feature_required` | **Oui** |
| admin-space T14 (D4) | Activation de la bêta par l'API admin (sans elle, seulement par SQL) | **Oui** |
| admin-space T3 (EF-6) | Réglages `routing.*` bornés, version en base | **Oui** |
| admin-space T4 (M15) | `routing.api_key` est de type `secret` | **Oui** (même si la clé reste vide en auto-hébergé) |
| admin-space T18 (EF-6.8) | `OUTBOUND_ALLOWED_HOSTS` et liaison secret ↔ destination | **Oui** |
| admin-space T6 (`ratelimit.py`) | RB-S5 | **Oui** |
| admin-space T19 (R3) | Ordonnanceur de fond qui porte le sondage `routing_health` | **Oui**, avant TR5 |
| admin-space T0 (r2) | Migrations par nom, pour `activities.kind` | **Oui**, avant TR4 |
| **ops** | Service compose GraphHopper (image du projet : jar de release 11.0 épinglé sur Temurin 21), données OSM **Hauts-de-France + Belgique** (extraits fusionnés), mise à jour **mensuelle** avec retour arrière, ressources chiffrées sur dune (service 2 Go, import 6 Go de tas, ~2,4 Go de disque ; ADR-0007) | **Oui** |
| **ops** (existant DEM) | `DEM_BBOX` à élargir pour couvrir la même emprise, sans quoi les traces belges n'ont pas d'altitude MNT : proposition `DEM_BBOX=48,1,52,7` (24 dalles d'1° au plus ; ~45 Mo par dalle terrestre selon `.env.example`, soit ~1 Go au plus, les dalles marines étant absentes) | Non (altitude du moteur ou absente sinon) |
| Spike TR0 | Validation ADR-0007 et choix ADR-0010 | **Oui** avant TR2/TR3 |
| Migration `activities.kind` | ADR-0009 | Oui, pour l'enregistrement |

## 11. Décisions

- [ADR-0007](../adr/0007-moteur-routage.md) — **GraphHopper 11.0 accepté** (spike TR0, mesures sur dune) ; chiffrage d'exploitation.
- [ADR-0009](../adr/0009-itineraires-planifies.md) — `activities.kind`, valeur `planned`.
- [ADR-0010](../adr/0010-strategie-detours.md) — stratégie de détours : proposé, choix final au spike.
- [ADR-0011](../adr/0011-destinations-sortantes.md) — destinations sortantes en liste blanche, secrets liés à leur destination (B3).

## 12. Arbitrages intégrés

| Question | Arbitrage | Vague |
|---|---|---|
| Q-R2 | Moteur auto-hébergé via compose | 1 |
| Q-R3 | Gravel = profil de routage seul | 1 |
| Q-R5 | Enregistrée comme trace, exclue des stats | 1 |
| Q-R7 | Activation par liste d'utilisateurs ou tous | 1 |
| Q-R4, Q-R6, Q-R8 | Valeurs proposées par défaut | 1 |
| **Q-R1** | **Toujours une boucle fermée passant par tous les points dans l'ordre, détours ajoutés pour atteindre la distance** (remplace la v2) | 2 |
| ADR-0007 | GraphHopper accepté sous réserve du spike TR0 | 2 |
| Emprise OSM | **Hauts-de-France + Belgique**, boucles transfrontalières permises (périmètre belge recommandé : pays entier, ADR-0007) | 4 |
| Mise à jour OSM | Mensuelle par défaut | 2 |
| Liste | Traces planifiées visibles par défaut (filtre « Toutes ») | 2 |
| Vocabulaire | « Trace » + badge « Planifiée », plus d'« itinéraire » | 2 |
| Seuils | Distance facultative avec ≥ 2 points ; tolérance 15 % ; accroche 50 m ; recouvrement 25 % ; budget 15 appels / 30 s | 3 |
| Filtre | « Toutes · Réalisées · Planifiées », défaut « Toutes » | 3 |
| D1 | GraphHopper 11.0 validé sous condition des mesures cible ; image communautaire ou jar à trancher en TR7 | spike |
| — | Condition levée (mesures dune) ; TR7 : jar de release épinglé sur `eclipse-temurin:21-jre-noble`, pas l'image communautaire ; service `-Xmx1536m` / 2 Go, import `-Xmx6g` ; `routing/update.sh` avec retour arrière (décision journalisée par coord) | dune |
| D2 | Stratégie hybride S1 puis pétale S2, jusqu'à 2 essais de graine supplémentaires | spike |
| D3 | Recouvrement : avertissement à 25 % à partir de 3 points, 40 % à 2 points | spike |
| D4 | 2 points hors tolérance : avertir et suggérer d'ajouter un point | spike |
| — | Couverture = rectangle configuré `routing.coverage_bbox`, pas le rectangle de `/info` (constat d'ops au spike) | spike |
| B3 | `routing.base_url` soumise à `OUTBOUND_ALLOWED_HOSTS`, clé liée à sa destination | post-review |
| M9, M13, M14, m10–m14, n5, n6, D4 | Attendu de la review appliqué | post-review |
| R3, n7, n8, découpage | Sondage du moteur porté par l'ordonnanceur de fond (admin-space T19), cache vide ⇒ indisponible, ré-enregistrement après suppression | re-review |

## 13. Reste ouvert

1. ~~Mesures sur la machine cible (« dune »)~~ : **faites** le 2026-11-24 (ops), consignées dans l'ADR-0007, qui passe « Accepté » ; TR7 débloquée.
2. **Appréciation visuelle** des traces par l'utilisateur, en particulier gravel : échantillon prêt dans `spike/detours/out/echantillon/` (`echantillon.md` et GPX S1/S2 par zone). Une remarque défavorable rouvrirait la stratégie ou le profil concerné, sans bloquer TR3.

Décisions D1 à D4 prises le 2026-11-24 (§12).

**Ordre pour dev** : **TR3** (adaptateur GraphHopper, profils, `health()`), **puis TR2** (`detours.py` hybride S1 → S2 + essais de graine, `builder.py`, RB-4.11 à RB-4.14, CA-R30 à CA-R33) ; TR7 (ops) débloquée (mesures cible faites) ; CA-R34 (hybride réel sur le jeu du spike) à mesurer une fois TR2 et TR3 livrés.

La région n'est plus un point ouvert (arbitrée, vague 4). Le **périmètre belge** retenu par la spec est le **pays entier** (recommandation argumentée dans l'ADR-0007) ; s'il ne convient pas, c'est une simple variante de l'étape d'extraction de TR7, sans impact sur le code.

**Tranché par défaut après review, à confirmer :**
- M9 : 2 générations simultanées sur l'instance (`routing.max_concurrent`), refus immédiat en 429 au-delà.
- M13, n8 : sondage de santé toutes les 60 s par l'ordonnanceur, premier passage au démarrage ; **avant le premier sondage réussi, fonction indisponible (503)** — remplace le défaut v4 « réputé joignable ».
- n7 : après suppression de la trace enregistrée, un nouveau `save` du même résultat crée une nouvelle trace (plutôt que 404).
- m10 : plafond global des résultats en mémoire 200 / 256 Mo.
- m11 : dédoublonnage sur le `result_id` (un second enregistrement du même résultat est refusé, même renommé).
- m12 : Pn confondu avec P1 retiré ; numérotation d'origine dans les messages.
- n5 : points en cours effacés à la déconnexion.
- Interprétations de dev (DEV-ETAT.md) **confirmées par spec** le 2026-11-24 et reportées : RB-4.1 (`1 + routing.seed_retries` graines à 1 point), RB-4.8 (altitude du moteur si complète, MNT à l'enregistrement), RB-3.2 (pas de 0,5 km côté front seulement), TR5 (`routing.coverage_bbox` au registre). Seule RB-4.8 change un comportement écrit (MNT à l'enregistrement au lieu de la génération) : signalée à l'utilisateur.
- ~~RB-1.3 : défaut `[1.35, 48.80, 6.45, 51.55]`~~ : **tranché** le 2026-11-25 (décision journalisée par coord) : défaut **`[1.35, 48.83, 6.42, 51.55]`**, aligné sur le code livré (RB-1.3, §5.6). Il englobe encore des bords des Pays-Bas, du Luxembourg, d'Allemagne, du Grand Est et de Normandie, qui répondent 422 (hors données).

**Point de reprise (2026-11-24, soir)** : aucune modification en cours. Dernières livraisons : interprétations de dev confirmées, §15 (précisions TR5/TR6), mesures dune et TR7 alignées sur l'ADR-0007 « Accepté ». En attente : confirmation de la liste ci-dessus et des défauts de §15, appréciation visuelle (point 2).

## 14. Découpage en tâches

Pré-requis : spec admin **T0, T3, T4, T5, T6, T14, T18, T19** (M15, D4, R3, r2). Chaque tâche apporte sa migration **nommée** (admin-space §7, ADR-0012) ; TR4 ajoute `activities.kind` sous un nom stable.

0. **TR0 (spike, dev + ops)** GraphHopper sur l'emprise **Hauts-de-France + Belgique** (fusion des extraits, cf. TR7) : RAM, disque et durée d'import mesurés ; continuité du réseau à la frontière ; profils, modèle gravel, `round_trip` ; jeu de cas incluant au moins 2 boucles transfrontalières ; implémentation jetable des deux stratégies de détours de l'ADR-0010 sur un jeu de ~10 cas (urbain, rural, relief, littoral, transfrontalier ; 1, 2, 5 et 15 points ; T de 1,2× à 3× D0) ; mesures : écart à la cible, recouvrement, appels, latence, appréciation visuelle. Conclusions consignées dans les ADR 0007 et 0010.
1. **TR1** `routing/base.py` + provider factice déterministe.
2. **TR3** Adaptateur GraphHopper + profils + `health()` ; tests sur réponses enregistrées, aucun appel réseau en CI. Débloquée par D1 ; **avant TR2**.
3. **TR2** `routing/detours.py` (**hybride S1 → pétale S2 + essais de graine**, RB-4.11 à RB-4.14, CA-R30 à CA-R33) + `routing/builder.py` (boucle de base, ajustement, budget, contrôles qualité, dédoublonnage m12, GPX par l'API XML) ; tests CA-R2 à R11, R26. **Après TR3.**
4. **TR4** Migration `activities.kind` + filtres (`dashboard_service`, `segment_service`, `activity_store`, `/api/users`, **`recompute_summaries`**) ; tests CA-R13, R14, R22.
5. **TR5** `results.py` (plafond global, dédoublonnage, n7) + routes (CA-R16 sur le modèle à deux compteurs, v5-2) + sémaphore de génération + limitations + **tâche `routing_health` enregistrée dans l'ordonnanceur (T19)** + **réglage `routing.coverage_bbox`** au registre (type rectangle, RB-1.3, §15 P-TR5-1) ; tests CA-R1, R12, R15, R16, R17, R23, R24, R25, R25b, R27, R29. Dépend de T19.
6. **TR6** Front `route-builder.js` (édition, progression, Annuler, effacement à la déconnexion) + badge « Planifiée » et filtre de la bibliothèque ; CA-R19, R28.
7. **TR7 (ops)** Service compose `graphhopper` (profil `routing`), durcissement, doc, et chaîne de données :
   - **Téléchargement** des extraits Geofabrik couvrant les Hauts-de-France (Geofabrik découpe la France par régions : vérifier si l'extrait porte le nom « Hauts-de-France » ou les anciennes régions Nord-Pas-de-Calais et Picardie) et de la **Belgique** (extrait pays), le même jour, avec contrôle des sommes publiées.
   - **Fusion** en un seul `.osm.pbf` (`osmium merge` ou équivalent, outil dans un conteneur d'outillage, pas dans l'image de l'appli) ; contrôle de la jointure à la frontière (objets en double ou en versions différentes entre extraits).
   - **Image du moteur** construite par le projet : jar `graphhopper-web` de la **release GitHub 11.0**, épinglé, **SHA-256 vérifié** au build ; base **`eclipse-temurin:21-jre-noble`** épinglée par empreinte ; **pas** l'image communautaire du spike (jar `11.0-SNAPSHOT`). Configuration des 6 profils (dont le modèle gravel du spike) versionnée dans `routing/`.
   - **Import** GraphHopper (`-Xmx6g`, dans un conteneur d'import lancé pour la seule mise à jour) dans un dossier neuf, puis **bascule** atomique sur le nouveau graphe ; l'ancien reste disponible jusqu'au succès (pas d'interruption si l'import échoue) **et pour un retour arrière**.
   - **Mise à jour mensuelle** par **`routing/update.sh`** (tâche planifiée côté hôte, modèle `dem-tiles` : relançable sans risque), avec **retour arrière** vers le graphe précédent.
   - **`DEM_BBOX`** élargie à la même emprise (cf. §10).
   - **Ressources** (mesures dune, ADR-0007) : service `-Xmx1536m` (`ROUTING_XMX`) et `mem_limit` **2 Go** (`ROUTING_MEM_LIMIT`) ; import `-Xmx6g`, ~4 min de calcul ; disque ~2,4 Go plus un graphe (~0,3 Go) pendant la bascule ; valeurs reportées dans la doc d'exploitation.
   - **Recette de TR7** : RAM du conteneur après charge ≤ ~1,8 Go sans OOM avec `-Xmx1536m` ; banc court rejoué sur l'image du projet (latence `route` par profil, `round_trip`, 6 profils dans `/info`) ; mise à jour mensuelle de bout en bout, retour arrière compris.
8. **TR8** Doc : README (traces planifiées bêta), aide admin.

## 15. Précisions pour TR5 et TR6 (2026-11-24)

Points que la spec laissait implicites, relevés avant le développement de TR5 et TR6. Chacun porte un **défaut proposé** : dev l'applique pour ne pas s'arrêter ; l'utilisateur peut le corriger (liste à confirmer, relayée par coord). Aucun ne change un arbitrage.

**TR5 (routes, `results.py`)**
- **P-TR5-1 Type du réglage `routing.coverage_bbox`** : nouveau type de registre « rectangle », défaut `[1.35, 48.83, 6.42, 51.55]` (RB-1.3) ; en base, tableau JSON de 4 nombres ; **pas de variable d'env**, comme les autres réglages `routing.*` hormis `routing.base_url` (valeur par défaut du registre, modifiable dans l'admin). « Côté » (borne 2 000 km) = largeur mesurée le long du parallèle le plus proche de l'équateur, et hauteur le long d'un méridien, chacune en haversine. Écriture invalide ⇒ 400 (CA-R20b).
- **P-TR5-2 Rabattement des points techniques** : dans l'intersection du rectangle RB-S7 (points de l'utilisateur élargis de T/4) et de `routing.coverage_bbox` ; jamais vide, puisque les points de l'utilisateur sont dans les deux.
- **P-TR5-3 Réponse de `options`** : `{available, state: "ok"|"starting"|"unavailable", activities: [{key, label}], default_activity, min_km: 1, max_km, step_km: 0.5, max_waypoints, tolerance_pct, coverage_bbox}`. `starting` = cache vide (RB-1.2). Fonction indisponible ou moteur non configuré ⇒ 404 (RB-1.1, RB-1.2).
- **P-TR5-4 Contrat de `route`** : `seed` entier de 0 à 2³¹ − 1, facultatif (absent ⇒ tiré par le serveur). Réponse : payload du visualiseur + `route_meta` (RB-4.9) avec en plus `seed` (graine de la trace retenue), **`next_seed`** (graine qui suit la dernière essayée : le front la renvoie pour « Autre proposition », RB-4.5, RB-4.12), `attempts`, `ascent_m` / `descent_m` (`null` sans altitude, RB-4.8) et `warnings: [{code, message}]` à codes stables (`min_distance`, `target_gap`, `overlap`, `add_waypoint`, `closing_point_removed`, `engine`).
- **P-TR5-5 `routing.profiles`** : **gardé en code** (table RB-3.1) pour la bêta, pas dans le registre : sa validation dépend de la liste annoncée par le moteur, donc de l'état du cache de santé. Il entrera au registre si le besoin apparaît. *Retire une ligne de §5.6 : à confirmer.*
- **P-TR5-6 Génération en cours après « Annuler »** : la place « une génération simultanée par utilisateur » reste prise jusqu'à la fin côté serveur (budget de temps) ; une nouvelle demande pendant ce temps ⇒ 429 « une génération est encore en cours, réessaie dans quelques secondes ». Le limiteur horaire compte chaque génération **acceptée** (après validation, avant le premier appel au moteur) ; une requête refusée en 400 ne compte pas.
- **P-TR5-7 Enregistrement** : `ingest_bytes(kind='planned', source='route_builder', defer_elevation=True)` puis `wake('dem_backfill')`, comme un upload (RB-4.8) ; nom par défaut daté dans `APP_TIMEZONE`, **y compris celui que le front préremplit** (`filename` du résultat de `route`) : le code livré date à l'heure du serveur (à corriger, décision de coord, 2026-11-25, à relire par l'utilisateur, E-26, CA-R39).
- **P-TR5-8 Taille comptée pour le plafond de 256 Mo** (RB-5.2) : octets du GPX + octets du payload sérialisé en JSON, par résultat.
- **P-TR5-9 Diagnostic pour l'onglet admin** (RB-1.2, RB-1.3) : exposé à l'API admin des fonctions bêta (admin-space §15 P-T14-5) : `configured`, `available`, `checked_at`, `info_bbox`, `coverage_contained` (rectangle configuré contenu dans celui de `/info`), profils annoncés.

**TR6 (front)**
- **P-TR6-1 Présence du bouton** : rendue côté serveur. Le gabarit reçoit un drapeau « `route_builder` disponible pour cet utilisateur **et** moteur configuré » ; faux ⇒ aucun élément dans le HTML (EF-7.3). Vrai mais `available: false` ⇒ le mode s'ouvre, message « service en cours de démarrage » ou « momentanément indisponible », bouton « Terminer » désactivé.
- **P-TR6-2 Marqueurs numérotés** : `L.divIcon` dont le contenu n'est que le numéro ; aucune donnée utilisateur injectée en HTML ; aucun style ni script en ligne (CSP).
- **P-TR6-3 Champ distance** : `type=number`, `min` 1, `max` = `options.max_km`, `step` 0,5 ; obligatoire avec 1 point ; vide avec ≥ 2 points ⇒ pas de cible (RB-3.2).
- **P-TR6-4 Couverture** : rectangle `coverage_bbox` tracé en pointillés en mode édition ; clic hors rectangle ⇒ point refusé, message in-page « hors de la zone couverte ».
- **P-TR6-5 Après génération** : trace injectée comme un upload ; panneau avec distance, D+ (si présent), écart à la cible, avertissements (`message`), boutons **Enregistrer** (nom prérempli, modifiable), **Autre proposition** (envoie `next_seed`), **Modifier les points** (retour à l'édition, points conservés). Enregistrement réussi ⇒ lien vers la trace dans la bibliothèque ; 409 ⇒ « déjà enregistrée » avec le lien.
- **P-TR6-6 Stockage local** : clé `kmlviewer.routeBuilder.v1.<id du compte>` ; à l'envoi du formulaire de déconnexion, **sur toute page** (script commun), suppression de toutes les clés de préfixe `kmlviewer.routeBuilder.` ; seuls les points, l'activité et la distance sont stockés (RB-2.6) ; à l'ouverture du mode, suppression des clés d'un autre compte (RB-2.6). Piles annuler/rétablir en mémoire seulement, 100 états au plus.
- **P-TR6-7 Bibliothèque** : badge « Planifiée » et filtre « Toutes · Réalisées · Planifiées » sur la liste servie par `activity_store.list_for_user` (RB-5.5), défaut « Toutes ».
