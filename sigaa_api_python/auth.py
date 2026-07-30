import hashlib
import json
import os
import time
from pathlib import Path
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
CLIENTS_FILE = Path(os.environ.get('SIGAA_API_CLIENTS_FILE', 'authorized_clients.json'))
SIGNATURE_TTL_SECONDS = int(os.environ.get('SIGAA_API_SIGNATURE_TTL', '60'))
import base64
import urllib.request
import urllib.error

class ClientAuthError(Exception):
    ''


class StorageProvider:

    def load(self):
        raise NotImplementedError

    def save(self, keys):
        raise NotImplementedError

class LocalFileProvider(StorageProvider):

    def load(self):
        if not CLIENTS_FILE.exists():
            return {}
        try:
            with CLIENTS_FILE.open('r', encoding='utf-8') as f:
                data = json.load(f)
            return {k.lower(): v for k, v in data.items()}
        except json.JSONDecodeError:
            return {}

    def save(self, keys):
        CLIENTS_FILE.write_text(json.dumps(keys, indent=2), encoding='utf-8')

class GitHubProvider(StorageProvider):

    def __init__(self):
        self.repo = os.environ.get('GITHUB_REPO')
        self.path = os.environ.get('GITHUB_FILE_PATH')
        self.token = os.environ.get('GITHUB_TOKEN')
        if not all([self.repo, self.path, self.token]):
            raise ValueError('GitHubProvider requires GITHUB_REPO, GITHUB_FILE_PATH, and GITHUB_TOKEN')
        self.api_url = f'https://api.github.com/repos/{self.repo}/contents/{self.path}'

    def load(self):
        req = urllib.request.Request(self.api_url, headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github.v3+json'})
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                content = base64.b64decode(data['content']).decode('utf-8')
                self._sha = data['sha']
                if not content.strip():
                    return {}
                keys = json.loads(content)
                return {k.lower(): v for k, v in keys.items()}
        except json.JSONDecodeError:
            return {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._sha = None
                return {}
            raise

    def save(self, keys):
        content = json.dumps(keys, indent=2)
        encoded_content = base64.b64encode(content.encode('utf-8')).decode('utf-8')
        payload = {'message': 'Update API keys via SIGAA API', 'content': encoded_content}
        if hasattr(self, '_sha') and self._sha:
            payload['sha'] = self._sha
        req = urllib.request.Request(self.api_url, data=json.dumps(payload).encode('utf-8'), headers={'Authorization': f'Bearer {self.token}', 'Accept': 'application/vnd.github.v3+json', 'Content-Type': 'application/json'}, method='PUT')
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            self._sha = data['content']['sha']

class GenericHttpProvider(StorageProvider):

    def __init__(self):
        self.get_url = os.environ.get('SIGAA_API_CLIENTS_GET_URL')
        self.put_url = os.environ.get('SIGAA_API_CLIENTS_PUT_URL')
        if not self.get_url or not self.put_url:
            raise ValueError('GenericHttpProvider requires SIGAA_API_CLIENTS_GET_URL and SIGAA_API_CLIENTS_PUT_URL')

    def load(self):
        req = urllib.request.Request(self.get_url)
        try:
            with urllib.request.urlopen(req) as response:
                content = response.read().decode('utf-8')
                if not content.strip():
                    return {}
                keys = json.loads(content)
                return {k.lower(): v for k, v in keys.items()}
        except json.JSONDecodeError:
            return {}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            raise

    def save(self, keys):
        payload = json.dumps(keys, indent=2).encode('utf-8')
        req = urllib.request.Request(self.put_url, data=payload, method='PUT')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req) as response:
            pass

def _get_provider():
    storage_type = os.environ.get('SIGAA_API_STORAGE_TYPE', 'local').lower()
    if storage_type == 'github':
        return GitHubProvider()
    elif storage_type == 'http':
        return GenericHttpProvider()
    return LocalFileProvider()
_storage_provider = None
_KEYS_CACHE_TTL = float(os.environ.get('SIGAA_API_KEYS_CACHE_TTL', '15'))
_keys_cache = None
_keys_cache_at = 0.0

def _load_authorized_keys(force: bool=False):
    global _storage_provider, _keys_cache, _keys_cache_at
    if _storage_provider is None:
        _storage_provider = _get_provider()
    now = time.monotonic()
    if not force and _keys_cache is not None and (now - _keys_cache_at < _KEYS_CACHE_TTL):
        return _keys_cache
    _keys_cache = _storage_provider.load()
    _keys_cache_at = now
    return _keys_cache

def _invalidate_keys_cache():
    global _keys_cache, _keys_cache_at
    _keys_cache = None
    _keys_cache_at = 0.0

def _save_authorized_keys(keys):
    global _storage_provider
    if _storage_provider is None:
        _storage_provider = _get_provider()
    _storage_provider.save(keys)
    _invalidate_keys_cache()

def is_authorized(public_key_hex):
    return public_key_hex.lower() in _load_authorized_keys()

def register_client(public_key_hex, name):
    keys = _load_authorized_keys()
    keys[public_key_hex.lower()] = {'name': name, 'added_at': int(time.time())}
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
    body_hash = hashlib.sha256(body or b'').hexdigest()
    return f'{method.upper()}|{path}|{timestamp}|{body_hash}'.encode('utf-8')

def verify_request(public_key_hex, signature_hex, timestamp, method, path, body):
    if not is_authorized(public_key_hex):
        raise ClientAuthError('Unknown or unauthorized client public key.')
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        signature_bytes = bytes.fromhex(signature_hex or '')
    except ValueError:
        raise ClientAuthError('Public key and signature must be hexadecimal.')
    if len(public_key_bytes) != 32:
        raise ClientAuthError('Invalid Ed25519 public key length.')
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise ClientAuthError('Missing or invalid X-Timestamp header.')
    if abs(int(time.time()) - ts) > SIGNATURE_TTL_SECONDS:
        raise ClientAuthError('Request timestamp expired or out of sync.')
    message = _canonical_message(method, path, ts, body)
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(signature_bytes, message)
    except InvalidSignature:
        raise ClientAuthError('Signature verification failed.')