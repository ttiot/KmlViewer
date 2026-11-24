"""Commandes d'exploitation : `flask --app kml_viewer <commande>`."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click

from app.auth import users
from app.db import connect
from app.services import inbox_watcher, ingest_service


def backup_database(db_path: str, target: Path) -> None:
    """Copie cohérente de la base, utilisable pendant que l'application tourne (API de backup SQLite)."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = target.with_name(target.name + '.partial')
    source = connect(db_path)
    try:
        destination = sqlite3.connect(partial)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    partial.chmod(0o600)
    partial.replace(target)  # le fichier final n'apparaît qu'une fois complet


def _notify(app, recipients, kind, **context) -> list:
    """
    Notification SYNCHRONE d'une commande CLI (EF-4.8, v5-3) : la file n'est pas traitée par un thread en CLI, chaque message
    part avant de rendre la main. Retourne les destinataires dont l'envoi a échoué ; liste vide si tout est parti ou si l'e-mail
    n'est pas configuré (ce n'est pas un échec : l'action est faite, l'appelant l'annonce).
    """
    from app.mailer import MailError, compose
    mailer = app.extensions['mailer']
    if not mailer.configured:
        return []
    subject, body = compose(kind, **context)
    failed = []
    for address in recipients:
        try:
            mailer.send(address, subject, body)
        except MailError:
            failed.append(address)
    return failed


def _admin_addresses(db) -> list:
    return [r['email'] for r in db.execute("SELECT email FROM users WHERE is_admin = 1 AND is_active = 1 AND email IS NOT NULL")]


def _report_notification(app, failed, what: str) -> None:
    """Message nommant la notification non partie, puis code de sortie non nul (l'action elle-même reste faite et tracée)."""
    if failed:
        click.echo(f"ATTENTION : la notification « {what} » n'est PAS partie ({len(failed)} destinataire(s) sans message). "
                   "L'action, elle, a bien été faite et tracée.", err=True)
        raise SystemExit(1)


def register_cli(app) -> None:
    @app.cli.command('inbox-scan')
    def inbox_scan():
        """Passe une fois sur le dossier surveillé et ingère les nouveaux fichiers."""
        from app.background import LOCK_NAME, FileLock
        # Même verrou que l'ordonnanceur : jamais deux passes en parallèle sur le dossier
        lock = FileLock(Path(app.config['DATA_DIR']) / LOCK_NAME)
        if not lock.try_acquire():
            raise click.ClickException("Un autre processus (l'ordonnanceur de l'application) surveille déjà le dossier : "
                                       "il le traite lui-même.")
        db = connect(app.config['DB_PATH'])
        try:
            counts = inbox_watcher.scan_once(db, app.config)
        finally:
            db.close()
            lock.release()
        click.echo(', '.join(f"{k}={v}" for k, v in counts.items()))

    @app.cli.command('recompute-stats')
    def recompute_stats():
        """Recalcule les résumés (distance, durées, records) de toutes les activités stockées."""
        db = connect(app.config['DB_PATH'])
        try:
            count = ingest_service.recompute_summaries(db)
        finally:
            db.close()
        click.echo(f"{count} activité(s) recalculée(s)")

    @app.cli.command('user-admin')
    @click.argument('login')
    @click.option('--revoke', is_flag=True, help="Retire le rôle admin au lieu de le donner.")
    def user_admin(login, revoke):
        """Donne (ou retire) le rôle admin à un compte, par username ou email."""
        from app.audit import Actor
        db = connect(app.config['DB_PATH'])
        try:
            matches = users.match_login(db, login)
            if not matches:
                raise click.ClickException(f"Aucun compte pour « {login} »")
            if len(matches) > 1:
                # Jamais de promotion en lot : un homonyme deviendrait admin au passage
                found = ', '.join(f"#{row['id']} {row['username']} <{row['email']}>" for row in matches)
                raise click.ClickException(f"« {login} » désigne plusieurs comptes ({found}) : rien n'est modifié")
            target = matches[0]
            try:
                users.set_admin(db, target['id'], not revoke, actor=Actor.cli(), activate=True)
            except users.AdminPromotionRefused as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            db.close()
        click.echo(f"Compte #{target['id']} ({target['username']}) mis à jour")

    def _canonical_or_none(value):
        from app.auth import tokens
        try:
            return tokens.canonical_email(value)
        except ValueError:
            return None

    @app.cli.command('user-invite')
    @click.argument('email', required=False)
    @click.option('--admin', 'amorcage', is_flag=True, help="Amorçage : le seul jeton qui donne l'admin (obligatoire en CLI).")
    @click.option('--account', default=None, help="Reprend l'amorçage d'un compte à mot de passe existant, sans facteur.")
    @click.option('--reset-password', 'reset_password', is_flag=True,
                  help="Avec --account : le lien fait CHOISIR un nouveau mot de passe (mot de passe oublié) avant l'enrôlement.")
    def user_invite(email, amorcage, account, reset_password):
        """
        Amorçage (EF-0.3) : émet un jeton à usage unique de 24 h dont la consommation crée le premier admin, ou
        reprend l'amorçage abandonné d'un compte (`--account`). Le lien est affiché une fois.
        """
        from app.audit import Actor
        from app.auth import factors, modes, tokens
        if not amorcage:
            raise click.UsageError("Seul l'amorçage est disponible en ligne de commande : ajoute --admin.")
        if reset_password and not account:
            raise click.UsageError("--reset-password demande --account <identifiant>.")
        if not modes.password_accounts_available(app.config):
            raise click.ClickException("Les comptes à mot de passe ne sont pas disponibles (mode password, ou mode oidc "
                                       "avec SECRETS_KEY et PUBLIC_URL).")
        if not app.config.get('PUBLIC_URL'):
            raise click.ClickException("PUBLIC_URL est absente : impossible de construire le lien.")
        db = connect(app.config['DB_PATH'])
        try:
            target_id, address = None, email
            if account:
                matches = users.match_login(db, account)
                if len(matches) != 1:
                    raise click.ClickException(f"« {account} » ne désigne pas exactement un compte")
                target = matches[0]
                if target['auth_method'] != 'password':
                    raise click.ClickException("Seul un compte à mot de passe peut reprendre un amorçage.")
                if factors.has_second_factor(db, target['id']):
                    raise click.ClickException("Ce compte a déjà un second facteur : promouvoir avec `flask user-admin`.")
                target_id, address = target['id'], target['email'] or email
            elif not email:
                raise click.UsageError("Donne l'adresse e-mail de la personne à amorcer (ou --account <identifiant>).")
            else:
                try:
                    wanted = tokens.canonical_email(email)
                except ValueError as exc:
                    raise click.ClickException(str(exc)) from exc
                # Comparaison sur la forme canonique (NFKC + casefold), jamais sur la saisie brute
                holders = [r['email'] for r in db.execute("SELECT email FROM users WHERE email IS NOT NULL")]
                if any(_canonical_or_none(h) == wanted for h in holders):
                    raise click.ClickException("Cette adresse est déjà portée par un compte.")
                if tokens.pending_for_email(db, wanted):
                    raise click.ClickException("Une invitation est déjà en attente pour cette adresse : "
                                              "révoque-la ou attends son expiration.")
            try:
                created = tokens.create(db, 'bootstrap', actor=Actor.cli(), target_user_id=target_id, email=address,
                                        is_admin_grant=True, note=tokens.RESET_PASSWORD_NOTE if reset_password else None)
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            db.close()
        link = tokens.link(app.config['PUBLIC_URL'], created.token)
        click.echo(f"Jeton d'amorçage créé (valable jusqu'au {created.expires_at}, usage unique). Lien, montré une seule fois :")
        click.echo(link)
        if not address:
            click.echo("Aucune adresse connue pour ce compte : transmets ce lien à la seule personne concernée.")
        elif not app.extensions['mailer'].configured:
            click.echo("E-mail non configuré : transmets ce lien à la seule personne concernée.")
        else:
            failed = _notify(app, [address], 'bootstrap', link=link)
            if not failed:
                click.echo("Le lien est aussi parti par e-mail.")
            _report_notification(app, failed, "lien d'amorçage par e-mail")

    @app.cli.command('user-mfa-reset')
    @click.argument('login')
    def user_mfa_reset(login):
        """
        Perte de tous les facteurs d'un compte à mot de passe : retire ses facteurs, ferme ses sessions et le
        rétrograde s'il est admin (EF-2.7, B5). Il se réenrôle à sa prochaine connexion.
        """
        from app.audit import Actor, record
        from app.auth import factors
        from app.db import write_transaction
        db = connect(app.config['DB_PATH'])
        try:
            matches = users.match_login(db, login)
            if len(matches) != 1:
                raise click.ClickException(f"« {login} » ne désigne pas exactement un compte")
            target = matches[0]
            if target['auth_method'] != 'password':
                raise click.ClickException("Seuls les comptes à mot de passe ont des facteurs à réinitialiser.")
            demoted = bool(target['is_admin'])
            holder_email = target['email']
            if demoted and target['is_active'] and factors.active_admin_count(db) <= 1:
                # EF-2.7, r6 : l'annonce précède l'action
                click.echo("ATTENTION : « %s » est le DERNIER administrateur actif ; il va être rétrogradé et l'instance restera "
                           "sans administrateur. Sortie : `flask user-invite --admin`." % target['username'], err=True)
            with write_transaction(db):
                removed = factors.clear_factors(db, target['id'])
                db.execute("UPDATE users SET session_token = ?, is_admin = 0 WHERE id = ?",
                           (users._new_session_token(), target['id']))
                record(db, 'user.mfa_reset', actor=Actor.cli(), target_type='user', target_id=target['id'],
                       details={'demoted': demoted, 'factors_removed': sum(removed.values())})   # clés neutres
            remaining = factors.active_admin_count(db)
        finally:
            db.close()
        click.echo(f"Facteurs de #{target['id']} ({target['username']}) retirés, sessions fermées"
                   + (", compte rétrogradé (à repromouvoir après réenrôlement)" if demoted else "") + ".")
        _warn_if_no_admin(remaining)
        if not holder_email:
            click.echo("Aucune adresse validée pour ce compte : aucune notification envoyée.")
        elif not app.extensions['mailer'].configured:
            click.echo("E-mail non configuré : aucune notification envoyée.")
        else:
            _report_notification(app, _notify(app, [holder_email], 'factor_reset'), "facteurs réinitialisés")

    def _warn_if_no_admin(count):
        if count == 0:
            click.echo("ATTENTION : l'instance n'a plus aucun administrateur actif. Sortie : `flask user-admin <login>` sur "
                       "un compte qui a un second facteur (ou l'amorçage `flask user-invite --admin`).", err=True)

    @app.cli.command('backup')
    @click.argument('destination', required=False, type=click.Path(dir_okay=False, path_type=Path))
    @click.option('--keep', type=click.IntRange(min=1), default=None,
                  help="Ne conserve que les N sauvegardes les plus récentes du dossier par défaut.")
    def backup(destination, keep):
        """Sauvegarde la base SQLite (par défaut dans DATA_DIR/backups, nom horodaté)."""
        backups_dir = Path(app.config['DATA_DIR']) / 'backups'
        if destination is None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            destination = backups_dir / f'kmlviewer-{stamp}.sqlite3'
        try:
            backup_database(app.config['DB_PATH'], destination)
        except (OSError, sqlite3.Error) as exc:
            raise click.ClickException(f"Sauvegarde impossible : {exc}") from exc
        click.echo(f"Base sauvegardée dans {destination}")
        if keep and backups_dir.is_dir():
            # Le nom horodaté trie par ancienneté
            for old in sorted(backups_dir.glob('kmlviewer-*.sqlite3'), reverse=True)[keep:]:
                old.unlink()
                click.echo(f"Ancienne sauvegarde supprimée : {old.name}")

    @app.cli.command('settings-list')
    def settings_list():
        """Affiche chaque réglage administrable : valeur effective, source (base / env / défaut), surcharge."""
        db = connect(app.config['DB_PATH'])
        try:
            store = app.extensions['settings']
            store.refresh(db, force=True)
            for d in store.diagnostic():
                note = f"  [surcharge {d['env_overridden']}]" if d['env_overridden'] else ''
                note += f"  [{d['env']} invalide, ignorée]" if d['env_invalid'] else ''
                click.echo(f"{d['key']} = {d['value']!r}  ({d['source']}){note}")
        finally:
            db.close()

    @app.cli.command('settings-reset')
    @click.argument('key', required=False)
    @click.option('--all', 'reset_all', is_flag=True, help="Supprime toutes les valeurs en base.")
    def settings_reset(key, reset_all):
        """Revient à la valeur d'env ou au défaut (supprime la valeur en base), tracé dans l'audit (acteur cli)."""
        if bool(key) == reset_all:
            raise click.UsageError("Donne une clé de réglage, ou --all (pas les deux).")
        from app.audit import Actor
        db = connect(app.config['DB_PATH'])
        failed = []
        try:
            touches_smtp = reset_all or (key or '').startswith('smtp.')
            if touches_smtp:
                failed = _notify(app, _admin_addresses(db), 'smtp_reset')          # avant : la configuration SMTP va changer
            try:
                count = app.extensions['settings'].reset(db, None if reset_all else key, actor=Actor.cli())
            except KeyError as exc:
                raise click.ClickException(str(exc.args[0])) from exc
        finally:
            db.close()
        click.echo(f"{count} valeur(s) en base supprimée(s)")
        _report_notification(app, failed, "réinitialisation SMTP, aux administrateurs")

    @app.cli.command('secrets-rotate')
    def secrets_rotate():
        """Rechiffre secrets et secrets TOTP avec la première clé de SECRETS_KEY (après y avoir ajouté la nouvelle en tête)."""
        from app.audit import Actor
        from app.secrets_store import SecretsError, UndecryptableSecrets
        db = connect(app.config['DB_PATH'])
        try:
            counts = app.extensions['secrets'].rotate(db, actor=Actor.cli())
        except UndecryptableSecrets as exc:
            raise click.ClickException(
                f"{exc}. Ancienne clé absente de SECRETS_KEY ? Clé perdue : `flask secrets-reset`.") from exc
        except SecretsError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            db.close()
        click.echo(f"Rechiffré : {counts['secrets']} secret(s), {counts['totp']} secret(s) TOTP"
                   + (f" ; {counts.get('skipped', 0)} ligne(s) modifiée(s) pendant la rotation, laissée(s) telle(s)"
                      " (relance la commande)" if counts.get('skipped', 0) else "")
                   + ". Tu peux retirer l'ancienne clé de SECRETS_KEY puis redémarrer.")

    @app.cli.command('secrets-reset')
    @click.option('--yes', is_flag=True, help="Ne demande pas de confirmation.")
    def secrets_reset(yes):
        """Perte de toutes les clés : efface secrets et secrets TOTP (à ressaisir ; les clés WebAuthn restent)."""
        from app.audit import Actor
        if not yes:
            click.confirm("Effacer TOUS les secrets chiffrés et les secrets TOTP ?", abort=True)
        db = connect(app.config['DB_PATH'])
        try:
            admins = _admin_addresses(db)
            # AVANT l'effacement : le mot de passe SMTP lui-même va disparaître, l'envoi doit encore pouvoir s'authentifier
            failed = _notify(app, admins, 'secrets_reset')
            counts = app.extensions['secrets'].reset(db, actor=Actor.cli())
        finally:
            db.close()
        click.echo(f"Effacé : {counts['secrets']} secret(s), {counts['totp']} secret(s) TOTP.")
        if counts.get('demoted'):
            click.echo(f"{counts['demoted']} administrateur(s) sans plus aucun facteur rétrogradé(s) ; ils se réenrôlent à leur "
                       "prochaine connexion (codes de secours invalidés).")
        _report_notification(app, failed, "effacement des secrets, aux administrateurs")
        from app.auth import factors
        db = connect(app.config['DB_PATH'])
        try:
            _warn_if_no_admin(factors.active_admin_count(db))
        finally:
            db.close()
