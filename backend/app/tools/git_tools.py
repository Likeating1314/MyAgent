from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator

from app.agent.tool_executor import ToolContext
from app.tools.file_tools import _resolve_workspace_path

Revision = Annotated[str, Field(min_length=1, max_length=256)]
_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^+\-]*$")


def _validate_revision(value: str | None) -> str | None:
    if value is not None and not _REVISION_PATTERN.fullmatch(value):
        raise ValueError("revision 只能使用仓库内引用、对象 ID 或相对提交表达式")
    return value


def _validate_relative_paths(paths: list[str]) -> list[str]:
    for value in paths:
        candidate = Path(value)
        if not value or "\x00" in value or candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Git 路径必须是仓库内、不含路径穿越的相对路径")
        if value.startswith(("-", ":")):
            raise ValueError("Git 路径不允许选项或 pathspec magic")
    return paths


class GitOperationBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str = "."
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_output_chars: int = Field(default=40_000, ge=1_000, le=100_000)


class GitStatusOperation(GitOperationBase):
    operation: Literal["status"]
    format: Literal["short", "porcelain-v1", "porcelain-v2", "long"] = "short"
    branch: bool = True
    untracked_files: Literal["no", "normal", "all"] = "normal"


class GitDiffOperation(GitOperationBase):
    operation: Literal["diff"]
    from_revision: Revision | None = None
    to_revision: Revision | None = None
    paths: list[str] = Field(default_factory=list, max_length=100)
    staged: bool = False
    format: Literal["patch", "stat", "name-only", "name-status"] = "patch"
    unified_lines: int = Field(default=3, ge=0, le=20)
    ignore_space_change: bool = False

    _from_revision = field_validator("from_revision")(_validate_revision)
    _to_revision = field_validator("to_revision")(_validate_revision)
    _paths = field_validator("paths")(_validate_relative_paths)


class GitLogOperation(GitOperationBase):
    operation: Literal["log"]
    revision: Revision | None = None
    paths: list[str] = Field(default_factory=list, max_length=100)
    max_count: int = Field(default=20, ge=1, le=500)
    since: date | datetime | None = None
    until: date | datetime | None = None
    format: Literal["oneline", "short", "medium", "fuller"] = "oneline"

    _revision = field_validator("revision")(_validate_revision)
    _paths = field_validator("paths")(_validate_relative_paths)


class GitShowOperation(GitOperationBase):
    operation: Literal["show"]
    object: Revision = "HEAD"
    path: str | None = None
    mode: Literal["commit", "file"] = "commit"
    format: Literal["patch", "stat", "name-only", "name-status", "summary"] = "patch"
    unified_lines: int = Field(default=3, ge=0, le=20)

    _object = field_validator("object")(_validate_revision)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_relative_paths([value])
        return value


class GitBranchOperation(GitOperationBase):
    operation: Literal["branch"]
    mode: Literal["list", "show-current"] = "list"
    include_all: bool = False
    verbose: bool = False


GitOperation = Annotated[
    GitStatusOperation | GitDiffOperation | GitLogOperation | GitShowOperation | GitBranchOperation,
    Field(discriminator="operation"),
]


class GitInspectArgs(RootModel[GitOperation]):
    root: GitOperation


def _safe_git_environment() -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "",
            "PAGER": "",
            "NO_COLOR": "1",
        }
    )
    return environment


def _git_prefix(git_executable: str) -> list[str]:
    return [
        git_executable,
        "--no-pager",
        "-c",
        "color.ui=false",
        "-c",
        "core.pager=",
        "-c",
        "pager.status=false",
        "-c",
        "pager.diff=false",
        "-c",
        "pager.log=false",
        "-c",
        "pager.show=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        "interactive.diffFilter=",
        "-c",
        "i18n.logOutputEncoding=utf-8",
    ]


def _git_metadata_path(
    prefix: list[str],
    cwd: Path,
    environment: dict[str, str],
    option: str | tuple[str, ...],
) -> Path:
    options = [option] if isinstance(option, str) else list(option)
    completed = subprocess.run(
        [*prefix, "rev-parse", *options],
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        shell=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError("cwd 必须位于有效 Git 工作树内")
    result = Path(completed.stdout.strip())
    if not result.is_absolute():
        result = cwd / result
    return result.resolve()


def _ensure_within(path: Path, parent: Path, message: str) -> None:
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise ValueError(message) from exc


def _repository_paths(
    workspace: Path,
    cwd: Path,
    prefix: list[str],
    environment: dict[str, str],
) -> tuple[Path, Path]:
    repository_root = _git_metadata_path(prefix, cwd, environment, "--show-toplevel")
    git_directory = _git_metadata_path(prefix, cwd, environment, "--absolute-git-dir")
    common_directory = _git_metadata_path(prefix, cwd, environment, "--git-common-dir")
    object_directory = _git_metadata_path(prefix, cwd, environment, ("--git-path", "objects"))
    _ensure_within(repository_root, workspace, "Git 仓库根目录必须位于 workspace 内")
    _ensure_within(git_directory, workspace, "Git 元数据目录必须位于 workspace 内")
    _ensure_within(common_directory, workspace, "Git common dir 必须位于 workspace 内")
    _ensure_within(object_directory, workspace, "Git 对象目录必须位于 workspace 内")
    _ensure_within(cwd, repository_root, "cwd 必须位于 Git 仓库工作树内")
    alternates_file = object_directory / "info" / "alternates"
    if alternates_file.is_file():
        for line in alternates_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            alternate = Path(line.strip())
            if not alternate.is_absolute():
                alternate = object_directory / alternate
            _ensure_within(alternate.resolve(), workspace, "Git alternate 对象目录必须位于 workspace 内")
    if (object_directory / "info" / "http-alternates").exists():
        raise ValueError("不允许 Git HTTP alternate 对象目录")
    return repository_root, git_directory


def _literal_pathspecs(paths: list[str], *, cwd: Path, repository_root: Path, workspace: Path) -> list[str]:
    pathspecs: list[str] = []
    for value in paths:
        candidate = (cwd / value).resolve()
        _ensure_within(candidate, workspace, "Git 路径必须位于 workspace 内")
        _ensure_within(candidate, repository_root, "Git 路径必须位于当前仓库内")
        relative = candidate.relative_to(repository_root).as_posix()
        pathspecs.append(f":(top,literal){relative}")
    return pathspecs


def _append_paths(command: list[str], paths: list[str]) -> None:
    if paths:
        command.extend(["--", *paths])


def _build_status_command(prefix: list[str], operation: GitStatusOperation) -> list[str]:
    command = [*prefix, "status", f"--untracked-files={operation.untracked_files}"]
    format_flags = {
        "short": "--short",
        "porcelain-v1": "--porcelain=v1",
        "porcelain-v2": "--porcelain=v2",
    }
    if operation.format != "long":
        command.append(format_flags[operation.format])
    if operation.branch:
        command.append("--branch")
    return command


def _build_diff_command(
    prefix: list[str], operation: GitDiffOperation, pathspecs: list[str]
) -> list[str]:
    if operation.to_revision and not operation.from_revision:
        raise ValueError("to_revision 需要同时提供 from_revision")
    command = [*prefix, "diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    format_flags = {
        "patch": "--patch",
        "stat": "--stat",
        "name-only": "--name-only",
        "name-status": "--name-status",
    }
    command.append(format_flags[operation.format])
    if operation.format == "patch":
        command.append(f"--unified={operation.unified_lines}")
    if operation.ignore_space_change:
        command.append("--ignore-space-change")
    if operation.staged:
        command.append("--cached")
    if operation.from_revision:
        command.append(operation.from_revision)
    if operation.to_revision:
        command.append(operation.to_revision)
    _append_paths(command, pathspecs)
    return command


def _build_log_command(
    prefix: list[str], operation: GitLogOperation, pathspecs: list[str]
) -> list[str]:
    command = [
        *prefix,
        "log",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        f"--max-count={operation.max_count}",
    ]
    format_flags = {
        "oneline": "--oneline",
        "short": "--format=short",
        "medium": "--format=medium",
        "fuller": "--format=fuller",
    }
    command.append(format_flags[operation.format])
    if operation.since:
        command.append(f"--since={operation.since.isoformat()}")
    if operation.until:
        command.append(f"--until={operation.until.isoformat()}")
    if operation.revision:
        command.append(operation.revision)
    _append_paths(command, pathspecs)
    return command


def _build_show_command(
    prefix: list[str],
    operation: GitShowOperation,
    pathspecs: list[str],
    repository_paths: list[str],
) -> list[str]:
    command = [*prefix, "show", "--no-ext-diff", "--no-textconv", "--no-color"]
    if operation.mode == "file":
        if not repository_paths:
            raise ValueError("show file 模式必须提供仓库内 path")
        command.append(f"{operation.object}:{repository_paths[0]}")
        return command
    format_flags = {
        "patch": ["--format=medium", "--patch", f"--unified={operation.unified_lines}"],
        "stat": ["--format=medium", "--stat"],
        "name-only": ["--format=medium", "--name-only"],
        "name-status": ["--format=medium", "--name-status"],
        "summary": ["--format=medium", "--summary"],
    }
    command.extend(format_flags[operation.format])
    command.append(operation.object)
    _append_paths(command, pathspecs)
    return command


def _build_branch_command(prefix: list[str], operation: GitBranchOperation) -> list[str]:
    if operation.mode == "show-current":
        if operation.include_all or operation.verbose:
            raise ValueError("show-current 不接受 include_all 或 verbose")
        return [*prefix, "branch", "--show-current", "--no-color"]
    command = [*prefix, "branch", "--list", "--no-color"]
    if operation.include_all:
        command.append("--all")
    if operation.verbose:
        command.append("--verbose")
    return command


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    output_limit: int,
) -> tuple[int, str, str, bool, int]:
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=environment,
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
                remaining = max(0, output_limit - totals["stored"])
                if remaining:
                    visible = chunk[:remaining]
                    buffers[name].append(visible)
                    totals["stored"] += len(visible)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    return (
        returncode,
        "".join(buffers["stdout"]),
        "".join(buffers["stderr"]),
        totals["all"] > output_limit,
        totals["all"],
    )


def git_inspect(context: ToolContext, args: GitInspectArgs) -> dict[str, Any]:
    operation = args.root
    workspace = context.workspace_dir.resolve()
    cwd = _resolve_workspace_path(workspace, operation.cwd)
    if not cwd.is_dir():
        raise NotADirectoryError(f"cwd 不是目录: {operation.cwd}")
    git_executable = shutil.which("git")
    if not git_executable:
        raise RuntimeError("系统未安装 Git")
    environment = _safe_git_environment()
    prefix = _git_prefix(git_executable)
    repository_root, _ = _repository_paths(workspace, cwd, prefix, environment)

    raw_paths: list[str] = []
    if isinstance(operation, (GitDiffOperation, GitLogOperation)):
        raw_paths = operation.paths
    elif isinstance(operation, GitShowOperation) and operation.path:
        raw_paths = [operation.path]
    pathspecs = _literal_pathspecs(raw_paths, cwd=cwd, repository_root=repository_root, workspace=workspace)
    repository_paths = [item.removeprefix(":(top,literal)") for item in pathspecs]

    if isinstance(operation, GitStatusOperation):
        command = _build_status_command(prefix, operation)
    elif isinstance(operation, GitDiffOperation):
        command = _build_diff_command(prefix, operation, pathspecs)
    elif isinstance(operation, GitLogOperation):
        command = _build_log_command(prefix, operation, pathspecs)
    elif isinstance(operation, GitShowOperation):
        command = _build_show_command(prefix, operation, pathspecs, repository_paths)
    else:
        command = _build_branch_command(prefix, operation)

    returncode, stdout, stderr, truncated, total_output_chars = _run_bounded(
        command,
        cwd=cwd,
        environment=environment,
        timeout_seconds=operation.timeout_seconds,
        output_limit=operation.max_output_chars,
    )
    return {
        "operation": operation.operation,
        "cwd": cwd.relative_to(workspace).as_posix(),
        "repository_root": repository_root.relative_to(workspace).as_posix(),
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
        "total_output_chars": total_output_chars,
    }
