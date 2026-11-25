# Altitude MNT : affichage et analyse d'une trace corrigée

Statut : **v1.1, 2026-11-25** (v1.1 : question ouverte tranchée, garde de course retenue par coord) · Rôle : spec · Demandée par coord après un défaut trouvé par test (diagnostic `dem-report.json`, scratchpad de test, 25/11).
Specs liées : [gpx-route-builder](gpx-route-builder.md) (RB-4.8, RB-5.3, P-TR5-7) ; [admin-space](admin-space.md) (§5.10, réglages `DEM_*`) ; [conformite](conformite.md).

---

## 1. Contexte

Une activité peut recevoir une altitude corrigée par le MNT. Deux chemins y mènent :
- l'ingestion synchrone ;
- la tâche de fond `dem_backfill`, qui reprend les imports différés (`defer_elevation=True`) : uploads et traces planifiées (P-TR5-7).

La correction écrit `ele_dem` dans les points stockés, `elev_source = 'dem'`, `dem_status = 'ok'`, ainsi que `ascent_m` et `descent_m`. Le fichier d'origine n'est jamais modifié.

**Défaut constaté** sur une trace planifiée de 20 km, dont le GPX n'a pas d'`<ele>`. Après correction, la bibliothèque et `/track` ont bien l'altitude MNT (D+ ≈ 1 065 m, 428 valeurs distinctes). Pourtant :
- `GET /api/activities/<id>/parsed` relit le fichier brut, et toutes les altitudes y valent 0 ;
- le panneau de gauche affiche D+ « — » ; le profil est masqué ; il n'y a pas d'altitude sous le curseur ; les analyses que la page calcule à partir de `/parsed` n'ont pas d'altitude. C'est vrai après correction, même à la réouverture ;
- une analyse enregistrée **avant** la correction reste servie telle quelle (`has_elevation: false`) jusqu'à un nouveau calcul manuel.

Ce document est **général** : il vaut pour toute activité corrigée, upload compris, pas seulement pour les traces planifiées.

## 2. Exigences

- **EA-1 Une seule altitude par activité, partout.** Quand `elev_source = 'dem'` et que `ele_dem` est présent, **toutes** les vues d'une activité enregistrée montrent l'altitude MNT des points de trace :
  - `GET /api/activities/<id>/track` : déjà le cas ;
  - `GET /api/activities/<id>/parsed` : l'altitude MNT est **superposée au parse en mémoire**, sans modifier le fichier brut. Sont concernés la 3e coordonnée des polylignes et l'altitude des marqueurs de points de trace (`trkpt_*`), avec `altitude`, `parsed_info.altitude_m` et `parsed_info.altitude_ft` cohérents ;
  - par voie de conséquence, tout calcul que la page fait à partir de `/parsed` (D+ du panneau, profil, altitude sous le curseur, analyses `/api/analysis/*`) utilise l'altitude MNT ;
  - les points qui ne sont pas des points de trace (waypoints `<wpt>`, P1…Pn d'une trace planifiée) gardent l'altitude du fichier, ou n'en ont pas.
- **EA-2 Alignement ou rien.** `ele_dem` est aligné point pour point sur les points stockés. Il est extrait par le même parcours et le même filtre que `activity_stats.extract_track`.
  - Si le parse ne donne pas exactement le même nombre de points de trace, la superposition n'est **pas** faite : le parse est rendu tel quel, avec une ligne `WARNING` qui donne les deux nombres, sans coordonnée.
  - Jamais de superposition partielle ni décalée.
- **EA-3 Cohérence des chiffres.** Après correction, le D+ et le D− du panneau de gauche sont ceux de la bibliothèque (`ascent_m`, `descent_m`), au mètre près pour l'arrondi d'affichage. Le profil et l'altitude sous le curseur viennent des mêmes valeurs `ele_dem`.
- **EA-4 Correction pas encore faite.** Tant que la correction est attendue, `/parsed` porte `dem_pending: true`. La correction est attendue quand :
  - `elev_source != 'dem'` ;
  - `dem_status` est `NULL` ;
  - le MNT est configuré et autorisé.

  Dans ce cas, le panneau affiche « altitude en cours de calcul, rouvre la trace dans un instant » à la place d'un D+ trompeur.

  `dem_pending` vaut `false` dans tous les autres cas : correction faite, `dem_status = 'no_coverage'` (hors dalles), MNT non configuré ou hors liste blanche. La page ne se recharge pas d'elle-même : la mise à jour automatique est hors v1.
- **EA-5 Analyse enregistrée à jour.**
  - **Invalidation :** une correction d'altitude (tâche de fond ou `POST /api/activities/<id>/elevation`) **supprime l'analyse enregistrée** de l'activité, dans la **même transaction** que l'écriture de l'altitude. `GET …/analysis` rend alors `analysis: null`, comme pour une analyse jamais lancée, et le prochain `POST …/analysis` la calcule avec l'altitude MNT. C'est le choix de dev, et il suffit.
  - **Course (retenue par coord, 2026-11-25 ; dev l'implémente) :** une analyse calculée sur l'altitude d'**avant** la correction n'est jamais enregistrée **après** elle. Ce cas arrive avec un `POST …/analysis` lancé pendant que la tâche de fond corrige. `compute` n'écrit son résultat que si l'état d'altitude de l'activité (`elev_source`, `dem_status`) est celui qu'il a lu au départ. Sinon, il rend le résultat sans l'enregistrer ; la demande suivante recalcule.
- **EA-6 Rien d'autre ne change.**
  - Le fichier d'origine n'est jamais modifié ; le téléchargement rend le fichier d'origine.
  - Le D+ GPS (`ascent_gps_m`) est conservé.
  - Aucun appel au MNT à l'ouverture d'une trace : la superposition lit les points stockés.
  - `/parsed` garde le sémaphore des traitements lourds (`heavy_slot`).

## 3. Critères d'acceptation

Ces critères se testent avec un MNT factice : un serveur local qui rend des altitudes connues et variées, ouvert ou fermé à la demande, comme dans le diagnostic de test.

| # | Critère |
|---|---|
| CA-A1 (EA-1, EA-3) | Trace planifiée de plusieurs centaines de points, GPX sans `<ele>`, enregistrée avec correction différée, MNT fermé. Ouverture ⇒ `dem_pending: true` et mention « altitude en cours de calcul » dans le panneau. Ouvrir le MNT et lancer `run_task('dem_backfill')` ⇒ `elev_source = 'dem'`. **Réouverture** ⇒ `/parsed` : altitudes des polylignes et des marqueurs `trkpt_*` égales à `ele_dem`, point pour point, et plus d'une valeur distincte ; `dem_pending: false`. Page : D+ et D− du panneau égaux à `ascent_m` et `descent_m` de la bibliothèque, arrondis ; profil affiché (section visible, courbe non plate) ; altitude sous le curseur égale à `ele_dem` du point survolé. Les waypoints P1…Pn n'ont pas d'altitude MNT. |
| CA-A2 (EA-1) | Upload d'un GPX **avec** `<ele>` (altitudes GPS connues), correction différée puis faite ⇒ `/parsed` rend les altitudes MNT, pas les altitudes GPS. Le fichier téléchargé est octet pour octet celui d'origine. `ascent_gps_m` est inchangé. Même résultat pour une correction manuelle (`POST …/elevation`). |
| CA-A3 (EA-2) | `ele_dem` stocké avec un point de moins que le parse (donnée forgée en base) ⇒ `/parsed` rend les altitudes du fichier, sans erreur, et une seule ligne `WARNING` avec les deux nombres, sans coordonnée. Fichier à plusieurs traces (`breaks`) ⇒ chaque trace reçoit ses propres valeurs, sans décalage à la jonction. |
| CA-A4 (EA-4) | `dem_pending` vaut `false` dans ces cas : MNT non configuré (`DEM_URL` vide) ; `DEM_URL` hors `OUTBOUND_ALLOWED_HOSTS` ; `dem_status = 'no_coverage'` ; correction faite. Il vaut `true` seulement quand une correction est attendue. Aucun appel au MNT pendant `GET …/parsed` ni `GET …/track` (compteur du MNT factice inchangé). |
| CA-A5 (EA-5) | Analyse enregistrée **avant** la correction (`has_elevation: false`), puis correction par la tâche de fond ⇒ `GET …/analysis` rend `analysis: null`. Le `POST …/analysis` suivant rend `has_elevation: true` et `elevation_source: 'dem'`, avec un D+ égal à `ascent_m` de la bibliothèque (arrondi), et `computed_at` postérieur à la correction. Même résultat pour une correction manuelle. La suppression et l'écriture de l'altitude se font dans une seule transaction : un échec injecté entre les deux ne laisse ni l'une ni l'autre. |
| CA-A6 (EA-5, course) | Calcul d'analyse suspendu (crochet injecté) après la lecture des points ; correction faite pendant la suspension ; reprise ⇒ le résultat est rendu à l'appelant mais **pas enregistré** ; `GET …/analysis` ⇒ `analysis: null`. Sans correction pendant la suspension ⇒ résultat enregistré. |

**Vérification dans un navigateur.** Le D+ du panneau, le profil et l'altitude sous le curseur (CA-A1) se vérifient avec le banc de test navigateur local, hors CI (admin-space EF-6.17), joué par `test`. La recette manuelle reste en complément.

## 4. Décisions

- **Point sans altitude dans le fichier** (tranché par coord, 2026-11-25) : sans correction MNT, un point de trace sans `<ele>` ressort de `/parsed` avec l'altitude **0**, pas `null` (diagnostic : 500 points, tous à 0). **Hors périmètre de ce correctif, et non modifié** : le parser commun touche toutes les vues. Aucun critère de ce document ne l'exige ni ne le teste. Il reste une limite connue : sans MNT, une analyse faite sur un tel fichier peut lire un relief plat à 0 m.
- **Garde de course** (EA-5, CA-A6) : **retenue par coord** ; dev l'implémente.
