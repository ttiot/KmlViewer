# Bibliothèque d'activités, tronçons et authentification

## Ce que ça fait

| Fonction | Où | Détail |
|---|---|---|
| Dossier surveillé | `INBOX_DIR` | Chaque `.gpx`/`.kml`/`.kmz` déposé est parsé, résumé puis stocké en SQLite. Les fichiers ne sont jamais déplacés ni modifiés. |
| Import depuis l'UI | Tableau de bord → « Importer », ou glisser-déposer du visualiseur (case « Enregistrer dans ma bibliothèque ») | Même pipeline que le dossier surveillé. |
| Tableau de bord | `/dashboard` | km, D+ et temps par semaine / mois / année, records, liste des activités. |
| Tronçons | `/segments` | Un tronçon se définit en deux clics sur une trace ; il est recherché dans toutes les traces, existantes et futures. |
| Correction d'altitude | `DEM_URL` | D+ recalculé sur un MNT (API OpenTopoData). Le D+ GPS reste conservé à côté. |
| Utilisateurs | OIDC + `/admin/users` | Chaque activité et chaque tronçon appartient à un utilisateur et n'est visible que de lui. |
| Types d'activité | `app/services/activity_types.py` | Liste fermée (course à pied, trail, randonnée…). Le type lu dans le fichier y est ramené ; il se modifie depuis la carte. Les meilleurs temps sur distance ne sont calculés que pour la course à pied. |
| Profil | `/profile` | Mesures facultatives (date de naissance, taille, poids, FC) et type donné par défaut aux fichiers importés sans type. Nom et email restent ceux du fournisseur d'identité. |
| Version | pied de page, `/api/health` | Tag git du commit, sinon empreinte courte : injectée au build de l'image (`APP_VERSION`), lue dans git hors image (`app/version.py`). |

## Organisation du code

```
web-app/app/
├── __init__.py            factory : câblage uniquement
├── config.py              toute la configuration (variables d'environnement)
├── db.py                  schéma SQLite et connexions
├── security.py            en-têtes HTTP, contrôle d'origine (CSRF), reverse proxy
├── cli.py                 commandes d'exploitation
├── version.py             version affichée (build de l'image, sinon git)
├── settings/              registre des réglages administrables (bornes) et valeur effective base > env > défaut
├── secrets_store.py       secrets chiffrés (Fernet, multi-clés), rotation
├── outbound.py            liste blanche des destinations sortantes, CSP des tuiles
├── mailer.py              e-mails (SMTP), file d'envoi
├── background.py          ordonnanceur de fond (dossier surveillé, altitude, purge d'audit)
├── ratelimit.py · audit.py   limiteur de débit en mémoire · journal d'audit
├── auth/
│   ├── checks.py          contrôles de configuration au démarrage (mode, clé, hôtes, PUBLIC_URL)
│   ├── modes.py           mode d'authentification validé : seule source pour le garde et les gabarits
│   ├── oidc.py            client OIDC, login / callback / logout
│   ├── guard.py           contrôle d'accès global (fermé par défaut), session liée au compte
│   ├── users.py           persistance des comptes, jeton de session
│   ├── tokens.py · token_routes.py · invitations.py   jetons à usage unique, invitations
│   ├── passwords.py       politique et hachage argon2id (liste : auth/wordlists/)
│   └── factors.py         seconds facteurs (lecture et remise à zéro ; TOTP, WebAuthn non livrés)
├── api/                   une famille de routes par fichier, aucune logique métier
│   ├── activities_routes.py · dashboard_routes.py · segments_routes.py · users_routes.py
│   ├── profile_routes.py  profil de l'utilisateur courant (aucun identifiant dans l'URL)
│   ├── payload.py         lecture validée des entrées (corps JSON borné, entiers, noms)
│   ├── uploads.py         fichiers envoyés : parsing, budget de points, messages d'erreur
│   └── workload.py        places de traitement lourd (503 quand tout est pris)
└── services/              logique métier, sans dépendance à Flask
    ├── geo.py             distances, projection, D+ (numpy)
    ├── activity_stats.py  trace -> résumé (fonctions pures)
    ├── activity_types.py  catalogue des types d'activité (clés stockées, libellés)
    ├── profile_service.py profil : validation en liste blanche et persistance
    ├── activity_store.py  SQL des activités, filtré par utilisateur
    ├── ingest_service.py  pipeline d'ingestion
    ├── inbox_watcher.py   dossier surveillé
    ├── segment_service.py recherche des tronçons
    ├── dem_service.py     client MNT
    └── dashboard_service.py agrégats et records
```

Règle de dépendance : `api` → `services` → `db`. Les services reçoivent une connexion et un
mapping de configuration, ce qui les rend testables et réutilisables hors requête HTTP
(le watcher et la CLI s'en servent tels quels).

## Schéma de base et interface

- **Migrations** : `app/db.py`. Les 5 premières (`MIGRATIONS`, `PRAGMA user_version`) sont figées ;
  toute évolution du schéma passe par une migration **nommée** (`NAMED_MIGRATIONS`, par exemple
  `0014_emails_canonical`), enregistrée dans la table `schema_migrations` dans la même transaction.
  Au démarrage, chaque migration absente de la table est appliquée, quel que soit l'ordre de fusion des
  branches. `SCHEMA` décrit l'état final des 5 premières. Pour faire évoluer le schéma : ajouter une
  migration nommée en fin de liste, idempotente (elle vérifie le schéma avant d'agir), sans jamais
  retoucher une migration publiée (publiée = fusionnée sur `main`) ; une migration modifiée après avoir
  été poussée change de nom (`_v2`). Exploitation (images de branche, nom inconnu) :
  [exploitation.md](exploitation.md#migrations-nommées-et-images-de-branche).
- **Thème** : toutes les couleurs sont des jetons définis dans `static/css/theme.css`, en clair
  et en sombre ; les autres feuilles ne référencent que des rôles (`--surface`, `--accent`…).
  Les polices (Barlow, Barlow Condensed, JetBrains Mono) sont embarquées dans `static/vendor/fonts`.
  Le profil le long d'une trace (carte, tronçons) est un composant commun : `static/js/profile-strip.js`.
  Le mode suit le système, avec une bascule mémorisée dans le navigateur (`static/js/theme.js`).

## Analyse avancée conservée

L'analyse avancée d'une activité de la bibliothèque est calculée une fois, à la demande, puis
enregistrée (table `activity_analyses`) et réaffichée à chaque ouverture de l'activité.

- Le serveur calcule à partir du fichier d'origine stocké : le navigateur n'envoie pas la trace.
- « Relancer l'analyse » recalcule et remplace ; « Effacer » supprime le résultat enregistré.
- `ANALYSIS_VERSION` (dans `advanced_analysis.py`) est à incrémenter dès qu'un changement
  modifie le résultat : les analyses d'une autre version sont ignorées et recalculées à la demande.
- Une trace modifiée dans l'éditeur, ou un fichier hors bibliothèque, garde son résultat en
  mémoire le temps de la session, sans rien enregistrer.

| Route | Rôle |
|---|---|
| `GET /api/activities/<id>/analysis` | résultat enregistré, `analysis: null` s'il n'y en a pas |
| `POST /api/activities/<id>/analysis` | calcule (ou recalcule) et enregistre |
| `DELETE /api/activities/<id>/analysis` | supprime le résultat enregistré |

## Dossier surveillé

```
inbox/
├── jmichaux/            -> bibliothèque du compte dont le username (ou l'email) est « jmichaux »
│   └── 2026/sortie.gpx     (sous-dossiers libres)
└── sortie.gpx           -> compte INBOX_DEFAULT_USER (ignoré si non défini)
```

- Scrutation toutes les `INBOX_SCAN_INTERVAL` secondes (polling : fiable sur bind mount et NFS).
- Un fichier modifié depuis moins de 5 s est laissé pour le passage suivant (copie en cours).
- Dédoublonnage par SHA-256 du contenu, par utilisateur.
- Un dossier sans compte actif correspondant reste en attente et est repris dès que le compte existe.
- Liens symboliques et fichiers cachés ignorés ; le volume peut être monté en lecture seule.
- Diagnostic : `GET /api/inbox` (admin) ou `flask --app kml_viewer inbox-scan`, qui refuse de tourner
  tant que l'application servie détient le verrou du dossier (`SCHEDULER_ENABLED=0` ou application arrêtée).
- L'intervalle, l'activation (`INBOX_ENABLED`) et le propriétaire par défaut sont des réglages administrables :
  relus à chaud (voir le [README](../README.md#réglages-administrables--base--env--défaut)).

## Calculs

- **D+ / D-** : lissage sur 5 points puis hystérésis (4 m en GPS, 2 m sur MNT). Sommer les
  variations brutes point à point additionne le bruit du capteur.
- **Temps en mouvement** : intervalles où la vitesse dépasse 0,5 m/s.
- **Records** : meilleur temps sur 1, 5, 10, 21,1 et 42,2 km par fenêtre glissante.
- **Tronçons** : tolérance latérale de 30 m, sens de parcours respecté, plusieurs passages par
  trace possibles ; abandon après 150 m parcourus hors du tracé. Constantes en tête de
  `segment_service.py`.

## Authentification OIDC (FusionAuth)

Flux authorization code + PKCE, client confidentiel. Dans FusionAuth, sur l'application :

| Réglage | Valeur |
|---|---|
| Authorized redirect URLs | `https://<hôte>/auth/callback` et `https://<hôte>/auth/logged-out` |
| Enabled grants | Authorization Code |
| Client authentication | Required |
| Rôle applicatif (optionnel) | `admin`, à reporter dans `OIDC_ADMIN_ROLE` |
| JWT → Enabled, clés « Access token » et « Id token » | une clé **asymétrique** (RS256 ou ES256) |

Deux pièges constatés sur FusionAuth 1.69 :

- Par défaut l'ID token est signé en HS256 : le JWKS est alors vide et la vérification de
  signature échoue (« L'authentification a échoué », avec le motif dans les logs). Il faut
  générer une clé RS256/ES256 (Settings → Key Master) et l'associer à l'application.
- `OIDC_ISSUER` est l'URL de base de FusionAuth (celle qui sert
  `/.well-known/openid-configuration`), pas le champ « Issuer » du tenant.

Les rôles applicatifs ne figurent pas dans l'ID token : ils sont lus via `/userinfo`, puis à
défaut dans l'access token après vérification de sa signature, de son audience et de son sujet.

Puis renseigner `.env` (modèle : `.env.example`) : `AUTH_MODE=oidc`, `SECRET_KEY`, `OIDC_ISSUER`,
`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `TRUSTED_HOSTS`, `TRUST_PROXY=1`. Les comptes à mot de passe
(invitations) s'y ajoutent avec `SECRETS_KEY` et `PUBLIC_URL`, et le mode `password` (sans SSO) existe : voir
[README, Configuration](../README.md#-configuration) (modes, état de livraison).

L'application refuse de démarrer, avec le motif dans les logs, plutôt que de tourner sur une
configuration dangereuse :

| Configuration | Refus |
|---|---|
| `AUTH_MODE=oidc` sans `OIDC_ISSUER` | l'authentification aurait disparu |
| Pas d'`OIDC_ISSUER`, pas d'`AUTH_MODE`, mais `TRUSTED_HOSTS`, `TRUST_PROXY`, `SESSION_COOKIE_SECURE`, `OIDC_CLIENT_ID` ou `OIDC_CLIENT_SECRET` renseigné | ressemble à une production sans authentification |
| `AUTH_MODE=local` avec `OIDC_ISSUER` | contradictoire |
| OIDC sans `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | incomplet |
| OIDC avec la `SECRET_KEY` par défaut ou de moins de 32 caractères | cookies falsifiables |
| OIDC sans `TRUSTED_HOSTS` | l'en-tête `Host` du client servirait à construire l'URL de retour |
| `AUTH_MODE=password` avec `OIDC_ISSUER`, ou sans `SECRET_KEY` valide, `TRUSTED_HOSTS`, `SECRETS_KEY`, `PUBLIC_URL`, ou avec une `PUBLIC_URL` hors de `TRUSTED_HOSTS` | le mode `password` n'a pas de SSO et ses liens, ses secrets et ses cookies exigent ces garanties |
| `SECRETS_KEY` mal formée, ou valeur chiffrée en base qu'aucune clé ne déchiffre | secrets perdus : voir [exploitation.md](exploitation.md#perte-de-la-secrets_key) |
| `OUTBOUND_ALLOWED_HOSTS` invalide (port manquant, IPv6 sans crochets, service inconnu) | liste blanche ambiguë |
| Liste de mots de passe courants absente ou vide, quand les comptes à mot de passe sont disponibles | politique silencieusement affaiblie |

Écrire `AUTH_MODE=oidc` en production : c'est ce qui transforme une variable OIDC perdue en
arrêt franc. Sans `AUTH_MODE`, une instance lancée sans aucune variable démarre en mode local,
limitée au nom `localhost`.

Points de sécurité à connaître :

- **Session** : 12 h à compter de la connexion (`SESSION_LIFETIME_HOURS`), sans prolongation
  par l'activité ; un rôle retiré côté IdP est donc relu au plus tard à cette échéance.
  La session porte un jeton propre au compte, vérifié à chaque requête : se déconnecter ou
  être désactivé change ce jeton et ferme **toutes** les sessions du compte, sur tous les
  appareils (un cookie copié avant ne vaut plus rien). Le cookie est marqué `Secure` d'office
  avec l'OIDC ; `SESSION_COOKIE_SECURE=0` est une dérogation pour le développement en HTTP.
- **Nom d'hôte** : l'appli ne répond qu'aux noms de `TRUSTED_HOSTS` (400 sinon). La liste est
  obligatoire avec l'OIDC ; en mode local, vide signifie `localhost` / `127.0.0.1`
  (protection contre le DNS rebinding).
- **Reverse proxy** : `X-Forwarded-Host` n'est cru qu'avec `TRUST_PROXY_HOST=1`. Derrière
  HAProxy, qui transmet `Host` tel quel, le laisser à 0.
- **Volumes** : 200 000 points par fichier, 50 fichiers par envoi, `USER_QUOTA_MB` (2 Go par
  défaut) de fichiers d'origine par utilisateur. Un envoi groupé vers le visualiseur renvoie
  au plus l'équivalent de deux fichiers pleins : au-delà, les fichiers restants sont refusés
  (« charge ce fichier séparément »). Les corps JSON sont limités à 2 Mo, sauf l'analyse et
  l'export d'une trace entière (16 Mo). La simplification accepte 50 000 points par trace.
- **Traitements lourds** : deux à la fois (parsing, analyse d'une trace entière, export,
  simplification), tous utilisateurs confondus ; une requête qui n'obtient pas de place en
  20 s reçoit un 503 et peut être relancée. Pas de limitation de débit sur les routes du visualiseur
  ni de la bibliothèque : la poser sur le reverse proxy si l'instance est ouverte à des comptes peu sûrs.
  Un limiteur en mémoire (`ratelimit.py`) protège seulement les routes publiques de jeton
  (`/auth/token*`, 10 essais par 15 min et par IP par défaut) et, quand elles seront livrées, celles de la
  connexion par mot de passe.

Gestion des comptes :

- Création à la première connexion à partir des claims `sub`, `email`, `preferred_username`, `name`.
- Sans `OIDC_ADMIN_ROLE` : aucun compte ne devient admin en se connectant (« le premier arrivé
  est admin » offrait l'instance à quiconque passait avant son propriétaire). Le propriétaire
  se connecte une fois, ce qui crée son compte, puis se désigne :
  `docker compose exec kml-viewer flask user-admin <login>`. La commande active aussi le
  compte ; il gère ensuite les autres dans `/admin/users`.
- Avec `OIDC_ADMIN_ROLE` : le droit admin suit le claim `roles` à chaque connexion, et un
  compte qui porte le rôle est actif d'office.
- Un nouveau compte attend l'activation par un admin (`OIDC_AUTO_ACTIVATE=0`, le défaut) ;
  `OIDC_AUTO_ACTIVATE=1` rend actif tout compte de l'IdP dès sa première connexion.
- `user-admin <login> --revoke` retire le rôle admin sans toucher à l'activation : un compte
  désactivé le reste.
- Un login contenant « @ » désigne un email, sinon un username (jamais l'un pour l'autre) ; s'il
  correspond à plusieurs comptes, il n'en désigne aucun : la commande et le dossier surveillé
  refusent d'agir. Un email n'est retenu que si l'IdP le déclare vérifié (claim
  `email_verified` à `true`) ; sinon le compte est désigné par son username.
- Supprimer un compte ne le bannit pas : avec l'activation automatique, la personne peut se
  reconnecter (sous un nouvel identifiant interne : celui d'un compte supprimé n'est jamais
  réattribué). Pour bloquer un accès, désactiver le compte.

En mode local (`AUTH_MODE=local`, ou aucune variable), l'application tourne en mono-utilisateur
**sans authentification** : chaque visiteur est administrateur.

## Sécurité

- Accès fermé par défaut : toute route exige une session, hors liste blanche explicite
  (`auth/guard.py`). Un test parcourt toutes les routes pour le garantir.
- Cloisonnement : chaque requête SQL métier filtre sur l'utilisateur courant ; l'accès à la
  ressource d'un autre répond 404.
- Session : cookie signé `HttpOnly`, `SameSite=Lax`, `Secure` avec l'OIDC, 12 h ; régénérée au
  login et liée à un jeton du compte, renouvelé à la déconnexion et à la désactivation.
  Démarrage refusé si l'OIDC est actif avec la `SECRET_KEY` par défaut.
- CSRF : les requêtes modifiantes dont l'origine n'est pas celle de l'appli sont refusées.
- XML : parsing via `defusedxml` (XXE, bombes d'entités).
- Fichiers : noms de stockage dérivés du SHA-256, jamais du nom fourni ; droits `0600`.
- En-têtes : CSP (dont `form-action` : nous-mêmes, plus l'origine de l'IdP pour la
  déconnexion), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.
- Erreurs : une entrée mal formée reçoit un 400/404 au message fixe ; le détail d'une
  exception (parser, SQL) ne part que dans les logs.
- Scripts : la CSP n'autorise que ceux servis par l'application (`script-src 'self'`). Les
  bibliothèques front sont embarquées dans `static/vendor` (aucun CDN) et aucun template ne
  contient de script ou de gestionnaire inline ; un test le vérifie. Les données issues des
  fichiers (noms, descriptions) sont toujours insérées comme texte, jamais comme HTML.
- KMZ : décompression bornée (64 Mo), seul le document KML est lu.
- Erreurs : une erreur inattendue de l'API est journalisée ; le client ne reçoit qu'un message générique.
- Conteneur : gunicorn, utilisateur non-root, racine en lecture seule, capacités supprimées,
  port publié sur `127.0.0.1` (à placer derrière un reverse proxy TLS).

Limite connue : `style-src` garde `'unsafe-inline'`, les templates utilisant des attributs `style`.

## Correction d'altitude

Le profil compose `dem` fournit tout le nécessaire, en local (les traces ne sortent pas de la machine) :

```bash
# .env
DEM_URL=http://opentopodata:5000/v1/copernicus30
DEM_BBOX=41,-6,52,10        # lat_min,lon_min,lat_max,lon_max : France métropolitaine, ~9 Go

docker compose --profile dem up -d
```

- `dem-tiles` : tâche ponctuelle qui télécharge les dalles Copernicus GLO-30 de l'emprise
  (bucket public AWS, ~45 Mo par degré carré) dans le volume `dem-data`. Relançable sans
  risque : les dalles présentes ne sont pas retéléchargées, et aucun accès Internet n'est
  requis une fois qu'elles sont là. Pour étendre la zone, élargir `DEM_BBOX` et relancer.
- `opentopodata` : API d'altitude, construite depuis le dépôt du projet (pas d'image publiée),
  épinglée sur un commit. Aucun port publié : seul `kml-viewer` peut l'appeler.

Un point tous les 20 m est interrogé, le reste est interpolé. Un import depuis l'interface
n'attend pas le MNT : l'activité est enregistrée avec l'altitude GPS, puis corrigée par la
tâche de fond, réveillée aussitôt (`DEM_BACKFILL_BATCH` activités par cycle, 25 par défaut).
Il en va de même si le service est indisponible au moment de l'ingestion (démarrage, panne). Une trace hors des dalles disponibles garde
son altitude GPS ; le bouton montagne du tableau de bord relance la correction à la demande.

## Sauvegarde

Trois choses sont à conserver : la base SQLite, les fichiers d'origine (`/data/files`) et, **à part**, la
`SECRETS_KEY` : la base contient les secrets chiffrés mais jamais la clé, et sans elle une base restaurée
ne peut plus les relire ([procédure](exploitation.md#perte-de-la-secrets_key)).

```bash
# Copie cohérente de la base, application en marche ; --keep limite le nombre de copies gardées
docker compose exec kml-viewer flask backup --keep 7      # -> /data/backups/kmlviewer-<date>.sqlite3

# Archive du volume complet (base sauvegardée + fichiers d'origine)
docker run --rm -v kmlviewer_kml-viewer-data:/data:ro -v "$PWD":/out alpine \
    tar czf /out/kmlviewer-data.tgz -C /data backups files
```

Le nom du volume dépend du dossier du projet (`docker volume ls`). Ne pas copier
`kmlviewer.sqlite3` à chaud avec `cp` : en mode WAL, la copie peut être incohérente.
Restauration : arrêter l'application, remettre la copie à la place de
`/data/kmlviewer.sqlite3` (supprimer les fichiers `-wal` et `-shm`), restaurer `files/`, relancer.

## Dépendances et image

- `requirements.in` liste les dépendances directes ; `requirements.txt` est le verrou complet,
  transitives et empreintes comprises, installé avec `--require-hashes`. Régénération :
  `uv pip compile requirements.in --generate-hashes --python-version 3.15 --upgrade -o requirements.txt`
  (idem pour `requirements-dev.in`).
- L'image applique les correctifs de sécurité Debian au build et ne contient ni `pip` ni les tests.
- Dependabot propose chaque semaine les montées de version (Python, image de base, actions).

## Intégration continue

`.github/workflows/docker.yml` enchaîne lint (ruff), tests avec seuil de couverture, audit des dépendances, build, essai de l'image
en conditions de production (racine en lecture seule, sans capacités), scan Trivy, puis
publication multi-architecture sur GHCR et Docker Hub. Rien n'est publié si une étape échoue.

| Déclencheur | Tags publiés |
|---|---|
| Tag git `vX.Y.Z` | `X.Y.Z`, `X.Y`, `X`, `latest` |
| Branche `main` | `unstable` |
| Autre branche | `unstable-<branche>` |
| Chaque lundi | la dernière release est reconstruite sans cache, pour embarquer les correctifs de l'image de base |

`/api/health` renvoie la version embarquée dans l'image. C'est la seule route servie quel que soit
l'en-tête `Host` (hors `TRUSTED_HOSTS` compris) : le `HEALTHCHECK` de l'image et la sonde d'un
répartiteur de charge l'interrogent sans le nom public de l'appli.

## Tests

```bash
cd web-app
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m ruff check .
python -m pytest --cov=app
```
