# Spec — Générateur de traces planifiées (bêta)

Statut : **v6, validée par l'utilisateur, en développement** (re-review v5 intégrée) ; arbitrée sauf résultats du spike TR0 (§13) · Mise à jour : 2026-11-24 · Rôle : spec
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

- Navigation guidée ; temps estimé ; routage en direct à chaque point (génération sur « Terminer » seulement).
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
  L'état de santé est sondé par la tâche `routing_health` de l'**ordonnanceur de fond** (admin-space §5.10, R3), qui tourne dans toute instance servie, inbox et DEM activés ou non : `health()` du provider, **premier passage au démarrage**, puis toutes les 60 s (réglage 15–600 s). Le résultat (état, emprise, date) est mis en cache ; aucune page ni requête utilisateur ne déclenche de sondage synchrone. Dernier état et date affichés dans l'onglet admin « Fonctions bêta ».
  **Cache vide** (démarrage, moteur jamais joint, n8) ⇒ fonction traitée comme **injoignable** : `options` renvoie `available: false` et aucune emprise, le front affiche « service en cours de démarrage » et ne permet pas de générer ; `route` répond 503. Aucun contrôle d'emprise n'est donc nécessaire sans cache.
- **RB-1.3** Couverture : `GET /api/route-builder/options` renvoie l'emprise couverte (rectangle englobant annoncé par le moteur au dernier sondage réussi). Le front l'affiche en mode édition (contour discret) et refuse localement un point hors emprise ; le serveur refait le contrôle (400, aucun appel au moteur). Un point dans le rectangle mais hors des données réelles (ex. Pays-Bas, Luxembourg, Grand Est) ⇒ 422 « hors de la zone couverte ».

### 5.2 Édition des points (front)

- **RB-2.1** Entrée « Créer une trace » dans la barre d'outils du visualiseur ; un mode actif à la fois.
- **RB-2.2** Clic/tap ⇒ ajoute un point à la fin ; glisser ⇒ déplace ; clic sur un point ⇒ supprimer / insérer avant ; annuler / rétablir (Ctrl+Z / Ctrl+Y et boutons) ; « Tout effacer » avec confirmation **in-page**.
- **RB-2.3** Points numérotés dans l'ordre de passage ; le point 1 est le départ **et** l'arrivée. La prévisualisation montre le segment de fermeture (dernier point → point 1).
- **RB-2.4** 1 à **50** points (réglage, plafond dur 100), contrôle front et serveur.
- **RB-2.5** Prévisualisation à vol d'oiseau (pointillés, boucle fermée) et sa longueur, calculées localement.
- **RB-2.6 (n5)** Avertissement in-page avant de quitter avec des points non générés ; points en cours en `localStorage` (try/catch), rien d'autre. Ces positions sont **effacées à la déconnexion** (formulaire de logout) et quand l'utilisateur courant change (clé de stockage suffixée par l'identifiant de compte ; une clé d'un autre compte est supprimée à l'ouverture du mode).

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

  Gravel = profil de routage seul ; `activity_types` inchangé. Noms de profils fixés au spike TR0. Défaut : `default_activity_type` du profil s'il correspond, sinon course à pied.
- **RB-3.2** **Distance approximative** : km, 1 à 300 (réglage), pas de 0,5. **Obligatoire avec 1 point** ; facultative avec ≥ 2 points (sans elle, boucle de base sans détour ; arbitré).
- **RB-3.3** Validation serveur (liste blanche, bornes).

### 5.4 Génération — arbitrage Q-R1 v3 : boucle fermée, points dans l'ordre, détours

Notations : P1…Pn les points de l'utilisateur, T la distance cible, τ la tolérance (réglage, défaut 15 %).

- **RB-4.1 — 1 point.** Boucle native du moteur au départ de P1, longueur ≈ T (`round_trip`). Hors tolérance ⇒ nouvelles graines dans la limite du budget (RB-4.6), puis meilleure proposition.
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
- **RB-4.8 — Sortie.** GPX 1.1, un `<trk>` / un `<trkseg>`, `<name>` « Trace planifiée du JJ/MM/AAAA » (modifiable à l'enregistrement), `<type>` = type stocké, **aucun horodatage**, P1…Pn en `<wpt>`. Altitude : DEM local si configuré, sinon celle du moteur, sinon absente. Le GPX passe par `parse_gpx_cached` et revient sous la même forme qu'un upload.
- **RB-4.9 — `route_meta`** : distance obtenue, D+/D−, cible, écart, D0, nombre de détours, taux de recouvrement, appels consommés, graine, profil, avertissements du moteur, `result_id`.
- **RB-4.10 — Attente.** Une génération peut durer plusieurs secondes : indicateur de progression et bouton « Annuler » (le front abandonne la requête ; le serveur respecte son budget de temps quoi qu'il arrive).

### 5.5 Enregistrement (Q-R5 ; ADR-0009)

- **RB-5.1 (m11)** Bouton « Enregistrer dans la bibliothèque » après génération, jamais automatique. `POST /api/route-builder/save {result_id, name?}`. `name` : 1 à 200 caractères après suppression des espaces de bord, sans caractère de contrôle (sinon 400) ; défaut « Trace planifiée du JJ/MM/AAAA ». Le `<name>` du GPX est écrit par l'API XML (échappement automatique), **jamais** par concaténation de chaînes.
- **RB-5.2 (m10, m11)** Résultats gardés **en mémoire** : 30 min, 20 au plus par utilisateur **et** plafond global de 200 résultats ou 256 Mo (le premier atteint), éviction LRU. Le client ne renvoie pas le GPX. `result_id` inconnu, expiré, évincé ou d'un autre utilisateur ⇒ 404. **Dédoublonnage** sur le `result_id` : un résultat déjà enregistré garde l'id de la trace créée ; un second `save` (même avec un autre `name`) ⇒ 409 avec cet id **si la trace existe encore** ; si elle a été supprimée entre-temps, le `save` **enregistre à nouveau** (nouvelle trace, nouvel id) (n7). Le dédoublonnage SHA-256 de l'ingestion reste actif en plus (même GPX octet pour octet).
- **RB-5.3** Enregistrement via `ingest_service.ingest_bytes` avec `source='route_builder'` et **`kind='planned'`** ; `start_time` = date d'enregistrement, `start_time_known=0`, `has_time=0`.
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
| `routing.provider` | enum | `graphhopper` |
| `routing.base_url` | url `http(s)` | vide (ex. `http://graphhopper:8989`) |
| `routing.api_key` | secret | vide (inutile en auto-hébergé) |
| `routing.timeout_s` | float 1–60 | 20 (par appel) |
| `routing.total_budget_s` | float 5–120 | 30 |
| `routing.max_calls` | int 1–30 | 15 |
| `routing.detour_strategy` | enum | fixé au spike (ADR-0010) |
| `routing.max_waypoints` | int 2–100 | 50 |
| `routing.max_distance_km` | int 1–1000 | 300 |
| `routing.distance_tolerance_pct` | int 1–100 | 15 |
| `routing.snap_tolerance_m` | int 10–500 | 50 |
| `routing.overlap_warning_pct` | int 1–100 | 25 |
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
| POST | `/api/route-builder/route` | `{waypoints: [[lat, lon], …], activity, target_km?, seed?}` ⇒ payload visualiseur + `route_meta` |
| POST | `/api/route-builder/save` | `{result_id, name?}` ⇒ trace enregistrée (`kind='planned'`) |

Codes (M9, M13) :

| Code | Cas |
|---|---|
| 200 | Succès |
| 400 | Entrée invalide (dont point hors emprise, nom invalide) |
| 404 | Fonction indisponible pour l'utilisateur, moteur **non configuré**, `result_id` inconnu |
| 409 | Résultat déjà enregistré |
| 413 | Quota de stockage atteint (comportement d'ingestion existant) |
| 422 | Point non approchable, hors des données, aucune boucle possible |
| 429 | Limite par utilisateur, génération simultanée du même utilisateur, **sémaphore de génération plein** |
| 502 | Réponse du moteur invalide (`InvalidResponse`) |
| 503 | Moteur **injoignable** (état en cache ou échec à l'appel) ; saturation de `heavy_slot()` au parse final |
| 504 | Budget de temps dépassé sans aucune trace exploitable |

---

## 8. Critères d'acceptation

Les critères marqués **[CI]** se vérifient en intégration continue avec le provider factice (aucun appel réseau) ; ceux marqués **[spike]** se mesurent sur GraphHopper réel au spike TR0 puis en recette, et leur résultat est consigné dans l'ADR-0007 ou 0010 (m13).

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
| CA-R20 [CI] | Point hors du rectangle de couverture ⇒ refusé côté front et 400 côté serveur sans appel au moteur ; point dans le rectangle mais hors données ⇒ 422 « hors de la zone couverte ». |
| CA-R21 [spike] | Une boucle passant par un point en Hauts-de-France et un point en Belgique est générée, fermée, et franchit la frontière sans interruption de la trace. |
| CA-R22 [CI] (M14) | Après enregistrement d'une trace planifiée puis `flask recompute-stats` : `best_efforts` de la trace = `'{}'`, agrégats du tableau de bord, records et efforts sur tronçons **inchangés**. |
| CA-R23 [CI] (B3) | `routing.base_url` hors `OUTBOUND_ALLOWED_HOSTS` ⇒ 400 à l'écriture ; forcée en base ⇒ aucun appel sortant (serveur factice : aucune requête reçue), fonction traitée comme non configurée ; changer `routing.base_url` efface `routing.api_key`. |
| CA-R24 [CI] (M13, R3) | Sondage exécuté par `run_task('routing_health')` (sans thread) : moteur factice arrêté ⇒ `options` renvoie `available: false`, le bouton reste visible, `route` ⇒ 503 ; aucun appel au moteur pendant le rendu des pages ; moteur relancé ⇒ `available: true` au sondage suivant. |
| CA-R25 [CI] (m11) | `name` de 201 caractères ou avec caractère de contrôle ⇒ 400 ; `name` contenant `</name><wpt …>` ⇒ GPX valide, texte échappé. Deuxième `save` du même `result_id` avec un autre nom ⇒ 409 avec l'id existant, aucune seconde ligne ; après suppression de cette trace, nouveau `save` ⇒ 200, nouvelle trace (n7). |
| CA-R26 [CI] (m12) | P2 à 3 m de P1 ⇒ retiré, erreurs et `route_meta` numérotées d'après l'envoi d'origine ; deux points confondus sans distance ⇒ 400 « distance requise » ; Pn à 3 m de P1 ⇒ retiré, avertissement. |
| CA-R27 [CI] (m10) | 201e résultat en mémoire (tous utilisateurs) ⇒ le plus ancien est évincé ; son `save` ⇒ 404. |
| CA-R28 (n5) | Déconnexion ⇒ points en cours absents du `localStorage` ; un autre compte ouvrant le mode sur le même navigateur ne voit pas les points du précédent. |
| CA-R29 [CI] (n8, R3) | Instance démarrée sans aucun sondage exécuté (cache vide) ⇒ `options` : `available: false`, aucune emprise ; `route` ⇒ 503 sans appel au moteur. Après `run_task('routing_health')` réussi ⇒ `available: true` et emprise présente. Avec `INBOX_ENABLED=0` et sans `DEM_URL`, la tâche `routing_health` est bien planifiée par l'ordonnanceur (`tick(now)`). |

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

**Risques (à mesurer au spike TR0)**

| Risque | Effet | Atténuation |
|---|---|---|
| Détours peu naturels (zigzags, éperons, aller-retour) | Trace inexploitable en vrai | Taux de recouvrement + avertissement ; choix de stratégie au spike ; « Autre proposition » |
| Non-convergence vers T | Écart à la cible | Itérations bornées, meilleure proposition + avertissement |
| Charge moteur ×15 par génération | Latence, saturation | Budget d'appels, 2 générations simultanées max, limite par utilisateur |
| Latence de plusieurs secondes | Expérience dégradée | Progression + Annuler ; budget de temps |
| Points techniques sur voies privées / interdites au profil | Trace non praticable | Le moteur ne route que les voies permises au profil ; avertissements moteur remontés |
| Qualité du profil gravel | Trace hors sujet | Modèle personnalisé évalué au spike |

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
| **ops** | Service compose GraphHopper, données OSM **Hauts-de-France + Belgique** (extraits fusionnés), mise à jour **mensuelle**, ressources chiffrées au spike | **Oui** |
| **ops** (existant DEM) | `DEM_BBOX` à élargir pour couvrir la même emprise, sans quoi les traces belges n'ont pas d'altitude MNT : proposition `DEM_BBOX=48,1,52,7` (24 dalles d'1° au plus ; ~45 Mo par dalle terrestre selon `.env.example`, soit ~1 Go au plus, les dalles marines étant absentes) | Non (altitude du moteur ou absente sinon) |
| Spike TR0 | Validation ADR-0007 et choix ADR-0010 | **Oui** avant TR2/TR3 |
| Migration `activities.kind` | ADR-0009 | Oui, pour l'enregistrement |

## 11. Décisions

- [ADR-0007](../adr/0007-moteur-routage.md) — **GraphHopper accepté**, sous réserve du spike TR0.
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
| B3 | `routing.base_url` soumise à `OUTBOUND_ALLOWED_HOSTS`, clé liée à sa destination | post-review |
| M9, M13, M14, m10–m14, n5, n6, D4 | Attendu de la review appliqué | post-review |
| R3, n7, n8, découpage | Sondage du moteur porté par l'ordonnanceur de fond (admin-space T19), cache vide ⇒ indisponible, ré-enregistrement après suppression | re-review |

## 13. Reste ouvert

1. **Résultats du spike TR0**, sur l'emprise Hauts-de-France + Belgique : levée de la réserve sur GraphHopper (critères de l'ADR-0007, dont RAM, disque et durée d'import chiffrés et fusion transfrontalière vérifiée) et choix de la stratégie de détours (ADR-0010, cible ≥ 80 % de générations dans la tolérance). **Bloquant pour TR2, TR3 et TR7** ; TR1, TR4, TR5 et TR6 peuvent avancer avec le provider factice.

La région n'est plus un point ouvert (arbitrée, vague 4). Le **périmètre belge** retenu par la spec est le **pays entier** (recommandation argumentée dans l'ADR-0007) ; s'il ne convient pas, c'est une simple variante de l'étape d'extraction de TR7, sans impact sur le code.

**Tranché par défaut après review, à confirmer :**
- M9 : 2 générations simultanées sur l'instance (`routing.max_concurrent`), refus immédiat en 429 au-delà.
- M13, n8 : sondage de santé toutes les 60 s par l'ordonnanceur, premier passage au démarrage ; **avant le premier sondage réussi, fonction indisponible (503)** — remplace le défaut v4 « réputé joignable ».
- n7 : après suppression de la trace enregistrée, un nouveau `save` du même résultat crée une nouvelle trace (plutôt que 404).
- m10 : plafond global des résultats en mémoire 200 / 256 Mo.
- m11 : dédoublonnage sur le `result_id` (un second enregistrement du même résultat est refusé, même renommé).
- m12 : Pn confondu avec P1 retiré ; numérotation d'origine dans les messages.
- n5 : points en cours effacés à la déconnexion.

## 14. Découpage en tâches

Pré-requis : spec admin **T0, T3, T4, T5, T6, T14, T18, T19** (M15, D4, R3, r2). Chaque tâche apporte sa migration **nommée** (admin-space §7, ADR-0012) ; TR4 ajoute `activities.kind` sous un nom stable.

0. **TR0 (spike, dev + ops)** GraphHopper sur l'emprise **Hauts-de-France + Belgique** (fusion des extraits, cf. TR7) : RAM, disque et durée d'import mesurés ; continuité du réseau à la frontière ; profils, modèle gravel, `round_trip` ; jeu de cas incluant au moins 2 boucles transfrontalières ; implémentation jetable des deux stratégies de détours de l'ADR-0010 sur un jeu de ~10 cas (urbain, rural, relief, littoral, transfrontalier ; 1, 2, 5 et 15 points ; T de 1,2× à 3× D0) ; mesures : écart à la cible, recouvrement, appels, latence, appréciation visuelle. Conclusions consignées dans les ADR 0007 et 0010.
1. **TR1** `routing/base.py` + provider factice déterministe.
2. **TR2** `routing/detours.py` (stratégie retenue) + `routing/builder.py` (boucle de base, ajustement, budget, contrôles qualité, dédoublonnage m12, GPX par l'API XML) ; tests CA-R2 à R11, R26.
3. **TR3** Adaptateur GraphHopper + profils + `health()` ; tests sur réponses enregistrées, aucun appel réseau en CI.
4. **TR4** Migration `activities.kind` + filtres (`dashboard_service`, `segment_service`, `activity_store`, `/api/users`, **`recompute_summaries`**) ; tests CA-R13, R14, R22.
5. **TR5** `results.py` (plafond global, dédoublonnage, n7) + routes (CA-R16 sur le modèle à deux compteurs, v5-2) + sémaphore de génération + limitations + **tâche `routing_health` enregistrée dans l'ordonnanceur (T19)** ; tests CA-R1, R12, R15, R16, R17, R23, R24, R25, R27, R29. Dépend de T19.
6. **TR6** Front `route-builder.js` (édition, progression, Annuler, effacement à la déconnexion) + badge « Planifiée » et filtre de la bibliothèque ; CA-R19, R28.
7. **TR7 (ops)** Service compose `graphhopper` (profil `routing`), durcissement, doc, et chaîne de données :
   - **Téléchargement** des extraits Geofabrik couvrant les Hauts-de-France (Geofabrik découpe la France par régions : vérifier si l'extrait porte le nom « Hauts-de-France » ou les anciennes régions Nord-Pas-de-Calais et Picardie) et de la **Belgique** (extrait pays), le même jour, avec contrôle des sommes publiées.
   - **Fusion** en un seul `.osm.pbf` (`osmium merge` ou équivalent, outil dans un conteneur d'outillage, pas dans l'image de l'appli) ; contrôle de la jointure à la frontière (objets en double ou en versions différentes entre extraits).
   - **Import** GraphHopper dans un dossier neuf, puis **bascule** atomique sur le nouveau graphe ; l'ancien reste disponible jusqu'au succès (pas d'interruption si l'import échoue).
   - **Mise à jour mensuelle** (tâche planifiée côté hôte, modèle `dem-tiles` : relançable sans risque).
   - **`DEM_BBOX`** élargie à la même emprise (cf. §10).
   - Ressources (RAM d'import, RAM de service, disque, durée) : valeurs mesurées au spike TR0, reportées dans la doc et dans `mem_limit`.
8. **TR8** Doc : README (traces planifiées bêta), aide admin.
