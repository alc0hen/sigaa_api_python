
import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import auth
from .exceptions import (
    SigaaException,
    SigaaInvalidCredentials,
    SigaaQuestionnaireError,
    SigaaSessionExpired,
)
from .sigaa import Sigaa
from .enums import InstitutionType

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = int(os.environ.get("SIGAA_API_SESSION_TTL", "900"))
CLEANUP_INTERVAL_SECONDS = 60
_sessions: dict[str, dict] = {}
_sessions_lock = asyncio.Lock()


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail=message)
        self.code = code


async def _cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        cutoff = time.time() - SESSION_TTL_SECONDS
        async with _sessions_lock:
            expired = [sid for sid, s in _sessions.items() if s["last_used"] < cutoff]
            for sid in expired:
                session = _sessions.pop(sid)
                try:
                    await session["sigaa"].close()
                except Exception:
                    logger.exception("Error closing expired session %s", sid)
            if expired:
                logger.info("Expired %d idle session(s).", len(expired))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        async with _sessions_lock:
            for session in _sessions.values():
                try:
                    await session["sigaa"].close()
                except Exception:
                    logger.exception("Error closing session during shutdown.")
            _sessions.clear()


app = FastAPI(title="SIGAA API Interface", lifespan=lifespan)

API_PREFIX = "/api/v1/"


@app.exception_handler(ApiError)
async def _api_error_handler(_request: Request, exc: ApiError):
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status_code)


class SignedRequestMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(API_PREFIX):
            return await call_next(request)

        client_public_key = path[len(API_PREFIX):].split("/", 1)[0]
        body = await request.body()
        try:
            auth.verify_request(
                public_key_hex=client_public_key,
                signature_hex=request.headers.get("X-Signature"),
                timestamp=request.headers.get("X-Timestamp"),
                method=request.method,
                path=path,
                body=body,
            )
        except auth.ClientAuthError as e:
            return JSONResponse({"detail": str(e)}, status_code=401)

        request.state.client_public_key = client_public_key.lower()
        return await call_next(request)


app.add_middleware(SignedRequestMiddleware)


async def _get_owned_session(session_id: str, client_public_key: str) -> dict:
    async with _sessions_lock:
        session = _sessions.get(session_id)
    if not session or session["client_public_key"] != client_public_key:
        raise ApiError(404, "session_not_found", "Session not found.")
    session["last_used"] = time.time()
    return session


@asynccontextmanager
async def _scraper_errors():
    try:
        yield
    except SigaaQuestionnaireError as e:
        raise ApiError(403, "questionnaire", str(e))
    except SigaaSessionExpired as e:
        raise ApiError(409, "sigaa_session_expired", str(e))
    except (SigaaException, ValueError) as e:
        raise ApiError(502, "sigaa_error", str(e))


async def _assert_sigaa_alive(session: dict) -> None:
    async with _scraper_errors():
        page = await session["sigaa"].session.get("/sigaa/portais/discente/discente.jsf")
    path = str(getattr(page.url, "path", page.url))
    if "login" in path:
        raise ApiError(409, "sigaa_session_expired", "The SIGAA session has expired.")


def _bond_summary(bond_id: str, status: str, bond) -> dict:
    if hasattr(bond, "registration"):
        return {
            "bond_id": bond_id,
            "status": status,
            "type": "student",
            "registration": bond.registration,
            "program": bond.program,
        }
    return {"bond_id": bond_id, "status": status, "type": "teacher"}


def _all_bond_summaries(account) -> list[dict]:
    bonds = [_bond_summary(f"active:{i}", "active", b) for i, b in enumerate(account.active_bonds)]
    bonds += [_bond_summary(f"inactive:{i}", "inactive", b) for i, b in enumerate(account.inactive_bonds)]
    return bonds


def _find_bond(account, bond_id: str):
    try:
        status, index_str = bond_id.split(":", 1)
        index = int(index_str)
    except ValueError:
        raise ApiError(400, "malformed_bond_id", "Malformed bond_id.")
    bonds = account.active_bonds if status == "active" else account.inactive_bonds if status == "inactive" else None
    if bonds is None or not (0 <= index < len(bonds)):
        raise ApiError(404, "bond_not_found", "Bond not found.")
    return bonds[index]


def _student_bond(session: dict, bond_id: str):
    bond = _find_bond(session["account"], bond_id)
    if not hasattr(bond, "get_courses"):
        raise ApiError(400, "teacher_bond", "Teacher bonds expose no student data.")
    return bond


async def _load_courses(session: dict, bond_id: str, bond, refresh: bool = False):
    cache = session.setdefault("courses", {})
    if refresh or bond_id not in cache:
        async with _scraper_errors():
            cache[bond_id] = await bond.get_courses()
    return cache[bond_id]


class LoginRequest(BaseModel):
    url: str
    institution: str
    username: str
    password: str


class HistoryRequest(BaseModel):
    cached_history: dict | None = None
    parallel: bool = True


class EnrollmentSelectionRequest(BaseModel):
    selected_class_ids: list[str]
    view_state: str | None = None


class EnrollmentConfirmRequest(BaseModel):
    password: str


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/api/v1/{client_public_key}/sessions")
async def create_session(client_public_key: str, payload: LoginRequest, request: Request):
    client_public_key = request.state.client_public_key

    try:
        institution = InstitutionType[payload.institution.upper()]
    except KeyError:
        raise ApiError(400, "unknown_institution", f"Unknown institution '{payload.institution}'.")

    sigaa = Sigaa(payload.url, institution)
    try:
        account = await sigaa.login(payload.username, payload.password)
    except SigaaInvalidCredentials:
        await sigaa.close()
        raise ApiError(401, "invalid_credentials", "Invalid SIGAA credentials.")
    except SigaaQuestionnaireError as e:
        await sigaa.close()
        raise ApiError(403, "questionnaire", str(e))
    except (SigaaException, ValueError) as e:
        await sigaa.close()
        raise ApiError(502, "sigaa_error", str(e))

    session_id = uuid.uuid4().hex
    async with _sessions_lock:
        _sessions[session_id] = {
            "sigaa": sigaa,
            "account": account,
            "client_public_key": client_public_key,
            "last_used": time.time(),
            "credentials": {
                "username": payload.username,
                "password": payload.password,
                "url": payload.url,
                "inst_type": institution,
            },
            "courses": {},
            "enrollment": {},
        }

    name = await account.get_name()
    return {"session_id": session_id, "name": name, "bonds": _all_bond_summaries(account)}


@app.get("/api/v1/{client_public_key}/sessions/{session_id}/bonds")
async def list_bonds(client_public_key: str, session_id: str, request: Request):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    return {"bonds": _all_bond_summaries(session["account"])}


@app.get("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/courses")
async def list_courses(client_public_key: str, session_id: str, bond_id: str, request: Request):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)
    courses = await _load_courses(session, bond_id, bond, refresh=True)
    return {
        "courses": [
            {"id": i, "title": c.title, "schedule_code": c.schedule_code}
            for i, c in enumerate(courses)
        ]
    }


@app.get("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/courses/{course_id}/details")
async def course_details(
    client_public_key: str, session_id: str, bond_id: str, course_id: int, request: Request
):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)
    courses = await _load_courses(session, bond_id, bond)
    if not (0 <= course_id < len(courses)):
        raise ApiError(404, "course_not_found", "Course not found.")

    course = courses[course_id]
    async with _scraper_errors():
        grades, frequency, professor = await course.get_all_details()

    return {
        "id": course_id,
        "title": course.title,
        "schedule_code": course.schedule_code,
        "grades": grades,
        "frequency": frequency,
        "professor": professor,
    }


@app.post("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/history")
async def bond_history(
    client_public_key: str, session_id: str, bond_id: str, payload: HistoryRequest, request: Request
):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)
    await _assert_sigaa_alive(session)

    credentials = session["credentials"] if payload.parallel else None
    async with _scraper_errors():
        history = await bond.get_history(
            cached_history=payload.cached_history,
            credentials=credentials,
        )

    return {"history": history}


@app.get("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment")
async def enrollment_disciplines(
    client_public_key: str, session_id: str, bond_id: str, request: Request
):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)
    await _assert_sigaa_alive(session)

    async with _scraper_errors():
        result = await bond.get_enrollment_disciplines()

    session.setdefault("enrollment", {})[bond_id] = {
        "view_state": result.get("view_state"),
        "action_url": result.get("action_url"),
    }
    return {"levels": result.get("levels"), "view_state": result.get("view_state")}


@app.post("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment/selection")
async def enrollment_selection(
    client_public_key: str,
    session_id: str,
    bond_id: str,
    payload: EnrollmentSelectionRequest,
    request: Request,
):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)

    if not payload.selected_class_ids:
        raise ApiError(400, "no_classes_selected", "No classes selected.")

    state = session.setdefault("enrollment", {}).get(bond_id, {})
    view_state = payload.view_state or state.get("view_state")
    if not view_state:
        raise ApiError(409, "enrollment_not_started", "Fetch the enrollment listing first.")

    async with _scraper_errors():
        page = await bond.submit_enrollment(
            payload.selected_class_ids, view_state, action_url=state.get("action_url")
        )

    state.update({"confirm_view_state": page.view_state, "confirm_body": page.body})
    session["enrollment"][bond_id] = state
    return {"html": page.body, "view_state": page.view_state}


@app.post("/api/v1/{client_public_key}/sessions/{session_id}/bonds/{bond_id}/enrollment/confirm")
async def enrollment_confirm(
    client_public_key: str,
    session_id: str,
    bond_id: str,
    payload: EnrollmentConfirmRequest,
    request: Request,
):
    session = await _get_owned_session(session_id, request.state.client_public_key)
    bond = _student_bond(session, bond_id)

    state = session.setdefault("enrollment", {}).get(bond_id, {})
    confirm_view_state = state.get("confirm_view_state")
    if not confirm_view_state:
        raise ApiError(409, "enrollment_not_submitted", "Submit the class selection first.")

    async with _scraper_errors():
        password_page = await bond.request_confirmation_page(confirm_view_state)
        final_page = await bond.confirm_enrollment(
            payload.password, password_page.view_state, password_page.body
        )

    return {"html": final_page.body}


@app.delete("/api/v1/{client_public_key}/sessions/{session_id}")
async def close_session(client_public_key: str, session_id: str, request: Request):
    client_public_key = request.state.client_public_key
    async with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if not session or session["client_public_key"] != client_public_key:
        raise ApiError(404, "session_not_found", "Session not found.")
    await session["sigaa"].close()
    return {"status": "closed"}


async def _run():
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [os.environ.get("SIGAA_API_BIND", "127.0.0.1:8000")]
    await serve(app, config)


if __name__ == "__main__":
    asyncio.run(_run())
