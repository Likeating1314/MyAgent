from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.tool_executor import ToolContext
from app.tools.file_tools import IGNORED_DIRS, _resolve_workspace_path, _workspace_relative


class SearchTextArgs(BaseModel):
    query: str
    path: str = "."
    case_sensitive: bool = False
    max_results: int = Field(default=50, ge=1, le=500)


def _is_text_file(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in sample


def search_text(context: ToolContext, args: SearchTextArgs) -> dict[str, Any]:
    root = _resolve_workspace_path(context.workspace_dir, args.path)
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {args.path}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {args.path}")

    needle = args.query if args.case_sensitive else args.query.lower()
    matches: list[dict[str, Any]] = []

    for current_root, dirs, files in __import__("os").walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        for file_name in files:
            if file_name in IGNORED_DIRS:
                continue
            file_path = Path(current_root) / file_name
            if not _is_text_file(file_path):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, start=1):
                haystack = line if args.case_sensitive else line.lower()
                if needle in haystack:
                    matches.append(
                        {
                            "path": _workspace_relative(file_path, context.workspace_dir),
                            "line_number": line_number,
                            "line": line,
                        }
                    )
                    if len(matches) >= args.max_results:
                        return {"query": args.query, "matches": matches, "truncated": True}
    return {"query": args.query, "matches": matches, "truncated": False}
