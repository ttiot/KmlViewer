# Recette manuelle — comptes à mot de passe, administration, traces planifiées (bêta)

Statut : **v1, 2026-11-24** · Rôle : spec · Specs : [admin-space](admin-space.md), [gpx-route-builder](gpx-route-builder.md)
Demandée par l'utilisateur (via coord) pour une **version de test**. Parcours pas à pas pour un testeur, avec le résultat attendu et le critère d'acceptation (CA) correspondant.

> **État de livraison au 2026-11-24 (d'après `DEV-ETAT.md`)** : le parcours **ne peut pas encore être joué de bout en bout**. Chaque étape porte sa colonne « Livré ? ». Le minimum à livrer pour le jouer est en §1 ; ce qui reste hors de cette version est en §5.

---

## 1. Ce qu'il faut pour jouer la recette

| Étape | Tâches nécessaires | État au 2026-11-24 |
|---|---|---|
| 1. Premier admin (CLI) | T8 (fait), T7b (fait), **T11** (enrôlement TOTP, fin d'amorçage) | **Bloquée** : la consommation du jeton répond 501 à l'enrôlement |
| 2. Connexion avec TOTP | T10 (fait), **T11** | **Bloquée** |
| 3. Validation de l'adresse de l'admin | **T13**, SMTP de test | **Bloquée** (T13 non fait) |
| 4. Invitation d'un utilisateur par l'admin | T7b (fait, API), **T15** (UI) ou appel d'API | API seulement |
| 5. Invitation par un utilisateur avec crédits | **T14** (crédits), **T13** (adresse validée exigée, EF-3.2), **T15** | **Bloquée** |
| 6. Réglages et secrets | T3, T4, T18, T9 (faits), **T14**, **T15** | **Bloquée** (aucune API admin) |
| 7. Activation de la bêta | T5 (fait), **T14**, **T15** | **Bloquée** (SQL seulement, D4) |
| 8. Trace planifiée | TR1-TR4 (faits), **TR5**, **TR6**, moteur joignable (**TR7** ou moteur du spike) | **Bloquée** |

Chaîne minimale : **T11 → T13 → T14 → T15** côté comptes, **TR5 → TR6** + moteur côté traces. Les deux chaînes sont indépendantes jusqu'à l'étape 7 (activation de la bêta, T14).

**Version réduite possible** si tout n'est pas prêt : étapes 1, 2, 4 (lien affiché, sans validation d'adresse), 7 et 8. L'étape 5 exige T13 et un SMTP de test : sans eux, un utilisateur ne peut pas inviter (adresse non validée, EF-3.2, EF-4.5). Le choix du périmètre revient à l'utilisateur.

## 2. Environnement de test

**Questions ouvertes (décision de l'utilisateur, avec ops)** :
- **Où tourne l'instance de test** (poste local, « dune ») ;
- **HTTPS** : le cookie de session est `Secure` par défaut (EF-0.2) ; sans TLS, la connexion échoue. Il faut soit un reverse proxy TLS devant `127.0.0.1:8080`, soit `SESSION_COOKIE_SECURE=0` **sur l'instance de test seulement**, jamais en production ;
- **SMTP de test** : un capteur de mails (ex. Mailpit) dans un compose de test, ou un relais réel ; sans SMTP, les étapes 3 et 5 sont impossibles ;
- **Moteur** : service compose de TR7, ou le conteneur GraphHopper du spike (`israelhikingmap/graphhopper:11.0`, données Hauts-de-France + Belgique) raccordé au réseau de la stack.

**Variables d'environnement** (`.env` généré par Ansible ou écrit à la main ; modèle : `.env.example`) :

```
AUTH_MODE=password                      # sans OIDC_ISSUER (incompatibles)
SECRET_KEY=<openssl rand -hex 32>
SECRETS_KEY=<clé Fernet>                # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
PUBLIC_URL=https://<hôte de test>
TRUSTED_HOSTS=<hôte de test>            # doit couvrir PUBLIC_URL
OUTBOUND_ALLOWED_HOSTS=routing=<hôte du moteur>:8989,smtp=<hôte SMTP>:<port>
```

Ne **pas** poser `routing.base_url` ni les réglages SMTP dans le `.env` : ils se règlent dans l'admin à l'étape 6 (c'est ce qu'on teste).

**Matériel du testeur** : une application TOTP sur téléphone (Aegis, Google Authenticator, FreeOTP…) ; deux navigateurs ou fenêtres privées (admin, utilisateurs) ; trois adresses e-mail de test (admin, Alice, Bob) qui arrivent dans le SMTP de test ; un serveur à l'heure (NTP : dérive > 30 s ⇒ TOTP refusé).

**Démarrage** : `docker compose up -d`, puis `docker compose logs kml-viewer` (aucune erreur, avertissements attendus : réglages surchargés, SMTP absent si non configuré) et `GET /api/health` via le proxy.

Contrôle négatif facultatif : retirer `SECRETS_KEY` ⇒ le conteneur refuse de démarrer avec un message explicite (CA-20). Remettre la variable.

---

## 3. Parcours

Notation : **Faire** = action du testeur ; **Attendu** = résultat à constater. Toute différence est un défaut à remonter avec l'heure, l'étape et une capture.

### Étape 1 — Premier admin par la CLI (EF-0.3, CA-33)

1. **Faire** sur le serveur, dans le dossier du projet :
   `docker compose exec kml-viewer flask user-invite --admin admin@test.example`
   **Attendu** : la console affiche « Jeton d'amorçage créé (valable jusqu'au …) » et un lien `https://<hôte>/auth/token#t=…`, **une seule fois** ; avec SMTP, « Le lien est aussi parti par e-mail ». Une ligne d'audit `actor_type = cli` est créée.
2. **Faire** : ouvrir le lien dans le navigateur « admin ».
   **Attendu** : la barre d'adresse ne montre plus le `#t=…` (effacé par la page) ; formulaire « identifiant + mot de passe ».
3. **Faire** : saisir l'identifiant `admin`.
   **Attendu** : refus « identifiant indisponible » (identifiant réservé, EF-1.2b). Saisir ensuite `recette-admin`.
4. **Faire** : mot de passe `azertyuiop123` puis un mot de passe de 11 caractères.
   **Attendu** : refus dans les deux cas (liste des mots de passe courants, longueur < 12 ; CA-8, CA-8b). Choisir un mot de passe long et propre.
5. **Faire** : valider.
   **Attendu** : page d'enrôlement du second facteur, **pas** de session ouverte (aller sur `/` dans un autre onglet ⇒ page de connexion). QR code et secret en texte.
6. **Faire** : scanner le QR code, saisir le code à 6 chiffres.
   **Attendu** : 10 codes de secours affichés **une seule fois** (format `XXXX-XXXX-XXXX-XXXX`), bouton « Copier ». Les noter. Puis redirection vers la page de connexion.
7. **Faire** : rouvrir le lien d'amorçage.
   **Attendu** : message neutre (jeton déjà utilisé), sans détail (CA-5).

Reprise si l'enrôlement est abandonné : `flask user-invite --admin --account recette-admin` (le lien redemande le mot de passe actuel, EF-0.3b).

### Étape 2 — Connexion avec second facteur TOTP (EF-1.6, EF-2.3, CA-9, CA-10, CA-12)

1. **Faire** : se connecter avec `recette-admin` et un **mauvais** mot de passe, puis un identifiant inexistant.
   **Attendu** : même message générique dans les deux cas (CA-7).
2. **Faire** : bon mot de passe.
   **Attendu** : page « code du second facteur ».
3. **Faire** : saisir le code affiché **à l'instant où l'enrôlement a été validé**, s'il est encore affiché.
   **Attendu** : refus « code invalide ou déjà utilisé » (anti-rejeu) ; attendre le code suivant.
4. **Faire** : saisir le code courant.
   **Attendu** : session ouverte ; lien « Administration » visible dans la barre de navigation.
5. **Faire** : se déconnecter, se reconnecter, et utiliser **un code de secours** à la place du TOTP.
   **Attendu** : connexion réussie ; le même code de secours, réutilisé à la connexion suivante, est refusé.
6. **Faire** (facultatif) : 5 codes faux de suite.
   **Attendu** : l'étape de connexion est annulée, il faut ressaisir le mot de passe (EF-2.6).

### Étape 3 — Validation de l'adresse de l'admin (EF-4.3, EF-4.3b, CA-17b)

Prérequis : SMTP réglé (étape 6, sous-étape 2) ou dans l'env. Peut se jouer après l'étape 6.

1. **Faire** : dans le profil, « renvoyer le mail de validation » ; ouvrir le mail dans le SMTP de test.
   **Attendu** : le mail nomme l'identifiant `recette-admin` et dit « si tu n'as pas créé ce compte, ignore ce message ».
2. **Faire** : ouvrir le lien **dans une fenêtre privée** (sans session).
   **Attendu** : page neutre « connecte-toi au compte concerné, puis rouvre ce lien » ; adresse toujours en attente.
3. **Faire** : rouvrir le lien dans le navigateur « admin » connecté.
   **Attendu** : adresse validée (profil).

### Étape 4 — Invitation d'un utilisateur par l'admin (EF-3, CA-3, CA-17, CA-45)

1. **Faire** : Administration › Invitations › nouvelle invitation pour `alice@test.example`, **crédits initiaux 0**, note « Bienvenue pour la recette ».
   **Attendu** : invitation créée ; lien affiché **une fois** avec « Copier » ; mail reçu (avec SMTP) contenant « Message de recette-admin : Bienvenue pour la recette ». Les crédits de l'admin ne bougent pas (CA-3).
2. **Faire** : réinviter `alice@test.example`.
   **Attendu** : 409 avec un message explicite (adresse déjà invitée ; émetteur admin, EF-3.10).
3. **Faire** : ouvrir le lien dans le navigateur « utilisateurs » ; créer `alice` avec un mot de passe conforme.
   **Attendu** : redirection vers la connexion, aucune session (CA-45). Connexion d'`alice` **sans** second facteur (politique `admins` par défaut). Bandeau « adresse en attente de validation ».
4. **Faire** (avec SMTP) : `alice`, connectée, ouvre le mail de validation reçu.
   **Attendu** : adresse validée.

### Étape 5 — Invitation par un utilisateur avec crédits (EF-3.2, EF-3.5, EF-3.7, CA-2, CA-6)

Prérequis : adresse d'`alice` validée (étape 4.4).

1. **Faire** (Alice) : profil › Invitations.
   **Attendu** : aucune invitation possible, crédits 0.
2. **Faire** (admin) : Administration › Utilisateurs › `alice` › crédits = **1**.
   **Attendu** : valeur enregistrée, ligne d'audit.
3. **Faire** (Alice, après rechargement) : inviter `bob@test.example`.
   **Attendu** : avertissement « le crédit n'est pas rendu en cas de révocation ou d'expiration » **avant** la création ; invitation créée, lien affiché une fois ; crédits 0. Pas de champ « note » (réservé aux admins).
4. **Faire** (Alice) : tenter une seconde invitation.
   **Attendu** : refus (409), crédits 0.
5. **Faire** (Alice) : révoquer l'invitation de Bob.
   **Attendu** : invitation révoquée, crédits **toujours 0** (CA-6) ; le lien de Bob ne fonctionne plus (message neutre).
6. **Faire** (admin) : recréditer 1 ; Alice réinvite Bob ; Bob crée son compte `bob`.
   **Attendu** : compte `bob` créé, crédits de Bob 0 (invitation d'un non-admin, EF-3.9).

### Étape 6 — Réglages et secrets dans l'espace admin (EF-5, EF-6, CA-19, CA-21, CA-34, CA-36, CA-37)

1. **Faire** : Administration › Réglages › validité des invitations = 3 jours.
   **Attendu** : source « base » ; `docker compose exec kml-viewer flask settings-list` affiche la même valeur et la même source. « Revenir au défaut » ⇒ source « défaut » (ou « env »).
2. **Faire** : Administration › E-mail : hôte et port du SMTP de test (présents dans `OUTBOUND_ALLOWED_HOSTS`), sécurité selon le serveur, expéditeur ; « Envoyer un e-mail de test ».
   **Attendu** : réponse « ok » ou « ko » seulement, sans détail ; mail reçu si « ok ».
3. **Faire** : saisir un hôte SMTP **absent** de `OUTBOUND_ALLOWED_HOSTS`.
   **Attendu** : 400 « destination non autorisée », rien n'est écrit (CA-36).
4. **Faire** (si le SMTP de test a une authentification) : saisir le mot de passe SMTP, enregistrer, recharger la page.
   **Attendu** : « configuré », date et auteur ; le mot de passe n'est **jamais** réaffiché, pas même ses 4 derniers caractères (CA-19).
5. **Faire** : changer l'hôte SMTP pour un autre hôte autorisé.
   **Attendu** : avertissement préalable « le secret associé sera effacé » ; après enregistrement, mot de passe « non configuré » ; ligne d'audit (CA-34).
6. **Faire** : Réglages › Traces planifiées › `routing.base_url` = `http://<hôte du moteur>:8989` ; puis « Tester ».
   **Attendu** : enregistré ; « Tester » répond « ok ». Une URL hors liste blanche ⇒ 400.
7. **Faire** : `routing.coverage_bbox` = `[1.35, 48.80, 6.45, 51.55]` (défaut) ; essayer `[6, 48, 1, 51]`.
   **Attendu** : la seconde valeur est refusée en 400 (min ≥ max, CA-R20b).
8. **Faire** : Journal.
   **Attendu** : une ligne par action ci-dessus, valeurs de secret masquées, acteur « recette-admin » ; la ligne de l'étape 1 a l'acteur « Ligne de commande ».

### Étape 7 — Activation de la bêta pour une liste d'utilisateurs (EF-7, RB-1.1, CA-22, CA-R1)

1. **Faire** (admin) : Administration › Fonctions bêta › `route_builder` : état **Liste d'utilisateurs**, cocher `alice`.
   **Attendu** : enregistré, audit ; diagnostic du moteur « configuré, joignable », date du dernier sondage, aucun avertissement de couverture.
2. **Faire** : recharger le visualiseur en `alice`, puis en `bob`.
   **Attendu** : `alice` voit « Créer une trace » ; `bob` ne la voit pas, et `GET /api/route-builder/options` lui répond 404.
3. **Faire** (admin) : état **Tous**.
   **Attendu** : `bob` voit l'entrée. Remettre **Liste d'utilisateurs** : la liste (`alice`) est conservée.

### Étape 8 — Création d'une trace planifiée (RB-2 à RB-5, CA-R2, CA-R13, CA-R20)

Prérequis : `alice` a importé **au moins une activité réalisée** (upload d'un GPX horodaté) pour que les statistiques aient des valeurs.

1. **Faire** (Alice) : noter le tableau de bord (nombre d'activités, distance de la période, records).
2. **Faire** : visualiseur › « Créer une trace » ; cliquer à Londres ou Amsterdam.
   **Attendu** : point refusé, « hors de la zone couverte » ; contour du rectangle couvert visible.
3. **Faire** : poser **1 point** à Lille sans distance, « Terminer ».
   **Attendu** : refus « distance requise » (RB-3.2), aucune génération.
4. **Faire** : poser 3 points autour de Lille (ex. Citadelle, Vieux-Lille, Wazemmes), activité **Course à pied**, distance **15 km**, « Terminer ».
   **Attendu** : indicateur de progression puis boucle fermée sur le point 1, passant par les 3 points **dans l'ordre**, sur des voies réelles ; distance entre 12,75 et 17,25 km (± 15 %) **ou** avertissement d'écart ; points numérotés, aucun point technique affiché comme point de l'utilisateur.
5. **Faire** : « Autre proposition ».
   **Attendu** : une trace différente, mêmes points.
6. **Faire** : refaire avec 2 points en Ardennes belges (ex. Durbuy) et 40 km en **Gravel**.
   **Attendu** : trace générée ; si elle reste hors tolérance, avertissement « ajoute un point de passage… » (RB-4.14).
7. **Faire** : « Enregistrer dans la bibliothèque », nom « Recette Lille 15 km ».
   **Attendu** : trace enregistrée, lien vers la bibliothèque ; un second « Enregistrer » du même résultat ⇒ « déjà enregistrée ».
8. **Faire** : bibliothèque.
   **Attendu** : la trace porte le badge **Planifiée** ; filtre « Planifiées » ⇒ elle seule ; « Réalisées » ⇒ elle n'y est pas ; « Toutes » (défaut) ⇒ elle y est. Le mot « itinéraire » n'apparaît nulle part (CA-R19).
9. **Faire** : tableau de bord.
   **Attendu** : **identique** au relevé de 8.1 : nombre d'activités, distances, records inchangés (CA-R13).
10. **Faire** : ouvrir la trace enregistrée.
    **Attendu** : profil d'altitude (MNT si `DEM_URL` est configuré, sinon altitude du moteur ou aucune) ; export GPX possible.
11. **Faire** : se déconnecter avec des points en cours, se reconnecter en `bob` sur le même navigateur, ouvrir le mode.
    **Attendu** : aucun point d'Alice (RB-2.6, CA-R28).

---

## 4. Contrôles transverses (à faire en fin de parcours)

- Journaux du conteneur : aucun jeton (`#t=…`), mot de passe, code ni coordonnée de point (`docker compose logs kml-viewer | grep -i "t="` ne renvoie aucun jeton) (CA-5, CA-R17).
- En-têtes de réponse : `Cache-Control: no-store` sur `/admin` et `/auth/*` ; CSP sans `unsafe-inline`.
- Rechargement à chaud : modifier un réglage en CLI (`flask settings-reset <clé>`) ⇒ la page suivante voit la nouvelle valeur sans redémarrage (CA-21d).

## 5. Pas livré dans cette version (même une fois §1 terminé)

- **WebAuthn** (clés de sécurité, T12) : seul le TOTP est proposé.
- **Conversion d'un compte SSO en compte à mot de passe** (EF-0.5) : sans objet en mode `password` pur à l'installation.
- **Mot de passe oublié** : livré (T10), hors parcours ; à essayer en option avec le SMTP de test.
- **Service moteur de production et mise à jour mensuelle OSM** (TR7 : image du projet, `routing/update.sh`) : la recette utilise des données fixes. Les mesures sur « dune » sont faites (ADR-0007).
- **Appréciation visuelle des traces gravel** : à faire par l'utilisateur sur l'échantillon du spike (`spike/detours/out/echantillon/`) ; la recette ne juge pas la qualité terrain.
- **Doc d'exploitation à jour** (T17, TR8) : les procédures de `docs/exploitation.md` marquent encore le mode `password` « non utilisable ».
- Points de doc restants de dev (Doc-2 à Doc-6 de `DEV-ETAT.md`) : avertissement « dernier admin » de `user-mfa-reset`, avertissement SMTP absent au démarrage, etc.
