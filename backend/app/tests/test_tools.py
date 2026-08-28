from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest

from app.agent.tool_executor import ToolContext
from app.services.approval_store import ApprovalStore
from app.tools.command_tools import RunCommandArgs, run_command
from app.tools.file_tools import ListFilesArgs, ReadFileArgs, WriteFileArgs, list_files, read_file, write_file
from app.tools.git_tools import GitInspectArgs, git_inspect
from app.tools.search_tools import SearchTextArgs, search_text


def test_file_roundtrip(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "agent.sqlite3")
    context = ToolContext(workspace_dir=tmp_path, session_id="s1", approval_store=store)
    args = WriteFileArgs(path="notes.txt", content="hello TODO world", overwrite=True)
    with pytest.raises(PermissionError):
        write_file(context, args)
    approval = store.list_requests(status="pending")[0]
    store.set_status(approval.id, "approved")
    write_result = write_file(context, args.model_copy(update={"approval_id": approval.id}))
    assert write_result["written_chars"] == 16

    read_result = read_file(context, ReadFileArgs(path="notes.txt"))
    assert "hello TODO world" in read_result["content"]


def test_list_and_search(tmp_path: Path) -> None:
    context = ToolContext(workspace_dir=tmp_path, allow_command_execution=False)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.txt").write_text("TODO one\nnothing", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignore.txt").write_text("TODO hidden", encoding="utf-8")

    files = list_files(context, ListFilesArgs(path="."))
    paths = {entry["path"] for entry in files["entries"]}
    assert "nested" in paths
    assert "nested/a.txt" in paths
    assert ".git" not in paths

    search_result = search_text(context, SearchTextArgs(query="TODO", path="."))
    assert len(search_result["matches"]) == 1
    assert search_result["matches"][0]["path"] == "nested/a.txt"


def test_run_command_rejected_by_default(tmp_path: Path) -> None:
    context = ToolContext(workspace_dir=tmp_path, allow_command_execution=False)
    with pytest.raises(PermissionError):
        run_command(context, RunCommandArgs(command=["pytest", "--version"]))


def test_run_command_allowed(tmp_path: Path) -> None:
    store = ApprovalStore(tmp_path / "agent.sqlite3")
    context = ToolContext(
        workspace_dir=tmp_path,
        session_id="s1",
        allow_command_execution=True,
        approval_store=store,
    )
    args = RunCommandArgs(command=[sys.executable, "--version"])
    with pytest.raises(PermissionError):
        run_command(context, args)
    approval = store.list_requests(status="pending")[0]
    store.set_status(approval.id, "approved")
    result = run_command(context, args.model_copy(update={"approval_id": approval.id}))
    assert result["returncode"] == 0


def test_git_read_only_inspection_still_works(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    context = ToolContext(workspace_dir=tmp_path)
    result = git_inspect(context, GitInspectArgs.model_validate({"operation": "status", "format": "short"}))
    assert result["returncode"] == 0
    assert result["operation"] == "status"
