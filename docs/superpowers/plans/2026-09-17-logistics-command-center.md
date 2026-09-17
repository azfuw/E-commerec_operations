# Logistics Command Center Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the implementation and review workflow. User has authorized autonomous decisions and parallel work; no further design approval is needed.

**Goal:** Deliver a running Chinese logistics workspace integrated with the existing commerce application.

**Architecture:** Reuse authenticated FastAPI and SQLAlchemy business services, a focused Vue logistics interface, and existing store scopes. PostgreSQL remains the deployment database; a separate persistent SQLite demo provides an immediately reviewable local result.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy, Vue 3, TypeScript, Element Plus, pytest, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-17-logistics-command-center-design.md`

## Global Constraints

- Reuse existing authentication and store authorization on all logistics operations.
- Preserve existing commerce order, inventory and refund facts.
- No external carrier, paid model call or outgoing message is needed for this delivery.
- Keep real and estimated timestamps distinct; persist UTC, display Asia/Shanghai.
- Demo data and deterministic rule-agent mode must be explicitly labeled.
- All new runtime files and caches remain on D drive; do not modify existing untracked user documents.
- Models selected by task complexity: GPT-6 Astra max leads architecture/domain implementation and final decisions; GPT-5.6 Sol high implements the UI; GPT-5.6 Terra high assists focused acceptance where useful.

## Task 1: Persistent logistics domain and API

Owner: Astra max. Files: `backend/logistics*.py`, `alembic/versions/0008_*.py`, `tests/test_logistics*.py`.

- [x] Define the API contract in `docs/logistics-api-contract.md` and communicate it before dependent UI work.
- [x] Add failing tests for scoped listing, dispatch and return transitions, invalid dates and repeat patrol.
- [x] Implement Shipment/ShipmentEvent/ReturnCase/ExceptionTask/AgentRun on the existing Base.
- [x] Provide authenticated routes, safe manual ingestion, scoped dashboard, grounded query and idempotent patrol.
- [x] Add migration and relative-date idempotent seed; run `D:\E-commerce_operations_env\python.exe -m pytest tests/test_logistics_api.py -q` (use final actual filenames in report).

## Task 2: Usable logistics workspace

Owner: Sol high. Files: new logistics Vue page/components/styles/API types; root coordinates router integration.

- [x] Build the overview, shipment table/details, return queue, exceptions and Agent panel against the agreed contract.
- [x] Wire all visible controls to real API actions, with failure/empty/loading and stale-request handling.
- [x] Cover the critical UI flow with a focused test; run `npm --prefix frontend test -- --run`, `npm --prefix frontend run build`.

## Task 3: Integration and local launch

Owner: root. Files: `backend/main.py`, `backend/models.py`, `frontend/src/router.ts`, `frontend/src/components/AppShell.vue`, `frontend/vite.config.ts`, `scripts/run_logistics_demo.py`, `start-logistics.ps1`, `README.md`.

- [x] Register logistics models/router and link the independent logistics workspace.
- [x] Add a local-only launcher with a separate persistent SQLite database, idempotent demo initialization, and retained JWT signing secret.
- [x] Verify restart persistence and unauthenticated API rejection; run the local service and build frontend assets.

## Task 4: Review and real-browser acceptance

Owner: root with independent scoped review. Files: `frontend/tests/logistics-real.spec.ts`, browser config, `docs/logistics-validation.md`, `docs/assets/logistics/`.

- [x] Review scope isolation, time semantics, state transitions, idempotency and usable end-to-end actions.
- [x] Run relevant backend regression, frontend tests, typecheck and production build.
- [x] Exercise real local API from the browser, including dispatch, return progress, exception handling and Agent evidence.
- [x] Inspect desktop/mobile screenshots, fix defects, and deliver the running URL and concise guide.

## Progress

- Repository inspected at `711ca05`; existing untracked docs preserved. Branch: `codex/logistics-command-center`.
- Existing `.venv` points to a removed interpreter. Verified reusable Python 3.11 runtime at `D:\E-commerce_operations_env\python.exe`; dependencies and Playwright Chromium already installed.

- Final offline regression: 1106 passed, 45 skipped; frontend 87 passed; real-browser workflows 3 passed. PostgreSQL migration round-trip and drift check passed. See docs/logistics-validation.md.
