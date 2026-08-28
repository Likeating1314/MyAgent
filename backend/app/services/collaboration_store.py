from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import uuid4

from app.models.schemas import (
    CollaborationAgentInfo,
    CollaborationCreateRequest,
    CollaborationEventInfo,
    CollaborationInfo,
    CollaborationRunInfo,
    CollaborationSummary,
)
from app.security import current_user_id


class CollaborationBusyError(RuntimeError):
    pass


class CollaborationNotFoundError(RuntimeError):
    pass


class CollaborationLeaseLostError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CollaborationStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collaborations (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT,
                    session_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    rounds INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collaboration_agents (
                    collaboration_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    is_coordinator INTEGER NOT NULL,
                    PRIMARY KEY (collaboration_id, id),
                    UNIQUE (collaboration_id, position),
                    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS collaboration_runs (
                    id TEXT PRIMARY KEY,
                    collaboration_id TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    terminal_event TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS collaboration_events (
                    collaboration_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    agent_id TEXT,
                    message_id TEXT,
                    round INTEGER,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (collaboration_id, sequence),
                    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
                    FOREIGN KEY (run_id) REFERENCES collaboration_runs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS collaboration_leases (
                    collaboration_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (collaboration_id) REFERENCES collaborations(id) ON DELETE CASCADE,
                    FOREIGN KEY (run_id) REFERENCES collaboration_runs(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS ix_collaborations_session
                    ON collaborations(owner_user_id, session_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS ix_collaboration_runs_room
                    ON collaboration_runs(collaboration_id, created_at);
                CREATE INDEX IF NOT EXISTS ix_collaboration_events_run
                    ON collaboration_events(run_id, sequence);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(collaborations)")}
            if "owner_user_id" not in columns:
                connection.execute("ALTER TABLE collaborations ADD COLUMN owner_user_id TEXT")

    def create(self, payload: CollaborationCreateRequest) -> CollaborationInfo:
        collaboration_id = str(uuid4())
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO collaborations (id, owner_user_id, session_id, title, rounds, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (collaboration_id, current_user_id(), payload.session_id, payload.title, payload.rounds, now, now),
            )
            connection.executemany(
                """
                INSERT INTO collaboration_agents
                    (collaboration_id, id, name, role, prompt, position, is_coordinator)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        collaboration_id,
                        agent.id,
                        agent.name,
                        agent.role,
                        agent.prompt,
                        agent.position,
                        int(agent.is_coordinator),
                    )
                    for agent in payload.agents
                ],
            )
        return self.require(collaboration_id)

    def list(self, session_id: str | None = None) -> list[CollaborationSummary]:
        where = "WHERE c.owner_user_id = ?" + (" AND c.session_id = ?" if session_id is not None else "")
        params: tuple[Any, ...] = (current_user_id(), session_id) if session_id is not None else (current_user_id(),)
        now = utc_now().isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*, COUNT(a.id) AS agent_count,
                    MAX(CASE WHEN l.expires_at > ? THEN l.run_id END) AS active_run_id
                FROM collaborations AS c
                JOIN collaboration_agents AS a ON a.collaboration_id = c.id
                LEFT JOIN collaboration_leases AS l ON l.collaboration_id = c.id
                {where}
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """,
                (now, *params),
            ).fetchall()
        return [
            CollaborationSummary(
                id=row["id"], session_id=row["session_id"], title=row["title"],
                rounds=row["rounds"], agent_count=row["agent_count"],
                active_run_id=row["active_run_id"], created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def get(self, collaboration_id: str) -> CollaborationInfo | None:
        with self._connect() as connection:
            room = connection.execute(
                "SELECT * FROM collaborations WHERE id = ? AND owner_user_id = ?", (collaboration_id, current_user_id())
            ).fetchone()
            if room is None:
                return None
            agent_rows = connection.execute(
                "SELECT * FROM collaboration_agents WHERE collaboration_id = ? ORDER BY position",
                (collaboration_id,),
            ).fetchall()
            run_rows = connection.execute(
                "SELECT * FROM collaboration_runs WHERE collaboration_id = ? ORDER BY created_at",
                (collaboration_id,),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT * FROM collaboration_events WHERE collaboration_id = ? ORDER BY sequence",
                (collaboration_id,),
            ).fetchall()
        return CollaborationInfo(
            id=room["id"], session_id=room["session_id"], title=room["title"],
            rounds=room["rounds"], created_at=room["created_at"], updated_at=room["updated_at"],
            agents=[self._agent(row) for row in agent_rows],
            runs=[self._run(row) for row in run_rows],
            events=[self._event(row) for row in event_rows],
        )

    def require(self, collaboration_id: str) -> CollaborationInfo:
        room = self.get(collaboration_id)
        if room is None:
            raise CollaborationNotFoundError("协作房间不存在。")
        return room

    def acquire_run(self, collaboration_id: str, user_message: str, *, lease_seconds: int) -> CollaborationRunInfo:
        run_id = str(uuid4())
        now = utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM collaborations WHERE id = ? AND owner_user_id = ?", (collaboration_id, current_user_id())).fetchone() is None:
                raise CollaborationNotFoundError("协作房间不存在。")
            lease = connection.execute(
                "SELECT run_id, expires_at FROM collaboration_leases WHERE collaboration_id = ?",
                (collaboration_id,),
            ).fetchone()
            if lease is not None and lease["expires_at"] > now.isoformat():
                raise CollaborationBusyError("该协作房间已有运行中的任务。")
            token_row = connection.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) + 1 AS token FROM collaboration_runs WHERE collaboration_id = ?",
                (collaboration_id,),
            ).fetchone()
            token = int(token_row["token"])
            connection.execute(
                "INSERT INTO collaboration_runs (id, collaboration_id, user_message, status, fencing_token, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (run_id, collaboration_id, user_message, token, now.isoformat(), now.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO collaboration_leases (collaboration_id, run_id, fencing_token, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(collaboration_id) DO UPDATE SET
                    run_id = excluded.run_id, fencing_token = excluded.fencing_token,
                    expires_at = excluded.expires_at
                """,
                (collaboration_id, run_id, token, expires_at.isoformat()),
            )
        return CollaborationRunInfo(
            id=run_id, collaboration_id=collaboration_id, user_message=user_message,
            status="running", fencing_token=token, created_at=now, updated_at=now,
        )

    def renew_run(self, collaboration_id: str, run_id: str, fencing_token: int, *, lease_seconds: int) -> bool:
        expires_at = (utc_now() + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE collaboration_leases SET expires_at = ?
                   WHERE collaboration_id = ? AND run_id = ? AND fencing_token = ?
                     AND EXISTS (SELECT 1 FROM collaborations c
                                 WHERE c.id = collaboration_leases.collaboration_id
                                   AND c.owner_user_id = ?)""",
                (expires_at, collaboration_id, run_id, fencing_token, current_user_id()),
            )
        return cursor.rowcount == 1

    def release_run(self, collaboration_id: str, run_id: str, fencing_token: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """DELETE FROM collaboration_leases
                   WHERE collaboration_id = ? AND run_id = ? AND fencing_token = ?
                     AND EXISTS (SELECT 1 FROM collaborations c
                                 WHERE c.id = collaboration_leases.collaboration_id
                                   AND c.owner_user_id = ?)""",
                (collaboration_id, run_id, fencing_token, current_user_id()),
            )
        return cursor.rowcount == 1

    def append_event(
        self, collaboration_id: str, run_id: str, fencing_token: int, event: str,
        data: dict[str, Any], *, agent_id: str | None = None,
        message_id: str | None = None, round_number: int | None = None,
    ) -> CollaborationEventInfo:
        now = utc_now()
        safe_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, collaboration_id, run_id, fencing_token, current_user_id())
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM collaboration_events WHERE collaboration_id = ?",
                (collaboration_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            connection.execute(
                """
                INSERT INTO collaboration_events
                    (collaboration_id, sequence, run_id, event, agent_id, message_id, round, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (collaboration_id, sequence, run_id, event, agent_id, message_id, round_number, safe_json, now.isoformat()),
            )
            connection.execute(
                "UPDATE collaborations SET updated_at = ? WHERE id = ? AND owner_user_id = ?",
                (now.isoformat(), collaboration_id, current_user_id()),
            )
        return CollaborationEventInfo(
            collaboration_id=collaboration_id, sequence=sequence, run_id=run_id,
            event=event, agent_id=agent_id, message_id=message_id, round=round_number,
            data=data, created_at=now,
        )

    def finish_run(
        self, collaboration_id: str, run_id: str, fencing_token: int,
        terminal_event: str, data: dict[str, Any],
    ) -> CollaborationEventInfo | None:
        now = utc_now()
        safe_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, collaboration_id, run_id, fencing_token, current_user_id())
            cursor = connection.execute(
                """
                UPDATE collaboration_runs SET status = ?, terminal_event = ?, updated_at = ?
                WHERE id = ? AND collaboration_id = ? AND fencing_token = ? AND terminal_event IS NULL
                """,
                (terminal_event, terminal_event, now.isoformat(), run_id, collaboration_id, fencing_token),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM collaboration_events WHERE collaboration_id = ?",
                (collaboration_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            connection.execute(
                "INSERT INTO collaboration_events (collaboration_id, sequence, run_id, event, data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (collaboration_id, sequence, run_id, terminal_event, safe_json, now.isoformat()),
            )
            connection.execute(
                """DELETE FROM collaboration_leases
                   WHERE collaboration_id = ? AND run_id = ? AND fencing_token = ?
                     AND EXISTS (SELECT 1 FROM collaborations c
                                 WHERE c.id = collaboration_leases.collaboration_id
                                   AND c.owner_user_id = ?)""",
                (collaboration_id, run_id, fencing_token, current_user_id()),
            )
            connection.execute(
                "UPDATE collaborations SET updated_at = ? WHERE id = ? AND owner_user_id = ?",
                (now.isoformat(), collaboration_id, current_user_id()),
            )
        return CollaborationEventInfo(
            collaboration_id=collaboration_id, sequence=sequence, run_id=run_id,
            event=terminal_event, data=data, created_at=now,
        )

    def finish_run_after_lease_loss(
        self,
        collaboration_id: str,
        run_id: str,
        fencing_token: int,
        data: dict[str, Any],
    ) -> CollaborationEventInfo | None:
        """Finalize only the fenced old run after its lease can no longer be trusted.

        This deliberately does not require an active lease. The run row remains the
        authority for ownership, and cleanup only targets a lease with the same
        run_id and fencing token, so a takeover lease is never changed.
        """
        now = utc_now()
        safe_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM collaborations WHERE id = ? AND owner_user_id = ?",
                (collaboration_id, current_user_id()),
            ).fetchone() is None:
                return None
            cursor = connection.execute(
                """
                UPDATE collaboration_runs SET status = 'error', terminal_event = 'error', updated_at = ?
                WHERE id = ? AND collaboration_id = ? AND fencing_token = ?
                    AND status = 'running' AND terminal_event IS NULL
                """,
                (now.isoformat(), run_id, collaboration_id, fencing_token),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM collaboration_events WHERE collaboration_id = ?",
                (collaboration_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            connection.execute(
                "INSERT INTO collaboration_events (collaboration_id, sequence, run_id, event, data_json, created_at) VALUES (?, ?, ?, 'error', ?, ?)",
                (collaboration_id, sequence, run_id, safe_json, now.isoformat()),
            )
            connection.execute(
                """DELETE FROM collaboration_leases
                   WHERE collaboration_id = ? AND run_id = ? AND fencing_token = ?
                     AND EXISTS (SELECT 1 FROM collaborations c
                                 WHERE c.id = collaboration_leases.collaboration_id
                                   AND c.owner_user_id = ?)""",
                (collaboration_id, run_id, fencing_token, current_user_id()),
            )
            connection.execute(
                "UPDATE collaborations SET updated_at = ? WHERE id = ? AND owner_user_id = ?",
                (now.isoformat(), collaboration_id, current_user_id()),
            )
        return CollaborationEventInfo(
            collaboration_id=collaboration_id,
            sequence=sequence,
            run_id=run_id,
            event="error",
            data=data,
            created_at=now,
        )

    def events_before(self, collaboration_id: str, sequence: int | None = None) -> list[CollaborationEventInfo]:
        if self.get(collaboration_id) is None:
            return []
        condition = "AND sequence < ?" if sequence is not None else ""
        params: Sequence[Any] = (collaboration_id, sequence) if sequence is not None else (collaboration_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM collaboration_events WHERE collaboration_id = ? {condition} ORDER BY sequence",
                params,
            ).fetchall()
        return [self._event(row) for row in rows]

    @staticmethod
    def _assert_lease(
        connection: sqlite3.Connection,
        collaboration_id: str,
        run_id: str,
        fencing_token: int,
        owner_user_id: str,
    ) -> None:
        row = connection.execute(
            """SELECT l.expires_at FROM collaboration_leases l
               JOIN collaborations c ON c.id = l.collaboration_id
               WHERE l.collaboration_id = ? AND l.run_id = ? AND l.fencing_token = ?
                 AND c.owner_user_id = ?""",
            (collaboration_id, run_id, fencing_token, owner_user_id),
        ).fetchone()
        if row is None or row["expires_at"] <= utc_now().isoformat():
            raise CollaborationLeaseLostError("协作 run 租约已失效。")

    @staticmethod
    def _agent(row: sqlite3.Row) -> CollaborationAgentInfo:
        return CollaborationAgentInfo(
            id=row["id"], name=row["name"], role=row["role"], prompt=row["prompt"],
            position=row["position"], is_coordinator=bool(row["is_coordinator"]),
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> CollaborationRunInfo:
        return CollaborationRunInfo(
            id=row["id"], collaboration_id=row["collaboration_id"],
            user_message=row["user_message"], status=row["status"],
            fencing_token=row["fencing_token"], terminal_event=row["terminal_event"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> CollaborationEventInfo:
        return CollaborationEventInfo(
            collaboration_id=row["collaboration_id"], sequence=row["sequence"],
            run_id=row["run_id"], event=row["event"], agent_id=row["agent_id"],
            message_id=row["message_id"], round=row["round"],
            data=json.loads(row["data_json"]), created_at=row["created_at"],
        )
