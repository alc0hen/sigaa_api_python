
import hashlib
import json
import os
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

CLIENTS_FILE = Path(os.environ.get("SIGAA_API_CLIENTS_FILE", "authorized_clients.json"))
SIGNATURE_TTL_SECONDS = int(os.environ.get("SIGAA_API_SIGNATURE_TTL", "60"))


class ClientAuthError(Exception):
    """"""

def _load_authorized_keys():
    if not CLIENTS_FILE.exists():
        return {}
    with CLIENTS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k.lower(): v for k, v in data.items()}


def _save_authorized_keys(keys):
    CLIENTS_FILE.write_text(json.dumps(keys, indent=2), encoding="utf-8")


def is_authorized(public_key_hex):
    return public_key_hex.lower() in _load_authorized_keys()


def register_client(public_key_hex, name):
    keys = _load_authorized_keys()
    keys[public_key_hex.lower()] = {"name": name, "added_at": int(time.time())}
    _save_authorized_keys(keys)


def revoke_client(public_key_hex):
    keys = _load_authorized_keys()
    pk = public_key_hex.lower()
    if pk not in keys:
        return False
    del keys[pk]
    _save_authorized_keys(keys)
    return True


def list_clients():
    return _load_authorized_keys()


def _canonical_message(method, path, timestamp, body):
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{method.upper()}|{path}|{timestamp}|{body_hash}".encode("utf-8")


def verify_request(public_key_hex, signature_hex, timestamp, method, path, body):

    if not is_authorized(public_key_hex):
        raise ClientAuthError("Unknown or unauthorized client public key.")

    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        signature_bytes = bytes.fromhex(signature_hex or "")
    except ValueError:
        raise ClientAuthError("Public key and signature must be hexadecimal.")

    if len(public_key_bytes) != 32:
        raise ClientAuthError("Invalid Ed25519 public key length.")

    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise ClientAuthError("Missing or invalid X-Timestamp header.")

    if abs(int(time.time()) - ts) > SIGNATURE_TTL_SECONDS:
        raise ClientAuthError("Request timestamp expired or out of sync.")

    message = _canonical_message(method, path, ts, body)
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, message)
    except InvalidSignature:
        raise ClientAuthError("Signature verification failed.")
