# Recette manuelle — comptes à mot de passe, administration, traces planifiées (bêta)

Statut : **v2, 2026-11-24** (Doc-15 : remise à jour sur le code livré) · Rôle : spec · Specs : [admin-space](admin-space.md), [gpx-route-builder](gpx-route-builder.md)
Demandée par l'utilisateur (via coord) pour une **version de test**. Parcours pas à pas pour un testeur, avec le résultat attendu et le critère d'acceptation (CA) correspondant.

Sources de cette version : code de l'arbre de travail (commits jusqu'à `815aa19` : T11, T13, T14, T15 minimal, TR7) et README rédigé par doc, dont le parcours d'amorçage a été **rejoué de bout en bout** (REVIEW.md, « Doc T17 — reprise »). Les étapes non rejouées par doc ou test sont à jouer pour la première fois par le testeur.

---

## 1. Ce qui est jouable aujourd'hui

| Étape | Jouable ? | Condition |
|---|---|---|
| 0. Service de routage (TR7) | **Oui** | profil compose `routing`, accès à `download.geofabrik.de` |
| 1. Premier admin par la CLI | **Oui** (rejoué par doc) | — |
| 2. Connexion avec TOTP | **Oui** (rejoué par doc) | serveur à l'heure (NTP) |
| 3. E-mail et validation de l'adresse de l'admin | **Oui** | **SMTP de test** |
| 4. Invitation d'un utilisateur par l'admin | **Oui** | SMTP pour la validation d'adresse (sinon lien affiché seulement) |
| 5. Invitation par un utilisateur avec crédits | **Oui** | **SMTP** : il faut une adresse validée pour inviter (EF-3.2) |
| 6. Réglages et secrets | **Oui** | — ; « Tester » le moteur répond « ko » tant que TR5 n'est pas livré |
| 7. Activation de la bêta | **En partie** | état et liste enregistrés ; **aucun effet visible** sans TR5/TR6 |
| 8. Trace planifiée | **Non** | **TR5** (routes, `results.py`, sondage `routing_health`, réglage `routing.coverage_bbox`) et **TR6** (écran « Créer une trace ») |

Défauts connus à constater pendant la recette (déjà relayés) : **Doc-12** (étape 6.9, `auth.mfa_policy = all` accepté au lieu d'être refusé) ; **Doc-3** (`user-invite --reset-password` absent, §5).

## 2. Environnement de test

**Questions ouvertes (décision de l'utilisateur, avec ops)** :
- **où tourne l'instance de test** (poste local, « dune ») ;
- **HTTPS** : le cookie de session est `Secure` par défaut (EF-0.2) ; sans TLS, la connexion échoue. Soit un reverse proxy TLS devant `127.0.0.1:8080`, soit `SESSION_COOKIE_SECURE=0` **sur l'instance de test seulement**, jamais en production ;
- **SMTP de test** : un capteur de mails (ex. Mailpit) joignable par la stack, ou un relais réel. Sans SMTP, les étapes 3 et 5 sont impossibles et les adresses restent « en attente ».

**Variables d'environnement** (`.env` généré par Ansible ou écrit à la main ; modèle : `.env.example`) :

```
AUTH_MODE=password                      # sans OIDC_ISSUER (incompatibles)
SECRET_KEY=<openssl rand -hex 32>
SECRETS_KEY=<clé Fernet>                # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
PUBLIC_URL=https://<hôte de test>
TRUSTED_HOSTS=<hôte de test>            # doit couvrir PUBLIC_URL
OUTBOUND_ALLOWED_HOSTS=routing=graphhopper:8989,smtp=<hôte SMTP>:<port>
ROUTING_BASE_URL=http://graphhopper:8989   # facultatif : sinon se règle dans l'admin (étape 6)
```

Il n'existe **aucune variable d'env SMTP** : le SMTP se règle dans l'admin (étape 3).

**Matériel du testeur** : une application TOTP (Aegis, Google Authenticator, FreeOTP…) ; deux navigateurs ou fenêtres privées (admin, utilisateurs) ; trois adresses e-mail de test (admin, Alice, Bob) qui arrivent dans le SMTP de test ; un serveur à l'heure (dérive > 30 s ⇒ TOTP refusé).

**Démarrage** : `docker compose up -d`, puis `docker compose logs kml-viewer`. **Attendu** : aucune erreur ; le `WARNING` « SMTP non configuré : les adresses e-mail ne peuvent pas être validées » est normal à ce stade. `GET /api/health` répond via le proxy.

Contrôle négatif facultatif : retirer `SECRETS_KEY` ⇒ le conteneur refuse de démarrer avec un message explicite (CA-20). Remettre la variable.

---

## 3. Parcours

Notation : **Faire** = action du testeur ; **Attendu** = résultat à constater. Toute différence est un défaut à remonter avec l'heure, l'étape et une capture.

### Étape 0 — Service de routage (TR7, ADR-0007) — facultative tant que TR5/TR6 ne sont pas livrés

1. **Faire** : dans le dossier du projet, `routing/update.sh`.
   **Attendu** : téléchargement des extraits Nord-Pas-de-Calais, Picardie et Belgique (MD5 vérifiés), fusion, import dans un dossier horodaté, bascule du lien `current`, moteur recréé et sain ; quelques minutes au total (154 s d'import sur dune).
2. **Faire** : `docker compose --profile routing ps` puis `docker stats --no-stream` après quelques appels.
   **Attendu** : `graphhopper` sain, **aucun port publié** ; RAM du conteneur **≤ ~1,8 Go** avec `-Xmx1536m` dans 2 Go, aucune mise à mort par manque de mémoire (point à valider de l'ADR-0007).
3. **Faire** : relancer `routing/update.sh`.
   **Attendu** : relance sans risque ; l'ancien graphe reste disponible (`ROUTING_KEEP_GRAPHS`, défaut 2). Retour arrière **manuel** : pas de commande dédiée, repointer `current` à la main (Doc-13).

### Étape 1 — Premier admin par la CLI (EF-0.3, CA-33)

1. **Faire** sur le serveur, dans le dossier du projet :
   `docker compose exec kml-viewer flask user-invite --admin admin@test.example`
   **Attendu** : « Jeton d'amorçage créé (valable jusqu'au …, usage unique). Lien, montré une seule fois : » suivi de `https://<hôte>/auth/token#t=…` ; sans SMTP, « E-mail non configuré : transmets ce lien à la seule personne concernée. » Une ligne d'audit avec l'acteur « Ligne de commande ».
2. **Faire** : ouvrir le lien dans le navigateur « admin ».
   **Attendu** : le `#t=…` disparaît de la barre d'adresse ; formulaire identifiant + mot de passe.
3. **Faire** : identifiant `admin`.
   **Attendu** : « Identifiant indisponible » (réservé, EF-1.2b), jeton non consommé. Saisir `recette-admin`.
4. **Faire** : mot de passe de 11 caractères, puis `azertyuiop123`.
   **Attendu** : refus pour la longueur (moins de 12 caractères), puis « Ce mot de passe est trop courant. » (CA-8, CA-8b). Choisir un mot de passe long et propre.
5. **Faire** : valider.
   **Attendu** : page d'enrôlement (QR code et secret en texte), **aucune session** (un autre onglet sur `/` mène à la connexion). Fenêtre d'enrôlement : **15 min**.
6. **Faire** : scanner le QR code, saisir le code à 6 chiffres.
   **Attendu** : le compte devient admin à cet instant ; **10 codes de secours** affichés une seule fois (`XXXX-XXXX-XXXX-XXXX`), à noter ; puis renvoi vers la connexion.
7. **Faire** : rouvrir le lien d'amorçage.
   **Attendu** : message neutre, sans détail (CA-5).

Enrôlement abandonné (fenêtre expirée, navigateur fermé) : le compte existe, non admin, sans facteur ; reprise par `flask user-invite --admin --account recette-admin`, dont le lien redemande le **mot de passe actuel** (EF-0.3b). Mot de passe perdu à ce stade : voir §5 (Doc-3).

### Étape 2 — Connexion avec second facteur TOTP (EF-1.6, EF-2.3, CA-7, CA-10, CA-12)

1. **Faire** : connexion avec `recette-admin` et un mauvais mot de passe, puis avec un identifiant inexistant.
   **Attendu** : même message générique (CA-7).
2. **Faire** : bon mot de passe.
   **Attendu** : page « code du second facteur ».
3. **Faire** : saisir le code déjà utilisé à l'enrôlement, s'il est encore affiché.
   **Attendu** : « Code invalide ou déjà utilisé » ; attendre le code suivant.
4. **Faire** : code courant.
   **Attendu** : session ouverte ; lien **Administration** dans la barre de navigation.
5. **Faire** : déconnexion, reconnexion avec **un code de secours** au lieu du TOTP ; à la connexion suivante, réutiliser ce même code.
   **Attendu** : la première fois, connexion ; la seconde, refus.
6. **Faire** (facultatif) : 5 codes faux de suite.
   **Attendu** : il faut ressaisir le mot de passe (EF-2.6).

### Étape 3 — E-mail et validation de l'adresse de l'admin (EF-4.1, EF-4.2, EF-4.3b, CA-17b)

1. **Faire** : Administration › **Clés API et e-mail** : hôte et port du SMTP de test (présents dans `OUTBOUND_ALLOWED_HOSTS`), sécurité adaptée au serveur (`none` pour un capteur sans TLS, **sans** identifiant ni mot de passe), expéditeur. Puis « Envoyer un e-mail de test ».
   **Attendu** : réponse « ok » ou « ko » seulement ; le mail arrive à l'adresse **en attente** de l'admin (P-T14-9).
2. **Faire** : hôte SMTP **absent** de `OUTBOUND_ALLOWED_HOSTS`.
   **Attendu** : 400, rien n'est écrit (CA-36). Remettre le bon hôte.
3. **Faire** : Administration › Utilisateurs › ligne `recette-admin` › « Renvoyer la validation » ; ouvrir le mail.
   **Attendu** : le mail nomme l'identifiant `recette-admin` et dit « si tu n'as pas créé ce compte, ignore ce message ».
4. **Faire** : ouvrir le lien **dans une fenêtre privée** (sans session).
   **Attendu** : page neutre « connecte-toi au compte concerné, puis rouvre ce lien » ; adresse toujours en attente.
5. **Faire** : rouvrir le lien dans le navigateur « admin » connecté.
   **Attendu** : adresse validée (onglet Utilisateurs).
6. **Faire** : redémarrer (`docker compose up -d --force-recreate kml-viewer`) et relire les logs.
   **Attendu** : plus de `WARNING` « SMTP non configuré ».

### Étape 4 — Invitation d'un utilisateur par l'admin (EF-3, CA-3, CA-17, CA-45)

1. **Faire** : Administration › Invitations : `alice@test.example`, crédits initiaux **0**, note « Bienvenue pour la recette ».
   **Attendu** : lien affiché **une fois** avec « Copier » ; mail reçu contenant « Message de recette-admin : Bienvenue pour la recette » ; crédits de l'admin inchangés (CA-3).
2. **Faire** : réinviter `alice@test.example`.
   **Attendu** : 409 avec un message explicite (adresse déjà invitée ; émetteur admin, EF-3.10).
3. **Faire** : ouvrir le lien dans le navigateur « utilisateurs » ; créer `alice` avec un mot de passe conforme.
   **Attendu** : renvoi vers la connexion, aucune session (CA-45) ; connexion d'`alice` **sans** second facteur (politique `admins`) ; adresse « en attente » ; mail de validation reçu.
4. **Faire** : `alice`, **connectée**, ouvre le lien de validation.
   **Attendu** : adresse validée.
5. **Faire** (facultatif) : `alice` › Profil › Second facteur › « Activer un code TOTP ».
   **Attendu** : le mot de passe actuel est exigé avant le QR code ; après confirmation, 10 codes de secours ; retirer le TOTP exige mot de passe **et** code (P-T11-7).

### Étape 5 — Invitation par un utilisateur avec crédits (EF-3.2, EF-3.5, EF-3.7, CA-2, CA-6)

Prérequis : adresse d'`alice` validée (4.4).

1. **Faire** (Alice) : Profil › Invitations.
   **Attendu** : aucune invitation possible, 0 crédit.
2. **Faire** (admin) : Administration › Utilisateurs › `alice` › crédits = **1**.
   **Attendu** : enregistré, ligne d'audit.
3. **Faire** (Alice, après rechargement) : inviter `bob@test.example`.
   **Attendu** : avertissement « crédit non rendu en cas de révocation ou d'expiration » **avant** la création ; lien affiché une fois ; 0 crédit ; pas de champ « note » (réservé aux admins).
4. **Faire** (Alice) : seconde invitation.
   **Attendu** : refusée (409), toujours 0 crédit.
5. **Faire** (Alice) : révoquer l'invitation de Bob.
   **Attendu** : révoquée, crédit **non rendu** (CA-6) ; le lien de Bob donne un message neutre.
6. **Faire** (admin) : recréditer 1 ; Alice réinvite Bob ; Bob crée `bob` et valide son adresse.
   **Attendu** : `bob` créé avec 0 crédit (invitation d'un non-admin, EF-3.9).

### Étape 6 — Réglages et secrets (EF-2.2, EF-5, EF-6, CA-13, CA-19, CA-21, CA-34)

1. **Faire** : Administration › Réglages › validité des invitations = 72 h.
   **Attendu** : source « base » ; `docker compose exec kml-viewer flask settings-list` affiche la même valeur et la même source. « Revenir au défaut » ⇒ source « défaut ».
2. **Faire** : une valeur hors bornes (validité 0 h ou 1 000 h).
   **Attendu** : 400 avec le champ, lot non écrit (CA-21c).
3. **Faire** (si le SMTP de test a une authentification, sécurité `starttls` ou `tls`) : saisir le mot de passe SMTP, enregistrer, recharger.
   **Attendu** : « configuré », date, auteur et destination liée ; mot de passe **jamais** réaffiché, pas même ses 4 derniers caractères (CA-19).
4. **Faire** : changer l'hôte SMTP pour un autre hôte autorisé.
   **Attendu** : mot de passe SMTP effacé (« non configuré »), ligne d'audit (CA-34).
5. **Faire** : `routing.base_url` hors liste blanche (ex. `http://exemple.org:8989`).
   **Attendu** : 400, rien n'est écrit.
6. **Faire** : « Tester » le moteur de routage.
   **Attendu aujourd'hui** : « ko » quel que soit l'état du moteur (aucune sonde avant TR5) ; **attendu après TR5** : « ok » avec le moteur de l'étape 0.
7. **Faire** : `docker compose exec kml-viewer flask settings-reset auth.invite_ttl_hours`, puis recharger l'onglet.
   **Attendu** : nouvelle valeur visible sans redémarrage (CA-21d).
8. **Faire** : Journal ; filtrer sur l'acteur « cli ».
   **Attendu** : une ligne par action, secrets masqués ; l'amorçage et le `settings-reset` apparaissent avec l'acteur « Ligne de commande ».
9. **Faire** : `bob` n'ayant pas de second facteur, passer `auth.mfa_policy` à `all`.
   **Attendu (spec, EF-2.2, CA-13b)** : **409** indiquant le nombre de comptes sans facteur, rien n'est écrit. **Constaté aujourd'hui (Doc-12)** : la valeur est acceptée et `bob` est conduit à l'enrôlement à sa prochaine connexion : défaut connu, à remonter s'il est encore présent. Remettre `admins`.

### Étape 7 — Activation de la bêta pour une liste d'utilisateurs (EF-7, CA-22)

1. **Faire** (admin) : Administration › Fonctions bêta › « Traces planifiées » : « Pour une liste de comptes », cocher `alice`, Enregistrer.
   **Attendu** : enregistré ; ligne d'audit.
2. **Faire** : passer à « Pour tous », puis revenir à « Pour une liste de comptes ».
   **Attendu** : la liste (`alice`) est conservée (EF-7.2).
3. **Attendu après TR5/TR6** (non jouable aujourd'hui) : `alice` voit « Créer une trace », `bob` ne la voit pas et `GET /api/route-builder/options` lui répond 404 ; diagnostic du moteur (état, dernier sondage, couverture) affiché sous la fonction.

### Étape 8 — Création d'une trace planifiée (RB-2 à RB-5, CA-R2, CA-R13, CA-R20) — **bloquée : TR5, TR6**

Prérequis : étape 0 faite ; `routing.base_url` réglée ; `alice` dans la liste de la bêta ; `alice` a importé **au moins une activité réalisée** (GPX horodaté).

1. **Faire** (Alice) : noter le tableau de bord (nombre d'activités, distance de la période, records).
2. **Faire** : visualiseur › « Créer une trace » ; cliquer à Londres ou Amsterdam.
   **Attendu** : point refusé, « hors de la zone couverte » ; contour du rectangle `routing.coverage_bbox` visible.
3. **Faire** : 1 point à Lille sans distance, « Terminer ».
   **Attendu** : « distance requise » (RB-3.2), aucune génération.
4. **Faire** : 3 points autour de Lille (Citadelle, Vieux-Lille, Wazemmes), **Course à pied**, **15 km**, « Terminer ».
   **Attendu** : progression puis boucle fermée sur le point 1, passant par les 3 points **dans l'ordre**, sur des voies réelles ; distance entre 12,75 et 17,25 km **ou** avertissement d'écart ; aucun point technique affiché comme point de l'utilisateur.
5. **Faire** : « Autre proposition ».
   **Attendu** : une trace différente, mêmes points.
6. **Faire** : 2 points à Durbuy (Ardennes belges), **Gravel**, 40 km.
   **Attendu** : trace générée ; hors tolérance ⇒ avertissement « ajoute un point de passage… » (RB-4.14).
7. **Faire** : « Enregistrer dans la bibliothèque », nom « Recette Lille 15 km » ; puis réenregistrer le même résultat.
   **Attendu** : trace enregistrée, lien vers la bibliothèque ; second essai ⇒ « déjà enregistrée ».
8. **Faire** : bibliothèque.
   **Attendu** : badge **Planifiée** ; filtres « Planifiées » (elle seule), « Réalisées » (absente), « Toutes » par défaut (présente) ; le mot « itinéraire » n'apparaît nulle part (CA-R19).
9. **Faire** : tableau de bord.
   **Attendu** : **identique** au relevé de 8.1 (CA-R13).
10. **Faire** : ouvrir la trace enregistrée.
    **Attendu** : profil d'altitude (MNT si `DEM_URL` est configuré, appliqué à l'enregistrement ; sinon altitude du moteur ou aucune, RB-4.8) ; export GPX possible.
11. **Faire** : se déconnecter avec des points en cours, se reconnecter en `bob` (bêta « Pour tous ») sur le même navigateur, ouvrir le mode.
    **Attendu** : aucun point d'Alice (RB-2.6, CA-R28).

---

## 4. Contrôles transverses (fin de parcours)

- Journaux du conteneur : aucun jeton (`#t=…`), mot de passe, code ni coordonnée de point (CA-5, CA-R17).
- En-têtes : `Cache-Control: no-store` sur `/admin` et `/auth/*` ; CSP sans `unsafe-inline`.
- Perte de facteur (facultatif) : `docker compose exec kml-viewer flask user-mfa-reset alice` ⇒ facteurs retirés, sessions fermées, notification ; sur le dernier admin, la commande annonce « dernier administrateur » **avant** d'agir.

## 5. Pas livré (ne pas tester)

| Élément | Tâche | Conséquence pour la recette |
|---|---|---|
| Constructeur de traces : routes, sondage du moteur, `routing.coverage_bbox` | **TR5** | étape 8 bloquée ; « Tester » le moteur répond « ko » ; pas de diagnostic du moteur dans « Fonctions bêta » |
| Écran « Créer une trace », badge et filtre de la bibliothèque | **TR6** | étape 8 bloquée |
| WebAuthn (clés de sécurité) | **T12** | seul le TOTP est proposé ; `WEBAUTHN_RP_ID` n'est pas lue |
| `flask user-invite --reset-password` | **Doc-3** | amorçage avec mot de passe perdu : si un autre admin existe, il émet un lien de réinitialisation, puis `user-invite --admin --account` ; sinon, amorcer un **autre** compte (autre identifiant) |
| Refus de `auth.mfa_policy = all` avec des comptes sans facteur | **Doc-12** (défaut relayé à dev) | étape 6.9 : constater le comportement actuel |
| Retour arrière manuel de `routing/update.sh` | Doc-13 | repointer `current` à la main |
| Appréciation visuelle des traces gravel | utilisateur | sur l'échantillon du spike (`spike/detours/out/echantillon/`), hors recette |
