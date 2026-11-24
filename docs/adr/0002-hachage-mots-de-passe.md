# ADR-0002 — Hachage des mots de passe : argon2id

Statut : **Accepté** (arbitrage utilisateur, 2e vague ; sémaphore de hachage post-review m4 ; liste de mots de passe courants (top 1 000 000) arbitrée le 2026-11-24 ; relayés par coord) · Spec : [admin-space](../specs/admin-space.md) EF-1.4, EF-1.4b, EF-1.5

## Contexte
Les comptes à mot de passe imposent un hash adaptatif et salé. Deux options sans compromis de sécurité réel : argon2id (premier choix OWASP) ou scrypt (acceptable OWASP, déjà fourni par `werkzeug.security`).

## Décision
- **argon2id** via `argon2-cffi` (`PasswordHasher`), paramètres par défaut de la bibliothèque au minimum, figés dans `app/auth/passwords.py`.
- Rehachage à la connexion si `check_needs_rehash` (paramètres relevés plus tard).
- Normalisation NFKC avant hachage ; longueur max. 128 (borne le coût CPU).
- Hash factice vérifié pour un compte inconnu (temps de réponse homogène).

## Liste de mots de passe courants (arbitrages utilisateur du 2026-11-24)
- Liste publique **top 1 000 000** embarquée dans le dépôt, jamais téléchargée à l'exécution : SecLists `Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt`, commit `c205c36a445bff37f8e58a9ec829105cd4975c58`, blob `e5e73232c8f4f53edfe9c0fd699f415da8a336e3`, SHA-256 du fichier brut `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51` (vérifié sur deux téléchargements indépendants ; détail : spec EF-1.4b).
- **Union avec la liste d'amorce locale** (71 entrées) : 46 146 entrées publiques de 12 caractères ou plus, 30 en commun, **46 187 au total** ; fichier livré de 675 446 octets.
- Licence **acceptée en l'état** : dépôt SecLists sous MIT (vérifié : `LICENSE`, « Copyright (c) 2018 Daniel Miessler ») ; données de Mark Burnett (2015), décrites comme versées au domaine public par la presse, texte d'origine non vérifiable (Medium : 403, archive inaccessible). Avis MIT complet et provenance livrés dans `web-app/app/auth/wordlists/common_passwords.SOURCE`.
- Génération par script reproductible (SHA-256 de la source vérifié, NFKC puis `casefold()`, filtre 12–128 après normalisation, union, dédoublonnage, tri ; sortie au SHA-256 attendu `ec1dce68…fecaa`).
- Chargement **au démarrage**, dans un `frozenset`, seulement quand les comptes à mot de passe sont disponibles : ~60 ms et ~5 Mo mesurés ; fichier absent ou vide ⇒ refus de démarrer. Comparaison inchangée (`normalize(mot de passe).casefold() in liste`).
- Le top 10 000 envisagé d'abord a été écarté : à 12 caractères minimum, il ne donnait que 24 entrées utiles.

## Conséquences
- Nouvelle dépendance directe (`argon2-cffi`, et `argon2-cffi-bindings` compilé) : à épingler avec empreintes dans `requirements.txt` ; vérifier la disponibilité de la roue pour la version Python de l'image.
- Coût par vérification : quelques dizaines de ms de CPU et ~64 Mio de mémoire avec les paramètres par défaut d'argon2-cffi ; 8 threads simultanés sur une route **publique** (hash factice compris) monteraient à ~512 Mio. D'où un **sémaphore de hachage à 2 places** (attente ≤ 5 s puis 429 générique, identique pour tous les identifiants) et une mention de la mémoire du conteneur dans la doc ops (m4). Le limiteur de débit est consulté avant le sémaphore (r9).

## Options écartées
- scrypt via Werkzeug : zéro dépendance, acceptable ; écarté car argon2id est la recommandation de référence et l'exigence est « sécurité d'abord ». **Repli valable** si l'ajout de dépendance est refusé.
- bcrypt : troncature à 72 octets, moins résistant aux GPU qu'argon2id.
- Liste `10k-most-common.txt` de SecLists : provenance non documentée.
