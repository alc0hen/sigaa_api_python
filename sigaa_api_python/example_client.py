import hashlib
import json
import logging
import os
import sys
import time
import plotext as plt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('SigaaScraper')
PRIVATE_KEY_HEX = 'YOUR_PRIVATE_KEY'
DEFAULT_BASE_URL = 'http://127.0.0.1:8000'
METRICAS_TEMPO = {}

class ApiError(Exception):

    def __init__(self, status_code, code, message):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f'{status_code} {code}: {message}')

class SigaaApiClient:

    def __init__(self, base_url: str, private_key_hex: str):
        self.base_url = base_url.rstrip('/')
        self._private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex))
        self.public_key_hex = self._private_key.public_key().public_bytes_raw().hex()

    def _signed_headers(self, method: str, path: str, body: bytes) -> dict:
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body or b'').hexdigest()
        message = f'{method.upper()}|{path}|{timestamp}|{body_hash}'.encode('utf-8')
        signature = self._private_key.sign(message)
        return {'X-Signature': signature.hex(), 'X-Timestamp': timestamp, 'Content-Type': 'application/json'}

    def _request(self, method: str, suffix: str, json_body: dict=None) -> dict:
        path = f'/api/v1/{self.public_key_hex}{suffix}'
        body = json.dumps(json_body).encode('utf-8') if json_body is not None else b''
        headers = self._signed_headers(method, path, body)
        t0 = time.perf_counter()
        response = requests.request(method, self.base_url + path, data=body if json_body is not None else None, headers=headers)
        elapsed = time.perf_counter() - t0
        logger.info(f'HTTP {method} {suffix} -> Status {response.status_code} ({elapsed:.2f}s)')
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            raise ApiError(response.status_code, payload.get('code'), payload.get('detail') or response.text)
        return response.json() if response.content else {}

    def healthz(self) -> dict:
        t0 = time.perf_counter()
        response = requests.get(self.base_url + '/healthz')
        elapsed = time.perf_counter() - t0
        logger.info(f'HTTP GET /healthz -> Status {response.status_code} ({elapsed:.2f}s)')
        response.raise_for_status()
        return response.json()

    def create_session(self, url: str, institution: str, username: str, password: str) -> dict:
        return self._request('POST', '/sessions', {'url': url, 'institution': institution, 'username': username, 'password': password})

    def list_bonds(self, session_id: str) -> dict:
        return self._request('GET', f'/sessions/{session_id}/bonds')

    def list_courses(self, session_id: str, bond_id: str) -> dict:
        return self._request('GET', f'/sessions/{session_id}/bonds/{bond_id}/courses')

    def course_details(self, session_id: str, bond_id: str, course_id: int) -> dict:
        return self._request('GET', f'/sessions/{session_id}/bonds/{bond_id}/courses/{course_id}/details')

    def history(self, session_id: str, bond_id: str, cached_history: dict=None, parallel: bool=True) -> dict:
        return self._request('POST', f'/sessions/{session_id}/bonds/{bond_id}/history', {'cached_history': cached_history, 'parallel': parallel})

    def enrollment_disciplines(self, session_id: str, bond_id: str) -> dict:
        return self._request('GET', f'/sessions/{session_id}/bonds/{bond_id}/enrollment')

    def close_session(self, session_id: str) -> dict:
        return self._request('DELETE', f'/sessions/{session_id}')

def _first_active_student_bond(bonds: list) -> dict | None:
    for bond in bonds:
        if bond.get('status') == 'active' and bond.get('type') == 'student':
            return bond
    return None

def run_read_only_walkthrough(client: SigaaApiClient, url, institution, username, password):
    t0 = time.perf_counter()
    logger.info('Executando health check...')
    client.healthz()
    METRICAS_TEMPO['0. Health Check'] = time.perf_counter() - t0
    t0 = time.perf_counter()
    logger.info('Iniciando login no SIGAA...')
    session_data = client.create_session(url, institution, username, password)
    session_id = session_data['session_id']
    logger.info(f"Sessão criada com sucesso. Nome: {session_data.get('name')}")
    METRICAS_TEMPO['1. Login (Session)'] = time.perf_counter() - t0
    t0 = time.perf_counter()
    logger.info('Buscando vínculos discentes...')
    bonds = client.list_bonds(session_id)['bonds']
    bond = _first_active_student_bond(bonds)
    METRICAS_TEMPO['2. Listar Vínculos'] = time.perf_counter() - t0
    if not bond:
        logger.warning('Nenhum vínculo discente ativo encontrado.')
        return (session_id, None)
    bond_id = bond['bond_id']
    t0 = time.perf_counter()
    logger.info(f'Buscando disciplinas do vínculo {bond_id}...')
    courses = client.list_courses(session_id, bond_id)['courses']
    METRICAS_TEMPO['3. Listar Disciplinas'] = time.perf_counter() - t0
    if courses:
        t0 = time.perf_counter()
        course_id = courses[0]['id']
        logger.info(f'Extraindo detalhes da disciplina {course_id}...')
        client.course_details(session_id, bond_id, course_id)
        METRICAS_TEMPO['4. Detalhes Disciplina'] = time.perf_counter() - t0
    t0 = time.perf_counter()
    logger.info('Extraindo histórico completo (sem cache)...')
    history_result = client.history(session_id, bond_id, cached_history=None, parallel=True)
    history = history_result['history']
    METRICAS_TEMPO['5. Histórico (Sem Cache)'] = time.perf_counter() - t0
    t0 = time.perf_counter()
    logger.info('Extraindo histórico (utilizando cache)...')
    client.history(session_id, bond_id, cached_history=history, parallel=True)
    METRICAS_TEMPO['5b. Histórico (Com Cache)'] = time.perf_counter() - t0
    t0 = time.perf_counter()
    logger.info('Verificando turmas em oferta de matrícula...')
    try:
        client.enrollment_disciplines(session_id, bond_id)
    except ApiError as e:
        logger.warning(f'Matrícula indisponível: {e.message}')
    METRICAS_TEMPO['6. Turmas em Oferta'] = time.perf_counter() - t0
    return (session_id, bond_id)

def plot_metrics(metricas: dict):
    if not metricas:
        return
    etapas = list(metricas.keys())
    tempos = list(metricas.values())
    print('\n' + '=' * 60)
    print('        MÉTRICAS DE DESEMPENHO DO SCRAPING (em segundos)')
    print('=' * 60)
    plt.clf()
    plt.bar(etapas, tempos, color='green')
    plt.title('Tempo de Resposta por Etapa do Scraping')
    plt.xlabel('Etapa')
    plt.ylabel('Segundos')
    plt.show()

def main():
    base_url = sys.argv[1] if len(sys.argv) > 1 and (not sys.argv[1].startswith('--')) else DEFAULT_BASE_URL
    url = os.environ.get('SIGAA_DEMO_URL', 'https://sigaa.sig.ufal.br/')
    institution = os.environ.get('SIGAA_DEMO_INSTITUTION', 'UFAL')
    username = os.environ.get('SIGAA_DEMO_USERNAME', 'demo')
    password = os.environ.get('SIGAA_DEMO_PASSWORD', 'demo')
    try:
        client = SigaaApiClient(base_url, PRIVATE_KEY_HEX)
    except ValueError as e:
        logger.error(f'PRIVATE_KEY_HEX inválida ({e}).')
        sys.exit(1)
    logger.info(f'Cliente iniciado. Chave pública: {client.public_key_hex}')
    session_id = None
    try:
        session_id, _ = run_read_only_walkthrough(client, url, institution, username, password)
    except ApiError as e:
        logger.error(f'Erro da API: status={e.status_code} code={e.code} detail={e.message}')
    except requests.exceptions.ConnectionError as e:
        logger.error(f'Não foi possível conectar em {base_url}: {e}')
    finally:
        if session_id:
            logger.info('Encerrando sessão no SIGAA...')
            try:
                client.close_session(session_id)
                logger.info('Sessão encerrada com sucesso.')
            except ApiError as e:
                logger.error(f'Falha ao encerrar a sessão: {e}')
        plot_metrics(METRICAS_TEMPO)
if __name__ == '__main__':
    main()