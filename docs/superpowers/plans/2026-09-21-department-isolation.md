# Department isolation implementation plan

> **For agentic workers:** Use test-driven-development and verification-before-completion. The user has authorized autonomous implementation and review.

**Goal:** Operations and logistics users cannot read or mutate the other department's business, even with the same store scope; administrators retain cross-department access.

**Architecture:** Add an explicit `UserDepartment` (`operations`, `logistics`) to the existing user model. Keep roles and store scopes as independent restrictions. Resolve permission from the database, gate API routes and internal execution boundaries, and mirror these rules in frontend navigation.

**Tech stack:** Existing FastAPI, SQLAlchemy, Alembic, Vue 3, Vitest, pytest and Playwright. No new dependencies.

**Spec:** This document defines the permission change requested on 2026-09-21.

## Global constraints

- Existing accounts migrate to operations; only known identities in the local logistics demo get an explicit logistics assignment during its one-time upgrade.
- Administrators may update department in system management with an audit record. Existing tokens must respect current database permissions.
- Authentication, current identity and authorized store listing remain shared. Operations knowledge, audit, evaluation and business endpoints require operations access. Logistics history stays available in logistics shipment events and Agent runs.
- Preserve existing demo data, passwords, account status, store scopes and user-owned files. Do not reset databases or add dependencies.
- Admin access still observes existing action-specific rules. Department access never expands a user's role or store permissions.

## Review focus

- Same-store supervisor accounts calling the other department's write APIs must receive 403 without side effects.
- Department changes must invalidate queued work's authority before processing or business writeback.
- Logistics assignees must be active logistics staff or administrators with applicable store access.
- Existing SQLite demo upgrades must preserve records and not undo later administrator edits.
- Direct links, refresh, mobile navigation and login return targets must never expose the opposite workspace.

## Task 1: Backend authorization and schema

Owner: backend implementer. Files: `backend/`, Alembic migration `0009`, related backend tests (excluding demo launcher tests).

- [x] Add a failing real-auth API test: an operations supervisor with the same store receives 403 from `/logistics/dashboard`; a logistics supervisor receives 403 from `/workbench/tasks` and approval writes.
- [x] Introduce `UserDepartment.OPERATIONS/LOGISTICS`; default existing users to operations in model and migration. Return required `department` in `/auth/me` and admin user views; accept it in admin patches.
- [x] Add minimal shared department guards, operations/logistics route boundaries, internal service and worker checks, and assignee filtering.
- [x] Test active-token department changes, no unauthorized mutations, internal jobs, admin bypass and retained store restrictions.
- [x] Run targeted pytest, then report exact results and remaining gaps to the coordinator.

## Task 2: Frontend access and account administration

Owner: frontend implementer. Files: `frontend/src/`, `frontend/tests/apiFixture.ts` and existing mocked browser tests as needed. Coordinator owns `frontend/tests/logistics-real.spec.ts`.

- [x] Add failing capability/login/router tests for both departments and admin access.
- [x] Make `department: 'operations' | 'logistics'` part of current and admin user contracts. Use it to select login/root destinations, hide unavailable departments and block direct links before component mounting.
- [x] Mark knowledge/audit/evaluations as operations; system management remains admin-only. Preserve the shared shell and remember admin workspace navigation.
- [x] Add department display/editing in user management and safe return links from denied/desktop pages. Keep role-based actions intact.
- [x] Run Vitest and production build; report exact results.

## Task 3: Persistent demo upgrade and acceptance

Owner: coordinator. Files: `scripts/run_logistics_demo.py`, `scripts/run_logistics_worker.py`, `tests/test_logistics_demo_launcher.py`, real-service browser tests and validation documentation.

- [x] Add and run a failing upgrade test using a previous-schema SQLite database with existing records.
- [x] Add an idempotent column upgrade and explicit demo identities for operations/logistics/admin; do not overwrite subsequent admin department edits on restart.
- [x] Test browser login landing, hidden links, direct cross-department URL denial, API reads/writes, admin switching and changed permissions with an existing token.
- [x] Run full backend/frontend regressions and relevant migration checks. Safely restart the local demo with the verified build.

## Task 4: Independent review and final verification

- [x] Request an independent security review of the complete diff with no inherited implementation reasoning.
- [x] Fix material findings and rerun affected checks.
- [x] Record observed test results and permission boundaries in `docs/department-isolation-validation.md`.
- [x] Deliver the running result and concise verification summary.

Completed 2026-09-21. Final results and independent review closures are recorded in `docs/department-isolation-validation.md`. No commits or external publication were performed.
