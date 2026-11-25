"""
E-mail (admin-space §5.4, ADR-0005) : construction, envoi synchrone ou file d'envoi, notifications.

Sécurité (B3, EF-4.1, EF-4.9) :
  - destination `smtp://hôte:port` soumise à OUTBOUND_ALLOWED_HOSTS À CHAQUE ENVOI (refus audité, jamais de connexion) ;
  - `starttls` : si la négociation échoue, l'envoi échoue, JAMAIS de repli en clair ; `tls` : TLS dès la connexion ;
    certificat toujours vérifié ; `none` : AUCUNE authentification n'est envoyée, et un état en base qui garde un
    identifiant ou un mot de passe avec `none` est refusé ;
  - le mot de passe SMTP est un secret lié à sa destination (déchiffré à l'instant de l'envoi, jamais journalisé) ;
  - adresses validées (syntaxe, ≤ 254, ni CR ni LF), message construit par `email.message.EmailMessage`, rien de
    saisi par un utilisateur dans les en-têtes hormis l'adresse du destinataire ; texte brut, en français ;
  - tous les liens sont construits sur PUBLIC_URL (jamais l'en-tête Host) ;
  - un échec journalise sans le lien ni le corps, et ne renvoie jamais de détail SMTP.
Deux modes (EF-4.8) : SYNCHRONE (invitation, e-mail de test, commandes CLI) et FILE D'ENVOI en mémoire (1 000 messages,
thread dédié) pour tout le reste : la requête ne dépend jamais de l'envoi. Perte au redémarrage acceptée et journalisée.
"""

import atexit
import logging
import queue
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Optional

from app import outbound
from app.auth import emails
from app.db import connect
from app.secrets_store import SecretUnavailable, SecretsError
from app.settings import effective

logger = logging.getLogger(__name__)

QUEUE_SIZE = 1000
SUBJECT_PREFIX = "[KmlViewer] "


class MailError(Exception):
    """Envoi impossible. Message générique : jamais de détail SMTP, d'hôte, de lien ni de secret."""

    def __init__(self):
        super().__init__("L'e-mail n'a pas pu être envoyé")


# -- Textes (EF-4.7) : français, texte brut, sujet fixe (aucune donnée utilisateur dans les en-têtes) ---------------------

def _note_line(inviter: str, note: Optional[str]) -> str:
    return f"\nMessage de {inviter} : {note}\n" if note else ""


def compose(kind: str, *, link: str = '', username: str = '', inviter: str = '', note: Optional[str] = None,
            detail: str = '', hours: Optional[int] = None) -> tuple:
    """(sujet, corps) d'un message. `kind` : invitation, verification, reset, bootstrap ou notification (voir NOTIFICATIONS)."""
    footer = "\nSi tu n'es pas à l'origine de cette demande, ignore ce message."
    if kind == 'invitation':
        return ("Invitation", f"Bonjour,\n\n{inviter} t'invite à créer un compte KmlViewer.\n{_note_line(inviter, note)}\n"
                "Pour choisir ton identifiant et ton mot de passe (lien à usage unique, limité dans le temps) :\n"
                f"{link}\n{footer}\n")
    if kind == 'bootstrap':
        return ("Création du compte administrateur", "Bonjour,\n\nPour créer le compte administrateur de cette instance "
                f"(lien à usage unique, valable 24 h) :\n{link}\n{footer}\n")
    if kind == 'verification':
        return ("Validation de ton adresse e-mail", f"Bonjour,\n\nTon compte « {username} » a été créé. Pour valider cette "
                f"adresse e-mail (lien à usage unique) :\n{link}\n\nSi tu n'as pas créé ce compte, ignore ce message.\n")
    if kind == 'reset':
        account = f" du compte « {username} »" if username else ""
        validity = f"valable {hours} h" if hours else "limité dans le temps"
        return ("Réinitialisation du mot de passe", f"Bonjour,\n\nPour choisir un nouveau mot de passe{account} "
                f"(lien à usage unique, {validity}) :\n{link}\n\nSi tu n'as rien demandé, préviens ton administrateur.\n")
    text = NOTIFICATIONS.get(kind)
    if text is None:
        raise ValueError(f"Type de message inconnu : {kind!r}")
    subject, body = text
    return (subject, f"Bonjour,\n\n{body}{(' ' + detail) if detail else ''}\n\nSi tu n'es pas à l'origine de ce changement, "
            "contacte un administrateur sans attendre.\n")


NOTIFICATIONS = {
    'password_changed': ("Ton mot de passe a changé", "Le mot de passe de ton compte KmlViewer vient d'être modifié."),
    'factor_added': ("Un second facteur a été ajouté", "Un second facteur d'authentification a été ajouté à ton compte."),
    'factor_removed': ("Un second facteur a été retiré", "Un second facteur d'authentification a été retiré de ton compte."),
    'factor_reset': ("Tes facteurs ont été réinitialisés", "Les seconds facteurs de ton compte ont été réinitialisés : "
                                                          "tu devras en enrôler un nouveau à ta prochaine connexion."),
    'recovery_used': ("Un code de secours a été utilisé", "Un code de secours de ton compte vient d'être utilisé."),
    'email_changed': ("Ton adresse e-mail change", "Une nouvelle adresse e-mail est en attente de validation pour ton compte ; "
                                                   "l'actuelle reste active tant qu'elle n'est pas validée."),
    'secrets_reset': ("Les secrets de l'instance vont être effacés", "Tous les secrets chiffrés (mot de passe SMTP, clés de "
                                                                     "services, secrets TOTP) sont en cours d'effacement par la "
                                                                     "ligne de commande : ils sont à ressaisir."),
    'smtp_reset': ("La configuration e-mail va être réinitialisée", "Les réglages SMTP de l'instance sont en cours de "
                                                                    "réinitialisation par la ligne de commande."),
}


def build_message(sender: str, to: str, subject: str, body: str) -> EmailMessage:
    """Message construit par EmailMessage (échappement des en-têtes) ; destinataire et expéditeur validés (EF-4.9)."""
    sender, to = validate_address(sender), validate_address(to)
    message = EmailMessage()
    message['From'] = sender
    message['To'] = to
    message['Subject'] = SUBJECT_PREFIX + subject
    message.set_content(body)
    return message


def validate_address(address) -> str:
    """Adresse unique, ≤ 254, sans espace ni CR/LF ni séparateur d'en-tête ; sinon ValueError. Forme canonique."""
    return emails.validate_email(address)                                         # validateur unique (app/auth/emails.py)


# -- Transport ------------------------------------------------------------------------------------------------------------

def smtp_settings(cfg) -> dict:
    return {key: effective(cfg, f'smtp.{key}') for key in ('host', 'port', 'security', 'username', 'sender', 'timeout_s')}


def configured(cfg) -> bool:
    """Hôte et expéditeur renseignés et destination autorisée : sinon aucun e-mail ne part."""
    s = smtp_settings(cfg)
    if not s['host'] or not s['sender']:
        return False
    try:
        return outbound.allowed(cfg, 'smtp', outbound.destination_of_smtp(s['host'], s['port']))
    except outbound.OutboundError:
        return False


def deliver(conn, cfg, secrets, message: EmailMessage) -> None:
    """
    Envoie UN message ou lève MailError (générique). Contrôle la destination juste avant de se connecter (refus audité), lit le
    mot de passe lié à la destination, n'envoie aucune authentification avec `none`.
    """
    s = smtp_settings(cfg)
    if not s['host'] or not s['sender']:
        raise MailError()
    try:
        destination = outbound.destination_of_smtp(s['host'], s['port'])
        outbound.guard(conn, cfg, 'smtp', destination)
        password = None
        if s['security'] == 'none':
            # Aucune authentification, et un état incohérent (identifiant ou secret gardé) est refusé net
            if s['username'] or (secrets is not None and secrets.status(conn, 'smtp.password')['configured']):
                logger.error("Envoi refusé : sécurité SMTP « none » avec un identifiant ou un mot de passe enregistré")
                raise MailError()
        elif s['username'] and secrets is not None:
            password = secrets.effective(conn, 'smtp.password', destination)
            if password is None:
                logger.error("Envoi refusé : mot de passe SMTP absent ou lié à une autre destination")
                raise MailError()
        context = ssl.create_default_context()                       # certificat et nom d'hôte toujours vérifiés
        timeout = float(s['timeout_s'])
        if s['security'] == 'tls':
            client = smtplib.SMTP_SSL(s['host'], s['port'], timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(s['host'], s['port'], timeout=timeout)
        with client:
            client.ehlo()
            if s['security'] == 'starttls':
                client.starttls(context=context)                      # non annoncé ou refusé : exception, jamais de clair
                client.ehlo()
            if password is not None:
                client.login(s['username'], password)
            client.send_message(message)
    except MailError:
        raise
    except outbound.DestinationRefused:
        raise MailError() from None
    except (smtplib.SMTPException, OSError, ssl.SSLError, ValueError, SecretUnavailable, SecretsError, outbound.OutboundError):
        logger.warning("Envoi SMTP en échec (détail non journalisé : il pourrait contenir un lien)")
        raise MailError() from None


class MailQueue:
    """File d'envoi en mémoire, bornée, traitée par un thread dédié ; `drain()` la vide de façon synchrone (tests)."""

    def __init__(self, send, max_size: int = QUEUE_SIZE):
        self._send = send
        self._queue: queue.Queue = queue.Queue(maxsize=max_size)
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self.dropped = 0
        self._inflight = None                     # message en cours d'envoi (comptabilisé à l'arrêt, E-16)

    def put(self, message) -> bool:
        """Met en file un message (EmailMessage) ou un TRAVAIL (fonction sans argument exécutée par le thread dédié)."""
        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            self.dropped += 1
            logger.error("File d'envoi pleine (%d messages) : message abandonné", self._queue.maxsize)
            return False

    def _process(self, message) -> None:
        try:
            if callable(message):
                message()
            else:
                self._send(message)
        except Exception:  # noqa: BLE001 - un message en échec ne tue pas le thread ; aucun détail (lien) journalisé
            logger.warning("Envoi différé en échec")

    def drain(self) -> int:
        count = 0
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                return count
            self._process(message)
            self._queue.task_done()
            count += 1

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name='mail-queue', daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            if self._stopping.is_set():
                return
            message = self._queue.get()
            try:
                if message is not None and self._stopping.is_set():
                    with self._queue.mutex:                   # non traité : remis en tête pour que l'arrêt le compte (E-16)
                        self._queue.queue.appendleft(message)
                    return
                if message is None:
                    return
                self._inflight = message
                self._process(message)
            finally:
                self._inflight = None
                self._queue.task_done()

    @staticmethod
    def _kind(message) -> str:
        """Type d'un message en file pour le journal : sujet FIXE (aucune adresse, aucun lien), ou « travail différé »."""
        if callable(message):
            return str(getattr(message, 'kind', None) or 'travail différé')[:80]
        try:
            return str(message['Subject'])[:80]
        except Exception:  # noqa: BLE001
            return 'message'

    def _log_lost(self) -> None:
        """E-16, CA-66 : une ligne WARNING si des messages sont perdus à l'arrêt ; rien si la file est vide."""
        with self._queue.mutex:
            waiting = [m for m in self._queue.queue if m is not None]
        if self._inflight is not None:
            waiting.append(self._inflight)
        if not waiting:
            return
        kinds: dict = {}
        for message in waiting:
            kinds[self._kind(message)] = kinds.get(self._kind(message), 0) + 1
        logger.warning("File d'envoi arrêtée avec %d message(s) non envoyé(s), perdus au redémarrage : %s", len(waiting),
                       ', '.join(f"{kind} ×{count}" for kind, count in sorted(kinds.items())))

    def stop(self) -> None:
        if self._thread is not None:
            self._stopping.set()                      # drapeau d'arrêt : ne dépend pas de la place dans la file
            try:
                self._queue.put_nowait(None)          # réveille un thread en attente sur une file vide
            except queue.Full:
                pass
            self._thread.join(timeout=0.5)               # thread démon : un envoi bloqué ne retient pas l'arrêt
            self._log_lost()
            self._thread = None

    def pending(self) -> int:
        return self._queue.qsize()

    def join(self, timeout: float = 5.0) -> bool:
        """Attend que le thread ait traité tout ce qui est en file (tests). Vrai s'il a fini dans le délai."""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            _time.sleep(0.01)
        return False


class Mailer:
    """Point d'entrée : `send` (synchrone), `queue` (différé), notifications. Un seul par application."""

    def __init__(self, app):
        self.app = app
        self.queue = MailQueue(self._send_queued)

    def _connection(self):
        return connect(self.app.config['DB_PATH'])

    @property
    def configured(self) -> bool:
        return configured(self.app.config)

    def _send_queued(self, message) -> None:
        conn = self._connection()
        try:
            deliver(conn, self.app.config, self.app.extensions.get('secrets'), message)
        finally:
            conn.close()

    @property
    def has_settings(self) -> bool:
        """Hôte et expéditeur renseignés (la destination est contrôlée, et son refus audité, à l'envoi par `deliver`)."""
        s = smtp_settings(self.app.config)
        return bool(s['host'] and s['sender'])

    def send(self, to: str, subject: str, body: str, *, conn=None) -> None:
        """SYNCHRONE : lève MailError si l'envoi échoue ou si l'e-mail n'est pas configuré."""
        if not self.has_settings:
            raise MailError()
        try:
            message = build_message(smtp_settings(self.app.config)['sender'], to, subject, body)
        except ValueError:
            raise MailError() from None
        own = conn is None
        conn = self._connection() if own else conn
        try:
            deliver(conn, self.app.config, self.app.extensions.get('secrets'), message)
        finally:
            if own:
                conn.close()

    def submit(self, job) -> bool:
        """Met en file un TRAVAIL différé (hors du thread de la requête) ; faux si la file est pleine."""
        return self.queue.put(job)

    def enqueue(self, to: str, subject: str, body: str) -> str:
        """DIFFÉRÉ : 'queued', 'not_configured', 'invalid' ou 'dropped'. Ne dépend jamais de l'envoi."""
        if not self.has_settings:
            return 'not_configured'
        try:
            message = build_message(smtp_settings(self.app.config)['sender'], to, subject, body)
        except ValueError:
            return 'invalid'
        return 'queued' if self.queue.put(message) else 'dropped'

    def send_test(self, to: str, timeout_s: Optional[float] = None) -> dict:
        """E-mail de test (EF-4.2) : réponse `{'ok': bool}` seulement, rendue à l'échéance `smtp.timeout_s`."""
        delay = timeout_s if timeout_s is not None else float(effective(self.app.config, 'smtp.timeout_s'))
        return outbound.run_neutral_test(lambda t: self.send(to, "E-mail de test", "Ceci est un e-mail de test.") or True, delay)


def init_mailer(app) -> Mailer:
    mailer = Mailer(app)
    app.extensions['mailer'] = mailer
    atexit.register(mailer.queue.stop)       # E-16 : à l'arrêt du processus la perte est journalisée ; sans thread : no-op
    return mailer


def start_queue(app) -> None:
    """Démarre le thread de la file dans les mêmes conditions que l'ordonnanceur (EF-10.5) ; jamais en CLI ni en test."""
    mailer = app.extensions.get('mailer')
    if mailer is not None:
        mailer.queue.start()
