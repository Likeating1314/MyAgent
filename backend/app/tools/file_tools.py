from __future__ import annotations

import difflib
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.agent.tool_executor import ToolContext

logger = logging.getLogger(__name__)

IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__"}


class ReadFileArgs(BaseModel):
    path: str
    max_chars: int = Field(default=20_000, ge=1, le=200_000)


class WriteFileArgs(BaseModel):
    path: str
    content: str
    overwrite: bool = True
    approval_id: str | None = None


class ListFilesArgs(BaseModel):
    path: str = "."
    max_depth: int = Field(default=6, ge=0, le=32)


def _resolve_workspace_path(workspace_dir: Path, target_path: str) -> Path:
    base = workspace_dir.resolve()
    candidate = Path(target_path)
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("路径必须位于工作目录内") from exc
    return candidate


def _workspace_relative(path: Path, workspace_dir: Path) -> str:
    return path.relative_to(workspace_dir.resolve()).as_posix()


def read_file(context: ToolContext, args: ReadFileArgs) -> dict[str, Any]:
    target = _resolve_workspace_path(context.workspace_dir, args.path)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {args.path}")
    content = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(content) > args.max_chars
    visible = content[: args.max_chars]
    return {
        "path": _workspace_relative(target, context.workspace_dir),
        "content": visible,
        "truncated": truncated,
        "total_chars": len(content),
    }


def write_file(context: ToolContext, args: WriteFileArgs) -> dict[str, Any]:
    target = _resolve_workspace_path(context.workspace_dir, args.path)
    existed = target.exists()
    if existed and not target.is_file():
        raise IsADirectoryError(f"目标不是普通文件: {args.path}")
    if existed and not args.overwrite:
        raise FileExistsError(f"文件已存在且不允许覆盖: {args.path}")
    relative_path = _workspace_relative(target, context.workspace_dir)
    previous_bytes = target.read_bytes() if existed else None
    previous_hash = hashlib.sha256(previous_bytes).hexdigest() if previous_bytes is not None else None
    previous_text = previous_bytes.decode("utf-8", errors="replace") if previous_bytes is not None else ""
    diff = "".join(
        difflib.unified_diff(
            previous_text.splitlines(keepends=True),
            args.content.splitlines(keepends=True),
            fromfile=f"a/{relative_path}" if existed else "/dev/null",
            tofile=f"b/{relative_path}",
        )
    )
    if len(diff) > 20_000:
        diff = f"{diff[:20_000]}\n... diff 已截断 ...\n"
    approval_arguments = {
        "path": relative_path,
        "content": args.content,
        "overwrite": args.overwrite,
        "expected_sha256": previous_hash,
    }
    if context.approval_store is None:
        raise PermissionError("写文件需要启用审批存储")
    approved = context.approval_store.consume_approved(
        args.approval_id,
        session_id=context.session_id,
        tool_name="write_file",
        arguments=approval_arguments,
    )
    if not approved:
        request = context.approval_store.create_pending(
            session_id=context.session_id,
            tool_name="write_file",
            arguments=approval_arguments,
            reason=f"需要{'覆盖' if existed else '新建'}文件: {relative_path}",
            details={
                "path": relative_path,
                "change_type": "overwrite" if existed else "create",
                "before_sha256": previous_hash,
                "diff": diff,
            },
            replacement_for=args.approval_id,
        )
        raise PermissionError(f"写文件需要用户审批，审批编号: {request.id}")

    target.parent.mkdir(parents=True, exist_ok=True)
    logger.info("write_file path=%s overwrite=%s", target, args.overwrite)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            delete=False,
            dir=target.parent,
        ) as temporary:
            temporary.write(args.content)
            temporary_path = temporary.name
        os.replace(temporary_path, target)
    finally:
        if temporary_path and Path(temporary_path).exists():
            Path(temporary_path).unlink()
    return {
        "path": relative_path,
        "written_chars": len(args.content),
        "overwritten": existed,
    }


def list_files(context: ToolContext, args: ListFilesArgs) -> dict[str, Any]:
    root = _resolve_workspace_path(context.workspace_dir, args.path)
    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {args.path}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {args.path}")

    entries: list[dict[str, Any]] = []
    for current_root, dirs, files in __import__("os").walk(root):
        current = Path(current_root)
        rel = current.relative_to(context.workspace_dir)
        depth = 0 if str(rel) == "." else len(rel.parts)
        if depth > args.max_depth:
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        for directory in dirs:
            entries.append({"path": _workspace_relative(current / directory, context.workspace_dir), "type": "directory"})
        for file_name in files:
            if file_name in IGNORED_DIRS:
                continue
            entries.append({"path": _workspace_relative(current / file_name, context.workspace_dir), "type": "file"})
    entries.sort(key=lambda item: item["path"])
    return {"root": _workspace_relative(root, context.workspace_dir), "entries": entries}
