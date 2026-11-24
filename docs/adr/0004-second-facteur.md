# ADR-0004 — Second facteur : TOTP et WebAuthn

Statut : **Accepté** (principe : Q6 ; bibliothèques et politique : 2e vague ; compléments post-review B5, M3, M12, m2, m3 et re-review r6, r8 relayés par coord le 2026-11-24) · Spec : [admin-space](../specs/admin-space.md) §5.2

## Contexte
Les comptes à mot de passe ont besoin d'un second facteur ; les comptes SSO le tiennent de leur IdP. CSP stricte. La session Flask est un **cookie signé côté client** : tout état mis dedans peut être rejoué (M3).

## Décision
- **TOTP** (RFC 6238, SHA-1, 6 chiffres, 30 s) : `pyotp` ; secret 160 bits chiffré (ADR-0003) ; fenêtre ±1 ; anti-rejeu **atomique** (mise à jour conditionnelle du dernier pas accepté, tout pas ≤ dernier refusé) (m2). QR code SVG par `segno`.
- **WebAuthn** en second facteur : `webauthn` (py_webauthn) ; RP ID = hôte de `PUBLIC_URL` (surcharge `WEBAUTHN_RP_ID`) ; origine = origine de `PUBLIC_URL`, `http://localhost` seulement en test/debug (M12) ; attestation `none` ; contrôle de régression du compteur.
- **État serveur** (M3) : la connexion en cours (`mfa_pending` : compte, étape, échéance, essais) et les défis WebAuthn vivent **côté serveur** (mémoire bornée) ; le cookie ne porte qu'un identifiant opaque, jamais `user_id` ; défis et états consommés atomiquement.
- **Codes de secours** : 10 codes de 16 caractères base32 (80 bits), SHA-256, comparaison en temps constant, usage unique (m3).
- **Invariant (B5)** : tout compte `password` admin a au moins un facteur actif, à tout instant. **Facteur actif = TOTP activé ou au moins une clé WebAuthn** ; les codes de secours n'en sont pas et sont invalidés quand plus aucun facteur ne reste (r6). Promotion sans facteur refusée (API, UI, CLI) ; dernier facteur d'un admin non supprimable ; réinitialisation des facteurs d'un admin ⇒ rétrogradation ; passage de `auth.mfa_policy` à `all` refusé tant qu'un compte `password` actif n'a pas de facteur ; `admin_required` revérifie la présence d'un facteur. Amorçage : compte créé non admin, `is_admin` posé dans la transaction qui enregistre le premier facteur. Les commandes CLI de secours (`user-mfa-reset`, `secrets-reset`) peuvent rétrograder le dernier admin ; l'UI et l'API ne le peuvent pas (r6).
- **Aucune consommation de jeton n'ouvre de session** (r8) : redirection vers la connexion, où s'appliquent second facteur et enrôlement obligatoire ; un seul chemin d'ouverture de session.
- Politique : `auth.mfa_policy` = `admins` (défaut) | `all`.

## Conséquences
- Dépendances directes `pyotp`, `segno`, `webauthn` (+ `cbor2`) à épingler avec empreintes.
- Redémarrage ⇒ connexions en deux étapes en cours perdues (reconnexion).
- Changer `PUBLIC_URL` invalide les clés enregistrées, sauf RP ID figé par `WEBAUTHN_RP_ID`.
- NTP requis.

## Options écartées
- État `mfa_pending` dans le cookie : rejouable (essais remis à zéro, défi réutilisable).
- RP ID tiré de `TRUSTED_HOSTS` : liste de motifs sans ordre significatif ; un ajout en tête casserait toutes les clés.
- TOTP écrit à la main : repli valable si une dépendance de moins est préférée.
- WebAuthn implémenté à la main ; SMS / e-mail comme facteur.
