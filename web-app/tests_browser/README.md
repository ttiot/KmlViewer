# Banc de test navigateur (optionnel, local)

Des défauts visuels (popup d'état vide qui capte les clics, JSON brut dans l'administration) ont échappé à tous les tests : ce banc ouvre
**l'application réelle dans un vrai Chromium**, déroule des scénarios et **photographie** chaque étape.

**Jamais dans la CI** : `pytest` ignore ce dossier (`norecursedirs` dans `pytest.ini`), la CI Python n'a ni Docker-dans-Docker ni navigateur.

## Commande unique

```bash
web-app/tests_browser/run.sh
```

Quatre instances de l'application sont démarrées : mode `local` sur 5277 (moteur de routage réel), mode `oidc` de test sur 5279, **page Réglages** sur 5281
(registre v1, état exact du CA-72) et **registre de test** sur 5283 (réglages `list`, `requires_restart` et `secret`, variable `DEM_TIMEOUT=abc`).
Captures et résultats dans `~/.cache/kmlviewer-browser/<horodatage>/` (**hors dépôt**, `run.sh` refuse un dossier dans le dépôt) :
`01` à `08` (visualiseur, constructeur, administration, connexion oidc, bandeau SMTP, confirmation du secret), `09` à `23` (page Réglages), `resultat.json`
(résultats **et liste des requêtes interceptées**), `application*.log` (et `ECHEC-*.png` au premier écart). Code de sortie non nul au moindre échec.

Variables (facultatives) : `BENCH_OUT` (dossier de sortie), `BENCH_PORT` (5277), `BENCH_OIDC_PORT` (5279), `BENCH_SETTINGS_PORT` (5281),
`BENCH_REGISTRY_PORT` (5283), `BENCH_ENGINE_URL` (moteur de routage, `http://127.0.0.1:8989`).

**Aucune sortie réseau** : toute requête du navigateur vers un autre hôte que les applications de test est interceptée (`page.route`), enregistrée puis
bloquée ; les tuiles des hôtes de tuiles reçoivent une image locale de 1 pixel. La liste (`intercepted_requests`) est dans `resultat.json`.

## Ce qui tourne

| Élément | Détail |
|---|---|
| Application | `python:3.15-slim` **épinglée par digest** (`Dockerfile.app`), dépendances de `requirements.txt` **avec hachages** (comme la CI), code monté en lecture seule, gunicorn, mode `local` (aucune authentification : le compte « local » est administrateur), base **jetable** (`tmpfs`), réseau hôte |
| Moteur de routage | le GraphHopper local `127.0.0.1:8989` (à démarrer avant ; `run.sh` le vérifie), déclaré dans la base par `seed.py` avec `OUTBOUND_ALLOWED_HOSTS=routing=127.0.0.1:8989` |
| Préparation | `seed.py` : fonction bêta « Créer une trace » ouverte à tous, carte centrée sur Lille (dans la zone couverte), quelques lignes d'audit |
| Navigateur | image officielle `mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031` **épinglée par version ET digest** (`Dockerfile.playwright`, publiée le 2026-10-22), client Python `playwright==1.64.1` (même version que les navigateurs de l'image), installé avec `pip install --require-hashes -r requirements.txt` (empreintes) |

Les deux images locales (`kmlviewer-bench-app:<hachage de requirements.txt>` et `kmlviewer-bench-playwright:1.64.1`) sont construites au premier lancement.

## Scénarios (`scenarios.py`)

- **(a) Créer une trace** : sans fichier, `#emptyState` est visible ; ouverture de « Créer une trace » ⇒ il est masqué ; le centre de l'ancien
  popup tombe sur la carte (`elementFromPoint`) ; un clic à cet endroit pose **un point** (`1 point`, un marqueur) ; fermeture ⇒ le popup revient.
- **(b) /admin** : diagnostic du moteur lisible (pastille d'état, « Vérifié … » en heure locale, zone, étendue, profils ; aucun JSON, aucun epoch) ;
  journal d'audit lisible (colonnes et détails, jamais `[object Object]`).
- **(d) Page de connexion en mode oidc** (deuxième instance, IdP factice jamais joint, comptes à mot de passe disponibles) : UNE page avec le bouton
  « Se connecter avec le SSO » (qui garde `next`) et le formulaire identifiant / mot de passe ; ni la page ni une route protégée n'envoient d'office vers l'IdP.
- **(e) Bandeau « E-mail non configuré »** dans Réglages quand le SMTP est absent.
- **(f) Confirmation avant d'effacer un secret lié** : changer la destination SMTP alors que `smtp.password` est configuré ⇒ confirmation **dans la page**
  (aucune boîte `confirm()`), annuler = aucune requête et secret intact, confirmer = un PATCH et secret effacé.
- **(c1) à (c7) Page Réglages** (§5.6b, CA-72 à CA-78 ; CA-79 pour le secret absent de la page) :
  (c1) 8 sous-onglets, compteurs, ancre `#reglages/<section>` (rechargement, ancre inconnue), rôles ARIA, flèches du clavier, alerte sur `DEM_TIMEOUT=abc` ;
  (c2) recherche (casse et accents ignorés), pastille de résultats, onglets grisés (contraste ≥ 4,5:1), filtre « valeurs modifiées », saisie conservée sous un filtre, ni URL ni stockage ;
  (c3) pastilles d'origine, aide repliée, « Rétablir… → valeur » avec confirmation dans la page, autres saisies conservées ;
  (c4) fonds de carte (cartes ordonnées, contrôle d'hôte `tiles=`, doublon, texte brut, Monter/Descendre puis ordre reçu par le visualiseur, fond par défaut, volet Avancé, 10 fonds, envoi forcé = 400) ;
  (c5) rectangle (4 champs, « ≈ 371 km × 302 km », aperçu qui suit la saisie, contrôles, volet Avancé, tuiles seulement des hôtes autorisés) ;
  (c6) enregistrement par onglet (avant/après, un seul PATCH avec la `version`, audits), 400 sous le champ (`aria-invalid`, focus), version périmée + « Recharger », rien ne se perd, `beforeunload` ;
  (c7) éditeur de liste, `requires_restart` (pastille, confirmation, bandeau), secret absent de la page (registre de test).
  (m) lisibilité (CA-82) : 8 onglets sur une ligne, pastilles de nombre, contrastes calculés (thèmes clair et sombre), tailles, largeurs d'éditeurs, colonnes, 390 px ;
  (n) rectangle édité sur la carte (CA-83) : poignées, souris, doigt réel (`hasTouch` + CDP), clavier, bornes, champs synchronisés, `PATCH` unique, aucune requête hors tuiles autorisées.
- **(j) / (l) Invitations** : profil d'un compte SSO (cookie signé par l'application, IdP jamais joint), puis onglet Invitations de l'administration (CA-84 : « Émise par », statuts, compte créé, comptes supprimés, texte seul).
- **(k) Constructeur** : points effacés du stockage local après l'enregistrement (réussi ou « déjà enregistrée »), gardés sinon (CA-R40).
- **(p0) à (v2) RB-2.7** (`rb27.py`, CA-R41 à CA-R48) : déplacer, insérer par appui sur la trace (tolérances 10 / 16 px, doigt réel), ajouter, retirer, clavier,
  échec et « Annuler » (retour exact), historique (onze gestes, plafond de 10 résultats), verrouillage pendant le calcul, points limites, trace enregistrée intacte.
  Le résultat de départ est une génération réelle, gardée ; les régénérations sont répondues par un double (`RouteMock`) qui peut les retarder ou les faire échouer
  (429, 503, 504, coupure) ; (v) et (k) utilisent le moteur et le serveur réels. Les générations réelles comptent dans la limite de 30 par heure (RB-S5) : un banc rejoué
  sans cesse sur la même instance finit par recevoir « Trop de générations ».
- **(g)** une requête vers un hôte externe est interceptée, enregistrée et bloquée.
- Aucune erreur JavaScript dans la console (les erreurs de tuiles de fond de carte / réseau sont ignorées et listées dans `resultat.json`).

## Mettre à jour

- Playwright : choisir une version publiée depuis **au moins deux semaines**, changer la version **et** le digest dans `Dockerfile.playwright` (`docker pull` puis
  `docker image inspect --format '{{index .RepoDigests 0}}'`), la version et les empreintes de `requirements.txt` (même version que l'image) et le nom de l'image dans `run.sh`.
- Un nouveau scénario : une fonction décorée `@scenario("...")` dans `scenarios.py`, ajoutée à la liste de `main()` ; prendre une capture à chaque étape utile.
