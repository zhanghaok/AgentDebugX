"""Local DataAgent diagnosis service (replica of the intranet deployment).

Layered on top of the upstream AgentDebugX package:

    SQLite (opencode.db) -> db_adapter.v2_adapter -> opencode_converter
    -> outcome signals -> DiagnosePipeline(ServiceJudge, ServiceAttributor,
       ZhReflexionSuggestion) -> #299 envelope + brief markdown,
    served over FastAPI on :7777.

Entry point: ``python -m agentdebug.api``. The same process serves a native
session/diagnosis console at ``/`` (``api/static/index.html``).

See ``docs/local-diagnosis-service.md`` for run instructions and
``scripts/local-diagnosis-service.sh`` for lifecycle management.
"""
