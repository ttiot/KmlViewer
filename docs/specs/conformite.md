# Matrice de conformité spec / code (avant la production)

Statut : **v3, 2026-11-25** (v2 : décisions de l'utilisateur ; v3 : décisions de coord et corrections de `6faa266`, §5) · Rôle : spec · Demandée par coord pour l'utilisateur.
Specs : [admin-space](admin-space.md), [gpx-route-builder](gpx-route-builder.md) ; recette : [recette.md](recette.md).

**Base relue** : `HEAD` = `9e415f1` (branche `feature/admin-space-and-route-builder`), plus l'arbre de travail non commité du 2026-11-25 matin, qui contient le correctif MFA-1 du limiteur du second facteur (`auth/mfa_limit.py` : `reserve` / `release`).

**Méthode** : lecture du code et des tests, par trois lectures indépendantes. Les écarts importants ont été relus par spec dans le code.
- **Aucun test n'a été exécuté** pour ce document. Un statut « testé » veut dire qu'un test existe et vérifie le point d'après sa lecture.
- Les résultats d'exécution sont ceux du journal `STATUS.md` : CI GitHub verte sur `117660d`, 5 623 passed, couverture 96,13 % ; porte locale de `test` sur `37ad93c`.

Chemins : code relatif à `web-app/app/`, tests relatifs à `web-app/tests/`.

---

## 1. Synthèse

- **Toutes les fonctions des deux specs sont livrées**, WebAuthn (T12) et constructeur de traces (TR5, TR6) compris.
- **Aucun écart bloquant de sécurité.** Les invariants forts sont livrés et testés :
  - jetons hors des journaux ;
  - état `mfa_pending` côté serveur ;
  - TOTP et codes de secours anti-rejeu, concurrence comprise ;
  - dernier admin protégé ;
  - liste blanche des destinations sortantes ;
  - liaison secret ↔ destination ;
  - CSP `img-src` restreinte ;
  - limiteur consulté avant le hachage ;
  - `result_id` lié à l'utilisateur.
- **Écarts relevés : 28** (E-1 à E-28, §5), **tous décidés** le 25/11 : 3 corrigés par dev (E-2, E-4, E-5, commit `6faa266`) ; à corriger dans le code, avec CA testables : E-1, E-3, E-6, E-7, E-8, E-10, E-12, E-13, E-15 (décisions de l'utilisateur), E-16, E-20/E-21 en partie, E-24, E-26 (décisions de coord) ; acceptés avec spec alignée ou reportés : le reste. Les décisions de coord sont à relire par l'utilisateur.
- **Refonte de la page Réglages** (retour de recette de l'utilisateur, 25/11) : admin-space §5.6b, EF-6.9 à EF-6.17, **CA-67 à CA-80**, non livrée ; les 6 questions ont été tranchées par coord le 25/11. Écarts qu'elle crée dans cette matrice : §2.4 (ligne Réglages), §3 (CA-67 à CA-80), E-21 (deux points du polissage accepté deviennent exigés), §7 (banc de test navigateur local, hors CI, à créer).
- **Décisions de coord à relire** : 16 (§6).
- **Trous de test** : §7. Les tests du front sont surtout statiques (recherche de chaînes dans le JS), pas des tests de comportement.
- **Incohérences de spec** trouvées et corrigées pendant ce travail : §8.

Légende des statuts :

| Statut | Sens |
|---|---|
| **OK** | livré, et un test vérifie le comportement |
| **OK (test partiel)** | livré ; le test ne couvre qu'une partie du critère |
| **Non testé** | livré, sans test (ou avec un test statique seulement) |
| **Écart** | le code fait autre chose que la spec (renvoi au §5) |
| **Non livré** | absent du code |

---

## 2. admin-space : exigences fonctionnelles

### 2.1 Modes, comptes à mot de passe, second facteur, invitations (§5.0 à §5.3)

| Exigence | Statut | Code | Test principal | Remarque |
|---|---|---|---|---|
| EF-0.0 garde sur `auth_mode()`, `sub='local'` en mode `local` seulement | OK | `auth/guard.py` `_load_user_and_guard` ; `auth/modes.py` | `test_auth_modes.py::test_password_mode_without_session_closes_every_route_and_creates_no_local_account` | — |
| EF-0.1 `password` explicite, incompatible avec `OIDC_ISSUER` | OK | `auth/checks.py` `_auth_mode` | `test_auth_modes.py::test_password_mode_is_never_deduced` | — |
| EF-0.2 démarrage refusé sans garanties, cookie `Secure` | OK | `auth/checks.py` `_check_password_mode` | `test_auth_modes.py::test_password_mode_refuses_to_start_without_a_guarantee` | avertissement « SMTP absent » (`__init__.py`) non testé |
| EF-0.3 amorçage du premier admin | OK | `cli.py` `user_invite` ; `auth/invitations.py` `accept` ; `auth/mfa_login.py` `_finish_enrollment` | `test_second_factor.py::test_the_bootstrap_ends_with_an_admin_having_one_factor_and_recovery_codes_and_no_session` | — |
| EF-0.3b reprise `--account` / `--reset-password` | OK, écart mineur | `auth/invitations.py` `confirm_bootstrap`, `reset_bootstrap` ; `auth/token_routes.py` | `test_t11_edge.py::test_resuming_a_bootstrap_for_an_existing_account_asks_for_the_current_password_and_keeps_the_hash` ; `test_doc3_edge.py` | compte refusé à l'émission (décision n° 47) ; réponse à la consommation acceptée par l'utilisateur, spec alignée : **E-9** |
| EF-0.4 mode `oidc` sans `SECRETS_KEY` ou `PUBLIC_URL` | Écart, à corriger (CA-61) | `auth/checks.py` `PASSWORD_ACCOUNTS` ; `api/admin_routes.py` `admin_secret_put` | `test_auth_modes.py::test_oidc_mode_enables_password_accounts_only_with_secrets_key_and_public_url` | saisie des secrets : **E-10** |
| EF-0.5 conversion SSO → mot de passe | OK (spec alignée, E-11) | `api/admin_routes.py` `admin_convert` ; `auth/invitations.py` `convert_account` | `test_admin_api.py::test_converting_an_sso_account_rewrites_it_in_one_transaction_and_demotes` | l'adresse passée en attente n'est signalée que dans l'audit et la liste : **E-11** |
| EF-0.6 sessions d'une méthode non permise refusées | OK | `auth/modes.py` `method_permitted` | `test_auth_modes.py::test_switching_from_oidc_to_password_invalidates_sso_sessions` | — |
| EF-1.1 création sur jeton seulement | OK | `auth/invitations.py` `accept` | `test_invitations_edge.py::test_no_anonymous_request_to_any_route_ever_creates_an_account` | — |
| EF-1.2 identifiant, unicité, collisions venues de l'IdP | Écart, à corriger (CA-62) | `auth/identifiers.py` ; `auth/users.py` `_flag_email_collision` | `test_invitations.py::test_a_taken_identifier_is_refused_case_insensitively_among_all_accounts_and_keeps_the_link` | collision d'**identifiant** : bandeau seulement, ni `WARNING` ni audit : **E-12** |
| EF-1.2b identifiants réservés | OK | `auth/identifiers.py` `RESERVED_IDENTIFIERS` | `test_invitations_edge.py::test_reserved_names_in_any_spelling_give_the_exact_answer_of_a_taken_name` | — |
| EF-1.3 `sub = pwd:<uuid4>` | OK | `auth/invitations.py` ; `auth/users.py` | `test_auth_modes.py::test_oidc_login_never_designates_a_password_namespace_sub` | — |
| EF-1.4 / 1.4b politique et liste de 46 187 mots de passe courants | OK | `auth/passwords.py` ; `auth/wordlists/` (SHA-256 conforme) | `test_common_passwords.py::test_delivered_file_has_the_expected_size_and_hash` | — |
| EF-1.5 argon2id, sémaphore de 2 places, limiteur avant hachage | OK | `auth/passwords.py` `HashGate` | `test_passwords.py::test_at_most_two_concurrent_hashes_even_under_load` | — |
| EF-1.6 connexion en deux étapes, `mfa_pending` serveur (5 min, 10 000 états) | OK | `auth/password_login.py` ; `auth/pending.py` | `test_password_login.py::test_replaying_a_cookie_does_not_reset_the_attempt_counter` | un non-admin sans facteur sous la politique `admins` reçoit directement une session complète : conforme à §4 |
| EF-1.7 changement de mot de passe | OK | `api/account_routes.py` `change_password` | `test_t11_edge.py::test_changing_the_password_needs_the_current_one_and_a_factor` | — |
| EF-1.8 réinitialisation par un admin | Écart, à corriger (CA-56 ; correction en cours chez dev) | `api/admin_routes.py` `RESET_LINK_TTL`, `admin_reset_link` | `test_t14_edge.py::test_the_reset_link_is_shown_once_single_use_one_hour_and_never_sent_to_the_admin_session` | lien valable **1 h** et **non envoyé** par mail : **E-1** |
| EF-1.9 mot de passe oublié | OK | `auth/password_login.py` `_forgot_available`, `password_forgot` | `test_password_login.py::test_forgot_work_runs_only_in_the_queue_thread_never_in_the_request` | sans SMTP : §6, décision D-5 |
| EF-1.10 SSO sans mot de passe, `password` sans OIDC | OK | `auth/password_login.py` ; `auth/users.py` | `test_password_login_edge.py::test_an_sso_or_none_account_never_logs_in_by_password_even_with_a_valid_hash_in_base_EF110` | — |
| EF-1.11 routes publiques | OK | `auth/guard.py` `PUBLIC_ENDPOINTS` | `test_auth_modes_edge.py::test_the_list_of_public_endpoints_is_exactly_the_expected_one` | — |
| EF-1.12 un jeton n'ouvre jamais de session complète | OK | `auth/token_routes.py` | `test_password_login_edge.py::test_consuming_an_invitation_opens_no_session_and_the_next_login_follows_the_policy_CA45` | — |
| EF-2.1 TOTP et jusqu'à 10 clés | OK | `auth/second_factor.py` ; `auth/webauthn_factor.py` `MAX_KEYS` | `test_webauthn.py::test_at_most_ten_keys_and_a_duplicate_credential_is_refused` | — |
| EF-2.2 invariant « admin = au moins un facteur » ; `mfa_policy=all` | OK (E-2 corrigé, `6faa266`) | `auth/users.py` `set_admin` ; `auth/guard.py` `admin_required` ; `settings/store.py` `_check_mfa_policy` | `test_second_factor.py::test_the_policy_all_is_refused_while_an_active_password_account_has_no_factor_CA_13b` | contrôle hors de la transaction d'écriture : **E-2** |
| EF-2.3 TOTP anti-rejeu atomique, secret chiffré | OK | `auth/second_factor.py` `verify_totp` | `test_second_factor.py::test_two_simultaneous_submissions_of_the_same_code_let_exactly_one_through` | — |
| EF-2.4 WebAuthn (RP ID, origine, défi serveur, compteur) | OK ; EF-2.4b à faire (CA-59) | `auth/webauthn_factor.py` `rp_id`, `expected_origin`, `verify_authentication` ; `auth/pending.py` `take_challenge` | `test_webauthn.py::test_origin_rp_id_and_challenge_mismatches_are_refused_and_count_as_tries_CA_11` | HTTPS exigé : décision D-6 ; suffixes publics en liste courte : **E-13** |
| EF-2.5 codes de secours | OK, écarts à corriger (CA-57, CA-58) | `auth/second_factor.py` ; `api/mfa_routes.py` `mfa_recovery_codes` | `test_t11_edge.py::test_concurrent_use_of_one_recovery_code_lets_exactly_one_through` | notification et régénération : **E-3** |
| EF-2.6 5 essais par état, limiteur par compte | OK (MFA-1 non commité) | `auth/pending.py` ; `auth/mfa_limit.py` | `test_mfa_limit_edge.py::test_exactly_five_failures_block_the_account_and_the_block_ends_with_the_window` | échecs seuls : décision D-8 |
| EF-2.7 perte de tous les facteurs, `user-mfa-reset` | OK | `api/admin_routes.py` `admin_mfa_reset` ; `cli.py` `user_mfa_reset` | `test_auth_modes.py::test_user_mfa_reset_removes_factors_closes_sessions_demotes_and_warns_when_no_admin_is_left` | — |
| EF-2.8 JS WebAuthn sans bibliothèque externe | OK | `static/js/webauthn.js` | `test_webauthn.py::test_the_front_script_is_served_by_the_application_without_external_library_EF_2_8` | — |
| EF-3.1 à EF-3.11 invitations et crédits | OK | `auth/invitations.py` ; `auth/tokens.py` ; `api/invitations_routes.py` | `test_invitations.py::test_two_concurrent_creations_with_one_credit_succeed_once` ; `test_tokens.py::test_unknown_expired_used_revoked_and_malformed_tokens_are_indistinguishable` | expiration ⇒ crédit non rendu : pas de test (le code ne rend jamais de crédit) |

### 2.2 E-mail, secrets, réglages, bêta, utilisateurs, audit, ordonnanceur (§5.4 à §5.10)

| Exigence | Statut | Code | Test principal | Remarque |
|---|---|---|---|---|
| EF-4.1 SMTP (STARTTLS sans repli, `none` sans authentification) | OK | `settings/store.py` `_check_smtp` ; `mailer.py` `deliver` | `test_mailer.py::test_starttls_that_the_server_cannot_do_fails_and_nothing_is_ever_sent_in_clear` | — |
| EF-4.2 e-mail de test ok/ko, durée = `smtp.timeout_s` | OK (spec alignée, E-14) | `api/admin_routes.py` `admin_mail_test` ; `outbound.py` `run_neutral_test` | `test_mailer.py::test_the_test_mail_answers_ok_or_ko_only_and_always_after_the_smtp_timeout` | tentative abandonnée, pas interrompue : **E-14** |
| EF-4.3 / 4.3b validation par aller-retour, session du compte cible | OK, écart mineur | `auth/token_routes.py` ; `auth/invitations.py` `verify_email` | `test_invitations.py::test_verify_email_over_http_needs_the_session_of_the_target_account` | renvoi non limité à 3 / h : **E-15** |
| EF-4.4 `pending_email` jamais utilisée | OK (test partiel) | `auth/users.py` `match_login` ; `services/inbox_watcher.py` | `test_invitations.py::test_match_login_compares_canonical_addresses_without_sql_lower` | aucun test dédié à `pending_email` ignorée |
| EF-4.5 adresse non validée : bandeau avec renvoi | Non livré, à faire (CA-60) | `templates/profile.html` (simple mention) | — | **E-15** |
| EF-4.6 changement d'adresse | OK | `api/account_routes.py` `change_email` | `test_invitations.py::test_an_address_already_validated_on_another_account_is_refused_neutrally_and_the_pending_one_is_cleared` | — |
| EF-4.7 / 4.9 textes, liens sur `PUBLIC_URL`, anti-injection | OK | `mailer.py` | `test_password_login_edge.py::test_forgot_link_uses_public_url_never_the_request_headers` ; `test_mailer_edge.py::test_hostile_recipients_are_refused_before_any_connection` | — |
| EF-4.8 file d'envoi bornée, CLI synchrone | OK, à corriger (journal de la perte, CA-66) | `mailer.py` `MailQueue` ; `cli.py` `_notify` | `test_mailer.py::test_user_mfa_reset_with_a_dead_smtp_is_done_and_traced_but_exits_non_zero` | perte de la file au redémarrage non journalisée : **E-16** |
| EF-4.10 adresses canoniques, migration `0014` | OK | `db.py` `_emails_canonical` | `test_emails_edge.py::test_the_migration_is_idempotent_and_a_second_pass_changes_and_audits_nothing` | — |
| EF-5.1 à 5.4 secrets chiffrés, écriture seule | OK | `secrets_store.py` | `test_secrets_store.py::test_status_shows_no_value_and_last4_only_for_api_keys` | `last4` absent sous 12 caractères (prudent) |
| EF-5.5 liaison secret ↔ destination | OK (spec alignée : secrets en base seulement, E-17) | `secrets_store.py` ; `settings/store.py` | `test_outbound.py::test_changing_the_destination_clears_the_linked_secret_in_the_same_transaction` | aucun secret ne peut venir de l'env : **E-17** |
| EF-5.6 « Tester » par service | OK (DEM hors v1, E-18) | `api/admin_routes.py` `admin_secret_test` ; `services/routing/runtime.py` `neutral_test` | `test_route_builder_api.py::test_the_engine_test_of_the_admin_calls_the_health_probe_and_answers_ok_or_ko_only` | moteur et SMTP livrés (l'écart « Tester » le moteur de la recette v3 est corrigé) ; **DEM non livré** : **E-18** |
| EF-5.7 rotation et remise à zéro des clés | OK | `secrets_store.py` ; `cli.py` | `test_secrets_store.py::test_rotation_reencrypts_with_the_first_key_so_the_old_one_can_be_removed` | — |
| EF-6.1 à 6.3c registre borné, base > env > défaut, diagnostic, CLI | OK | `settings/registry.py` ; `settings/store.py` | `test_settings.py::test_every_registry_entry_has_explicit_bounds` ; `::test_write_is_validated_and_the_batch_is_all_or_nothing` | — |
| EF-6.4 version des réglages, rechargement par requête et par tour | OK (P-T14-6 alignée, E-19) | `settings/store.py` ; `settings/__init__.py` ; `background.py` `settings_reload` | `test_settings.py::test_next_request_sees_a_change_made_by_another_process` | la réponse du PATCH ne contient pas la version (P-T14-6) : **E-19** |
| EF-6.5 / 6.6 périmètre, hors registre | OK | `settings/registry.py` | `test_t16.py::test_each_setting_has_the_documented_bounds_and_refuses_values_beyond_them` | — |
| EF-6.8 liste blanche par service, CSP `img-src` | OK, écart mineur | `outbound.py` ; `security.py` ; `auth/checks.py` | `test_outbound.py::test_an_entry_authorizes_its_own_service_only_and_the_exact_port` ; `::test_csp_img_src_follows_the_tiles_entries` | diagnostic des destinations hors liste partiel : **E-20** |
| EF-7.1 à 7.4 fonctions bêta | OK | `features.py` ; `api/admin_routes.py` | `test_tokens_features_edge.py::test_ca22_off_users_without_the_user_users_with_the_user_and_all` | — |
| EF-8.1 à 8.4 utilisateurs, dernier admin, actions | OK | `api/users_routes.py` ; `api/admin_routes.py` | `test_admin_api.py::test_the_last_active_admin_is_protected_and_a_non_last_admin_can_be_disabled` | — |
| EF-8.5 bandeau « collision d'identité » | OK (API), UI partielle | `api/users_routes.py` `identity_collisions` ; `static/js/admin.js` | `test_admin_api.py::test_identity_collisions_are_reported_case_insensitively` | numéros de comptes sans lien : **E-21** |
| EF-9.1 `actor_type` et contrainte CHECK | OK | `db.py` ; `audit.py` | `test_audit.py::test_database_refuses_a_cli_row_with_an_account_and_a_user_row_without` | libellés d'acteur bruts dans l'UI : **E-21** |
| EF-9.2 actions tracées | OK (E-4 corrigé, `6faa266`) | `api/users_routes.py` `update_user`, `delete_user` | `test_audit.py::test_cli_and_system_actions_are_typed_with_no_account_and_an_empty_label` | activation, désactivation et suppression de compte **non auditées** : **E-4** |
| EF-9.3 consultation et purge | OK | `audit.py` ; `background.py` | `test_admin_api.py::test_the_audit_is_paginated_filtered_and_bounded` | — |
| EF-10.1 / 10.1b démarrage, verrou des tâches partagées | OK ; diagnostic non exposé | `background.py` | `test_background.py::test_without_the_lock_only_process_tasks_run_and_the_lock_is_retried_every_tick` | `Scheduler.status()` n'est appelé par aucune route : **E-22** |
| EF-10.2 à 10.4 tâches, `wake`, thread long, `tick(now)` | OK (E-5 corrigé, `6faa266`) | `background.py` ; `services/routing/runtime.py` `init_routing` | `test_background.py::test_a_blocked_long_task_never_freezes_the_short_ones_nor_gets_a_second_instance` | `routing_health` déclarée **longue** : **E-5** |
| EF-10.5 thread de la file d'envoi | OK | `mailer.py` | `test_mailer.py::test_commands_do_not_start_a_queue_thread` | — |

### 2.3 Sécurité (§6)

| Exigence | Statut | Code | Test principal | Remarque |
|---|---|---|---|---|
| EF-S1 `admin_required` sur toutes les routes admin | OK | `api/admin_routes.py` ; `api/users_routes.py` | `test_t14_edge.py` (paramétré sur `url_map`) | — |
| EF-S2 POST de même origine + jeton CSRF | OK | `auth/csrf.py` ; `auth/password_login.py` | `test_invitations_edge.py::test_a_cross_origin_post_is_refused_even_with_a_valid_csrf_token` | — |
| EF-S3 limiteur à deux compteurs, tailles fixées | OK | `ratelimit.py` | `test_ratelimit.py::test_never_more_than_limit_per_fixed_window_nor_twice_per_sliding_window` ; `::test_default_sizes` | — |
| EF-S4 comparaisons en temps constant | OK | `secrets_store.py` ; `ratelimit.py` | `test_tokens.py::test_constant_time_match_helper` | codes de secours comparés par égalité SQL sur l'empreinte (sans effet : l'empreinte est hachée) |
| EF-S5 journaux sans secret | OK | `mailer.py` ; routes | `test_mailer_edge.py::test_no_link_token_or_password_in_logs_audit_exceptions_or_reprs_on_any_failure_path` | — |
| EF-S6 `no-store` | OK | `security.py` | `test_t14_edge.py` | une assertion neutralisée par `or True` dans `test_admin_api.py` (§7) |
| EF-S7 `PUBLIC_URL` seule source des liens | OK | `auth/tokens.py` `link` | `test_invitations.py::test_api_link_is_built_on_public_url_never_on_the_host_header` | — |
| EF-S8 jeton dans le fragment, `inspect` neutre, aucun GET ne consomme | OK (test partiel) | `auth/token_routes.py` | `test_invitations.py::test_no_token_ever_reaches_the_application_or_access_log` | pas de capture du flux gunicorn (CA-5, §7) |
| EF-S9 état et défis côté serveur | OK | `auth/pending.py` | `test_password_login.py::test_replaying_a_cookie_does_not_reset_the_attempt_counter` | — |
| EF-S10 destinations sortantes | OK sauf « Tester » DEM | `outbound.py` | voir EF-6.8 | **E-18** |

### 2.4 Interface (§9)

| Point | Statut | Remarque |
|---|---|---|
| Onglets d'administration | Écart (accepté de fait) | 6 onglets : « Clés API » et « E-mail » fusionnés en « Clés API et e-mail » (`templates/admin.html`). Sans effet fonctionnel |
| Bandeaux « SMTP absent », « destination hors liste » | Non livré | seul le bandeau de collision existe : **E-21** |
| Réglages : source, surcharge d'env, retour au défaut, avertissement sur le secret | Partiel | la source est affichée ; le marqueur de surcharge ne l'est pas ; l'avertissement « le secret sera effacé » est une mention fixe ; hôtes de tuiles autorisés non affichés : **E-21** |
| **Page Réglages refondue (§5.6b, 25/11)** : sous-onglets par section, compteurs, recherche, filtre « modifiées », lignes compactes, éditeurs structurés (fonds de carte, rectangle — retiré par le lot Z1 —, listes), volet « Avancé », enregistrement par onglet avec confirmation avant / après, erreurs sous le champ, `requires_restart` | Non livré (nouvelle exigence) | formulaire générique actuel : un seul formulaire, JSON brut pour `basemaps` et `rectangle` (`static/js/admin.js`, `CONTROL_FOR_TYPE`). API à étendre : `sections`, `unit`, `fallback`, `fallback_source`, `env_invalid`, `restart_pending`, `allowlist` des tuiles ; `field` et `index` dans les 400 ; `version` au `PATCH` (409 `settings_version_conflict`). Registre : `SECTIONS`, `unit`. Banc de test navigateur `web-app/tests_browser/` (EF-6.17). CA-67 à CA-80 |
| Diagnostic des tâches de fond | Non livré | **E-22** |
| Connexion en mode `oidc` : SSO et formulaire sur la même page | Écart | `/auth/login` redirige directement vers l'IdP ; le formulaire n'est joignable que par `/auth/password/login`. **Devient utile** : l'utilisateur utilisera le mode `oidc` avec des comptes à mot de passe (décision du 25/11) ; non tranché : **E-21** |
| Liens et codes affichés une fois, bouton « Copier » | OK | `static/js/admin.js` ; `templates/profile.html` |

---

## 3. admin-space : critères d'acceptation

| CA | Statut | Test principal | Remarque |
|---|---|---|---|
| CA-1 | OK | `test_invitations_edge.py::test_no_anonymous_request_to_any_route_ever_creates_an_account` | — |
| CA-2, CA-3 | OK | `test_invitations.py::test_two_concurrent_creations_with_one_credit_succeed_once` ; `::test_an_admin_creates_many_invitations_without_touching_credits` | — |
| CA-4 | OK (CA aligné sur le code, E-23) | `test_invitations_edge.py::test_a_member_cannot_raise_their_own_credits_through_the_invitation_api` | non-admin envoyant des crédits par `/api/invitations` ⇒ **400**, pas 403 : **E-23** |
| CA-5 | OK (test partiel) | `test_invitations_edge.py::test_the_whole_flow_leaves_no_token_or_password_in_logs_audit_responses_or_the_database` | parcours d'invitation seulement ; pas de capture gunicorn |
| CA-6 | OK (test partiel) | `test_invitations.py::test_revocation_by_the_owner_or_an_admin_only_and_the_credit_is_not_returned` | expiration non testée |
| CA-7 | OK | `test_password_login_edge.py::test_the_sixth_attempt_on_the_same_identifier_is_refused_and_the_window_slides` | — |
| CA-8, CA-8b | OK | `test_passwords.py::test_common_passwords_are_refused_after_nfkc_and_casefold` ; `test_common_passwords.py` | — |
| CA-9 | OK | `test_password_login_edge.py::test_the_pending_state_expires_server_side_whatever_the_cookie_says` | — |
| CA-10 | OK | `test_second_factor.py::test_two_simultaneous_submissions_of_the_same_code_let_exactly_one_through` | — |
| CA-11 | OK | `test_webauthn.py` (tests `…_CA_11`) | — |
| CA-12 | OK | `test_second_factor.py::test_recovery_codes_are_ten_unique_formatted_hashed_and_single_use` | — |
| CA-13 | OK | `test_auth_modes.py::test_every_admin_route_refuses_a_password_admin_without_a_second_factor` | — |
| CA-13b | OK (test partiel), écart mineur | `test_second_factor.py::test_a_batch_containing_the_refused_policy_is_rejected_as_a_whole` | « version inchangée » non vérifiée par un test ; **E-2** |
| CA-14 | OK | `test_password_login_edge.py::test_a_cookie_issued_before_a_password_or_factor_change_is_refused_CA14` | — |
| CA-15 | OK | `test_password_login.py::test_forgot_answers_identically_for_every_kind_of_account_and_sends_one_mail_to_the_eligible_one` | — |
| CA-16 | OK (test partiel) | `test_invitations.py::test_who_can_invite` | voir EF-4.4 |
| CA-17, CA-17b | OK | `test_invitations.py::test_accepting_an_invitation_creates_an_active_password_account_with_a_pending_address` ; `::test_verify_email_over_http_needs_the_session_of_the_target_account` | — |
| CA-18, CA-19, CA-20 | OK | `test_mailer_edge.py::test_hostile_recipients_are_refused_before_any_connection` ; `test_secrets_store.py::test_application_refuses_to_start_when_a_value_cannot_be_decrypted` | — |
| CA-21 à CA-21d | OK | `test_settings.py` ; `test_background_edge.py::test_interval_and_enabled_settings_are_read_at_every_tick_ca21d` | — |
| CA-22, CA-23 | OK | `test_tokens_features_edge.py::test_ca22_off_users_without_the_user_users_with_the_user_and_all` ; `test_t14_edge.py` | — |
| CA-24 | OK (E-4 corrigé) | `test_audit.py` | actions de compte non auditées : **E-4** |
| CA-24b | OK | `test_audit.py::test_an_sso_account_named_cli_stays_a_user_and_is_not_found_by_the_cli_filter` | — |
| CA-25 | OK (test partiel) | `test_password_login.py::test_the_pages_and_endpoints_do_not_exist_outside_password_mode` | `/auth/password/mfa*`, `/enroll*`, `/auth/token/consume`, `/api/me/mfa` non couverts en mode `local` (le code répond 404) |
| CA-26, CA-47 | OK | `test_migrations_edge.py::test_v5_database_keeps_its_data` ; `test_migrations.py::test_reverse_order_applies_only_the_missing_one` | — |
| CA-27 | OK | `test_webauthn.py::test_a_key_keeps_an_admin_an_admin_after_secrets_reset_CA_27` | — |
| CA-28, CA-29 | OK | `test_auth_modes.py::test_switching_from_oidc_to_password_invalidates_sso_sessions` | — |
| CA-30 | OK (test partiel) | `test_t14_edge.py::test_consuming_the_conversion_link_rewrites_the_account_in_one_step_and_logs_nobody_in` | reconnexion OIDC avec l'ancien `sub` non testée (garantie par le code) |
| CA-31 | Écart | `test_invitations.py::test_an_sso_address_taken_from_another_account_is_tolerated_but_flagged` | **E-12** |
| CA-32, CA-33 | OK | `test_ratelimit.py::test_ip_flood_cannot_erase_a_targeted_identifier_counter` ; `test_t11_edge.py::test_bootstrap_makes_a_non_admin_account_in_enrollment_then_the_first_factor_grants_admin_in_one_step` | — |
| CA-34, CA-35, CA-36 | OK | `test_mailer_edge.py::test_a_destination_forced_by_raw_sql_never_receives_the_password_bound_to_the_old_one` ; `test_outbound.py::test_a_url_forced_in_the_database_outside_the_list_disables_the_function_and_makes_no_call` | — |
| CA-37 | OK (DEM hors v1, CA aligné, E-18) | `test_outbound_edge.py::test_every_kind_of_probe_takes_the_service_timeout_within_ten_percent` | **E-18** |
| CA-38, CA-39 | OK | `test_secrets_store.py::test_secrets_rotate_command` ; `test_invitations.py::test_an_admin_gets_409_for_a_taken_address_a_non_admin_gets_the_same_answer_as_for_a_free_one` | — |
| CA-40 | OK (test unitaire) | `test_passwords.py::test_at_most_two_concurrent_hashes_even_under_load` | pas de test par 10 connexions HTTP |
| CA-41, CA-42 | OK | `test_t16.py::test_a_basemap_forced_in_the_database_outside_the_list_is_not_sent_to_the_browser_and_is_audited` ; `test_outbound.py::test_malformed_entries_refuse_the_startup` | — |
| CA-43, CA-48, CA-51 | OK (E-5 corrigé ; le test vise désormais la tâche réellement enregistrée) | `test_background.py::test_shared_tasks_of_the_second_scheduler_resume_when_the_first_stops` | CA-51 utilise un faux `routing_health` **court**, ce qui ne correspond pas à la configuration réelle |
| CA-44, CA-50, CA-55 | OK | `test_invitations.py::test_two_concurrent_validations_of_the_same_address_succeed_once` ; `test_admin_api.py::test_the_address_moves_to_pending_when_a_password_account_already_holds_it` ; `test_invitations.py::test_migration_0014_never_fails_on_collisions_and_flags_them` | — |
| CA-45, CA-46 | OK | `test_password_login_edge.py::test_consuming_an_invitation_opens_no_session_and_the_next_login_follows_the_policy_CA45` ; `::test_twenty_simultaneous_requests_over_the_identifier_limit_never_touch_the_hash_gate` | — |
| CA-49, CA-52, CA-54 | OK | `test_mailer.py::test_user_mfa_reset_notifies_the_holder_before_returning` ; `test_tokens.py::test_user_invite_account_refuses_sso_accounts_accounts_with_a_factor_and_ambiguous_logins` ; `test_t11_edge.py::test_the_password_check_of_a_resumed_bootstrap_is_rate_limited_per_target_account_before_hashing` | — |
| CA-67 à CA-80 | Non livré (nouveau) | refonte de la page Réglages (§5.6b), demandée par l'utilisateur en recette le 25/11 ; CA-72 à CA-78 vérifiés par le banc de test navigateur local (EF-6.17, CA-80), recette manuelle en complément (§7) |
| CA-82, ~~CA-83~~ | Non livré (nouveau) ; CA-83 supprimé par le lot Z1 (26/11) | second retour de l'utilisateur sur la page Réglages (25/11) : lisibilité (EF-6.18) et rectangle édité sur la carte (EF-6.19) ; CA-72 à CA-74 mis à jour (pastilles) ; vérifiés par le banc local (EF-6.17) |
| CA-84 | Non livré (dev en cours) | onglet Invitations de l'admin : « Émise par », statuts en français (« Acceptée » aussi dans le profil), compte créé (EF-3.13, demande de coord du 25/11) |
| CA-85, CA-86, CA-87 | Non livré (nouveau ; CA-85 et CA-86 réécrits, CA-87 ajouté par le lot Z1, 26/11) | zone couverte en polygone (EF-6.20, EF-6.21) : demande de l'utilisateur, décision de coord du 25/11 (polygone libre) ; lot Z1 : un seul réglage, défaut = 4 sommets, migration `0017_coverage_polygon_only` (CA-87) ; CA-86 vérifié par le banc local (EF-6.17) |
| CA-56 à CA-66b | Non livré (à corriger) | critères ajoutés le 25/11 : CA-56 à CA-62 (décisions de l'utilisateur : E-1, E-3, E-13, E-15, E-10, E-12), CA-63 à CA-66 (décisions de coord : E-21, E-20, E-16) ; CA-66b (arrêt réel sous gunicorn) et précisions de CA-60 (compte SSO ⇒ 404) et CA-61 (liste admin des invitations ⇒ 404) ajoutées après les écarts du groupe 3a (test, décisions de coord) |

---

## 4. gpx-route-builder

Abréviations : `RBR` = `api/route_builder_routes.py` ; `B` = `services/routing/builder.py` ; `RT` = `services/routing/runtime.py` ; `RS` = `services/routing/results.py` ; `GH` = `services/routing/graphhopper.py` ; `JS` = `static/js/viewer/route-builder.js`.

### 4.1 Exigences

| Exigence | Statut | Code | Test principal | Remarque |
|---|---|---|---|---|
| RB-1.1 bêta, 404, bouton absent | OK | `features.py` ; `RBR` ; `templates/viewer/toolbar.html` | `test_route_builder_api.py::test_the_feature_off_or_not_listed_or_an_unconfigured_engine_answers_404_everywhere` | — |
| RB-1.2 non configuré (404) / injoignable (503) / cache vide | OK | `RT` `configured`, `state` | `test_route_builder_api_edge.py::test_a_cold_cache_is_starting_options_stay_visible_and_route_is_503_without_touching_the_engine_CA_R29` | diagnostic admin affiché en JSON brut, `checked_at` en secondes epoch : **E-21** |
| RB-1.2 tâche `routing_health` (au démarrage, puis toutes les 60 s) | OK (E-5 corrigé, `6faa266`) | `RT` `init_routing` ; `background.py` | `test_route_builder_api.py::test_the_probe_follows_the_engine_and_the_task_is_scheduled_without_inbox_or_dem_CA_R24_R29` | **E-5** |
| RB-1.3 zone de couverture, défaut `[1.35, 48.83, 6.42, 51.55]` (4 sommets depuis Z1), 400 avant tout appel | OK pour le rectangle ; **à reprendre (lot Z1, 26/11)** | `settings/registry.py` ; `B` ; `services/routing/zone.py` | `test_route_builder_api.py::test_london_and_amsterdam_are_refused_and_lille_and_liege_accepted_CA_R20b` | décision D-2 ; Z1 : `routing.coverage_bbox` retiré du registre, défaut du polygone = 4 sommets, `[]` refusé, migration (admin-space CA-87) ; tests qui écrivent `routing.coverage_bbox` à réécrire |
| RB-1.3 422 pour un point mal accroché | OK (spec alignée, §8) | `RBR` `_error` ; `GH` | `test_route_builder_api.py::test_the_422_message_is_the_specified_one_with_the_original_number_and_no_forbidden_word_CA_R19_R20` | « hors données » et « trop loin d'une voie » donnent le même message (le moteur ne les distingue pas) |
| RB-1.3 diagnostic « zone non contenue » | OK ; message à reprendre (Z1 : il cite `routing.coverage_bbox`, `static/js/admin.js`) | `RT` | `test_route_builder_api_edge.py::test_the_admin_diagnostic_reports_the_engine_state_and_the_coverage_containment` | — |
| RB-1.3b zone couverte polygonale (25/11) | Non livré (nouveau) | `B`, `RT`, `RBR`, `JS` | — | `routing.coverage_polygon` (rectangle en repli jusqu'au lot Z1, qui le supprime) ; appartenance calculée dans la projection de la carte, identique front / serveur ; points techniques S1 ramenés dans la zone ; message 400 inchangé ; CA-R49 à CA-R51 |
| RB-2.1 entrée « Créer une trace », un seul mode actif | Écart, à corriger (CA-R35) | `JS` `open` ; `static/js/viewer/editor.js` | — | l'éditeur de points et le constructeur peuvent être ouverts ensemble : **E-6** |
| RB-2.2 à RB-2.5 édition, annuler/rétablir, « Tout effacer » confirmé, prévisualisation | Non testé (test statique) | `JS` | `test_route_builder_api.py::test_the_builder_front_covers_rb_2_2_undo_keys_insert_before_touch_removal_and_in_page_confirmation` | corrigé depuis la recette v3 ; test par recherche de chaînes seulement |
| RB-2.6 points en `localStorage`, effacés à la déconnexion, par compte et après un enregistrement réussi | Écart partiel, à corriger (CA-R36, CA-R40) | `JS` `wipeOnLogout` ; `static/js/common.js` | `test_route_builder_api.py::test_a_401_clears_the_builder_points_stored_in_the_browser_TR6_n1` | effacement à la déconnexion depuis le visualiseur seulement : **E-7** |
| RB-2.7 modifier les points sur le résultat affiché ⇒ régénération (25/11) | Non livré (nouveau) | `JS` | — | déplacer, insérer (appui sur la trace, déplaçable jusqu'au lâcher), ajouter (clic hors trace, après Pn) et retirer régénèrent ; « Tout effacer » revient à l'édition ; mêmes activité, distance voulue (conservée) et graine ; résultat précédent gardé jusqu'au succès, retour à l'état précédent en cas d'échec ; historique avec résultats ; clavier ; une génération à la fois ; après un enregistrement réussi, constructeur à neuf, et une trace enregistrée ne se retouche que par « Éditer la trace » (CA-R40 prévaut, CA-R48 réécrit, décision de coord du 25/11) (décisions de coord et de l'utilisateur, 25/11, qui remplacent « ajout ou retrait ⇒ retour à l'édition ») ; CA-R41 à CA-R48 [banc] |
| RB-3.1 à RB-3.3 activités, distance, validation serveur | OK | `services/routing/profiles.py` ; `B` | `test_graphhopper.py::test_the_activity_table_matches_the_spec` ; `test_route_builder_api_edge.py::test_a_huge_integer_distance_is_a_400_never_a_500` | — |
| RB-4.1 à RB-4.4 boucle, tolérance, détours | OK | `B` ; `services/routing/detours.py` | `test_route_builder.py::test_detours_converge_into_the_tolerance_within_four_iterations` | — |
| RB-4.5 « Autre proposition » sans doublon | OK, avec limites | `RBR` ; `B` ; `JS` | `test_route_builder_api_edge.py::test_a_chain_of_other_proposals_never_repeats_the_previous_one_and_mostly_gives_new_ones` | décision D-9 ; sans autre trace dans le budget, la même revient avec l'avertissement `no_alternative` |
| RB-4.6 budget de 15 appels et 30 s | OK | `services/routing/detours.py` | `test_route_builder.py::test_never_more_than_the_total_time_budget_with_a_slow_provider` | — |
| RB-4.7 contrôles qualité | OK à partir de 2 points ; 1 point à corriger (CA-R38) | `services/routing/quality.py` ; `B` | `test_route_builder.py::test_the_trace_is_closed_and_passes_by_every_point_in_order` | à 1 point, ni accroche ni fermeture contrôlées : **E-24** |
| RB-4.8 GPX, altitude du moteur si complète, MNT à l'enregistrement | OK, écart mineur | `B` ; `GH` ; `RBR` (`defer_elevation=True`, `wake('dem_backfill')`) | `test_route_builder.py::test_engine_elevation_is_kept_only_when_complete` ; `test_route_builder_api_edge.py::test_the_elevation_is_scheduled_at_save_time_through_the_dem_task` | décision D-1 ; une altitude non finie du moteur ⇒ 502 pour toute la génération : **E-25** |
| RB-4.9 `route_meta` | OK | `B` ; `RBR` | `test_route_builder_api_edge.py::test_the_response_has_the_viewer_payload_plus_route_meta_with_stable_codes` | codes `no_alternative` et `user_point_unreachable` ajoutés à la liste de P-TR5-4 |
| RB-4.10 progression et « Annuler » | Non testé | `JS` | — | — |
| RB-4.11 à RB-4.14 hybride, essais de graine, seuils 25 / 40, suggestion d'ajouter un point | OK | `services/routing/detours.py` ; `B` | `test_route_builder.py::test_hybrid_falls_back_to_a_petal_when_lateral_points_do_not_reach_the_tolerance_CA_R30` ; `::test_overlap_threshold_depends_on_the_number_of_points` | — |
| RB-5.1 enregistrement, nom écrit par l'API XML | OK | `RBR` ; `B` | `test_route_builder_api.py::test_save_default_name_is_dated_and_a_hostile_name_is_escaped_not_injected_CA_R25` | nom par défaut à l'heure du serveur : **E-26** |
| RB-5.2 mémoire 30 min / 20 / 200 / 256 Mo, 409 avec SHA-256 | OK | `RS` ; `RBR` | `test_route_builder_api.py::test_eight_concurrent_saves_of_one_result_create_exactly_one_trace_TR5_1` | `ResultStore.clear_user` jamais appelé : **E-27** |
| RB-5.3 à RB-5.6 `kind='planned'`, impacts, upload et inbox | OK | `services/ingest_service.py` ; `services/dashboard_service.py` ; `services/activity_store.py` ; `services/segment_service.py` | `test_planned_kind.py::test_a_planned_track_changes_neither_stats_records_types_nor_counter` | — |
| §5.6 réglages `routing.*` | OK (spec alignée : pas de `routing.provider`, E-28) | `settings/registry.py` | `test_route_builder.py::test_config_from_settings_reads_the_registry_keys` | `routing.provider` absent (GraphHopper en dur) : **E-28** ; `routing.profiles` en code (P-TR5-5, décision D-3) |
| §7 codes HTTP | OK sauf 413, à corriger (CA-R37) | `RBR` | `test_route_builder_api.py::test_engine_errors_map_to_fixed_statuses_without_any_internal_detail` | quota atteint ⇒ **503** « Enregistrement impossible » au lieu de 413 : **E-8** |
| RB-S1 à RB-S10 entrées, moteur non fiable, charge, journaux, `result_id` | OK | `B` ; `GH` ; `RT` ; `RS` | `test_route_builder_api_edge.py::test_every_invalid_request_is_a_400_without_any_engine_call_and_is_not_counted` ; `test_routing_tr_edge.py::test_a_gzip_bomb_is_refused_on_the_decoded_size` | clé API jamais en clair sauf `ROUTING_ALLOW_CLEARTEXT_KEY` : décision D-10 |
| RB-S11 moteur : réseau interne, aucun port, conteneur durci, version épinglée | Non testé (ops) | `docker-compose.yml` ; `routing/Dockerfile` | — | vérifié par review (TR7) et par la recette de `test` |

### 4.2 Critères d'acceptation

| CA | Statut | Remarque |
|---|---|---|
| CA-R1 à CA-R15 | OK | tests dans `test_route_builder.py`, `test_route_builder_api.py`, `test_route_builder_api_edge.py`, `test_planned_kind.py` |
| CA-R16 | OK (test partiel) | 429 immédiat testé ; « un upload passe dans `heavy_slot()` pendant 2 générations lentes » **non testé** |
| CA-R17 | OK (statique pour le JS) | — |
| CA-R18, CA-R19 | OK | `test_route_builder.py::test_a_new_strategy_is_added_without_touching_the_builder` ; `test_route_builder_api.py::test_no_user_visible_text_uses_the_word_itineraire_CA_R19` |
| CA-R20, CA-R20b | OK | CA-R20 aligné sur le code (§8) |
| CA-R21 [spike] | Consigné | ADR-0007 : cas transfrontaliers dans la tolérance |
| CA-R22 à CA-R27, CA-R29 à CA-R33 | OK | CA-R27 testé avec un plafond réduit à 4 résultats |
| CA-R4s, CA-R6s [spike] | Consigné | ADR-0010 : S1 87 %, S2 88 %, 0 violation d'ordre ou de fermeture (S1 et S2 mesurés séparément) |
| CA-R28 | Écart partiel | **E-7** ; non rejoué dans un navigateur (recette de `test`, STATUS n° 44) |
| CA-R34 [spike/recette] | **Non mesuré** | ADR-0010 ne donne qu'une borne haute estimée (95 %) pour l'hybride ; à mesurer en recette, ou à accepter tel quel |
| CA-R40 à CA-R48 [banc] | Non livré (nouveau) | retours de l'utilisateur du 25/11 : points restaurés après enregistrement (CA-R40, défaut, dev corrige) ; déplacement d'un point sur le résultat affiché (CA-R41 à CA-R48, RB-2.7 : déplacer, insérer, ajouter, retirer) ; vérifiés par le banc navigateur local (admin-space EF-6.17) |
| CA-R49 à CA-R51 | Non livré (nouveau) | zone couverte polygonale (RB-1.3b, 25/11) ; CA-R50 et la partie front de CA-R51 au banc navigateur local |
| CA-R35 à CA-R39 | Non livré (à corriger) | critères ajoutés le 25/11 (E-6, E-7, E-8 ; E-24, E-26 par coord) ; CA-R35 et CA-R36 demandent un test de comportement dans un navigateur, ou la recette à défaut |

---

## 5. Écarts connus

Gravité : **à arbitrer** = décision de l'utilisateur avant la prod ; **mineur** = correction simple ou acceptation, sans risque de sécurité ; **interface** = présentation seulement.

Décisions de l'utilisateur du 2026-11-25 (relayées par coord), reportées dans les specs le jour même :

| Décision | Écarts | Specs touchées |
|---|---|---|
| **À corriger dans le code** (dev) | E-1, E-3 (a et b), E-6, E-7, E-8, E-10, E-12, E-13, E-15 | EF-1.8, EF-2.4 / EF-2.4b, EF-2.5, EF-4.5, P-T11-7, RB-2.1, RB-2.6, P-TR6-6, §7 (413) ; nouveaux critères CA-56 à CA-62, CA-R35 à CA-R37 |
| **Accepté, spec alignée sur le code** | E-9, E-11, E-14 | EF-0.3b, EF-0.5, EF-4.2, EF-5.6 |
| **Gardé** | D-1 (altitude MNT à l'enregistrement) | — |
| `restart.sh` | remplacé par `docker compose up -d --force-recreate` | doc et exploitation |
| **Corrigé par dev** (commit `6faa266`) | E-2, E-4, E-5 | — |

Décisions de **coord** du 2026-11-25 (délégation de l'utilisateur ; **à relire par l'utilisateur**, comme le §6), reportées le jour même :

| Décision | Écarts / questions | Specs touchées |
|---|---|---|
| **À corriger dans le code** | E-16, E-20 + E-21 (part utile en `oidc` + mot de passe), E-24, E-26 | EF-4.8, §9, P-TR5-7 ; nouveaux CA-63 à CA-66, CA-R38, CA-R39 |
| **Accepté, spec alignée** | E-17 (secrets en base seulement), E-18 (pas de « Tester » DEM), E-19, E-23, E-28 (un seul fournisseur) | EF-5.5, EF-5.6, CA-4, CA-34, CA-37, P-T14-6, gpx §5.6, ADR-0008 (amendement) |
| **Accepté / plus tard** | E-22 (diagnostic de l'ordonnanceur), E-25, E-27, reste du polissage d'E-21 | EF-10.1b, §9 annotés |
| Licence MPL-2.0 de la Public Suffix List | **acceptée** (instantané épinglé, `SOURCE` avec licence et provenance) | EF-2.4b |
| Liste absente ou `WEBAUTHN_RP_ID` invalide | **refus de démarrer** (fermé par défaut) | EF-2.4b, CA-59 |
| Assertion WebAuthn au profil | **seulement** pour retirer une clé et régénérer les codes ; **pas** pour le mot de passe, l'adresse ni le TOTP (une clé prouve la possession, pas la connaissance) | P-T11-7 |
| `localStorage` du constructeur | activité et distance **gardées** avec les points, effacées à toute déconnexion | RB-2.6, P-TR6-6, CA-R36 |

Plus aucun écart sans décision.

Composant tiers sous licence copyleft dans l'image publiée (lot Z2, nit de review SHP-n1, 2026-11-26) :

| Composant | Licence et conditions | Statut | Specs touchées |
|---|---|---|---|
| **GEOS 3.14.1**, embarqué par `shapely==2.2.0` (BSD-3-Clause) dans `shapely.libs/`, donc dans l'image publiée sur GHCR à partir de TZ2-a | **LGPL-2.1**, liaison dynamique, bibliothèque remplaçable ; texte de licence présent dans l'image (`dist-info/licenses/LICENSE_GEOS` de la roue) ; source : dépôt `libgeos/geos`, étiquette `3.14.1` | **compatible** ; mention du composant, de sa licence et du lien vers son source à ajouter au README (doc, TZ2-d) | ADR-0013 point 3, routing-zone-donnees §11 |

### 5.1 À arbitrer avant la prod

| # | Exigence | Écart constaté (code) | Proposition de spec |
|---|---|---|---|
| E-1 | EF-1.8 | Le lien de réinitialisation créé par un admin vaut **1 h** (`RESET_LINK_TTL`) et n'est **pas envoyé** à l'adresse validée : il est seulement montré à l'admin, qui le transmet. Un test valide ce comportement. | **Décidé par l'utilisateur (25/11)** : **24 h et envoi par mail** (EF-1.8 inchangée et précisée) ; sans SMTP, le lien est montré à l'admin. Code à mettre en conformité (en cours chez dev). CA-56. |
| E-2 | EF-2.2, CA-13b | Le refus de `auth.mfa_policy=all` est contrôlé **avant** la transaction d'écriture (`settings/store.py`, `_check_mfa_policy` puis `write_transaction`). Une invitation consommée entre les deux passe. | **Corrigé** (commit `6faa266`, vérifié dans le code par spec) : contrôle **dans** la transaction d'écriture ; test `test_second_factor.py::test_the_policy_check_runs_inside_the_write_transaction_so_a_concurrent_factor_removal_is_seen`. |
| E-3 | EF-2.5, P-T11-7, T12-n2 | (a) Un code de secours utilisé depuis le profil (changement de mot de passe ou d'adresse, retrait de clé) **ne déclenche pas** de notification. (b) Un compte qui n'a **que des clés** WebAuthn ne peut pas régénérer ses codes (TOTP exigé) et doit **brûler un code** pour retirer une clé. Ses codes s'épuisent sans moyen de les renouveler. | **Décidé par l'utilisateur (25/11)** : (a) **à corriger** (notifier sur tous les chemins), CA-57 ; (b) **accepter une assertion WebAuthn** comme preuve au profil, pour le retrait de clé et la régénération des codes (P-T11-7 réécrite), CA-58. |
| E-4 | EF-9.2, CA-24 | Activation, désactivation et suppression d'un compte par un admin **ne sont pas auditées** (`api/users_routes.py` `update_user`, `delete_user` ; aucune action `user.activate` ni `user.delete`). | **Corrigé** (commit `6faa266`, vérifié dans le code par spec) : audits `user.activated`, `user.deactivated`, `user.deleted` ; test `test_admin_api.py::test_activation_deactivation_and_deletion_of_an_account_are_audited_without_secret`. |
| E-5 | EF-10.3 (v5-6), RB-1.2, CA-51 | `routing_health` est enregistrée en tâche **longue** (`runtime.py`, `long=True`). Elle passe donc dans le fil unique des tâches longues, derrière `inbox` et `dem_backfill`. Au démarrage, ou pendant une longue passe d'altitude, le sondage attend : la fonction reste « en cours de démarrage » ou garde un état périmé. Le test CA-51 utilise un faux `routing_health` court et ne le voit pas. | **Corrigé** (commit `6faa266`, vérifié dans le code par spec) : `routing_health` est une tâche **courte** ; test `test_background_edge.py::test_the_registered_routing_health_probe_runs_while_a_long_task_is_blocked` (tâche réellement enregistrée, plus un faux). |
| E-6 | RB-2.1 | L'éditeur de points du visualiseur et le constructeur peuvent être actifs **en même temps**. Un clic sur la carte peut alors ajouter un point de trace **et** ouvrir le formulaire de point. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; RB-2.1 précisée, CA-R35. |

### 5.2 Mineurs

| # | Exigence | Écart | Proposition |
|---|---|---|---|
| E-7 | RB-2.6, CA-R28, P-TR6-6 | Les points en cours ne sont effacés à la déconnexion **que depuis le visualiseur** quand le mode est disponible. Une déconnexion depuis le tableau de bord, l'admin ou le profil les laisse dans le navigateur. Atténuations : clé suffixée par le compte, clés des autres comptes effacées à l'ouverture du mode, effacement sur 401. L'activité et la distance sont stockées aussi. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; effacement porté par le script commun (RB-2.6, P-TR6-6 précisées), CA-R36. |
| E-8 | §7 (413) | Quota atteint à l'enregistrement ⇒ **503** « Enregistrement impossible » au lieu de 413. Le message ne dit pas que le quota est atteint. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; 413 avec le message de quota (§7 précisé), CA-R37. |
| E-9 | EF-0.3b | Reprise d'amorçage pour un compte devenu inéligible (facteur obtenu, désactivé, converti) ⇒ 400 « Vérification impossible : mot de passe ou lien non valable. », au lieu de la réponse neutre de EF-3.6 (404). Le message est celui d'un mauvais mot de passe et ne révèle rien. | **Décidé par l'utilisateur (25/11)** : **accepté**, EF-0.3b alignée sur le code. |
| E-10 | EF-0.4 | En mode `oidc`, la saisie de secrets répond 409 (pas 404) et ne dépend que de `SECRETS_KEY`. L'admin n'affiche ni explication ni formulaire masqué. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; l'utilisateur utilisera le mode `oidc` avec des comptes à mot de passe. EF-0.4 inchangée, CA-61. |
| E-11 | EF-0.5 | Conversion SSO : l'adresse passée en attente n'est pas signalée dans la réponse à l'admin, seulement dans l'audit et la liste des comptes. | **Décidé par l'utilisateur (25/11)** : **accepté**, EF-0.5 alignée sur le code. |
| E-12 | EF-1.2, CA-31 | Collision d'**identifiant** créée par l'IdP : bandeau dans l'admin, mais ni `WARNING` ni audit `identity.collision` (les collisions d'adresse en ont). | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; mode `oidc` utilisé. EF-1.2 inchangée, CA-62 (une ligne par apparition de collision). |
| E-13 | EF-2.4 | « Suffixe de domaine enregistrable » approché par une liste de 18 suffixes publics (`PUBLIC_SUFFIXES`), pas la Public Suffix List. `gouv.fr` ou `blogspot.com` passeraient. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; **Public Suffix List complète, embarquée, sans dépendance réseau** (EF-2.4b), CA-59. Licence MPL-2.0 à accepter. |
| E-14 | EF-4.2, EF-5.6 | Les tests neutres rendent leur réponse à l'échéance, mais la tentative est **abandonnée** dans un thread, pas interrompue : un e-mail de test lent peut partir après une réponse « ko ». | **Décidé par l'utilisateur (25/11)** : **accepté**, EF-4.2 et EF-5.6 alignées sur le code. |
| E-15 | EF-4.3, EF-4.5 | Pas de bandeau « adresse non validée » ni de renvoi par l'utilisateur. Le renvoi par l'admin n'est pas limité à 3 / h. | **Décidé par l'utilisateur (25/11)** : **à corriger dans le code** ; bandeau, renvoi par l'utilisateur, limite de 3 / h partagée avec l'admin (EF-4.5 précisée), CA-60. |
| E-16 | EF-4.8 | La perte de la file d'envoi au redémarrage n'est pas journalisée. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **à corriger** ; EF-4.8 précisée, CA-66. |
| E-17 | EF-5.5 | Aucun secret ne peut venir de l'environnement : l'API le prévoit, mais aucun appelant ne lui passe de valeur d'env et aucune variable n'existe. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **spec alignée** : secrets en base seulement en v1 (EF-5.5, CA-34, ADR-0008 amendé). |
| E-18 | EF-5.6, CA-37 | Le bouton « Tester » du **DEM** n'est pas livré (moteur et SMTP le sont). | **Décidé par coord (25/11, à relire par l'utilisateur)** : **accepté en v1** (EF-5.6, CA-37 alignés). |
| E-19 | EF-6.4, P-T14-6 | La réponse de `PATCH /api/admin/settings` ne contient pas la version des réglages. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **accepté** (P-T14-6 alignée). |
| E-20 | EF-6.8 | Le diagnostic n'affiche pas les destinations effectives **hors** liste blanche. L'appel est refusé et audité malgré tout. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **bandeau « destination hors liste » à faire** (CA-64) ; le reste du diagnostic plus tard. |
| E-23 | CA-4 | Un non-admin qui envoie des crédits par `/api/invitations` reçoit **400**, pas 403. Les crédits ne bougent pas. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **accepté**, CA-4 aligné. |
| E-24 | RB-4.7 | Avec 1 point, la distance de P1 à la trace (≤ 50 m) et la fermeture ne sont pas contrôlées. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **à corriger**, CA-R38. |
| E-25 | RB-4.8 | Une seule altitude non finie dans la réponse du moteur fait échouer toute la génération en 502, au lieu d'ignorer l'altitude. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **plus tard** (sans effet tant que le moteur est déployé sans altitude). |
| E-26 | P-TR5-7 | Le nom par défaut est daté à l'heure du serveur (`datetime.now()`), pas dans `APP_TIMEZONE`. Le front envoie toujours ce nom. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **à corriger** (date dans `APP_TIMEZONE`, nom prérempli compris), P-TR5-7 précisée, CA-R39. |
| E-27 | RB-5.2 | `ResultStore.clear_user` (déconnexion, suppression du compte) n'est jamais appelé. | **Décidé par coord (25/11, à relire par l'utilisateur)** : **accepté**. |
| E-28 | §5.6 | Le réglage `routing.provider` n'existe pas : GraphHopper est en dur (`runtime.py`). | **Décidé par coord (25/11, à relire par l'utilisateur)** : **spec alignée** (§5.6 : pas de réglage `routing.provider` en v1). |

### 5.3 Interface

| # | Écart |
|---|---|
| E-21 | Admin : 6 onglets au lieu de 7 (« Clés API et e-mail » fusionnés) ; pas de bandeau « SMTP absent » ni « destination hors liste » ; marqueur de surcharge d'env non affiché ; avertissement « le secret sera effacé » sous forme de mention fixe, sans confirmation ; hôtes de tuiles autorisés non affichés ; libellés d'acteur bruts dans le journal (`user · cli`, `cli`, `system`) ; bandeau de collision sans lien vers les comptes ; diagnostic du moteur en JSON brut. En mode `oidc`, pas de page qui réunit SSO et formulaire. <br>**Décidé par coord (25/11, à relire par l'utilisateur)** : **à faire** pour l'usage `oidc` + comptes à mot de passe : page de connexion qui réunit SSO et formulaire (CA-63), bandeaux « SMTP absent » et « destination hors liste » (CA-64), confirmation dans la page avant d'effacer un secret lié (CA-65). **Accepté pour l'instant** : le reste du polissage (6 onglets, marqueur de surcharge, hôtes de tuiles, libellés d'acteur, lien du bandeau de collision, diagnostic en JSON). <br>**Mise à jour du 25/11 (refonte de la page Réglages, §5.6b)** : le **marqueur de surcharge** et les **hôtes de tuiles autorisés** ne sont plus du polissage accepté : ils sont **exigés** (EF-6.11, EF-6.12, CA-74, CA-75). |
| E-22 | Diagnostic de l'ordonnanceur (verrou détenu, état des tâches) : `Scheduler.status()` existe mais n'est exposé nulle part (EF-10.1b, §9). <br>**Décidé par coord (25/11, à relire par l'utilisateur)** : **accepté**, diagnostic de l'ordonnanceur reporté après la v1 (EF-10.1b et §9 annotés). |

---

## 6. Décisions de coord à relire avant la prod

Source : `STATUS.md`, section « Décisions de coord (à relire avant la prod) ». Le numéro renvoie à l'entrée du journal. Ces décisions ont été prises par coord, en autonomie, pendant la nuit du 24 au 25/11 (« il sera encore temps avant une prod de les remettre en question »).

| # | Décision | N° | Effet pour l'utilisateur | Réversible ? |
|---|---|---|---|---|
| D-1 | **Altitude MNT calculée à l'enregistrement** de la trace, pas à la génération (RB-4.8). Altitude du moteur gardée seulement si elle est complète. | 1 | Pas de profil d'altitude ni de D+ avant l'enregistrement (le moteur est déployé sans altitude). | **Gardé : décision de l'utilisateur (25/11)** |
| D-2 | **Rectangle de couverture** par défaut `[1.35, 48.83, 6.42, 51.55]`, réglable dans l'admin (`routing.coverage_bbox` ; depuis le lot Z1 du 26/11 : 4 sommets par défaut de `routing.coverage_polygon`). | 26 | Un point hors de la zone ⇒ 400 sans appel au moteur. | Oui (réglage) |
| D-3 | **Toutes les précisions par défaut de spec validées** (admin-space §15, gpx-route-builder §15), dont l'enrôlement valable 15 min (P-T11-2) et `routing.profiles` en code (P-TR5-5). | 2 | Comportements fixés sans question à l'utilisateur. | Oui |
| D-4 | Interprétations de dev confirmées : essais de graine à 1 point = `1 + routing.seed_retries` ; distance minimale 1 km. | 1 | — | Oui |
| D-5 | **Mot de passe oublié sans SMTP** : page et route en 404, lien absent de la page de connexion ; l'admin génère un lien de réinitialisation (1 h, voir E-1) et le transmet. | README « Non livré » | Sans SMTP, l'utilisateur dépend de l'admin. | Oui (configurer SMTP) |
| D-6 | **WebAuthn exige HTTPS** : origine = `PUBLIC_URL` ; `http://localhost` en test ou debug seulement. | 44 (4) | Sur une instance de test en HTTP, les clés ne sont pas proposées. | Non (sécurité, M12) |
| D-7 | **Retrait d'une clé WebAuthn** : mot de passe **et** code TOTP ou de secours (T12-n2 conservé). | 38 | Voir E-3 (b) pour un compte qui n'a que des clés. | Oui |
| D-8 | **Limiteur du second facteur : échecs seuls**. Un essai est réservé avant la vérification et rendu au succès (correctif MFA-1, commité dans `6faa266`). | 44 (1), 47, 53, 54 | Des connexions réussies répétées ne bloquent plus le compte ; 5 échecs ⇒ 429. | Oui |
| D-9 | **« Autre proposition » sans doublon** : le serveur écarte la trace précédente dans la limite du budget, sinon avertissement « Aucune autre proposition différente trouvée ». | 44 (2), 47 | — | Oui |
| D-10 | **Clé API du moteur jamais envoyée en clair** (HTTP), sauf `ROUTING_ALLOW_CLEARTEXT_KEY` explicite de l'exploitant. | 27 | Moteur auto-hébergé sans clé : sans effet. | Oui |
| D-11 | **Messages distincts** : 400 « Le point N est hors de la zone couverte. » (avant tout appel) ; 422 « Le point N est trop loin d'une voie praticable : rapproche-le d'un chemin. ». | 44 (3), 47 | Spec alignée (§8). | Oui |
| D-12 | `user-invite --admin --account [--reset-password]` **refuse un compte désactivé** (audit `user.bootstrap_refused`). | 44, 47 | — | Oui |
| D-13 | **Moteur GraphHopper validé définitivement** : image du projet (jar release 11.0 vérifié + Temurin 21), service `-Xmx1536m` / 2 Go, import `-Xmx6g`, mise à jour mensuelle par `routing/update.sh` avec retour arrière (ADR-0007). | 7, 8 | Environ 2,4 Go de disque, avec un pic d'environ 4 Go pendant la mise à jour (prévoir 5 Go). | Oui |
| D-14 | Déploiement **géré par Ansible**, un seul opérateur a accès à la CLI ; `restart.sh` sans objet en production, **remplacé par `docker compose up -d --force-recreate`** (décision de l'utilisateur, 25/11). | « Contexte de déploiement » | Les commandes CLI de secours (`user-mfa-reset`, `secrets-reset`) restent réservées à cet opérateur. | — |
| D-15 | Défauts préexistants **non corrigés** : migrations positionnelles 1 à 5 sans verrou, `PRAGMA journal_mode=WAL` concurrent sur une base neuve. | « Défauts préexistants » | Risque faible avec **1 worker gunicorn** ; à garder ainsi. | — |
| D-16 | Test de spec (`test_the_default_coverage_is_the_one_written_in_the_spec`) **sauté en CI** (seul `web-app` est monté). Lot Z1 : la ligne `routing.coverage_bbox` du §5.6 disparaît ; le test doit lire la ligne `routing.coverage_polygon` (défaut en liste de sommets). | 34, 36 | La concordance spec / code de la zone ne se vérifie qu'en local. | Oui |

Restent aussi ouverts pour l'utilisateur, hors décisions de coord :
- appréciation visuelle des traces **gravel** (`spike/detours/out/echantillon/`) ;
- **CA-R34** : taux réel de l'hybride dans la tolérance, non mesuré ;
- nit T16-n3 : fond de carte par défaut indisponible, non signalé dans l'admin ;

---

## 7. Trous de test

Aucun n'est un défaut du code. Ils diminuent la confiance accordée aux critères concernés.

- **Front** : RB-2.2 à RB-2.6, RB-4.10, P-TR6-* et CA-R17 sont vérifiés par recherche de chaînes dans le JS, pas par un navigateur. La recette de `test` (STATUS n° 44) n'a pas pu rejouer : badge Planifiée, effacement des points à la déconnexion, progression et « Annuler », profil d'altitude, WebAuthn dans un navigateur. **À jouer à la main** (recette, étape 8).
- **Page Réglages refondue (§5.6b)** : CA-72 à CA-78 sont des comportements de navigateur (onglets, recherche, éditeurs, confirmation, saisies conservées). Ils seront vérifiés par le **banc de test navigateur local Playwright, hors CI** (admin-space EF-6.17, CA-80, décision de coord du 25/11), joué par `test` avant chaque lot qui touche au front ; recette manuelle en complément. Tant que le banc n'existe pas, ils restent à jouer à la main.
- **CA-5** : pas de capture du flux d'accès gunicorn ; parcours d'invitation seulement (ni réinitialisation, ni amorçage).
- **CA-6** : expiration d'une invitation ⇒ crédit non rendu.
- **CA-13b** : « version des réglages inchangée » après un refus.
- **CA-25** : routes `/auth/password/mfa*`, `/enroll*`, `/auth/token/consume` et `/api/me/mfa` en mode `local`.
- **CA-30** : reconnexion OIDC avec l'ancien `sub` après conversion.
- **CA-40** : sémaphore testé en unitaire, pas par 10 connexions HTTP.
- **CA-51** : le faux `routing_health` du test est une tâche courte, alors que la vraie est longue (E-5).
- **CA-R16** : « un upload passe dans `heavy_slot()` pendant 2 générations lentes ».
- **EF-0.2** : l'avertissement « SMTP absent » au démarrage.
- **EF-4.4** : `pending_email` ignorée par `match_login` et par l'inbox.
- `test_admin_api.py` : une assertion sur `no-store` est neutralisée par `or True`.
- Non vérifié par spec : comportement interne de `py_webauthn` (attestation `none` par défaut, détection de la régression du compteur par le texte de l'exception).

---

## 8. Incohérences de spec trouvées et corrigées

| Où | Incohérence | Correction |
|---|---|---|
| `gpx-route-builder.md` RB-1.3 et CA-R20 | 422 « hors de la zone couverte » pour un point mal accroché, alors que la décision de coord n° 47 a fixé « Le point N est trop loin d'une voie praticable… » (code et tests conformes à la décision). | Libellés alignés sur la décision et le code, avec la raison ; le 400 « hors de la zone couverte » est inchangé. |
| `recette.md` (v3 → v3.1) | WebAuthn annoncé « non livré » ; trois écarts listés au §5 sont corrigés depuis (« Tester » le moteur, message « itinéraire », RB-2.2) ; étape 8.11 avec des messages périmés. | En-tête, §5, étapes 8.5 et 8.11 mis à jour ; renvoi vers ce document. |
| `admin-space.md` (25/11, décisions de l'utilisateur) | E-9, E-11, E-14 acceptés. | EF-0.3b, EF-0.5, EF-4.2 et EF-5.6 alignées sur le code. |
| `admin-space.md` (25/11, décisions de l'utilisateur) | E-1, E-3, E-10, E-12, E-13, E-15 à corriger. | EF-1.8 précisée (envoi, `sent_by_mail`) ; EF-2.4b ajoutée (Public Suffix List embarquée) ; EF-2.5 (notification sur tous les chemins) ; EF-4.5 (bandeau, renvoi, 3 / h partagés) ; P-T11-7 (assertion WebAuthn au profil) ; CA-56 à CA-62. |
| `gpx-route-builder.md` (25/11, décisions de l'utilisateur) | E-6, E-7, E-8 à corriger. | RB-2.1, RB-2.6, P-TR6-6, §7 (413) ; CA-R35 à CA-R37. |
| `admin-space.md`, `gpx-route-builder.md`, ADR-0008 (25/11, décisions de coord) | E-17, E-18, E-19, E-22, E-23, E-28 acceptés ; E-16, E-20/E-21, E-24, E-26 à corriger ; questions du §8 tranchées. | EF-2.4b, P-T11-7, EF-4.8, EF-5.5, EF-5.6, EF-10.1b, §9, P-T14-6, CA-4, CA-34, CA-37, CA-63 à CA-66 ; RB-2.6, P-TR6-6, §5.6, P-TR5-7, CA-R36, CA-R38, CA-R39 ; ADR-0008 amendé (secrets en base seulement). |
| `admin-space.md` (25/11, écarts du groupe 3a et refonte de la page Réglages) | Écarts du groupe 3a (test) : compte SSO ⇒ 404 au renvoi de vérification ; liste admin des invitations ⇒ 404 quand EF-0.4 s'applique ; arrêt réel sous gunicorn. Refonte de la page Réglages demandée par l'utilisateur. P-T15-5 remplacé. | EF-0.4, EF-4.5, EF-4.8, CA-60, CA-61, nouveau CA-66b ; nouvelle §5.6b (EF-6.9 à EF-6.17), §9, P-T15-5, CA-67 à CA-80 ; questions tranchées par coord. |
| `admin-space.md` (25/11, review du lot 2) | E-15-1 : pas de limite sur les changements d'adresse ; E-1-1 : plusieurs liens de réinitialisation valables en même temps. | EF-4.6 (3 changements par heure et par compte, 10 par heure et par IP, budget distinct de celui des renvois), EF-S3, CA-60b ; EF-1.8 (un seul lien `reset` valable par compte), CA-56b. |
| `altitude-mnt.md` (nouvelle, 25/11), `gpx-route-builder.md` | Défaut trouvé par test : l'altitude MNT d'une trace corrigée n'arrive pas à la page (`/parsed` relit le fichier brut), et l'analyse enregistrée n'est pas invalidée par la correction. | altitude-mnt EA-1 à EA-6, CA-A1 à CA-A6 ; RB-4.8, RB-5.3 renvoient vers elle ; question tranchée par coord le 25/11 : altitude absente rendue 0, hors périmètre et non modifiée ; garde de course (CA-A6) retenue. |
| `admin-space.md` (25/11, défaut trouvé par l'utilisateur) | Un compte SSO ne voit pas la section « Invitations » du profil : le gabarit la réserve aux comptes `password`, contre EF-3.2 et §9. | Nouvelle EF-3.12, §9 (Profil) précisé, CA-81. |
| `admin-space.md` (25/11, second retour de l'utilisateur sur les Réglages ; demande de coord sur les invitations) | Page Réglages « toujours pas super lisible » : onglets sur 2 lignes, pastilles de total, champs pleine largeur, aide peu contrastée, pastilles dispersées. Rectangle de couverture à éditer sur la carte. Onglet Invitations de l'admin sans émetteur, statuts en anglais. | Nouvelles EF-6.18 (lisibilité), EF-6.19 (rectangle sur la carte), EF-3.13 ; EF-6.9 à EF-6.12 et CA-72 à CA-74, CA-76 ajustés ; CA-82, CA-83, CA-84. Décisions de coord du 25/11 : plus de pastille « Défaut » ; poignées au clavier et aucune nouvelle dépendance Leaflet ; « Acceptée » partout (profil et admin), EF-3.12 renvoie aux libellés de EF-3.13. |
| `gpx-route-builder.md` (25/11, retours de l'utilisateur sur le constructeur) | Points restaurés au rechargement après un enregistrement réussi. Après « Terminer », déplacer un point ne régénère pas la trace : la trace affichée ne correspond plus aux points. | RB-2.6 (effacement après enregistrement), CA-R40 ; nouvelle RB-2.7, §3 (exception au « pas de routage en direct »), légende [banc] au §8, CA-R41 à CA-R48 ; question §13 n° 3 tranchée par coord le 25/11 puis **remplacée** le même jour sur retour de l'utilisateur : déplacer, insérer (appui sur la trace, un seul geste pour clic et glisser), ajouter et retirer régénèrent ; seul « Tout effacer » revient à l'édition ; distance voulue conservée (décision de spec justifiée dans RB-2.7). |
| `gpx-route-builder.md` CA-R48 (25/11, écart relevé par test) | CA-R48 supposait le résultat enregistré encore affiché et modifiable, contraire à CA-R40 (constructeur remis à neuf après l'enregistrement). | CA-R40 prévaut (décision de coord) : CA-R48 et RB-2.7 réécrits ; N jamais modifiée, nouvel enregistrement = activité N′ distincte, retouche d'une trace enregistrée par « Éditer la trace ». |
| `gpx-route-builder.md` RB-1.3b, CA-R49, CA-R51 ; `admin-space.md` EF-6.20, CA-85 (25/11, relevé par test) | CA-R49 mesurait les points techniques S1 sur une génération à 1 point, qui n'en a pas ; la règle « pas de point technique si le milieu de la corde est hors zone » s'appliquait aussi à un point déjà dans la zone ; « alignés » et le bord du pré-filtre n'étaient pas définis. | Décisions de coord : CA-R49 à 3 points (1 à 3 points techniques dans l'échancrure avec le rectangle seul, 0 avec le polygone) ; point technique dans la zone gardé ; alignés sur la carte (Web Mercator) ; hors de l'emprise = dehors, même à moins de 10⁻⁹ d'une arête du bord, front compris (`zone_cases.json`). |
| `admin-space.md` EF-6.19 à EF-6.21, §5.6b, §7, §12, CA-76, CA-83, CA-85 à CA-87 ; `gpx-route-builder.md` RB-1.3, RB-1.3b, §5.6, §12, §14, P-TR5-1, P-TR6-4, CA-R20, CA-R20b, CA-R49, CA-R50 ; `recette.md` étapes 6.6, 8 (26/11, lot Z1 demandé par coord) | Deux réglages pour une seule zone (rectangle de repli + polygone, polygone vide = rectangle), source de confusion dans l'admin (« Inutilisé ») et de double code. | Un seul réglage `routing.coverage_polygon`, défaut = 4 sommets du rectangle par défaut, `[]` refusé ; `routing.coverage_bbox`, type `rectangle` et EF-6.19 supprimés ; `coverage_bbox` gardé dans `options` comme emprise calculée ; migration nommée `0017_coverage_polygon_only` qui garde la zone en vigueur (CA-87). |

Questions ouvertes nées de ces reports : **toutes tranchées par coord le 25/11** (tableau du §5) : licence MPL-2.0 acceptée ; refus de démarrer confirmé ; assertion WebAuthn limitée au retrait de clé et à la régénération des codes ; activité et distance gardées en `localStorage`.

Plus aucun alignement en attente. Les 6 questions de la refonte de la page Réglages ont été tranchées par coord le 25/11 (admin-space §5.6b).
