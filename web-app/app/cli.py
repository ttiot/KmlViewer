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


def register_cli(app) -> None:
    @app.cli.command('inbox-scan')
    def inbox_scan():
        """Passe une fois sur le dossier surveillé et ingère les nouveaux fichiers."""
        db = connect(app.config['DB_PATH'])
        try:
            counts = inbox_watcher.scan_once(db, app.config)
        finally:
            db.close()
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
            if revoke:
                # Retirer un droit ne doit rien donner d'autre : un compte désactivé le reste
                db.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (target['id'],))
            else:
                # Promouvoir active aussi le compte : c'est ainsi que se désigne le premier
                # admin, dont le compte attend sinon une activation que personne ne peut donner
                db.execute("UPDATE users SET is_admin = 1, is_active = 1 WHERE id = ?", (target['id'],))
            db.commit()
        finally:
            db.close()
        click.echo(f"Compte #{target['id']} ({target['username']}) mis à jour")

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
        try:
            try:
                count = app.extensions['settings'].reset(db, None if reset_all else key, actor=Actor.cli())
            except KeyError as exc:
                raise click.ClickException(str(exc.args[0])) from exc
        finally:
            db.close()
        click.echo(f"{count} valeur(s) en base supprimée(s)")
