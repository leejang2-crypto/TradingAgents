import asyncio
import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate

import main
from tradingagents.agents.utils.experience import ExperienceStore, ROLE_FIELDS
from tradingagents.llm_clients.experience_wrapper import ExperienceLLM
from tradingagents.llm_clients.routing import AGENT_LABELS
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.brokers.toss_executor import execute_plan, plan_from_decision
from tradingagents.learning import main as learning_main
from copy import deepcopy


@pytest.fixture
def store(tmp_path):
    return ExperienceStore(tmp_path / "experiences.sqlite3")


def start(store, day="2026-09-01", mode="dry-run", ticker="005930.KS", objective=None):
    return store.start(mode=mode, ticker=ticker, trade_date=day,
                       objective=objective or {"style": "intraday"},
                       routing={r: {"provider": "ollama", "model": "test-local"} for r in ROLE_FIELDS})


def reports():
    state = {}
    for role, fields in ROLE_FIELDS.items():
        if len(fields) == 1:
            state[fields[0]] = role + " past reasoning"
        else:
            state.setdefault(fields[0], {})[fields[1]] = role + " past reasoning"
    state["local_safe_validation"] = {"ok": False, "reasons": ["unsupported claim"]}
    return state


def test_all_roles_persist_and_reload_with_outcomes_unknown(store):
    first = start(store)
    store.save_analysis(first, reports())
    store.event(first, "PLANNED", {"plan": {"action": "BUY", "order_amount": "1000000"}})
    store.event(first, "RESULT", {"execution": {"executed": True, "result": {"privateAccount": "secret"}}})
    current = start(store, "2026-09-02")
    contexts = ExperienceStore(store.path).contexts(current)
    assert set(contexts) == set(AGENT_LABELS)
    for role, context in contexts.items():
        example = json.loads(context)[0]
        assert example["agent"] == role
        assert example["model"]["provider"] == "ollama"
        assert example["plan"]["action"] == "BUY"
        assert example["performance"].startswith("UNKNOWN")
        assert "secret" not in context
    assert len(store.get(first)["events"]) == 3


def test_modes_dates_objectives_and_roles_do_not_leak(store):
    for day, mode, objective in [
        ("2026-09-01", "live", None),
        ("2026-09-02", "dry-run", None),
        ("2026-09-03", "dry-run", None),
        ("2026-09-01", "dry-run", {"style": "long-term"}),
    ]:
        old = start(store, day, mode, objective=objective)
        store.save_analysis(old, reports())
    assert store.contexts(start(store, "2026-09-02")) == {}
    old = start(store)
    store.save_analysis(old, {"news_report": "Only news"})
    assert set(store.contexts(start(store, "2026-09-02"))) == {"news_analyst"}


def test_future_feedback_and_late_analysis_not_used(store):
    old = start(store)
    current = start(store, "2026-09-02")
    store.save_analysis(old, reports())
    store.add_feedback(old, "news_analyst", "late review", "source")
    assert store.contexts(current) == {}
    next_run = start(store, "2026-09-02")
    assert "late review" in store.contexts(next_run)["news_analyst"]


def test_prompt_budget_and_same_ticker_priority(store):
    for day in range(1, 9):
        old = start(store, f"2026-09-{day:02d}", ticker="000660.KS" if day > 4 else "005930.KS")
        store.save_analysis(old, {"news_report": "x" * 10000})
    context = store.contexts(start(store, "2026-09-10"))["news_analyst"]
    examples = json.loads(context)
    assert len(examples) <= 3 and len(context) <= 6500
    assert examples[0]["ticker"] == "005930.KS"


def test_feedback_requires_known_run_and_role(store):
    old = start(store)
    with pytest.raises(ValueError):
        store.add_feedback(old, "not-agent", "lesson", "source")
    with pytest.raises(ValueError):
        store.get("missing")
    assert store.recent("live") == []
    assert store.recent("dry-run")[0]["id"] == old


@pytest.mark.parametrize("binding", [None, "tools", "structured"])
def test_wrapper_preserves_config_schema_and_tool_sequence(binding):
    llm = Mock()
    llm.bind_tools.return_value = llm
    llm.with_structured_output.return_value = llm
    context = {"text": "past-example"}
    wrapped = ExperienceLLM(llm, lambda: context["text"])
    if binding == "tools":
        wrapped = wrapped.bind_tools(["tool"])
    elif binding == "structured":
        wrapped = wrapped.with_structured_output(dict)
    messages = [SystemMessage(content="system"), HumanMessage(content="current"),
                AIMessage(content="", tool_calls=[{"name": "tool", "args": {}, "id": "id"}]),
                ToolMessage(content="result", tool_call_id="id")]
    wrapped.invoke(messages, config={"tags": ["test"]}, temperature=0)
    sent = llm.invoke.call_args.args[0]
    assert sent[0] == messages[0]
    assert "past-example" in sent[1].content
    assert sent[2:] == messages[1:]
    assert len(messages) == 4
    assert llm.invoke.call_args.kwargs == {"config": {"tags": ["test"]}, "temperature": 0}
    context["text"] = ""
    wrapped.invoke("original")
    assert llm.invoke.call_args.args[0] == "original"


def test_wrapper_accepts_prompt_values():
    llm = Mock(return_value=None)
    (ChatPromptTemplate.from_messages([("system", "system"), ("human", "{question}")])
     | ExperienceLLM(llm, lambda: "past")).invoke({"question": "today"})
    sent = llm.invoke.call_args.args[0]
    assert sent[-1].content == "today"
    assert "past" in sent[1].content


@pytest.mark.parametrize("provider", ["openai", "ollama", "hybrid"])
def test_graph_loads_memory_for_every_role_and_preserves_routing(store, monkeypatch, tmp_path, provider):
    old = start(store)
    store.save_analysis(old, reports())
    current = start(store, "2026-09-02")
    config = deepcopy(DEFAULT_CONFIG)
    config.update({
        "llm_provider": provider, "quick_think_llm": "quick", "deep_think_llm": "deep",
        "hybrid_openai_quick_llm": "cloud-quick", "hybrid_openai_deep_llm": "cloud-deep",
        "hybrid_ollama_quick_llm": "local-quick", "hybrid_ollama_deep_llm": "local-deep",
        "hybrid_agent_providers": {r: "ollama" if r == "news_analyst" else "openai" for r in ROLE_FIELDS},
        "experience_enabled": True, "experience_db_path": str(store.path),
        "experience_run_id": current, "memory_log_path": None, "checkpoint_enabled": False,
        "results_dir": str(tmp_path / "logs"), "data_cache_dir": str(tmp_path / "cache"),
        "llm_timeout": None, "local_safe_validation": False,
    })
    def client(provider, model, **kwargs):
        llm = Mock()
        llm.provider_name = provider
        llm.model_name = model
        llm.bind_tools.return_value = llm
        llm.with_structured_output.return_value = llm
        return Mock(get_llm=Mock(return_value=llm))

    monkeypatch.setattr("tradingagents.graph.trading_graph.create_llm_client", client)
    graph = TradingAgentsGraph(selected_analysts=("market", "news"), config=config)
    monkeypatch.setattr(graph, "_resolve_pending_entries", Mock(side_effect=AssertionError("legacy memory must not run")))
    monkeypatch.setattr(graph, "resolve_instrument_context", lambda *a: "instrument")
    monkeypatch.setattr(graph, "_log_state", lambda *a: None)
    monkeypatch.setattr(graph, "process_signal", lambda *a: "Hold")
    graph.graph = Mock(invoke=Mock(return_value=reports()))
    graph.propagate("005930.KS", "2026-09-02")
    assert set(graph.experience_contexts) == set(ROLE_FIELDS)
    for role in ROLE_FIELDS:
        wrapper = graph.graph_setup._llm(role)
        assert isinstance(wrapper, ExperienceLLM)
        wrapper.invoke("today's task")
        assert role in wrapper._llm.invoke.call_args.args[0][0].content
        expected = "ollama" if provider == "hybrid" and role == "news_analyst" else "openai" if provider == "hybrid" else provider
        assert wrapper._llm.provider_name == expected
    assert store.get(current)["reports"]


def test_analysis_save_is_idempotent(store):
    run_id = start(store)
    store.save_analysis(run_id, reports())
    store.save_analysis(run_id, reports())
    assert len(store.get(run_id)["events"]) == 1


def test_cli_list_review_and_show(store, capsys):
    run_id = start(store)
    learning_main(["--database", str(store.path), "list", "--mode", "dry-run"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == run_id
    learning_main(["--database", str(store.path), "feedback", run_id,
                   "--agent", "news_analyst", "--lesson", "Check original publication date",
                   "--source", "review.md"])
    assert json.loads(capsys.readouterr().out)["saved"]
    learning_main(["--database", str(store.path), "show", run_id])
    assert json.loads(capsys.readouterr().out)["feedback"][0]["source"] == "review.md"


def test_paper_journal_contains_experience_id(tmp_path):
    result = asyncio.run(execute_plan(
        plan_from_decision("**Rating**: Buy", "005930.KS"),
        paper_trade_log=tmp_path / "trades.jsonl", experience_run_id="test-run",
    ))
    assert result["paperTrade"]["record"]["experienceRunId"] == "test-run"
    assert json.loads((tmp_path / "trades.jsonl").read_text())["experienceRunId"] == "test-run"


@pytest.mark.parametrize("execute", [False, True])
@pytest.mark.parametrize("action", ["Buy", "Hold", "error"])
def test_runner_links_reports_and_trade_result(tmp_path, monkeypatch, execute, action):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_korea_news_snapshot", lambda args: None)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    args = main.make_parser().parse_args(["--scenario", "korea-execute" if execute else "korea-dry-run", "--yes", "--date", "2026-09-01"])
    graph = Mock()
    graph.propagate.return_value = ({"news_report": "news evidence", "final_trade_decision": f"**Rating**: {action}"}, action)
    graph.save_reports.return_value = "report.md"
    factory = Mock(return_value=graph)
    monkeypatch.setattr(main, "TradingAgentsGraph", factory)

    async def executor(plan, **kwargs):
        assert kwargs["execute"] is execute
        saved = ExperienceStore(tmp_path / "learning" / "experiences.sqlite3").get(kwargs["experience_run_id"])
        assert saved["events"][-1]["kind"] == "ORDER_ATTEMPT"
        if action == "error":
            raise RuntimeError("broker response unavailable")
        return {"executed": execute and action == "Buy", "dryRun": not execute, "plan": plan.__dict__}

    monkeypatch.setattr(main, "execute_plan", executor)
    if action == "error":
        with pytest.raises(RuntimeError, match="broker response"):
            asyncio.run(main.run_korea_toss(args, execute=execute))
    else:
        result = asyncio.run(main.run_korea_toss(args, execute=execute))
        assert result["learning"]["enabled"]
    mode = "live" if execute else "dry-run"
    store = ExperienceStore(tmp_path / "learning" / "experiences.sqlite3")
    saved = store.get(store.recent(mode)[0]["id"])
    assert saved["reports"]["news_analyst"] == "news evidence"
    assert saved["events"][-1]["kind"] == ("ERROR" if action == "error" else "RESULT")
    assert saved["events"][-1]["payload"]["realizedPnl"] is None
    config = factory.call_args.kwargs["config"]
    assert config["memory_log_path"] is None
    assert mode in config["results_dir"] and saved["id"] in config["results_dir"]
