import json
import subprocess
import sys
import os
import pytest
from scripts.run_agent_evaluation import main
from backend.agent_evaluations import load_fixed_cases, evaluate_fixed_case


@pytest.mark.parametrize('agent',['analysis','optimization','compliance','knowledge_retrieval'])
def test_readonly_cli_computes_safe_results_without_settings_or_network(agent,monkeypatch,capsys):
    import socket
    import backend.config
    def forbidden(*args,**kwargs): raise AssertionError('external dependency used')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(backend.config.Settings,'__init__',forbidden)
    monkeypatch.setattr(backend.config,'get_settings',forbidden)
    assert main(['--agent-type',agent]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['agent_type'] == agent and result['passed_cases'] == result['total_cases'] == 2
    assert result['run_id'] is None
    assert set(result) == {'agent_type','run_id','status','total_cases','passed_cases','failed_cases','error_code'}


@pytest.mark.parametrize('args', [[],['--agent-type','online'],['--agent-type','analysis','--write-results']])
def test_cli_invalid_arguments_or_missing_guard(args,monkeypatch,capsys):
    monkeypatch.delenv('RUN_PHASE9_EVALUATION_WRITE',raising=False)
    assert main(args) == 2
    assert 'Traceback' not in capsys.readouterr().err


def test_fixture_expectation_change_causes_failed_result():
    case = load_fixed_cases()[0]
    case['expected']['candidate_set_valid'] = False
    assert evaluate_fixed_case(case).outcome == 'failed'


async def test_write_creates_new_runs_and_preserves_disabled_case(session,monkeypatch):
    from contextlib import asynccontextmanager
    from sqlalchemy import select,func
    from backend.models import User,Store,EvaluationRun,EvaluationCase
    from backend.common import UserRole,EvaluationAgentType
    import backend.database as database
    from scripts.run_agent_evaluation import _write
    session.add_all([User(id='cli-admin',username='cli-admin',password_hash='test',role=UserRole.ADMIN),Store(id='cli-store',code='cli-store',name='CLI')])
    await session.commit()
    @asynccontextmanager
    async def factory(): yield session
    monkeypatch.setattr(database,'async_session_factory',factory)
    first = await _write(EvaluationAgentType.ANALYSIS,'cli-store')
    case = await session.get(EvaluationCase,'p9-analysis-exact-v1')
    case.enabled = False
    await session.commit()
    second = await _write(EvaluationAgentType.ANALYSIS,'cli-store')
    assert first.id != second.id and first.summary['total_cases'] == 2 and second.summary['total_cases'] == 1
    assert await session.scalar(select(func.count(EvaluationRun.id))) == 2
