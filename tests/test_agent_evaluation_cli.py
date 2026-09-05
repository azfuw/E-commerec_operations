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
    import sqlalchemy.ext.asyncio as database
    from unittest.mock import AsyncMock
    from scripts.run_agent_evaluation import _write
    session.add_all([User(id='cli-admin',username='cli-admin',password_hash='test',role=UserRole.ADMIN),Store(id='cli-store',code='cli-store',name='CLI')])
    await session.commit()
    @asynccontextmanager
    async def factory(): yield session
    monkeypatch.setattr(database,'create_async_engine',lambda *a, **kw: AsyncMock())
    monkeypatch.setattr(database,'async_sessionmaker',lambda *a, **kw: factory)
    first = await _write(EvaluationAgentType.ANALYSIS,'cli-store')
    case = await session.get(EvaluationCase,'p9-analysis-exact-v1')
    case.enabled = False
    await session.commit()
    second = await _write(EvaluationAgentType.ANALYSIS,'cli-store')
    assert first.id != second.id and first.summary['total_cases'] == 2 and second.summary['total_cases'] == 1
    assert await session.scalar(select(func.count(EvaluationRun.id))) == 2


def test_write_bootstrap_does_not_load_application_settings_or_api_keys(tmp_path):
    code = '''
import asyncio
import os
import sys
import socket
import backend.config
import sqlalchemy.ext.asyncio
def forbidden(*args, **kwargs): raise AssertionError('application settings loaded')
backend.config.get_settings = forbidden
backend.config.Settings.__init__ = forbidden
from scripts.run_agent_evaluation import _write
from backend.common import EvaluationAgentType, UserRole
from backend.models import Base, User, Store
async def verify():
    socket.socket.connect = forbidden
    engine = sqlalchemy.ext.asyncio.create_async_engine(os.environ['DATABASE_URL'])
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sqlalchemy.ext.asyncio.async_sessionmaker(engine)() as session:
        session.add_all([User(id='admin', username='admin', password_hash='test', role=UserRole.ADMIN), Store(id='test-store',name='Test',code='test')])
        await session.commit()
    await engine.dispose()
    result = await _write(EvaluationAgentType.ANALYSIS, 'test-store')
    assert result.summary['passed_cases'] == 2
    assert 'backend.database' not in sys.modules
asyncio.run(verify())
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
        env={**os.environ, 'DATABASE_URL': f'sqlite+aiosqlite:///{(tmp_path / "cli.db").as_posix()}'})
    assert result.returncode == 0, result.stderr
