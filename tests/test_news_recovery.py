import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import news_analyst as module
from tradingagents.agents.utils.local_news import NewsAnalysisError
from tradingagents.monitoring.control import RunStopped


STATE = {"trade_date": "2026-09-29", "company_of_interest": "005930.KS"}
VALID = '{"ticker":"005930.KS","items":[{"article":1,"excerpt":1,"signal":"neutral"}]}'


@pytest.fixture
def news(monkeypatch):
    tool = Mock(invoke=Mock(return_value="### Earnings report\nReported earnings today.\nLink: https://example.org/news"))
    monkeypatch.setattr(module, "get_news", tool)
    monkeypatch.setattr(module, "get_config", lambda: {"news_local_single_pass": True, "llm_provider": "hybrid",
        "hybrid_ollama_quick_llm": "qwen3:8b", "hybrid_openai_quick_llm": "configured-cloud"})
    return tool


def test_success_uses_no_retry_or_fallback(news):
    local, cloud = Mock(), Mock()
    local.invoke.return_value = AIMessage(content=VALID)
    result = module.create_news_analyst(local, cloud)(STATE)
    local.invoke.assert_called_once()
    cloud.invoke.assert_not_called()
    assert result["news_execution"]["provider"] == "ollama"
    assert not result["news_execution"]["fallbackUsed"]


@pytest.mark.parametrize("invalid", [
    '{"ticker":"005930","items":[]}',
    '{"ticker":"005930.KS","items":[{"article":99,"excerpt":1,"signal":"risk"}]}',
    '{"ticker":"005930.KS","items":[{"article":1,"excerpt":99,"signal":"risk"}]}',
    'not-json', '',
])
def test_validation_failure_is_retried_once_on_same_sources(news, invalid):
    local, cloud = Mock(), Mock()
    local.invoke.side_effect = [AIMessage(content=invalid), AIMessage(content=VALID)]
    result = module.create_news_analyst(local, cloud)(STATE)
    assert local.invoke.call_count == 2
    cloud.invoke.assert_not_called()
    news.invoke.assert_called_once()
    audit = result["news_execution"]
    assert [row["status"] for row in audit["attempts"]] == ["validation-failed", "validated"]
    retry = local.invoke.call_args.args[0]
    assert "Previous selection failed validation" in retry[-2].content
    assert all(message.content != invalid for message in retry)


def test_bounded_cloud_fallback_uses_same_catalog_and_is_audited(news):
    local, cloud = Mock(), Mock()
    local.invoke.return_value = AIMessage(content='{"ticker":"wrong"}')
    cloud.invoke.return_value = AIMessage(content=VALID)
    result = module.create_news_analyst(local, cloud)(STATE)
    assert local.invoke.call_count == 2
    cloud.invoke.assert_called_once()
    local.bind_tools.assert_not_called()
    cloud.bind_tools.assert_not_called()
    news.invoke.assert_called_once()
    assert cloud.invoke.call_args.args[0][1] == local.invoke.call_args_list[0].args[0][1]
    assert cloud.invoke.call_args.kwargs == {"max_tokens": 1600, "response_format": {"type": "json_object"}}
    audit = result["news_execution"]
    assert audit["fallbackUsed"] and audit["provider"] == "openai" and audit["model"] == "configured-cloud"
    assert len(audit["attempts"]) == 3 and "openai (validated)" in result["news_report"]
    assert result["messages"][0].content == result["news_report"]


@pytest.mark.parametrize("response", [AIMessage(content='{"ticker":"wrong"}'),
    AIMessage(content=VALID, response_metadata={"finish_reason": "length"}),
    AIMessage(content='{"ticker":"005930.KS","items":[{"article":90,"excerpt":1,"signal":"risk"}]}')])
def test_cloud_cannot_bypass_validation_or_loop(news, response):
    local = Mock(invoke=Mock(return_value=AIMessage(content='{"ticker":"wrong"}')))
    cloud = Mock(invoke=Mock(return_value=response))
    with pytest.raises(NewsAnalysisError) as error:
        module.create_news_analyst(local, cloud)(STATE)
    assert local.invoke.call_count == 2 and cloud.invoke.call_count == 1
    assert error.value.news_execution["attempts"][-1]["status"] == "validation-failed"


def test_api_failure_is_sanitized_and_does_not_retry_paid_analysis(news):
    local = Mock(invoke=Mock(return_value=AIMessage(content='{"ticker":"wrong"}')))
    cloud = Mock(invoke=Mock(side_effect=TimeoutError("sensitive-provider-error")))
    with pytest.raises(NewsAnalysisError) as error:
        module.create_news_analyst(local, cloud)(STATE)
    assert "sensitive-provider-error" not in str(error.value) + json.dumps(error.value.news_execution)
    cloud.invoke.assert_called_once()


def test_stop_before_fallback_prevents_cloud_call(news, monkeypatch):
    local = Mock(invoke=Mock(return_value=AIMessage(content='{"ticker":"wrong"}')))
    cloud = Mock()
    def checkpoint(stage):
        if stage == "news-openai-fallback":
            raise RunStopped("stop")
    monkeypatch.setattr(module, "checkpoint", checkpoint)
    with pytest.raises(RunStopped):
        module.create_news_analyst(local, cloud)(STATE)
    cloud.invoke.assert_not_called()


def test_local_transport_failure_does_not_trigger_validation_retries(news):
    local = Mock(invoke=Mock(side_effect=TimeoutError("local transport")))
    cloud = Mock()
    with pytest.raises(TimeoutError):
        module.create_news_analyst(local, cloud)(STATE)
    local.invoke.assert_called_once()
    cloud.invoke.assert_not_called()
