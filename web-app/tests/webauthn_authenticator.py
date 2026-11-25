"""Authentificateur WebAuthn VIRTUEL pour les tests (clé EC P-256, attestation `none`) : fabrique les réponses d'un navigateur."""

import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


class VirtualAuthenticator:
    def __init__(self, rp_id: str, origin: str, counter: int = 0):
        self.rp_id, self.origin, self.counter = rp_id, origin, counter
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)

    def _cose(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, 'big'), -3: numbers.y.to_bytes(32, 'big')})

    def create(self, options_json, *, origin=None, rp_id=None, challenge=None, transports=('usb',)) -> dict:
        """Réponse à `navigator.credentials.create` pour des options JSON du serveur."""
        options = json.loads(options_json)
        client = json.dumps({'type': 'webauthn.create', 'challenge': challenge or options['challenge'],
                             'origin': origin or self.origin, 'crossOrigin': False}).encode()
        rp_hash = hashlib.sha256((rp_id or self.rp_id).encode()).digest()
        attested = (b'\x00' * 16) + struct.pack('>H', len(self.credential_id)) + self.credential_id + self._cose()
        auth_data = rp_hash + bytes([0x45]) + struct.pack('>I', self.counter) + attested
        attestation = cbor2.dumps({'fmt': 'none', 'attStmt': {}, 'authData': auth_data})
        return {'id': b64(self.credential_id), 'rawId': b64(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': b64(client), 'attestationObject': b64(attestation), 'transports': list(transports)},
                'clientExtensionResults': {}}

    def get(self, options_json, *, origin=None, rp_id=None, challenge=None, counter=None) -> dict:
        """Réponse à `navigator.credentials.get` ; le compteur s'incrémente sauf valeur imposée."""
        options = json.loads(options_json)
        self.counter = self.counter + 1 if counter is None else counter
        client = json.dumps({'type': 'webauthn.get', 'challenge': challenge or options['challenge'],
                             'origin': origin or self.origin, 'crossOrigin': False}).encode()
        auth_data = hashlib.sha256((rp_id or self.rp_id).encode()).digest() + bytes([0x05]) + struct.pack('>I', self.counter)
        signature = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {'id': b64(self.credential_id), 'rawId': b64(self.credential_id), 'type': 'public-key',
                'response': {'clientDataJSON': b64(client), 'authenticatorData': b64(auth_data), 'signature': b64(signature),
                             'userHandle': None}, 'clientExtensionResults': {}}
