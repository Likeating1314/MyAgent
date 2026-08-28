from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from app.windows_parent_watch import (
    PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_SYNCHRONIZE,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    ParentProcessHandleError,
    WindowsParentProcessWatcher,
)


class FakeKernel32:
    def __init__(self, *, handle: int = 41, wait_results: list[int] | None = None) -> None:
        self.handle = handle
        self.wait_results = list(wait_results or [WAIT_OBJECT_0])
        self.open_calls: list[tuple[int, bool, int]] = []
        self.wait_calls: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def OpenProcess(self, access: int, inherit_handle: bool, process_id: int) -> int:
        self.open_calls.append((access, inherit_handle, process_id))
        return self.handle

    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int:
        self.wait_calls.append((handle, milliseconds))
        return self.wait_results.pop(0)

    def CloseHandle(self, handle: int) -> bool:
        self.closed.append(handle)
        return True


def test_watcher_opens_exact_process_object_and_closes_once() -> None:
    kernel = FakeKernel32(wait_results=[WAIT_TIMEOUT, WAIT_OBJECT_0])
    watcher = WindowsParentProcessWatcher(12345, kernel32=kernel)

    assert kernel.open_calls == [
        (PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, 12345)
    ]
    assert watcher.wait_until_exit(threading.Event(), interval_ms=1) is True
    assert watcher.close() is True
    assert watcher.close() is False
    assert kernel.closed == [41]


@pytest.mark.parametrize("parent_pid", [0, -1])
def test_watcher_rejects_invalid_parent_pid(parent_pid: int) -> None:
    with pytest.raises(ParentProcessHandleError):
        WindowsParentProcessWatcher(parent_pid, kernel32=FakeKernel32())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles only")
def test_parent_handle_signals_after_normal_exit() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with WindowsParentProcessWatcher(child.pid) as watcher:
        assert watcher.wait_until_exit(threading.Event(), interval_ms=25) is True
    assert child.wait(timeout=3) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles only")
def test_parent_handle_signals_after_forced_termination() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with WindowsParentProcessWatcher(child.pid) as watcher:
            child.kill()
            child.wait(timeout=3)
            assert watcher.wait_until_exit(threading.Event(), interval_ms=25) is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles only")
def test_nonexistent_parent_pid_fails_closed() -> None:
    with pytest.raises(ParentProcessHandleError):
        WindowsParentProcessWatcher(2_147_483_647)
