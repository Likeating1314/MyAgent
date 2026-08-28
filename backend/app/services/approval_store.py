from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.agent.memory import utc_now
from app.models.schemas import ApprovalRequest
from app.security import current_user_id


class ApprovalResumeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.session_id = session_id


class ApprovalMutationError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def canonicalize_arguments(arguments: dict[str, Any]) -> str:
    sanitized = {key: value for key, value in arguments.items() if key != "approval_id"}
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ApprovalStore:
    def __init__(self, database_path: Path, *, ttl_seconds: int = 900) -> None:
        self.database_path = database_path
        self.ttl_seconds = ttl_seconds
        self._init_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    owner_user_id TEXT,
                    session_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    consumed_at TEXT,
                    last_resume_outcome TEXT,
                    replacement_approval_id TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(approvals)")}
            migrations = {
                "details_json": "ALTER TABLE approvals ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'",
                "expires_at": "ALTER TABLE approvals ADD COLUMN expires_at TEXT",
                "consumed_at": "ALTER TABLE approvals ADD COLUMN consumed_at TEXT",
                "last_resume_outcome": "ALTER TABLE approvals ADD COLUMN last_resume_outcome TEXT",
                "replacement_approval_id": "ALTER TABLE approvals ADD COLUMN replacement_approval_id TEXT",
                "owner_user_id": "ALTER TABLE approvals ADD COLUMN owner_user_id TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            connection.execute("CREATE INDEX IF NOT EXISTS ix_approvals_owner_status ON approvals(owner_user_id, status, created_at DESC)")

    def create_pending(
        self,
        *,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
        details: dict[str, Any] | None = None,
        replacement_for: str | None = None,
    ) -> ApprovalRequest:
        now = utc_now()
        expires_at = now + timedelta(seconds=self.ttl_seconds)
        canonical_arguments = canonicalize_arguments(arguments)
        normalized_arguments = json.loads(canonical_arguments)
        request = ApprovalRequest(
            id=uuid4().hex,
            session_id=session_id,
            tool_name=tool_name,
            arguments=normalized_arguments,
            reason=reason,
            details=details or {},
            status="pending",
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM approvals
                WHERE session_id = ? AND owner_user_id = ?
                  AND tool_name = ?
                  AND arguments_json = ?
                  AND status = 'pending'
                  AND expires_at IS NOT NULL
                  AND expires_at > ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, current_user_id(), tool_name, canonical_arguments, now.isoformat()),
            ).fetchone()
            if existing is not None:
                result = self._row_to_request(existing)
            else:
                connection.execute(
                    """
                    INSERT INTO approvals (
                        id, owner_user_id, session_id, tool_name, arguments_json, reason, details_json,
                        status, created_at, updated_at, expires_at, consumed_at,
                        last_resume_outcome, replacement_approval_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request.id,
                        current_user_id(),
                        request.session_id,
                        request.tool_name,
                        canonical_arguments,
                        request.reason,
                        json.dumps(request.details, ensure_ascii=False),
                        request.status,
                        request.created_at.isoformat(),
                        request.updated_at.isoformat(),
                        request.expires_at.isoformat(),
                        None,
                        None,
                        None,
                    ),
                )
                result = request
            if replacement_for:
                original = connection.execute(
                    """
                    SELECT replacement_approval_id FROM approvals
                    WHERE id = ? AND owner_user_id = ? AND session_id = ?
                      AND tool_name = ? AND status = 'approved'
                    """,
                    (replacement_for, current_user_id(), session_id, tool_name),
                ).fetchone()
                if original is not None:
                    previous = original["replacement_approval_id"]
                    if previous and previous != result.id:
                        connection.execute(
                            """
                            UPDATE approvals SET expires_at = ?, updated_at = ?
                            WHERE id = ? AND owner_user_id = ? AND status = 'pending'
                            """,
                            (now.isoformat(), now.isoformat(), previous, current_user_id()),
                        )
                    connection.execute(
                        """
                        UPDATE approvals SET replacement_approval_id = ?, updated_at = ?
                        WHERE id = ? AND owner_user_id = ? AND status = 'approved'
                        """,
                        (result.id, now.isoformat(), replacement_for, current_user_id()),
                    )
        return result

    def list_requests(self, status: str | None = None) -> list[ApprovalRequest]:
        query = "SELECT * FROM approvals WHERE (owner_user_id = ? OR (owner_user_id IS NULL AND ? = '00000000-0000-0000-0000-000000000001'))"
        params: tuple[str, ...] = (current_user_id(), current_user_id())
        if status:
            query += " AND status = ?"
            params = (current_user_id(), current_user_id(), status)
        query += " ORDER BY created_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_request(row) for row in rows]

    def get(self, request_id: str) -> ApprovalRequest | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ? AND (owner_user_id = ? OR (owner_user_id IS NULL AND ? = '00000000-0000-0000-0000-000000000001'))", (request_id, current_user_id(), current_user_id())).fetchone()
        return self._row_to_request(row) if row else None

    def require_resumable(self, request_id: str) -> ApprovalRequest:
        request = self.get(request_id)
        if request is None:
            raise ApprovalResumeError("approval_not_found", "审批请求不存在。", status_code=404)
        if request.replacement_approval_id:
            raise ApprovalResumeError(
                "approval_replaced",
                "该审批已由新的待审批请求替代，请使用替代审批继续。",
                session_id=request.session_id,
            )
        if request.status == "consumed":
            raise ApprovalResumeError(
                "approval_consumed",
                "审批已消费，执行结果可能已产生；为避免重复副作用，不能自动重试。",
            )
        if request.status == "rejected":
            raise ApprovalResumeError("approval_rejected", "审批请求已拒绝。")
        if request.expires_at <= utc_now():
            raise ApprovalResumeError("approval_expired", "审批请求已过期，不能继续执行。")
        if request.status == "pending":
            raise ApprovalResumeError("approval_pending", "审批请求尚未批准。")
        if request.status != "approved":
            raise ApprovalResumeError("approval_invalid_state", "审批状态不允许继续执行。")
        return request

    def has_open_for_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM approvals
                WHERE session_id = ? AND owner_user_id = ? AND status IN ('pending', 'approved')
                  AND expires_at IS NOT NULL AND expires_at > ?
                LIMIT 1
                """,
                (session_id, current_user_id(), utc_now().isoformat()),
            ).fetchone()
        return row is not None

    def set_resume_outcome_fenced(
        self,
        request_id: str,
        outcome: str | None,
        *,
        session_id: str,
        run_id: str,
    ) -> bool:
        if outcome not in {None, "cancelled"}:
            raise ValueError("无效的审批续跑结果")
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT 1 FROM session_runs
                WHERE session_id = ? AND run_id = ? AND expires_at > ?
                  AND EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.session_id = session_runs.session_id AND s.owner_user_id = ?
                  )
                """,
                (session_id, run_id, now, current_user_id()),
            ).fetchone()
            if lease is None:
                return False
            approval = connection.execute(
                "SELECT status FROM approvals WHERE id = ? AND owner_user_id = ? AND session_id = ?",
                (request_id, current_user_id(), session_id),
            ).fetchone()
            if approval is None:
                raise ApprovalResumeError(
                    "approval_not_found", "审批请求不存在。", status_code=404
                )
            if outcome == "cancelled":
                has_tool_record = connection.execute(
                    """
                    SELECT 1 FROM session_events
                    WHERE session_id = ?
                      AND event_type = 'tool_call'
                      AND json_extract(payload_json, '$.arguments.approval_id') = ?
                    LIMIT 1
                    """,
                    (session_id, request_id),
                ).fetchone()
                if approval["status"] in {"consumed", "rejected"} or has_tool_record is not None:
                    return True
            connection.execute(
                "UPDATE approvals SET last_resume_outcome = ? WHERE id = ? AND owner_user_id = ? AND session_id = ?",
                (outcome, request_id, current_user_id(), session_id),
            )
            return True

    def set_resume_outcome_unfenced_for_memory(
        self, request_id: str, outcome: str | None, *, session_id: str
    ) -> None:
        if outcome not in {None, "cancelled"}:
            raise ValueError("无效的审批续跑结果")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM approvals WHERE id = ? AND owner_user_id = ? AND session_id = ?",
                (request_id, current_user_id(), session_id),
            ).fetchone()
            if row is None:
                raise ApprovalResumeError(
                    "approval_not_found", "审批请求不存在。", status_code=404
                )
            if outcome == "cancelled" and row["status"] in {"consumed", "rejected"}:
                return
            connection.execute(
                "UPDATE approvals SET last_resume_outcome = ? WHERE id = ? AND owner_user_id = ? AND session_id = ?",
                (outcome, request_id, current_user_id(), session_id),
            )

    def set_status(self, request_id: str, status: str) -> ApprovalRequest:
        if status not in {"approved", "rejected"}:
            raise ValueError("审批只能设置为 approved 或 rejected")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status, expires_at FROM approvals WHERE id = ? AND owner_user_id = ?", (request_id, current_user_id())).fetchone()
            if row is None:
                raise ApprovalMutationError(
                    "approval_not_found", "审批请求不存在。", status_code=404
                )
            if row["status"] == "consumed":
                raise ApprovalMutationError(
                    "approval_consumed", "审批已消费，不能再次修改状态。"
                )
            if row["status"] != "pending":
                raise ApprovalMutationError(
                    "approval_invalid_state", "审批当前状态不允许执行此操作。"
                )
            if row["expires_at"] is None or row["expires_at"] <= now.isoformat():
                raise ApprovalMutationError("approval_expired", "审批请求已过期。")
            connection.execute(
                "UPDATE approvals SET status = ?, updated_at = ? WHERE id = ? AND owner_user_id = ? AND status = 'pending'",
                (status, now.isoformat(), request_id, current_user_id()),
            )
        request = self.get(request_id)
        if request is None:
            raise ApprovalMutationError(
                "approval_not_found", "审批请求不存在。", status_code=404
            )
        return request

    def consume_approved(
        self,
        request_id: str | None,
        *,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        if not request_id:
            return False
        now = utc_now()
        canonical_arguments = canonicalize_arguments(arguments)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE approvals
                SET status = 'consumed', updated_at = ?, consumed_at = ?
                WHERE id = ?
                  AND owner_user_id = ?
                  AND session_id = ?
                  AND tool_name = ?
                  AND arguments_json = ?
                  AND status = 'approved'
                  AND expires_at IS NOT NULL
                  AND expires_at > ?
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    request_id,
                    current_user_id(),
                    session_id,
                    tool_name,
                    canonical_arguments,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
        expires_at = row["expires_at"] or row["created_at"]
        return ApprovalRequest(
            id=row["id"],
            session_id=row["session_id"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"]),
            reason=row["reason"],
            details=json.loads(row["details_json"] or "{}"),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=expires_at,
            consumed_at=row["consumed_at"],
            last_resume_outcome=row["last_resume_outcome"],
            replacement_approval_id=row["replacement_approval_id"],
        )
