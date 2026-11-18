"""Commandes d'exploitation : `flask --app kml_viewer <commande>`."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import click

from app.db import connect
from app.services import inbox_watcher


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

    @app.cli.command('user-admin')
    @click.argument('login')
    @click.option('--revoke', is_flag=True, help="Retire le rôle admin au lieu de le donner.")
    def user_admin(login, revoke):
        """Donne (ou retire) le rôle admin à un compte, par username ou email."""
        db = connect(app.config['DB_PATH'])
        try:
            cur = db.execute(
                "UPDATE users SET is_admin = ?, is_active = 1 "
                "WHERE lower(username) = lower(?) OR lower(email) = lower(?)",
                (0 if revoke else 1, login, login))
            db.commit()
        finally:
            db.close()
        if cur.rowcount == 0:
            raise click.ClickException(f"Aucun compte pour « {login} »")
        click.echo(f"{cur.rowcount} compte(s) mis à jour")

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
