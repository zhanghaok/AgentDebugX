"""FastAPI application exposing the diagnosis endpoints.

The service listens on :7777 (``python -m agentdebug.api``). Routes are
registered twice, mirroring the browser contract of issue #266:

* ``/api/v1/diagnose[/{session_id}]`` — service-native path
* ``/api/agentdebug/api/v1/diagnose[/{session_id}]`` — the path the browser
  sees after the Gateway forwards ``/api/agentdebug/**`` (same envelope)

The same process also serves ``GET /api/v1/sessions[/{id}]`` for the native
HTML console at ``/`` (and ``/ui``), backed by ``api/static/index.html``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from agentdebug.api.db_adapter.v2_adapter import (
    SessionNotFound,
    list_sessions as list_session_summaries,
    load_session,
)
from agentdebug.api.opencode_converter import convert_session, trajectory_to_wire
from agentdebug.api.service import (
    ATTRIBUTORS,
    DETECTORS,
    DiagnosisService,
    LLMNotConfigured,
    ServiceConfig,
    config_from_env,
)

BROWSER_PREFIX = '/api/agentdebug'
DIAGNOSE_PATH = '/api/v1/diagnose'
SESSIONS_PATH = '/api/v1/sessions'
STATIC_DIR = Path(__file__).resolve().parent / 'static'
INDEX_HTML = STATIC_DIR / 'index.html'


def create_app(
    config: Optional[ServiceConfig] = None,
    service: Optional[DiagnosisService] = None,
) -> FastAPI:
    config = config or config_from_env()
    service = service or DiagnosisService(config)

    app = FastAPI(title='DataAgent Diagnosis Service', version='0.1.0')
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r'^http://(localhost|127\.0\.0\.1)(:\d+)?$',
        allow_methods=['*'],
        allow_headers=['*'],
    )
    app.state.diagnosis_service = service

    def healthz() -> Dict[str, Any]:
        return {
            'status': 'ok',
            'db': config.db_path,
            'model': config.model,
            'llm_configured': service.llm_configured,
        }

    app.add_api_route('/healthz', healthz, methods=['GET'])
    # The gateway-prefixed page checks this path too; root /healthz may not be
    # forwarded when only /api/agentdebug/** is exposed.
    app.add_api_route(BROWSER_PREFIX + '/healthz', healthz, methods=['GET'])

    def post_diagnose(payload: Dict[str, Any] = Body(...)) -> Any:
        return _handle_post(service, payload)

    def get_diagnose(session_id: str) -> Any:
        return _handle_get(service, session_id)

    def get_sessions(limit: int = Query(100, ge=1, le=500)) -> Any:
        return _handle_sessions(config, limit)

    def get_session_detail(session_id: str) -> Any:
        return _handle_session_detail(config, session_id)

    def ui_index() -> Any:
        return FileResponse(INDEX_HTML)

    app.add_api_route(DIAGNOSE_PATH, post_diagnose, methods=['POST'])
    app.add_api_route(DIAGNOSE_PATH + '/{session_id}', get_diagnose, methods=['GET'])
    app.add_api_route(BROWSER_PREFIX + DIAGNOSE_PATH, post_diagnose, methods=['POST'])
    app.add_api_route(
        BROWSER_PREFIX + DIAGNOSE_PATH + '/{session_id}', get_diagnose, methods=['GET']
    )
    app.add_api_route(SESSIONS_PATH, get_sessions, methods=['GET'])
    app.add_api_route(
        SESSIONS_PATH + '/{session_id}', get_session_detail, methods=['GET']
    )
    app.add_api_route(BROWSER_PREFIX + SESSIONS_PATH, get_sessions, methods=['GET'])
    app.add_api_route(
        BROWSER_PREFIX + SESSIONS_PATH + '/{session_id}',
        get_session_detail,
        methods=['GET'],
    )
    # The page is same-origin and uses relative API paths, so it works both
    # directly on :7777 and behind a gateway forwarding /api/agentdebug/**.
    for path in ('/', '/ui', BROWSER_PREFIX, BROWSER_PREFIX + '/', BROWSER_PREFIX + '/ui'):
        app.add_api_route(path, ui_index, methods=['GET'], include_in_schema=False)
    return app


def _handle_post(service: DiagnosisService, payload: Dict[str, Any]) -> Any:
    session_id = _text(payload.get('session_id'))
    detector = _text(payload.get('detector'))
    attributor = _text(payload.get('attributor'))
    if not (session_id and detector and attributor):
        return JSONResponse(status_code=400, content={
            'error': 'session_id, detector and attributor are required',
        })
    if detector not in DETECTORS or attributor not in ATTRIBUTORS:
        return JSONResponse(status_code=400, content={
            'error': 'unsupported detector=%s attributor=%s' % (detector, attributor),
        })
    try:
        return service.submit(session_id, detector, attributor)
    except SessionNotFound:
        return JSONResponse(status_code=404, content={
            'error': 'session_not_found',
            'session_id': session_id,
        })
    except LLMNotConfigured as exc:
        return JSONResponse(status_code=503, content={
            'error': 'llm_not_configured',
            'message': str(exc),
        })


def _handle_get(service: DiagnosisService, session_id: str) -> Any:
    envelope = service.status(session_id)
    if envelope is None:
        return JSONResponse(status_code=404, content={
            'error': 'session_not_found',
            'session_id': session_id,
        })
    return envelope


def _handle_sessions(config: ServiceConfig, limit: int) -> Any:
    try:
        sessions = list_session_summaries(config.db_path, limit=limit)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            'error': 'session_list_failed',
            'message': str(exc),
        })
    return {'sessions': [summary.to_wire() for summary in sessions]}


def _handle_session_detail(config: ServiceConfig, session_id: str) -> Any:
    try:
        record = load_session(config.db_path, session_id)
    except SessionNotFound:
        return JSONResponse(status_code=404, content={
            'error': 'session_not_found',
            'session_id': session_id,
        })
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            'error': 'session_load_failed',
            'message': str(exc),
        })
    try:
        trajectory = convert_session(record)
        wire = trajectory_to_wire(trajectory)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            'error': 'session_conversion_failed',
            'message': str(exc),
        })
    return {
        'session': {
            'session_id': record.session_id,
            'project_id': record.project_id,
            'title': record.title,
            'slug': record.slug,
            'directory': record.directory,
            'version': record.version,
            'agent': record.agent,
            'message_count': len(record.messages),
        },
        'trajectory': wire,
    }


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ''
