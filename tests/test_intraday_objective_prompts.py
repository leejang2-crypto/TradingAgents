import importlib
import inspect
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

import main
from tradingagents.agents.schemas import TraderProposal, render_trader_proposal, PortfolioDecision
from tradingagents.agents.utils.agent_utils import get_trading_objective_instruction
from tradingagents.dataflows.config import run_config
from tradingagents.monitoring.summary import saved_objective


@pytest.mark.parametrize('target,percent', [('0.01', '1%'), ('0.015', '1.5%')])
def test_goal_is_dynamic_explicit_and_never_a_profit_guarantee(target, percent):
    args = main.make_parser().parse_args([])
    args.position_target_return = target
    args.market_close = '15:30'
    args.liquidation_minutes = 15
    args.buy_cutoff_minutes = 30
    objective = main.trading_objective(args)
    assert objective['liquidation_start'] == '15:15'
    assert objective['buy_cutoff'] == '15:00'
    with run_config({'trading_objective': objective}):
        prompt = get_trading_objective_instruction()
    for expected in (f'+{percent}', 'same trading day', '15:15', '15:00', 'not account-wide daily profit',
                     'HOLD with no shares', 'HOLD with shares', 'Only explicit user approval', 'rather than inventing'):
        assert expected in prompt


def test_custom_liquidation_time_is_saved_not_assumed():
    args = main.make_parser().parse_args([])
    args.market_close, args.liquidation_minutes, args.buy_cutoff_minutes = '15:20', 20, 40
    objective = main.trading_objective(args)
    assert objective['liquidation_start'] == '15:00'
    assert objective['buy_cutoff'] == '14:40'


def test_unconfigured_or_long_term_research_does_not_inherit_intraday_contract():
    with run_config({'trading_objective': {}}):
        assert get_trading_objective_instruction() == ''
    with run_config({'trading_objective': {'style': 'long-term', 'holding_period': '12 months'}}):
        prompt = get_trading_objective_instruction()
    assert '12 months' in prompt and 'Intraday decision contract' not in prompt


@pytest.mark.parametrize('module,factory', [
    ('market_analyst', 'create_market_analyst'), ('news_analyst', 'create_news_analyst'),
    ('fundamentals_analyst', 'create_fundamentals_analyst'),
])
def test_analyst_receives_objective_in_actual_prompt(module, factory):
    module = importlib.import_module('tradingagents.agents.analysts.' + module)
    captured = []
    llm = Mock()
    llm.bind_tools.return_value = RunnableLambda(lambda prompt: captured.append(prompt.to_string()) or AIMessage(content='Done'))
    with run_config({'trading_objective': main.trading_objective(main.make_parser().parse_args([])),
                     'news_local_single_pass': False}):
        getattr(module, factory)(llm)({'trade_date': '2026-10-21', 'company_of_interest': 'TEST', 'messages': []})
    assert '+1%' in captured[0] and 'same trading day' in captured[0]


@pytest.mark.parametrize('module', [
    'analysts.sentiment_analyst', 'researchers.bull_researcher', 'researchers.bear_researcher',
    'risk_mgmt.aggressive_debator', 'risk_mgmt.conservative_debator', 'risk_mgmt.neutral_debator',
])
def test_remaining_agent_contexts_include_objective(module):
    source = inspect.getsource(importlib.import_module('tradingagents.agents.' + module))
    assert 'instrument_context = get_instrument_context_from_state(state) + "\\n" + get_trading_objective_instruction()' in source


def test_trader_exit_fields_render_and_old_proposals_remain_valid():
    proposal = TraderProposal(action='Buy', reasoning='Verified entry', entry_price=100,
        stop_loss=99.5, take_profit=101, exit_plan='Target +1%; liquidation starts 15:15 even if target is not met.')
    text = render_trader_proposal(proposal)
    assert '**Take Profit**: 101.0' in text and '**Exit Plan**:' in text and '15:15' in text
    assert TraderProposal(action='Hold', reasoning='No entry', take_profit='unknown').take_profit is None
    assert '3-6 months' not in PortfolioDecision.model_fields['time_horizon'].description


def test_saved_goal_is_not_replaced_with_current_defaults():
    assert saved_objective({})['available'] is False
    objective = main.trading_objective(main.make_parser().parse_args([]))
    objective['position_target_return'] = '.015'
    policy = saved_objective({'tradingObjective': objective})
    assert policy['targetPercent'] == '1.5' and policy['sameDay']
    assert any('15:15' in line for line in policy['lines'])
    assert any('개장 후 60분 이내' in line for line in policy['lines'])
    objective.pop('carry_exit_minutes')
    policy = saved_objective({'tradingObjective': objective})
    assert any('최대 2거래일' in line for line in policy['lines'])
    objective.pop('liquidation_start')
    policy = saved_objective({'tradingObjective': objective})
    assert any('과거 분석 파일에는 기록되지 않음' in line for line in policy['lines'])


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', 'bad', None])
def test_invalid_saved_target_is_unknown(value):
    policy = saved_objective({'tradingObjective': {'position_target_return': value}})
    assert policy['targetPercent'] is None
