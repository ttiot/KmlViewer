# Banc de test navigateur (optionnel, local)

Des défauts visuels (popup d'état vide qui capte les clics, JSON brut dans l'administration) ont échappé à tous les tests : ce banc ouvre
**l'application réelle dans un vrai Chromium**, déroule des scénarios et **photographie** chaque étape.

**Jamais dans la CI** : `pytest` ignore ce dossier (`norecursedirs` dans `pytest.ini`), la CI Python n'a ni Docker-dans-Docker ni navigateur.

## Commande unique

```bash
web-app/tests_browser/run.sh
```

Deux instances de l'application sont démarrées (mode `local` sur 5077, mode `oidc` de test sur 5079). Captures et résultats dans `~/.cache/kmlviewer-browser/<horodatage>/` (**hors dépôt**, `run.sh` refuse un dossier dans le dépôt) :
`01-visualiseur-etat-vide.png`, `02-constructeur-point-pose.png`, `03-constructeur-ferme-popup-revenu.png`, `04-admin-diagnostic.png`,
`05-admin-journal.png`, `06-connexion-oidc-page-unique.png`, `07-admin-bandeau-smtp-absent.png`, `08-admin-confirmation-effacement-secret.png`, `resultat.json`, `application.log` (et `ECHEC-*.png` au premier écart). Code de sortie non nul au moindre échec.

Variables : `BENCH_OUT` (dossier de sortie), `BENCH_PORT` (5077 par défaut), `BENCH_ENGINE_URL` (moteur de routage, `http://127.0.0.1:8989`).

## Ce qui tourne

| Élément | Détail |
|---|---|
| Application | `python:3.15-slim` **épinglée par digest** (`Dockerfile.app`), dépendances de `requirements.txt` **avec hachages** (comme la CI), code monté en lecture seule, gunicorn, mode `local` (aucune authentification : le compte « local » est administrateur), base **jetable** (`tmpfs`), réseau hôte |
| Moteur de routage | le GraphHopper local `127.0.0.1:8989` (à démarrer avant ; `run.sh` le vérifie), déclaré dans la base par `seed.py` avec `OUTBOUND_ALLOWED_HOSTS=routing=127.0.0.1:8989` |
| Préparation | `seed.py` : fonction bêta « Créer une trace » ouverte à tous, carte centrée sur Lille (dans la zone couverte), quelques lignes d'audit |
| Navigateur | image officielle `mcr.microsoft.com/playwright/python:v1.65.2-noble` **épinglée par version ET digest** (`Dockerfile.playwright`), client Python `playwright==1.65.2` (même version que les navigateurs de l'image) |

Les deux images locales (`kmlviewer-bench-app:<hachage de requirements.txt>` et `kmlviewer-bench-playwright:1.65.2`) sont construites au premier lancement.

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
- Pas de scénario pour la refonte des Réglages (CA-72 à CA-78) : c'est la 3ᵉ image.
- Aucune erreur JavaScript dans la console (les erreurs de tuiles de fond de carte / réseau sont ignorées et listées dans `resultat.json`).

## Mettre à jour

- Playwright : changer la version **et** le digest dans `Dockerfile.playwright` (`docker pull` puis `docker image inspect --format '{{index .RepoDigests 0}}'`),
  la version du paquet pip et le nom de l'image dans `run.sh`.
- Un nouveau scénario : une fonction décorée `@scenario("...")` dans `scenarios.py`, ajoutée à la liste de `main()` ; prendre une capture à chaque étape utile.
