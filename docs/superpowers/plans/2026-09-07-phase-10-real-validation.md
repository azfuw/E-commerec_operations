# Phase 10 Real Validation and Fault Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove typed failure behavior across DeepSeek, Milvus, Worker and platform boundaries, then execute bounded real DeepSeek and real local knowledge-service validation.

**Architecture:** Deterministic fault tests remain the repeatable correctness gate. Real DeepSeek and local-model/Milvus checks are separate explicit opt-ins with synthetic inputs, strict call ceilings and sanitized evidence; a real integration failure is reported, never retried until it happens to pass.

**Tech Stack:** Python 3.11, pytest, httpx MockTransport/ASGITransport, DeepSeek API, PostgreSQL 16, Milvus 2.5, local BGE models.

**Spec:** `docs/superpowers/specs/2026-09-07-phase-10-production-validation-design.md`

## Global Constraints

- Execute only after Plan 1 is independently approved, in the same isolated Phase 10 worktree, with `superpowers:executing-plans`; do not dispatch subagents.
- Add no dependency or persistent fault-control API. Faults enter only through injected transports, test support apps, process termination or opt-in test configuration.
- Use only synthetic product facts and repository-owned knowledge fixtures. Do not send customer, merchant, order or personal data to DeepSeek.
- A real DeepSeek test allows at most one primary and one schema-repair request per Agent. Do not loop, rerun automatically, or loosen assertions after a model failure.
- Never print or persist a Key, Authorization header, Prompt, trusted input, raw model response, provider request ID, traceback containing request data, or connection string.
- Real DeepSeek uses only `RUN_PHASE10_DEEPSEEK=1`; real local RAG uses only `RUN_KNOWLEDGE_INTEGRATION=1`; ordinary tests keep both unset.
- Use the configured model exactly; if the configured model differs from the approved `deepseek-v4-flash`, stop with `DEEPSEEK_SMOKE_MODEL_MISMATCH` rather than silently changing it.
- Store temporary evidence under `D:\E-commerce_operations_env\phase10-evidence`; create the exact directory if absent, but do not clean any broader path.
- Do not read, modify or stage `docs/business-and-technical-guide.md`, `docs/interview-q-and-a.md`, or `docs/architecture/`.
- Do not contact a real commerce platform. Platform validation stays on loopback.

---

### Task 1: Deterministic cross-boundary fault campaign

**Files:**
- Create: `tests/test_phase10_fault_campaign.py`
- Modify: `tests/test_deepseek_runtime.py`
- Modify: `tests/test_knowledge_search.py`
- Modify: `tests/test_platform_delivery_worker.py`
- Modify: `tests/test_platform_webhooks.py`
- Modify: `tests/test_manual_review_worker.py`

**Interfaces:**
- Produces no new production API.
- Verifies the closed error/state mapping defined by the Phase 10 spec across existing injection seams.

- [ ] **Step 1: Write the failing campaign matrix test**

Use a table whose expected values are concrete:

```python
FAULT_EXPECTATIONS = {
    "deepseek_timeout": ("DEEPSEEK_TIMEOUT", 1),
    "deepseek_rate_limit": ("DEEPSEEK_RATE_LIMIT", 1),
    "deepseek_invalid_json": ("DEEPSEEK_SCHEMA_INVALID", 2),
    "milvus_timeout": ("KNOWLEDGE_DEPENDENCY_TIMEOUT", 0),
    "milvus_zero_hit": ("zero_hit", 0),
    "platform_accepted_disconnect": ("succeeded", 1),
    "platform_three_5xx": ("failed", 3),
    "worker_stale_owner": ("processing", 0),
    "webhook_bad_signature": ("PLATFORM_WEBHOOK_SIGNATURE_INVALID", 0),
    "webhook_duplicate": ("accepted", 1),
}
```

Each parameter calls an existing public runtime/worker/service seam and returns `(safe_outcome, side_effect_count)`. The test asserts exact equality and serializes all persisted rows to prove these forbidden strings are absent: `authorization`, `api_key`, `prompt`, `raw_response`, `traceback`, `connection_string`.

- [ ] **Step 2: Run the campaign and verify RED only where coverage is missing**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_fault_campaign.py -q --tb=short
```

Expected: failures identify missing injected paths or incorrect safe outcomes; already-correct boundaries may pass immediately.

- [ ] **Step 3: Add the smallest missing injection seams and assertions**

Prefer test-side `httpx.MockTransport`, `ASGITransport`, fake clocks and existing dependency injection arguments. Production code may change only when a fault reveals incorrect behavior. Do not add a generic chaos framework, decorator registry, retry library or global fault flag.

For invalid DeepSeek JSON, assert exactly one schema-repair call. For an accepted-then-disconnected platform write, assert the second delivery attempt uses the same 64-character idempotency hash and completes with the first external operation ID. For stale Worker ownership, assert zero terminal updates from the old owner.

- [ ] **Step 4: Run focused boundary suites GREEN**

```powershell
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_fault_campaign.py tests/test_deepseek_runtime.py tests/test_knowledge_search.py tests/test_platform_delivery_worker.py tests/test_platform_webhooks.py tests/test_manual_review_worker.py -q --tb=short
```

Expected: all deterministic faults pass with exact error/state semantics and no external network.

- [ ] **Step 5: Commit Task 1**

```powershell
git add tests/test_phase10_fault_campaign.py tests/test_deepseek_runtime.py tests/test_knowledge_search.py tests/test_platform_delivery_worker.py tests/test_platform_webhooks.py tests/test_manual_review_worker.py
# If production code changed, add only each exact reviewed path, for example:
# git add backend/deepseek_runtime.py backend/platform_delivery_worker.py
git diff --cached --check
git commit -m "test: verify phase ten fault boundaries"
```

Before committing, inspect `git diff --cached --name-only`; every staged production file must be one actually changed for this task. Reset only unintended staged entries with `git restore --staged -- <exact-path>`; never discard their working-tree content.

---

### Task 2: Bounded real DeepSeek contract gate

**Files:**
- Create: `tests/test_phase10_deepseek.py`
- Modify: `pyproject.toml`
- Reuse without copying production logic: `backend/analysis_agent.py`
- Reuse without copying production logic: `backend/optimization_agent.py`
- Reuse without copying production logic: `backend/compliance_agent.py`
- Reuse fixture shapes from: `tests/test_deepseek_smoke.py`
- Reuse fixture shapes from: `tests/test_optimization_deepseek_smoke.py`

**Interfaces:**
- Produces marker `phase10_deepseek` and environment guard `RUN_PHASE10_DEEPSEEK=1`.
- Produces three live tests: analysis, optimization and compliance.
- Prints only one `PHASE10_DEEPSEEK_SUMMARY=<canonical-json>` line per test on success.

- [ ] **Step 1: Write offline guard tests before the live tests**

```python
def _enabled() -> bool:
    return os.getenv("RUN_PHASE10_DEEPSEEK") == "1"


def _settings_or_fail() -> Settings:
    settings = get_settings()
    if settings.deepseek_model != "deepseek-v4-flash":
        pytest.fail("DEEPSEEK_SMOKE_MODEL_MISMATCH")
    if settings.deepseek_api_key is None or not settings.deepseek_api_key.get_secret_value().strip():
        pytest.fail("DEEPSEEK_SMOKE_KEY_REQUIRED")
    return settings
```

Mark all live tests with `@pytest.mark.phase10_deepseek` and `skipif(not _enabled(), reason='explicit Phase 10 DeepSeek opt-in required')`. Add an ordinary test that clears the flag, collects the file and verifies all three live tests skip without constructing an httpx client.

- [ ] **Step 2: Add exact synthetic fixtures and call ceilings**

Copy only the minimal synthetic values needed to construct `AnalysisFacts` and `TrustedOptimizationInput`; do not import helper functions from another test module. Use fixed IDs beginning with `phase10-smoke-` and Chinese synthetic listing text.

The analysis orchestration permits primary then one schema repair only. Optimization and compliance each permit primary then one schema repair only when the production validator rejects the first schema. A shared guard enforces at most two HTTP attempts:

```python
def two_attempt_guard():
    calls = 0
    async def before_http_attempt(*_args) -> bool:
        nonlocal calls
        calls += 1
        return calls <= 2
    return before_http_attempt, lambda: calls
```

Assert the final typed response passes its production validator. Do not assert exact generated prose.

- [ ] **Step 3: Emit one allowlisted success summary**

```python
def safe_record(record) -> dict[str, object]:
    return {
        "agent_type": agent_type,
        "model": record.model,
        "prompt_version": record.prompt_version,
        "call_type": record.call_type.value,
        "attempt": record.attempt,
        "duration_ms": record.duration_ms,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "total_tokens": record.total_tokens,
        "estimated_cost": None if record.estimated_cost is None else str(record.estimated_cost),
        "error_code": record.error_code,
    }
```

Serialize with `ensure_ascii=False`, `sort_keys=True`, compact separators and `allow_nan=False`. Assert the serialized line does not contain any known synthetic input text, Key value, `authorization`, `prompt`, `payload` or `raw_response`.

- [ ] **Step 4: Run the new file offline first**

```powershell
Remove-Item Env:RUN_PHASE10_DEEPSEEK -ErrorAction SilentlyContinue
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_deepseek.py -q --tb=short
```

Expected: offline guard tests pass and exactly three live tests skip. No network request occurs.

- [ ] **Step 5: Review the live command before enabling it**

Confirm the working tree contains no Key, the configured base URL is exactly the approved DeepSeek endpoint, and the test contains no automatic rerun plugin. Do not print environment variables. The user has approved bounded DeepSeek calls for Phase 10, so no additional scope expansion is needed; stop only if the Key/model/base URL is missing or different.

- [ ] **Step 6: Run the live gate once**

```powershell
$env:RUN_PHASE10_DEEPSEEK='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_deepseek.py -m phase10_deepseek -q -s --tb=short
} finally {
  Remove-Item Env:RUN_PHASE10_DEEPSEEK -ErrorAction SilentlyContinue
}
```

Expected: three live tests pass, each Agent produces one allowlisted summary, and no Agent exceeds two requests. If any fails, preserve the safe failure category and stop; do not rerun automatically.

- [ ] **Step 7: Save sanitized evidence outside Git**

Create `D:\E-commerce_operations_env\phase10-evidence` with `New-Item -ItemType Directory -Force` against that exact path. Rerun is forbidden; save the output from Step 6 as it is produced by teeing the original single run to `deepseek-summary.txt`, not by executing a second live run. Inspect the file for forbidden key names and synthetic input text before later quoting aggregate values in the report.

- [ ] **Step 8: Commit Task 2 code only**

```powershell
git add pyproject.toml tests/test_phase10_deepseek.py
git diff --cached --check
git commit -m "test: add bounded live deepseek validation"
```

Do not add `D:\E-commerce_operations_env\phase10-evidence` or `.env.local`.

---

### Task 3: Real PostgreSQL, Milvus and local-model gate

**Files:**
- Modify only if a reproducible integration defect is found: `tests/test_knowledge_integration.py`
- Modify only for an actual defect: `backend/knowledge_index.py`
- Modify only for an actual defect: `backend/knowledge_worker.py`
- Modify only for an actual defect: `backend/knowledge_search.py`
- Evidence outside Git: `D:\E-commerce_operations_env\phase10-evidence\local-services-summary.txt`

**Interfaces:**
- Verifies existing local-model and Milvus interfaces without adding a second retrieval implementation.

- [ ] **Step 1: Confirm local services and model paths without revealing configuration values**

Run `docker compose ps` and require PostgreSQL, etcd, MinIO and Milvus healthy. Use `Test-Path` only for the configured BGE model directories; do not print directory contents or environment values. If a service is stopped, `docker compose up -d` is in scope. Do not recreate volumes.

- [ ] **Step 2: Run the existing real knowledge integration once**

```powershell
$env:RUN_KNOWLEDGE_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_knowledge_integration.py tests/test_optimization_rag.py -q --tb=short
} finally {
  Remove-Item Env:RUN_KNOWLEDGE_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: real PostgreSQL/Milvus/local-model tests pass, including active version, disabled version, zero-hit and citation-version accuracy. Tests use unique IDs and delete only their own rows/collection entities.

- [ ] **Step 3: Run the real PostgreSQL Phase 10 suite**

```powershell
$env:RUN_POSTGRES_INTEGRATION='1'
try {
  D:\E-commerce_operations_env\python.exe -m pytest tests/test_phase10_postgres.py tests/test_phase9_postgres.py tests/test_analysis_postgres.py tests/test_optimization_postgres.py tests/test_manual_review_postgres.py -q --tb=short
} finally {
  Remove-Item Env:RUN_POSTGRES_INTEGRATION -ErrorAction SilentlyContinue
}
```

Expected: all real persistence, migration, transaction and concurrency tests pass.

- [ ] **Step 4: Diagnose any failure before changing code**

Distinguish unavailable service/model files from a product defect. A missing Docker service or model directory is an environment blocker, not permission to weaken the integration test. For a real defect, add the smallest deterministic regression assertion to the existing test, fix the shared production boundary, and rerun only the failed integration command once.

- [ ] **Step 5: Save the original sanitized command summaries**

Store only command, pass/fail counts, elapsed time, image/service versions and safe error categories in `local-services-summary.txt`. Exclude database URLs, local usernames, model absolute paths, document text and vectors.

- [ ] **Step 6: Commit only if Task 3 found and fixed a defect**

```powershell
git diff --check
git status --short
```

If no code changed, create no commit. If a defect was fixed, stage exact touched files, inspect `git diff --cached`, and commit `fix: correct phase ten integration boundary`. Stop for independent review before Plan 3.
