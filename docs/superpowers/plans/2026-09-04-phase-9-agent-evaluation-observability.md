# Phase 9 Agent Evaluation and Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Persist fixed offline Agent evaluation evidence through one controlled local CLI and safely expose evaluation and Agent-call reads to supervisors and admins.

**Architecture:** This plan consumes Plan 1 immutable evaluation tables and audit compatibility. A compact module validates fixed synthetic facts and writes a complete successful run atomically, or a separately committed safe failed header with zero results. The local one-shot CLI is the sole writer; FastAPI and Vue supply read-only observability without an evaluation service, Worker write path, or browser write control.

**Tech Stack:** Python standard library, FastAPI, SQLAlchemy async, pytest, Vue 3, TypeScript, Element Plus, Vitest, Playwright.

**Spec:** docs/superpowers/specs/2026-09-04-phase-9-management-console-design.md

## Global Constraints

- Consume Plan 1 migration 0006, EvaluationCase, EvaluationRun, EvaluationResult, audit resource fields, and safe audit-key validation unchanged. Create no migration.
- scripts/run_agent_evaluation.py is the sole evaluation writer and requires both RUN_PHASE9_EVALUATION_WRITE=1 and --write-results. HTTP, browser, Worker, and an independent service cannot write an evaluation fact.
- Use version-controlled synthetic fixtures, fake trusted loaders, deterministic validators, and MockTransport. Do not read a key or call a provider, RAG, Milvus, local model, or network.
- Success writes the immutable header, every enabled result, and one safe audit event in one transaction. A failed success transaction rolls back first; a new transaction may write only a safe failed header and zero results.
- Read/API/CLI fields are closed safe metadata. Exclude fixture/expected input, prompt, raw I/O, hidden reasoning, input_hash, workflow payload, checkpoint, lease, secret, header, connection data, or exception.
- Supervisor is limited to current enabled scope; admin sees store-scoped plus global facts without a scope row; operator gets 403. Mobile management deep links issue no management request.
- Phase 10 owns real-model evaluation, fault injection, real-service browser E2E, reports, and document verification.

---

## Locked File Structure and Responsibilities

| Path | Responsibility |
|---|---|
| data/evaluation/phase9-agent-cases.json | Bounded synthetic versioned cases. |
| backend/agent_evaluations.py | Closed metrics, immutable writes, safe run/detail query, AgentCall projection. |
| scripts/run_agent_evaluation.py | Explicit one-shot CLI, never daemon/scheduler/service. |
| backend/schemas.py and backend/routes.py | Read-only query/response DTOs and routes. |
| tests/test_agent_evaluations.py, tests/test_agent_evaluation_cli.py, tests/test_agent_evaluation_api.py, tests/test_agent_calls_api.py | Offline persistence, CLI, RBAC, safety, and replay evidence. |
| frontend/src/api.ts, types.ts, router.ts, capabilities.ts, components/AppShell.vue, pages/AgentEvaluationsPage.vue | Safe dashboard with no evaluation start action. |
| frontend/src/pages/AgentEvaluationsPage.test.ts and frontend/tests/phase9-management.spec.ts | Component and deterministic browser-fixture evidence. |

### Task 1: Validate fixed cases and persist immutable evidence

**Files:**
- Create: data/evaluation/phase9-agent-cases.json, backend/agent_evaluations.py, tests/test_agent_evaluations.py
- Modify: backend/schemas.py

**Interfaces:**
- Consumes: Plan 1 evaluation/audit models and existing deterministic Agent validators.
- Produces: validate_evaluation_metrics(agent_type, metrics), persist_evaluation_run(session, actor_id, agent_type, store_id, suite_version, runner_version, dataset_version, results), and persist_failed_evaluation_run.

- [ ] **Step 1: Write failing closed-metric and atomic-write tests**

~~~python
async def test_success_writes_header_all_enabled_results_and_one_audit(session) -> None:
    run = await persist_evaluation_run(session, **complete_offline_run())
    assert await result_count(session, run.id) == enabled_case_count()

def test_unknown_or_nonfinite_metric_fails_before_add() -> None:
    with pytest.raises(EvaluationDomainError, match='EVALUATION_INPUT_INVALID'):
        validate_evaluation_metrics('analysis', {'unknown': float('nan')})
~~~

Cover exact metrics per agent type, boolean/proportion/latency rules, closed result codes, run/case/result type match, disabled-case exclusion, a distinct run on repeat, a failed zero-result header, and no raw fixture in summary/audit.

- [ ] **Step 2: Run the focused RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py -v

Expected: FAIL because the fixture parser, validators, and persistence functions are absent.

- [ ] **Step 3: Implement the smallest closed persistence path**

~~~python
_METRIC_KEYS = {
    'analysis': frozenset({'candidate_set_valid', 'rank_order_valid', 'latency_ms'}),
    'optimization': frozenset({'output_schema_valid', 'trusted_fact_valid', 'citation_valid', 'latency_ms'}),
    'compliance': frozenset({'deterministic_valid', 'semantic_schema_valid', 'citation_valid', 'latency_ms'}),
    'knowledge_retrieval': frozenset({'recall_at_10', 'mrr', 'citation_document_version_accuracy', 'latency_ms'}),
}
~~~

Validate synthetic JSON before any ORM add. In one transaction write the run, all enabled results, and an evaluation_run_persisted safe audit. On an observed transaction error, roll back then call the failed-header function in a new transaction with status failed, EVALUATION_RUNNER_FAILED, a safe aggregate summary, and no result rows. Do not add a queue, retry, provider client, or generic runner abstraction.

- [ ] **Step 4: Run focused GREEN and evaluator regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py tests/test_knowledge_evaluation.py -v

Expected: PASS offline with no environment opt-in.

- [ ] **Step 5: Commit immutable evaluation facts**

~~~bash
git add data/evaluation/phase9-agent-cases.json backend/agent_evaluations.py backend/schemas.py tests/test_agent_evaluations.py
git diff --cached --check
git commit -m "feat: persist offline agent evaluations"
~~~

### Task 2: Add the explicit local-only evaluator command

**Files:**
- Create: scripts/run_agent_evaluation.py, tests/test_agent_evaluation_cli.py
- Modify: backend/agent_evaluations.py

**Interfaces:**
- Consumes: load_phase9_cases(), persistence functions, existing validators, and MockTransport.
- Produces: python scripts/run_agent_evaluation.py --agent-type <closed type> [--store-id <id>] [--write-results], printing a safe aggregate only.

- [ ] **Step 1: Write failing CLI-boundary tests**

~~~python
def test_no_write_mode_prints_safe_aggregate_and_creates_no_rows(monkeypatch, capsys) -> None:
    assert main(['--agent-type', 'analysis']) == 0
    assert 'fixture' not in capsys.readouterr().out

def test_write_requires_two_explicit_local_guards() -> None:
    assert main(['--agent-type', 'analysis', '--write-results']) == 2
~~~

Cover invalid agent/store pairing, no-write mode, both write guards, one fresh run per invocation, controlled failed header, and no provider/knowledge-runtime import.

- [ ] **Step 2: Run the CLI RED test**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_cli.py -v

Expected: FAIL because the command and explicit write boundary are absent.

- [ ] **Step 3: Implement the one-shot command**

~~~python
if arguments.write_results and os.getenv('RUN_PHASE9_EVALUATION_WRITE') != '1':
    raise SystemExit('RUN_PHASE9_EVALUATION_WRITE=1 is required with --write-results')
~~~

Use argparse, one asyncio.run, and existing async_session_factory. It accepts no prompt, key, URL, arbitrary fixture path, or online mode. Output only agent type, run ID/status, enabled-case/pass/fail counts, and a closed error code.

- [ ] **Step 4: Run CLI GREEN**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_cli.py tests/test_agent_evaluations.py -v

Expected: PASS offline.

- [ ] **Step 5: Commit the controlled command**

~~~bash
git add scripts/run_agent_evaluation.py backend/agent_evaluations.py tests/test_agent_evaluation_cli.py
git diff --cached --check
git commit -m "feat: add offline agent evaluation cli"
~~~

### Task 3: Expose safe evaluation and Agent-call GET models

**Files:**
- Modify: backend/agent_evaluations.py, backend/schemas.py, backend/routes.py
- Create: tests/test_agent_evaluation_api.py, tests/test_agent_calls_api.py

**Interfaces:**
- Consumes: EvaluationRun/Result and AgentCall→WorkflowRun→Store chain.
- Produces: GET /agent-evaluations/runs, GET /agent-evaluations/runs/{run_id}, GET /agent-calls; list_evaluation_runs(), get_evaluation_run_for_actor(), list_safe_agent_calls().

- [ ] **Step 1: Write failing API/RBAC/safe-shape tests**

~~~python
async def test_agent_call_view_omits_hash_and_workflow_payload(client, supervisor_headers) -> None:
    response = await client.get('/agent-calls?page=1&page_size=20', headers=supervisor_headers)
    assert response.status_code == 200
    assert 'input_hash' not in response.text
    assert 'checkpoint' not in response.text
~~~

Cover anonymous 401, operator 403, scoped supervisor rows, admin global rows, global-run 404 for supervisor, cross-store/unknown 404, page/filter 422, created_at DESC/id DESC paging, and safe detail fields.

- [ ] **Step 2: Run API RED tests**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py -v

Expected: FAIL because read DTOs, resource filters, and routes are absent.

- [ ] **Step 3: Implement read-only projections**

~~~python
@router.get('/agent-evaluations/runs/{run_id}', response_model=EvaluationRunDetailView)
async def get_agent_evaluation_run(run_id: Annotated[str, Path(min_length=1, max_length=36)], ...):
    return await get_evaluation_run_for_actor(session, actor_id=user.id, run_id=run_id)
~~~

Return only run metadata, allowlisted aggregates/results, case key/version, short codes, safe AgentCall metadata, token totals, duration, estimated cost, and timestamps. Freshly load actor; add scope predicates for supervisor and omit those joins for admin while retaining resource ownership. Do not add POST or direct ORM responses.

- [ ] **Step 4: Run API GREEN/regression**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py tests/test_optimization_api.py tests/test_approval_api.py -v

Expected: PASS offline.

- [ ] **Step 5: Commit safe observability reads**

~~~bash
git add backend/agent_evaluations.py backend/schemas.py backend/routes.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py
git diff --cached --check
git commit -m "feat: expose safe agent observability reads"
~~~

### Task 4: Build the evaluation console and final Plan 2 gate

**Files:**
- Modify: frontend/src/api.ts, frontend/src/types.ts, frontend/src/router.ts, frontend/src/capabilities.ts, frontend/src/components/AppShell.vue, tests/test_phase9_postgres.py
- Create: frontend/src/pages/AgentEvaluationsPage.vue, frontend/src/pages/AgentEvaluationsPage.test.ts, frontend/tests/phase9-management.spec.ts

**Interfaces:**
- Consumes: three read-only API paths and ApiError.
- Produces: route /app/agent-evaluations, listAgentEvaluationRuns(), getAgentEvaluationRun(), listAgentCalls(), and zero browser write surface.

- [ ] **Step 1: Write failing component/browser/transaction tests**

~~~ts
it('shows safe run/call details but no evaluation trigger', async () => {
  await flushPromises()
  expect(wrapper.find('[data-test="run-evaluation"]').exists()).toBe(false)
})
~~~

~~~python
async def test_failed_success_transaction_leaves_only_failed_zero_result_header(session) -> None:
    run = await invoke_forced_result_insert_failure(session)
    assert (run.status.value, await result_count(session, run.id)) == ('failed', 0)
~~~

Cover supervisor/admin nav, operator rejection, list states, mobile zero requests, filter preservation, and PostgreSQL proof that no partial result survives.

- [ ] **Step 2: Run focused RED tests**

Run: npm --prefix frontend test -- AgentEvaluationsPage capabilities

Expected: FAIL because page, route, and typed calls are absent.

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py::test_failed_success_transaction_leaves_only_failed_zero_result_header -v

Expected: FAIL until the two persistence transactions are separated.

- [ ] **Step 3: Implement the minimal table/drawer UI and only proven transaction repairs**

~~~ts
export function listAgentEvaluationRuns(query: EvaluationRunQuery, signal?: AbortSignal) {
  return apiRequest<EvaluationRunList>('/agent-evaluations/runs?' + queryString(query), { signal })
}
~~~

Use Element Plus filters/tables/drawers, ApiError, AbortController, text interpolation, and no control that starts evaluation. The mobile route returns the device restriction before onMounted fetches. If the PostgreSQL test exposes an atomicity defect, repair it only with rollback followed by persist_failed_evaluation_run; do not alter Plan 1 schema or add retry/network work.

- [ ] **Step 4: Run Plan 2 verification**

Run: D:\E-commerce_operations_env\python.exe -m pytest tests/test_agent_evaluations.py tests/test_agent_evaluation_cli.py tests/test_agent_evaluation_api.py tests/test_agent_calls_api.py tests/test_deepseek_runtime.py tests/test_analysis_agent.py tests/test_optimization_agent.py tests/test_compliance_agent.py -v

Expected: PASS offline.

Run: npm --prefix frontend test && npm --prefix frontend run typecheck && npm --prefix frontend run build && npm --prefix frontend run test:e2e -- phase9-management

Expected: PASS with fixtures only.

Run: $env:RUN_POSTGRES_INTEGRATION='1'; D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase9_postgres.py -v

Expected: PASS against local PostgreSQL, then clear the opt-in.

- [ ] **Step 5: Commit Plan 2 acceptance evidence**

~~~bash
git add frontend/src/api.ts frontend/src/types.ts frontend/src/router.ts frontend/src/capabilities.ts frontend/src/components/AppShell.vue frontend/src/pages/AgentEvaluationsPage.vue frontend/src/pages/AgentEvaluationsPage.test.ts frontend/tests/phase9-management.spec.ts tests/test_phase9_postgres.py
git diff --cached --check
git commit -m "feat: add agent evaluation console"
~~~
