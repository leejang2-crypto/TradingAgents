from __future__ import annotations

import time

import pytest

from tradingagents.llm_clients.timeout_wrapper import HardTimeoutLLM


class SlowLLM:
    def invoke(self, *args, **kwargs):
        time.sleep(1)
        return "done"

    def bind_tools(self, *args, **kwargs):
        return self

    def with_structured_output(self, *args, **kwargs):
        return self


def test_hard_timeout_raises_before_slow_invoke_returns():
    llm = HardTimeoutLLM(SlowLLM(), timeout=0.01)
    with pytest.raises(TimeoutError):
        llm.invoke("hello")


def test_hard_timeout_wraps_bound_llms():
    llm = HardTimeoutLLM(SlowLLM(), timeout=0.01)
    with pytest.raises(TimeoutError):
        llm.bind_tools([]).invoke("hello")
    with pytest.raises(TimeoutError):
        llm.with_structured_output(dict).invoke("hello")
