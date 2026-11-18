"""Commandes d'exploitation : `flask --app kml_viewer <commande>`."""

import click

from app.db import connect
from app.services import inbox_watcher


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
