"""In-memory WebAuthn authenticator for tests. Synthetic keys only."""

import hashlib
import json

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url, encode_cbor
from webauthn.helpers.cose import COSEAlgorithmIdentifier, COSECRV, COSEKTY


class SoftwareAuthenticator:
    def __init__(self, origin, rp_id):
        self.origin = origin
        self.rp_id = rp_id
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = hashlib.sha256(self.private_key.private_numbers().private_value.to_bytes(32, "big")).digest()
        self.sign_count = 0

    def _client_data(self, type_name, challenge_b64):
        payload = {
            "type": type_name,
            "challenge": challenge_b64,
            "origin": self.origin,
            "crossOrigin": False,
        }
        return json.dumps(payload, separators=(",", ":")).encode()

    def _cose_public_key(self):
        numbers = self.private_key.public_key().public_numbers()
        return encode_cbor(
            {
                1: COSEKTY.EC2,
                3: COSEAlgorithmIdentifier.ECDSA_SHA_256,
                -1: COSECRV.P256,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _auth_data(self, *, attested):
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        flags = 0x01 | 0x04  # user present + user verified
        if attested:
            flags |= 0x40
        body = rp_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        if attested:
            aaguid = bytes(16)
            cred_id = self.credential_id
            body += aaguid + len(cred_id).to_bytes(2, "big") + cred_id + self._cose_public_key()
        return body

    def _sign(self, auth_data, client_data):
        signed = auth_data + hashlib.sha256(client_data).digest()
        return self.private_key.sign(signed, ec.ECDSA(hashes.SHA256()))

    def create(self, options):
        parsed = json.loads(options) if isinstance(options, str) else options
        client_data = self._client_data("webauthn.create", parsed["challenge"])
        auth_data = self._auth_data(attested=True)
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        raw_id = bytes_to_base64url(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options):
        parsed = json.loads(options) if isinstance(options, str) else options
        self.sign_count += 1
        client_data = self._client_data("webauthn.get", parsed["challenge"])
        auth_data = self._auth_data(attested=False)
        raw_id = bytes_to_base64url(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(self._sign(auth_data, client_data)),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }
