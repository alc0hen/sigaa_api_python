import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from fastapi import FastAPI
from starlette.responses import JSONResponse

from .exceptions import SigaaException, SigaaInvalidCredentials, SigaaQuestionnaireError, SigaaSessionExpired
from .sigaa import Sigaa
from .enums import InstitutionType

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

SESSION_TTL_SECONDS = int(os.environ.get('SIGAA_API_SESSION_TTL', '900'))
CLEANUP_INTERVAL_SECONDS = 60
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
WORKER_ID = os.environ.get('SIGAA_WORKER_ID') or uuid.uuid4().hex[:8]
HEARTBEAT_INTERVAL = 30
RESULT_TTL = 120
WS_URL = os.environ.get('WS_URL')
NUM_WORKERS = int(os.environ.get('SIGAA_NUM_WORKERS', '9'))

SHARED_QUEUE = 'sigaa:tasks'
WORKER_QUEUE = f'sigaa:worker:{WORKER_ID}:tasks'
HEARTBEAT_KEY = f'sigaa:worker:{WORKER_ID}:heartbeat'

# ─── Logging ──────────────────────────────────────────────────────────────────
import sys
SYSTEM_LOG_FILE = os.path.join(os.path.dirname(__file__), 'system.log')
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.FileHandler(SYSTEM_LOG_FILE, mode='a', encoding='utf-8')
file_handler.setFormatter(formatter)
logging.getLogger().addHandler(file_handler)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logging.getLogger().addHandler(stream_handler)

logging.getLogger().setLevel(logging.INFO)

# ─── State ────────────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}
_sessions_lock = asyncio.Lock()
_redis: aioredis.Redis = None


# ─── Worker Error ─────────────────────────────────────────────────────────────

class WorkerError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


# ─── Session helpers (preserved from original interface.py) ───────────────────

async def _get_session(session_id: str) -> dict:
    """Retrieve a session by ID, updating its last-used timestamp."""
    async with _sessions_lock:
        session = _sessions.get(session_id)
    if not session:
        raise WorkerError(404, 'session_not_found', 'Session not found.')
    session['last_used'] = time.time()
    return session


def _bond_summary(bond_id: str, status: str, bond) -> dict:
    if hasattr(bond, 'registration'):
        return {'bond_id': bond_id, 'status': status, 'type': 'student',
                'registration': bond.registration, 'program': bond.program}
    return {'bond_id': bond_id, 'status': status, 'type': 'teacher'}


def _all_bond_summaries(account) -> list[dict]:
    bonds = [_bond_summary(f'active:{i}', 'active', b)
             for i, b in enumerate(account.active_bonds)]
    bonds += [_bond_summary(f'inactive:{i}', 'inactive', b)
              for i, b in enumerate(account.inactive_bonds)]
    return bonds


def _find_bond(account, bond_id: str):
    try:
        status, index_str = bond_id.split(':', 1)
        index = int(index_str)
    except ValueError:
        raise WorkerError(400, 'malformed_bond_id', 'Malformed bond_id.')
    bonds = (account.active_bonds if status == 'active'
             else account.inactive_bonds if status == 'inactive'
             else None)
    if bonds is None or not 0 <= index < len(bonds):
        raise WorkerError(404, 'bond_not_found', 'Bond not found.')
    return bonds[index]


def _student_bond(session: dict, bond_id: str):
    bond = _find_bond(session['account'], bond_id)
    if not hasattr(bond, 'get_courses'):
        raise WorkerError(400, 'teacher_bond', 'Teacher bonds expose no student data.')
    return bond


async def _load_courses(session: dict, bond_id: str, bond, refresh: bool = False):
    cache = session.setdefault('courses', {})
    if refresh or bond_id not in cache:
        cache[bond_id] = await bond.get_courses()
    return cache[bond_id]


async def _assert_sigaa_alive(session: dict) -> None:
    page = await session['sigaa'].session.get('/sigaa/portais/discente/discente.jsf')
    path = str(getattr(page.url, 'path', page.url))
    if 'login' in path:
        raise WorkerError(409, 'sigaa_session_expired', 'The SIGAA session has expired.')


# ─── Task handlers ────────────────────────────────────────────────────────────

async def _handle_create_session(payload: dict) -> dict:
    institution_str = (payload.get('institution') or '').upper()
    try:
        institution = InstitutionType[institution_str]
    except KeyError:
        raise WorkerError(400, 'unknown_institution', f"Unknown institution '{institution_str}'.")

    sigaa = Sigaa(payload['url'], institution)
    try:
        account = await sigaa.login(payload['username'], payload['password'])
    except SigaaInvalidCredentials:
        await sigaa.close()
        raise WorkerError(401, 'invalid_credentials', 'Invalid SIGAA credentials.')
    except SigaaQuestionnaireError as e:
        await sigaa.close()
        raise WorkerError(403, 'questionnaire', str(e))
    except (SigaaException, ValueError) as e:
        await sigaa.close()
        raise WorkerError(502, 'sigaa_error', str(e))

    session_id = uuid.uuid4().hex
    async with _sessions_lock:
        _sessions[session_id] = {
            'sigaa': sigaa,
            'account': account,
            'last_used': time.time(),
            'credentials': {
                'username': payload['username'],
                'password': payload['password'],
                'url': payload['url'],
                'inst_type': institution
            },
            'courses': {},
            'enrollment': {}
        }

    # Store session → worker mapping in Redis for routing
    await _redis.set(f'sigaa:session:{session_id}:worker', WORKER_ID, ex=SESSION_TTL_SECONDS)

    name = await account.get_name()
    return {
        'session_id': session_id,
        'name': name,
        'bonds': _all_bond_summaries(account),
        'worker_id': WORKER_ID
    }


async def _handle_list_bonds(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    return {'bonds': _all_bond_summaries(session['account'])}


async def _handle_list_courses(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    courses = await _load_courses(session, payload['bond_id'], bond, refresh=True)
    return {'courses': [{'id': i, 'title': c.title, 'schedule_code': c.schedule_code}
                        for i, c in enumerate(courses)]}


async def _handle_course_details(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    courses = await _load_courses(session, payload['bond_id'], bond)
    course_id = payload['course_id']
    if not 0 <= course_id < len(courses):
        raise WorkerError(404, 'course_not_found', 'Course not found.')
    course = courses[course_id]
    grades, frequency, professor = await course.get_all_details()
    return {
        'id': course_id,
        'title': course.title,
        'schedule_code': course.schedule_code,
        'grades': grades,
        'frequency': frequency,
        'professor': professor
    }


async def _handle_history(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    await _assert_sigaa_alive(session)
    credentials = session['credentials'] if payload.get('parallel', True) else None
    history = await bond.get_history(
        cached_history=payload.get('cached_history'),
        credentials=credentials
    )
    return {'history': history}


async def _handle_enrollment(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    await _assert_sigaa_alive(session)
    result = await bond.get_enrollment_disciplines()
    session.setdefault('enrollment', {})[payload['bond_id']] = {
        'view_state': result.get('view_state'),
        'action_url': result.get('action_url')
    }
    return {'levels': result.get('levels'), 'view_state': result.get('view_state')}


async def _handle_enrollment_selection(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    selected_class_ids = payload.get('selected_class_ids', [])
    if not selected_class_ids:
        raise WorkerError(400, 'no_classes_selected', 'No classes selected.')
    state = session.setdefault('enrollment', {}).get(payload['bond_id'], {})
    view_state = payload.get('view_state') or state.get('view_state')
    if not view_state:
        raise WorkerError(409, 'enrollment_not_started', 'Fetch the enrollment listing first.')
    page = await bond.submit_enrollment(selected_class_ids, view_state,
                                         action_url=state.get('action_url'))
    state.update({'confirm_view_state': page.view_state, 'confirm_body': page.body})
    session['enrollment'][payload['bond_id']] = state
    return {'html': page.body, 'view_state': page.view_state}


async def _handle_enrollment_confirm(payload: dict) -> dict:
    session = await _get_session(payload['session_id'])
    bond = _student_bond(session, payload['bond_id'])
    state = session.setdefault('enrollment', {}).get(payload['bond_id'], {})
    confirm_view_state = state.get('confirm_view_state')
    if not confirm_view_state:
        raise WorkerError(409, 'enrollment_not_submitted', 'Submit the class selection first.')
    password_page = await bond.request_confirmation_page(confirm_view_state)
    final_page = await bond.confirm_enrollment(payload['password'],
                                                password_page.view_state,
                                                password_page.body)
    return {'html': final_page.body}


async def _handle_close_session(payload: dict) -> dict:
    session_id = payload['session_id']
    async with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if not session:
        raise WorkerError(404, 'session_not_found', 'Session not found.')
    await session['sigaa'].close()
    try:
        await _redis.delete(f'sigaa:session:{session_id}:worker')
    except Exception:
        pass
    return {'status': 'closed'}


# ─── Action routing table ────────────────────────────────────────────────────

HANDLERS = {
    'create_session': _handle_create_session,
    'list_bonds': _handle_list_bonds,
    'list_courses': _handle_list_courses,
    'course_details': _handle_course_details,
    'history': _handle_history,
    'enrollment': _handle_enrollment,
    'enrollment_selection': _handle_enrollment_selection,
    'enrollment_confirm': _handle_enrollment_confirm,
    'close_session': _handle_close_session,
}


# ─── Background loops ────────────────────────────────────────────────────────

async def _worker_loop():
    """Main worker loop — listens for tasks on Redis queues via BRPOP."""
    logger.info('Worker %s started. Listening on queues: %s, %s',
                WORKER_ID, WORKER_QUEUE, SHARED_QUEUE)
    while True:
        try:
            # Polling inteligente com RPOP para evitar TimeoutError na ponte WebSocket
            result = None
            for queue in [WORKER_QUEUE, SHARED_QUEUE]:
                raw_task = await _redis.rpop(queue)
                if raw_task:
                    result = (queue, raw_task)
                    break

            if result is None:
                await asyncio.sleep(0.05)
                continue

            _queue_name, raw_task = result
            task = json.loads(raw_task)
            task_id = task['task_id']
            action = task['action']
            payload = task.get('payload', {})

            logger.info('Worker %s: Processing task %s (action=%s)', WORKER_ID, task_id, action)

            handler = HANDLERS.get(action)
            if not handler:
                response = {
                    'task_id': task_id,
                    'success': False,
                    'error': {'code': 'unknown_action',
                              'detail': f'Unknown action: {action}',
                              'status_code': 400},
                    'worker_id': WORKER_ID
                }
            else:
                try:
                    data = await handler(payload)
                    response = {
                        'task_id': task_id,
                        'success': True,
                        'data': data,
                        'worker_id': WORKER_ID
                    }
                except WorkerError as e:
                    response = {
                        'task_id': task_id,
                        'success': False,
                        'error': {'code': e.code, 'detail': e.message,
                                  'status_code': e.status_code},
                        'worker_id': WORKER_ID
                    }
                except SigaaQuestionnaireError as e:
                    response = {
                        'task_id': task_id,
                        'success': False,
                        'error': {'code': 'questionnaire', 'detail': str(e),
                                  'status_code': 403},
                        'worker_id': WORKER_ID
                    }
                except SigaaSessionExpired as e:
                    response = {
                        'task_id': task_id,
                        'success': False,
                        'error': {'code': 'sigaa_session_expired', 'detail': str(e),
                                  'status_code': 409},
                        'worker_id': WORKER_ID
                    }
                except (SigaaException, ValueError) as e:
                    response = {
                        'task_id': task_id,
                        'success': False,
                        'error': {'code': 'sigaa_error', 'detail': str(e),
                                  'status_code': 502},
                        'worker_id': WORKER_ID
                    }
                except Exception as e:
                    logger.error('Worker %s: Unexpected error on task %s: %s',
                                 WORKER_ID, task_id, e, exc_info=True)
                    response = {
                        'task_id': task_id,
                        'success': False,
                        'error': {'code': 'internal_error', 'detail': str(e),
                                  'status_code': 500},
                        'worker_id': WORKER_ID
                    }

            # Publish result to Redis
            result_key = f'sigaa:result:{task_id}'
            await _redis.lpush(result_key, json.dumps(response))
            await _redis.expire(result_key, RESULT_TTL)

            logger.info('Worker %s: Task %s completed (success=%s)',
                        WORKER_ID, task_id, response.get('success'))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error('Worker %s: Loop error: %s', WORKER_ID, e, exc_info=True)
            await asyncio.sleep(1)


async def _cleanup_loop():
    """Periodically clean up expired SIGAA sessions."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cutoff = time.time() - SESSION_TTL_SECONDS
        async with _sessions_lock:
            expired = [sid for sid, s in _sessions.items() if s['last_used'] < cutoff]
            for sid in expired:
                session = _sessions.pop(sid)
                try:
                    await session['sigaa'].close()
                except Exception:
                    logger.exception('Error closing expired session %s', sid)
                try:
                    await _redis.delete(f'sigaa:session:{sid}:worker')
                except Exception:
                    pass
            if expired:
                logger.info('Expired %d idle session(s).', len(expired))


async def _heartbeat_loop():
    """Periodically update heartbeat so the app principal knows this worker is alive."""
    while True:
        try:
            await _redis.set(HEARTBEAT_KEY, json.dumps({
                'worker_id': WORKER_ID,
                'sessions': len(_sessions),
                'timestamp': int(time.time())
            }), ex=HEARTBEAT_INTERVAL * 3)
        except Exception:
            pass
        await asyncio.sleep(HEARTBEAT_INTERVAL)


# ─── FastAPI app (minimal, for Render keep-alive) ────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _redis

    bridge_task = None
    if WS_URL:
        bridge_task = asyncio.create_task(_start_redis_bridge())
        # Dar um pequeno delay para a ponte TCP iniciar
        await asyncio.sleep(1)
    else:
        logger.info('WS_URL not set — Redis bridge disabled (using direct Redis connection)')

    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)

    # Spawn pool of concurrent workers
    worker_tasks = [asyncio.create_task(_worker_loop()) for _ in range(NUM_WORKERS)]
    cleanup_task = asyncio.create_task(_cleanup_loop())
    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    logger.info('SIGAA Worker %s online (%d concurrent loops). Redis: %s',
                WORKER_ID, NUM_WORKERS, REDIS_URL)

    try:
        yield
    finally:
        for t in worker_tasks:
            t.cancel()
        cleanup_task.cancel()
        heartbeat_task.cancel()
        if bridge_task:
            bridge_task.cancel()

        # Close all active sessions gracefully
        async with _sessions_lock:
            for session in _sessions.values():
                try:
                    await session['sigaa'].close()
                except Exception:
                    logger.exception('Error closing session during shutdown.')
            _sessions.clear()

        await _redis.close()
        logger.info('Worker %s shut down.', WORKER_ID)


app = FastAPI(title='SIGAA Worker', lifespan=lifespan)


@app.get('/healthz')
async def healthz():
    return {
        'status': 'ok',
        'worker_id': WORKER_ID,
        'active_sessions': len(_sessions)
    }


@app.get('/')
async def root():
    return {
        'service': 'SIGAA Worker',
        'worker_id': WORKER_ID,
        'active_sessions': len(_sessions)
    }


# ─── Redis WebSocket Bridge (ponte para Render) ─────────────────────────────

async def _handle_bridge_client(reader, writer):
    """Proxy TCP ↔ WebSocket para o Redis remoto via WS."""
    import websockets
    try:
        async with websockets.connect(WS_URL) as ws:
            async def tcp_to_ws():
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    await ws.send(data)

            async def ws_to_tcp():
                async for msg in ws:
                    writer.write(msg if isinstance(msg, bytes) else msg.encode())
                    await writer.drain()

            await asyncio.gather(tcp_to_ws(), ws_to_tcp())
    except Exception:
        pass
    finally:
        writer.close()


async def _start_redis_bridge():
    """Inicia a ponte local TCP:6379 → WS_URL para acesso ao Redis remoto."""
    try:
        server = await asyncio.start_server(_handle_bridge_client, '127.0.0.1', 6379)
        logger.info('🚀 Ponte local do Redis ativa em 127.0.0.1:6379 -> %s', WS_URL)
        async with server:
            await server.serve_forever()
    except OSError as e:
        logger.warning('⚠️ Não foi possível iniciar a ponte do Redis (porta 6379 já em uso?): %s', e)


# ─── Runner ──────────────────────────────────────────────────────────────────

async def _run():
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    config = Config()
    config.bind = [os.environ.get('SIGAA_API_BIND', '0.0.0.0:8000')]
    await serve(app, config)


if __name__ == '__main__':
    asyncio.run(_run())