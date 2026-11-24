# ADR-0006 — Limiteur de débit en mémoire

Statut : **Accepté** (2e vague ; révisé post-review M1, M8, M15 et re-review r1, r9, relayées par coord le 2026-11-24) · Specs : [admin-space](../specs/admin-space.md) EF-S3, [gpx-route-builder](../specs/gpx-route-builder.md) RB-S5

## Contexte
Login, second facteur, mot de passe oublié, jetons et génération de traces planifiées (n2) doivent être limités. Un seul worker gunicorn (8 threads). La review a relevé : une clé par **compte** existant est un oracle d'existence (M1) ; la politique à saturation n'était pas définie (M8) ; la spec et l'ADR divergeaient (fenêtres fixes vs attente progressive, M15).

## Décision
- **Fenêtres glissantes à seuil fixe**, 429 générique, pas d'attente progressive, pas de verrouillage définitif.
- **Compteur à mémoire constante** (r1) : fenêtre glissante **approchée à deux compteurs** (fenêtre fixe courante et précédente, la seconde pondérée par la part de fenêtre écoulée) plutôt qu'une liste d'horodatages ; erreur d'approximation bornée par le compte de la fenêtre précédente, sans incidence pour un limiteur anti-force brute. **Garantie testable** (v5-2) : au plus « seuil » par fenêtre fixe, et au plus 2 × seuil sur toute fenêtre glissante ; les critères d'acceptation sont écrits ainsi, horloge injectée.
- **Ordre** (r9) : le limiteur est consulté **avant** toute opération coûteuse (hachage argon2id) ; une requête refusée n'acquiert pas le sémaphore de hachage.
- **Clés** : identifiant ou e-mail **saisi**, normalisé (NFKC, casefold), qu'il corresponde à un compte ou non ; IP ; compte (second facteur seulement, quand l'identifiant est déjà établi) ; utilisateur (générations).
- **Mémoire bornée par famille, sans éviction exploitable** (M8) :
  - famille « identifiant » : tableau **fixe** de **65 536** compartiments par famille (mot de passe, mot de passe oublié) indexés par HMAC de l'identifiant normalisé, avec une clé aléatoire tirée au démarrage ; mémoire constante, rien n'est évincé ; deux identifiants qui partagent un compartiment se partagent la limite (plus strict, jamais plus permissif) ;
  - famille « IP » : LRU bornée à **50 000** entrées ; l'éviction ne profite qu'à un attaquant disposant de nombreuses IP, qui contourne de toute façon la limite par IP ;
  - familles « compte » et « utilisateur » : bornées par le nombre de comptes.
- Tailles injectables par configuration (tests à petite échelle, CA-32).
- Interface `RateLimiter` (`hit(key) -> allowed, retry_after`) remplaçable par un stockage partagé.
- Les seuils sont des réglages bornés en bas **et** en haut (ADR-0008, M7).

## Conséquences
- Compteurs remis à zéro au redémarrage : acceptable (fenêtres de 15 min à 1 h).
- **À revoir** si plusieurs workers : chaque processus aurait ses compteurs.
- L'IP vient de `ProxyFix` : un `TRUST_PROXY` faux fausse la limite par IP, pas celle par identifiant.

## Options écartées
- Clé par identifiant de compte (version initiale) : oracle d'existence (M1).
- « Compteurs par identifiant jamais évincés », suggéré par la review : les identifiants saisis étant choisis par l'attaquant, la mémoire croîtrait sans borne ; les compartiments fixes donnent la même garantie (aucune éviction) à mémoire constante.
- Table SQLite ; Flask-Limiter.
