"""FastAPI application exposing the diagnosis endpoints.

The service listens on :7777 (``python -m agentdebug.api``). Routes are
registered twice, mirroring the browser contract of issue #266:

* ``/api/v1/diagnose[/{session_id}]`` — service-native path
* ``/api/agentdebug/api/v1/diagnose[/{session_id}]`` — the path the browser
  sees after the Gateway forwards ``/api/agentdebug/**`` (same envelope)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agentdebug.api.db_adapter.v2_adapter import SessionNotFound
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

    @app.get('/healthz')
    def healthz() -> Dict[str, Any]:
        return {
            'status': 'ok',
            'db': config.db_path,
            'model': config.model,
            'llm_configured': service.llm_configured,
        }

    def post_diagnose(payload: Dict[str, Any] = Body(...)) -> Any:
        return _handle_post(service, payload)

    def get_diagnose(session_id: str) -> Any:
        return _handle_get(service, session_id)

    app.add_api_route(DIAGNOSE_PATH, post_diagnose, methods=['POST'])
    app.add_api_route(DIAGNOSE_PATH + '/{session_id}', get_diagnose, methods=['GET'])
    app.add_api_route(BROWSER_PREFIX + DIAGNOSE_PATH, post_diagnose, methods=['POST'])
    app.add_api_route(
        BROWSER_PREFIX + DIAGNOSE_PATH + '/{session_id}', get_diagnose, methods=['GET']
    )
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


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ''
