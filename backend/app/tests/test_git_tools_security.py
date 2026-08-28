from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.tools.git_tools as git_module
from app.agent.tool_executor import ToolContext
from app.tools.git_tools import GitInspectArgs, git_inspect


def run_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, Path, ToolContext]:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    run_git(repo, "init")
    run_git(repo, "config", "user.name", "Security Test")
    run_git(repo, "config", "user.email", "security@example.test")
    tracked = repo / "tracked.txt"
    tracked.write_text("first line\n", encoding="utf-8")
    run_git(repo, "add", "tracked.txt")
    run_git(repo, "commit", "-m", "initial")
    run_git(repo, "branch", "keep-branch")
    return workspace, repo, ToolContext(workspace_dir=workspace)


def inspect(context: ToolContext, payload: dict) -> dict:
    return git_inspect(context, GitInspectArgs.model_validate(payload))


def branch_names(repo: Path) -> list[str]:
    output = run_git(repo, "branch", "--format=%(refname:short)").stdout
    return sorted(line for line in output.splitlines() if line)


@pytest.mark.parametrize(
    "dangerous_args",
    [
        ["-d", "keep-branch"],
        ["-D", "keep-branch"],
        ["-m", "renamed"],
        ["-M", "renamed"],
        ["-c", "created"],
        ["-C", "created"],
        ["-f", "forced"],
        ["--delete", "keep-branch"],
        ["--move", "renamed"],
        ["--copy", "copied"],
        ["--set-upstream-to=origin/main"],
    ],
)
def test_branch_mutation_arguments_are_rejected_without_changes(
    repository: tuple[Path, Path, ToolContext], dangerous_args: list[str]
) -> None:
    _, repo, _ = repository
    before_branches = branch_names(repo)
    before_status = run_git(repo, "status", "--porcelain=v1").stdout
    before_content = (repo / "tracked.txt").read_bytes()

    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate({"operation": "branch", "args": dangerous_args})

    assert branch_names(repo) == before_branches
    assert run_git(repo, "status", "--porcelain=v1").stdout == before_status
    assert (repo / "tracked.txt").read_bytes() == before_content


@pytest.mark.parametrize("mode", ["create", "delete", "move", "copy", "set-upstream"])
def test_branch_mutation_modes_are_not_in_schema(
    repository: tuple[Path, Path, ToolContext], mode: str
) -> None:
    _, repo, _ = repository
    before = branch_names(repo)
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate({"operation": "branch", "mode": mode, "name": "changed"})
    assert branch_names(repo) == before


def test_show_output_argument_cannot_create_file(repository: tuple[Path, Path, ToolContext]) -> None:
    workspace, repo, _ = repository
    output = workspace / "leaked.txt"
    before_status = run_git(repo, "status", "--porcelain=v1").stdout
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate(
            {"operation": "show", "args": [f"--output={output}", "HEAD"]}
        )
    assert not output.exists()
    assert run_git(repo, "status", "--porcelain=v1").stdout == before_status


def test_diff_no_index_cannot_read_outside_workspace(repository: tuple[Path, Path, ToolContext], tmp_path: Path) -> None:
    _, repo, context = repository
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    tracked_before = (repo / "tracked.txt").read_bytes()
    branches_before = branch_names(repo)

    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate(
            {"operation": "diff", "args": ["--no-index", "tracked.txt", str(outside)]}
        )
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate({"operation": "diff", "paths": ["../outside-secret.txt"]})
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate({"operation": "diff", "paths": [str(outside)]})

    assert outside.read_text(encoding="utf-8") == "outside secret"
    assert (repo / "tracked.txt").read_bytes() == tracked_before
    assert branch_names(repo) == branches_before


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "diff", "args": ["--ext-diff"]},
        {"operation": "diff", "args": ["--textconv"]},
        {"operation": "show", "args": ["--ext-diff", "HEAD"]},
        {"operation": "show", "args": ["--textconv", "HEAD"]},
    ],
)
def test_external_diff_and_textconv_arguments_are_rejected(payload: dict) -> None:
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "show", "object": "--output=leaked.txt"},
        {"operation": "diff", "from_revision": "--no-index"},
        {"operation": "diff", "paths": ["--ext-diff"]},
        {"operation": "show", "path": ":(attr:diff)tracked.txt"},
    ],
)
def test_option_injection_cannot_hide_in_structured_fields(payload: dict) -> None:
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate(payload)


def test_legacy_subcommand_and_args_shape_is_rejected() -> None:
    with pytest.raises(ValidationError):
        GitInspectArgs.model_validate({"subcommand": "status", "args": ["--short"]})


def test_legal_read_only_operations_execute(repository: tuple[Path, Path, ToolContext]) -> None:
    _, repo, context = repository
    (repo / "tracked.txt").write_text("first line\nsecond line\n", encoding="utf-8")
    before_branches = branch_names(repo)

    status = inspect(context, {"operation": "status", "cwd": "repo", "format": "porcelain-v1"})
    diff = inspect(
        context,
        {"operation": "diff", "cwd": "repo", "paths": ["tracked.txt"], "format": "patch"},
    )
    log = inspect(context, {"operation": "log", "cwd": "repo", "max_count": 1, "format": "oneline"})
    show = inspect(context, {"operation": "show", "cwd": "repo", "object": "HEAD", "format": "summary"})
    show_file = inspect(
        context,
        {"operation": "show", "cwd": "repo", "object": "HEAD", "path": "tracked.txt", "mode": "file"},
    )
    branches = inspect(context, {"operation": "branch", "cwd": "repo", "mode": "list"})
    current = inspect(context, {"operation": "branch", "cwd": "repo", "mode": "show-current"})

    for result in [status, diff, log, show, show_file, branches, current]:
        assert result["returncode"] == 0
        assert result["truncated"] is False
    assert "tracked.txt" in status["stdout"]
    assert "+second line" in diff["stdout"]
    assert "initial" in log["stdout"]
    assert "first line" in show_file["stdout"]
    assert "keep-branch" in branches["stdout"]
    assert current["stdout"].strip()
    assert branch_names(repo) == before_branches
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "first line\nsecond line\n"


def test_execution_environment_disables_git_process_hooks(
    repository: tuple[Path, Path, ToolContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, context = repository
    real_run = git_module.subprocess.run
    observed_environments: list[dict[str, str]] = []

    def recording_run(*args, **kwargs):  # noqa: ANN002, ANN003
        observed_environments.append(kwargs["env"])
        assert kwargs["shell"] is False
        assert kwargs["stdin"] is subprocess.DEVNULL
        return real_run(*args, **kwargs)

    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "must-not-run")
    monkeypatch.setenv("GIT_PAGER", "must-not-run")
    monkeypatch.setattr(git_module.subprocess, "run", recording_run)
    result = inspect(context, {"operation": "status", "cwd": "repo"})

    assert result["returncode"] == 0
    assert observed_environments
    for environment in observed_environments:
        assert "GIT_EXTERNAL_DIFF" not in environment
        assert environment["GIT_PAGER"] == ""
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"


def test_git_output_is_truncated_before_returning_to_agent(
    repository: tuple[Path, Path, ToolContext]
) -> None:
    _, repo, context = repository
    (repo / "tracked.txt").write_text("first line\n" + ("changed output\n" * 500), encoding="utf-8")
    result = inspect(
        context,
        {
            "operation": "diff",
            "cwd": "repo",
            "paths": ["tracked.txt"],
            "max_output_chars": 1_000,
        },
    )
    assert result["returncode"] == 0
    assert result["truncated"] is True
    assert len(result["stdout"]) + len(result["stderr"]) <= 1_000
    assert result["total_output_chars"] > 1_000


def test_repository_and_git_metadata_must_be_inside_workspace(tmp_path: Path) -> None:
    outside_repo = tmp_path / "outside-repo"
    outside_repo.mkdir()
    run_git(outside_repo, "init")
    workspace = outside_repo / "nested-workspace"
    workspace.mkdir()
    context = ToolContext(workspace_dir=workspace)
    with pytest.raises(ValueError, match="仓库根目录"):
        inspect(context, {"operation": "status"})


def test_external_alternate_object_store_is_rejected(
    repository: tuple[Path, Path, ToolContext], tmp_path: Path
) -> None:
    _, repo, context = repository
    outside_objects = tmp_path / "outside-objects"
    outside_objects.mkdir()
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(outside_objects), encoding="utf-8")
    before_branches = branch_names(repo)
    before_content = (repo / "tracked.txt").read_bytes()

    with pytest.raises(ValueError, match="alternate"):
        inspect(context, {"operation": "show", "cwd": "repo", "object": "HEAD"})

    assert branch_names(repo) == before_branches
    assert (repo / "tracked.txt").read_bytes() == before_content


def test_git_cwd_cannot_escape_workspace(repository: tuple[Path, Path, ToolContext]) -> None:
    _, _, context = repository
    with pytest.raises(ValueError, match="工作目录"):
        inspect(context, {"operation": "status", "cwd": "../"})
