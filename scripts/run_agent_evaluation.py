"""Run repository-owned offline fixtures. Persistence requires both local guards."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from backend.agent_evaluations import evaluate_fixed_case, load_fixed_cases, SUITE_VERSION
from backend.common import EvaluationAgentType


async def _write(agent_type, store_id):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from backend.config import Settings
    from backend.models import User, EvaluationCase
    from backend.common import UserRole, UserStatus
    from backend.agent_evaluations import seed_fixed_cases, persist_evaluation_run
    # Only database configuration is needed; never load .env.local or API settings.
    engine = create_async_engine(os.environ.get('DATABASE_URL') or Settings.model_fields['database_url'].default)
    async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
      async with async_session_factory() as session:
        actor_id = await session.scalar(select(User.id).where(User.role == UserRole.ADMIN,User.status == UserStatus.ACTIVE).order_by(User.id).limit(1))
        if actor_id is None: raise ValueError('EVALUATION_FORBIDDEN')
        await seed_fixed_cases(session)
        enabled = set(await session.scalars(select(EvaluationCase.id).where(EvaluationCase.agent_type == agent_type,EvaluationCase.enabled.is_(True))))
        cases = [case for case in load_fixed_cases() if case['agent_type'] == agent_type.value and case['id'] in enabled]
        results = [evaluate_fixed_case(case) for case in cases]
        return await persist_evaluation_run(session,actor_id=actor_id,agent_type=agent_type,store_id=store_id,
            suite_version=SUITE_VERSION,runner_version=SUITE_VERSION,dataset_version=SUITE_VERSION,results=results)
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Run fixed offline evaluation cases')
    parser.add_argument('--agent-type',required=True,choices=[item.value for item in EvaluationAgentType])
    parser.add_argument('--store-id')
    parser.add_argument('--write-results',action='store_true')
    try:
        args = parser.parse_args(argv)
        agent_type = EvaluationAgentType(args.agent_type)
        if args.write_results and os.getenv('RUN_PHASE9_EVALUATION_WRITE') != '1':
            parser.error('RUN_PHASE9_EVALUATION_WRITE=1 is required with --write-results')
        if args.store_id is not None and not 1 <= len(args.store_id) <= 36:
            parser.error('invalid store ID')
        if agent_type == EvaluationAgentType.KNOWLEDGE_RETRIEVAL and args.store_id is not None:
            parser.error('retrieval fixtures are global')
        if args.write_results and agent_type != EvaluationAgentType.KNOWLEDGE_RETRIEVAL and args.store_id is None:
            parser.error('store ID is required for persistence')
    except SystemExit as error:
        return int(error.code)
    try:
        if args.write_results:
            run = asyncio.run(_write(agent_type,args.store_id))
            summary = run.summary
            run_id, status, error_code = run.id, run.status.value, run.error_code
        else:
            results = [evaluate_fixed_case(case) for case in load_fixed_cases() if case['agent_type'] == args.agent_type and case['enabled']]
            passed = sum(result.outcome == 'passed' for result in results)
            summary = {'total_cases':len(results),'passed_cases':passed,'failed_cases':len(results)-passed}
            run_id, status, error_code = None, 'completed', None
        print(json.dumps({'agent_type':args.agent_type,'run_id':run_id,'status':status,
            **{k:summary[k] for k in ('total_cases','passed_cases','failed_cases')},'error_code':error_code}))
        return 0 if status == 'completed' and not summary['failed_cases'] else 1
    except Exception:
        print(json.dumps({'agent_type':args.agent_type,'run_id':None,'status':'failed',
            'total_cases':0,'passed_cases':0,'failed_cases':0,'error_code':'EVALUATION_RUNNER_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
