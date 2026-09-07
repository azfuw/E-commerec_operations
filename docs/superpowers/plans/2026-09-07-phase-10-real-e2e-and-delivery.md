# Phase 10 Real-Service E2E and Final Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the complete product against isolated real local services, capture truthful browser evidence, and publish the final operator-facing README and validation report.

**Architecture:** A deterministic loopback DeepSeek-compatible server makes the browser flow repeatable while all product API, database, Milvus, local BGE and Worker boundaries remain real. A one-shot Python launcher creates a dedicated PostgreSQL database and unique Milvus collection, starts the existing processes plus the platform simulator, runs Playwright, and cleans only resources bearing its run ID.

**Tech Stack:** Python 3.11 standard library, FastAPI, Uvicorn, SQLAlchemy/asyncpg, PostgreSQL 16, Milvus 2.5, local BGE models, Vue 3, TypeScript, Playwright, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-07-phase-10-production-validation-design.md`

## Global Constraints

- Execute only after Plans 1 and 2 are independently approved, in the same isolated Phase 10 worktree, with `superpowers:executing-plans`; do not dispatch subagents.
- Add no dependency, deployment system, message queue, cache, second product service, provider factory or browser-visible fault-control interface.
- Product traffic uses real loopback HTTP and the dedicated PostgreSQL database. Playwright must not use `page.route()`, `route.fulfill()`, `installApiFixture()` or any mocked business response.
- The deterministic model server and platform simulator are test facilities under `tests/support`; neither is imported by `backend` or shipped as a product service.
- Generate a cryptographically random run ID. The launcher may create and later delete only `ecommerce_phase10_<run-id>`, `phase10_<run-id>` Milvus data, its child processes and its exact D-drive evidence directory.
- Never drop or reset the default `ecommerce` database, shared Docker volumes, shared Milvus collections, user files or another run's data.
- Keep `RUN_PHASE10_DEEPSEEK` unset. The real DeepSeek gate was executed once in Plan 2 and must not be repeated while producing E2E or documentation.
- Use synthetic demo users, products and knowledge only. Do not expose Key, Token, Authorization header, Prompt, raw model response, connection string, local username or personal absolute path in screenshots, reports, process output or Git.
- Price, SKU, inventory and order remain read-only. The platform target is always labeled `contract_simulator`; never describe it as a real merchant-platform integration.
- Do not read, modify or stage `docs/business-and-technical-guide.md`, `docs/interview-q-and-a.md`, or the user-owned `docs/architecture/` directory.
- Put temporary files under `D:\E-commerce_operations_env\phase10-evidence`; do not modify or clean personal C-drive files.

---

### Task 1: Deterministic DeepSeek-compatible test server

**Files:**
- Create: `tests/support/deterministic_deepseek.py`
- Create: `tests/test_deterministic_deepseek.py`

**Interfaces:**
- Produces: `create_deterministic_deepseek() -> FastAPI`.
- Produces: loopback `POST /chat/completions` with the response envelope consumed by `DeepSeekJsonRuntime`.
- Produces: `GET /health/live` and process-local counters containing only request type and count.

- [ ] **Step 1: Write failing contract tests**

```python
@pytest.mark.parametrize("kind", ["analysis", "optimization", "compliance"])
async def test_server_returns_a_production_validated_response(kind, request_body):
    request_json, validator = request_body[kind]
    app = create_deterministic_deepseek()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://model"
    ) as client:
        response = await client.post("/chat/completions", json=request_json)
    assert response.status_code == 200
    content = response.json()["choices"][0]["message"]["content"]
    assert validator(content)
    assert app.state.calls == [{"kind": kind, "attempt": 1}]


async def test_server_rejects_unknown_or_oversized_shapes_without_echoing_body(client):
    response = await client.post("/chat/completions", json={"model": "unknown"})
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"}}
```

Build each `request_body` value from the same minimal synthetic objects used by the three production client tests, and validate content with `parse_agent_response`, `validate_optimization_response` and `validate_compliance_response`. Assert the server's state and error bodies contain none of the inbound messages.

- [ ] **Step 2: Run RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_deterministic_deepseek.py -q --tb=short
```

Expected: collection fails because the support server does not exist.

- [ ] **Step 3: Implement the minimum compatible endpoint**

Parse only this existing runtime envelope:

```python
class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["deepseek-v4-flash"]
    response_format: dict[Literal["type"], Literal["json_object"]]
    messages: list[Message] = Field(min_length=2, max_length=2)
```

Require one system and one user message, decode the user message once as JSON, and select a response by its mutually exclusive top-level shape:

```python
if set(payload) == {"facts"}:
    content = analysis_content(payload["facts"])
elif "response_template" in payload and "trusted_facts" in payload:
    content = optimization_content(payload)
elif "candidate_output" in payload and "canonical_citations" in payload:
    content = compliance_content(payload)
else:
    raise HTTPException(422, {"code": "DETERMINISTIC_MODEL_REQUEST_INVALID"})
```

For analysis, preserve every trusted product ID exactly once and assign ranks `1..N`. For optimization, return the supplied closed `response_template`; when `required_changes` is non-empty, apply each title/selling-point/description/keyword instruction through the existing response fields and declare matching `changes` evidence from the supplied allowlists. For compliance, normally return a low-risk pass with empty violations/changes and only supplied canonical citations.

Reserve the synthetic title prefix `阶段十连续失败验证` for one browser-only fault case. When compliance sees that prefix, return `passed=false`, `risk_level=medium`, one `UNPROVABLE_PROMISE` title violation and one matching semantic required change using the first supplied canonical citation. When optimization receives that required change, change the title suffix but preserve the reserved prefix and add a matching title `changes` entry, causing the second compliance iteration to fail deterministically. No HTTP fault switch or product setting is added. Wrap every JSON string in the existing DeepSeek `choices/message/content` envelope with fixed non-secret usage counts.

Do not log or retain messages, decoded payloads or output text. Store only `{kind, attempt}` counters for test assertions.

- [ ] **Step 4: Run GREEN and production-client compatibility tests**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_deterministic_deepseek.py tests/test_analysis_agent.py tests/test_optimization_agent.py tests/test_compliance_agent.py -q --tb=short
```

Expected: all pass without external network.

- [ ] **Step 5: Commit Task 1**

```powershell
git add tests/support/deterministic_deepseek.py tests/test_deterministic_deepseek.py
git diff --cached --check
git commit -m "test: add deterministic agent service"
```

---

### Task 2: Isolated one-shot real-service launcher

**Files:**
- Create: `scripts/run_phase10_e2e.py`
- Create: `tests/test_phase10_e2e_runner.py`
- Create: `tests/fixtures/phase10-knowledge.md`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `python scripts/run_phase10_e2e.py` as the only full-stack E2E entry point.
- Produces: `build_run_names(run_id: str) -> tuple[str, str]` for the PostgreSQL database and Milvus collection.
- Produces: exit code `0` only after Playwright passes and scoped cleanup succeeds.

- [ ] **Step 1: Write failing name, environment and cleanup tests**

```python
def test_run_names_are_unique_and_strictly_scoped(monkeypatch):
    database, collection = build_run_names("a1b2c3d4")
    assert database == "ecommerce_phase10_a1b2c3d4"
    assert collection == "phase10_a1b2c3d4"


def test_cleanup_refuses_unscoped_targets():
    with pytest.raises(ValueError, match="PHASE10_CLEANUP_SCOPE_INVALID"):
        validate_cleanup_target("ecommerce", "knowledge_chunks")


def test_child_environment_keeps_live_deepseek_disabled(monkeypatch):
    monkeypatch.setenv("RUN_PHASE10_DEEPSEEK", "1")
    environment = build_child_environment("a1b2c3d4")
    assert "RUN_PHASE10_DEEPSEEK" not in environment
    assert environment["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:8765"
    assert environment["MILVUS_COLLECTION"] == "phase10_a1b2c3d4"
```

Use fake `Popen`, database and Milvus adapters to prove child processes terminate in reverse order and cleanup still runs when migration, health polling or Playwright fails. The fake adapters must also prove the default database and collection are never passed to a delete operation.

- [ ] **Step 2: Run RED**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_e2e_runner.py -q --tb=short
```

Expected: collection fails because the launcher functions do not exist.

- [ ] **Step 3: Implement bounded resource creation**

Use `secrets.token_hex(4)` and allow only lowercase hex run IDs. Derive both database URLs with SQLAlchemy's installed `make_url()` rather than string replacement. Connect to PostgreSQL's `postgres` maintenance database with existing `asyncpg`, create the quoted validated database, then run:

```text
python -m alembic upgrade head
python scripts/seed_demo.py
```

against only the derived database environment. Set a unique Milvus collection and an exact evidence child directory `D:\E-commerce_operations_env\phase10-evidence\<run-id>`.

- [ ] **Step 4: Start and health-check the existing stack**

Start these child processes with `subprocess.Popen`, hidden windows on Windows, inherited sanitized environment and individual log files in the run evidence directory:

```text
python -m uvicorn tests.support.deterministic_deepseek:create_deterministic_deepseek --factory --host 127.0.0.1 --port 8765
python -m uvicorn tests.support.platform_simulator:create_platform_simulator --factory --host 127.0.0.1 --port 8766
python -m uvicorn backend.main:create_app --factory --host 127.0.0.1 --port 4174
python scripts/run_analysis_worker.py
python scripts/run_knowledge_worker.py
python scripts/run_optimization_worker.py
python scripts/run_manual_review_worker.py
python scripts/run_platform_delivery_worker.py
```

Build the frontend before starting the API so `backend.main` serves `frontend/dist`. Configure the platform simulator for one accepted-then-disconnect event through its process-only environment and configure `RUN_PHASE10_PLATFORM_DELIVERY=1` only for the platform worker. Poll `/health/live` for the three HTTP processes with a 60-second total deadline; abort on early child exit. Do not print child environments or URLs containing credentials.

- [ ] **Step 5: Run Playwright once and always clean exactly this run**

Invoke:

```text
npm --prefix frontend run test:e2e -- --config playwright.real.config.ts
```

with `PHASE10_E2E_BASE_URL=http://127.0.0.1:4174/app/` and the run evidence path. In `finally`, terminate only recorded child PIDs, wait with a finite timeout, delete only the exact unique Milvus collection, terminate remaining database connections to the unique database, and drop only that database. Preserve sanitized logs and screenshots; no cleanup command may target their parent directory.

- [ ] **Step 6: Add the fixed synthetic knowledge fixture**

`tests/fixtures/phase10-knowledge.md` contains one short Chinese commerce-copy rule with no merchant data. It names allowed claim boundaries, forbids absolute promises, and includes a stable heading that the browser can verify after indexing. The browser uploads this exact file; the launcher never scans another directory for knowledge.

- [ ] **Step 7: Run launcher unit tests GREEN**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_e2e_runner.py -q --tb=short
```

Expected: scoped creation, reverse-order process termination and failure-path cleanup pass without starting real services.

- [ ] **Step 8: Commit Task 2**

```powershell
git add .gitignore scripts/run_phase10_e2e.py tests/test_phase10_e2e_runner.py tests/fixtures/phase10-knowledge.md
git diff --cached --check
git commit -m "test: orchestrate isolated phase ten stack"
```

---

### Task 3: Unmocked Playwright success and recovery evidence

**Files:**
- Create: `frontend/playwright.real.config.ts`
- Create: `frontend/tests/phase10-real-services.spec.ts`
- Create after successful run: `docs/assets/phase10/e2e-platform-recovered.png`
- Modify: `tests/test_phase10_e2e_runner.py`

**Interfaces:**
- Consumes: the isolated launcher, demo users, actual product API, all Workers, deterministic model server and platform simulator.
- Produces: a desktop success/recovery browser scenario, supervisor rejection scenario and two-failure compliance scenario with no route interception.

- [ ] **Step 1: Add a Playwright configuration with no web-server mock**

```typescript
export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: process.env.PHASE10_E2E_BASE_URL,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium-real-services', use: { browserName: 'chromium' } }],
})
```

Fail configuration loading when `PHASE10_E2E_BASE_URL` or `PHASE10_EVIDENCE_DIR` is absent. Do not declare a Playwright `webServer`; the Python launcher owns every process.

- [ ] **Step 2: Write the real success/recovery scenario**

Use the existing Chinese labels and demo password `DemoPass!2026`, but do not import `apiFixture.ts` and do not call `page.route`. The test performs these real UI actions in order:

1. Admin uploads `tests/fixtures/phase10-knowledge.md` and waits for the version to become active.
2. Operator starts a flagship-store analysis for `2026-07-26` through `2026-08-24`, waits for candidates, selects one, waits for optimization and compliance, enters one manual title revision, and submits approval.
3. Supervisor opens the real pending proposal and approves it.
4. The accepted-then-disconnect simulator forces the platform Worker to replay the same idempotency key; the page is reloaded until it shows `平台投递成功`, attempt count `2`, one external operation ID and the local publish record.
5. The test asserts the displayed version increased by one while price, SKU and stock values equal their pre-approval values.
6. The test opens observability/audit reads and checks safe Agent call summaries plus enqueue/completion evidence without raw content.

At the final stable screen call:

```typescript
await page.screenshot({
  path: path.join(process.env.PHASE10_EVIDENCE_DIR!, 'e2e-platform-recovered.png'),
  fullPage: true,
})
```

- [ ] **Step 3: Write an independent real rejection scenario**

Create a second proposal through the same real analysis/selection flow, log in as supervisor, reject it with a fixed synthetic comment, and assert there is no `PublishRecord`, no platform-delivery summary and no platform completion audit for that proposal. Confirm the first success evidence remains visible and unchanged.

- [ ] **Step 4: Write the two-failure compliance scenario**

Create a third proposal, enter a manual title beginning with `阶段十连续失败验证`, submit the manual revision and wait for the real manual-review Worker. Assert the UI shows two failed compliance iterations and returns to a human-action state, with no approval action, publish record or platform delivery. Assert each iteration has a distinct immutable proposal revision and safe compliance review, while the original manual revision remains unchanged.

- [ ] **Step 5: Add protocol and safety assertions**

Record response statuses for `/auth`, `/analysis-runs`, `/workflow-runs`, `/proposals`, `/approvals`, `/knowledge`, `/agent-calls` and `/audit-events`. Assert no business request is fulfilled from a service worker or intercepted route, every request host is `127.0.0.1:4174`, and rendered text does not contain `Authorization`, `Bearer`, `api_key`, `raw_response`, `prompt`, `traceback` or a PostgreSQL URL.

- [ ] **Step 6: Run the real stack once**

```powershell
D:\E-commerce_operations_env\python.exe scripts/run_phase10_e2e.py
```

Expected: Playwright reports all three real-service scenarios passed, the platform success required exactly two attempts and one simulator mutation, and scoped cleanup reports only the generated database and collection names. If it fails, preserve the sanitized logs, fix the root cause, and rerun only after independent review approves another full-stack attempt.

- [ ] **Step 7: Copy only the reviewed screenshot into Git**

After checking the evidence image contains no token, URL credential, personal path or raw model content, copy the exact screenshot to `docs/assets/phase10/e2e-platform-recovered.png`. Do not add traces, child logs, Playwright HTML reports or the evidence directory.

- [ ] **Step 8: Commit Task 3**

```powershell
git add frontend/playwright.real.config.ts frontend/tests/phase10-real-services.spec.ts tests/test_phase10_e2e_runner.py docs/assets/phase10/e2e-platform-recovered.png
git diff --cached --check
git commit -m "test: prove the real service workflow"
```

---

### Task 4: README, validation report and final gate

**Files:**
- Create: `README.md`
- Create: `docs/phase10-validation-report.md`
- Modify only if a final gate exposes a defect: files already owned by Plans 1-3.

**Interfaces:**
- Produces: one truthful project entry point and one evidence-backed acceptance record.
- Declares no deployment or real commerce-platform certification.

- [ ] **Step 1: Write the root README from verified behavior**

Use exactly these sections: 项目定位、已实现能力、系统边界、架构、角色与权限、本地快速启动、演示流程、测试与验收、安全配置、已知限制、后续可选工作. Link the approved specs rather than duplicating them. Include commands for Docker Compose, migration, deterministic seed, API/frontend start, ordinary tests, opt-in local-service tests and the one-shot real-service E2E.

State explicitly that DeepSeek validation used synthetic inputs and explicit opt-in, the platform is a local contract simulator, production merchant OAuth was not performed, and price/SKU/inventory/order are never written. Do not include a working secret, `.env.local`, machine-specific model path or private evidence path.

- [ ] **Step 2: Create the validation report from original evidence**

Use exactly these sections: 验收范围、提交链、环境版本、普通回归、PostgreSQL、Milvus 与本地 BGE、真实 DeepSeek 受控验证、平台故障矩阵、真实服务浏览器 E2E、敏感信息检查、已知非阻塞问题、最终结论. Record actual commands, timestamps, commit IDs, pass/skip counts and safe error categories from Plans 1-3; do not invent a result or rerun DeepSeek.

Embed `docs/assets/phase10/e2e-platform-recovered.png` with a relative link. The conclusion must separately say what was genuinely executed and that no real merchant-platform credential, OAuth approval, production API or real listing mutation was available.

- [ ] **Step 3: Run all ordinary gates with external switches cleared**

```powershell
Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue
Remove-Item Env:RUN_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_TASK10_DEEPSEEK_SMOKE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE9_EVALUATION_WRITE -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE10_DEEPSEEK -ErrorAction SilentlyContinue
Remove-Item Env:RUN_PHASE10_PLATFORM_DELIVERY -ErrorAction SilentlyContinue
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
D:\E-commerce_operations_env\python.exe -m pytest -q --tb=short
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:e2e
```

Expected: all ordinary backend and frontend tests pass; only explicitly guarded real integrations skip. Fixture Playwright and real-service Playwright counts remain separately labeled.

- [ ] **Step 4: Run final PostgreSQL and repository gates**

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py tests/test_phase9_postgres.py tests/test_analysis_postgres.py tests/test_optimization_postgres.py tests/test_manual_review_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
$env:JWT_SECRET_KEY='phase10-final-check-only-at-least-32-characters'
try {
  D:\E-commerce_operations_env\python.exe -m compileall -q backend scripts alembic tests/support
  D:\E-commerce_operations_env\python.exe -m alembic current
  D:\E-commerce_operations_env\python.exe -m alembic check
} finally {
  Remove-Item Env:JWT_SECRET_KEY -ErrorAction SilentlyContinue
}
docker compose config --quiet
git diff --check
```

Expected: PostgreSQL proofs pass, Alembic is `0007 (head)` with no pending operations, Compose validates and the diff has no whitespace errors.

- [ ] **Step 5: Scan tracked text and deliverables for sensitive material**

First use `rg -n -i "authorization:|bearer [a-z0-9._-]+|api[_-]?key[=:]|postgresql[^ ]*://[^ ]+@|raw_response|provider_request_id" README.md docs/phase10-validation-report.md backend frontend scripts tests`. Review expected identifier-only matches; no match may contain a value or raw content. Then load the configured DeepSeek secret into a non-echoed PowerShell variable, use `Select-String -SimpleMatch -Quiet` against tracked text files and `D:\E-commerce_operations_env\phase10-evidence`, and raise only `PHASE10_SECRET_SCAN_FAILED` if found. Clear the variable in `finally`; never print it.

- [ ] **Step 6: Commit the final artifacts**

```powershell
git add README.md docs/phase10-validation-report.md
git diff --cached --check
git diff --cached --name-only
git commit -m "docs: deliver phase ten validation evidence"
git status --short
```

Expected: only the two reviewed documents are in this commit and the isolated worktree is clean.

- [ ] **Step 7: Stop for final independent review**

Report every Phase 10 commit, exact gate counts, the single real DeepSeek run result, real-service E2E result, PostgreSQL/Milvus versions, screenshot path and any non-blocking warning. Do not merge, push, deploy or begin a real platform adapter until the review window separately approves it.
