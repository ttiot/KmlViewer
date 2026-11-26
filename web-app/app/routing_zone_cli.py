"""
`flask routing-zone` : dialogue de l'hôte avec l'application (routing-zone-donnees ZD-10). Appelée par l'hôte seulement
(`docker compose exec -T kml-viewer flask routing-zone …`).

Sortie : UNE ligne JSON sur la sortie standard ; journaux sur la sortie d'erreur, sans coordonnée. Codes : 0 succès,
2 argument ou contenu invalide, 3 demande dépassée ou inconnue, 4 désactivé (`ROUTING_ZONE_BUILD=0`). Contenus volumineux par
l'entrée standard (`-`), taille lue et bornée AVANT toute analyse. La commande ne démarre ni ordonnanceur ni fil de fond
(déjà garanti hors service par `_start_scheduler`) ; elle ne fait qu'écrire en base, en une transaction.
"""

import json
import re
import sys
from datetime import datetime, timezone

import click

from app.db import connect
from app.services.routing import catalog as catalog_module
from app.services.routing import coverage_state as coverage
from app.services.routing import zone
from app.settings import effective

EXIT_INVALID, EXIT_STALE, EXIT_DISABLED = 2, 3, 4
_BUDGET = re.compile(r'(\d{1,9}),(\d{1,15})')


def _now(app) -> datetime:
    clock = app.config.get('ROUTING_ZONE_CLOCK')
    return clock() if clock else datetime.now(timezone.utc).replace(tzinfo=None)


def _read_stdin(limit: int) -> bytes:
    """Entrée standard bornée : lit au plus `limit + 1` octets ; au-delà, refus SANS rien analyser."""
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        click.echo("invalide : contenu trop volumineux", err=True)
        sys.exit(EXIT_INVALID)
    return data


def _emit(payload: dict) -> None:
    click.echo(json.dumps(payload, separators=(',', ':'), sort_keys=True))


def register(app) -> None:
    @app.cli.group('routing-zone')
    def routing_zone():
        """Dialogue de l'hôte avec l'application (couverture pilotée par la zone, ZD-10)."""

    def require_enabled():
        """Désactivé ⇒ code 4, AVANT de lire l'entrée standard."""
        if not coverage.enabled(app.config):
            click.echo("routing-zone désactivé (ROUTING_ZONE_BUILD=0)", err=True)
            sys.exit(EXIT_DISABLED)

    def run(operation):
        """Contrôle d'activation, connexion, codes de sortie : un seul endroit pour toutes les sous-commandes."""
        require_enabled()
        conn = connect(app.config['DB_PATH'])
        try:
            _emit(operation(conn, _now(app)) or {})
        except coverage.CoverageError as exc:
            click.echo(f"invalide : {exc}", err=True)
            sys.exit(EXIT_INVALID)
        except coverage.StaleRequest:
            click.echo("demande dépassée ou inconnue", err=True)
            sys.exit(EXIT_STALE)
        except Exception as exc:  # noqa: BLE001 - hors contrat (GEOS, base verrouillée) : code 2, jamais de trace (coordonnées)
            click.echo(f"erreur interne : {type(exc).__name__}", err=True)
            sys.exit(EXIT_INVALID)
        finally:
            conn.close()

    def requested():
        return zone.effective_zone(lambda key: effective(app.config, key))

    def margin():
        return int(effective(app.config, 'routing.zone_margin_km'))

    @routing_zone.command('catalog')
    @click.argument('source')
    def catalog_command(source):
        """Catalogue des régions, lu sur l'entrée standard (`-`)."""
        if source != '-':
            raise click.UsageError("le catalogue se lit sur l'entrée standard : catalog -")
        require_enabled()
        raw = _read_stdin(catalog_module.MAX_CATALOG_BYTES)
        run(lambda conn, now: coverage.load_catalog_document(conn, raw, app.config, now))

    @routing_zone.command('claim')
    @click.option('--monthly', is_flag=True, help="Échéance mensuelle passée : reconstruit aussi la zone active.")
    def claim_command(monthly):
        """Prend la demande en attente (ZD-9.3) ; `{}` s'il n'y a rien à prendre."""
        run(lambda conn, now: coverage.claim(conn, app.config, requested(), margin(), now, monthly))

    @routing_zone.command('switching')
    @click.argument('request_id', type=int)
    def switching_command(request_id):
        """Le graphe est basculé, le moteur redémarre."""
        run(lambda conn, now: coverage.switching(conn, request_id, requested(), now))

    @routing_zone.command('done')
    @click.argument('request_id', type=int)
    @click.option('--graph', required=True)
    @click.option('--data-date', 'data_date', required=True)
    @click.option('--new-tiles', 'new_tiles', default='-', help="Dalles nouvelles (JSON) sur l'entrée standard : `-`.")
    def done_command(request_id, graph, data_date, new_tiles):
        """La construction est terminée et en service."""
        if new_tiles != '-':
            raise click.UsageError("les dalles se lisent sur l'entrée standard : --new-tiles -")
        require_enabled()

        def operation(conn, now):
            try:
                tiles = json.loads(_read_stdin(64 * 1024) or b'[]')
            except (ValueError, RecursionError):
                raise coverage.CoverageError("dalles illisibles") from None
            return coverage.done(conn, request_id, graph, data_date, tiles, requested(), now)

        run(operation)

    @routing_zone.command('failed')
    @click.argument('request_id', type=int)
    @click.option('--code', required=True)
    def failed_command(request_id, code):
        """La construction a échoué : l'actif est inchangé."""
        run(lambda conn, now: coverage.failed(conn, request_id, code, requested(), now))

    @routing_zone.command('report')
    @click.option('--graph', required=True)
    @click.option('--zone', 'zone_source', default=None, help="`zone.json` du graphe en service, sur l'entrée standard : `-`.")
    @click.option('--historic', is_flag=True, help="Graphe d'avant Z2 : « sources historiques ».")
    @click.option('--budget', default=None, help="Compteurs du jour de l'hôte : TÂCHES,OCTETS.")
    def report_command(graph, zone_source, historic, budget):
        """Rapport de l'hôte : graphe en service, sa zone, budget du jour."""
        if bool(zone_source) == bool(historic) or (zone_source and zone_source != '-'):
            raise click.UsageError("exactement un de : --zone - ou --historic")
        counters = None
        if budget is not None:
            match = _BUDGET.fullmatch(budget)
            if not match:
                click.echo("invalide : budget", err=True)
                sys.exit(EXIT_INVALID)
            counters = (int(match.group(1)), int(match.group(2)))
        require_enabled()
        raw = _read_stdin(coverage.MAX_ZONE_JSON_BYTES) if zone_source else b''

        def operation(conn, now):
            info = None
            if not historic:
                try:
                    info = coverage.parse_zone_json(raw)
                except coverage.CoverageError:
                    if coverage.report_invalid(conn, raw):
                        click.echo("zone rapportée invalide : ignorée", err=True)
                    raise
            return coverage.report(conn, graph, info, historic, counters, requested(), now)

        run(operation)
