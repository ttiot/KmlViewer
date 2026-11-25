"""
Service d'altitude FACTICE du banc (format opentopodata : GET ?locations=lat,lon|lat,lon -> results[].elevation).
Une « porte » pilote le scénario du correctif d'altitude : /gate/close (503 : la tâche de fond échoue et
réessaie), /gate/open, /stats.
Lancé par run.sh dans un conteneur (image de l'application, réseau hôte) : `python fake_dem.py <port>`.
"""

import json
import math
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

STATE = {'open': False, 'calls': 0, 'points': 0}


def elevation(lat, lon):
    """Un relief net (centaines de mètres sur quelques kilomètres) : le dénivelé se voit."""
    return round(100 + (lat - 50.6) * 6000 + 25 * math.sin(lon * 900), 1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (nom imposé par http.server)
        url = urlsplit(self.path)
        if url.path in ('/gate/open', '/gate/close'):
            STATE['open'] = url.path.endswith('open')
            return self.reply(200, STATE)
        if url.path == '/stats':
            return self.reply(200, STATE)
        if not STATE['open']:
            return self.reply(503, {'status': 'ERROR', 'error': 'porte fermée'})
        locations = [loc for loc in parse_qs(url.query).get('locations', [''])[0].split('|') if loc]
        STATE['calls'] += 1
        STATE['points'] += len(locations)
        results = [{'elevation': elevation(*map(float, loc.split(','))), 'location': {}} for loc in locations]
        return self.reply(200, {'status': 'OK', 'results': results})


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
