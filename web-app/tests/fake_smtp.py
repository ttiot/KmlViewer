"""Serveur SMTP factice (tests) : enregistre chaque commande reçue ; modes ok / starttls annoncé / muet. Aucun TLS."""

import socket
import socketserver
import threading


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        server.connections += 1
        if server.mode == 'hang':
            self.request.settimeout(30)
            try:
                self.rfile.readline()
            except OSError:
                pass
            return
        self.wfile.write(b"220 fake ESMTP\r\n")
        data_mode, lines = False, []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode('utf-8', 'replace').rstrip('\r\n')
            if data_mode:
                if line == '.':
                    server.messages.append('\n'.join(lines))
                    data_mode, lines = False, []
                    self.wfile.write(b"250 queued\r\n")
                else:
                    lines.append(line[1:] if line.startswith('..') else line)
                continue
            server.commands.append(line)
            verb = line.split(' ', 1)[0].upper()
            if verb == 'EHLO' or verb == 'HELO':
                extra = b"250-STARTTLS\r\n" if server.advertise_starttls else b""
                self.wfile.write(b"250-fake\r\n" + extra + b"250 AUTH PLAIN LOGIN\r\n")
            elif verb == 'STARTTLS':
                self.wfile.write(b"454 TLS not available\r\n")
            elif verb == 'AUTH':
                self.wfile.write(b"235 ok\r\n")
            elif verb in ('MAIL', 'RCPT'):
                self.wfile.write(b"250 ok\r\n")
            elif verb == 'DATA':
                data_mode = True
                self.wfile.write(b"354 go\r\n")
            elif verb == 'QUIT':
                self.wfile.write(b"221 bye\r\n")
                return
            else:
                self.wfile.write(b"250 ok\r\n")


class FakeSMTP(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, mode='ok', advertise_starttls=False):
        super().__init__(('127.0.0.1', 0), _Handler)
        self.mode, self.advertise_starttls = mode, advertise_starttls
        self.connections, self.commands, self.messages = 0, [], []
        self.port = self.server_address[1]
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def verbs(self):
        return [c.split(' ', 1)[0].upper() for c in self.commands]

    def stop(self):
        self.shutdown()
        self.server_close()


def closed_port() -> int:
    """Un port local sur lequel rien n'écoute (connexion refusée)."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
