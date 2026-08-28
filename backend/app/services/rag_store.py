from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID

from app.agent.memory import utc_now
from app.tools.file_tools import IGNORED_DIRS, _workspace_relative
from app.security import current_user_id

TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".ts",
    ".tsx",
    ".vue",
    ".js",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".css",
    ".html",
}


class RagStore:
    def __init__(self, database_path: Path, workspace_dir: Path) -> None:
        self.database_path = database_path
        self.workspace_dir = workspace_dir
        self._remove_cross_user_legacy_paths()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def scoped_to(self, workspace_dir: Path) -> "RagStore":
        """Return a view that can only crawl the already validated tool workspace."""
        return RagStore(self.database_path, workspace_dir.resolve())

    def _remove_cross_user_legacy_paths(self) -> None:
        """Remove rows created when the old production store crawled workspace/users."""
        with self._connect() as connection:
            try:
                rows = connection.execute(
                    "SELECT owner_user_id, path FROM rag_documents "
                    "WHERE owner_user_id IS NOT NULL AND path LIKE 'users/%/%'"
                ).fetchall()
            except sqlite3.OperationalError:
                return
            polluted: list[tuple[str, str]] = []
            for row in rows:
                parts = str(row["path"]).replace("\\", "/").split("/")
                if len(parts) < 3 or parts[0] != "users":
                    continue
                try:
                    path_user_id = str(UUID(parts[1]))
                    owner_user_id = str(UUID(row["owner_user_id"]))
                except (ValueError, TypeError):
                    continue
                if path_user_id != owner_user_id:
                    polluted.append((row["owner_user_id"], row["path"]))
            connection.executemany(
                "DELETE FROM rag_documents WHERE owner_user_id = ? AND path = ?",
                polluted,
            )

    def index_workspace(self, *, max_files: int = 300, max_chars_per_file: int = 80_000) -> dict[str, Any]:
        owner_user_id = current_user_id()
        indexed = 0
        skipped = 0
        for path in self.workspace_dir.rglob("*"):
            if indexed >= max_files:
                break
            if not path.is_file():
                continue
            if any(part in IGNORED_DIRS for part in path.parts):
                skipped += 1
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                skipped += 1
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")[:max_chars_per_file]
            except OSError:
                skipped += 1
                continue
            relative = _workspace_relative(path, self.workspace_dir)
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO rag_documents (owner_user_id, path, content, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(owner_user_id, path) DO UPDATE SET
                        content = excluded.content,
                        updated_at = excluded.updated_at
                    """,
                    (owner_user_id, relative, content, utc_now().isoformat()),
                )
            indexed += 1
        return {"indexed": indexed, "skipped": skipped, "limit_reached": indexed >= max_files}

    def search(self, query: str, *, max_results: int = 8) -> dict[str, Any]:
        terms = [term.lower() for term in query.split() if term.strip()]
        if not terms:
            terms = [query.lower()]
        with self._connect() as connection:
            rows = connection.execute("SELECT path, content, updated_at FROM rag_documents WHERE owner_user_id = ?", (current_user_id(),)).fetchall()

        scored: list[dict[str, Any]] = []
        for row in rows:
            content = row["content"]
            lowered = content.lower()
            score = sum(lowered.count(term) for term in terms)
            if score <= 0:
                continue
            first_index = min((lowered.find(term) for term in terms if term in lowered), default=0)
            start = max(first_index - 120, 0)
            end = min(first_index + 360, len(content))
            scored.append(
                {
                    "path": row["path"],
                    "score": score,
                    "snippet": content[start:end],
                    "updated_at": row["updated_at"],
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {"query": query, "matches": scored[:max_results], "total_matches": len(scored)}
