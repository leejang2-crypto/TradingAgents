"""Hard timeout wrapper for synchronous LangChain chat model calls."""

from __future__ import annotations

import queue
import threading
from typing import Any

from langchain_core.runnables import Runnable


class HardTimeoutLLM(Runnable):
    """Proxy an LLM and cap each ``invoke`` call with a daemon thread timeout.

    Provider SDK timeouts are not always enough to prevent a long blocking
    socket read from stalling an automated paper-trading run. This wrapper is
    intentionally tiny and only wraps methods the agents use directly:
    ``invoke``, ``bind_tools``, and ``with_structured_output``.
    """

    def __init__(self, llm: Any, timeout: float):
        self._llm = llm
        self._timeout = float(timeout)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def invoke(self, *args, **kwargs):
        result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def target() -> None:
            try:
                result_queue.put((True, self._llm.invoke(*args, **kwargs)))
            except BaseException as exc:
                result_queue.put((False, exc))

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        try:
            ok, value = result_queue.get(timeout=self._timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"LLM invoke exceeded hard timeout of {self._timeout:g}s.") from exc
        if ok:
            return value
        raise value

    def bind_tools(self, *args, **kwargs):
        return HardTimeoutLLM(self._llm.bind_tools(*args, **kwargs), self._timeout)

    def with_structured_output(self, *args, **kwargs):
        return HardTimeoutLLM(self._llm.with_structured_output(*args, **kwargs), self._timeout)
