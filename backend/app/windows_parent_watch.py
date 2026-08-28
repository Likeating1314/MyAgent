from __future__ import annotations

import ctypes
import os
import sys
import threading
from ctypes import wintypes
from typing import Protocol

PROCESS_SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF


class ParentProcessHandleError(RuntimeError):
    pass


class Kernel32Protocol(Protocol):
    def OpenProcess(self, access: int, inherit_handle: bool, process_id: int): ...
    def WaitForSingleObject(self, handle, milliseconds: int) -> int: ...
    def CloseHandle(self, handle) -> bool: ...


def _load_kernel32() -> Kernel32Protocol:
    if sys.platform != "win32":
        raise ParentProcessHandleError("parent process handles are only supported on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class WindowsParentProcessWatcher:
    def __init__(self, parent_pid: int, *, kernel32: Kernel32Protocol | None = None) -> None:
        if parent_pid <= 0 or parent_pid == os.getpid():
            raise ParentProcessHandleError("invalid parent process id")
        self._kernel32 = kernel32 or _load_kernel32()
        self._handle = self._kernel32.OpenProcess(
            PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            parent_pid,
        )
        if not self._handle:
            error_code = ctypes.get_last_error() if kernel32 is None else 0
            raise ParentProcessHandleError(
                f"unable to open parent process handle (winerror={error_code})"
            )
        self._closed = False

    def wait_until_exit(
        self, stop_event: threading.Event, *, interval_ms: int = 250
    ) -> bool:
        if interval_ms < 1:
            raise ValueError("interval_ms must be positive")
        while not stop_event.is_set():
            result = self._kernel32.WaitForSingleObject(self._handle, interval_ms)
            if result == WAIT_OBJECT_0:
                return True
            if result == WAIT_TIMEOUT:
                continue
            if result == WAIT_FAILED:
                error_code = ctypes.get_last_error()
                raise ParentProcessHandleError(
                    f"waiting for parent process failed (winerror={error_code})"
                )
            raise ParentProcessHandleError(
                f"waiting for parent process returned unexpected status {result}"
            )
        return False

    def close(self) -> bool:
        if self._closed:
            return False
        self._closed = True
        handle = self._handle
        self._handle = None
        if handle:
            self._kernel32.CloseHandle(handle)
        return True

    def __enter__(self) -> WindowsParentProcessWatcher:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
