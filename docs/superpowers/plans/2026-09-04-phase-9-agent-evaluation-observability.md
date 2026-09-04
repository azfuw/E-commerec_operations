# Phase 9 Agent Evaluation and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist fixed offline Agent evaluation evidence through one controlled local CLI and safely expose evaluation plus Agent-call reads.

**Architecture:** This plan consumes Plan 1 immutable evaluation tables and audit compatibility. `backend/agent_evaluations.py` validates fixture-derived results, atomically writes a complete run, or independently writes a failed zero-result header. `scripts/run_agent_evaluation.py` is the only writer. FastAPI and Vue provide read-only views; no evaluation service, Worker write path, or browser write model exists.

**Tech Stack:** Python standard library, FastAPI, SQLAlchemy async, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md`

## Global Constraints

- The current 电商项目开发窗口 executes this plan sequentially with `superpowers:executing-plans`; do not dispatch subagents.
- Consume Plan 1 `0006`, `EvaluationCase`, `EvaluationRun`, `EvaluationResult`, and audit fields unchanged. Do not create a migration.
- The only writer is `scripts/run_agent_evaluation.py` with both `RUN_PHASE9_EVALUATION_WRITE=1` and `--write-results`. Browser, HTTP API, existing Worker, and independent service cannot create a run/result.
- Use only repository synthetic fixtures, fake trusted loaders, deterministic validators, and `MockTransport`. Do not read a key, call a provider, start RAG/Milvus, load a model, or use a network connection.
- Before every ordinary offline gate, execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue; Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue; Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue; Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue`. PostgreSQL proof alone sets `RUN_POSTGRES_INTEGRATION=1`; immediately after every PostgreSQL command execute `Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue`.
- Run/results are immutable. Success commits header, every enabled result, and one safe audit together. Success-transaction failure rolls back before an independently committed safe failed header with zero results.
- API/CLI responses exclude fixture/expected input, prompt, provider data, hidden reasoning, `input_hash`, workflow payload, checkpoint, lease, secret, header, connection detail, and exception.
- `supervisor` sees current enabled-store scope; `admin` sees global/store facts without scope; `operator` receives 403. Phase 10 retains real-model evaluation, fault injection, and real-service browser E2E.
- Do not read, modify, or stage `docs/business-and-technical-guide.md` or `docs/interview-q-and-a.md`.
- Do not modify or clean C-drive personal files outside the current Codex worktree; put cache and temporary files on D drive first.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| `data/evaluation/phase9-agent-cases.json` | Bounded, versioned synthetic cases without source document/provider data. |
| `backend/agent_evaluations.py` | Closed metric validation, persistence, safe run/detail reads, safe AgentCall projection. |
| `scripts/run_agent_evaluation.py` | One-shot explicit CLI, no daemon or scheduler. |
| `backend/schemas.py`, `backend/routes.py` | Evaluation/call query DTOs and three read-only routes. |
| `tests/test_agent_evaluations.py`, `tests/test_agent_evaluation_cli.py`, `tests/test_agent_evaluation_api.py`, `tests/test_agent_calls_api.py` | Offline validator, persistence, CLI, route, RBAC, and safe-view tests. |
| `tests/test_phase9_postgres.py` | Real PostgreSQL atomic-write and constraint proof. |
| `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`, `frontend/src/pages/AgentEvaluationsPage.vue` | Typed desktop/tablet observability console. |

### Task 1: Validate fixed results and persist immutable evaluation facts

**Files:**
- Create: `data/evaluation/phase9-agent-cases.json`, `backend/agent_evaluations.py`, `tests/test_agent_evaluations.py`
- Modify: `backend/schemas.py`, `tests/test_phase9_postgres.py`

**Interfaces:**
- Consumes: Plan 1 models, `add_audit_event`, existing deterministic validators, and `backend.database.async_session_factory`.
- Produces: `EvaluationResultInput(case_id: str, outcome: Literal['passed', 'failed'], metrics: dict[str, object], result_code: str, latency_ms: float)`, `validate_evaluation_metrics(agent_type: EvaluationAgentType, metrics: dict[str, object]) -> dict[str, bool | float]`, `persist_evaluation_run(session: AsyncSession, *, actor_id: str, agent_type: EvaluationAgentType, store_id: str | None, suite_version: str, runner_version: str, dataset_version: str, results: list[EvaluationResultInput]) -> EvaluationRun`, and `persist_failed_evaluation_run(session: AsyncSession, *, actor_id: str, agent_type: EvaluationAgentType, store_id: str | None, error_code: str) -> EvaluationRun`.

- [ ] **Step 1: Write failing closed-metric, completeness, and PostgreSQL atomicity tests**

~~~python
def test_analysis_metrics_accept_only_the_closed_shape() -> None:
    assert validate_evaluation_metrics(
        EvaluationAgentType.ANALYSIS,
        {'candidate_set_valid': True, 'rank_order_valid': True, 'latency_ms': 4.0},
    )['latency_ms'] == 4.0
    with pytest.raises(EvaluationDomainError, match='EVALUATION_INPUT_INVALID'):
        validate_evaluation_metrics(EvaluationAgentType.ANALYSIS, {'latency_ms': float('nan')})
~~~

In `tests/test_phase9_postgres.py`, inject the first flush failure without a helper so rollback is followed by a working flush for the failed zero-result header:

~~~python
async def test_persistence_flush_failure_writes_failed_zero_result_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
  async with async_session_factory() as session:
    original_flush = session.flush
    failed_once = False

    async def fail_first_flush(*args: object, **kwargs: object) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise IntegrityError('INSERT', {}, Exception('forced'))
        await original_flush(*args, **kwargs)

    arguments = {
        'actor_id': 'admin-1', 'agent_type': EvaluationAgentType.ANALYSIS,
        'store_id': 'store-1', 'suite_version': 'fixture-v1',
        'runner_version': 'phase9-v1', 'dataset_version': 'fixture-v1',
        'results': [EvaluationResultInput(
            case_id='analysis-case-1', outcome='passed',
            metrics={'candidate_set_valid': True, 'rank_order_valid': True, 'latency_ms': 1.0},
            result_code='EVALUATION_PASSED', latency_ms=1.0,
        )],
    }
    monkeypatch.setattr(session, 'flush', fail_first_flush)
    failed = await persist_evaluation_run(session, **arguments)
    assert failed.status is EvaluationRunStatus.FAILED
    assert await session.scalar(
        select(func.count(EvaluationResult.id)).where(EvaluationResult.evaluation_run_id == failed.id)
    ) == 0
~~~

Create `EvaluationResultInput` at the top of `backend/agent_evaluations.py` as `@dataclass(frozen=True)` with `case_id: str`, `outcome: Literal['passed', 'failed']`, `metrics: dict[str, object]`, `result_code: str`, and `latency_ms: float`. Also test disabled-case exclusion, exact result unique identity, run/case/result agent equality, new immutable run on repeat, audit safe fields, local PostgreSQL constraints, and downgrade fact refusal.

- [ ] **Step 2: Run RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py -v`

Expected: FAIL because the evaluation types, validator, and persistence functions are absent.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: FAIL because no Phase 9 persistence path can preserve failed zero-result atomicity; clear the opt-in.

- [ ] **Step 3: Implement closed persistence**

~~~python
_METRIC_KEYS = {
    EvaluationAgentType.ANALYSIS: frozenset({'candidate_set_valid', 'rank_order_valid', 'latency_ms'}),
    EvaluationAgentType.OPTIMIZATION: frozenset({'output_schema_valid', 'trusted_fact_valid', 'citation_valid', 'latency_ms'}),
    EvaluationAgentType.COMPLIANCE: frozenset({'deterministic_valid', 'semantic_schema_valid', 'citation_valid', 'latency_ms'}),
    EvaluationAgentType.KNOWLEDGE_RETRIEVAL: frozenset({'recall_at_10', 'mrr', 'citation_document_version_accuracy', 'latency_ms'}),
}
~~~

Validate every case/result and metric before `session.add()`. In the success transaction insert one run, exactly one result per enabled case, and `evaluation_run_persisted` audit. Catch the controlled `IntegrityError`/`SQLAlchemyError`, rollback, then call the failed-header function in a fresh transaction. Its header uses status `failed`, closed code `EVALUATION_RUNNER_FAILED`, safe aggregate summary, and zero results. Do not add retry, queue, provider client, or generic runner.

- [ ] **Step 4: Run GREEN and PostgreSQL persistence proof**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py tests/test_knowledge_evaluation.py -v`

Expected: PASS offline.

Run: `$env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v`

Expected: PASS with successful complete runs, failed zero-result rows, constraints, audit, and downgrade guard; clear the opt-in.

- [ ] **Step 5: Commit evaluation persistence**

~~~bash
git add data/evaluation/phase9-agent-cases.json backend/agent_evaluations.py backend/schemas.py tests/test_agent_evaluations.py tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: persist offline agent evaluations"
~~~

### Task 2: Add the explicit local evaluator CLI

**Files:**
- Create: `scripts/run_agent_evaluation.py`, `tests/test_agent_evaluation_cli.py`
- Modify: `backend/agent_evaluations.py`

**Interfaces:**
- Consumes: `persist_evaluation_run`, `persist_failed_evaluation_run`, repository fixture JSON, fake loaders, deterministic validators, and `MockTransport`.
- Produces: `python scripts/run_agent_evaluation.py --agent-type <agent-type> [--store-id <store-id>] [--write-results]`, with only a safe aggregate JSON result.

- [ ] **Step 1: Write failing CLI boundary tests**

~~~python
def test_write_mode_requires_both_local_guards(monkeypatch) -> None:
    monkeypatch.delenv('RUN_PHASE9_EVALUATION_WRITE', raising=False)
    assert main(['--agent-type', 'analysis', '--write-results']) == 2

def test_read_only_mode_creates_no_run(monkeypatch, capsys) -> None:
    assert main(['--agent-type', 'analysis']) == 0
    assert 'fixture' not in capsys.readouterr().out
~~~

Use the existing test database session to assert no `EvaluationRun` exists in no-write mode. Cover invalid closed agent type, missing store for analysis/optimization/compliance, rejected store for global retrieval, one new run per authorized invocation, and no provider/RAG import.

- [ ] **Step 2: Run CLI RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_cli.py -v`

Expected: FAIL because the command and its two explicit guards do not exist.

- [ ] **Step 3: Implement the one-shot command**

~~~python
if arguments.write_results and os.getenv('RUN_PHASE9_EVALUATION_WRITE') != '1':
    raise SystemExit('RUN_PHASE9_EVALUATION_WRITE=1 is required with --write-results')
~~~

Use `argparse`, one `asyncio.run`, and existing `async_session_factory`. The parser accepts no prompt, key, URL, arbitrary fixture path, or online mode. Output exactly agent type, run ID/status, enabled/pass/fail count, and closed error code.

- [ ] **Step 4: Run CLI GREEN**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_cli.py tests/test_agent_evaluations.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit the CLI**

~~~bash
git add scripts/run_agent_evaluation.py backend/agent_evaluations.py tests/test_agent_evaluation_cli.py
git diff --cached --check
git commit -m "feat: add offline agent evaluation cli"
~~~

### Task 3: Add safe evaluation and Agent-call read routes

**Files:**
- Modify: `backend/agent_evaluations.py`, `backend/schemas.py`, `backend/routes.py`
- Create: `tests/test_agent_evaluation_api.py`, `tests/test_agent_calls_api.py`

**Interfaces:**
- Consumes: `EvaluationRun`, `EvaluationResult`, `AgentCall -> WorkflowRun -> Store`, and fresh actor/scope state.
- Produces: `GET /agent-evaluations/runs`, `GET /agent-evaluations/runs/{run_id}`, `GET /agent-calls`; `list_evaluation_runs`, `get_evaluation_run_for_actor`, `list_safe_agent_calls`.

- [ ] **Step 1: Write failing RBAC/safe-view tests**

~~~python
async def test_agent_call_view_excludes_hash_and_workflow_body(client, supervisor_headers) -> None:
    response = await client.get('/agent-calls?page=1&page_size=20', headers=supervisor_headers)
    assert response.status_code == 200
    assert 'input_hash' not in response.text
    assert 'workflow_input' not in response.text
~~~

Cover anonymous 401, operator 403, scoped supervisor data, admin global data, global-run 404 for supervisor, cross-store/unknown 404, all filter/page 422, descending stable paging, and returned metric/detail closed fields.

- [ ] **Step 2: Run route RED**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py -v`

Expected: FAIL because DTOs, safe projections, and routes are absent.

- [ ] **Step 3: Implement safe projections**

~~~python
@router.get('/agent-evaluations/runs/{run_id}', response_model=EvaluationRunDetailView)
async def get_agent_evaluation_run(
    run_id: Annotated[str, Path(min_length=1, max_length=36)],
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EvaluationRunDetailView:
    return await get_evaluation_run_for_actor(session, actor_id=user.id, run_id=run_id)
~~~

Project only allowed run/result fields, safe call metadata, token totals, duration, estimated cost, error code, and timestamp. Add supervisor scope predicates; omit those joins for admin while preserving resource chain. Do not add a write endpoint or return ORM objects.

- [ ] **Step 4: Run route GREEN**

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py tests/test_optimization_api.py tests/test_approval_api.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit read models**

~~~bash
git add backend/agent_evaluations.py backend/schemas.py backend/routes.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py
git diff --cached --check
git commit -m "feat: expose safe agent observability reads"
~~~

### Task 4: Build Agent Observability with `canViewAgentObservability`

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/types.ts`, `frontend/src/router.ts`, `frontend/src/capabilities.ts`, `frontend/src/components/AppShell.vue`
- Create: `frontend/src/pages/AgentEvaluationsPage.vue`, `frontend/src/pages/AgentEvaluationsPage.test.ts`
- Modify: `frontend/tests/phase9-management.spec.ts`

**Interfaces:**
- Consumes: the three read-only routes and `ApiError`.
- Produces: `canViewAgentObservability(role: UserRole, isMobile: boolean): boolean`, route `/app/agent-evaluations`, `listAgentEvaluationRuns`, `getAgentEvaluationRun`, and `listAgentCalls`.

- [ ] **Step 1: Write failing page/capability tests**

~~~ts
it('does not fetch observability data on mobile', async () => {
  session.user = { id: 'supervisor-1', username: 'supervisor', role: 'supervisor' }
  setMobile(true)
  await router.push({ name: 'agent-evaluations' })
  expect(fetch).not.toHaveBeenCalled()
})
~~~

Cover supervisor/admin nav, operator forbidden state, loading/empty/filtered-empty/401/403/404/422/error states, run drawer, call summary, filter paging, and absence of an evaluation-start button.

- [ ] **Step 2: Run frontend RED**

Run: `npm --prefix frontend test -- AgentEvaluationsPage capabilities`

Expected: FAIL because capability, route, API types, and page are absent.

- [ ] **Step 3: Implement the read-only console**

~~~ts
export function canViewAgentObservability(role: UserRole, isMobile: boolean): boolean {
  return !isMobile && (role === 'supervisor' || role === 'admin')
}
~~~

Use this exact function in navigation, route guard, and mobile test. Add `api.ts` requests through `apiRequest`, `AbortController` cancellation, Element Plus filters/table/drawer, text interpolation, visible error/status labels, and no write control.

- [ ] **Step 4: Run frontend GREEN and Plan 2 checks**

Run: `npm --prefix frontend test -- AgentEvaluationsPage capabilities && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management`

Expected: PASS using fixture browser responses.

Run: `D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py tests/test_agent_evaluation_cli.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py tests/test_deepseek_runtime.py tests/test_analysis_agent.py tests/test_optimization_agent.py tests/test_compliance_agent.py -v`

Expected: PASS offline.

- [ ] **Step 5: Commit observability UI**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/AgentEvaluationsPage.vue frontend/src/pages/AgentEvaluationsPage.test.ts frontend/tests/phase9-management.spec.ts
git diff --cached --check
git commit -m "feat: add agent evaluation console"
~~~
