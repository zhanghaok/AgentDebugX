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


@dataclass
class SessionSummary:
    """One row of the session picker (no message payloads)."""

    session_id: str
    project_id: str
    title: Optional[str]
    slug: str
    directory: str
    version: str
    agent: Optional[str]
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    message_count: int = 0

    def to_wire(self) -> Dict[str, Any]:
        return {
            'session_id': self.session_id,
            'project_id': self.project_id,
            'title': self.title,
            'slug': self.slug,
            'directory': self.directory,
            'version': self.version,
            'agent': self.agent,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'message_count': self.message_count,
        }


def list_sessions(db_path: str, limit: int = 200) -> List[SessionSummary]:
    """List OpenCode sessions, most recently updated first.

    The count includes only the message types ``load_session`` consumes, so
    the number shown in the picker matches the conversation the page renders.
    """

    limit = max(1, min(int(limit), 500))
    connection = sqlite3.connect('file:%s?mode=ro' % db_path, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT s.id, s.project_id, s.title, s.slug, s.directory,'
            ' s.version, s.agent, s.time_created, s.time_updated,'
            ' (SELECT COUNT(*) FROM session_message m'
            "  WHERE m.session_id = s.id AND m.type IN ('user', 'assistant'))"
            ' AS message_count'
            ' FROM session_v2 s'
            ' ORDER BY COALESCE(s.time_updated, s.time_created, 0) DESC, s.id ASC'
            ' LIMIT ?',
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [
        SessionSummary(
            session_id=row['id'],
            project_id=row['project_id'],
            title=row['title'],
            slug=row['slug'],
            directory=row['directory'],
            version=row['version'],
            agent=row['agent'],
            created_at=row['time_created'],
            updated_at=row['time_updated'],
            message_count=row['message_count'],
        )
        for row in rows
    ]


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
