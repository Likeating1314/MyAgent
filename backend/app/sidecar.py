from __future__ import annotations

import argparse
import threading

import uvicorn

from app.main import app
from app.windows_parent_watch import ParentProcessHandleError, WindowsParentProcessWatcher


def run_server(port: int, parent_pid: int) -> None:
    watcher = WindowsParentProcessWatcher(parent_pid)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_config=None,
        )
    )
    watcher_stop = threading.Event()

    def watch_parent() -> None:
        try:
            if watcher.wait_until_exit(watcher_stop):
                server.should_exit = True
        except ParentProcessHandleError:
            server.should_exit = True

    watch_thread = threading.Thread(
        target=watch_parent,
        name="electron-parent-watch",
        daemon=True,
    )
    watch_thread.start()
    try:
        server.run()
    finally:
        watcher_stop.set()
        watch_thread.join(timeout=1)
        watcher.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MyAgent backend sidecar")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65_535:
        parser.error("port must be between 1 and 65535")
    if args.parent_pid <= 0:
        parser.error("parent-pid must be positive")

    run_server(args.port, args.parent_pid)


if __name__ == "__main__":
    main()
