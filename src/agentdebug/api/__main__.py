"""Run the diagnosis service: ``python -m agentdebug.api`` (FastAPI :7777)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get('DIAGNOSIS_HOST', '127.0.0.1')
    port = int(os.environ.get('DIAGNOSIS_PORT', '7777'))
    log_level = os.environ.get('DIAGNOSIS_LOG_LEVEL', 'info')
    uvicorn.run(
        'agentdebug.api.app:create_app',
        factory=True,
        host=host,
        port=port,
        log_level=log_level,
    )


if __name__ == '__main__':
    main()
