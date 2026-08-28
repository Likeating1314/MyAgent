from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from app.agent.memory import ConversationMemory, utc_now
from app.models.schemas import SessionSummary, ToolCallRecord
from app.security import current_user_id

logger = logging.getLogger(__name__)
SessionEventType = Literal["message", "tool_call"]
SessionEventInput = tuple[SessionEventType, dict[str, Any] | ToolCallRecord]


class SessionBusyError(RuntimeError):
    pass


class SessionConflictError(RuntimeError):
    pass


class SessionLeaseLostError(RuntimeError):
    pass


class SessionNotFoundError(RuntimeError):
    pass


class SessionArchivedError(RuntimeError):
    pass


class SessionOpenApprovalError(RuntimeError):
    pass


class SessionStore(Protocol):
    def get_or_create(self, session_id: str) -> ConversationMemory: ...
    def get(self, session_id: str) -> ConversationMemory | None: ...
    def save(self, session: ConversationMemory) -> None: ...
    def list_sessions(self, *, archived: bool = False) -> list[ConversationMemory]: ...
    def rename_session(self, session_id: str, display_title: str) -> ConversationMemory: ...
    def set_archived(self, session_id: str, *, archived: bool) -> ConversationMemory: ...
    def append_event(
        self,
        session_id: str,
        event_type: SessionEventType,
        payload: dict[str, Any] | ToolCallRecord,
        *,
        run_id: str | None = None,
    ) -> int: ...
    def append_batch(
        self, session_id: str, events: Sequence[SessionEventInput], *, run_id: str | None = None
    ) -> list[int]: ...
    def acquire_run(self, session_id: str, *, lease_seconds: int) -> str: ...
    def renew_run(self, session_id: str, run_id: str, *, lease_seconds: int) -> bool: ...
    def release_run(self, session_id: str, run_id: str) -> bool: ...


def _event_payload(event_type: SessionEventType, payload: dict[str, Any] | ToolCallRecord) -> dict[str, Any]:
    if event_type == "message":
        if not isinstance(payload, dict):
            raise TypeError("message 事件负载必须是对象")
        if payload.get("role") not in {"system", "user", "assistant", "tool"} or "content" not in payload:
            raise ValueError("message 事件缺少合法 role 或 content")
        if payload.get("role") == "tool" and not isinstance(payload.get("tool_call_id"), str):
            raise ValueError("tool message 缺少 tool_call_id")
        normalized = payload
    elif event_type == "tool_call":
        normalized = ToolCallRecord.model_validate(payload).model_dump(mode="json")
    else:
        raise ValueError(f"不支持的会话事件类型: {event_type}")
    return json.loads(json.dumps(normalized, ensure_ascii=False, allow_nan=False))


def _memory_from_events(
    *,
    session_id: str,
    created_at: datetime,
    updated_at: datetime,
    display_title: str,
    archived_at: datetime | None,
    events: Sequence[tuple[SessionEventType, dict[str, Any]]],
) -> ConversationMemory:
    messages: list[dict[str, Any]] = []
    tool_calls: list[ToolCallRecord] = []
    for event_type, payload in events:
        if event_type == "message":
            messages.append(payload)
        elif event_type == "tool_call":
            tool_calls.append(ToolCallRecord.model_validate(payload))
    return ConversationMemory(
        session_id=session_id,
        display_title=display_title,
        archived_at=archived_at,
        created_at=created_at,
        updated_at=updated_at,
        messages=messages,
        tool_calls=tool_calls,
    )


class InMemorySessionStore:
    def __init__(self) -> None:
        self._metadata: dict[str, tuple[datetime, datetime, str, datetime | None]] = {}
        self._events: dict[str, list[tuple[int, SessionEventType, dict[str, Any], datetime]]] = {}
        self._runs: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.RLock()

    def _ensure_session(self, session_id: str) -> None:
        if session_id not in self._metadata:
            now = utc_now()
            self._metadata[session_id] = (now, now, session_id, None)
            self._events[session_id] = []

    def get_or_create(self, session_id: str) -> ConversationMemory:
        with self._lock:
            self._ensure_session(session_id)
            return self._get_locked(session_id)

    def get(self, session_id: str) -> ConversationMemory | None:
        with self._lock:
            if session_id not in self._metadata:
                return None
            return self._get_locked(session_id)

    def _get_locked(self, session_id: str) -> ConversationMemory:
        created_at, updated_at, display_title, archived_at = self._metadata[session_id]
        events = [(event_type, json.loads(json.dumps(payload))) for _, event_type, payload, _ in self._events[session_id]]
        return _memory_from_events(
            session_id=session_id,
            created_at=created_at,
            updated_at=updated_at,
            display_title=display_title,
            archived_at=archived_at,
            events=events,
        )

    def save(self, session: ConversationMemory) -> None:
        with self._lock:
            self._ensure_session(session.session_id)
            current = self._get_locked(session.session_id)
            if session.messages[: len(current.messages)] != current.messages:
                raise SessionConflictError("save 不能修改已追加的消息")
            current_tools = [item.model_dump(mode="json") for item in current.tool_calls]
            incoming_tools = [item.model_dump(mode="json") for item in session.tool_calls]
            if incoming_tools[: len(current_tools)] != current_tools:
                raise SessionConflictError("save 不能修改已追加的工具记录")
            events: list[SessionEventInput] = [
                ("message", message) for message in session.messages[len(current.messages) :]
            ]
            events.extend(
                ("tool_call", record) for record in session.tool_calls[len(current.tool_calls) :]
            )
            self.append_batch(session.session_id, events)

    def list_sessions(self, *, archived: bool = False) -> list[ConversationMemory]:
        with self._lock:
            sessions = [
                self._get_locked(session_id)
                for session_id, metadata in self._metadata.items()
                if (metadata[3] is not None) is archived
            ]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def rename_session(self, session_id: str, display_title: str) -> ConversationMemory:
        with self._lock:
            if session_id not in self._metadata:
                raise SessionNotFoundError("会话不存在")
            created_at, updated_at, _, archived_at = self._metadata[session_id]
            if archived_at is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            active = self._runs.get(session_id)
            if active is not None and active[1] > utc_now():
                raise SessionBusyError("该会话已有任务正在运行")
            now = utc_now()
            self._metadata[session_id] = (created_at, now, display_title, None)
            return self._get_locked(session_id)

    def set_archived(self, session_id: str, *, archived: bool) -> ConversationMemory:
        with self._lock:
            if session_id not in self._metadata:
                raise SessionNotFoundError("会话不存在")
            created_at, _, display_title, archived_at = self._metadata[session_id]
            active = self._runs.get(session_id)
            if active is not None and active[1] > utc_now():
                raise SessionBusyError("该会话已有任务正在运行")
            if archived and archived_at is None:
                archived_at = utc_now()
            elif not archived:
                archived_at = None
            self._metadata[session_id] = (created_at, utc_now(), display_title, archived_at)
            return self._get_locked(session_id)

    def append_event(
        self,
        session_id: str,
        event_type: SessionEventType,
        payload: dict[str, Any] | ToolCallRecord,
        *,
        run_id: str | None = None,
    ) -> int:
        return self.append_batch(session_id, [(event_type, payload)], run_id=run_id)[0]

    def append_batch(
        self, session_id: str, events: Sequence[SessionEventInput], *, run_id: str | None = None
    ) -> list[int]:
        normalized = [(event_type, _event_payload(event_type, payload)) for event_type, payload in events]
        if not normalized:
            return []
        with self._lock:
            self._ensure_session(session_id)
            if self._metadata[session_id][3] is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            if run_id is not None:
                active = self._runs.get(session_id)
                if active is None or active[0] != run_id or active[1] <= utc_now():
                    raise SessionLeaseLostError("会话 run 租约已失效")
            sequence = self._events[session_id][-1][0] if self._events[session_id] else 0
            now = utc_now()
            sequences: list[int] = []
            for event_type, payload in normalized:
                sequence += 1
                sequences.append(sequence)
                self._events[session_id].append((sequence, event_type, payload, now))
            created_at, _, display_title, archived_at = self._metadata[session_id]
            self._metadata[session_id] = (created_at, now, display_title, archived_at)
            return sequences

    def acquire_run(self, session_id: str, *, lease_seconds: int) -> str:
        now = utc_now()
        with self._lock:
            self._ensure_session(session_id)
            if self._metadata[session_id][3] is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            active = self._runs.get(session_id)
            if active is not None and active[1] > now:
                raise SessionBusyError("该会话已有任务正在运行")
            run_id = uuid4().hex
            self._runs[session_id] = (run_id, now + timedelta(seconds=lease_seconds))
            return run_id

    def renew_run(self, session_id: str, run_id: str, *, lease_seconds: int) -> bool:
        with self._lock:
            active = self._runs.get(session_id)
            if active is None or active[0] != run_id:
                return False
            self._runs[session_id] = (run_id, utc_now() + timedelta(seconds=lease_seconds))
            return True

    def release_run(self, session_id: str, run_id: str) -> bool:
        with self._lock:
            active = self._runs.get(session_id)
            if active is None or active[0] != run_id:
                return False
            del self._runs[session_id]
            return True


class SQLiteSessionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._init_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_database(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    owner_user_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    tool_calls_json TEXT NOT NULL,
                    display_title TEXT NOT NULL DEFAULT '',
                    archived_at TEXT
                )
                """
            )
            session_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(sessions)")
            }
            if "display_title" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN display_title TEXT NOT NULL DEFAULT ''"
                )
            if "archived_at" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
            if "owner_user_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN owner_user_id TEXT")
            connection.execute(
                "UPDATE sessions SET display_title = session_id WHERE display_title = ''"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL CHECK (event_type IN ('message', 'tool_call')),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_event_migrations (
                    session_id TEXT PRIMARY KEY,
                    migrated_at TEXT NOT NULL,
                    legacy_message_count INTEGER NOT NULL,
                    legacy_tool_call_count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_runs (
                    session_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    owner_user_id TEXT,
                    path TEXT NOT NULL,
                    content TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner_user_id, path)
                )
                """
            )
            rag_columns = {row["name"] for row in connection.execute("PRAGMA table_info(rag_documents)")}
            if "owner_user_id" not in rag_columns:
                connection.execute("ALTER TABLE rag_documents RENAME TO rag_documents_legacy")
                connection.execute("CREATE TABLE rag_documents (owner_user_id TEXT, path TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(owner_user_id, path))")
                connection.execute("INSERT INTO rag_documents (owner_user_id, path, content, updated_at) SELECT NULL, path, content, updated_at FROM rag_documents_legacy")
                connection.execute("DROP TABLE rag_documents_legacy")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            approval_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(approvals)")
            }
            if "expires_at" not in approval_columns:
                connection.execute("ALTER TABLE approvals ADD COLUMN expires_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ix_session_events_type ON session_events(session_id, event_type, sequence)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS ix_sessions_owner_updated ON sessions(owner_user_id, updated_at DESC)")
            self._migrate_legacy_sessions(connection)

    def _migrate_legacy_sessions(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT s.* FROM sessions AS s
            LEFT JOIN session_event_migrations AS m ON m.session_id = s.session_id
            WHERE m.session_id IS NULL
            ORDER BY s.created_at, s.session_id
            """
        ).fetchall()
        for row in rows:
            try:
                messages = json.loads(row["messages_json"])
                tool_calls = json.loads(row["tool_calls_json"])
                if not isinstance(messages, list) or not isinstance(tool_calls, list):
                    raise ValueError("legacy session JSON must contain arrays")
                normalized_messages = [_event_payload("message", item) for item in messages]
                normalized_tools = [_event_payload("tool_call", item) for item in tool_calls]
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.error(
                    "legacy session migration failed session_id=%s exception_type=%s",
                    row["session_id"],
                    type(exc).__name__,
                )
                raise RuntimeError(f"旧会话数据损坏，迁移失败: {row['session_id']}") from exc

            existing = connection.execute(
                "SELECT COUNT(*) FROM session_events WHERE session_id = ?", (row["session_id"],)
            ).fetchone()[0]
            if existing and (normalized_messages or normalized_tools):
                raise RuntimeError(f"旧会话迁移状态冲突: {row['session_id']}")
            sequence = existing
            for event_type, payloads in (("message", normalized_messages), ("tool_call", normalized_tools)):
                for payload in payloads:
                    sequence += 1
                    connection.execute(
                        """
                        INSERT INTO session_events (session_id, sequence, event_type, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            row["session_id"],
                            sequence,
                            event_type,
                            json.dumps(payload, ensure_ascii=False, allow_nan=False),
                            row["created_at"],
                        ),
                    )
            connection.execute(
                """
                INSERT INTO session_event_migrations (
                    session_id, migrated_at, legacy_message_count, legacy_tool_call_count
                ) VALUES (?, ?, ?, ?)
                """,
                (row["session_id"], utc_now().isoformat(), len(normalized_messages), len(normalized_tools)),
            )

    @staticmethod
    def _ensure_session(connection: sqlite3.Connection, session_id: str) -> None:
        now = utc_now().isoformat()
        owner_user_id = current_user_id()
        connection.execute(
            """
            INSERT INTO sessions (
                session_id, owner_user_id, created_at, updated_at, messages_json, tool_calls_json,
                display_title, archived_at
            )
            VALUES (?, ?, ?, ?, '[]', '[]', ?, NULL)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session_id, owner_user_id, now, now, session_id),
        )
        connection.execute(
            """
            INSERT INTO session_event_migrations (
                session_id, migrated_at, legacy_message_count, legacy_tool_call_count
            ) VALUES (?, ?, 0, 0)
            ON CONFLICT(session_id) DO NOTHING
            """,
            (session_id, now),
        )

    def get_or_create(self, session_id: str) -> ConversationMemory:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_session(connection, session_id)
        session = self.get(session_id)
        if session is None:
            raise RuntimeError(f"会话创建失败: {session_id}")
        return session

    def get(self, session_id: str) -> ConversationMemory | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, created_at, updated_at, display_title, archived_at
                FROM sessions WHERE session_id = ? AND (owner_user_id = ? OR (owner_user_id IS NULL AND ? = '00000000-0000-0000-0000-000000000001'))
                """,
                (session_id, current_user_id(), current_user_id()),
            ).fetchone()
            if row is None:
                return None
            event_rows = connection.execute(
                "SELECT event_type, payload_json FROM session_events WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        try:
            events = [(event["event_type"], json.loads(event["payload_json"])) for event in event_rows]
            return _memory_from_events(
                session_id=row["session_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                display_title=row["display_title"] or row["session_id"],
                archived_at=(
                    datetime.fromisoformat(row["archived_at"]) if row["archived_at"] else None
                ),
                events=events,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error(
                "session event load failed session_id=%s exception_type=%s",
                session_id,
                type(exc).__name__,
            )
            raise RuntimeError(f"会话事件数据损坏: {session_id}") from exc

    def save(self, session: ConversationMemory) -> None:
        current = self.get_or_create(session.session_id)
        if session.messages[: len(current.messages)] != current.messages:
            raise SessionConflictError("save 不能修改已追加的消息")
        current_tools = [item.model_dump(mode="json") for item in current.tool_calls]
        incoming_tools = [item.model_dump(mode="json") for item in session.tool_calls]
        if incoming_tools[: len(current_tools)] != current_tools:
            raise SessionConflictError("save 不能修改已追加的工具记录")
        events: list[SessionEventInput] = [
            ("message", message) for message in session.messages[len(current.messages) :]
        ]
        events.extend(("tool_call", record) for record in session.tool_calls[len(current.tool_calls) :])
        self.append_batch(session.session_id, events)

    def list_sessions(self, *, archived: bool = False) -> list[ConversationMemory]:
        condition = "archived_at IS NOT NULL" if archived else "archived_at IS NULL"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT session_id FROM sessions WHERE (owner_user_id = ? OR (owner_user_id IS NULL AND ? = '00000000-0000-0000-0000-000000000001')) AND {condition} ORDER BY updated_at DESC",
                (current_user_id(), current_user_id()),
            ).fetchall()
        sessions: list[ConversationMemory] = []
        for row in rows:
            session = self.get(row["session_id"])
            if session is not None:
                sessions.append(session)
        return sessions

    def rename_session(self, session_id: str, display_title: str) -> ConversationMemory:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT archived_at FROM sessions WHERE session_id = ? AND owner_user_id = ?", (session_id, current_user_id())
            ).fetchone()
            if row is None:
                raise SessionNotFoundError("会话不存在")
            if row["archived_at"] is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            active = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id = ? AND expires_at > ?",
                (session_id, now),
            ).fetchone()
            if active is not None:
                raise SessionBusyError("该会话已有任务正在运行")
            connection.execute(
                "UPDATE sessions SET display_title = ?, updated_at = ? WHERE session_id = ? AND owner_user_id = ?",
                (display_title, now, session_id, current_user_id()),
            )
        session = self.get(session_id)
        if session is None:
            raise SessionNotFoundError("会话不存在")
        return session

    def set_archived(self, session_id: str, *, archived: bool) -> ConversationMemory:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT archived_at FROM sessions WHERE session_id = ? AND owner_user_id = ?", (session_id, current_user_id())
            ).fetchone()
            if row is None:
                raise SessionNotFoundError("会话不存在")
            active = connection.execute(
                "SELECT 1 FROM session_runs WHERE session_id = ? AND expires_at > ?",
                (session_id, now),
            ).fetchone()
            if active is not None:
                raise SessionBusyError("该会话已有任务正在运行")
            if archived:
                open_approval = connection.execute(
                    """
                    SELECT 1 FROM approvals
                    WHERE session_id = ? AND owner_user_id = ? AND status IN ('pending', 'approved')
                      AND expires_at IS NOT NULL AND expires_at > ?
                    LIMIT 1
                    """,
                    (session_id, current_user_id(), now),
                ).fetchone()
                if open_approval is not None:
                    raise SessionOpenApprovalError("会话存在未处理审批，不能归档")
            archived_at = now if archived else None
            connection.execute(
                "UPDATE sessions SET archived_at = ?, updated_at = ? WHERE session_id = ? AND owner_user_id = ?",
                (archived_at, now, session_id, current_user_id()),
            )
        session = self.get(session_id)
        if session is None:
            raise SessionNotFoundError("会话不存在")
        return session

    def append_event(
        self,
        session_id: str,
        event_type: SessionEventType,
        payload: dict[str, Any] | ToolCallRecord,
        *,
        run_id: str | None = None,
    ) -> int:
        return self.append_batch(session_id, [(event_type, payload)], run_id=run_id)[0]

    def append_batch(
        self, session_id: str, events: Sequence[SessionEventInput], *, run_id: str | None = None
    ) -> list[int]:
        normalized = [(event_type, _event_payload(event_type, payload)) for event_type, payload in events]
        if not normalized:
            return []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_session(connection, session_id)
            session_row = connection.execute(
                "SELECT archived_at FROM sessions WHERE session_id = ? AND owner_user_id = ?", (session_id, current_user_id())
            ).fetchone()
            if session_row is None:
                raise SessionNotFoundError("会话不存在")
            if session_row["archived_at"] is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            if run_id is not None:
                active = connection.execute(
                    "SELECT run_id, expires_at FROM session_runs WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if (
                    active is None
                    or active["run_id"] != run_id
                    or active["expires_at"] <= utc_now().isoformat()
                ):
                    raise SessionLeaseLostError("会话 run 租约已失效")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            now = utc_now().isoformat()
            sequences: list[int] = []
            for event_type, payload in normalized:
                sequence += 1
                sequences.append(sequence)
                connection.execute(
                    """
                    INSERT INTO session_events (session_id, sequence, event_type, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence,
                        event_type,
                        json.dumps(payload, ensure_ascii=False, allow_nan=False),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ? AND owner_user_id = ?",
                (now, session_id, current_user_id()),
            )
            return sequences

    def acquire_run(self, session_id: str, *, lease_seconds: int) -> str:
        now = utc_now()
        run_id = uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_session(connection, session_id)
            session_row = connection.execute(
                "SELECT archived_at FROM sessions WHERE session_id = ? AND owner_user_id = ?", (session_id, current_user_id())
            ).fetchone()
            if session_row is None:
                raise SessionNotFoundError("会话不存在")
            if session_row["archived_at"] is not None:
                raise SessionArchivedError("归档会话为只读，请先恢复会话")
            active = connection.execute(
                "SELECT expires_at FROM session_runs WHERE session_id = ?", (session_id,)
            ).fetchone()
            if active is not None and active["expires_at"] > now.isoformat():
                raise SessionBusyError("该会话已有任务正在运行")
            connection.execute(
                """
                INSERT INTO session_runs (session_id, run_id, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at
                """,
                (
                    session_id,
                    run_id,
                    now.isoformat(),
                    now.isoformat(),
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                ),
            )
        return run_id

    def renew_run(self, session_id: str, run_id: str, *, lease_seconds: int) -> bool:
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE session_runs SET updated_at = ?, expires_at = ? WHERE session_id = ? AND run_id = ? AND EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = session_runs.session_id AND s.owner_user_id = ?)",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    session_id,
                    run_id,
                    current_user_id(),
                ),
            )
            return cursor.rowcount == 1

    def release_run(self, session_id: str, run_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM session_runs WHERE session_id = ? AND run_id = ? AND EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = session_runs.session_id AND s.owner_user_id = ?)",
                (session_id, run_id, current_user_id()),
            )
            return cursor.rowcount == 1


def summarize_session(session: ConversationMemory) -> SessionSummary:
    return SessionSummary(
        session_id=session.session_id,
        display_title=session.display_title or session.session_id,
        archived_at=session.archived_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages),
        tool_call_count=len(session.tool_calls),
    )
