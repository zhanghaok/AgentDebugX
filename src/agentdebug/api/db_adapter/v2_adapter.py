"""OpenCode 2 SQLite adapter: ``session_v2`` + ``session_message`` tables.

Reads the local ``opencode.db`` and assembles the opencode-export shape
(``messages[].parts[]``) the converter consumes. The connection is opened
read-only: the diagnosis service never writes to the OpenCode store.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SessionNotFound(Exception):
    """Raised when the requested session id does not exist in the database."""


@dataclass
class SessionRecord:
    """One OpenCode session with its user/assistant messages in order."""

    session_id: str
    project_id: str
    title: Optional[str]
    slug: str
    directory: str
    version: str
    agent: Optional[str]
    messages: List[Dict[str, Any]] = field(default_factory=list)


def session_exists(db_path: str, session_id: str) -> bool:
    """Cheap existence check used before accepting a diagnosis submission."""
    connection = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        row = connection.execute(
            'SELECT 1 FROM session_v2 WHERE id = ?', (session_id,)
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def load_session(db_path: str, session_id: str) -> SessionRecord:
    """Load a session and its conversation messages from the OpenCode DB."""
    connection = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        info = connection.execute(
            'SELECT id, project_id, title, slug, directory, version, agent'
            ' FROM session_v2 WHERE id = ?',
            (session_id,),
        ).fetchone()
        if info is None:
            raise SessionNotFound(session_id)
        rows = connection.execute(
            "SELECT id, type, data FROM session_message"
            " WHERE session_id = ? AND type IN ('user', 'assistant')"
            ' ORDER BY seq',
            (session_id,),
        ).fetchall()
    finally:
        connection.close()

    messages = [
        {'id': row['id'], 'role': row['type'], 'data': _parse_json_object(row['data'])}
        for row in rows
    ]
    return SessionRecord(
        session_id=info['id'],
        project_id=info['project_id'],
        title=info['title'],
        slug=info['slug'],
        directory=info['directory'],
        version=info['version'],
        agent=info['agent'],
        messages=messages,
    )


def _parse_json_object(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
