from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.agent.tool_executor import ToolContext
from app.tools.file_tools import _resolve_workspace_path

ALLOWED_COMMANDS = {"python", "python3", "py", "pytest", "npm", "pnpm", "uvicorn"}
DENIED_COMMANDS = {
    "rm",
    "del",
    "rmdir",
    "rd",
    "format",
    "mkfs",
    "shutdown",
    "reboot",
    "poweroff",
    "git",
}


class RunCommandArgs(BaseModel):
    command: list[str]
    cwd: str = "."
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    approval_id: str | None = None

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("command 不能为空")
        return value


def _base_command(command: list[str]) -> str:
    return Path(command[0]).name.lower().removesuffix(".exe").removesuffix(".cmd")


def _is_dangerous(command: list[str]) -> bool:
    base = _base_command(command)
    if base in DENIED_COMMANDS:
        return True
    if base not in ALLOWED_COMMANDS:
        return True
    if base == "git":
        return True
    return False


def run_command(context: ToolContext, args: RunCommandArgs) -> dict[str, Any]:
    if not context.allow_command_execution:
        raise PermissionError("命令执行当前被禁用")
    if _is_dangerous(args.command):
        raise PermissionError("该命令不在允许列表中")
    cwd = _resolve_workspace_path(context.workspace_dir, args.cwd)
    if not cwd.is_dir():
        raise NotADirectoryError(f"cwd 不是目录: {args.cwd}")
    approval_arguments = {
        "command": args.command,
        "cwd": cwd.relative_to(context.workspace_dir.resolve()).as_posix(),
        "timeout_seconds": args.timeout_seconds,
    }
    if context.approval_store is None:
        raise PermissionError("命令执行需要启用审批存储")
    approved = context.approval_store.consume_approved(
        args.approval_id,
        session_id=context.session_id,
        tool_name="run_command",
        arguments=approval_arguments,
    )
    if not approved:
        request = context.approval_store.create_pending(
            session_id=context.session_id,
            tool_name="run_command",
            arguments=approval_arguments,
            reason=f"需要执行命令: {' '.join(args.command)}",
            replacement_for=args.approval_id,
        )
        raise PermissionError(f"命令需要用户审批，审批编号: {request.id}")

    process = subprocess.Popen(
        args.command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    buffers: dict[str, list[str]] = {"stdout": [], "stderr": []}
    totals = {"all": 0, "stored": 0}
    lock = threading.Lock()

    def drain(name: str, stream) -> None:  # noqa: ANN001
        while chunk := stream.read(4096):
            with lock:
                totals["all"] += len(chunk)
                remaining = max(0, context.max_command_output_chars - totals["stored"])
                if remaining:
                    buffers[name].append(chunk[:remaining])
                    totals["stored"] += min(len(chunk), remaining)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=args.timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    return {
        "command": args.command,
        "cwd": str(cwd.relative_to(context.workspace_dir)),
        "returncode": returncode,
        "stdout": "".join(buffers["stdout"]),
        "stderr": "".join(buffers["stderr"]),
        "truncated": totals["all"] > context.max_command_output_chars,
        "original_chars": totals["all"],
    }
