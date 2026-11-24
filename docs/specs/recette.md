# Recette manuelle — comptes à mot de passe, administration, traces planifiées (bêta)

Statut : **v3, 2026-11-25** (TR5, TR6, Doc-3 et Doc-12 livrés) · Rôle : spec · Specs : [admin-space](admin-space.md), [gpx-route-builder](gpx-route-builder.md)
Demandée par l'utilisateur (via coord) pour une **version de test**. Parcours pas à pas pour un testeur, avec le résultat attendu et le critère d'acceptation (CA) correspondant.

Sources : code de l'arbre de travail jusqu'aux commits `85ca649` (TR5) et `15fc4f0` (TR6, Doc-3), `15b46ee` (Doc-12) ; README et `docs/exploitation.md` rédigés par doc (amorçage du premier admin et procédures du service de routage rejoués). Les messages cités entre guillemets sont ceux du code.

---

## 1. Ce qui est jouable

| Étape | Jouable ? | Condition |
|---|---|---|
| 0. Service de routage (compose, profil `routing`) | **Oui** | accès à `download.geofabrik.de` |
| 1. Premier admin par la CLI (et reprise `--reset-password`) | **Oui** | — |
| 2. Connexion avec TOTP | **Oui** | serveur à l'heure (NTP) |
| 3. E-mail et validation de l'adresse de l'admin | **Oui** | **SMTP de test** |
| 4. Invitation d'un utilisateur par l'admin | **Oui** | SMTP pour la validation d'adresse |
| 5. Invitation par un utilisateur avec crédits | **Oui** | **SMTP** (adresse validée exigée pour inviter, EF-3.2) |
| 6. Réglages et secrets | **Oui** | — |
| 7. Activation de la bêta par liste | **Oui** | étape 0 faite, moteur sain |
| 8. Trace planifiée | **Oui** | étapes 0 et 7 |

Seul élément non livré : **WebAuthn (T12)** (§5). Écarts connus entre le code et la spec, à constater sans les remonter à nouveau : §5.

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
ROUTING_BASE_URL=http://graphhopper:8989
DEM_URL=<facultatif : altitude MNT des traces enregistrées>
```

Il n'existe **aucune variable d'env SMTP** : le SMTP se règle dans l'admin (étape 3). Ne pas poser `COMPOSE_PROFILES=routing` avant le premier graphe (étape 0).

**Matériel du testeur** : une application TOTP (Aegis, Google Authenticator, FreeOTP…) ; deux navigateurs ou fenêtres privées (admin, utilisateurs) ; trois adresses e-mail de test (admin, Alice, Bob) qui arrivent dans le SMTP de test ; un fichier GPX **horodaté** d'une vraie sortie (pour l'étape 8).

**Démarrage** : `docker compose up -d`, puis `docker compose logs kml-viewer`. **Attendu** : aucune erreur ; le `WARNING` « SMTP non configuré : les adresses e-mail ne peuvent pas être validées » est normal à ce stade.

---

## 3. Parcours

Notation : **Faire** = action du testeur ; **Attendu** = résultat à constater. Toute différence est un défaut à remonter avec l'heure, l'étape et une capture.

### Étape 0 — Service de routage du compose (TR7, ADR-0007)

Procédure détaillée : `docs/exploitation.md`, « Service de routage ».

1. **Faire** : dans le dossier du projet, `./routing/update.sh`.
   **Attendu** : lignes horodatées `routing-update: début … téléchargement … fusion … import … bascule … terminé : graphe graph-… en service` ; code de sortie 0 ; quelques minutes (import 154 s sur dune, ~11 min avec le téléchargement sur un poste).
2. **Faire** : `docker compose --profile routing ps graphhopper`.
   **Attendu** : état **`healthy`** (contrôle `/health` interne toutes les 30 s), **aucun port publié** (colonne des ports sans `0.0.0.0` ni `127.0.0.1`), aucun redémarrage en boucle.
3. **Faire** : poser `COMPOSE_PROFILES=routing` dans `.env`, puis `docker compose up -d` ; `docker compose config --services`.
   **Attendu** : `graphhopper` listé ; le service reste `healthy`.
4. **Faire** : après l'étape 8 (quelques générations), `docker stats --no-stream` et `docker inspect --format '{{.State.OOMKilled}} {{.RestartCount}}' <conteneur graphhopper>`.
   **Attendu** : RAM du conteneur **≤ ~1,5 Gio** (1,43 Gio mesurés après 256 appels avec `-Xmx1536m`), `false 0`.
5. **Faire** (facultatif) : relancer `./routing/update.sh`.
   **Attendu** : relance sans risque (verrou) ; l'ancien graphe reste (`ROUTING_KEEP_GRAPHS`, défaut 2). Retour arrière manuel : `docs/exploitation.md`.

### Étape 1 — Premier admin par la CLI (EF-0.3, EF-0.3b, CA-33, CA-54)

1. **Faire** sur le serveur, dans le dossier du projet :
   `docker compose exec kml-viewer flask user-invite --admin admin@test.example`
   **Attendu** : « Jeton d'amorçage créé (valable jusqu'au …, usage unique). Lien, montré une seule fois : » suivi de `https://<hôte>/auth/token#t=…` ; sans SMTP, « E-mail non configuré : transmets ce lien à la seule personne concernée. » ; ligne d'audit avec l'acteur « Ligne de commande ».
2. **Faire** : ouvrir le lien dans le navigateur « admin ».
   **Attendu** : le `#t=…` disparaît de la barre d'adresse ; formulaire identifiant + mot de passe.
3. **Faire** : identifiant `admin`.
   **Attendu** : « Identifiant indisponible » (réservé, EF-1.2b), jeton non consommé. Saisir `recette-admin`.
4. **Faire** : mot de passe de 11 caractères, puis `azertyuiop123`.
   **Attendu** : refus pour la longueur (moins de 12 caractères), puis « Ce mot de passe est trop courant. » (CA-8, CA-8b). Choisir un mot de passe long et propre.
5. **Faire** : valider.
   **Attendu** : page d'enrôlement (QR code et secret en texte), **aucune session** (un autre onglet sur `/` mène à la connexion). Fenêtre d'enrôlement : **15 min**.
6. **Faire** : scanner le QR code, saisir le code à 6 chiffres.
   **Attendu** : le compte devient admin à cet instant ; **10 codes de secours** affichés une seule fois (`XXXX-XXXX-XXXX-XXXX`), à noter ; renvoi vers la connexion.
7. **Faire** : rouvrir le lien d'amorçage.
   **Attendu** : message neutre, sans détail (CA-5).

**Reprise d'un amorçage abandonné** (à jouer avec un second compte jetable, ex. `flask user-invite --admin jetable@test.example`, en fermant l'onglet à la page d'enrôlement) :
8. **Faire** : `docker compose exec kml-viewer flask user-invite --admin --account jetable`.
   **Attendu** : nouveau lien ; il demande le **mot de passe actuel** du compte ; un mauvais mot de passe est refusé sans consommer le jeton ; le bon mène à l'enrôlement, et le mot de passe reste inchangé.
9. **Faire** : `docker compose exec kml-viewer flask user-invite --admin --account jetable --reset-password` (mot de passe perdu).
   **Attendu** : le lien propose de **définir un nouveau mot de passe** (politique appliquée), puis l'enrôlement ; ensuite l'ancien mot de passe est refusé (CA-54). Sans `--account` : « --reset-password demande --account <identifiant>. »
10. **Faire** : supprimer `jetable` (Administration › Utilisateurs) une fois l'essai fini.

### Étape 2 — Connexion avec second facteur TOTP (EF-1.6, EF-2.3, CA-7, CA-10, CA-12)

1. **Faire** : connexion avec `recette-admin` et un mauvais mot de passe, puis avec un identifiant inexistant.
   **Attendu** : même message générique (CA-7).
2. **Faire** : bon mot de passe.
   **Attendu** : page « code du second facteur ».
3. **Faire** : saisir le code déjà utilisé à l'enrôlement, s'il est encore affiché.
   **Attendu** : « Code invalide ou déjà utilisé » ; attendre le code suivant.
4. **Faire** : code courant.
   **Attendu** : session ouverte ; lien **Administration** dans la barre de navigation.
5. **Faire** : déconnexion, reconnexion avec **un code de secours** ; à la connexion suivante, réutiliser ce même code.
   **Attendu** : la première fois, connexion ; la seconde, refus.
6. **Faire** (facultatif) : 5 codes faux de suite.
   **Attendu** : il faut ressaisir le mot de passe (EF-2.6).

### Étape 3 — E-mail et validation de l'adresse de l'admin (EF-4.1, EF-4.2, EF-4.3b, CA-17b)

1. **Faire** : Administration › **Clés API et e-mail** : hôte et port du SMTP de test (présents dans `OUTBOUND_ALLOWED_HOSTS`), sécurité adaptée (`none` pour un capteur sans TLS, **sans** identifiant ni mot de passe), expéditeur ; puis « Envoyer un e-mail de test ».
   **Attendu** : réponse « ok » ou « ko » seulement ; le mail arrive à l'adresse **en attente** de l'admin (P-T14-9).
2. **Faire** : hôte SMTP **absent** de `OUTBOUND_ALLOWED_HOSTS`.
   **Attendu** : 400, rien n'est écrit (CA-36). Remettre le bon hôte.
3. **Faire** : Administration › Utilisateurs › ligne `recette-admin` › « Renvoyer la validation » ; ouvrir le mail.
   **Attendu** : le mail nomme l'identifiant `recette-admin` et dit « si tu n'as pas créé ce compte, ignore ce message ».
4. **Faire** : ouvrir le lien **dans une fenêtre privée** (sans session).
   **Attendu** : page neutre « connecte-toi au compte concerné, puis rouvre ce lien » ; adresse toujours en attente.
5. **Faire** : rouvrir le lien dans le navigateur « admin » connecté.
   **Attendu** : adresse validée (onglet Utilisateurs).
6. **Faire** : `docker compose up -d --force-recreate kml-viewer`, puis relire les logs.
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
6. **Faire** (admin) : recréditer 1 ; Alice réinvite Bob ; Bob crée `bob` (sans second facteur) et valide son adresse.
   **Attendu** : `bob` créé avec 0 crédit (invitation d'un non-admin, EF-3.9).

### Étape 6 — Réglages et secrets (EF-2.2, EF-5, EF-6, CA-13b, CA-19, CA-21, CA-34)

1. **Faire** : Administration › Réglages › validité des invitations = 72 h.
   **Attendu** : source « base » ; `docker compose exec kml-viewer flask settings-list` affiche la même valeur et la même source. « Revenir au défaut » ⇒ source « défaut ».
2. **Faire** : une valeur hors bornes (0 h ou 1 000 h).
   **Attendu** : 400 avec le champ, lot non écrit (CA-21c).
3. **Faire** (si le SMTP de test a une authentification, sécurité `starttls` ou `tls`) : saisir le mot de passe SMTP, enregistrer, recharger.
   **Attendu** : « configuré », date, auteur et destination liée ; mot de passe **jamais** réaffiché, pas même ses 4 derniers caractères (CA-19).
4. **Faire** : changer l'hôte SMTP pour un autre hôte autorisé.
   **Attendu** : mot de passe SMTP effacé, ligne d'audit (CA-34). Remettre l'hôte de test.
5. **Faire** : `routing.base_url` = `http://exemple.org:8989` (hors liste blanche).
   **Attendu** : 400, rien n'est écrit.
6. **Faire** : `routing.coverage_bbox` = `[6, 48, 1, 51]`, puis une latitude de 95.
   **Attendu** : 400 dans les deux cas (CA-R20b) ; la valeur par défaut `[1.35, 48.80, 6.45, 51.55]` reste en place.
7. **Faire** : `docker compose exec kml-viewer flask settings-reset auth.invite_ttl_hours`, puis recharger l'onglet.
   **Attendu** : nouvelle valeur visible sans redémarrage (CA-21d).
8. **Faire** : `bob` n'ayant pas de second facteur, passer `auth.mfa_policy` à `all`.
   **Attendu** : refus (409) « 1 compte(s) à mot de passe actif(s) n'ont aucun second facteur… », valeur toujours `admins`, ligne d'audit `setting.refused` (EF-2.2, CA-13b).
9. **Faire** : Journal ; filtrer sur l'acteur « cli ».
   **Attendu** : une ligne par action, secrets masqués ; l'amorçage et le `settings-reset` apparaissent avec l'acteur « Ligne de commande ».

### Étape 7 — Activation de la bêta pour une liste d'utilisateurs (EF-7, RB-1.1, RB-1.2, CA-22, CA-R1)

Prérequis : étape 0 (moteur `healthy`) ; `ROUTING_BASE_URL` posée et application recréée.

1. **Faire** (admin) : Administration › Fonctions bêta › « Traces planifiées ».
   **Attendu** : diagnostic du moteur (JSON) avec `configured: true`, `available: true`, `checked_at` récent (sondage au démarrage puis toutes les 60 s), les 6 profils. `coverage_contained` vaut **`false`** avec le rectangle par défaut : il déborde légèrement, au sud et à l'est, du rectangle annoncé par le moteur (`48,83` et `6,42`) ; c'est un signalement sans effet sur le service (RB-1.3, voir §5).
2. **Faire** : état « Pour une liste de comptes », cocher `alice`, Enregistrer.
   **Attendu** : enregistré ; ligne d'audit.
3. **Faire** : recharger le visualiseur en `alice`, puis en `bob`.
   **Attendu** : `alice` voit le bouton **« Créer une trace »** ; `bob` ne le voit pas, et `GET /api/route-builder/options` lui répond **404**.
4. **Faire** (admin) : « Pour tous », puis revenir à « Pour une liste de comptes ».
   **Attendu** : `bob` voit le bouton pendant l'état « Pour tous » ; la liste (`alice`) est conservée au retour (EF-7.2).
5. **Faire** (facultatif) : `docker compose stop graphhopper`, attendre une minute, ouvrir le mode en `alice`.
   **Attendu** : le bouton reste visible ; « Service momentanément indisponible. », « Terminer » désactivé ; une génération par l'API répond 503 (RB-1.2, CA-R24). `docker compose start graphhopper` ⇒ disponible au sondage suivant.

### Étape 8 — Création d'une trace planifiée (RB-2 à RB-5, CA-R13, CA-R19, CA-R20, CA-R25, CA-R28)

Prérequis : `alice` dans la liste de la bêta ; `alice` a importé **une activité réalisée** (GPX horodaté) pour que le tableau de bord ait des valeurs.

**Placement et limites**
1. **Faire** (Alice) : noter le tableau de bord (nombre d'activités, distance de la période, records).
2. **Faire** : visualiseur › « Créer une trace ».
   **Attendu** : panneau « Créer une trace » ; rectangle de couverture en pointillés bleus ; activité par défaut selon le profil, sinon Course à pied.
3. **Faire** : cliquer à Londres ou à Amsterdam.
   **Attendu** : « Hors de la zone couverte. », aucun point posé (RB-1.3).
4. **Faire** : 1 point à Lille sans distance.
   **Attendu** : « Terminer » reste **désactivé** tant que la distance est vide (RB-3.2).
5. **Faire** : poser, déplacer (glisser), retirer (clic droit) des points ; « Annuler le dernier », « Rétablir », « Effacer ».
   **Attendu** : numérotation dans l'ordre, compteur « N points » à jour.

**Génération**
6. **Faire** : 3 points autour de Lille (Citadelle, Vieux-Lille, Wazemmes), **Course à pied**, **15 km**, « Terminer ».
   **Attendu** : « Calcul en cours… » puis la trace s'affiche comme un fichier ouvert : boucle fermée sur le point 1, passant par les 3 points **dans l'ordre**, sur des voies réelles ; résumé « 15,x km · écart ±x % » ; distance entre 12,75 et 17,25 km, sinon avertissement « Distance obtenue : X km pour 15.0 km voulus (±N %). ».
7. **Faire** : « Autre proposition ».
   **Attendu** : une trace différente, mêmes points.
8. **Faire** : 2 points à 5 km l'un de l'autre en plaine (ex. deux villages des Flandres), **Vélo de route**, sans distance.
   **Attendu** : boucle de base sans détour ; l'avertissement « La trace emprunte plusieurs fois les mêmes voies. » n'apparaît que si le recouvrement dépasse **40 %** (seuil à 2 points ; 25 % à partir de 3 points, RB-4.13).
9. **Faire** : 2 points à Durbuy (Ardennes belges), **Gravel**, 40 km ; puis 2 points sur le littoral (Wissant), **Course à pied**, 25 km.
   **Attendu** : trace générée ; hors tolérance ⇒ avertissement d'écart **suivi de** « Ajoute un point de passage pour une boucle plus proche de la distance voulue. » (RB-4.14). Le cas est fréquent en relief et sur le littoral (ADR-0010) : ce n'est pas un défaut.
10. **Faire** : 3 points espacés de 3 km, distance **5 km**.
    **Attendu** : avertissement « Distance minimale passant par tes points : X km. » ; aucun point retiré ni déplacé (RB-4.3).
11. **Faire** : un point dans le rectangle mais hors des données (ex. Maastricht, Pays-Bas, ou Charleville-Mézières).
    **Attendu** : 422 « Le point N ne peut pas être approché (hors réseau ou hors de la zone couverte). », ou « Aucun itinéraire trouvé… » selon la réponse du moteur (RB-1.3 ; voir §5) ; jamais de trace.
12. **Faire** : lancer une génération longue (15 points, 100 km), cliquer « Annuler » puis relancer aussitôt.
    **Attendu** : « Calcul abandonné (le serveur termine dans son budget de temps). » ; la relance immédiate peut répondre « Une génération est encore en cours, réessaie dans quelques secondes. » (P-TR5-6).

**Altitude (réserve RB-4.8)**
13. **Attendu** pendant toute la génération : **pas de profil d'altitude ni de D+** avant l'enregistrement. Le moteur est déployé sans altitude, et l'altitude MNT n'est calculée qu'à l'enregistrement. Ce n'est pas un défaut.

**Enregistrement et bibliothèque**
14. **Faire** : nom « Recette Lille 15 km », « Enregistrer ».
    **Attendu** : « Enregistrée : ouvrir dans la bibliothèque ».
15. **Faire** : « Enregistrer » à nouveau, même résultat, autre nom.
    **Attendu** : « Déjà enregistrée : ouvrir dans la bibliothèque », aucune seconde trace (CA-R25).
16. **Faire** : un nom de 201 caractères (après une nouvelle génération).
    **Attendu** : « Nom : 1 à 200 caractères, sans caractère de contrôle ».
17. **Faire** : ouvrir la trace enregistrée.
    **Attendu** : avec `DEM_URL`, profil d'altitude MNT après quelques secondes (calcul en tâche de fond) ; sans `DEM_URL`, pas d'altitude. Export GPX possible ; le GPX n'a ni horodatage ni point technique en `<wpt>`.
18. **Faire** : bibliothèque (page du tableau de bord), filtre « Nature des traces ».
    **Attendu** : badge **Planifiée** sur la trace ; « Planifiées » ⇒ les traces planifiées seules ; « Réalisées » ⇒ elles sont absentes ; « Toutes » (défaut) ⇒ présentes.
19. **Faire** : relever le tableau de bord.
    **Attendu** : **identique** au relevé de l'étape 8.1 : nombre d'activités, distances et records inchangés (CA-R13).
20. **Faire** : attendre 30 min (ou redémarrer `kml-viewer`) après une génération, puis « Enregistrer ».
    **Attendu** : « Résultat introuvable ou expiré » ; régénérer.

**Points en cours et comptes**
21. **Faire** : poser des points sans générer, se déconnecter, se reconnecter en `bob` (bêta « Pour tous ») sur le même navigateur, ouvrir le mode.
    **Attendu** : aucun point d'Alice (RB-2.6, CA-R28).

---

## 4. Contrôles transverses (fin de parcours)

- Journaux de `kml-viewer` : aucun jeton (`#t=…`), mot de passe, code ni **coordonnée de point** (CA-5, CA-R17).
- En-têtes : `Cache-Control: no-store` sur `/admin` et `/auth/*` ; CSP sans `unsafe-inline`.
- Perte de facteur (facultatif) : `docker compose exec kml-viewer flask user-mfa-reset alice` ⇒ facteurs retirés, sessions fermées, notification ; sur le dernier admin, la commande annonce « dernier administrateur » **avant** d'agir.

## 5. Non livré et écarts connus

| Élément | Nature | Conséquence pour la recette |
|---|---|---|
| **WebAuthn** (clés de sécurité) | non livré (**T12**) | seul le TOTP est proposé ; `WEBAUTHN_RP_ID` n'est pas lue |
| « Tester » le moteur (Clés API et e-mail) | écart signalé à coord : aucune sonde branchée (`routing_test`) | répond « ko » même moteur sain ; l'état réel se lit dans le diagnostic de « Fonctions bêta » (étape 7.1) |
| Message 422 « Aucun itinéraire trouvé : essaie d'autres points. » | écart signalé à coord : le mot « itinéraire » est banni de l'UI (CA-R19) | à constater seulement si aucune boucle n'est possible |
| Édition des points (RB-2.2) : pas de raccourcis Ctrl+Z / Ctrl+Y, pas d'« insérer avant », « Effacer » sans confirmation, retrait par clic droit seulement (pas d'équivalent tactile) | écart signalé à coord | « Annuler le dernier » rattrape un effacement involontaire |
| `coverage_contained: false` avec le rectangle par défaut | choix de spec à confirmer (§13 de la spec routage) | signalement sans effet ; défaut proposé `[1.35, 48.83, 6.42, 51.55]` pour l'éviter |
| Retour arrière manuel de `routing/update.sh` | procédure à la main (`docs/exploitation.md`) | — |
| Appréciation visuelle des traces gravel | à faire par l'utilisateur | sur l'échantillon du spike ou sur les traces de l'étape 8.9 |
