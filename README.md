[![Build and Push Docker Image](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml/badge.svg)](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml)

# 🗺️ KmlViewer

Application web auto-hébergée pour visualiser, analyser et archiver des traces GPS (KML, KMZ, GPX) :
un visualiseur cartographique, et une bibliothèque d'activités avec tableau de bord et tronçons chronométrés.

## 🚀 Démarrage rapide

### Avec Docker (Recommandé)

1. **Cloner et lancer l'application** :

   ```bash
   git clone https://github.com/ttiot/KmlViewer
   cd kmlViewer
   ./start.sh
   ```

2. **Accéder à l'application** :
   Ouvrez votre navigateur à l'adresse : **<http://localhost:8080>**

3. **Arrêter l'application** :

   ```bash
   ./stop.sh
   ```

### Tests

```bash
cd web-app
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m ruff check .
python -m pytest --cov=app
```

## 🏃 Bibliothèque, tableau de bord, tronçons et comptes

Dossier surveillé, import depuis l'interface, statistiques par semaine / mois / année, tronçons
personnels chronométrés, correction d'altitude par MNT, authentification OIDC (FusionAuth),
sécurité et sauvegarde : voir [docs/bibliotheque.md](docs/bibliotheque.md).
Configuration : créer le `.env` en `0600` (`install -m 600 .env.example .env` : il contiendra des clés secrètes) et lire
[la section Configuration](#-configuration). `TRUST_PROXY` vaut `0` dans le modèle (sans reverse proxy) : le passer à `1` derrière un proxy TLS.
Procédures d'exploitation (perte de clé, perte d'un second facteur, bascule de mode, mise à jour,
horloge, mémoire) : [docs/exploitation.md](docs/exploitation.md).

## ✨ Visualiseur

### 📁 Fichiers

- **Formats** : KML, KMZ et GPX, jusqu'à 16 Mo, plusieurs fichiers à la fois (glisser-déposer ou sélection)
- **Fichiers d'exemple** : les traces placées dans `web-app/sample_files/` sont proposées dans la barre
  latérale, à tous les comptes
- **Multi-fichiers** : chaque fichier a sa case d'affichage et son onglet ; l'onglet actif alimente
  la liste des points, l'analyse, l'édition et l'export
- **Modes d'affichage** : double unité (km/h + kts, ft + m) ou unité simple

### 🗺️ Carte

- **5 fonds de carte** : OpenStreetMap, Satellite (Esri), Terrain (OpenTopoMap), CartoDB Positron, CartoDB Dark Matter
- Traces, polygones et points aux couleurs des styles KML, popups de détail
- **Navigation entre les points** : flèches ← → du clavier, boutons Précédent/Suivant, clic dans la liste ou sur un marqueur

### 📊 Analyse

- **Stats, Élévation, Vitesse** : distance, durée et temps en mouvement mesurés, vitesses, dénivelés, profils en graphique (rien n'est estimé : une trace non horodatée n'a ni durée ni vitesse)
- **Avancé** (à la demande) : arrêts détectés, segments homogènes, zones de vitesse, pentes du terrain

### ✏️ Édition

Les modifications portent sur le fichier affiché, dans le navigateur ; l'export les enregistre.

- **Ajouter un point** : bouton, puis clic sur la carte, puis nom et description
- **Modifier / supprimer un point** : bouton, puis clic sur le point (carte ou liste) ; Échap annule
- **Simplifier les traces** : algorithme de Douglas-Peucker, tolérance réglable en mètres

### 📤 Export

GPX, CSV, GeoJSON ou KML, avec nom de fichier au choix. Les points de trace peuvent être
exclus du CSV.

## 🛠️ Scripts utilitaires

| Script | Description |
|--------|-------------|
| `./start.sh` | Démarre l'application avec Docker |
| `./stop.sh` | Arrête l'application |
| `./restart.sh` | Redémarre le conteneur **sans relire `.env`** |
| `./logs.sh` | Affiche les logs en temps réel |
| `./cleanup.sh` | Nettoyage complet du projet, **données comprises** (demande confirmation) |

Ces scripts sont des **outils de développement local**, pas un mode de déploiement. En production, la stack est
gérée par Ansible : [Déploiement géré par Ansible](#déploiement-géré-par-ansible).

Commandes d'exploitation (`docker compose exec kml-viewer flask <commande>`) :

| Commande | Description |
|----------|-------------|
| `backup [--keep N]` | Copie cohérente de la base dans `/data/backups` |
| `inbox-scan` | Passe une fois sur le dossier surveillé. Refuse de tourner (« Un autre processus… ») tant que l'application servie détient le verrou du dossier, c'est-à-dire en temps normal : le dossier est déjà scruté toutes les `INBOX_SCAN_INTERVAL` secondes |
| `recompute-stats` | Recalcule distance, durées et records des activités déjà stockées (à lancer après une mise à jour qui corrige ces calculs) |
| `user-admin <login>` | Donne le rôle admin et active le compte (c'est ainsi que se désigne le premier admin) ; `--revoke` retire le rôle. Sur un compte à mot de passe sans second facteur : refusé |
| `user-invite --admin <e-mail>` | Amorçage du premier admin d'une instance à comptes à mot de passe : émet un jeton de 24 h à usage unique et affiche son lien une seule fois ([pas à pas](#amorçage-du-premier-administrateur)). `--account <login>` reprend l'amorçage d'un compte existant sans facteur ; avec `--reset-password` (qui exige `--account`), le lien fait **choisir un nouveau mot de passe** (mot de passe oublié) avant l'enrôlement. Un compte **désactivé** est refusé (« Ce compte est désactivé : réactive-le d'abord »), refus tracé |
| `user-mfa-reset <login>` | Perte de tous les seconds facteurs d'un compte à mot de passe : retire ses facteurs (TOTP, codes de secours), ferme ses sessions, le rétrograde s'il est admin. Annonce « dernier administrateur » **avant** d'agir si c'est le cas |
| `secrets-rotate` | Rechiffre les secrets avec la première clé de `SECRETS_KEY` (rotation) |
| `secrets-reset [--yes]` | Clé `SECRETS_KEY` perdue : efface tous les secrets chiffrés (à ressaisir) et les secrets TOTP ; rétrograde les admins qui n'ont plus aucun facteur |
| `settings-list` | Valeur effective et source (base / env / défaut) de chaque réglage administrable |
| `settings-reset <clé>` ou `--all` | Supprime la valeur en base d'un réglage (ou de tous) : retour à la valeur d'env, sinon au défaut |

Chaque commande qui modifie quelque chose est inscrite au journal d'audit (acteur « ligne de
commande »). Détails et procédures : [docs/exploitation.md](docs/exploitation.md).

## 🏗️ Architecture

```text
web-app/
├── kml_viewer.py          point d'entrée (gunicorn kml_viewer:app)
├── app/
│   ├── __init__.py        factory Flask
│   ├── errors.py          réponses d'erreur JSON, sans détail interne
│   ├── security.py        CSP, en-têtes, contrôle d'origine (CSRF)
│   ├── auth/              modes d'authentification, OIDC, sessions, jetons, invitations, mots de passe
│   ├── settings/          registre des réglages administrables (bornes) et valeur effective base > env > défaut
│   ├── secrets_store.py   secrets chiffrés (Fernet), rotation
│   ├── outbound.py        liste blanche des destinations sortantes (OUTBOUND_ALLOWED_HOSTS)
│   ├── mailer.py          envoi d'e-mails (SMTP), file d'envoi
│   ├── background.py      ordonnanceur de fond (dossier surveillé, altitude, purge d'audit)
│   ├── ratelimit.py       limiteur de débit en mémoire
│   ├── audit.py           journal d'audit
│   ├── api/               routes REST (visualiseur, analyse, export, bibliothèque, tronçons, invitations)
│   ├── services/          parsing KML/GPX/KMZ, analyse, ingestion, MNT
│   ├── db.py              SQLite et migrations nommées
│   └── cli.py             commandes d'exploitation
├── static/
│   ├── js/viewer/         visualiseur : core, prefs, map, settings, payload, points, files, opener,
│   │                      analysis, editor, library, main
│   ├── js/                pages bibliothèque (dashboard, segments, users) et utilitaires
│   └── vendor/            Leaflet, Bootstrap, Chart.js, Font Awesome (embarqués, sans CDN)
├── templates/
└── tests/
```

- **Backend** : Python 3.15, Flask, gunicorn, SQLite, numpy ; dépendances verrouillées avec empreintes
- **Frontend** : JavaScript sans framework ni étape de build ; toute donnée issue d'un fichier est
  insérée comme texte, et la CSP n'autorise que les scripts de l'application
- **Déploiement** : image non-root, système de fichiers en lecture seule, port publié sur
  `127.0.0.1:8080` (à placer derrière un reverse proxy TLS)

### API du visualiseur

| Route | Rôle |
|-------|------|
| `POST /api/upload`, `POST /api/multi-upload` | Parse un ou plusieurs fichiers |
| `GET /api/sample-files`, `GET /api/load-sample/<nom>` | Fichiers d'exemple |
| `POST /api/analysis/*` | Analyses de trajectoire |
| `POST /api/editor/simplify-trace` | Simplification d'une trace |
| `POST /api/export/<gpx\|csv\|geojson\|kml>` | Export |
| `GET /api/health` | Santé et version |
| `GET /api/stats` | Temps d'exécution cumulés (admin) |

L'API de la bibliothèque est décrite dans [docs/bibliotheque.md](docs/bibliotheque.md).

## 🔧 Configuration

La configuration vient de trois sources, de la plus forte à la plus faible :

1. **la base** (réglages administrables, voir plus bas) : une valeur posée par un admin prime ;
2. **les variables d'environnement** (`.env`, modèle commenté : `.env.example`) ;
3. **le défaut** du code.

Seuls les *réglages administrables* suivent cette règle. Les variables d'amorçage et de sécurité
(`AUTH_MODE`, `SECRET_KEY`, `SECRETS_KEY`, `PUBLIC_URL`, `OUTBOUND_ALLOWED_HOSTS`, `TRUSTED_HOSTS`…)
ne se règlent **que** dans l'environnement : l'administration ne les voit pas.

Les variables sont lues au démarrage du processus. Après avoir modifié `.env`, recréer le
conteneur : `docker compose up -d` (en déploiement Ansible : relancer le playbook, voir
[Déploiement géré par Ansible](#déploiement-géré-par-ansible)). `docker compose restart` et `./restart.sh`
redémarrent le conteneur existant **sans relire** `.env`. Pour changer le port, modifier `ports` dans
`docker-compose.yml`.

Sans aucune variable, l'application tourne en mode local sans authentification, joignable uniquement
sous le nom `localhost`. Une configuration incomplète ou dangereuse **empêche le démarrage**, avec le
motif dans les logs (`./logs.sh`).

> **Statut** : tout ce qui est décrit ici est livré, sauf ce qui porte la mention **non livré**
> (liste récapitulative : [Non livré](#non-livré)). Les trois modes d'authentification sont utilisables ;
> le mode `password` (comptes sur invitation, second facteur TOTP) se met en route avec
> [l'amorçage du premier administrateur](#amorçage-du-premier-administrateur). Le constructeur de traces
> ([bêta](#constructeur-de-traces-bêta)) et WebAuthn sont livrés ; les restes éventuels sont listés dans [Non livré](#non-livré).

### Essayer la version de test (image publiée par la CI)

La CI publie l'image de la branche `ghcr.io/ttiot/kmlviewer:unstable-feature-admin-space-and-route-builder` (aussi sur Docker
Hub), qui contient tout : modes `oidc` / `local` / `password`, TOTP, codes de secours, WebAuthn, invitations, `/admin`,
constructeur de traces. **Instance de test, base neuve, jamais en production.** Variables obligatoires du mode `password` :
`AUTH_MODE`, `PUBLIC_URL`, `TRUSTED_HOSTS`, `SECRET_KEY`, `SECRETS_KEY` (et `SESSION_COOKIE_SECURE=0` pour un test local en HTTP
seulement) ; `.env` en `0600` et `TRUST_PROXY=0` sans reverse proxy. Le tag de branche **bouge à chaque push** : pour un test
reproductible, épingler `sha-<7 caractères du commit>`. Déploiement avec le compose, amorçage du premier admin, moteur de routage et **checklist « avant de tester »** :
[docs/exploitation.md](docs/exploitation.md#déployer-limage-publiée-pour-tester).

### Déploiement géré par Ansible

Cadre de référence : la stack `docker-compose.yml` tourne sur un **serveur dédié**, déployée par Ansible, avec **un seul
opérateur** (l'administrateur du serveur). Détail, exemples et procédures : [docs/exploitation.md](docs/exploitation.md#déploiement-géré-par-ansible).

- **Environnement** : le `.env` est généré par un template Ansible ; secrets (`SECRET_KEY`, `SECRETS_KEY`,
  `OIDC_CLIENT_SECRET`) dans **Ansible Vault**, `.env` en mode `0600`, `no_log` sur les tâches concernées.
- **Changement de configuration** : par **recréation** du conteneur (`docker compose up -d`, module
  `community.docker.docker_compose_v2` en `state: present`), **jamais** par `restart`, qui ne relit pas `.env`.
  `start.sh`, `stop.sh` et `restart.sh` sont des outils de développement local.
- **Réglages administrables** : ce qu'Ansible écrit n'est qu'une **valeur par défaut** (base > env > défaut, pas de
  verrouillage). Une valeur saisie dans l'admin l'emporte et survit aux redéploiements ; écarts visibles avec
  `flask settings-list`, retour à la valeur d'Ansible avec `flask settings-reset <clé>`. Ne régler dans l'interface que
  ce qu'Ansible ne gère pas.
- **Montée de version** : **sauvegarder le volume avant** (service arrêté, archive de tout `/data`, tâche Ansible avant `docker compose up -d`). Le retour arrière sûr est la **restauration** de cette archive ; une ancienne version sur la base migrée démarre mais compte les traces planifiées dans les statistiques et ignore le mode `password` : [procédure](docs/exploitation.md#montée-de-version-et-retour-arrière).
- **Image** : une image locale `kml-viewer:local` déjà présente n'est **pas reconstruite** par `docker compose up -d` : `build:
  always` dans la tâche Ansible, ou une image publiée épinglée ; vérifier la version servie (`/api/health`) après chaque déploiement.
- **Journaux** : aucune rotation configurée par le compose : poser `json-file` `max-size`/`max-file` (`daemon.json`) sur l'hôte.
- **Reverse proxy** : avec `TRUST_PROXY=1`, il doit poser `X-Forwarded-For` et `X-Forwarded-Proto` et ne jamais relayer ceux du
  client ([détail](docs/exploitation.md#reverse-proxy--ce-quil-doit-poser-trust_proxy)).
- **Données** : volume nommé `kml-viewer-data` (`project_name` fixe), sauvegardes à sortir du serveur, **archive en `0600`, image
  épinglée, chiffrée avant de quitter le serveur** ; la `SECRETS_KEY` est dans le Vault, pas dans les sauvegardes.
- **CLI** : canal **opérateur de confiance**, sans authentification propre : l'accès à `docker` équivaut à l'accès à la
  CLI, et aucun autre utilisateur système ne doit exister sur le serveur. Les commandes se lancent à la main
  (`docker compose exec` ou `run`), pas depuis un playbook.
- **Premier admin** : en mode `oidc`, `OIDC_ADMIN_ROLE` ou première connexion SSO puis `flask user-admin <login>` ;
  en mode `password`, `flask user-invite --admin <adresse>` puis le lien, le mot de passe et l'enrôlement TOTP
  ([pas à pas](#amorçage-du-premier-administrateur)).
- **Moteur de routage** : service `graphhopper` du compose (profil `routing`, activé par `COMPOSE_PROFILES=routing` après le
  premier `routing/update.sh`), graphe mis à jour chaque mois par `routing/update.sh` (timer ou cron posé par Ansible) : [Service de routage](#service-de-routage-graphhopper).

### Modes d'authentification (`AUTH_MODE`)

| Mode | Connexion | Exigences au démarrage | État |
|------|-----------|------------------------|------|
| `oidc` | SSO obligatoire (FusionAuth ou autre IdP OIDC) ; en plus, comptes à mot de passe **sur invitation** si `SECRETS_KEY` **et** `PUBLIC_URL` sont renseignées | `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SECRET_KEY` (32 caractères au moins), `TRUSTED_HOSTS` | livré (SSO) ; comptes à mot de passe sur invitation (connexion avec second facteur TOTP) livrés |
| `local` | aucune : mono-utilisateur, **chaque visiteur est administrateur** | aucune ; interdit avec `OIDC_ISSUER` | livré |
| `password` | identifiant + mot de passe + second facteur, sans SSO | `SECRET_KEY` (32 caractères au moins), `TRUSTED_HOSTS`, `SECRETS_KEY`, `PUBLIC_URL` couverte par `TRUSTED_HOSTS` ; interdit avec `OIDC_ISSUER` | livré (second facteur : TOTP ou clé de sécurité WebAuthn) |

- `AUTH_MODE=password` s'écrit toujours explicitement : un `AUTH_MODE` vide ne le choisit jamais
  (il donne `oidc` si `OIDC_ISSUER` est renseigné, sinon `local`).
- En production, écrire `AUTH_MODE=oidc` : une variable OIDC perdue arrête alors l'appli au lieu de
  l'ouvrir à tous.
- Sans `AUTH_MODE` ni `OIDC_ISSUER`, le démarrage est refusé dès que `TRUSTED_HOSTS`, `TRUST_PROXY`,
  `SESSION_COOKIE_SECURE`, `OIDC_CLIENT_ID` ou `OIDC_CLIENT_SECRET` est renseigné (cela ressemble à une
  production sans authentification) : écrire `AUTH_MODE=local` si c'est voulu.
- Le mode est celui validé au démarrage : le garde d'accès, les routes d'authentification et les
  gabarits ne le déduisent jamais de la présence d'`OIDC_ISSUER`. Seul le mode `local` crée le compte
  `local`. Une session dont le compte n'a pas une méthode d'authentification permise par le mode courant
  (`password` ⇒ compte à mot de passe ; `oidc` ⇒ SSO ou mot de passe ; `local` ⇒ compte local) est vidée :
  après une bascule de mode, aucun cookie émis avant ne vaut plus rien.
- Mode `oidc` **sans** `SECRETS_KEY` ou sans `PUBLIC_URL` : comptes à mot de passe, invitations et saisie de
  secrets désactivés (routes 404) ; pas de refus de démarrage.
- Mode `local` : routes de mot de passe, d'invitation et de jetons en 404.

Détails OIDC (FusionAuth), session, comptes SSO : [docs/bibliotheque.md](docs/bibliotheque.md).

### Amorçage du premier administrateur

Mode `password`, instance neuve (aucun compte). Vérifié de bout en bout sur le code livré.

1. **Déployer** avec `AUTH_MODE=password`, `SECRET_KEY`, `SECRETS_KEY`, `TRUSTED_HOSTS` et `PUBLIC_URL` (son hôte dans
   `TRUSTED_HOSTS`) ; ne pas définir `OIDC_ISSUER`. Contrôler le démarrage (`docker compose logs kml-viewer`) : le
   `WARNING` « SMTP non configuré : les adresses e-mail ne peuvent pas être validées » est normal tant que l'e-mail n'est
   pas réglé.
2. **Émettre le jeton**, sur le serveur, dans le dossier du projet :

   ```bash
   docker compose exec kml-viewer flask user-invite --admin admin@exemple.fr
   ```

   ```text
   Jeton d'amorçage créé (valable jusqu'au 2026-11-25T18:33:04Z, usage unique). Lien, montré une seule fois :
   https://traces.example.com/auth/token#t=…
   E-mail non configuré : transmets ce lien à la seule personne concernée.
   ```

   Le lien est valable 24 h et n'est affiché **qu'une fois** ; avec le SMTP configuré il part aussi par e-mail. Le jeton
   n'est jamais écrit dans les journaux (il est dans le fragment `#t=`, que le navigateur n'envoie pas au serveur).
3. **Ouvrir le lien** dans un navigateur : la page efface le jeton de la barre d'adresse et propose un identifiant et un
   mot de passe. Identifiant : 3 à 64 caractères `[a-z0-9._-]`, sans `@` ; `cli`, `system`, `local`, `admin` et `root`
   sont réservés (« Identifiant indisponible »). Mot de passe : 12 à 128 caractères, ni l'identifiant, ni l'adresse, ni
   un mot de passe courant (« Ce mot de passe est trop courant. »). Un refus ne consomme pas le jeton.
4. **Enrôler le second facteur**, dans le **même navigateur** (aucune session n'est ouverte à ce stade) : la page affiche un QR code
   et le secret en texte. Scanner le QR code avec une application TOTP (SHA-1, 6 chiffres, 30 s), saisir le code à
   6 chiffres. Le compte **devient admin à cet instant, pas avant**. La page affiche alors **10 codes de secours**,
   **une seule fois** (`XXXX-XXXX-XXXX-XXXX`) : les noter. La fenêtre d'enrôlement dure 15 minutes.
5. **Se connecter** : on est renvoyé à la page de connexion ; identifiant, mot de passe, puis le code TOTP (ou un code de
   secours, à usage unique). Le lien **Administration** apparaît dans la barre de navigation.
6. **Suite** : [espace d'administration](#espace-dadministration-admin) : régler le SMTP (onglet « Clés API et e-mail »),
   valider l'adresse de l'admin (elle reste « en attente » tant que le mail de validation n'a pas été reçu et le lien
   ouvert **connecté**), inviter les autres comptes, garder **un second admin**.

Enrôlement abandonné (fenêtre expirée, navigateur fermé) : le compte existe, **non admin et sans facteur**, et le lien est
consommé. Reprise : `flask user-invite --admin --account <identifiant>` ; le nouveau lien demande le **mot de passe actuel**
du compte (conservé tel quel), puis mène au même enrôlement. Relancer sans `--account` créerait un second compte.

**Mot de passe perdu** (compte créé, enrôlement jamais terminé) : ajouter `--reset-password` :

```bash
docker compose exec kml-viewer flask user-invite --admin --account recette-admin --reset-password
```

Le lien affiché (24 h, usage unique) ne demande plus le mot de passe actuel mais en fait **choisir un nouveau** (mêmes règles
de politique), qui **remplace** l'ancien (les sessions du compte sont fermées, action tracée), puis mène à l'enrôlement du TOTP
et à l'octroi du rôle admin comme à l'amorçage. Refusé si le compte est inconnu, désactivé, SSO, ou s'il a déjà un second
facteur (utiliser alors `flask user-mfa-reset` ou `flask user-admin`) ; `--reset-password` sans `--account` est une erreur
d'usage. Un compte **désactivé** est refusé aussi, avec ou sans `--reset-password` (aucun jeton émis, refus tracé) : le
réactiver d'abord (onglet Utilisateurs).

### Second facteur (TOTP) et codes de secours

- **TOTP** (RFC 6238, SHA-1, 6 chiffres, 30 s, tolérance ±1 pas ; **l'horloge du serveur doit être juste**, voir
  [docs/exploitation.md](docs/exploitation.md#horloge-et-ntp)). Secret de 160 bits chiffré au repos par `SECRETS_KEY`. Un code
  n'est accepté qu'**une fois** (le même code rejoué est refusé : « Code invalide ou déjà utilisé »). **Seuls les échecs
  comptent** : 5 essais ratés par connexion (puis il faut ressaisir le mot de passe) et, par compte, 5 échecs par
  15 minutes (`ratelimit.mfa_per_account`, 3 à 10) avant un 429 ; un code juste ne consomme rien, et une connexion réussie ne
  bloque jamais le compte, quel que soit le nombre de connexions. Le mot de passe reste limité **avant** le hachage
  (`ratelimit.password_per_identifier`, `ratelimit.password_per_ip`), mais une connexion réussie rend son accès.
- **Obligatoire pour les admins** (un admin à mot de passe a toujours un facteur actif, à tout instant) ; pour tous si
  `auth.mfa_policy` vaut `all`. Passer le réglage à `all` est **refusé** (erreur 409 indiquant le nombre de comptes) tant qu'un
  compte à mot de passe actif n'a pas de facteur ; un compte créé ensuite sans facteur est conduit à l'enrôlement à sa première
  connexion.
- **Codes de secours** : 10 codes de 16 caractères, générés avec le **premier** facteur, montrés une seule fois, à usage
  unique ; leur utilisation envoie une notification par e-mail. Ils servent à se connecter quand le TOTP est perdu mais ne
  comptent **pas** comme un facteur. Régénérables depuis le profil (les anciens sont invalidés).
- **Profil › Second facteur** : ajouter ou retirer le TOTP ou une clé, régénérer les codes ; toute modification exige le mot de passe
  actuel, et retirer le TOTP ou régénérer les codes exige en plus un code TOTP valide. Le dernier facteur d'un admin ne
  peut pas être retiré. Changer son mot de passe ou son adresse e-mail (profil) exige le mot de passe actuel **et** un code
  du second facteur ; l'ancienne adresse est notifiée.
- **Facteur perdu** : un autre admin réinitialise le compte (onglet Utilisateurs, ou `POST /api/admin/users/<id>/mfa-reset` ;
  jamais sur soi-même), ou l'opérateur du serveur lance `flask user-mfa-reset <identifiant>`. Les facteurs sont retirés, les
  sessions fermées, le compte est **rétrogradé** s'il était admin ; le titulaire se reconnecte avec son mot de passe, enrôle
  un nouveau facteur, puis un admin le repromeut (`flask user-admin` est **refusé** tant que le compte n'a pas de
  facteur). Procédure : [docs/exploitation.md](docs/exploitation.md#perte-dun-second-facteur).
- **Clés de sécurité WebAuthn** (FIDO2, ou authentificateur de l'appareil) : second facteur au même titre que le TOTP, à
  l'enrôlement (« Enregistrer une clé de sécurité »), à la connexion (« Utiliser une clé de sécurité » quand le compte en a
  une) et dans le profil. Un compte peut cumuler TOTP et jusqu'à 10 clés. **Exigences** : `PUBLIC_URL` en **HTTPS**
  (`http://localhost` n'est accepté qu'en test ou en mode debug : sans HTTPS le bouton n'apparaît pas) ; l'identifiant de
  partie de confiance (RP ID) est l'hôte de `PUBLIC_URL`, qui ne doit pas être une adresse IP ; `WEBAUTHN_RP_ID` peut le figer sur
  un domaine parent (au moins **deux libellés**, suffixe de l'hôte, jamais un suffixe public comme `com`, `co.uk` ou
  `github.io`) ; une valeur refusée désactive WebAuthn (message dans les journaux). **Changer `PUBLIC_URL` (ou le RP ID)
  rend les clés déjà enregistrées inutilisables** : fixer `WEBAUTHN_RP_ID` sur le domaine parent avant d'enrôler pour s'en
  protéger. Défi de 32 octets gardé côté serveur (2 minutes, à usage unique) ; un compteur de signatures qui régresse est
  refusé et audité. **Ajouter une clé** (profil) : mot de passe actuel ; **retirer une clé** : mot de passe actuel **et** code
  TOTP ou de secours. Le dernier facteur d'un admin ne peut pas être retiré. Une clé enregistrée en premier facteur génère
  les 10 codes de secours.

### Espace d'administration (`/admin`)

Réservé aux admins (un admin à mot de passe doit avoir un second facteur actif, vérifié à chaque requête) ; lien
« Administration » de la barre de navigation. `/admin/users` mène à la même page. Onglets :

| Onglet | Contenu |
|--------|---------|
| Utilisateurs | liste (méthode d'authentification, adresse validée ou en attente, facteurs, crédits d'invitation) ; activer, promouvoir (refusé sans facteur pour un compte à mot de passe), crédits, lien de réinitialisation du mot de passe, réinitialisation du second facteur, fermeture des sessions, renvoi de la validation d'adresse, conversion SSO → mot de passe (mode `password` seulement), suppression ; le dernier admin actif ne peut être ni désactivé, ni rétrogradé, ni supprimé |
| Invitations | toutes les invitations ; créer (note libre réservée aux admins), révoquer ; lien affiché une seule fois |
| Fonctions bêta | état de chaque fonction : désactivée, liste d'utilisateurs, ou tous (aujourd'hui `route_builder`, le [constructeur de traces](#constructeur-de-traces-bêta)), avec le diagnostic du moteur |
| Réglages | valeur, source (base / env / défaut), surcharge, retour au défaut ; bornes validées, lot tout ou rien |
| Clés API et e-mail | SMTP (hôte, port, sécurité, identifiant, expéditeur), secrets en écriture seule, e-mail de test et « Tester » du moteur de routage (réponse « ok » ou « ko » sans détail, rendue à l'échéance du service) |
| Journal | journal d'audit paginé et filtrable ; l'origine se lit sur le type d'acteur (compte, ligne de commande, système) |

L'API correspondante est sous `/api/admin/*` (utilisateurs, invitations, réglages et `settings/diagnostic`, secrets
et leur test, `mail/test`, `features`, `audit`). Les secrets ne sont jamais relus : on ne reçoit que « configuré »,
la date, l'auteur et la destination liée.

### Variables d'amorçage et de sécurité (environnement seulement)

| Variable | Rôle | Défaut |
|----------|------|--------|
| `AUTH_MODE` | `oidc`, `local` ou `password` (voir ci-dessus) | déduit |
| `PUBLIC_URL` | Origine publique : `https://hôte[:port]`, sans chemin, identifiants, paramètres (`http://` toléré pour `localhost` seulement). **Seule source** des liens envoyés ou affichés ; l'en-tête `Host` n'est jamais utilisé pour cela. Renseignée, elle sert aussi de référence au contrôle d'`Origin` des requêtes modifiantes : derrière un proxy TLS, c'est l'URL `https` publique, et une instance jointe sous un autre nom que celui-ci reçoit 403 sur ses POST. Obligatoire en mode `password` | vide |
| `SECRET_KEY` | Signature des cookies de session. Refus de démarrer en `oidc`/`password` si elle est par défaut ou fait moins de 32 caractères (`openssl rand -hex 32`) | valeur par défaut (refusée) |
| `SECRETS_KEY` | Clé(s) Fernet qui chiffrent les secrets en base (mot de passe SMTP, clé d'API, secrets TOTP). **Plusieurs clés séparées par des virgules** : la première chiffre, toutes déchiffrent. Distincte de `SECRET_KEY`. Obligatoire en mode `password` ; variable d'environnement seulement, **jamais dans la base ni dans `flask backup`**. Voir ci-dessous | vide |
| `TRUSTED_HOSTS` | Noms d'hôte acceptés (virgules ; `.example.com` couvre aussi les sous-domaines). Tout autre en-tête `Host` reçoit 400, sauf pour `/api/health`. Obligatoire en `oidc` et `password` ; en `local`, vide = `localhost` et `127.0.0.1` | vide |
| `TRUST_PROXY` | Nombre de reverse proxies de confiance (`X-Forwarded-For/Proto`) ; indispensable derrière un proxy TLS pour l'URL de callback OIDC. Le proxy doit **poser** ces en-têtes et ne jamais relayer ceux du client ; **sans proxy, laisser `0`** (avec `1`, le client choisit son adresse et son schéma). Le `.env.example` a `0` ; passer à `1` derrière un proxy TLS. Voir [Reverse proxy](docs/exploitation.md#reverse-proxy--ce-quil-doit-poser-trust_proxy) | `0` |
| `TRUST_PROXY_HOST` | `1` seulement si le proxy réécrit `Host` **et** pose `X-Forwarded-Host` (HAProxy : laisser à `0`) | `0` |
| `SESSION_COOKIE_SECURE` | Cookie marqué `Secure`. Vide : activé d'office en `oidc` et `password` ; `0` : dérogation de développement en HTTP, signalée dans les logs | vide |
| `SESSION_LIFETIME_HOURS` | Durée absolue d'une session, depuis la connexion | `12` |
| `OUTBOUND_ALLOWED_HOSTS` | Liste blanche des destinations sortantes. Voir ci-dessous | vide |
| `SCHEDULER_ENABLED` | `0` coupe l'ordonnanceur de fond (réplique secondaire, diagnostic). Par défaut il démarre dans tout processus servi, jamais en ligne de commande | `1` |
| `WEBAUTHN_RP_ID` | Identifiant de partie de confiance WebAuthn : domaine parent de l'hôte de `PUBLIC_URL`, au moins deux libellés, ni suffixe public ni adresse IP ([détail](#second-facteur-totp-et-codes-de-secours)) | hôte de `PUBLIC_URL` |
| `ROUTING_ALLOW_CLEARTEXT_KEY` | `1` autorise l'envoi d'une clé API du moteur (`routing.api_key`) en **clair** (URL `http://`). Sans lui, une clé configurée pour une destination `http` fait déclarer le moteur indisponible (avertissement dans les journaux). Le moteur du compose n'a **pas besoin de clé** : ne pas saisir de `routing.api_key` et laisser à `0` | `0` |
| `ROUTING_BASE_URL` | URL du moteur de routage (`http://graphhopper:8989` avec le service du compose). Valeur initiale du réglage `routing.base_url` ; autorisée d'office pour le service `routing` quand elle vient de l'environnement. Voir [Service de routage](#service-de-routage-graphhopper) | vide |
| `ROUTING_XMX`, `ROUTING_MEM_LIMIT`, `ROUTING_IMPORT_XMX`, `ROUTING_IMPORT_MEM_LIMIT` | Mémoire du moteur (tas Java, limite du conteneur) et de l'import mensuel ; lues par `docker-compose.yml` et `routing/update.sh`, pas par l'appli | `1536m`, `2g`, `6g`, `8g` |
| `ROUTING_EXTRACT_URLS`, `ROUTING_ALLOWED_HOSTS`, `ROUTING_KEEP_GRAPHS`, `ROUTING_HEALTH_TIMEOUT`, `ROUTING_MAX_DOWNLOAD_BYTES`, `ROUTING_LOCK` | Lues par `routing/update.sh` seulement (voir [Service de routage](#service-de-routage-graphhopper)) | voir le script |
| `DATA_DIR`, `DB_PATH`, `INBOX_DIR` | Dossier de données (`/data` dans l'image), base SQLite (`DATA_DIR/kmlviewer.sqlite3`), dossier surveillé (`/inbox` avec le compose) | — |
| `LOG_LEVEL` | Niveau de logs | `INFO` |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_ADMIN_ROLE` | Client OIDC ; `OIDC_ADMIN_ROLE` : le droit admin suit ce rôle à chaque connexion | — |
| `KMLVIEWER_MEM_LIMIT` | Limite mémoire du conteneur (lue par `docker-compose.yml`) | `2g` |

#### `SECRETS_KEY` et rotation

Générer une clé :

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# `python` + `cryptography` disponibles sur le serveur : commande ci-dessus. Sinon, la même commande dans l'image de test (vérifié) :
docker run --rm --entrypoint python ghcr.io/ttiot/kmlviewer:unstable-feature-admin-space-and-route-builder -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Une valeur mal formée (autre chose qu'une clé Fernet) refuse le démarrage ; une valeur chiffrée en base
qu'aucune clé fournie ne déchiffre aussi (le message donne le nombre de valeurs). Rotation :
`SECRETS_KEY=<nouvelle>,<ancienne>`, recréer le conteneur, `flask secrets-rotate`, retirer l'ancienne,
recréer à nouveau ; clé perdue : `flask secrets-reset`. Pas à pas : [docs/exploitation.md](docs/exploitation.md).

#### `OUTBOUND_ALLOWED_HOSTS` : destinations sortantes

Toute adresse vers laquelle l'application envoie des données d'utilisateurs ou un secret est une
frontière de sécurité. La variable liste, **par service**, ce qui est permis. Syntaxe : entrées
séparées par des virgules, de la forme `service=destination`.

```text
OUTBOUND_ALLOWED_HOSTS=routing=graphhopper:8989,dem=opentopodata:5000,smtp=relay.example.com:587,tiles=a.tile.openstreetmap.org
```

| Service | Ce qu'il autorise | Destination attendue |
|---------|-------------------|----------------------|
| `routing` | `routing.base_url` (moteur de routage, utilisé par le [constructeur de traces](#constructeur-de-traces-bêta)) | `hôte:port` |
| `dem` | `DEM_URL` (service d'altitude) | `hôte:port` |
| `smtp` | `smtp.host` + `smtp.port` (envoi d'e-mails) | `hôte:port` |
| `tiles` | hôtes des fonds de carte (navigateur des utilisateurs) | `hôte` seul (https, 443) |

- **Un hôte autorisé pour un service ne vaut pas pour un autre** (`dem=h:5000` n'ouvre rien à `smtp`).
- **Port obligatoire** pour `routing`, `dem` et `smtp`, comparé exactement au port réel (`https://moteur`
  vaut le port 443). Pas de port sur `tiles`.
- **Littéral IPv6 entre crochets** : `routing=[fd00::1]:8989`.
- Une entrée invalide (port manquant, IPv6 sans crochets, service inconnu) **refuse le démarrage**.
- `DEM_URL` (et `ROUTING_BASE_URL`) posée **dans l'environnement** est autorisée d'office, pour son seul
  service : le `.env.example` n'a rien à ajouter pour un `DEM_URL` d'env. Une destination posée par
  l'administration (base) doit au contraire figurer dans la liste, sinon elle est refusée à l'écriture (400) et à l'usage.
- La liste vide n'autorise aucune destination nouvelle depuis l'administration.
- Comparaison sur le nom, sans résolution DNS ; les redirections HTTP ne sont jamais suivies.
- Fonds de carte : sans entrée `tiles=`, les hôtes des 5 fonds embarqués restent autorisés (aucun
  changement à la mise à jour). **La première entrée `tiles=` remplace toute cette liste par défaut.**
  La CSP `img-src` vaut `'self' data: blob:` plus exactement les hôtes de tuiles autorisés : une carte
  grise après avoir posé un `tiles=` signifie qu'il manque un hôte (par exemple `a|b|c.tile.openstreetmap.org`,
  `server.arcgisonline.com`, `a|b|c.tile.opentopomap.org`, `a|b|c.basemaps.cartocdn.com`). La liste est lue
  au démarrage : ajouter un fournisseur de tuiles = ajouter son `tiles=`, recréer le conteneur, puis saisir
  le fond dans l'administration (réglage `map.basemaps`, onglet Réglages).
- Une destination effective qui sort de la liste (liste réduite après coup) est refusée à l'appel, inscrite
  au journal d'audit (dédoublonnée), et ne reçoit jamais de secret.

### Réglages administrables : base > env > défaut

Un registre borné (`web-app/app/settings/registry.py`) liste ce qu'un admin peut changer sans
redémarrer. Chaque réglage a des bornes **basse et haute** obligatoires, et une variable d'environnement
facultative qui donne sa valeur initiale.

- **Valeur effective** : la valeur en base si elle existe, sinon la variable d'environnement, sinon le défaut.
- **Valeur d'environnement invalide** (illisible ou hors bornes) : **ignorée**, le défaut s'applique,
  `WARNING` au démarrage (le nom de la variable, jamais sa valeur). Jamais de refus de démarrage pour un
  réglage du registre. Des variables jadis libres sont désormais bornées : `INBOX_SCAN_INTERVAL` < 5 vaut
  maintenant 30 (et non plus 5), `DEM_TOTAL_TIMEOUT` > 600 vaut 90, `DEM_TIMEOUT` < 1 vaut 15.
- **Surcharge** : une variable d'environnement masquée par une valeur en base produit un `WARNING` au
  démarrage et un marqueur « surcharge `VAR` » dans le diagnostic. L'admin prime sur `.env` : modifier le
  fichier ne change rien tant que la valeur en base existe.
- **Effet immédiat** : toute écriture incrémente une version en base. Le serveur web la relit à chaque
  requête, l'ordonnanceur à chaque tour (5 s au plus) : une valeur changée (même par la CLI, depuis un autre
  processus) s'applique à la requête ou au tour suivant, y compris l'activation du dossier surveillé et
  l'intervalle des tâches. Aucun réglage n'exige de redémarrage.
- **Diagnostic** : `flask settings-list` (valeur, source `base` / `env` / `défaut`, surcharge). Exemple :

  ```text
  INBOX_SCAN_INTERVAL = 30  (défaut)
  USER_QUOTA_MB = 2048  (env)
  DEM_TIMEOUT = 15.0  (défaut)  [DEM_TIMEOUT invalide, ignorée]
  ```

- **Retour à la valeur d'environnement ou au défaut** : `flask settings-reset <clé>` (une valeur) ou
  `flask settings-reset --all` (toutes les valeurs en base). Tracé dans l'audit.
- **Écrire une valeur en base** : dans l'espace d'administration (onglet Réglages) ou par l'API
  (`PATCH /api/admin/settings`, `DELETE /api/admin/settings/<clé>` pour revenir au défaut,
  `GET /api/admin/settings/diagnostic`). Une écriture invalide est refusée (400, avec le champ) et rien n'est écrit.

| Réglage | Variable d'env | Défaut | Bornes |
|---------|----------------|--------|--------|
| `USER_QUOTA_MB` (quota de stockage par utilisateur, Mo) | `USER_QUOTA_MB` | 2048 | 0 – 102 400 (0 = illimité) |
| `audit.retention_days` (rétention du journal d'audit) | — | 90 | 30 – 3650 |
| `OIDC_AUTO_ACTIVATE` | `OIDC_AUTO_ACTIVATE` | non | oui / non |
| `auth.invite_ttl_hours` (validité d'une invitation) | — | 168 | 1 – 720 |
| `auth.mfa_policy` (second facteur obligatoire pour) | — | `admins` | `admins` \| `all` |
| `auth.forgot_enabled` (mot de passe oublié ; exige le SMTP) | — | oui | oui / non |
| `INBOX_ENABLED` | `INBOX_ENABLED` | oui | oui / non |
| `INBOX_SCAN_INTERVAL` (s) | `INBOX_SCAN_INTERVAL` | 30 | 5 – 3600 |
| `INBOX_DEFAULT_USER` | `INBOX_DEFAULT_USER` | vide | ≤ 254 car. |
| `DEM_URL` (destination sortante, service `dem`) | `DEM_URL` | vide | URL http(s) ≤ 500 car. |
| `DEM_BATCH_SIZE` | `DEM_BATCH_SIZE` | 100 | 1 – 1000 |
| `DEM_TIMEOUT` (s) | `DEM_TIMEOUT` | 15 | 1 – 120 |
| `DEM_TOTAL_TIMEOUT` (s) | `DEM_TOTAL_TIMEOUT` | 90 | 10 – 600 |
| `DEM_REQUEST_DELAY` (s) | `DEM_REQUEST_DELAY` | 0 | 0 – 10 |
| `DEM_BACKFILL_BATCH` | `DEM_BACKFILL_BATCH` | 25 | 1 – 1000 |
| `DEM_BACKFILL_INTERVAL` (s) | `DEM_BACKFILL_INTERVAL` | 300 | 30 – 3600 |
| `APP_TIMEZONE` | `APP_TIMEZONE` | `Europe/Paris` | fuseau IANA connu |
| `smtp.host`, `smtp.port`, `smtp.security`, `smtp.username`, `smtp.sender`, `smtp.timeout_s` | — | vide, 587, `starttls`, vide, vide, 10 | voir [E-mail](#e-mail-et-notifications) |
| `ratelimit.password_per_identifier` (tentatives / 15 min / identifiant) | — | 5 | 3 – 20 |
| `ratelimit.password_per_ip` (/ 15 min / IP) | — | 20 | 10 – 100 |
| `ratelimit.mfa_per_account` (**échecs** de second facteur / 15 min / compte) | — | 5 | 3 – 10 |
| `ratelimit.forgot_per_identifier` (mot de passe oublié, / h / identifiant) | — | 3 | 1 – 10 |
| `ratelimit.forgot_per_ip` (/ h / IP) | — | 10 | 10 – 100 |
| `ratelimit.token_per_ip` (essais de jeton, / 15 min / IP) | — | 10 | 5 – 30 |
| `map.basemaps`, `map.default_basemap`, `map.center_lat`, `map.center_lon`, `map.zoom` | — | 5 fonds embarqués, `osm`, 46,603354 / 1,888334, zoom 6 | ≤ 10 fonds ; lat −85 – 85, lon −180 – 180, zoom 1 – 22 |
| `routing.*` (traces planifiées : délais, budgets, appels, points, distance, tolérances, recouvrement, stratégie de détours `s1` / `s2` / `hybrid` (défaut `hybrid`), essais de graine 0 – 2, `routing.base_url` ← `ROUTING_BASE_URL`) | `ROUTING_BASE_URL` pour l'URL | voir [Constructeur de traces](#constructeur-de-traces-bêta) | bornées dans le registre |
| `routing.coverage_polygon` (zone couverte : sommets `[lon, lat]`) | — | 4 sommets du rectangle `[1.35, 48.83, 6.42, 51.55]` | 3 à 50 sommets, lon −180 – 180, lat −85,0511 – 85,0511, contour sans croisement, emprise ≤ 2 000 km de côté ; `[]` refusé |

Les seuils du limiteur s'appliquent à chaud, sans remettre les compteurs à zéro. Ne sont **pas**
administrables (environnement ou code seulement) : tout ce qui figure dans « Variables d'amorçage et de
sécurité », `DEM_RETRIES`, `DEM_RETRY_BACKOFF`, `DEM_MAX_GAP_M` et les limites dures contre les abus
(taille de fichier, nombre de points, cache).

**Fonds de carte** : le visualiseur et la page des tronçons lisent `GET /api/map-config` (fonds dont **tous** les
hôtes sont autorisés par `tiles=`, fond par défaut, cadrage) ; les 5 fonds embarqués
(`web-app/static/js/viewer/map.js`) ne servent que d'attente avant la réponse. Un fond saisi dans l'administration dont
un hôte n'est pas dans `tiles=` est refusé à l'écriture (400) ; un fond devenu non autorisé disparaît de la liste envoyée
au navigateur.

### E-mail et notifications

L'application envoie des e-mails en texte brut, en français, par SMTP : invitation, validation d'adresse,
mot de passe oublié, notifications de sécurité. Tous les liens sont construits sur `PUBLIC_URL`.

- **Réglages** : `smtp.host` et `smtp.sender` (si l'un des deux est vide, ou si la destination n'est pas dans la
  liste blanche : aucun e-mail), `smtp.port` (587), `smtp.security`, `smtp.username`, `smtp.timeout_s` (10 s), plus le **secret** `smtp.password`. Il n'existe **aucune variable
  d'environnement SMTP** : ils s'écrivent par l'API d'administration (`/api/admin/settings`,
  `/api/admin/secrets`), c'est-à-dire dans l'onglet « Clés API et e-mail » de l'administration. Tant que le SMTP n'est pas
  réglé, aucun e-mail ne part : les liens d'invitation et d'amorçage sont montrés à l'émetteur, qui les transmet lui-même,
  et les adresses restent « en attente de validation ». Un `WARNING` au démarrage le rappelle en mode `password`.
- **Sécurité** : `starttls` (par défaut ; si la négociation échoue, l'envoi échoue, **jamais de repli en
  clair**), `tls` (TLS dès la connexion) ou `none` (**aucune authentification n'est envoyée**, un identifiant
  ou un mot de passe enregistré avec `none` est refusé). Le certificat du serveur est toujours vérifié.
- **Liste blanche** : `smtp=<hôte>:<port>` dans `OUTBOUND_ALLOWED_HOSTS`, contrôlée à chaque envoi.
- **Mot de passe SMTP lié à sa destination** : il n'est envoyé qu'à l'hôte et au port pour lesquels il a été
  saisi. Changer `smtp.host` ou `smtp.port` (ou `settings-reset`) **efface** le mot de passe, à ressaisir.
  Jamais réaffiché, jamais journalisé.
- **Deux modes d'envoi** : synchrone (invitation, e-mail de test : l'émetteur sait si le message est parti)
  et file d'envoi en mémoire pour le reste (1 000 messages au plus ; perdue au redémarrage, journalisé).
  Un échec n'affiche jamais de détail SMTP.
- **Notifications des commandes CLI** : envoyées **de façon synchrone** avant que la commande rende la main,
  si le SMTP est configuré. Un échec d'envoi n'annule pas l'action (faite et tracée) : la commande le dit et
  sort avec le **code 1**.

  | Commande | Destinataires | Message |
  |----------|---------------|---------|
  | `user-invite --admin` | l'adresse visée | le lien d'amorçage |
  | `user-mfa-reset` | l'adresse validée du compte | facteurs réinitialisés |
  | `secrets-reset` | les admins actifs ayant une adresse, **avant** l'effacement | secrets effacés |
  | `settings-reset` (`smtp.*` ou `--all`) | les admins actifs ayant une adresse, **avant** le changement | configuration e-mail réinitialisée |

  Sans SMTP configuré, la commande le signale (« E-mail non configuré ») et réussit.
- **Mot de passe oublié** : actif dès que le SMTP est configuré (désactivable : `auth.forgot_enabled`) ; la réponse est
  toujours la même (compte existant ou non) et l'envoi se fait hors requête ; lien valable 1 h, qui ne retire pas le
  second facteur.
- **Validation d'adresse** : toute adresse d'un compte à mot de passe est validée par un aller-retour (lien valable 48 h,
  à ouvrir **connecté au compte concerné**, sinon rien n'est validé). Tant qu'elle ne l'est pas, le compte fonctionne mais ne
  peut ni inviter ni utiliser « mot de passe oublié ».

### Mots de passe : politique et liste de mots de passe courants

Politique des comptes à mot de passe : 12 à 128 caractères (mesurés après normalisation NFKC), aucune règle
de composition, refus s'il est égal à l'identifiant ou à l'adresse e-mail (casse ignorée) ou s'il figure dans
la liste de mots de passe courants. Hachage argon2id (64 Mo, 3 passes), au plus **2 hachages en parallèle**
(une 3e requête attend 5 s au plus, puis reçoit un 429 identique pour tous). Mémoire du conteneur :
[docs/exploitation.md](docs/exploitation.md).

La liste est embarquée dans l'image (`web-app/app/auth/wordlists/`) ; l'application **ne télécharge
jamais rien** :

| Fichier | Contenu |
|---------|---------|
| `common_passwords.txt` | 46 187 entrées (12 caractères et plus, NFKC + casefold, triées, sans doublon), SHA-256 `ec1dce68…fecaa` |
| `common_passwords_local.txt` | 71 entrées propres au projet (dont des mots de passe français), fusionnées à la génération |
| `common_passwords.SOURCE` | provenance, empreintes, effectifs, avis de licence |

Source publique : SecLists (`xato-net-10-million-passwords-1000000.txt`, commit figé, licence MIT du dépôt ;
données publiées par Mark Burnett en 2015, dont le statut juridique n'a pas pu être vérifié, voir
`common_passwords.SOURCE`). Régénération, sur décision seulement (réseau requis, ou fichier local en argument ;
le SHA-256 de la source est vérifié, sinon arrêt sans rien écrire) :

```bash
cd web-app
python -I scripts/build_common_passwords.py [fichier_source] [--out-dir DOSSIER]
```

Relire les effectifs et le nombre d'entrées ajoutées et retirées qu'affiche le script, mettre à jour
`common_passwords.SOURCE`, puis committer. La liste est chargée **au démarrage**, quand les comptes à mot de
passe sont disponibles (mode `password`, ou `oidc` avec `SECRETS_KEY` et `PUBLIC_URL`) ; absente, illisible ou
vide, elle **refuse le démarrage** (jamais de politique silencieusement affaiblie). En mode `local`, elle n'est
pas chargée.

### Routes ajoutées

| Route | Accès | Rôle |
|-------|-------|------|
| `GET /auth/token`, `POST /auth/token/inspect`, `POST /auth/token/consume` | publiques | Page et API de consommation d'un lien à usage unique (invitation, amorçage, réinitialisation, validation d'adresse, conversion SSO). Le jeton est dans le **fragment** de l'URL (`/auth/token#t=…`), jamais dans le chemin ni la requête, donc absent des journaux d'accès |
| `/auth/password/login`, `/auth/password/mfa`, `/auth/password/enroll`, `/auth/password/forgot` | publiques (les deux premières étapes exigent leur état de connexion) | Connexion en deux étapes, enrôlement, mot de passe oublié |
| `GET /api/me`, `POST /api/me/password`, `POST /api/me/email`, `/api/me/mfa/*` | session complète, compte à mot de passe | Profil de sécurité : mot de passe, adresse, second facteur, codes de secours |
| `GET/POST/DELETE /api/invitations` | session complète ; comptes à mot de passe disponibles | Ses invitations (profil › Invitations), création, révocation |
| `/api/admin/*`, `/admin` | admin | [Espace d'administration](#espace-dadministration-admin) |
| `GET /api/map-config` | session | Fonds de carte autorisés, fond par défaut, cadrage |
| `GET /api/route-builder/options`, `POST /api/route-builder/route`, `POST /api/route-builder/save` | session ; fonction bêta ouverte à l'utilisateur **et** moteur configuré (sinon 404) | [Constructeur de traces](#constructeur-de-traces-bêta) |

Invitation : une adresse cible, validité `auth.invite_ttl_hours` (7 jours par défaut), lien montré **une seule fois** à
l'émetteur (et envoyé par e-mail si le SMTP est configuré). Un admin invite sans limite ; un utilisateur actif, à adresse
**validée**, avec `invite_credits ≥ 1` (un admin règle les crédits ; crédit non rendu en cas de révocation ou
d'expiration). Le compte créé est actif, adresse **en attente** de validation, et n'est jamais admin. Un lien consommé
n'ouvre jamais de session : il renvoie à la page de connexion (sauf l'amorçage, qui mène d'abord à l'enrôlement).

### Service de routage (GraphHopper)

Le moteur de routage du [constructeur de traces](#constructeur-de-traces-bêta) est livré comme **service du compose**. Sans lui
(ou tant que `routing.base_url` est vide), le constructeur n'apparaît pas dans l'application.

- **Service** : `graphhopper` (profil compose `routing`), image **construite par le projet** (`routing/Dockerfile` : jar de
  la release GraphHopper 11.0, SHA-256 vérifié, sur Temurin 21 épinglé par empreinte), conteneur durci (non-root uid 10002,
  système de fichiers en lecture seule, capacités supprimées). **Aucun port publié** ; il n'est joignable que par `kml-viewer`
  sur le réseau interne `kml-viewer-routing` (sans accès sortant).
- **Variables** (dans `.env`) : `ROUTING_BASE_URL=http://graphhopper:8989` pour l'application (autorisée d'office pour le
  service `routing` quand elle vient de l'environnement ; posée dans l'administration, elle doit figurer dans
  `OUTBOUND_ALLOWED_HOSTS` : `routing=graphhopper:8989`), puis `ROUTING_XMX` (tas du service, défaut `1536m`),
  `ROUTING_MEM_LIMIT` (défaut `2g`), `ROUTING_IMPORT_XMX` (défaut `6g`), `ROUTING_IMPORT_MEM_LIMIT` (défaut `8g`).
- **Données** : le graphe n'est **jamais** produit au démarrage. Sans graphe, le conteneur `graphhopper` redémarre en
  boucle : ne pas démarrer le profil `routing` avant le premier graphe, **lancer d'abord `routing/update.sh`**, qui crée le
  volume `routing-data`, télécharge les extraits Geofabrik
  (Nord-Pas-de-Calais, Picardie, Belgique ; sommes MD5 vérifiées), les fusionne (osmium), importe dans un dossier
  horodaté, bascule le lien `current` de façon atomique, recrée le moteur et attend sa santé. En cas d'échec de santé,
  l'ancien graphe est rétabli. Relançable sans risque (verrou : une seule exécution à la fois).
- **Mise à jour mensuelle** : `routing/update.sh` depuis l'hôte, dans le dossier du projet, chaque mois (timer systemd ou cron
  posé par Ansible). Il a besoin de Docker, de `flock` et d'un accès à `download.geofabrik.de` (liste `ROUTING_ALLOWED_HOSTS`).
  Réglages : `ROUTING_EXTRACT_URLS`, `ROUTING_ALLOWED_HOSTS`, `ROUTING_KEEP_GRAPHS` (2 à 20, défaut 2, courant compris),
  `ROUTING_HEALTH_TIMEOUT` (5 à 3600 s, défaut 300), `ROUTING_MAX_DOWNLOAD_BYTES`, `ROUTING_LOCK` ; lus dans l'environnement
  du processus, puis dans `.env`, puis valeur par défaut, et validés avant usage.
- **Dimensionnement** (mesures sur la machine cible « dune », Xeon E5-1650 v3, 12 cœurs, 64 Go, Hauts-de-France + Belgique,
  6 profils ; [ADR-0007](docs/adr/0007-moteur-routage.md)) : import **154 s** (`-Xmx6g`, pic conteneur 6,2 Go), fusion 41 s,
  graphe **320 Mo**, service prêt en **3 s**, RAM du conteneur après charge **1,2 Go** avec `-Xmx1g` (3,24 Go avec `-Xmx3g`),
  latence d'un appel `route` à 5 points p95 **≤ 378 ms**. Rejeu avec l'image du projet (jar de release) : **`-Xmx1536m` dans
  2 Go validé** (1,43 Gio après 256 appels dont 8 en parallèle, aucun OOM) ; disque **2,4 Go** avec un graphe, **pic ~4 Go**
  pendant une mise à jour : **prévoir 5 Go libres**. Prévoir ~8 Go de mémoire disponible pendant l'import mensuel.
  Les journaux du moteur ne contiennent aucune coordonnée mais ne sont pas bornés par le compose : à limiter côté Docker
  ([docs/exploitation.md](docs/exploitation.md#journal-du-moteur)).
- **Profil gravel** : il évite surtout le bitume sans viser le gravier (peu de surfaces taguées dans OSM) ; le jugement
  terrain revient à l'utilisateur.

Activer le service avec Ansible : `COMPOSE_PROFILES=routing` (testé) plutôt que l'option `profiles` du module
`docker_compose_v2` (non confirmée), et seulement **après** le premier `update.sh`. Retour arrière manuel, suppression d'un
graphe rejeté, cron : [docs/exploitation.md](docs/exploitation.md#service-de-routage).

### Constructeur de traces (bêta)

Créer sur la carte une **boucle planifiée** à partir de points de passage, une activité et une distance voulue, puis
l'enregistrer dans la bibliothèque. Fonction **bêta**, fermée par défaut.

**Activer** (admin) :

1. Le moteur tourne ([Service de routage](#service-de-routage-graphhopper)) et son adresse est connue de l'appli :
   `ROUTING_BASE_URL=http://graphhopper:8989` dans `.env`, ou le réglage `routing.base_url` (onglet Réglages) ; saisie dans
   l'administration, l'adresse doit figurer dans `OUTBOUND_ALLOWED_HOSTS` (`routing=graphhopper:8989`).
2. Onglet **Fonctions bêta** › « Créer une trace » : **Désactivée** (défaut), **Pour une liste de comptes** (cocher les
   comptes) ou **Pour tous**. La liste est conservée quand on passe à « Pour tous » puis qu'on revient. Le diagnostic affiché
   dit si le moteur est configuré et joignable, la date du dernier sondage et si l'emprise de la zone couverte tient dans la zone
   annoncée par le moteur.
3. Le bouton n'existe **que si la fonction est ouverte à l'utilisateur ET le moteur configuré** (adresse autorisée) :
   sinon il n'y a aucun élément dans la page et `/api/route-builder/*` répond 404. Le moteur est sondé au démarrage puis toutes
   les `routing.health_interval_s` secondes (60 par défaut, 15 à 600) ; tant que le premier sondage n'a pas réussi, la
   génération répond « Service de routage en cours de démarrage » (503).

**Utiliser** (visualiseur › « Créer une trace ») :

1. **Poser des points** : cliquer sur la carte (points numérotés, 1 à `routing.max_waypoints` = 50). **Cliquer SUR un chemin
   ou une route** : le moteur rattache chaque point à la voie la plus proche, et un point à plus de
   `routing.snap_tolerance_m` (50 m par défaut) d'une voie praticable est refusé (voir les erreurs plus bas). Glisser un point
   pour le déplacer ; cliquer ou appuyer sur un point ouvre un menu **Retirer ce point** / **Insérer avant** (clic droit pour
   retirer directement) ; « Annuler le dernier » et « Rétablir » (100 états, aussi `Ctrl+Z` / `Ctrl+Y` ou `Ctrl+Maj+Z` hors des
   champs de saisie) ; « Tout effacer » demande une confirmation dans la page. Une ligne en pointillé relie les points en
   boucle fermée et la longueur à vol d'oiseau s'affiche ; le navigateur prévient avant de quitter la page tant que des
   points n'ont pas été transformés en trace. Le contour en pointillé est la **zone couverte** ; un point hors de la zone est
   refusé (« Hors de la zone couverte. »).
2. **Choisir l'activité** : Course à pied (profil `foot`), Trail et Randonnée (`hike`), Vélo de route (`racingbike`), Gravel
   (`gravel`, stocké comme vélo de route), VTT (`mtb`) ; la valeur par défaut suit l'activité par défaut du profil.
3. **Distance voulue** (km, 1 à `routing.max_distance_km` = 300, pas de 0,5 dans le formulaire). **Obligatoire avec un seul
   point** (boucle à distance cible autour de ce point) ; facultative avec plusieurs : sans distance, c'est la boucle la plus
   directe qui passe par les points.
4. **Terminer** : le serveur calcule une **boucle fermée sur le point 1**, qui passe par les points **dans l'ordre**, avec des
   détours pour s'approcher de la distance voulue (tolérance `routing.distance_tolerance_pct` = ±15 %, stratégie
   `routing.detour_strategy`, essais de graine `routing.seed_retries`). Le résultat s'affiche sur la carte avec sa distance, le
   D+ quand il est connu et l'écart à la distance voulue. « Annuler » abandonne l'attente (le serveur termine dans son budget).
5. **Autre proposition** (mêmes points, graine suivante) : le serveur écarte toute trace identique à celles déjà proposées
   dans la série (même géométrie) en essayant les graines suivantes dans la limite de `routing.seed_retries` ; s'il n'en trouve
   pas d'autre, il rend la meilleure avec l'avertissement « Aucune autre proposition différente trouvée pour ces points. »
   (ajouter ou déplacer un point en donne de nouvelles). **Modifier les points** (revenir aux points), ou donner un **nom**
   puis **Enregistrer**. Un second « Enregistrer » du même résultat répond « Déjà enregistrée ». Un résultat non enregistré est
   gardé 30 minutes côté serveur (20 par utilisateur au plus) : au-delà, « Résultat introuvable ou expiré » (404).
6. **Bibliothèque** : la trace enregistrée porte le badge **Planifiée** ; le filtre « Nature des traces » propose **Toutes**
   (défaut), **Réalisées**, **Planifiées**. Elle est **exclue des statistiques** : le tableau de bord (distances, durées,
   records, compteurs) ne compte que les traces réalisées, et aucun effort sur tronçon ni meilleur temps n'est calculé pour une
   trace planifiée. Ses points ne quittent pas le navigateur avant « Terminer » (conservés localement par compte, effacés à la
   déconnexion).

**Avertissements** (liste sous le résultat ; la trace est tout de même proposée) :

| Message | Cause |
|---------|-------|
| « Le point N est confondu avec le point 1 : retiré. » | le dernier point est à côté du premier (la boucle se ferme déjà) |
| « Distance minimale passant par tes points : X km. » | les points imposent déjà plus que la distance voulue (+ tolérance) |
| « Distance obtenue : X km pour Y km voulus (±Z %). » | hors tolérance après les essais ; avec **2 points** s'ajoute « Ajoute un point de passage pour une boucle plus proche de la distance voulue. » |
| « La trace emprunte plusieurs fois les mêmes voies. » | recouvrement au-dessus de `routing.overlap_warning_pct` (25 %), de `routing.overlap_warning_pct_2pts` (40 %) avec 2 points |
| « Aucune autre proposition différente trouvée pour ces points. » | « Autre proposition » n'a rien trouvé de différent des traces déjà vues |
| avertissements du moteur | repris tels quels |

**Erreurs** (message fixe, jamais de coordonnée ni d'adresse du moteur ; N est le numéro du point **tel que posé**) : 400 entrée
invalide, sans appel au moteur : points, activité, distance (« Distance : de 1 à 300 km »), **« Le point N est hors de la zone
couverte. »** (hors `routing.coverage_polygon`), « Distance requise avec un seul point », « Points trop éloignés les uns des
autres » ; 422 **« Le point N est trop loin d'une voie praticable : rapproche-le d'un chemin. »** (le point est dans la zone
mais à plus de `routing.snap_tolerance_m` d'une voie : le **déplacer sur un chemin**) ou « Aucune trace possible avec ces
points : essaie d'autres points. » ;
429 « Une génération est encore en cours » (une par utilisateur), « Service occupé » (`routing.max_concurrent` = 2 pour l'instance),
« Trop de générations » (`routing.rate_limit_per_user` = 30 par heure et par utilisateur) ; 502 réponse invalide, 503 moteur
indisponible, 504 délai dépassé (`routing.timeout_s`, budget `routing.total_budget_s`, appels `routing.max_calls`).

**Zone couverte (`routing.coverage_polygon`)** : polygone de 3 à 50 sommets `[lon, lat]`, **seul réglage de zone** ; défaut : les 4
sommets du rectangle `[1.35, 48.83, 6.42, 51.55]` (`[[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]`, Hauts-de-France
et Belgique, resserré sur l'emprise réelle du moteur : le diagnostic de la fonction indique si l'emprise de la zone tient dans la
zone annoncée par le moteur). Le serveur refuse (400) toute demande dont un point en sort, avant tout appel au moteur ; le
rectangle annoncé par le moteur (`/info`) est trop large pour servir de zone. Saisie : onglet Réglages, éditeur sur carte (déplacer,
ajouter, retirer des sommets) ou liste de sommets ; valeur refusée hors de 3 à 50 sommets (`[]` compris), hors des limites de
la carte, si le contour se croise ou si l'emprise dépasse 2 000 km de côté ; `flask settings-reset routing.coverage_polygon` revient
au défaut. L'ancien réglage `routing.coverage_bbox` est supprimé : la migration `0017_coverage_polygon_only` reprend un rectangle
personnalisé en 4 sommets. La zone filtre les demandes côté application, elle ne réduit pas le graphe du moteur.

**Altitude** : le moteur n'est **pas** interrogé pour l'altitude (aucun fournisseur d'altitude dans `routing/config.yml`) ; la
trace générée n'a donc **ni profil d'altitude ni D+ avant l'enregistrement**. À l'enregistrement, la correction d'altitude par
MNT est confiée à la tâche de fond (réveillée aussitôt) : elle ne s'applique que si `DEM_URL` est configurée et si les dalles
couvrent la zone (`DEM_BBOX` à élargir aux Hauts-de-France et à la Belgique) ; sinon la trace reste sans altitude.

### Non livré

Mis à jour le 2026-11-25 d'après le code (HEAD `37ad93c`) et `STATUS.md`. Les fonctions prévues par
[docs/specs/admin-space.md](docs/specs/admin-space.md) et [docs/specs/gpx-route-builder.md](docs/specs/gpx-route-builder.md)
sont **toutes livrées**, WebAuthn et constructeur de traces compris. Restent, par ordre d'importance pour l'exploitant :

| Point | Conséquence aujourd'hui |
|-------|-------------------------|
| Appréciation visuelle des traces **gravel** | À faire par l'utilisateur sur l'échantillon du spike (`spike/detours/out/echantillon/`) ; la qualité terrain du profil `gravel` (il évite surtout le bitume sans viser le gravier) n'est pas jugée |
| Mot de passe oublié sans SMTP | Aucun mail ne part : un admin génère un lien de réinitialisation (onglet Utilisateurs) et le transmet |
| `./restart.sh` ne relit pas `.env` | Outil de développement local ; en déploiement, recréer le conteneur (`docker compose up -d`). Modification du script en attente de décision |
| Diagnostic « fond par défaut indisponible » (nit T16-n3) | Si le fond de carte par défaut devient non autorisé, la carte retombe sur le premier fond autorisé sans le signaler dans l'administration |

### Limites connues du déploiement

- **Service d'altitude `opentopodata` (profil `dem`)** : contrairement au moteur de routage, il n'est **pas durci** : il tourne
  en **root**, sans `cap_drop`, sans système de fichiers en lecture seule, sans limite de mémoire ni de processus (seul
  `no-new-privileges` est posé), sur le réseau par défaut (accès sortant) ; il reçoit les coordonnées des traces. Il n'a aucun port
  publié et son image est épinglée sur un commit. Durcissement envisagé (même protections que `graphhopper`, réseau interne
  dédié) : non fait.
- **Journaux des conteneurs** : aucune rotation n'est configurée par le compose, voir
  [Journaux des conteneurs](docs/exploitation.md#journaux-des-conteneurs--prévoir-une-rotation).
- **Image locale** : `docker compose up -d` ne reconstruit pas une image `kml-viewer:local` déjà présente, voir
  [Mise à jour](docs/exploitation.md#sauvegarde-et-mise-à-jour).

## 🐛 Dépannage

- **Port déjà utilisé** : changer le port dans `docker-compose.yml`, ou trouver le service qui
  l'occupe (`sudo lsof -i :8080`).
- **Fichier refusé** : le message indique la cause (extension, encodage UTF-8 attendu, archive
  KMZ illisible). Les détails d'une erreur interne sont dans les logs : `./logs.sh`.
- **Fond de carte vide** : les tuiles viennent de serveurs tiers, une connexion Internet est
  nécessaire ; le reste de l'application fonctionne hors ligne. Si le fond était affiché avant que
  `OUTBOUND_ALLOWED_HOSTS` ne reçoive une entrée `tiles=`, l'hôte de ce fond n'y figure pas (la CSP
  `img-src` bloque tout autre hôte) : voir [OUTBOUND_ALLOWED_HOSTS](#outbound_allowed_hosts--destinations-sortantes).
- **Le conteneur redémarre en boucle** : le motif du refus de démarrer est dans `./logs.sh`
  (`SECRET_KEY` par défaut, `TRUSTED_HOSTS` manquant, `PUBLIC_URL` non couverte, valeur chiffrée
  indéchiffrable avec `SECRETS_KEY`, entrée invalide d'`OUTBOUND_ALLOWED_HOSTS`…).
- **Une modification de `.env` n'a aucun effet** : `docker compose restart` et `./restart.sh` ne relisent
  pas `.env` ; recréer le conteneur (`docker compose up -d`, ou le playbook Ansible). Pour un réglage administrable, vérifier aussi qu'une valeur
  en base ne le masque pas (`flask settings-list`).
- **Flèches du clavier sans effet** : un fichier contenant des points doit être chargé, et le
  curseur ne doit pas être dans un champ de saisie.

## 📄 Licence

Ce projet est libre d'utilisation.
