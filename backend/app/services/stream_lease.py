from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from typing import Any, Literal

from app.security import current_user_id, reset_current_user, set_current_user
from app.services.session_store import SessionStore


LeaseOwner = Literal["route", "response", "generator", "closed"]
logger = logging.getLogger(__name__)


class StreamRunLeaseGuard:
    """Own one fenced session run across route/response/generator handoff."""

    def __init__(self, store: SessionStore, session_id: str, run_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.run_id = run_id
        self.owner_user_id = current_user_id()
        self._lock = threading.Lock()
        self._owner: LeaseOwner = "route"
        self._release_done = threading.Event()
        self._release_started = False
        self._release_result = False
        self._release_error: Exception | None = None

    @property
    def owner(self) -> LeaseOwner:
        with self._lock:
            return self._owner

    def claim_response(self) -> bool:
        with self._lock:
            if self._owner != "route":
                return False
            self._owner = "response"
            return True

    def claim_generator(self) -> bool:
        with self._lock:
            if self._owner not in {"route", "response"}:
                return False
            self._owner = "generator"
            return True

    def close_sync(self) -> bool:
        with self._lock:
            if self._owner not in {"route", "response"}:
                return False
            self._owner = "closed"
            if self._release_started:
                return False
            self._release_started = True
            worker = threading.Thread(
                target=self._release_worker,
                name=f"stream-lease-release-{self.run_id[:8]}",
                daemon=True,
            )
            try:
                worker.start()
            except Exception as exc:  # noqa: BLE001
                self._release_error = exc
                self._release_done.set()
                return False
        return True

    def _release_worker(self) -> None:
        context_token = set_current_user(self.owner_user_id)
        try:
            result = self.store.release_run(self.session_id, self.run_id)
            with self._lock:
                self._release_result = result
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._release_error = exc
        finally:
            reset_current_user(context_token)
            self._release_done.set()

    def _begin_async_release(self) -> None:
        with self._lock:
            self._owner = "closed"
            if self._release_started:
                return
            self._release_started = True
            worker = threading.Thread(
                target=self._release_worker,
                name=f"stream-lease-release-{self.run_id[:8]}",
                daemon=True,
            )
            try:
                worker.start()
            except Exception as exc:  # noqa: BLE001
                self._release_error = exc
                self._release_done.set()

    async def close(self) -> bool:
        self._begin_async_release()
        wait_task = asyncio.create_task(asyncio.to_thread(self._release_done.wait))
        try:
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            # The release worker is a dedicated thread and cannot be cancelled by
            # the response task group. A later response/generator close can wait
            # for the same one-shot result.
            raise
        with self._lock:
            error = self._release_error
            result = self._release_result
        if error is not None:
            logger.warning(
                "stream lease release failed session_id=%s exception_type=%s",
                self.session_id,
                type(error).__name__,
            )
            return False
        return result


class LeaseBodyIterator:
    def __init__(self, body: AsyncIterator[str], guard: StreamRunLeaseGuard) -> None:
        self._body = body
        self._guard = guard
        self._started = False

    def __aiter__(self) -> LeaseBodyIterator:
        return self

    async def __anext__(self) -> str:
        if not self._started:
            self._started = True
            if not self._guard.claim_generator():
                raise StopAsyncIteration
        try:
            return await anext(self._body)
        except StopAsyncIteration:
            await self._guard.close()
            raise
        except BaseException:
            await self._guard.close()
            raise

    async def aclose(self) -> None:
        try:
            close = getattr(self._body, "aclose", None)
            if callable(close):
                result: Any = close()
                if asyncio.iscoroutine(result):
                    await result
        finally:
            await self._guard.close()
